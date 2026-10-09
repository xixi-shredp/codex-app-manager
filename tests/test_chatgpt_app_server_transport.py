import ast, base64, hashlib, importlib.util, json, os, socket, struct, tempfile, threading
from pathlib import Path
from importlib.machinery import SourceFileLoader
path=str(Path(__file__).resolve().parents[1] / 'codex-app-manager')
ast.parse(open(path).read())
spec=importlib.util.spec_from_loader('transport', SourceFileLoader('transport',path))
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
def exact(s,n):
 b=b''
 while len(b)<n:
  d=s.recv(n-len(b))
  if not d: raise EOFError()
  b+=d
 return b
def read(s):
 a,b=exact(s,2);n=b&127
 if n==126:n=struct.unpack('!H',exact(s,2))[0]
 if n==127:n=struct.unpack('!Q',exact(s,8))[0]
 assert b&128
 mask=exact(s,4);p=exact(s,n)
 return a&15,bytes(v^mask[i%4] for i,v in enumerate(p))
def frame(s,p,op=1,fin=True):
 if isinstance(p,dict):p=json.dumps(p).encode()
 head=bytes([(128 if fin else 0)|op,len(p)]) if len(p)<126 else bytes([(128 if fin else 0)|op,126])+struct.pack('!H',len(p))
 s.sendall(head+p)
errors=[]
with tempfile.TemporaryDirectory(prefix='ws-test-') as tmp:
 sp=tmp+'/sock';server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(sp);server.listen()
 def serve():
  try:
   s,_=server.accept();buf=b''
   while b'\r\n\r\n' not in buf:buf+=s.recv(4096)
   key=[l.split(b': ',1)[1] for l in buf.split(b'\r\n') if l.startswith(b'Sec-WebSocket-Key:')][0]
   acc=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
   s.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+acc+b'\r\n\r\n')
   _,p=read(s);init=json.loads(p);assert init['method']=='initialize'
   frame(s,{'id':init['id'],'result':{'ok':True}})
   _,p=read(s);assert json.loads(p)['method']=='initialized'
   _,p=read(s);req=json.loads(p)
   frame(s,b'hello',op=9);op,p=read(s);assert op==10 and p==b'hello'
   data=json.dumps({'method':'turn/completed','params':{'turnId':'fake'}}).encode()
   frame(s,data[:10],fin=False);frame(s,data[10:],op=0)
   frame(s,{'id':req['id'],'result':{'value':42}})
   frame(s,{'id':'approval','method':'item/commandExecution/requestApproval','params':{}})
   # Concurrent calls answered in reverse order to exercise ID dispatch.
   requests=[json.loads(read(s)[1]) for _ in range(2)]
   for r in reversed(requests):frame(s,{'id':r['id'],'result':{'method':r['method']}})
   frame(s,b'\x03\xe8',op=8);assert read(s)[0]==8;s.close()
  except Exception as e:errors.append(e)
 t=threading.Thread(target=serve);t.start()
 with mod.AppServerClient(sp) as c:
  assert c.peer_pid==os.getpid()
  assert c.call('test',{})=={'value':42}
  assert c.wait_notification(1)['method']=='turn/completed'
  a=c.wait_notification(1);assert a['_server_request'] and a['id']=='approval'
  out={}
  def invoke(name):out[name]=c.call(name,{})
  workers=[threading.Thread(target=invoke,args=(name,)) for name in ('one','two')]
  for w in workers:w.start()
  for w in workers:w.join()
  assert out=={'one':{'method':'one'},'two':{'method':'two'}}
  try:c.wait_notification(1)
  except mod.ConnectionClosed:pass
  else:raise AssertionError('disconnect did not wake notification waiter')
 t.join(2);assert not t.is_alive();assert not errors,errors
 server.close()
print('PASS: syntax, handshake, masking, fragmentation, ping/pong, RPC dispatch, server request notification, disconnect wakeup')
