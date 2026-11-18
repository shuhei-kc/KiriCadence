"""BookMiner-side worker: named losses at their first missing peta position.

This only edits an owned block in peta_start_sfens. The running BookMiner GUI
does all planning and bounded refill; no miner, engine, or direct enqueue here.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
import sys

DEFAULT_YOSC = Path(__file__).resolve().parents[1]
COMPASS = DEFAULT_YOSC.parent.parent/'KiriCompass'
if str(COMPASS) not in sys.path:
    sys.path.insert(0,str(COMPASS))
BEGIN = '# BEGIN KiriCompass BookMiner tasks'
END = '# END KiriCompass BookMiner tasks'


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temp.write_bytes(data)
    os.replace(temp, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell()==0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name=='nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def dependencies(yosc):
    # Code belongs to this worker installation; yosc selects its data/workdir.
    tools = Path(__file__).resolve().parent
    if not (tools/'gui_workflow.py').is_file():
        raise ValueError('BookMiner連携先のYOSCが見つかりません。')
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import cshogi
    from tuyoura_priority import PetaView, position_id
    from gui_workflow import decode
    from gui_control import live_status
    return cshogi, PetaView, position_id, decode, live_status


def name_settings(path):
    values = {}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        parts = line.split(None, 1)
        if len(parts)==2:
            values[parts[0]] = parts[1].strip()
    filters = re.split(r'[\s,]+', values.get('NameFilters','').strip())
    return [name for name in filters if name], values.get('DrawWinner','white').lower()


def matched_loss(game, filters, draw_winner):
    if game.source!='floodgate' or game.end_reason not in ('toryo','tsumi','kachi','sennichite','jishogi','hikiwake'):
        return False
    loser = 2 if game.result==1 else 1 if game.result==2 else (
        1 if draw_winner=='white' else 2) if game.result==0 else None
    name = game.black_name if loser==1 else game.white_name if loser==2 else ''
    return bool(name and any(token.lower() in name.lower() for token in filters))


GAME_EVAL_LIMIT = 2000


def first_boundary(game, pv, deps):
    cshogi, _, position_id, _, _ = deps
    board = cshogi.Board(game.initial_sfen) if game.initial_sfen else cshogi.Board()
    origin = 'sfen '+game.initial_sfen if game.initial_sfen else 'startpos'
    # Verify the entire source before trusting any of its prefix.
    for move in game.moves_usi:
        parsed = board.move_from_usi(move)
        if not parsed or not board.is_legal(parsed):
            raise ValueError('棋譜に非合法手があります: '+move)
        board.push(parsed)
    board = cshogi.Board(game.initial_sfen) if game.initial_sfen else cshogi.Board()
    previous = pv.moves(board)
    if not previous:
        return None  # No known-book prefix; do not register arbitrary whole games.
    for index, move in enumerate(game.moves_usi):
        prior_value = next((v for m,v,d in previous if m==move), None)
        board.push_usi(move)
        current = pv.moves(board)
        if not current:
            if board.move_number>=200 or board.is_game_over() or board.is_nyugyoku():
                return None
            if prior_value is not None and abs(prior_value)>GAME_EVAL_LIMIT:
                return None  # Existing native eval_limit would refuse this frontier.
            return {'id':position_id(board),'command':'position '+origin+' moves '+' '.join(game.moves_usi[:index+1]),
                    'ply':board.move_number-1,'sfen':board.sfen(),'last_game_move':move,
                    'parent_move_value':prior_value,'source_game_id':game.game_id,
                    'event':game.event,'black_name':game.black_name,'white_name':game.white_name,
                    'result':game.result,'source_legal':True}
        previous = current
    return None


def split_block(data):
    text = data.decode('utf-8-sig')
    lines = text.splitlines(keepends=True)
    starts = [i for i,line in enumerate(lines) if line.strip()==BEGIN]
    ends = [i for i,line in enumerate(lines) if line.strip()==END]
    if not starts and not ends:
        return text, '', ''
    if len(starts)!=1 or len(ends)!=1 or starts[0]>=ends[0]:
        raise ValueError('KiriCompass管理範囲のコメントが壊れています。開始局面ファイルは変更しません。')
    a,b = starts[0],ends[0]
    return ''.join(lines[:a]), ''.join(lines[a+1:b]), ''.join(lines[b+1:])


def render_roots(before, commands):
    prefix, old, suffix = split_block(before)
    bom = b'\xef\xbb\xbf' if before.startswith(b'\xef\xbb\xbf') else b''
    if not commands:
        return bom+(prefix+suffix).encode('utf-8'), old
    if prefix and not prefix.endswith('\n'):
        prefix += '\n'
    return bom+(prefix+BEGIN+'\n'+''.join(c+'\n' for c in commands)+END+'\n'+suffix).encode('utf-8'), old


def reconcile_acknowledgements(work, state):
    """Only path-correlated native acknowledgements count, never planned tasks."""
    log = work/'gui.log'
    if not log.exists():
        return
    size = log.stat().st_size
    offset = state.get('log_offset',size)
    if offset>size:
        offset = 0
    state['log_offset'] = offset
    with log.open('rb') as stream:
        stream.seek(offset)
        chunk = stream.read(4<<20)
    end = chunk.rfind(b'\n')
    if end<0:
        return
    state['log_offset'] = offset+end+1
    by_id = {item['id']:item for item in state['candidates']}
    for line in chunk[:end+1].decode('utf-8',errors='replace').splitlines():
        if '[TaskQueueStart]' not in line:
            continue
        match = re.search(r'\bpath=(.*?)\s+deferred=',line)
        if not match:
            continue
        path = Path(match.group(1))
        if not path.is_absolute():
            path = work/path
        if not path.resolve().is_relative_to((work/'book/auto').resolve()):
            continue
        journal = read_json(path.with_suffix('.json'), {})
        if journal.get('source')!='auto':
            continue
        # Input files are immutable and have unique paths. Persisted log_offset
        # makes rereading after a restart idempotent.
        for task in journal.get('batch',[]):
            item = by_id.get(task.get('root'))
            if item and item['phase']=='active':
                item['accepted'] += 1
                item['last_accepted_path'] = str(path)


def cycle(db_path, *, yosc=DEFAULT_YOSC, state_dir=None, enabled=True,
          max_active=4, task_budget=2, log=lambda message:None, deps=None):
    """One serialized GUI job; no periodic thread or process of its own."""
    if not 1<=max_active<=16 or not 1<=task_budget<=32:
        raise ValueError('同時課題は1～16、交代目安は1～32件を指定してください。')
    yosc = Path(yosc).resolve()
    work = yosc/'runtime/gui'
    state_dir = Path(state_dir or COMPASS/'runtime/bookminer-tasks')
    with exclusive(state_dir/'cycle.lock'):
        state = read_json(state_dir/'state.json', {'version':1,'cursors':{},'candidates':[]})
        root_path = work/'book/peta_start_sfens.txt'
        before = root_path.read_bytes()
        prefix, owned, suffix = split_block(before)
        if 'owned_commands' not in state and owned.strip():
            raise ValueError('台帳のない管理範囲があるため変更しません。')
        actual = [line.strip() for line in owned.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        if actual!=state.get('owned_commands',[]) and not (
            state.get('roots_write_pending') and actual==state.get('previous_owned_commands',[])):
            raise ValueError('管理範囲が手動編集されています。入力を保持して停止しました。')
        if enabled:
            deps = deps or dependencies(yosc)
            cshogi, PetaView, position_id, decode, live_status = deps
            from kifudb.query import PrecedentReader
            from kifudb.db import resolve_db_path
            filters, draw_winner = name_settings(yosc.parent/'makebook/config.txt')
            if not filters:
                raise ValueError('CadenceのNameFiltersが空です。')
            reader = PrecedentReader(db_path)
            reader.conn.execute('PRAGMA query_only=ON')
            identity = str(resolve_db_path(db_path).resolve())
            maximum = reader.conn.execute('SELECT COALESCE(MAX(game_id),0) FROM games').fetchone()[0]
            if identity not in state['cursors'] or state['cursors'][identity]>maximum:
                # Subscribe to future imports; never backfill an entire old DB.
                state['cursors'][identity] = maximum
                log('新着監視を開始しました。既存の全棋譜は投入しません。')
            reconcile_acknowledgements(work,state)
            current = read_json(work/'current.json',{})
            if not current.get('peta') or not Path(current['peta']).is_file():
                reader.close()
                raise ValueError('完成したペタが見つかりません。')
            pv = PetaView(current['peta'])
            known = {item['id'] for item in state['candidates']}
            try:
                ids = reader.conn.execute('SELECT game_id FROM games WHERE game_id>? ORDER BY game_id LIMIT 256',
                                          (state['cursors'][identity],)).fetchall()
                for (gid,) in ids:
                    game = reader.get_game(gid)
                    if game and matched_loss(game,filters,draw_winner):
                        try:
                            item = first_boundary(game,pv,deps)
                        except ValueError as exc:
                            log(f'棋譜 {gid} を除外: {exc}')
                            item = None
                        if item and item['id'] not in known:
                            item.update(phase='pending',accepted=0,created_unix=time.time(),database=identity,
                                        peta=current['peta'],publication=current.get('publication'))
                            state['candidates'].append(item)
                            known.add(item['id'])
                    state['cursors'][identity] = gid
            finally:
                pv.close(); reader.close()
            manual = set()
            for line in (prefix+suffix).splitlines():
                if line.strip() and not line.lstrip().startswith('#'):
                    manual.add(position_id(decode(line)[0]))
            status = live_status(work)
            ready = status.get('gui_alive') and status.get('running') and not status.get('closing') and status.get('auto_fill')
            for item in state['candidates']:
                if item['phase']=='active' and item['accepted']>=task_budget:
                    item['phase']='retired'  # registration turn ended; mining may still be active
                elif item['phase'] in ('active','pending') and item['id'] in manual:
                    item['phase']='existing_manual_root'
            if ready:
                active_count = sum(item['phase']=='active' for item in state['candidates'])
                for item in state['candidates']:
                    if active_count>=max_active:
                        break
                    if item['phase']=='pending':
                        item['phase']='active'; active_count += 1
            # Reducing the visible limit returns excess registrations to waiting.
            active = [item for item in state['candidates'] if item['phase']=='active']
            for item in active[max_active:]:
                item['phase']='pending'
            state['filters'] = filters
            state['ready'] = bool(ready)
        commands = [item['command'] for item in state['candidates'] if item['phase']=='active'] if enabled else []
        content,_ = render_roots(before,commands)
        state.update(owned_commands=commands,updated_unix=time.time(),enabled=enabled,
                     max_active=max_active,task_budget=task_budget)
        summary = {'waiting':sum(item['phase']=='pending' for item in state['candidates']),
                   'active':len(commands),'accepted':sum(item['accepted'] for item in state['candidates']),
                   'retired':sum(item['phase']=='retired' for item in state['candidates']),
                   'ready':state.get('ready',False),'enabled':enabled,'state_file':str(state_dir/'state.json')}
        # Persist desired membership first; writing the same block is idempotent
        # after a crash between the state save and the root-file replacement.
        state['previous_owned_commands'] = actual
        state['roots_write_pending'] = content!=before
        atomic(state_dir/'state.json',json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
        if content!=before:
            if root_path.read_bytes()!=before:
                # Restore the last observed ownership expectation for a clean retry.
                state['owned_commands'] = actual
                state['roots_write_pending'] = False
                atomic(state_dir/'state.json',json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
                raise RuntimeError('開始局面が同時編集されたため書き換えませんでした。次回再確認します。')
            atomic(state_dir/'roots-before-last-change.txt',before)
            atomic(root_path,content)
        state['roots_write_pending'] = False
        atomic(state_dir/'state.json',json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
        atomic(state_dir/'latest.json',json.dumps(summary,ensure_ascii=False,indent=2).encode('utf-8'))
        rows = ['phase\taccepted\tgame_id\tply\tblack\twhite\tposition']
        rows += ['\t'.join(map(str,(item['phase'],item['accepted'],item['source_game_id'],item['ply'],
                                     item['black_name'],item['white_name'],item['command']))) for item in state['candidates']]
        atomic(state_dir/'tasks.tsv',('\n'.join(rows)+'\n').encode('utf-8-sig'))
        return summary


def main():
    """One JSON request/result, called by the standard-library Compass client."""
    messages = []
    try:
        request = json.load(sys.stdin)
        if request.get('protocol')!=1:
            raise ValueError('Unsupported BookMiner bridge protocol')
        if request.get('probe'):
            dependencies(request.get('yosc',DEFAULT_YOSC))
            summary = {'worker_python':sys.executable,'dependencies_ready':True}
        else:
            summary = cycle(request['db_path'],yosc=request.get('yosc',DEFAULT_YOSC),
                            state_dir=request['state_dir'],enabled=request.get('enabled',True),
                            max_active=request.get('max_active',4),task_budget=request.get('task_budget',2),
                            log=messages.append)
            summary['worker_python'] = sys.executable
        result = {'ok':True,'protocol':1,'summary':summary,'messages':messages}
        code = 0
    except Exception as exc:
        result = {'ok':False,'protocol':1,'error':str(exc),'messages':messages}
        code = 1
    sys.stdout.write(json.dumps(result,ensure_ascii=False)+'\n')
    return code


if __name__=='__main__':
    # Old connection files must also use finite enqueue, never root registration.
    from compass_enqueue_worker import main as enqueue_main
    raise SystemExit(enqueue_main())
