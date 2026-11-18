"""Read completed TuyouraOu games, rank tasks by current peta, and build mining frontiers."""
from __future__ import annotations
import argparse
from collections import Counter,OrderedDict,defaultdict
import hashlib,heapq,json,math,os,sqlite3,sys,time
from pathlib import Path
import cshogi
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent/'wrapper'))
from yaneuraou_book_lib import YbbBookProbe,pack_sfen
from flip import flipped_sfen_main,flipped_move


def key(board):return ' '.join(board.sfen().split()[:3])
def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for chunk in iter(lambda:f.read(8<<20),b''):h.update(chunk)
 return h.hexdigest()
def position_id(board):
 other=cshogi.Board(flipped_sfen_main(key(board))+' 1')
 return min(pack_sfen(board),pack_sfen(other)).hex()
def play(origin,moves):
 board=cshogi.Board() if origin=='startpos' else cshogi.Board(origin.removeprefix('sfen '))
 for move in moves:
  parsed=board.move_from_usi(move)
  if not parsed or not board.is_legal(parsed):raise ValueError('Illegal recorded move: '+move)
  board.push(parsed)
 return board


def extract(config_path,completion_path,output):
 output=Path(output)
 if output.exists():raise FileExistsError(output)
 cfg=json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
 completion=json.loads(Path(completion_path).read_text(encoding='utf-8-sig'))
 accepted=set(completion['verified_task_ids'])
 base=cfg['start_position'].removeprefix('position ').split(' moves ',1)
 prefix=base[1].split() if len(base)>1 else []
 focuses={f['id']:f for f in cfg['sequential_study']['focuses'] if f['id'] in accepted}
 source=Path(cfg['runtime_state_dir'])/'state.sqlite3'
 db=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
 db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
 seq=db.execute('select coalesce(max(seq),0) from events').fetchone()[0]
 nodes={};edges=Counter();totals=Counter();excluded=Counter();ids=set()
 for row in db.execute('select payload from games order by rowid'):
  g=json.loads(row[0]);fid=g.get('focus',{}).get('id')
  if not g.get('completed') or fid not in focuses:
   excluded['unfinished_or_outside_completed_project']+=1;continue
  if g['game_id'] in ids:raise ValueError('Duplicate game identity')
  start=g['prefix_length'];moves=g['moves']
  if not 0<=start<=len(moves) or len(g.get('selections',[]))!=len(moves)-start:
   raise ValueError('Recorded selections and actual-start boundary disagree')
  board=play(g['origin'],moves[:start])
  expected=play(base[0],prefix+focuses[fid]['moves'])
  if key(board)!=key(expected):raise ValueError('Actual task root differs from configured focus: '+fid)
  local_nodes={};local_edges=set()
  for offset,move in enumerate(moves[start:]):
   at=start+offset;parent=key(board);item=g['selections'][offset]
   if item['key']!=parent or item['move']!=move:raise ValueError('Selection route mismatch')
   parsed=board.move_from_usi(move)
   if not parsed or not board.is_legal(parsed):raise ValueError('Illegal recorded route')
   local_nodes.setdefault(parent,(board.move_number,' '.join(moves[:at])))
   board.push(parsed)
   local_edges.add((parent,move,key(board)))
  # The final board has no engine selection, but remains an observed tree endpoint.
  local_nodes.setdefault(key(board),(board.move_number,' '.join(moves)))
  result=g.get('rule_result','unresolved')
  for sfen,(ply,route) in local_nodes.items():
   item=nodes.setdefault((fid,sfen),[0,0,0,0,ply,route])
   item[0]+=1
   item[1 if result=='black_win' else 2 if result=='white_win' else 3]+=1
   if ply<item[4]:item[4:]=[ply,route]
  for parent,move,child in local_edges:edges[(fid,parent,move,child)]+=1
  totals[fid]+=1;ids.add(g['game_id'])
 db.rollback();db.close()
 if sum(totals.values())!=completion['completed_games']:raise ValueError('Completed-game total differs from final project ledger')
 output.parent.mkdir(parents=True,exist_ok=True)
 temporary=output.with_suffix('.tmp.sqlite3')
 dst=sqlite3.connect(temporary)
 dst.executescript('''create table metadata(key text primary key,value text);
 create table tasks(id text primary key,label text,sfen text,route text,games integer);
 create table nodes(task text,sfen text,visits integer,black_wins integer,white_wins integer,other integer,ply integer,route text,primary key(task,sfen));
 create table edges(task text,parent text,move text,child text,visits integer,primary key(task,parent,move));
 create index node_priority on nodes(task,visits desc,ply);
 create index edge_parent on edges(task,parent,visits desc);''')
 for fid,f in focuses.items():
  board=play(base[0],prefix+f['moves'])
  dst.execute('insert into tasks values (?,?,?,?,?)',(fid,f['label'],board.sfen(),' '.join(prefix+f['moves']),totals[fid]))
 dst.executemany('insert into nodes values (?,?,?,?,?,?,?,?)',[(fid,sfen,*values) for (fid,sfen),values in nodes.items()])
 dst.executemany('insert into edges values (?,?,?,?,?)',[(*item,count) for item,count in edges.items()])
 info={'source':str(source),'source_seq':seq,'campaign_id':cfg['campaign_id'],'completed_games':sum(totals.values()),
       'task_count':len(focuses),'task_position_pairs':len(nodes),'task_edges':len(edges),'excluded':dict(excluded),
       'basis':'distinct completed games after actual task start; official outcomes; no reference-prefix counts',
       'created_unix':time.time(),'config_sha256':sha(config_path),'completion_sha256':sha(completion_path)}
 dst.execute('insert into metadata values (?,?)',('source',json.dumps(info,ensure_ascii=False)))
 dst.commit();dst.close();os.rename(temporary,output)
 return info


