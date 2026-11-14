import os,sys,json,sqlite3,secrets,hashlib,base64,struct,socket,threading,time,re
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
DB='/app/data/huddle.db'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
os.makedirs('/app/data',exist_ok=True)
init_lock=threading.Lock()
def db():
    c=sqlite3.connect(DB,timeout=5,check_same_thread=False)
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
with init_lock:
 c=db(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS channame ON channels(workspace_id,name);
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT);
CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,event_id INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message_id INTEGER,kind TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
'''); c.close()
clients=[]; clients_lock=threading.Lock()
def now(): return time.strftime('%Y-%m-%dT%H:%M:%S.',time.gmtime())+f'{int(time.time()*1000)%1000:03d}Z'
def token_user(h):
 t=h.get('Authorization','')
 if not t.startswith('Bearer '): return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t[7:],)).fetchone(); c.close(); return r
def userobj(r): return {'id':r[0],'username':r[1],'display_name':r[3] or r[1],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}
def channelobj(r): return {'id':r[0],'workspace_id':r[1],'name':r[2],'is_private':bool(r[3]),'is_dm':False,'topic':r[4] or '','is_archived':bool(r[5])}
def msgobj(c,r):
 u=c.execute('SELECT * FROM users WHERE id=?',(r[2],)).fetchone()
 return {'id':r[0],'channel_id':r[1],'author_id':r[2],'author':userobj(u),'body':r[3],'parent_id':r[4],'reply_count':c.execute('SELECT count(*) FROM messages WHERE parent_id=?',(r[0],)).fetchone()[0],'created_at':r[5],'edited_at':r[6],'files':[],'reactions':[],'mentions':[]}
def publish(frame):
 try:
  s=socket.create_connection(('127.0.0.1',6379),.3); b=json.dumps(frame,separators=(',',':')).encode()
  def cmd(*x):
   z=b'*%d\r\n'%len(x)
   for a in x: z+=b'$%d\r\n'%len(a)+a+b'\r\n'
   return z
  s.sendall(cmd(b'PUBLISH',b'huddle',b)); s.close()
 except: pass
def emit(cid,eid,mid,kind):
 c=db(); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); f={'type':kind,'event_id':eid,'channel_id':cid,'message':msgobj(c,r)}; c.close(); publish(f); return f
def poller():
 while True:
  try:
   with clients_lock: cs=list(clients)
   for cl in cs:
    if cl.get('dead'): continue
    c=db()
    for cid in list(cl['subs']):
     last=cl['last'].get(cid,0)
     rows=c.execute('SELECT event_id,message_id,kind FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last)).fetchall()
     if rows: open('/tmp/poller-trace.log','a').write('node=%s cid=%s last=%s rows=%s\\n'%(NODE,cid,last,[r[0] for r in rows]))
     for eid,mid,k in rows:
      if eid<=cl['last'].get(cid,0): continue
      f=emit(cid,eid,mid,k)
      try:
       ws_send(cl['sock'],f)
       cl['last'][cid]=eid
       open('/tmp/poller-trace.log','a').write('sent node=%s cid=%s eid=%s\\n'%(NODE,cid,eid))
      except Exception:
       cl['dead']=True
       break
    c.close()
   with clients_lock: clients[:]=[x for x in clients if not x.get('dead')]
  except Exception as e:
   open('/tmp/poller-errors.log','a').write(repr(e)+'\n')
  time.sleep(.06)
threading.Thread(target=poller,daemon=True).start()
ws_locks={}; ws_locks_guard=threading.Lock()
def ws_send(s,o):
 b=json.dumps(o,separators=(',',':')).encode(); n=len(b)
 with ws_locks_guard:
  lock=ws_locks.setdefault(s,threading.Lock())
 with lock:
  if n<126: h=bytes([129,n])
  elif n<65536: h=bytes([129,126])+struct.pack('>H',n)
  else: h=bytes([129,127])+struct.pack('>Q',n)
  try:
   old_timeout=s.gettimeout()
  except Exception:
   old_timeout=None
  try:
   s.settimeout(1.0)
   s.sendall(h+b)
  finally:
   try: s.settimeout(old_timeout)
   except Exception: pass
def _recvn(s,n):
 d=b''
 while len(d)<n:
  z=s.read(n-len(d)) if hasattr(s,'read') else s.recv(n-len(d))
  if not z: return None
  d+=z
 return d
def ws_recv(s):
 h=_recvn(s,2)
 if not h:return None
 n=h[1]&127
 if n==126:
  z=_recvn(s,2)
  if not z:return None
  n=struct.unpack('>H',z)[0]
 elif n==127:
  z=_recvn(s,8)
  if not z:return None
  n=struct.unpack('>Q',z)[0]
 m=_recvn(s,4)
 d=_recvn(s,n)
 if m is None or d is None:return None
 try:
  return json.loads(bytes(x^m[i%4] for i,x in enumerate(d)).decode('utf-8'))
 except (UnicodeDecodeError, json.JSONDecodeError):
  return {}
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,code,o):
  b=json.dumps(o).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if path.startswith('/api/ws'):
   return self.websocket(parse_qs(p.query).get('token',[''])[0])
  u=token_user(self.headers)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=db()
  if path=='/api/auth/me': c.close(); return self.sendj(200,{'user':userobj(u)})
  m=re.fullmatch(r'/api/workspaces/([^/]+)',path)
  if m:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
   if not w:c.close();return self.sendj(404,{'error':'not found'})
   chans=[channelobj(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=?',(w[0],))]
   mem=[{'user_id':x[0],'role':x[1]} for x in c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w[0],))]
   c.close();return self.sendj(200,{'workspace':{'id':w[0],'slug':w[1],'name':w[2],'owner_id':w[3],'join_mode':'open'},'channels':chans,'members':mem})
  if path=='/api/workspaces':
   a=[{'id':x[0],'slug':x[1],'name':x[2],'owner_id':x[3],'join_mode':'open'} for x in c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u[0],))]
   c.close();return self.sendj(200,{'workspaces':a})
  m=re.fullmatch(r'/api/channels/(\d+)/messages',path)
  if m:
   cid=int(m.group(1)); rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL ORDER BY id DESC',(cid,)).fetchall()
   o=[msgobj(c,x) for x in rows];c.close();return self.sendj(200,{'messages':o,'next_cursor':None})
  m=re.fullmatch(r'/api/channels/(\d+)/members',path)
  if m:
   a=[userobj(x) for x in c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(int(m.group(1)),))]
   c.close();return self.sendj(200,{'members':a})
  c.close();return self.sendj(404,{'error':'not found'})
 def do_POST(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/auth/register':
   try:
    x=self.body(); un=x['username']; pw=x['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8: raise ValueError()
    c=db(); c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,x.get('display_name',un))); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return self.sendj(201,{'user':userobj(r),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
   except:return self.sendj(400,{'error':'invalid'})
  if path=='/api/auth/login':
   x=self.body();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),x.get('password'))).fetchone()
   if not r:c.close();return self.sendj(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r[0]));c.commit();c.close();return self.sendj(200,{'user':userobj(r),'token':t})
  u=token_user(self.headers)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=db()
  try:
   if path=='/api/workspaces':
    x=self.body(); c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(x['slug'],x['name'],u[0]));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u[0],'owner'));c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u[0]));c.execute('INSERT INTO counters VALUES(?,0)',(cid,));c.commit();return self.sendj(201,{'workspace':{'id':wid,'slug':x['slug'],'name':x['name'],'owner_id':u[0],'join_mode':'open'},'general_channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',path)
   if m:
    x=self.body();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return self.sendj(404,{'error':'not found'})
    c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w[0],x['name'],int(x.get('is_private',False)),x.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u[0]));c.execute('INSERT INTO counters VALUES(?,0)',(cid,));c.commit();return self.sendj(201,{'channel':channelobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   m=re.fullmatch(r'/api/channels/(\d+)/join',path)
   if m:
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(int(m.group(1)),u[0]));c.commit();return self.sendj(200,{'joined':True})
   m=re.fullmatch(r'/api/channels/(\d+)/messages',path)
   if m:
    cid=int(m.group(1));x=self.body();body=x.get('body','')
    if not body:return self.sendj(400,{'error':'empty'})
    c.execute('BEGIN IMMEDIATE');t=now();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u[0],body,x.get('parent_id'),t));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('UPDATE counters SET event_id=event_id+1 WHERE channel_id=?',(cid,));eid=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(cid,)).fetchone()[0];kind='message.reply' if x.get('parent_id') else 'message.created';c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(cid,eid,mid,kind,t));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();o=msgobj(c,r);c.close();publish({'type':kind,'event_id':eid,'channel_id':cid,'message':o});return self.sendj(201,{'message':o})
  except sqlite3.IntegrityError:return self.sendj(409,{'error':'conflict'})
  except Exception as e:return self.sendj(400,{'error':str(e)})
  finally:
   try:c.close()
   except:pass
 def websocket(self,tk):
  c=db();r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tk,)).fetchone();c.close()
  if not r:self.send_response(401);self.end_headers();return
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:self.send_response(400);self.end_headers();return
  self.close_connection=False
  self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
  cl={'sock':self.connection,'subs':set(),'last':{},'dead':False}
  with ws_locks_guard: ws_locks.setdefault(self.connection,threading.Lock())
  with clients_lock:clients.append(cl)
  try:
   while True:
    x=ws_recv(self.rfile)
    if not x:break
    cid=int(x.get('channel_id',0))
    if x.get('type')=='subscribe':
     cl['subs'].add(cid);c=db();head=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(cid,)).fetchone();c.close();cl['last'][cid]=head[0] if head else 0;ws_send(self.connection,{'type':'subscribed','channel_id':cid,'head_event_id':cl['last'][cid]})
    elif x.get('type')=='resume':
     since=int(x.get('since_event_id',0));cl['subs'].add(cid);cl['last'][cid]=since
     c=db()
     rows=c.execute('SELECT event_id,message_id,kind FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
     for eid,mid,k in rows:
      f=emit(cid,eid,mid,k)
      ws_send(self.connection,f)
      cl['last'][cid]=eid
     head=c.execute('SELECT coalesce(max(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
     c.close()
     ws_send(self.connection,{'type':'resumed','channel_id':cid,'head_event_id':head})
  except Exception as e:
   open('/tmp/ws-errors.log','a').write(repr(e)+'\n')
  # The reader may exit after a client control frame; retain the subscription until delivery fails.
  if getattr(cl.get('sock'),'_closed',False): cl['dead']=True
srv=ThreadingHTTPServer(('127.0.0.1',PORT),H)
srv.serve_forever()
