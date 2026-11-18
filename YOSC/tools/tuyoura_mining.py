"""Independent, bounded TuyouraOu mining supervisor. Never installs live books.

run: native miner 12 x Threads1, bounded six-ply waves, immutable snapshots,
resident petad, sampled legality, cumulative B0 overlay, current-peta replanning.
status/pause/resume/save/stop communicate through an atomic local inbox.
"""
from __future__ import annotations
import argparse, collections, ctypes, json, os, queue, re, shutil, subprocess, sys, threading, time, uuid
from ctypes import wintypes
from pathlib import Path
import tuyoura_priority as priority
from ybb_ops import difference, write, sha256, validate
from legal_probes import verify

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / 'config/tuyoura-production.json'

def atomic(path, value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
 temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
 try:
  for attempt in range(8):
   try:
    os.replace(temporary,path)
    break
   except PermissionError:
    # A concurrent Windows reader can briefly deny replacement. Keep the
    # same complete temporary file and retry; never truncate the live JSON.
    if attempt==7:raise
    time.sleep(.01*(attempt+1))
 finally:
  try:temporary.unlink(missing_ok=True)
  except OSError:pass

def read(path,default=None):
 return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).exists() else default

class Memory(ctypes.Structure):
 _fields_=[('length',wintypes.DWORD),('load',wintypes.DWORD)]+[(n,ctypes.c_ulonglong) for n in ('total','free','page_total','page_free','virtual','virtual_free','extended')]

def free_gib():
 m=Memory();m.length=ctypes.sizeof(m)
 if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):raise ctypes.WinError()
 return m.free/2**30

class Job:
 """Own children only; supervisor exit closes the handle and kills its tree."""
 def __init__(self):
  class Basic(ctypes.Structure):
   _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',wintypes.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),('active',wintypes.DWORD),('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
  class Extended(ctypes.Structure):
   _fields_=[('basic',Basic),('io',ctypes.c_ulonglong*6),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
  self.k=ctypes.WinDLL('kernel32',use_last_error=True)
  self.k.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR];self.k.CreateJobObjectW.restype=wintypes.HANDLE
  self.k.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
  self.k.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
  self.k.CloseHandle.argtypes=[wintypes.HANDLE]
  self.handle=self.k.CreateJobObjectW(None,None)
  info=Extended();info.basic.flags=0x2000
  if not self.handle or not self.k.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):raise ctypes.WinError(ctypes.get_last_error())
 def attach(self,proc):
  if not self.k.AssignProcessToJobObject(self.handle,wintypes.HANDLE(int(proc._handle))):
   proc.kill();raise ctypes.WinError(ctypes.get_last_error())
 def close(self):
  if self.handle:self.k.CloseHandle(self.handle);self.handle=None

class Proc:
 def __init__(self,name,args,cwd,job,log):
  self.name,self.log,self.q=name,log,queue.Queue();self.pending=collections.deque()
  self.p=subprocess.Popen([str(a) for a in args],cwd=cwd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS|subprocess.CREATE_NO_WINDOW)
  job.attach(self.p)
  threading.Thread(target=self.reader,daemon=True).start()
 def reader(self):
  for raw in self.p.stdout:
   line=raw.decode('utf-8','replace').rstrip();self.log(self.name,line);self.q.put(line)
  self.q.put(None)
 def send(self,line):
  self.log(self.name+'>',line);self.p.stdin.write((line+'\n').encode());self.p.stdin.flush()
 def wait(self,pattern,timeout=1800):
  end=time.monotonic()+timeout
  while time.monotonic()<end:
   try:line=self.q.get(timeout=min(5,max(.1,end-time.monotonic())))
   except queue.Empty:continue
   if line is None:raise RuntimeError(self.name+' exited while waiting for '+pattern)
   if re.search(pattern,line):return line
   if any(tag in line for tag in ('[TaskQueueDone]','[TaskError]','[MiningProgress]','[TaskQueueProgress]')):self.pending.append(line)
  raise TimeoutError(self.name+': '+pattern)
 def poll(self):
  return self.pending.popleft() if self.pending else self.q.get_nowait()
 def command_json(self,command):
  self.send(command);line=self.wait(r'^\{')
  result=json.loads(line)
  if not result.get('ok'):raise RuntimeError(result)
  return result

