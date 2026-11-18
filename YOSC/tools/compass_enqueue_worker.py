"""BookMiner-side one-line enqueue; no recurring roots are registered."""
from __future__ import annotations
import hashlib, json, re, sys, time
from pathlib import Path
from compass_tasks_worker import (COMPASS, DEFAULT_YOSC, BEGIN, END, atomic, read_json,
    exclusive, dependencies, name_settings, matched_loss, first_boundary, split_block,
    render_roots, reconcile_acknowledgements as legacy_acknowledgements)
from compass_tasks_worker import GAME_EVAL_LIMIT


class GuiTransport:
    def __init__(self,work):
        from gui_control import ControlInbox, submit
        self.work=work; self.inbox=ControlInbox(work); self.send=submit
    def result(self,ident):
        return self.inbox.result(ident)
    def submit(self,path,ident):
        return self.send(self.work,'enqueue',path,ident)


def receipt_update(item,result):
    if not result: return
    phase=result.get('state'); item['receipt_state']=phase
    if phase=='accepted':
        item.update(phase='accepted',accepted=1,job_id=result['job_id'],
                    accepted_unix=result.get('updated_unix',time.time()),accepted_path=result.get('path'))
    elif phase in ('unknown','rejected','expired'):
        item.update(phase=phase,error=result.get('error',phase))


