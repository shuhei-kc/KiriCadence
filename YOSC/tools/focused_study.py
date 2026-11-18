"""User-selected routes with bounded, read-only KiriCompass precedents.

The official pn candidate set, six-ply native mining and peta solver are reused.
The supplied route is replayed before each pn frontier, including transpositions.
"""
from __future__ import annotations
import collections, json, re, sqlite3, sys, time, uuid
from pathlib import Path
import cshogi
from tuyoura_priority import ROOT, PetaView, key, position_id, play, native_plan, sha
from tuyoura_game_candidates import GameAllocator, augment
from tuyoura_mining import atomic, read
from flip import flipped_sfen_main, flipped_move

KIRICOMPASS = ROOT.parents[1] / 'KiriCompass'
sys.path.insert(0, str(KIRICOMPASS))
from kifudb.board import position_key_from_sfen, move16_to_usi


def parse_routes(text):
    chunks = re.findall(r'position\s+startpos\s+moves\s+(.*?)(?=position\s+startpos\s+moves|\Z)', text, re.S)
    if not chunks:
        raise ValueError('No supplied position routes')
    routes = []
    for i, chunk in enumerate(chunks, 1):
        moves = chunk.split()
        board = play('startpos', moves)
        routes.append({'id': f'route-{i:02}', 'label': f'指定手順{i}', 'moves': moves,
                       'root_sfen': board.sfen(), 'position_id': position_id(board), 'role': 'supplied'})
    return routes


def make_manifest(attachment, output):
    text = Path(attachment).read_text(encoding='utf-8-sig')
    routes = parse_routes(text)
    main = routes[0]['moves'][:53]
    if len(main) != 53 or main[-1] != '6h6g':
        raise ValueError('The requested main route must end at 53.6h6g')
    anchors = []
    for length in (47, 48, 50, 52, 53):
        moves = main[:length]; board = play('startpos', moves)
        anchors.append({'id': f'anchor-{length:02}', 'label': f'共通進行{length}手後', 'moves': moves,
                        'root_sfen': board.sfen(), 'position_id': position_id(board), 'role': 'backbone'})
    result = {'created_unix': time.time(), 'source_attachment': str(attachment), 'source_sha256': sha(attachment),
              'main_moves': main, 'tasks': anchors + routes,
              'reference_games_per_first_move': 8, 'reference_first_moves': 8, 'reference_plies': 24,
              'target': 'Improve current black peta values and compare preferred black moves with legal alternatives; no forced book ranking'}
    atomic(output, result)
    return result


def reference_rows(db, board, per_move=8, move_limit=8):
    keys = [position_key_from_sfen(key(board)), position_key_from_sfen(flipped_sfen_main(key(board)))]
    where = 'pg.position_key in (?,?) and g.result in (0,1,2) and g.end_reason != ?'
    counts = db.execute('select pg.next_move,count(distinct pg.game_id) from position_games pg join games g using(game_id) where ' + where + ' group by pg.next_move order by 2 desc,1', (*keys,'task')).fetchall()
    counts = [x for x in counts if x[0]]
    choices = []; used = set()
    for n in range(min(move_limit, len(counts))):
        order = counts if n % 2 == 0 else list(reversed(counts))
        item = next(x for x in order if x[0] not in used)
        choices.append(item); used.add(item[0])
    rows = []; games = set()
    for code, count in choices:
        added = 0
        for row in db.execute('select pg.game_id,pg.ply,g.event,g.result,g.initial_sfen,g.moves from position_games pg join games g using(game_id) where ' + where + ' and pg.next_move=? order by pg.sort_key desc,pg.game_id desc,pg.ply limit ?', (*keys,'task',code,per_move*8)):
            if row[0] in games:
                continue
            rows.append(row); games.add(row[0])
            # Per-move sampling uses distinct game ids, not occurrence counts.
            added += 1
            if added >= per_move:
                break
    return rows, counts


