"""Local JSON IPC: no pickle, no TCP listener, owner-only Unix sockets."""
import base64
import contextvars
import hashlib
import json
import os
import socket
import socketserver
import struct
import threading
from pathlib import Path

import cv2
import numpy as np
from fastapi import HTTPException

selected_camera=contextvars.ContextVar('selected_camera',default=None)
MAX_MESSAGE=32*1024*1024


def socket_path(name):
    root=Path(os.environ.get('FIELD_DEMO_DATA','Data'))/'Runtime'
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(root,0o700)
    return root/(hashlib.sha256(name.encode()).hexdigest()[:24]+'.sock')


def encode(value):
    if isinstance(value,np.ndarray):
        ok,raw=cv2.imencode('.png',value)
        if not ok:raise ValueError('Cannot encode frame')
        return {'__frame__':base64.b64encode(raw).decode()}
    if isinstance(value,bytes):return {'__bytes__':base64.b64encode(value).decode()}
    if isinstance(value,(list,tuple)):return [encode(v) for v in value]
    if isinstance(value,dict):return {k:encode(v) for k,v in value.items()}
    return value


def decode(value):
    if isinstance(value,dict):
        if set(value)=={'__frame__'}:
            raw=base64.b64decode(value['__frame__'],validate=True)
            # Encoded frames originate only in trusted local worker processes.
            image=cv2.imdecode(np.frombuffer(raw,dtype=np.uint8),cv2.IMREAD_COLOR)
            if image is None or image.size>48_000_000:raise ValueError('Invalid frame')
            return image
        if set(value)=={'__bytes__'}:return base64.b64decode(value['__bytes__'],validate=True)
        return {k:decode(v) for k,v in value.items()}
    if isinstance(value,list):return [decode(v) for v in value]
    return value


def read_exact(sock,size):
    pieces=[]
    while size:
        part=sock.recv(size)
        if not part:raise ConnectionError('IPC connection ended')
        pieces.append(part);size-=len(part)
    return b''.join(pieces)


def receive(sock):
    size=struct.unpack('!I',read_exact(sock,4))[0]
    if size>MAX_MESSAGE:raise ValueError('IPC message exceeds limit')
    return decode(json.loads(read_exact(sock,size)))


def send(sock,value):
    data=json.dumps(encode(value),ensure_ascii=False,allow_nan=False).encode()
    if len(data)>MAX_MESSAGE:raise ValueError('IPC message exceeds limit')
    sock.sendall(struct.pack('!I',len(data))+data)


def call(name,method,*args,**kwargs):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(90 if name=='ocr' else 5)
        sock.connect(str(socket_path(name)))
        send(sock,{'method':method,'args':args,'kwargs':kwargs})
        result=receive(sock)
    if 'error' in result:
        raise HTTPException(result.get('status',503),result['error'])
    return result['result']


class RpcServer:
    def __init__(self,name,handlers):
        self.path=socket_path(name)
        # Caller holds a process flock before replacing a stale socket.
        self.path.unlink(missing_ok=True)
        slots=threading.BoundedSemaphore(4)
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(95)
                if not slots.acquire(blocking=False):
                    send(self.request,{'error':'Worker busy','status':503});return
                try:
                    req=receive(self.request)
                    method=req.get('method')
                    if method not in handlers:
                        raise HTTPException(404,'Unknown internal operation')
                    result=handlers[method](*req.get('args',[]),**req.get('kwargs',{}))
                    send(self.request,{'result':result})
                except Exception as exc:
                    send(self.request,{'error':exc.detail if isinstance(exc,HTTPException) else type(exc).__name__,
                                       'status':exc.status_code if isinstance(exc,HTTPException) else 503})
                finally:
                    slots.release()
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads=True
            request_queue_size=8
        self.server=Server(str(self.path),Handler)
        os.chmod(self.path,0o600)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)

    def start(self):self.thread.start()
    def close(self):
        self.server.shutdown();self.server.server_close();self.path.unlink(missing_ok=True)


class CameraProxy:
    def __init__(self,target):self.default_target=target
    @property
    def target(self):return selected_camera.get() or self.default_target
    def snapshot(self):
        try:return call('camera:'+self.target,'receiver.snapshot')
        except (OSError,HTTPException):return {'id':self.target,'status':'worker_unavailable','service_status_available':False,'service_status':{}}
    def preview_part(self,recognition,previous):
        return call('camera:'+self.target,'receiver.preview',recognition,previous)
    def frame(self):
        try:return call('camera:'+self.target,'receiver.frame')
        except (OSError,HTTPException) as exc:raise ValueError('Camera worker unavailable') from exc
    def start(self):pass
    def close(self):pass


class ComponentProxy:
    def __init__(self,name,target):
        self.name=name;self.default_target=target;self.lock=threading.RLock()
    @property
    def target_id(self):return selected_camera.get() or self.default_target
    @property
    def session(self):return call('camera:'+self.target_id,'scanner.snapshot')
    def camera(self):return CameraProxy(self.target_id)
    def target(self):return self.target_id
    def __getattr__(self,method):
        def invoke(*args,**kwargs):
            args=[a.model_dump(mode='json') if hasattr(a,'model_dump') else str(a) if isinstance(a,__import__('uuid').UUID) else a for a in args]
            try:return call('camera:'+self.target_id,self.name+'.'+method,*args,**kwargs)
            except (OSError,HTTPException):
                if method=='snapshot':return {'status':'worker_unavailable','enabled':False,'camera_id':self.target_id}
                if method=='preview':return None
                raise
        return invoke
    def close(self):pass


class DetectorProxy:
    def enabled(self):return os.environ.get('FIELD_PANEL_DETECTOR_ENABLED','0')=='1'
    def snapshot(self):
        try:return call('ocr','detector.snapshot')
        except (OSError,HTTPException):return {'status':'worker_unavailable','enabled':self.enabled(),'resident':False}
    def close(self):pass
    def warmup(self):return call('ocr','warmup')


def worker_role():
    return os.environ.get('FIELD_SERVICE_ROLE','combined')


def camera_component(name,target):
    return ComponentProxy(name,target) if worker_role()=='api' else None