def migrate_roots(work,state,folder):
    """One-time cleanup of the former owned block; preserve manual roots."""
    legacy_acknowledgements(work,state)
    path=work/'book/peta_start_sfens.txt'; before=path.read_bytes()
    _,owned,_=split_block(before)
    actual=[line.strip() for line in owned.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if actual and actual!=state.get('owned_commands',[]) and not (
        state.get('roots_write_pending') and actual==state.get('previous_owned_commands',[])):
        raise ValueError('旧専用範囲が手動編集されています。開始局面は変更していません。')
    content,_=render_roots(before,[])
    atomic(folder/'state-before-direct-migration.json',json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
    if content!=before:
        if path.read_bytes()!=before: raise RuntimeError('開始局面の同時編集を検出しました。')
        atomic(folder/'roots-before-direct-migration.txt',before); atomic(path,content)
    for item in state['candidates']:
        if item['phase']=='active':
            item['phase']='accepted' if item.get('accepted') else 'pending'
            if item.get('accepted'): item['origin']='legacy_auto_root'
    state.update(version=2,mode='one_shot_enqueue',owned_commands=[],previous_owned_commands=[],roots_write_pending=False)


def completions(work,state,status):
    path=work/'gui.log'
    if not path.exists(): return
    size=path.stat().st_size; offset=state.get('log_offset',size)
    if offset>size: offset=0
    state['log_offset']=offset
    with path.open('rb') as stream:
        stream.seek(offset); data=stream.read(4<<20)
    end=data.rfind(b'\n')
    if end<0:return
    state['log_offset']=offset+end+1
    for line in data[:end+1].decode('utf-8',errors='replace').splitlines():
        if '[TaskQueueJobDone]' not in line and '[TaskQueueDone]' not in line:continue
        match=re.search(r'\bjob=(\d+)\b',line)
        if not match:continue
        for item in state['candidates']:
            if item.get('job_id')==int(match.group(1)) and item.get('miner_pid')==status.get('miner_pid'):
                item.update(completed=True,completed_unix=time.time())


def outstanding(state,status):
    return sum(item['phase'] in ('submitting','submitted') or
               (item['phase']=='accepted' and not item.get('completed') and item.get('miner_pid')==status.get('miner_pid'))
               for item in state['candidates'])


def cycle(db_path,*,yosc=DEFAULT_YOSC,state_dir=None,enabled=True,max_active=4,task_budget=2,
          log=lambda message:None,deps=None,transport=None,ack_wait=3):
    if not 1<=max_active<=16 or not 1<=task_budget<=32:raise ValueError('投入上限の範囲が不正です。')
    work=Path(yosc).resolve()/'runtime/gui'; folder=Path(state_dir or COMPASS/'runtime/bookminer-tasks')
    deps=deps or dependencies(yosc)
    cshogi,PetaView,position_id,decode,live_status=deps
    with exclusive(folder/'cycle.lock'):
        state=read_json(folder/'state.json',{'version':1,'cursors':{},'candidates':[]})
        status=live_status(work)
        if state.get('version',1)<2:migrate_roots(work,state,folder)
        transport=transport or GuiTransport(work)
        for item in state['candidates']:
            if item.get('request_id'):receipt_update(item,transport.result(item['request_id']))
        completions(work,state,status)
        if enabled:
            from kifudb.query import PrecedentReader
            from kifudb.db import resolve_db_path
            filters,draw=name_settings(Path(yosc).parent/'makebook/config.txt')
            if not filters:raise ValueError('CadenceのNameFiltersが空です。')
            reader=PrecedentReader(db_path);reader.conn.execute('PRAGMA query_only=ON')
            identity=str(resolve_db_path(db_path).resolve())
            maximum=reader.conn.execute('SELECT COALESCE(MAX(game_id),0) FROM games').fetchone()[0]
            if identity not in state['cursors'] or state['cursors'][identity]>maximum:
                state['cursors'][identity]=maximum
                log('新着監視を開始しました。過去の全棋譜は投入しません。')
            current=read_json(work/'current.json',{})
            pv=None
            try:
                pv=PetaView(current['peta'])
                known={item['id'] for item in state['candidates']}
                ids=reader.conn.execute('SELECT game_id FROM games WHERE game_id>? ORDER BY game_id LIMIT 256',
                                       (state['cursors'][identity],)).fetchall()
                for (gid,) in ids:
                    game=reader.get_game(gid)
                    if game and matched_loss(game,filters,draw):
                        try:item=first_boundary(game,pv,deps)
                        except ValueError as exc:log(f'棋譜 {gid} を除外: {exc}');item=None
                        if item and item['id'] not in known:
                            item.update(phase='pending',accepted=0,created_unix=time.time(),database=identity,
                                        peta=current['peta'],publication=current.get('publication'))
                            state['candidates'].append(item);known.add(item['id'])
                    state['cursors'][identity]=gid
            finally:
                if pv:pv.close()
                reader.close()
            state['filters']=filters
        ready=bool(status.get('gui_alive') and status.get('running') and not status.get('closing'))
        sent=0
        for item in state['candidates']:
            if not enabled or not ready or sent>=task_budget:break
            if item['phase'] not in ('pending','submitting','submitted'):continue
            # Goal exclusions apply to AI research, not game-derived intake.
            # Pending input is finite. Once accepted, this candidate is never
            # automatically extended, regenerated, or submitted with another ID.
            ident=item.setdefault('request_id','kc-'+hashlib.sha256(item['id'].encode()).hexdigest()[:40])
            if item['phase']=='pending':
                if outstanding(state,status)>=max_active:break
                status=live_status(work)
                if not status.get('gui_alive') or not status.get('running') or status.get('closing'):break
                payload=folder/'payloads'/(ident+'.txt')
                if not payload.exists():
                    row=item['command']+f', book_extend_ply=6, eval_limit={GAME_EVAL_LIMIT}, game_ply_limit=200'
                    atomic(payload,(row+'\n').encode('utf-8'))
                item.update(phase='submitting',payload=str(payload),miner_pid=status.get('miner_pid'))
                atomic(folder/'state.json',json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
            result=transport.submit(Path(item['payload']),ident)
            item['phase']='submitted';receipt_update(item,result)
            deadline=time.monotonic()+ack_wait
            while item['phase']=='submitted' and time.monotonic()<deadline:
                receipt_update(item,transport.result(ident))
                if item['phase']=='submitted':time.sleep(.1)
            sent+=1
            atomic(folder/'state.json',json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
            if item['phase']=='submitted':break
            log(f"1件投入: game={item['source_game_id']} state={item['phase']} job={item.get('job_id','?')}")
        state.update(updated_unix=time.time(),enabled=enabled,ready=ready,max_active=max_active,task_budget=task_budget)
        summary={'waiting':sum(item['phase']=='pending' for item in state['candidates']),
                 'active':outstanding(state,status),'accepted':sum(item.get('accepted',0) for item in state['candidates']),
                 'retired':sum(bool(item.get('completed')) for item in state['candidates']),
                 'errors':sum(item['phase'] in ('unknown','rejected','expired') for item in state['candidates']),
                 'ready':ready,'enabled':enabled,'state_file':str(folder/'state.json')}
        atomic(folder/'state.json',json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
        atomic(folder/'latest.json',json.dumps(summary,ensure_ascii=False,indent=2).encode('utf-8'))
        rows=['phase\taccepted\tjob\tcompleted\tgame_id\tply\tblack\twhite\tposition']
        rows+=['\t'.join(map(str,(item['phase'],item.get('accepted',0),item.get('job_id',''),item.get('completed',False),
                                item['source_game_id'],item['ply'],item['black_name'],item['white_name'],item['command']))) for item in state['candidates']]
        atomic(folder/'tasks.tsv',('\n'.join(rows)+'\n').encode('utf-8-sig'))
        return summary


def main():
    messages=[]
    try:
        request=json.load(sys.stdin)
        if request.get('protocol')!=1:raise ValueError('Unsupported bridge protocol')
        if request.get('probe'):
            dependencies(request.get('yosc',DEFAULT_YOSC));summary={'dependencies_ready':True}
        else:
            summary=cycle(request['db_path'],yosc=request.get('yosc',DEFAULT_YOSC),state_dir=request['state_dir'],
                          enabled=request.get('enabled',True),max_active=request.get('max_active',4),
                          task_budget=request.get('task_budget',2),log=messages.append)
        summary['worker_python']=sys.executable
        response={'ok':True,'protocol':1,'summary':summary,'messages':messages};code=0
    except Exception as exc:response={'ok':False,'protocol':1,'error':str(exc),'messages':messages};code=1
    print(json.dumps(response,ensure_ascii=False));return code


if __name__=='__main__':raise SystemExit(main())
