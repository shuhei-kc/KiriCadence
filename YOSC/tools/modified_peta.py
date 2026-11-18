"""Immutable, bounded-memory input overrides for a separate white peta profile."""
from __future__ import annotations
import hashlib,json,mmap,os,shutil,struct,time,uuid
from pathlib import Path
import cshogi
import numpy as np
from tuyoura_priority import ROOT,key
from yaneuraou_book_lib import pack_sfen,usi_to_move16,YbbBookProbe
from flip import flipped_sfen_main,flipped_move
from ybb_ops import HEADER,INDEX,MAGIC,sha256
from tuyoura_mining import atomic,read

DTYPE=np.dtype([('packed','V32'),('offset','<u8'),('ply','<u2'),('count','<u2')])

def board_from_text(text):
    parts=text.strip().removeprefix('position ').removeprefix('sfen ').split()
    if len(parts)==3:parts.append('1')
    if len(parts)!=4:raise ValueError('Override requires an SFEN position')
    return cshogi.Board(' '.join(parts))

def load_overrides(path):
    data=Path(path).read_bytes();cfg=json.loads(data.decode('utf-8-sig'))
    if cfg.get('version')!=1:raise ValueError('Unknown override version')
    operations=[];seen=set()
    for op in cfg['operations']:
        b=board_from_text(op['sfen']);other=cshogi.Board(flipped_sfen_main(key(b))+' 1')
        ident=min(pack_sfen(b),pack_sfen(other)).hex()
        if ident in seen:raise ValueError('Conflicting duplicate override position: '+op['id'])
        seen.add(ident)
        if op['action']=='upsert_move':
            move=b.move_from_usi(op['move'])
            if not move or not b.is_legal(move):raise ValueError('Illegal override move: '+op['id'])
            if not isinstance(op['value'],int) or not -32000<=op['value']<=32000:raise ValueError('Override value range')
        elif op['action']!='delete_position':raise ValueError('Unknown override action')
        operations.append(op|{'sfen':b.sfen()})
    return cfg|{'operations':operations,'sha256':hashlib.sha256(data).hexdigest(),'path':str(Path(path).resolve())}