def extract_references(manifest_path, db_path, output):
    manifest = read(manifest_path); output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    source = sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True)
    source.execute('pragma query_only=ON')
    dest = sqlite3.connect(output)
    dest.executescript('''create table metadata(key text primary key,value text);
    create table tasks(id text primary key,label text,sfen text,route text,games integer);
    create table nodes(task text,sfen text,visits integer,black_wins integer,white_wins integer,other integer,ply integer,route text,primary key(task,sfen));
    create table edges(task text,parent text,move text,child text,visits integer,primary key(task,parent,move));
    create index node_priority on nodes(task,visits desc,ply);
    create index edge_parent on edges(task,parent,visits desc);''')
    summary = []; all_ids = set()
    try:
        for task in manifest['tasks']:
            root = cshogi.Board(task['root_sfen']); nodes = {}; edges = collections.Counter(); verified = []
            rows, counts = reference_rows(source, root, manifest['reference_games_per_first_move'],manifest['reference_first_moves'])
            for gid, at, event, result, initial, blob in rows:
                import struct
                moves = [move16_to_usi(x[0]) for x in struct.iter_unpack('<H', blob)]
                origin = initial or 'startpos'; original = play(origin, moves[:at])
                is_flip = key(original) == flipped_sfen_main(key(root))
                if key(original) != key(root) and not is_flip:
                    raise ValueError('Precedent hash/index replay mismatch')
                continuation = moves[at:at+manifest['reference_plies']]
                if is_flip:
                    continuation = [flipped_move(m) for m in continuation]
                    result = 3-result if result in (1,2) else result
                board = cshogi.Board(root.sfen()); local_nodes = {}; local_edges = set(); played=[]
                for offset, move in enumerate(continuation):
                    if board.move_number >= 200:
                        break
                    parent = key(board)
                    parsed = board.move_from_usi(move)
                    if not parsed or not board.is_legal(parsed):
                        raise ValueError('Illegal precedent continuation')
                    local_nodes.setdefault(parent,(board.move_number,' '.join(task['moves']+played)))
                    board.push(parsed); played.append(move); local_edges.add((parent,move,key(board)))
                local_nodes.setdefault(key(board),(board.move_number,' '.join(task['moves']+played)))
                for sfen,(ply,route) in local_nodes.items():
                    item = nodes.setdefault(sfen,[0,0,0,0,ply,route]); item[0]+=1
                    item[1 if result==1 else 2 if result==2 else 3]+=1
                for edge in local_edges:
                    edges[edge]+=1
                verified.append({'game_id':gid,'event':event,'indexed_ply':at,'flipped':is_flip}); all_ids.add(gid)
            # With no observed game, one is an explicit policy scale, not a
            # fabricated game/visit. The study plan exposes true count zero.
            dest.execute('insert into tasks values (?,?,?,?,?)',(task['id'],task['label'],task['root_sfen'],' '.join(task['moves']),max(1,len(verified))))
            dest.executemany('insert into nodes values (?,?,?,?,?,?,?,?)',[(task['id'],sfen,*v) for sfen,v in nodes.items()])
            dest.executemany('insert into edges values (?,?,?,?,?)',[(task['id'],*edge,count) for edge,count in edges.items()])
            summary.append({'id':task['id'],'verified_games':verified,'sampled_game_count':len(verified),'first_move_counts':counts,'nodes':len(nodes),'edges':len(edges)})
        meta = {'source':str(db_path),'candidate_source_kind':'kiricompass_sfen','completed_games':len(all_ids),'task_count':len(summary),'scope':'distinct completed games, bounded per first move; pseudo task prefixes excluded','tasks':summary}
        dest.execute('insert into metadata values (?,?)',('source',json.dumps(meta,ensure_ascii=False)));dest.commit()
    finally:
        source.close();dest.close()
    return meta


def build_plan(manifest_path, frequency, peta, output, excluded=(), min_pool=48, max_diff=240):
    manifest = read(manifest_path); output = Path(output); output.parent.mkdir(parents=True,exist_ok=True)
    token = uuid.uuid4().hex[:6]
    native_output = output.with_name(output.stem+'-native-'+token+'.json')
    plan = native_plan(frequency,peta,native_output,min_pool=min_pool,max_diff=max_diff)
    result = augment(plan,frequency,output,excluded)
    db = sqlite3.connect(Path(frequency).resolve().as_uri()+'?mode=ro',uri=True)
    meta = json.loads(db.execute("select value from metadata where key='source'").fetchone()[0]); db.close()
    refs = {t['id']:t['sampled_game_count'] for t in meta['tasks']}
    by_id = {t['id']:t for t in manifest['tasks']}
    for task in result['tasks']:
        specified = by_id[task['id']]
        task.update(role=specified['role'],input_moves=specified['moves'],weight_scale_games=task['games'],games=refs[task['id']])
        for item in task['pool']+task['game_pool']:
            item['position_command'] = 'position startpos moves '+' '.join(specified['moves']+item['peta_route'])
            item['requested_route_id'] = task['id']
    pv = PetaView(peta)
    try:
        comparisons = []
        main = manifest['main_moves']
        for at in (48,50,52):
            board = play('startpos',main[:at]);moves=pv.moves(board);target=main[at]
            value = next((v for m,v,d in moves if m==target),None)
            best = max((v for m,v,d in moves),default=None)
            comparisons.append({'played_ply':at+1,'preferred_move':target,'preferred_black_cp':value,'best_black_cp':best,'gap_cp':best-value if best is not None and value is not None else None})
    finally:
        pv.close()
    result['focused_study']={'manifest':str(manifest_path),'source_sha256':manifest['source_sha256'],'supplied_route_count':sum(t['role']=='supplied' for t in manifest['tasks']),'backbone_comparisons':comparisons,'bounded_reference_games':meta['completed_games']}
    atomic(output,result)
    return result


class FocusedAllocator(GameAllocator):
    def __init__(self,plan,seen=(),cursors=None,slot=0,task_counts=None,game_seen=None,game_counts=None):
        # A just-submitted unknown root may still be absent from the older
        # snapshot being converted. Do not submit its pilot again each epoch.
        counts=task_counts or {}
        allocation_plan=dict(plan,unknown=[tid for tid in plan['unknown'] if not counts.get(tid,0)])
        super().__init__(allocation_plan,seen,cursors,slot,task_counts,game_seen,game_counts)

    def wave(self,count):
        # Unknown supplied roots must receive pilot work, rather than waiting
        # indefinitely behind the nonempty known-root pools.
        pilot=[]
        for _ in range(min(4,count)):
            item=self.one('unknown')
            if item is None:
                break
            pilot.append(item)
        return pilot+super().wave(count-len(pilot))