class Allocator:
 """4 low : 1 high, round-robin tasks inside each group, global dedup.

The fraction measures submitted native task counts. It is not a CPU-time claim.
Score ties at the cutoff stay together; all tied means equal round-robin.
"""
 def __init__(self,plan,seen=(),cursors=None,slot=0):
  self.plan=plan;self.seen=set(seen);self.cursors=dict(cursors or {});self.slot=slot
  self.by_id={t['id']:t for t in plan['tasks']}
  self.offsets=collections.defaultdict(int)
 def one(self,group):
  ids=self.plan[group]
  for _ in ids:
   cursor=self.cursors.get(group,0);tid=ids[cursor%len(ids)];self.cursors[group]=cursor+1
   pool=self.by_id[tid]['pool']
   while self.offsets[tid]<len(pool):
    candidate=pool[self.offsets[tid]];self.offsets[tid]+=1
    if candidate['id'] in self.seen:continue
    self.seen.add(candidate['id'])
    return dict(candidate,task=tid,group=group,black_cp=self.by_id[tid]['black_cp'])
  return None
 def wave(self,count):
  result=[]
  for _ in range(count):
   preferred='high' if self.slot%5==4 else 'low'
   item=self.one(preferred)
   if item is None and preferred=='low' and self.plan['low']:
    # Do not spend the rest of this generation exclusively on high-score tasks.
    # Refresh peta to expose the low group next frontier instead.
    break
   if item is None:item=self.one('low' if preferred=='high' else 'high')
   # Unknown roots are never silently treated as low. Reserve only pilot work.
   if item is None:item=self.one('unknown')
   if item is None:break
   self.slot+=1
   result.append(item)
  return result

