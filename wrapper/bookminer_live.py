"""Game book-exit events -> existing GUI enqueue, off the USI critical path."""
from __future__ import annotations
import hashlib, json, os, queue, subprocess, sys, threading, time
from pathlib import Path
import cshogi
from flip import flipped_sfen_main


class LiveBookMiner:
    def __init__(self, config, log=lambda message:None, runner=None):
        self.config=config; self.log=log; self.runner=runner or self._run
        self.outside=False; self.seen=set(); self.queue=queue.Queue()
        self.thread=None

    def configure(self, config): self.config=config

    def newgame(self):
        self.outside=False; self.seen.clear()

    def observe(self, state, command, sfen, game_id, hit=None):
        if not self.config.bookminer_live or state not in ('hit','miss'):return
        if state=='hit':
            self.outside=False
            if hit is None:return
            # The actual chosen move includes a/deep2, but the threshold and
            # ordering use RAW side-to-move values, never selection scores.
            candidates={}
            for candidate in hit.depth0_candidates:
                if candidate.depth!=0 or candidate.value<hit.value-30:continue
                if candidate.usi not in candidates or candidate.value>candidates[candidate.usi].value:
                    candidates[candidate.usi]=candidate
            board=cshogi.Board(sfen)
            for candidate in sorted(candidates.values(),key=lambda c:(-c.value,c.usi))[:3]:
                move=board.move_from_usi(candidate.usi)
                if not move or not board.is_legal(move):continue
                child=board.copy();child.push(move)
                route=command.rstrip()+(' ' if ' moves ' in command else ' moves ')+candidate.usi
                self._enqueue('position sfen '+child.sfen(),child.sfen(),game_id,
                    trigger='depth0',source_command=route,parent_sfen=sfen,
                    selected_move=hit.usi,selected_value=hit.value,candidate_move=candidate.usi,
                    candidate_value=candidate.value,threshold=hit.value-30,book_file=hit.file_name)
            return
        if self.outside:return
        self.outside=True
        self._enqueue(command,sfen,game_id,trigger='book_exit')

    def _enqueue(self,command,sfen,game_id,**metadata):
        main=' '.join(sfen.split()[:3]);flip=flipped_sfen_main(main)
        canonical=min(main,flip) if flip else main
        ident='kc-live-'+hashlib.sha256(canonical.encode()).hexdigest()[:40]
        if ident in self.seen:return
        self.seen.add(ident)
        event={'id':ident,'command':command,'sfen':sfen,'game_id':game_id,'created_unix':time.time(),
               'journal_dir':str(Path(self.config.log_dir)/'bookminer-live'),'work':self.config.bookminer_work,
               **metadata}
        self.queue.put_nowait(event)  # Unbounded: no game/task/worker count limit.
        if self.thread is None:
            self.thread=threading.Thread(target=self._loop,daemon=True,name='BookMinerLive')
            self.thread.start()

    def _loop(self):
        while True:
            event=self.queue.get()
            try:
                result=self.runner(event)
                receipt=result.get('receipt',{})
                self.log(f"[BookMinerLive] {result.get('state')} id={event['id']} job={receipt.get('job_id','-')}"
                         +(f" reason={result['reason']}" if result.get('reason') else ''))
            except Exception as exc:
                self.log(f'[BookMinerLive] 送信確認失敗（対局は続行・同じIDで照会）: {event["id"]}: {exc!r}')
            finally:self.queue.task_done()

    @staticmethod
    def _run(event):
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0) if os.name=='nt' else 0
        done=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--deliver'],
            input=json.dumps(event),text=True,encoding='utf-8',errors='replace',capture_output=True,
            timeout=30,creationflags=flags)
        if done.returncode:raise RuntimeError(done.stderr or done.stdout or f'worker exit {done.returncode}')
        return json.loads(done.stdout)


def deliver(event):
    """Isolated worker: durable ID, journal and GUI receipt; no miner lifecycle."""
    tools=Path(__file__).resolve().parents[1]/'YOSC/tools'
    sys.path.insert(0,str(tools))
    from gui_control import ControlInbox,live_status,submit
    from gui_workflow import decode
    from compass_tasks_worker import atomic,exclusive,read_json,GAME_EVAL_LIMIT
    work=Path(event['work']);folder=Path(event['journal_dir']);ident=event['id']
    with exclusive(folder/(ident+'.lock')):
        path=folder/(ident+'.json');old=read_json(path,{})
        inbox=ControlInbox(work);existing=inbox.result(ident)
        def save(state,**fields):
            record=(old or event)|{'state':state,'updated_unix':time.time()}|fields
            if old.get('state')=='manual_hold' and state!='manual_hold':
                record['previous_exclusion']={'state':'manual_hold','reason':old.get('reason'),
                                              'updated_unix':old.get('updated_unix')}
                if 'reason' not in fields:record.pop('reason',None)
            atomic(path,(json.dumps(record,ensure_ascii=False,indent=2)+'\n').encode('utf-8'))
            return record
        if existing:return save(existing['state'],receipt=existing,reused=True)
        # A timeout/crash must not turn an uncertain input into a new request.
        for name in ('inbox','processing'):
            if (inbox.root/name/(ident+'.json')).exists():return save('queued',reused=True)
        if old.get('state') in ('submitting','queued','received','submitted','unknown','accepted'):
            return save('unknown',reason='Previous possible delivery has no receipt; do not resend')
        source=old if old.get('command') else event
        board,command=decode(source['command'])
        if board.sfen()!=source['sfen']:raise ValueError('Position history and SFEN differ')
        if source.get('source_command'):
            origin,_=decode(source['source_command'])
            if origin.sfen()!=board.sfen():raise ValueError('Candidate origin history and SFEN differ')
        # Research-goal holds do not exclude our actual games (2026-11-02).
        # This live-game route is separate from scheduled study/Compass intake.
        if board.move_number>=200 or board.is_game_over() or board.is_nyugyoku():
            return save('skipped',reason='Existing native 200-ply/terminal boundary')
        status=live_status(work)
        if not status.get('gui_alive') or not status.get('running') or status.get('closing'):
            return save('not_submitted',reason='GUI stopped/unavailable/closing; never start it')
        # Book exits retain the full history to fill missing prefixes. Depth0
        # alternatives explicitly start at the child SFEN; the parent is known.
        row=command+f', book_extend_ply=6, eval_limit={GAME_EVAL_LIMIT}, game_ply_limit=200'
        payload=(row+'\n').encode('utf-8');digest=hashlib.sha256(payload).hexdigest()
        frozen=folder/(ident+'.txt');atomic(frozen,payload)
        save('submitting',input=row,sha256=digest,miner_pid=status.get('miner_pid'))
        receipt=submit(work,'enqueue',frozen,ident);until=time.monotonic()+20
        while receipt.get('state') not in ('accepted','rejected','expired','unknown') and time.monotonic()<until:
            time.sleep(.1);receipt=inbox.result(ident) or receipt
        if receipt['state']=='accepted':
            if receipt.get('count')!=1 or receipt.get('sha256')!=digest or Path(receipt['path']).read_text(encoding='utf-8-sig')!=payload.decode():
                return save('unknown',receipt=receipt,reason='Receipt content mismatch; do not resend')
        return save(receipt['state'],input=row,sha256=digest,receipt=receipt)


if __name__=='__main__':
    try:print(json.dumps(deliver(json.load(sys.stdin)),ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'state':'error','reason':str(exc)}),file=sys.stderr);sys.exit(1)