def patch_snapshot(source,target,override_path,check_cancel=lambda:None,log=lambda *x:None):
    """Copy unchanged blobs verbatim; adjust index offsets in small numpy chunks."""
    source,target=Path(source),Path(target);cfg=load_overrides(override_path)
    if target.exists():raise FileExistsError(target)
    before=source.stat();events=[];audit=[]
    with source.open('rb') as stream,mmap.mmap(stream.fileno(),0,access=mmap.ACCESS_READ) as mm:
        magic,n,flags=HEADER.unpack_from(mm)
        if magic!=MAGIC or flags not in (0,1):raise ValueError('Invalid source YBB')
        base=32+n*44;move_size=6 if flags&1 else 4;move_format=struct.Struct('<HhH' if flags&1 else '<Hh')
        def find(pk):
            lo,hi=0,n
            while lo<hi:
                mid=(lo+hi)//2
                if mm[32+mid*44:64+mid*44]<pk:lo=mid+1
                else:hi=mid
            exists=lo<n and mm[32+lo*44:64+lo*44]==pk
            return lo,exists
        for op in cfg['operations']:
            b=board_from_text(op['sfen']);other=cshogi.Board(flipped_sfen_main(key(b))+' 1');matches=[]
            for flip,board in ((False,b),(True,other)):
                pk=pack_sfen(board);i,exists=find(pk)
                if exists:matches.append((i,pk,flip,board))
            if op['action']=='delete_position':
                for i,pk,flip,board in matches:
                    _,off,ply,count=INDEX.unpack_from(mm,32+i*44)
                    events.append({'i':i,'pk':pk,'old_len':count*move_size,'off':off,'blob':None,'insert':False,'ply':ply})
                audit.append({'id':op['id'],'action':op['action'],'matched_records':len(matches),'sfen':op['sfen']})
                continue
            if len(matches)>1:raise ValueError('Input is not canonical at '+op['id'])
            if matches:i,pk,flip,board=matches[0]
            else:
                flip=pack_sfen(other)<pack_sfen(b);board=other if flip else b;pk=pack_sfen(board);i,_=find(pk)
            move=flipped_move(op['move']) if flip else op['move'];m16=usi_to_move16(board,move)
            if matches:
                _,off,ply,count=INDEX.unpack_from(mm,32+i*44)
                entries={r[0]:tuple(r[1:]) for r in move_format.iter_unpack(mm[base+off:base+off+count*move_size])}
            else:
                off=INDEX.unpack_from(mm,32+i*44)[1] if i<n else len(mm)-base
                ply=b.move_number;count=0;entries={}
            was_present=m16 in entries
            entries[m16]=(op['value'],0) if flags&1 else (op['value'],)
            blob=b''.join(move_format.pack(m,*v) for m,v in sorted(entries.items(),key=lambda x:(-x[1][0],x[0])))
            events.append({'i':i,'pk':pk,'off':off,'old_len':count*move_size,'blob':blob,'insert':not bool(matches),'ply':ply})
            audit.append({'id':op['id'],'action':op['action'],'sfen':op['sfen'],'move':op['move'],'value':op['value'],'existing_move_replaced':was_present,'record_inserted':not bool(matches),'flipped_record':flip})
        events.sort(key=lambda e:(e['i'],e['pk']))
        if len({e['pk'] for e in events})!=len(events):raise ValueError('Overlapping profile operations')
        count=n+sum(e['insert'] for e in events)-sum(e['blob'] is None for e in events)
        target.parent.mkdir(parents=True,exist_ok=True)
        index_path=target.with_suffix('.index.tmp');moves_path=target.with_suffix('.moves.tmp');tmp=target.with_suffix('.saving')
        def copy_blob(out,start,end):
            for at in range(start,end,8<<20):
                check_cancel();out.write(mm[at:min(end,at+(8<<20))])
        try:
            with index_path.open('wb') as idx,moves_path.open('wb') as mov:
                cursor=0;old_move=0;delta=0
                def index_span(start,end):
                    for at in range(start,end,32768):
                        check_cancel();arr=np.frombuffer(mm,dtype=DTYPE,count=min(32768,end-at),offset=32+at*44).copy()
                        if delta:arr['offset']=(arr['offset'].astype(np.int64)+delta).astype(np.uint64)
                        idx.write(arr.tobytes())
                for event in events:
                    index_span(cursor,event['i']);copy_blob(mov,base+old_move,base+event['off'])
                    if event['blob'] is not None:
                        idx.write(INDEX.pack(event['pk'],event['off']+delta,event['ply'],len(event['blob'])//move_size));mov.write(event['blob'])
                    delta+=(len(event['blob']) if event['blob'] is not None else 0)-event['old_len']
                    cursor=event['i']+(not event['insert']);old_move=event['off']+event['old_len']
                index_span(cursor,n);copy_blob(mov,base+old_move,len(mm))
            with tmp.open('wb') as out:
                out.write(HEADER.pack(MAGIC,count,flags))
                for path in (index_path,moves_path):
                    with path.open('rb') as part:
                        while data:=part.read(8<<20):check_cancel();out.write(data)
                out.flush();os.fsync(out.fileno())
            after=source.stat()
            if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Source snapshot changed')
            check_cancel();os.replace(tmp,target)
        finally:
            for path in (index_path,moves_path,tmp):
                if path.exists():path.unlink()
    report={'source':str(source),'target':str(target),'source_positions':n,'positions':count,'flags':flags,'overrides_sha256':cfg['sha256'],'override_path':cfg['path'],'operations':audit}
    atomic(target.with_suffix('.json'),report);log('profile',json.dumps(report,ensure_ascii=False));return report

def verify_overrides(path,override_path):
    cfg=load_overrides(override_path);results=[]
    with YbbBookProbe(path) as probe:
        for op in cfg['operations']:
            b=board_from_text(op['sfen']);other=flipped_sfen_main(key(b))+' 1'
            if op['action']=='delete_position':
                if probe.probe(b.sfen()) is not None or probe.probe(other) is not None:raise ValueError('Deleted position survived: '+op['id'])
                results.append({'id':op['id'],'absent':True})
            else:
                moves=probe.probe(b.sfen());flip=False
                if moves is None:moves=probe.probe(other);flip=True
                wanted=flipped_move(op['move']) if flip else op['move']
                found=[m for m in moves or [] if m.move==wanted]
                if len(found)!=1 or found[0].value!=op['value'] or found[0].depth!=0:raise ValueError('Override value/depth mismatch: '+op['id'])
                results.append({'id':op['id'],'move':op['move'],'value':found[0].value,'depth':found[0].depth})
    return results

def publish_white(source,target,digest):
    from file_swap import replace_file
    source,target=Path(source),Path(target)
    if target.name!='400_w_modifiedpeta.ybb' or target.parent.resolve()!=(ROOT.parent/'book').resolve():raise ValueError('Unexpected white target')
    staged=target.with_name('.400_w_modifiedpeta-'+uuid.uuid4().hex+'.tmp')
    try:
        shutil.copyfile(source,staged)
        if sha256(staged)!=digest:raise ValueError('White publication copy mismatch')
        replace_file(str(staged),str(target))
    finally:
        if staged.exists():staged.unlink()
    return {'target':str(target),'sha256':digest,'published_unix':time.time()}

class AlternatingPetaSchedule:
    """GUI-owned sequencing only. Failure never advances the next output."""
    def __init__(self,cfg):
        self.cfg=cfg;self.enabled=bool(cfg.get('alternating_peta',{}).get('enabled',False))
        self.interval=float(cfg.get('alternating_peta',{}).get('interval_seconds',1200))
        if self.interval<=0:raise ValueError('Peta interval must be positive')
        self.path=Path(cfg['workdir'])/'peta-schedule.json'
    def state(self):return read(self.path,{'next_kind':'normal','next_due_unix':0})
    def due(self,now=None):return not self.enabled or (now if now is not None else time.time())>=self.state()['next_due_unix']
    def kind(self):return self.state()['next_kind'] if self.enabled else 'normal'
    def completed(self,kind,started):
        if self.enabled:atomic(self.path,{'next_kind':'white' if kind=='normal' else 'normal','next_due_unix':started+self.interval,'last_kind':kind,'last_completed_unix':time.time()})