class Supervisor:
 def __init__(self,cfg):
  self.cfg=cfg;self.work=Path(cfg['workdir']).resolve();self.work.mkdir(parents=True,exist_ok=True)
  self.lock=(self.work/'supervisor.lock').open('a+b')
  import msvcrt
  self.lock.seek(0);self.lock.write(b'0');self.lock.flush();self.lock.seek(0)
  msvcrt.locking(self.lock.fileno(),msvcrt.LK_NBLCK,1)
  self.log_lock=threading.Lock();self.log_file=(self.work/'supervisor.log').open('a',encoding='utf-8')
  self.job=Job();self.miner=None;self.petad=None
  self.state=read(self.work/'state.json',{})
  self.state.update(pid=os.getpid(),phase='starting',started=time.time(),error=None)
  self.paused=bool(self.state.get('paused',False));self.stopping=False;self.active=False;self.done=0
  self.converter=None;self.convert_result=queue.Queue();self.cycle_due=False
  self.last_snapshot=time.monotonic();self.started_wave=False;self.first_cycle=True
  self.plan=read(self.state.get('plan',cfg['initial_plan']))
  self.alloc=Allocator(self.plan,self.state.get('seen',[]),self.state.get('cursors'),self.state.get('slot',0))
  self.stats=collections.Counter(self.state.get('submitted_by_task',{}))
  for folder in ('control/inbox','control/processed','waves','generations','baseline','book/backup'):(self.work/folder).mkdir(parents=True,exist_ok=True)
 def log(self,tag,line):
  with self.log_lock:self.log_file.write(time.strftime('%Y-%m-%d %H:%M:%S')+' ['+tag+'] '+line+'\n');self.log_file.flush()
 def status(self,phase=None):
  if phase:self.state['phase']=phase
  self.state.update(updated=time.time(),paused=self.paused,stopping=self.stopping,active_wave=self.active,
   converter_active=bool(self.converter and self.converter.is_alive()),miner_pid=self.miner.p.pid if self.miner else None,
   petad_pid=self.petad.p.pid if self.petad else None,free_gib=round(free_gib(),2),disk_free_gib=round(shutil.disk_usage(self.work).free/2**30,1),
   submitted_by_task=dict(self.stats),seen=sorted(self.alloc.seen),cursors=self.alloc.cursors,slot=self.alloc.slot,
   plan_peta=self.plan['peta'],low=self.plan['low'],high=self.plan['high'],unknown=self.plan['unknown'],
   current_scores={t['id']:t['black_cp'] for t in self.plan['tasks']})
  atomic(self.work/'state.json',self.state)
  # Compact status for CLI/phone inspection, without the potentially large seen set.
  atomic(self.work/'status.json',{k:v for k,v in self.state.items() if k not in ('seen','cursors')})
 def save(self):
  self.status('saving_snapshot');self.miner.send('w')
  path=Path(self.miner.wait(r'\[ManualBackupDone\] path=',3600).split('path=',1)[1])
  if not path.is_absolute():path=self.work/path
  path=path.resolve();path.relative_to(self.work/'book/backup')
  self.state['last_snapshot']=str(path);self.last_snapshot=time.monotonic();self.status()
  return path
 def inbox(self):
  for path in sorted((self.work/'control/inbox').glob('*.json')):
   command=read(path).get('command')
   if command=='pause':self.paused=True
   elif command=='resume':self.paused=False
   elif command=='save':self.cycle_due=True
   elif command=='stop':self.stopping=True
   else:self.log('control','rejected '+str(command))
   os.replace(path,self.work/'control/processed'/path.name);self.log('control',str(command))
 def submit(self):
  # Avoid even filling a large in-memory queue. One bounded wave at a time.
  batch=self.alloc.wave(self.cfg['wave_tasks'])
  if not batch:
   if not (self.converter and self.converter.is_alive()):
    # One new conversion may reveal descendants of this generation's mined
    # frontier. If a fresh plan is empty, pause instead of cycling unchanged data.
    if self.state.get('submitted_since_peta',0):self.cycle_due=True
    else:self.paused=True
   self.state['attention']='waiting for refreshed low-score frontier';return
  stamp=time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6]
  atomic(self.work/'waves'/f'{stamp}.json',{'plan':self.state.get('plan',self.cfg['initial_plan']),'peta_sha256':self.plan['peta_sha256'],'tasks':batch,'book_extend_ply':self.cfg['extension_ply']})
  text=''.join('position sfen '+item['sfen']+', book_extend_ply='+str(self.cfg['extension_ply'])+', eval_limit=400, game_ply_limit=256\n' for item in batch)
  path=self.work/'book/think_sfens.txt';temporary=path.with_suffix('.tmp');temporary.write_text(text,encoding='utf-8');os.replace(temporary,path)
  self.active=True;self.started_wave=True;self.state['wave']=stamp;self.state['wave_count']=len(batch)
  for item in batch:self.stats[item['task']]+=1
  self.state['submitted_since_peta']=self.state.get('submitted_since_peta',0)+len(batch)
  self.status('mining');self.miner.send('e')
 def build(self,snapshot,folder):
  try:
   started=time.time();folder.mkdir();peta=folder/'peta.ybb'
   if not self.petad:
    self.petad=Proc('petad',[ROOT/'runtime/petad/petad.exe'],ROOT,self.job,self.log)
    ready=json.loads(self.petad.wait(r'^\{'))
    if not ready.get('ok'):raise RuntimeError(ready)
    self.petad.command_json('threads '+str(self.cfg['petad_threads']))
    graph=self.petad.command_json('load '+str(snapshot))
   else:graph=self.petad.command_json('apply '+str(snapshot))
   solved=self.petad.command_json('solve');written=self.petad.command_json('write '+str(peta))
   structural=validate(peta,True);legal=verify(peta,self.cfg['legal_samples'])
   delta=folder/'mining-delta.ybb'
   delta_result=write(delta,difference(self.state['baseline'],snapshot),depth=True)
   new_plan=folder/'priority.json';new=priority.plan(self.cfg['frequency'],peta,new_plan)
   manifest={'schema':1,'completed_unix':time.time(),'elapsed_s':round(time.time()-started,3),'baseline':self.state['baseline'],
    'snapshot':str(snapshot),'snapshot_sha256':sha256(snapshot),'peta_sha256':legal['candidate_sha256'],
    'delta_sha256':sha256(delta),'delta':delta_result,'graph':graph,'solve':solved,'write':written,
    'structural':structural,'legal':legal,'priority':str(new_plan),'score_policy':'current peta only; no trend',
    'engine':self.cfg['engine'],'nodes':self.cfg['nodes'],'workers':self.cfg['workers'],'extension_ply':self.cfg['extension_ply'],
    'deployment':'candidate only; not installed in KiriCadence/book'}
   atomic(folder/'READY.json',manifest);self.convert_result.put((folder,new,None))
  except Exception as exc:self.convert_result.put((folder,None,repr(exc)))
 def finish_conversion(self):
  try:folder,plan,error=self.convert_result.get_nowait()
  except queue.Empty:return
  if error:
   self.paused=True;self.state['error']='conversion: '+error;self.status('conversion_failed')
   self.log('error',self.state['error']);return
  # Active wave keeps its old manifest; all subsequent submissions use new scores.
  self.plan=plan;self.alloc=Allocator(plan,cursors=self.alloc.cursors,slot=0)
  self.state.update(plan=str(folder/'priority.json'),last_generation=str(folder),last_peta_sha256=plan['peta_sha256'],error=None,submitted_since_peta=0)
  self.state.pop('attention',None)
  atomic(self.work/'current.json',{'generation':str(folder),'peta':plan['peta'],'sha256':plan['peta_sha256'],'priority':str(folder/'priority.json')})
  self.prune();self.status('mining' if self.active else 'idle');self.log('generation','READY '+str(folder))
 def prune(self):
  # Keep B0 separately, latest four completed native snapshots and two READY
  # generations. Never recursively delete directories or touch upstream data.
  baseline=Path(self.state['baseline']).resolve()
  snapshots=sorted((self.work/'book/backup').glob('book_miner-*.ybb'),key=lambda p:p.stat().st_mtime,reverse=True)
  for path in snapshots[self.cfg['keep_snapshots']:]:
   if path.resolve()!=baseline:path.unlink()
  generations=sorted((p.parent for p in (self.work/'generations').glob('*/READY.json') if not (p.parent/'PRUNED.json').exists()),key=lambda p:(p/'READY.json').stat().st_mtime,reverse=True)
  for folder in generations[self.cfg['keep_generations']:]:
   for name in ('peta.ybb','mining-delta.ybb'):
    path=folder/name
    if path.exists():path.unlink()
   atomic(folder/'PRUNED.json',{'time':time.time(),'retained':'manifests and priority only'})
 def run(self):
  try:
   self.status('loading_miner')
   self.miner=Proc('miner',[self.work/'BookMinerCpp.exe','--from_gui'],self.work,self.job,self.log)
   self.miner.wait(r'\[CommandReady\]',1800)
   if not self.state.get('baseline'):
    snapshot=self.save();baseline=self.work/'baseline/B0.ybb';shutil.copyfile(snapshot,baseline)
    atomic(self.work/'baseline/B0.json',{'sha256':sha256(baseline),'seed':self.cfg['seed'],'seed_sha256':sha256(self.cfg['seed']),
      'kind':'native no-mining load/save, excludes normalization from mining delta','created':time.time()})
    self.state['baseline']=str(baseline);self.status('baseline_ready')
   while True:
    self.inbox();self.finish_conversion()
    try:
     while True:
      line=self.miner.poll()
      if line is None:raise RuntimeError('miner exited unexpectedly')
      if '[TaskQueueDone]' in line:self.active=False;self.done+=1;self.state['completed_waves']=self.state.get('completed_waves',0)+1
      if '[MiningProgress]' in line:self.state['mining_progress']=line
      if '[TaskQueueProgress]' in line:self.state['task_progress']=line
      if line.startswith('Error :') or '[TaskError]' in line:raise RuntimeError(line)
    except queue.Empty:pass
    busy=bool(self.converter and self.converter.is_alive())
    if self.stopping and not self.active and not busy:
     self.save();self.miner.send('!');self.miner.p.wait(600);self.status('stopped');break
    if self.started_wave and self.done and self.first_cycle:self.cycle_due=True
    if time.monotonic()-self.last_snapshot>=self.cfg['snapshot_seconds']:self.cycle_due=True
    if self.cycle_due and not busy and not self.stopping:
     required=self.cfg['apply_min_free_gib'] if self.petad else self.cfg['load_min_free_gib']
     if free_gib()>=required and shutil.disk_usage(self.work).free/2**30>=self.cfg['min_disk_free_gib']:
      snapshot=self.save();folder=self.work/'generations'/(time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:6])
      self.converter=threading.Thread(target=self.build,args=(snapshot,folder),daemon=True);self.converter.start()
      self.cycle_due=False;self.first_cycle=False
     else:self.state['attention']='conversion waiting for memory/disk headroom'
    if not self.active and not self.paused and not self.stopping:
     if free_gib()<self.cfg['min_mining_free_gib'] or shutil.disk_usage(self.work).free/2**30<self.cfg['min_disk_free_gib']:
      self.state['attention']='next wave waiting for memory/disk headroom'
     else:self.submit()
    self.status('paused' if self.paused and not self.active else None)
    time.sleep(2)
  except Exception as exc:
   self.state['error']=repr(exc);self.status('failed');self.log('fatal',repr(exc));raise
  finally:
   if self.petad and not (self.converter and self.converter.is_alive()):
    try:self.petad.send('quit');self.petad.p.wait(30)
    except Exception:pass
   self.job.close();self.lock.close()