class PetaView:
 def __init__(self,path):self.probe=YbbBookProbe(path);self.cache=OrderedDict()
 def moves(self,board):
  sfen=key(board)
  if sfen in self.cache:self.cache.move_to_end(sfen);return self.cache[sfen]
  raw=self.probe.probe(board.sfen());flipped=False
  if not raw:raw=self.probe.probe(flipped_sfen_main(sfen)+' 1');flipped=bool(raw)
  result=[]
  for m in raw or []:
   move=flipped_move(m.move) if flipped else m.move
   parsed=board.move_from_usi(move)
   if not parsed or not board.is_legal(parsed):raise ValueError('Illegal peta move')
   result.append((move,m.value,m.depth))
  self.cache[sfen]=result
  if len(self.cache)>32768:self.cache.popitem(last=False)
  return result
 def black_value(self,board):
  moves=self.moves(board)
  return max(m[1] for m in moves)*(1 if board.turn==0 else -1) if moves else None
 def close(self):self.probe.close()


def low_groups(tasks):
 known=[t for t in tasks if t['black_cp'] is not None]
 unknown=[t['id'] for t in tasks if t['black_cp'] is None]
 known.sort(key=lambda t:(t['black_cp'],t['id']))
 cutoff=known[(len(known)-1)//2]['black_cp'] if known else None
 low=[t['id'] for t in known if t['black_cp']<=cutoff]
 high=[t['id'] for t in known if t['black_cp']>cutoff]
 return low,high,unknown,cutoff


def plan(frequency,peta,output,max_nodes=5000,pool_limit=192,local_diff=30):
 output=Path(output)
 if output.exists():raise FileExistsError(output)
 db=sqlite3.connect(Path(frequency).resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
 pv=PetaView(peta);tasks=[]
 try:
  for task in db.execute('select * from tasks order by id'):
   root=cshogi.Board(task['sfen']);candidates={};seen=0;visited=set();serial=0
   node_counts={r['sfen']:r['visits'] for r in db.execute('select sfen,visits from nodes where task=?',(task['id'],))}
   edge_counts={(r['parent'],r['move']):r['visits'] for r in db.execute('select parent,move,visits from edges where task=?',(task['id'],))}
   # Follow only existing peta edges from the actual task root. Starting at an
   # arbitrary observed missing node would leave disconnected islands and might
   # never change the root minimax value. Each new generation extends this walk.
   todo=[(-1.,serial,root.sfen(),task['games'],[],None,'task_root')]
   while todo and seen<max_nodes and len(candidates)<pool_limit:
    negative,_,sfen,visits,route,parent_eval,basis=heapq.heappop(todo)
    board=cshogi.Board(sfen);ident=position_id(board)
    if ident in visited:continue
    visited.add(ident);seen+=1
    if board.is_game_over() or board.is_nyugyoku() or board.move_number>=256:continue
    moves=pv.moves(board)
    if not moves:
     candidates[ident]={'id':ident,'sfen':board.sfen(),'frequency':visits,'task_games':task['games'],
       'frequency_basis':basis,'frequency_fraction':round(visits/task['games'],6),'priority':-negative,
       'source_position':key(root),'source_ply':root.move_number,
       'source_route':'startpos moves '+task['route'],'peta_route':route,
       'reason':'reachable_peta_frontier','parent_move_cp':parent_eval}
     continue
    best=max(m[1] for m in moves)
    if abs(best)>400:continue
    for move,value,depth in moves:
     if value<best-local_diff:continue
     child=cshogi.Board(sfen);child.push_usi(move);child_key=key(child)
     actual=edge_counts.get((key(board),move),node_counts.get(child_key))
     if actual is None:
      count=max(1.,visits*.25);child_basis='inherited prior; no observed edge count'
     else:count=actual;child_basis='distinct completed games for observed edge/node'
     distance=max(0,child.move_number-root.move_number)
     weight=(count/task['games'])/math.sqrt(1+distance);serial+=1
     heapq.heappush(todo,(-weight,serial,child.sfen(),count,route+[move],value,child_basis))
   items=sorted(candidates.values(),key=lambda x:(-x['priority'],-x['frequency'],x['id']))[:pool_limit]
   tasks.append({'id':task['id'],'label':task['label'],'root_sfen':task['sfen'],'games':task['games'],
                 'black_cp':pv.black_value(root),'positions_scanned':seen,'pool':items})
 finally:pv.close();db.close()
 low,high,unknown,cutoff=low_groups(tasks)
 output.parent.mkdir(parents=True,exist_ok=True)
 report={'schema':1,'peta':str(Path(peta).resolve()),'peta_sha256':sha(peta),'frequency':str(Path(frequency).resolve()),
         'created_unix':time.time(),'score_basis':'current peta root maximum, converted to black POV; no trend or historic score',
         'low_fraction':0.8,'low_cutoff_cp':cutoff,'low':low,'high':high,'unknown':unknown,'tasks':tasks,
         'pool_limit':pool_limit,'nodes_scanned_per_task':max_nodes,'local_diff':local_diff,
         'candidate_scope':'existing peta edges reachable from each actual root; local best minus diff on both turns',
         'frequency_scope':'completed-game edge/node counts; marked discounted priors for unobserved peta edges'}
 output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 return report

def native_next(root,pv,diff=30,max_ply=200,max_visits=200000):
 """Disk-backed port of upstream peta_next_for_turn, with identical branch rules.

Both turns run separately. The selected turn follows only current best moves;
the other turn uses the sign-alternating root bound. Frequency never changes
this set. Return a native position command, not a disconnected leaf SFEN.
"""
 from collections import deque
 output={}
 if root.move_number>=max_ply:return []
 for turn in (1,0):
  root_moves=pv.moves(root)
  if not root_moves:
   output[position_id(root)]=(root.sfen(),[]);continue
  todo=deque([(root.sfen(),[],max(x[1] for x in root_moves))]);visited=set()
  while todo:
   sfen,route,root_value=todo.popleft();board=cshogi.Board(sfen)
   if board.move_number>=max_ply:continue
   ident=position_id(board)
   if ident in visited:continue
   visited.add(ident)
   if len(visited)>max_visits:raise RuntimeError('native peta_next safety limit reached; no truncated plan published')
   moves=pv.moves(board)
   if not moves:
    output.setdefault(ident,(board.sfen(),route));continue
   best=moves[0][1]
   threshold=best if board.move_number%2==turn else root_value-diff
   for move,value,depth in moves:
    if value<threshold:continue
    child=cshogi.Board(sfen);child.push_usi(move)
    if child.move_number>=max_ply:continue
    todo.append((child.sfen(),route+[move],-root_value))
 return list(output.values())


def native_plan(frequency,peta,output,diff=30,max_ply=200,min_pool=48,max_diff=240):
 """Only added policy: observed frequency ordering and current-root task quotas."""
 output=Path(output)
 if output.exists():raise FileExistsError(output)
 db=sqlite3.connect(Path(frequency).resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
 pv=PetaView(peta);tasks=[]
 try:
  for task in db.execute('select * from tasks order by id'):
   root=cshogi.Board(task['sfen']);pool=[]
   counts={r['sfen']:r['visits'] for r in db.execute('select sfen,visits from nodes where task=?',(task['id'],))}
   edges={(r['parent'],r['move']):r['visits'] for r in db.execute('select parent,move,visits from edges where task=?',(task['id'],))}
   task_diff=diff;frontiers=native_next(root,pv,task_diff,max_ply)
   while len(frontiers)<min_pool and task_diff<max_diff:
    task_diff=min(max_diff,task_diff*2);frontiers=native_next(root,pv,task_diff,max_ply)
   for sfen,route in frontiers:
    board=cshogi.Board(root.sfen());weight=float(task['games']);observed=0
    for move in route:
     parent=key(board);board.push_usi(move);actual=edges.get((parent,move),counts.get(key(board)))
     if actual is None:weight=max(1.,weight*.25)
     else:weight=float(actual);observed+=1
    pool.append({'id':position_id(board),'sfen':sfen,'position_command':'position sfen '+root.sfen()+(' moves '+' '.join(route) if route else ''),
      'peta_route':route,'frequency_weight':weight,'observed_edges':observed,
      'priority':weight/task['games']/math.sqrt(1+len(route)),
      'reason':'upstream_peta_next_frontier'})
   pool.sort(key=lambda x:(-x['priority'],x['id']))
   tasks.append({'id':task['id'],'label':task['label'],'root_sfen':task['sfen'],'games':task['games'],'black_cp':pv.black_value(root),'pool':pool,'diff':task_diff})
 finally:pv.close();db.close()
 low,high,unknown,cutoff=low_groups(tasks)
 result={'schema':2,'peta':str(Path(peta).resolve()),'peta_sha256':sha(peta),'frequency':str(frequency),'created_unix':time.time(),
  'score_basis':'current peta root only, black POV; no historical trend','candidate_scope':'upstream peta_next_for_turn branch rules, both turns',
  'low_fraction':.8,'low_cutoff_cp':cutoff,'low':low,'high':high,'unknown':unknown,'tasks':tasks,'diff':diff,'max_ply':max_ply,'min_pool':min_pool,'max_diff':max_diff}
 output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 return result


if __name__=='__main__':
 ap=argparse.ArgumentParser(description=__doc__);sub=ap.add_subparsers(dest='command',required=True)
 e=sub.add_parser('extract');e.add_argument('--campaign',type=Path,required=True);e.add_argument('--completion',type=Path,required=True);e.add_argument('--output',type=Path,required=True)
 p=sub.add_parser('plan');p.add_argument('--frequency',type=Path,required=True);p.add_argument('--peta',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
 a=ap.parse_args()
 if a.command=='extract':print(json.dumps(extract(a.campaign,a.completion,a.output),ensure_ascii=False,indent=2))
 else:
  r=plan(a.frequency,a.peta,a.output)
  print(json.dumps({'low':r['low'],'high':r['high'],'unknown':r['unknown'],'tasks':[{k:t[k] for k in ('id','black_cp','games','positions_scanned')}|{'pool':len(t['pool'])} for t in r['tasks']]},ensure_ascii=False,indent=2))
