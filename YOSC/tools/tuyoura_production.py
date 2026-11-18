"""100M-node Tuyoura task weighting with stock BookMinerCpp and periodic publication.

Upstream sources are unchanged. Only enqueue quotas, frequency ordering, backup
observation, conversion and atomic publication are managed here.
"""
from __future__ import annotations
import argparse,collections,json,os,queue,re,shutil,subprocess,sys,threading,time,uuid
from pathlib import Path
from tuyoura_mining import ROOT,Job,Proc,Allocator,atomic,read,free_gib
from tuyoura_priority import native_plan
from tuyoura_game_candidates import GameAllocator,augment
from ybb_ops import sha256,validate,write,difference
from legal_probes import verify
sys.path.insert(0,str(ROOT.parent/'wrapper'))
from file_swap import replace_file

DEFAULT=ROOT/'config/tuyoura-100m.json'
SNAPSHOT=re.compile(r'^book_miner-\d{14}_\d+\.ybb$')

def publish(source,target,digest):
 """Stage on the destination volume; existing wrapper readers retain their inode."""
 source,target=Path(source),Path(target)
 if target.name!='478_x_peta.ybb' or target.parent.resolve()!=(ROOT.parent/'book').resolve():raise ValueError('Unexpected publication target')
 temporary=target.with_name('.478_x_peta-'+uuid.uuid4().hex+'.tmp')
 try:
  shutil.copyfile(source,temporary)
  if sha256(temporary)!=digest:raise ValueError('Publication copy checksum mismatch')
  replace_file(str(temporary),str(target))
 finally:
  if temporary.exists():temporary.unlink()
 return {'target':str(target),'sha256':digest,'published_unix':time.time()}

def initial_publish(cfg):
 folder=Path(cfg['initial_peta']).parent
 structural=read(folder/'STRUCTURAL_READY.json');legal=read(folder/'LEGAL_PROBES_OK.json');canonical=read(folder/'CANONICAL_INPUT_OK.json')
 digest=sha256(cfg['initial_peta'])
 if digest!=structural['candidate_sha256'] or digest!=legal['candidate_sha256'] or canonical['extra_equivalent_records'] or canonical['source_sha256']!=structural['source_sha256']:
  raise ValueError('Initial peta gates do not match')
 return publish(cfg['initial_peta'],cfg['target'],digest)

def completed_backups(work):
 return sorted((p for p in (Path(work)/'book/backup').glob('*.ybb') if SNAPSHOT.match(p.name)),key=lambda p:p.stat().st_mtime)