def prepare(cfg):
 work=Path(cfg['workdir']).resolve();work.mkdir(parents=True,exist_ok=True)
 if (work/'BookMinerCpp.exe').exists():raise FileExistsError('Already prepared; use run/status')
 for folder in ('settings','book/backup'):(work/folder).mkdir(parents=True,exist_ok=True)
 shutil.copyfile(ROOT/'runtime/miner/BookMinerCpp.exe',work/'BookMinerCpp.exe')
 atomic(work/'settings/book_miner_settings.json5',{'max_book_ply':256,'peta_next_start_sfens_path':'book/peta_start_sfens.txt','auto_save_interval_seconds':86400})
 atomic(work/'settings/engine_settings.json5',[{'name':'suisho11plus','multipv':4,'path':cfg['engine'],'multi':cfg['workers'],'multipv_delta':100,'nodes':cfg['nodes']}])
 seed=work/'book/backup/book_miner-20000101000000_21648183.ybb'
 shutil.copyfile(cfg['seed'],seed)
 atomic(work/'PREPARED.json',{'created':time.time(),'seed':cfg['seed'],'seed_sha256':sha256(seed),'config':cfg,
  'miner_sha256':sha256(work/'BookMinerCpp.exe'),'engine_sha256':sha256(cfg['engine']),'petad_sha256':sha256(ROOT/'runtime/petad/petad.exe')})
 print(str(work))

def main():
 ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--config',type=Path,default=DEFAULT)
 ap.add_argument('command',choices=['prepare','run','status','watch','pause','resume','save','stop']);a=ap.parse_args();cfg=read(a.config);work=Path(cfg['workdir'])
 if a.command=='prepare':prepare(cfg)
 elif a.command=='run':
  if (work/'STOPPED_BY_USER.json').exists():
   raise SystemExit('Explicit user stop: this runtime is quarantined. Do not restart or reuse outputs without new user authorization.')
  Supervisor(cfg).run()
 elif a.command in ('status','watch'):
  while True:
   print(json.dumps(read(work/'status.json',{'phase':'not_started'}),ensure_ascii=False,indent=2),flush=True)
   if a.command=='status':break
   time.sleep(10)
 else:
  path=work/'control/inbox'/(time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex+'.json')
  atomic(path,{'command':a.command,'created':time.time()});print(str(path))

if __name__=='__main__':main()
