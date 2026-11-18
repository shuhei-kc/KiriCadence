"""Local durable GUI inbox and CLI. Never starts a miner or opens a port."""
from __future__ import annotations
import argparse, ctypes, hashlib, json, os, re, sys, time, uuid
from pathlib import Path
from gui_workflow import ROOT, decode
from tuyoura_mining import atomic, read


def process_alive(pid):
    if not pid: return False
    if os.name != 'nt':
        try: os.kill(int(pid),0); return True
        except OSError: return False
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,int(pid))
    if not handle: return False
    code=wintypes.DWORD()
    try: return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
    finally: kernel.CloseHandle(handle)


def live_status(work):
    status=read(Path(work)/'gui-status.json',{})
    status['gui_alive']=process_alive(status.get('pid')) and time.time()-status.get('updated_unix',0)<15
    return status


def valid_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):
        raise ValueError('Invalid request id')
    return value


class ControlInbox:
    def __init__(self,work):
        self.work=Path(work).resolve(); self.root=self.work/'control'
        for name in ('inbox','processing','results','payloads','processed'):
            (self.root/name).mkdir(parents=True,exist_ok=True)

    def result(self,ident):
        return read(self.root/'results'/(valid_id(ident)+'.json'))

    def receipt(self,ident,state,**fields):
        ident=valid_id(ident); old=self.result(ident) or {'id':ident,'created_unix':time.time()}
        old.update(state=state,updated_unix=time.time(),**fields)
        atomic(self.root/'results'/(ident+'.json'),old)
        return old

    def recover(self):
        # A crash after stdin write has an uncertain outcome. Never replay it.
        for path in (self.root/'processing').glob('*.json'):
            ident=valid_id(path.stem); result=self.result(ident)
            if not result or result.get('state') not in ('accepted','stopped','rejected','expired'):
                self.receipt(ident,'unknown',error='Previous GUI ended before acknowledgement; inspect the native log, do not automatically resend.')
            os.replace(path,self.root/'processed'/path.name)

    def claim(self):
        paths=sorted((self.root/'inbox').glob('*.json'),key=lambda p:p.stat().st_mtime)
        if not paths: return None
        path=paths[0]; ident=valid_id(path.stem); previous=self.result(ident)
        if previous:
            os.replace(path,self.root/'processed'/path.name)
            return None
        target=self.root/'processing'/path.name; os.replace(path,target)
        try:
            request=read(target,{})
            if request.get('id')!=ident: raise ValueError('Request id mismatch')
            if request.get('action') not in ('enqueue','stop'): raise ValueError('Unsupported action')
            if time.time()>request.get('expires_unix',0):
                self.receipt(ident,'expired',error='Request expired before GUI submission'); self.finish(ident); return None
            self.receipt(ident,'received',action=request['action'])
            return request
        except Exception as exc:
            self.receipt(ident,'rejected',error=str(exc)); self.finish(ident); return None

    def lines(self,request):
        ident=valid_id(request['id']); path=(self.root/'payloads'/(ident+'.txt')).resolve()
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=request.get('sha256'): raise ValueError('Input SHA mismatch')
        lines=[line.strip() for line in data.decode('utf-8-sig').splitlines() if line.strip() and not line.lstrip().startswith('#')]
        if len(lines)!=request.get('count') or not lines: raise ValueError('Input count mismatch or empty input')
        for line in lines: decode(line)
        return lines

    def finish(self,ident):
        path=self.root/'processing'/(valid_id(ident)+'.json')
        if path.exists(): os.replace(path,self.root/'processed'/path.name)


def submit(work,action,path=None,ident=None):
    inbox=ControlInbox(work); ident=valid_id(ident or uuid.uuid4().hex)
    existing=inbox.result(ident)
    if existing: return existing
    # A retry with the same id only observes an existing durable request.
    for folder in ('inbox','processing'):
        if (inbox.root/folder/(ident+'.json')).exists(): return {'id':ident,'state':'queued'}
    status=live_status(work)
    if not status.get('gui_alive'): raise RuntimeError('GUI is not running. It will not be started automatically.')
    if action=='enqueue' and (not status.get('running') or status.get('closing')):
        raise RuntimeError('Mining is stopped or closing. Start it explicitly in the GUI before enqueueing.')
    request={'id':ident,'action':action,'created_unix':time.time(),'expires_unix':time.time()+300}
    if action=='enqueue':
        data=Path(path).read_text(encoding='utf-8-sig')
        lines=[line.strip() for line in data.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        if not lines: raise ValueError('No input positions')
        for line in lines: decode(line)
        payload=('\n'.join(lines)+'\n').encode('utf-8')
        (inbox.root/'payloads'/(ident+'.txt')).write_bytes(payload)
        request.update(sha256=hashlib.sha256(payload).hexdigest(),count=len(lines),source_file=str(Path(path).resolve()))
    elif action!='stop': raise ValueError('Unsupported action')
    atomic(inbox.root/'inbox'/(ident+'.json'),request)
    return {'id':ident,'state':'queued','count':request.get('count')}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('status','enqueue','stop','result'))
    parser.add_argument('--work',type=Path,default=ROOT/'runtime/gui')
    parser.add_argument('--path',type=Path); parser.add_argument('--id'); parser.add_argument('--wait',type=float,default=30)
    args=parser.parse_args()
    if args.action=='status': result=live_status(args.work)
    elif args.action=='result':
        if not args.id: parser.error('--id is required')
        result=ControlInbox(args.work).result(args.id) or {'id':args.id,'state':'not_found'}
    else:
        if args.action=='enqueue' and not args.path: parser.error('--path is required')
        result=submit(args.work,args.action,args.path,args.id); ident=result['id']; inbox=ControlInbox(args.work)
        deadline=time.monotonic()+min(max(args.wait,0),55)
        terminal=('accepted','stopped','rejected','expired','unknown')
        while result.get('state') not in terminal and time.monotonic()<deadline:
            result=inbox.result(ident) or result
            if result.get('state') in terminal: break
            time.sleep(.1)
        if result.get('state') not in terminal:
            result.update(note='Still pending; query result with this id. Do not resend with a new id.')
    print(json.dumps(result,ensure_ascii=False))
    return 1 if result.get('state') in ('rejected','expired','unknown') else 0


if __name__=='__main__':
    try: sys.exit(main())
    except Exception as exc:
        print(json.dumps({'state':'rejected','error':str(exc)},ensure_ascii=False));sys.exit(1)