class Production:
 def __init__(self,cfg):
  self.cfg=cfg;self.w=Path(cfg['workdir']).resolve();self.w.mkdir(parents=True,exist_ok=True)
  self.lock=(self.w/'supervisor.lock').open('a+b');self.lock.seek(0);self.lock.write(b'0');self.lock.flush();self.lock.seek(0)
  import msvcrt
  msvcrt.locking(self.lock.fileno(),msvcrt.LK_NBLCK,1)
  self.job=Job();self.log_lock=threading.Lock();self.log_file=(self.w/'supervisor.log').open('a',encoding='utf-8')
  self.state=read(self.w/'state.json',{});self.state.update(pid=os.getpid(),phase='starting',started_unix=time.time(),error=None)
  self.counts=collections.Counter(self.state.get('submitted_by_task',{}));self.groups=collections.Counter(self.state.get('submitted_by_group',{}))
  self.game_seen=set(self.state.get('game_candidates_seen',[]))
  self.game_counts=collections.Counter(self.state.get('game_submitted_by_task',{}))
  self.sources=collections.Counter(self.state.get('submitted_by_source',{'stock_pn':sum(self.counts.values())}))
  self.plan=read(self.state.get('plan',cfg['initial_plan']))
  if cfg.get('focus_manifest'):
   from focused_study import build_plan
   self.plan=build_plan(cfg['focus_manifest'],cfg['frequency'],self.plan['peta'],self.w/'restart-priority.json',self.game_seen,min_pool=cfg['min_pool'],max_diff=cfg['max_diff'])
   self.state['plan']=str(self.w/'restart-priority.json')
  elif cfg.get('game_diversity'):
   self.plan=augment(self.plan,cfg['frequency'],self.w/'restart-priority.json',self.game_seen)
   self.state['plan']=str(self.w/'restart-priority.json')
  self.alloc=self.allocator(self.plan,self.state.get('seen',[]),self.state.get('slot',0))
  old_progress=self.state.get('mining_progress','');old_nodes=re.search(r'(?<!avg_)nodes=(\d+)',old_progress)
  self.nodes_offset=self.state.get('nodes_offset',0)+self.state.get('session_nodes',int(old_nodes[1]) if old_nodes else 0)
  self.state.update(nodes_offset=self.nodes_offset,session_nodes=0)
  self.paused=False;self.stop=False;self.remaining=0;self.pending=False;self.pending_count=0;self.backup=False;self.manual_backup=False
  self.miner=None;self.converter=None;self.converter_proc=None;self.stage='idle';self.results=queue.Queue();self.backup_pending=None
  self.min_free=min(free_gib(),self.state.get('min_free_gib_observed',float('inf')));self.started_at=time.time();self.min_during_conversion=None
  for folder in ('control/inbox','control/processed','batches','generations'):(self.w/folder).mkdir(parents=True,exist_ok=True)
  backups=completed_backups(self.w);current=read(self.w/'current.json',{});ready=read(Path(current['generation'])/'READY.json',{}) if current else {}
  if backups and str(backups[-1])!=ready.get('snapshot'):self.backup_pending=backups[-1]
 def allocator(self,plan,seen=(),slot=0):
  if self.cfg.get('focus_manifest'):
   from focused_study import FocusedAllocator
   return FocusedAllocator(plan,seen,self.state.get('cursors'),slot,self.counts,self.game_seen,self.game_counts)
  if self.cfg.get('game_diversity'):
   return GameAllocator(plan,seen,self.state.get('cursors'),slot,self.counts,self.game_seen,self.game_counts)
  return Allocator(plan,seen,self.state.get('cursors'),slot)
 def log(self,tag,line):
  with self.log_lock:self.log_file.write(time.strftime('%Y-%m-%d %H:%M:%S')+' ['+tag+'] '+line+'\n');self.log_file.flush()
 def status(self,phase=None):
  if phase:self.state['phase']=phase
  free=free_gib();self.min_free=min(self.min_free,free)
  if self.converter and self.converter.is_alive():self.min_during_conversion=min(self.min_during_conversion or free,free)
  self.state.update(updated_unix=time.time(),paused=self.paused,native_remaining=self.remaining,enqueue_pending=self.pending,
   backup_active=self.backup,conversion_stage=self.stage,converter_active=bool(self.converter and self.converter.is_alive()),
   miner_pid=self.miner.p.pid if self.miner else None,converter_pid=self.converter_proc.p.pid if self.converter_proc else None,
   free_gib=round(free,3),min_free_gib_observed=round(self.min_free,3),disk_free_gib=round(shutil.disk_usage(self.w).free/2**30,1),
   hash_mb=self.cfg['hash_mb'],nodes=self.cfg['nodes'],workers=self.cfg['workers'],extension_ply=6,max_ply=200,
   submitted_by_task=dict(self.counts),submitted_by_group=dict(self.groups),low=self.plan['low'],high=self.plan['high'],unknown=self.plan['unknown'],
   submitted_by_source=dict(self.sources),game_candidates_seen=sorted(self.game_seen),game_submitted_by_task=dict(self.game_counts),game_diversity=self.cfg.get('game_diversity',False),
   tasks=[{k:t[k] for k in ('id','label','black_cp','diff')}|{'candidates':len(t['pool']),'game_candidates':sum(x['id'] not in self.game_seen for x in t.get('game_pool',[]))} for t in self.plan['tasks']],
   seen=sorted(self.alloc.seen),cursors=self.alloc.cursors,slot=self.alloc.slot,target=self.cfg['target'])
  atomic(self.w/'state.json',self.state)
  atomic(self.w/'status.json',{k:v for k,v in self.state.items() if k not in ('seen','cursors','game_candidates_seen')})
 def controls(self):
  for path in sorted((self.w/'control/inbox').glob('*.json')):
   command=read(path)['command']
   if command=='stop':self.stop=True
   elif command=='pause':self.paused=True
   elif command=='resume':self.paused=False
   elif command=='save' and not self.backup:
    # Manual w is stock/synchronous; the existing 24-task buffer keeps workers
    # occupied while its command thread writes. Scheduled saves use its thread.
    self.backup=True;self.manual_backup=True;self.miner.send('w')
   self.log('control',command);os.replace(path,self.w/'control/processed'/path.name)
 def events(self):
  try:
   while True:
    line=self.miner.poll()
    if line is None:raise RuntimeError('native miner exited')
    if '[TaskQueueStart]' in line:
     self.pending=False;self.pending_count=0
    if '[TaskQueue' in line:
     m=re.search(r'(?<!job_)remaining=(\d+)',line)
     if m:self.remaining=int(m[1])
     self.state['queue_progress']=line
    if '[MiningProgress]' in line:
     self.state['mining_progress']=line
     values=dict(re.findall(r'\b(positions|nodes)=(\d+)',line))
     if 'positions' in values:self.state['mined_positions_total']=int(values['positions'])-getattr(self,'cfg',{}).get('baseline_positions',21648183)
     if 'nodes' in values:self.state['session_nodes']=int(values['nodes'])
     self.state['mining_nodes_total']=self.nodes_offset+self.state.get('session_nodes',0)
    if '[BackupStart]' in line:self.backup=True;self.state['backup_started_unix']=time.time()
    if '[BackupDone]' in line:
     self.backup=False;self.backup_pending=completed_backups(self.w)[-1];self.state['last_snapshot']=str(self.backup_pending)
    if '[ManualBackupDone]' in line:
     self.backup=False;self.manual_backup=False
     path=Path(line.split('path=',1)[1]);self.backup_pending=path if path.is_absolute() else self.w/path
     self.state['last_snapshot']=str(self.backup_pending)
    if '[BookWriteProgress]' in line:self.state['backup_progress']=line
    if 'auto save failed:' in line or 'Exception :' in line or 'Error :' in line:raise RuntimeError(line)
  except queue.Empty:pass
 def fill(self):
  if self.pending or self.remaining>self.cfg['refill_at'] or self.paused:return
  if shutil.disk_usage(self.w).free<self.cfg['min_disk_gib']*2**30:
   self.state['attention']='low disk: not enqueueing';return
  count=self.cfg['target_inflight']-self.remaining
  batch=self.alloc.wave(count)
  if not batch:
   self.state['attention']='eligible low-score candidates exhausted; awaiting scheduled generation'
   self.state['phase']='waiting_candidates';return
  stamp=time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6]
  atomic(self.w/'batches'/f'{stamp}.json',{'plan':self.state.get('plan',self.cfg['initial_plan']),'peta_sha256':self.plan['peta_sha256'],'tasks':batch,'nodes':100000000,'hash_mb':1024,'extension_ply':6})
  content=''.join(t['position_command']+', book_extend_ply=6, eval_limit=400, game_ply_limit=200\n' for t in batch)
  path=self.w/'book/think_sfens.txt';tmp=path.with_suffix('.tmp');tmp.write_text(content,encoding='utf-8');os.replace(tmp,path)
  self.pending=True;self.pending_count=len(batch)
  for t in batch:
   self.counts[t['task']]+=1;self.groups[t['group']]+=1;self.sources[t.get('source_kind','stock_pn')]+=1
  self.state['submitted_since_peta']=self.state.get('submitted_since_peta',0)+len(batch)
  self.state.pop('attention',None);self.status('mining');self.miner.send('e')
 def convert(self,snapshot,folder):
  proc=None
  try:
   started=time.time();folder.mkdir();self.stage='petad_load'
   proc=Proc('petad',[ROOT/'runtime/petad/petad.exe'],ROOT,self.job,self.log);self.converter_proc=proc
   ready=json.loads(proc.wait(r'^\{'))
   if not ready.get('ok'):raise RuntimeError(ready)
   proc.command_json('threads '+str(self.cfg['petad_threads']))
   load=proc.command_json('load '+str(snapshot));self.stage='petad_solve';solve=proc.command_json('solve')
   peta=folder/'peta.ybb';self.stage='petad_write';written=proc.command_json('write '+str(peta))
   proc.send('quit');proc.p.wait(60);self.converter_proc=None
   self.stage='validation';structural=validate(peta,True);legal=verify(peta,1000)
   self.stage='priority'
   if self.cfg.get('focus_manifest'):
    from focused_study import build_plan
    plan=build_plan(self.cfg['focus_manifest'],self.cfg['frequency'],peta,folder/'priority.json',list(self.game_seen),min_pool=self.cfg['min_pool'],max_diff=self.cfg['max_diff'])
   else:
    native_output=folder/('native-priority.json' if self.cfg.get('game_diversity') else 'priority.json')
    plan=native_plan(self.cfg['frequency'],peta,native_output,min_pool=self.cfg['min_pool'],max_diff=self.cfg['max_diff'])
    if self.cfg.get('game_diversity'):
     self.stage='game_candidates';plan=augment(plan,self.cfg['frequency'],folder/'priority.json',list(self.game_seen))
   self.stage='cumulative_delta';delta=folder/'mining-delta.ybb';delta_info=write(delta,difference(self.cfg['baseline'],snapshot),depth=True)
   self.stage='publication';installed=publish(peta,self.cfg['target'],legal['candidate_sha256'])
   report={'kind':'suisho11plus-100M','snapshot':str(snapshot),'snapshot_sha256':sha256(snapshot),'baseline':self.cfg['baseline'],
    'nodes':100000000,'hash_mb':1024,'workers':12,'structural':structural,'legal':legal,'delta':delta_info,'delta_sha256':sha256(delta),
    'load':load,'solve':solve,'write':written,'publication':installed,'elapsed_s':time.time()-started,'completed_unix':time.time()}
   atomic(folder/'READY.json',report);self.results.put((folder,plan,report,None))
  except Exception as exc:self.results.put((folder,None,None,repr(exc)))
  finally:
   if proc and proc.p.poll() is None:proc.p.kill();proc.p.wait()
   self.converter_proc=None;self.stage='idle'
 def finish(self):
  try:folder,plan,report,error=self.results.get_nowait()
  except queue.Empty:return
  if error:
   self.state['conversion_error']=error;self.log('conversion_failed',error);return
  self.state['cursors']=self.alloc.cursors
  self.plan=plan;self.alloc=self.allocator(plan)
  self.state.update(plan=str(folder/'priority.json'),last_generation=str(folder),last_publication=report['publication'],submitted_since_peta=0)
  self.state.pop('attention',None);self.state.pop('conversion_error',None)
  atomic(self.w/'current.json',report['publication']|{'generation':str(folder),'plan':str(folder/'priority.json')})
  self.log('generation','READY '+str(folder));self.prune()
 def prune(self):
  # Exact filenames only, only this fresh runtime. Keep original B0 untouched.
  backups=completed_backups(self.w)
  for path in backups[:-4]:path.unlink()
  folders=sorted((p.parent for p in (self.w/'generations').glob('*/READY.json') if not (p.parent/'PRUNED.json').exists()),key=lambda p:(p/'READY.json').stat().st_mtime)
  for folder in folders[:-2]:
   for name in ('peta.ybb','mining-delta.ybb'):
    path=folder/name
    if path.exists():path.unlink()
   atomic(folder/'PRUNED.json',{'time':time.time()})
 def run(self):
  try:
   settings=read(self.w/'settings/engine_settings.json5')
   options=(Path(self.cfg['engine']).parent/'engine_options.txt').read_text()
   if len(settings)!=1 or settings[0]['nodes']!=100000000 or settings[0]['multi']!=12 or 'USI_Hash 1024' not in options or 'Threads 1' not in options:
    raise RuntimeError('Refusing to start: 100M / Hash1024 / Threads1 / 12worker settings not verified')
   self.status('loading');self.miner=Proc('miner',[self.w/'BookMinerCpp.exe','--from_gui'],self.w,self.job,self.log)
   self.miner.wait(r'\[CommandReady\]',1800);self.status('ready')
   while not self.stop:
    self.controls()
    if self.stop:break
    self.events();self.finish();self.fill()
    busy=self.converter and self.converter.is_alive()
    if self.backup_pending and not busy:
     if free_gib()>=self.cfg['conversion_min_free_gib']:
      snapshot=self.backup_pending;self.backup_pending=None
      folder=self.w/'generations'/(time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6])
      self.min_during_conversion=None;self.converter=threading.Thread(target=self.convert,args=(snapshot,folder),daemon=True);self.converter.start()
     else:self.state['attention']='peta update waiting for RAM; 1024MB Hash and mining remain unchanged'
    self.status('paused' if self.paused else None);time.sleep(1)
   self.job.close();self.remaining=0;self.pending=False;self.status('stopped')
  except Exception as exc:
   self.state['error']=repr(exc);self.log('fatal',repr(exc));self.job.close();self.status('failed');raise
  finally:self.job.close();self.lock.close()

def prepare(cfg):
 w=Path(cfg['workdir']);w.mkdir(parents=True,exist_ok=True)
 if (w/'PREPARED.json').exists():raise FileExistsError(w)
 for folder in ('settings','book/backup','baseline'):(w/folder).mkdir(parents=True,exist_ok=True)
 engine=Path(cfg['engine']);engine.parent.mkdir(parents=True,exist_ok=True);(engine.parent/'eval').mkdir(exist_ok=True)
 existing=Path(cfg['existing_engine'])
 shutil.copyfile(existing,engine);shutil.copyfile(existing.parent/'eval/nn.bin',engine.parent/'eval/nn.bin')
 shutil.copyfile(existing.parent/'engine_options.txt',engine.parent/'engine_options.txt')
 options=(engine.parent/'engine_options.txt').read_text()
 if 'USI_Hash 1024' not in options or 'Threads 1' not in options:raise ValueError('Existing production Hash/Threads mismatch')
 shutil.copyfile(ROOT/'runtime/miner/BookMinerCpp.exe',w/'BookMinerCpp.exe')
 shutil.copyfile(cfg['baseline_source'],cfg['baseline'])
 seed=w/'book/backup/book_miner-20000101000000_21648183.ybb';shutil.copyfile(cfg['baseline'],seed)
 if sha256(seed)!=cfg['baseline_sha256']:raise ValueError('Baseline is not the no-mining B0')
 atomic(w/'settings/book_miner_settings.json5',{'max_book_ply':200,'auto_save_interval_seconds':cfg['snapshot_seconds'],'peta_next_start_sfens_path':'book/peta_start_sfens.txt'})
 atomic(w/'settings/engine_settings.json5',[{'name':'suisho11plus-100M','path':str(engine),'nodes':100000000,'multi':12,'multipv':4,'multipv_delta':100}])
 atomic(w/'PREPARED.json',{'created_unix':time.time(),'config':cfg,'seed_sha256':sha256(seed),'engine_sha256':sha256(engine),'eval_sha256':sha256(engine.parent/'eval/nn.bin'),'engine_options':options,
  'excludes':'all prior 1M-node mining snapshots/deltas/peta outputs'})
 print(str(w))

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--config',type=Path,default=DEFAULT)
 ap.add_argument('command',choices=['prepare','install-initial','run','status','watch','pause','resume','save','stop']);a=ap.parse_args();cfg=read(a.config);w=Path(cfg['workdir'])
 if a.command=='prepare':prepare(cfg)
 elif a.command=='install-initial':
  result=initial_publish(cfg);atomic(w/'initial-publication.json',result);print(json.dumps(result))
 elif a.command=='run':Production(cfg).run()
 elif a.command in ('status','watch'):
  while True:
   print(json.dumps(read(w/'status.json',{'phase':'not_started'}),ensure_ascii=False,indent=2),flush=True)
   if a.command=='status':break
   time.sleep(10)
 else:
  path=w/'control/inbox'/(time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex+'.json');atomic(path,{'command':a.command,'time':time.time()});print(str(path))

if __name__=='__main__':main()
