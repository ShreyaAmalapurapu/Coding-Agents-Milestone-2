import os,sys,json,sqlite3,time,uuid,re,hashlib,base64,struct,threading,subprocess,signal,socket
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
from datetime import datetime,timezone
DB='/app/data/huddle.sqlite'; os.makedirs('/app/data',exist_ok=True)
PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
db_lock=threading.RLock(); sockets=[]; sockets_lock=threading.RLock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def conn():
 c=sqlite3.connect(DB,timeout=5,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=5000'); return c
def init():
 c=conn()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created REAL,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,created REAL);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created REAL,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
 CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,deleted INTEGER DEFAULT 0,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS online_users(user_id INTEGER PRIMARY KEY,updated REAL);
 CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
 CREATE TABLE IF NOT EXISTS mention_counts(user_id INTEGER,channel_id INTEGER,n INTEGER,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,uses INTEGER DEFAULT 0,max_uses INTEGER DEFAULT 1);'''); c.commit(); c.close()
init()
def userobj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def channelobj(r):
 return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['archived'])}
def reactions_of(c,mid):
 rows=c.execute('select emoji,user_id from reactions where message_id=? order by rowid',(mid,)).fetchall()
 out=[]
 for x in rows:
  z=next((a for a in out if a['emoji']==x['emoji']),None)
  if not z: z={'emoji':x['emoji'],'count':0,'user_ids':[]}; out.append(z)
  z['count']+=1; z['user_ids'].append(x['user_id'])
 return out
def fileobj(r):
 return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def resolve_mentions(c,ch,author,body):
 r=c.execute('select workspace_id from channels where id=?',(ch,)).fetchone(); ids=[]
 if not r:return ids
 import re
 for m in re.finditer(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body):
  h=m.group(1); found=[]
  if h=='channel': found=[x[0] for x in c.execute('select user_id from cmembers where channel_id=?',(ch,))]
  elif h=='here': found=[x[0] for x in c.execute('select cm.user_id from cmembers cm join online_users o on o.user_id=cm.user_id where cm.channel_id=?',(ch,))]
  else:
   z=c.execute('select id from users where username=?',(h,)).fetchone()
   if z: found=[z[0]]
   else:
    g=c.execute('select id from groups where workspace_id=? and handle=? and deleted=0',(r[0],h)).fetchone()
    if g: found=[x[0] for x in c.execute('select gm.user_id from group_members gm join cmembers cm on cm.user_id=gm.user_id where gm.group_id=? and cm.channel_id=?',(g[0],ch))]
  for uid in found:
   if uid!=author and uid not in ids:ids.append(uid)
 return ids
def messageobj(c,r):
 u=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[fileobj(z) for z in c.execute('select f.* from files f join attachments a on a.file_id=f.id where a.message_id=?',(r['id'],)).fetchall()],'reactions':reactions_of(c,r['id']),'mentions':[x[0] for x in c.execute('select user_id from message_mentions where message_id=?',(r['id'],)).fetchall()]}
def emit(c,ch,kind,payload):
 c.execute('insert or ignore into counters(channel_id,n) values(?,0)',(ch,))
 c.execute('update counters set n=n+1 where channel_id=?',(ch,))
 n=c.execute('select n from counters where channel_id=?',(ch,)).fetchone()[0]
 c.execute('insert into events values(?,?,?,?,?)',(ch,n,kind,json.dumps(payload),time.time())); c.commit(); return n
def broadcast_global(frame):
 with sockets_lock:
  for s in list(sockets): s.send(frame)
def broadcast(ch,eid,kind,payload):
 frame={'type':kind,'event_id':eid,'channel_id':ch}; frame.update(payload)
 with sockets_lock:
  for s in list(sockets):
   if ch in s.subs: s.send(frame,ch,eid)
def auth(h):
 x=h.get('Authorization','')
 if not x.startswith('Bearer '): return None
 c=conn(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(x[7:],)).fetchone(); c.close(); return r
class WS:
 def __init__(self,raw,u):
  self.raw=raw; self.u=u; self.subs=set(); self.last={}; self.alive=True; self.send_lock=threading.Lock()
 def send(self,obj,ch=None,eid=None):
  if ch and eid and eid<=self.last.get(ch,0): return
  try:
   with self.send_lock:
    if ch and eid and eid<=self.last.get(ch,0): return
    b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
    if n<126: hdr=bytes([129,n])
    elif n<65536: hdr=bytes([129,126])+struct.pack('!H',n)
    else: hdr=bytes([129,127])+struct.pack('!Q',n)
    self.raw.sendall(hdr+b)
    if ch and eid:self.last[ch]=eid
  except: self.alive=False
 def run(self):
  self.raw.settimeout(.5)
  with sockets_lock:sockets.append(self)
  cc=conn(); cc.execute('insert or replace into online_users values(?,?)',(self.u['id'],time.time())); cc.commit(); cc.close()
  try:
   while self.alive:
    try:
     h=self.raw.recv(2)
     if not h:break
     op=h[0]&15; masked=h[1]&128; n=h[1]&127
     if n==126:n=struct.unpack('!H',self.raw.recv(2))[0]
     if n==127:n=struct.unpack('!Q',self.raw.recv(8))[0]
     mask=self.raw.recv(4) if masked else b''; data=self.raw.recv(n)
     if masked:data=bytes(data[i]^mask[i%4] for i in range(n))
     if op==8:break
     if op==1:
      q=json.loads(data); typ=q.get('type'); ch=int(q.get('channel_id',0)); c=conn()
      head=c.execute('select n from counters where channel_id=?',(ch,)).fetchone(); head=head[0] if head else 0
      if typ=='subscribe':self.subs.add(ch);self.send({'type':'subscribed','channel_id':ch,'head_event_id':head})
      elif typ=='resume':
       since=int(q.get('since_event_id',0)); self.subs.add(ch)
       rows=c.execute('select event_id,kind,payload from events where channel_id=? and event_id>? order by event_id',(ch,since)).fetchall()
       for r in rows:
        p=json.loads(r['payload']); f={'type':r['kind'],'event_id':r['event_id'],'channel_id':ch};f.update(p);self.send(f,ch,r['event_id'])
       self.send({'type':'resumed','channel_id':ch,'head_event_id':head})
      c.close()
    except TimeoutError: pass
    except socket_error:break
    except Exception: pass
  finally:
   with sockets_lock:
    if self in sockets:sockets.remove(self)
   cc=conn(); cc.execute('delete from online_users where user_id=?',(self.u['id'],)); cc.commit(); cc.close()
def socket_error(): return ConnectionError
def poller():
 last=0; glast=0
 while True:
  try:
   c=conn(); grows=c.execute('select id,kind,payload from global_events where id>? order by id',(glast,)).fetchall()
   for gr in grows:
    glast=gr['id']; broadcast_global(json.loads(gr['payload']))
   rows=c.execute('select channel_id,event_id,kind,payload from events where rowid>? order by rowid',(last,)).fetchall()
   for r in rows:
    last=max(last,c.execute('select rowid from events where channel_id=? and event_id=?',(r['channel_id'],r['event_id'])).fetchone()[0])
    p=json.loads(r['payload']); broadcast(r['channel_id'],r['event_id'],r['kind'],p)
   c.close()
  except: pass
  time.sleep(.05)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def sendj(self,status,obj,headers=None):
  b=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)))
  if headers:
   for k,v in headers.items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/':
   b=open('/app/index.html','rb').read();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  if path=='/static/app.css' or path=='/static/app.js':
   fn='/app/'+('app.css' if path.endswith('.css') else 'app.js');b=open(fn,'rb').read();self.send_response(200);self.send_header('Content-Type','text/css' if fn.endswith('.css') else 'application/javascript');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
  if path.startswith('/api/ws'):
   u=auth(self.headers)
   if not u:
    qt=parse_qs(p.query).get('token',[''])[0]
    if qt:
     cc=conn(); u=cc.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(qt,)).fetchone(); cc.close()
   if not u:return self.sendj(401,{'error':'unauthorized'})
   key=self.headers.get('Sec-WebSocket-Key')
   if not key:return self.sendj(400,{'error':'websocket required'})
   accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101);self.send_header('Upgrade','WebSocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
   WS(self.connection,u).run();return
  u=auth(self.headers)
  if path=='/api/auth/me':
   if not u:return self.sendj(401,{'error':'unauthorized'})
   return self.sendj(200,{'user':userobj(u)})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=conn(); parts=path.strip('/').split('/')
  if len(parts)==4 and parts[1]=='files':
   fid=int(parts[2]); fr=c.execute('select * from files where id=?',(fid,)).fetchone()
   if not fr:c.close(); return self.sendj(404,{'error':'not found'})
   if parts[3]=='download':
    try:
     data=open(fr['path'],'rb').read()
     self.send_response(200); self.send_header('Content-Type',fr['content_type']); self.send_header('Content-Disposition','attachment; filename="'+fr['filename'].replace('"','')+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    finally:c.close()
    return
  if len(parts)==3 and parts[1]=='files':
   fr=c.execute('select * from files where id=?',(int(parts[2]),)).fetchone(); c.close()
   return self.sendj(200,{'file':fileobj(fr)}) if fr else self.sendj(404,{'error':'not found'})

  try:
   if parts[:2]==['api','users'] and len(parts)==3:
    r=c.execute('select * from users where id=?',(parts[2],)).fetchone()
    return self.sendj(200,{'user':userobj(r)}) if r else self.sendj(404,{'error':'not found'})
   if path=='/api/workspaces':
    rs=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall()
    return self.sendj(200,{'workspaces':[dict(r) for r in rs]})
   if len(parts)==4 and parts[1]=='workspaces' and parts[3]=='groups':
    w=c.execute('select * from workspaces where slug=?',(parts[2],)).fetchone()
    if not w:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    gs=[]
    for g in c.execute('select * from groups where workspace_id=? and deleted=0',(w['id'],)).fetchall():
     gs.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x[0] for x in c.execute('select user_id from group_members where group_id=?',(g['id'],))]})
    return self.sendj(200,{'groups':gs})
   if len(parts)==4 and parts[1]=='workspaces' and parts[3]=='members':
    w=c.execute('select * from workspaces where slug=?',(parts[2],)).fetchone()
    if not w:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    return self.sendj(200,{'members':[{'user_id':r['user_id'],'role':r['role']} for r in c.execute('select user_id,role from members where workspace_id=?',(w['id'],)).fetchall()]})
   if parts[:2]==['api','workspaces'] and len(parts)==3:
    w=c.execute('select * from workspaces where slug=?',(parts[2],)).fetchone()
    if not w:return self.sendj(404,{'error':'not found'})
    cs=c.execute('select * from channels where workspace_id=? and archived=0',(w['id'],)).fetchall()
    return self.sendj(200,{'workspace':dict(w),'channels':[channelobj(x) for x in cs],'read_state':{}})
   if len(parts)==4 and parts[1]=='channels' and parts[3]=='read':
    ch=int(parts[2]); channel=c.execute('select * from channels where id=?',(ch,)).fetchone()
    if not channel:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch,u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    head=c.execute('select coalesce(n,0) from counters where channel_id=?',(ch,)).fetchone(); head=head[0] if head else 0
    rs=c.execute('select last_read_event_id from read_states where user_id=? and channel_id=?',(u['id'],ch)).fetchone(); last=rs[0] if rs else 0
    mc=c.execute('select coalesce(n,0) from mention_counts where user_id=? and channel_id=?',(u['id'],ch)).fetchone()
    state={'channel_id':ch,'last_read_event_id':last,'unread_count':max(0,head-last),'mention_count':mc[0] if mc else 0}
    return self.sendj(200,{'read_state':state})
   if parts[:2]==['api','channels'] and len(parts)==3:
    ch=c.execute('select * from channels where id=?',(parts[2],)).fetchone()
    if not ch:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    if path.endswith('/members'): pass
   if len(parts)==4 and parts[1]=='channels' and parts[3]=='members':
    rs=c.execute('select u.* from users u join cmembers m on m.user_id=u.id where m.channel_id=?',(parts[2],)).fetchall();return self.sendj(200,{'members':[userobj(x) for x in rs]})
   if len(parts)==4 and parts[1]=='channels' and parts[3]=='messages':
    ch=int(parts[2]); channel=c.execute('select * from channels where id=?',(ch,)).fetchone()
    if not channel:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch,u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    rs=c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(ch,)).fetchall()
    return self.sendj(200,{'messages':[messageobj(c,x) for x in rs],'next_cursor':None})
   if len(parts)==4 and parts[1]=='messages' and parts[3]=='replies':
    rs=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(parts[2],)).fetchall();return self.sendj(200,{'replies':[messageobj(c,x) for x in rs],'next_cursor':None})
  finally:c.close()
  self.sendj(404,{'error':'not found'})
 def do_POST(self):
  p=urlparse(self.path);path=p.path
  if path=='/api/auth/register':
   try:
    x=self.body(); un=x['username'];pw=x['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:raise ValueError()
    c=conn();c.execute('insert into users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) values(?,?,?,?,?,?,?)',(un,pw,x.get('display_name') or un,'UTC','','',''));uid=c.execute('select last_insert_rowid()').fetchone()[0];t=uuid.uuid4().hex+uuid.uuid4().hex;c.execute('insert into tokens values(?,?)',(t,uid));c.commit();r=c.execute('select * from users where id=?',(uid,)).fetchone();c.close();return self.sendj(201,{'user':userobj(r),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
   except:return self.sendj(400,{'error':'invalid'})
  if path=='/api/auth/login':
   x=self.body();c=conn();r=c.execute('select * from users where username=? and password=?',(x.get('username'),x.get('password'))).fetchone()
   if not r:c.close();return self.sendj(401,{'error':'invalid'})
   t=uuid.uuid4().hex+uuid.uuid4().hex;c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();c.close();return self.sendj(200,{'user':userobj(r),'token':t})
  u=auth(self.headers)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  if path=='/api/files':
   n=int(self.headers.get('Content-Length','0'))
   raw=self.rfile.read(n)
   if n>10*1024*1024+1024:return self.sendj(413,{'error':'too large'})
   ct=self.headers.get('Content-Type','')
   if 'multipart/form-data' not in ct:return self.sendj(400,{'error':'multipart required'})
   try:
    bm=re.search(r'boundary=(?:"([^"]+)"|([^;]+))',ct)
    if not bm:return self.sendj(400,{'error':'boundary required'})
    boundary=(bm.group(1) or bm.group(2)).encode()
    found=None
    for section in raw.split(b'--'+boundary):
     if b'Content-Disposition' not in section: continue
     head,sep,data=section.partition(b'\r\n\r\n')
     if not sep: head,sep,data=section.partition(b'\n\n')
     if b'name="file"' not in head and b"name='file'" not in head: continue
     if data.endswith(b'\r\n'): data=data[:-2]
     elif data.endswith(b'\n'): data=data[:-1]
     dm=re.search(br'filename="([^"]*)"',head,re.I)
     tm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
     found=(dm.group(1).decode('utf-8','replace') if dm else 'file',tm.group(1).decode().strip() if tm else 'application/octet-stream',data); break
    if found is None:return self.sendj(400,{'error':'file required'})
    fn,typ,data=found
    if len(data)>10*1024*1024:return self.sendj(413,{'error':'too large'})
    created=now()
    c=conn(); os.makedirs('/app/data/files',exist_ok=True)
    c.execute('insert into files(uploader_id,filename,content_type,size,created_at,path) values(?,?,?,?,?,?)',(u['id'],fn,typ,len(data),'%s'%created,''))
    fid=c.execute('select last_insert_rowid()').fetchone()[0]; fp='/app/data/files/'+str(fid); open(fp,'wb').write(data)
    c.execute('update files set path=? where id=?',(fp,fid));c.commit(); fr=c.execute('select * from files where id=?',(fid,)).fetchone();c.close()
    return self.sendj(201,{'file':fileobj(fr)})
   except Exception:return self.sendj(400,{'error':'invalid multipart'})
  x=self.body();c=conn();parts=path.strip('/').split('/')
  try:
   if len(parts)==4 and parts[1]=='workspaces' and parts[3]=='members':
    w=c.execute('select * from workspaces where slug=?',(parts[2],)).fetchone()
    if not w:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    return self.sendj(200,{'members':[{'user_id':r['user_id'],'role':r['role']} for r in c.execute('select user_id,role from members where workspace_id=?',(w['id'],)).fetchall()]})
   if len(parts)==4 and parts[1]=='workspaces' and parts[3]=='invitations':
    w=c.execute('select * from workspaces where slug=?',(parts[2],)).fetchone()
    if not w:return self.sendj(404,{'error':'not found'})
    if w['owner_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
    code=uuid.uuid4().hex[:16];c.execute('insert into invitations(code,workspace_id,creator_id) values(?,?,?)',(code,w['id'],u['id']));c.commit()
    return self.sendj(201,{'invitation':{'code':code,'workspace_id':w['id']}})
   if len(parts)==4 and parts[1]=='invitations' and parts[3]=='accept':
    inv=c.execute('select * from invitations where code=?',(parts[2],)).fetchone()
    if not inv or inv['uses']>=inv['max_uses']:return self.sendj(404,{'error':'not found'})
    c.execute('insert or ignore into members values(?,?,?)',(inv['workspace_id'],u['id'],'member'));c.execute('update invitations set uses=uses+1 where code=?',(parts[2],));c.commit()
    return self.sendj(200,{'workspace_id':inv['workspace_id']})
   if path=='/api/dms':
    oid=x.get('user_id')
    if oid is None:return self.sendj(400,{'error':'user_id required'})
    other=c.execute('select * from users where id=?',(oid,)).fetchone()
    if not other:return self.sendj(404,{'error':'not found'})
    a,b=sorted((int(u['id']),int(oid))); name='dm-'+str(a)+'-'+str(b)
    r=c.execute('select * from channels where name=? and is_dm=1',(name,)).fetchone()
    if not r:
     c.execute('insert into channels(workspace_id,name,is_dm,topic) values(NULL,?,1,?)',(name,name))
     cid=c.execute('select last_insert_rowid()').fetchone()[0]
     c.execute('insert into cmembers values(?,?)',(cid,a)); c.execute('insert into cmembers values(?,?)',(cid,b)); c.commit()
    else: cid=r['id']
    return self.sendj(200,{'channel_id':cid})
   if len(parts)==4 and parts[1]=='messages' and parts[3]=='reactions':
    mid=int(parts[2]); m=c.execute('select * from messages where id=? and deleted=0',(mid,)).fetchone()
    if not m:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(m['channel_id'],u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    emoji=x.get('emoji')
    if not isinstance(emoji,str) or not emoji:return self.sendj(400,{'error':'emoji required'})
    exists=c.execute('select 1 from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji)).fetchone()
    if not exists:
     c.execute('insert into reactions values(?,?,?,?)',(mid,u['id'],emoji,time.time()))
     eid=emit(c,m['channel_id'],'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':u['id']})
    c.commit(); return self.sendj(200,{'reactions':reactions_of(c,mid)})
   if len(parts)==4 and parts[1]=='workspaces' and parts[3]=='groups':
    w=c.execute('select * from workspaces where slug=?',(parts[2],)).fetchone()
    if not w:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    h=x.get('handle','')
    if not re.fullmatch(r'[a-z0-9_-]{2,32}',h):return self.sendj(400,{'error':'invalid handle'})
    if c.execute('select 1 from groups where workspace_id=? and handle=? and deleted=0',(w['id'],h)).fetchone():return self.sendj(409,{'error':'duplicate'})
    c.execute('insert into groups(workspace_id,handle,name,creator_id) values(?,?,?,?)',(w['id'],h,x.get('name',''),u['id']));gid=c.execute('select last_insert_rowid()').fetchone()[0]
    for uid in x.get('user_ids',[]): c.execute('insert or ignore into group_members values(?,?)',(gid,uid))
    c.commit();return self.sendj(201,{'group':{'id':gid,'handle':h,'name':x.get('name',''),'member_user_ids':x.get('user_ids',[])}})
   if parts[:2]==['api','workspaces'] and len(parts)==2:
    slug=x.get('slug',''); 
    if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return self.sendj(400,{'error':'invalid'})
    c.execute('insert into workspaces(slug,name,owner_id,join_mode) values(?,?,?,"open")',(slug,x.get('name',slug),u['id']));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));ch=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(ch,u['id']));c.commit();w=c.execute('select * from workspaces where id=?',(wid,)).fetchone();cc=c.execute('select * from channels where id=?',(ch,)).fetchone();return self.sendj(201,{'workspace':dict(w),'general_channel':channelobj(cc)})
   if len(parts)==4 and parts[1]=='workspaces' and parts[3]=='channels':
    w=c.execute('select * from workspaces where slug=?',(parts[2],)).fetchone(); name=x.get('name','')
    if not w:return self.sendj(404,{'error':'not found'})
    if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],name)).fetchone():return self.sendj(409,{'error':'duplicate'})
    c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],name,int(x.get('is_private',False)),x.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(cid,u['id']));c.commit();return self.sendj(201,{'channel':channelobj(c.execute('select * from channels where id=?',(cid,)).fetchone())})
   if len(parts)==4 and parts[1]=='channels' and parts[3]=='join':
    ch=int(parts[2]);r=c.execute('select * from channels where id=?',(ch,)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    c.execute('insert or ignore into cmembers values(?,?)',(ch,u['id']));c.execute('insert or ignore into members select workspace_id,?,\"member\" from channels where id=?',(u['id'],ch));c.commit();return self.sendj(200,{'channel':channelobj(r)})
   if len(parts)==4 and parts[1]=='channels' and parts[3]=='messages':
    ch=int(parts[2]);r=c.execute('select * from channels where id=?',(ch,)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch,u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    body=str(x.get('body',''))
    if r['archived'] and not (body.startswith('/unarchive') and body.strip()=='/unarchive'): return self.sendj(423,{'error':'archived'})
    if not body.strip():return self.sendj(400,{'error':'empty'})
    if body.startswith('/') and not body.startswith('//'):
     cmd,*rest=body.split(' ',1); text=rest[0].strip() if rest else ''
     w=c.execute('select * from workspaces where id=?',(r['workspace_id'],)).fetchone()
     owner=bool(w and w['owner_id']==u['id'])
     if cmd=='/me':
      if not text:return self.sendj(400,{'error':'text required'})
     elif cmd=='/shrug':
      body=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
     elif cmd=='/topic':
      if len(text)>250:return self.sendj(400,{'error':'topic too long'})
      if not text:return self.sendj(400,{'error':'topic required'})
      c.execute('update channels set topic=? where id=?',(text,ch));c.commit(); cc=c.execute('select * from channels where id=?',(ch,)).fetchone()
      eid=emit(c,ch,'channel.updated',{'channel':channelobj(cc)}); return self.sendj(201,{'message':None,'channel':channelobj(cc)})
     elif cmd=='/invite':
      name=text[1:] if text.startswith('@') else text; target=c.execute('select * from users where username=?',(name,)).fetchone()
      if not target:return self.sendj(404,{'error':'not found'})
      c.execute('insert or ignore into cmembers values(?,?)',(ch,target['id'],));c.commit();return self.sendj(201,{'message':None,'invited_user':userobj(target)})
     elif cmd in ('/archive','/unarchive'):
      if not owner:return self.sendj(403,{'error':'forbidden'})
      val=1 if cmd=='/archive' else 0;c.execute('update channels set archived=? where id=?',(val,ch));c.commit();cc=c.execute('select * from channels where id=?',(ch,)).fetchone()
      eid=emit(c,ch,'channel.archived' if val else 'channel.unarchived',{'channel':channelobj(cc)});return self.sendj(201,{'message':None,'channel':channelobj(cc)})
     else:return self.sendj(400,{'error':'unknown command'})
    fids=x.get('file_ids',[])
    if not isinstance(fids,list):return self.sendj(400,{'error':'file_ids'})
    for fid in fids:
     fr=c.execute('select * from files where id=?',(fid,)).fetchone()
     if not fr or fr['uploader_id']!=u['id'] or c.execute('select 1 from attachments where file_id=?',(fid,)).fetchone():return self.sendj(400,{'error':'invalid file'})
    c.execute('begin immediate');c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch,u['id'],body,x.get('parent_id'),now()));mid=c.execute('select last_insert_rowid()').fetchone()[0]
    for fid in fids:c.execute('insert into attachments values(?,?)',(mid,fid))
    mids=resolve_mentions(c,ch,u['id'],body)
    for uid in mids:
     c.execute('insert into message_mentions values(?,?)',(mid,uid)); c.execute('insert or ignore into mention_counts values(?,?,0)',(uid,ch)); c.execute('update mention_counts set n=n+1 where user_id=? and channel_id=?',(uid,ch))
    m=messageobj(c,c.execute('select * from messages where id=?',(mid,)).fetchone());eid=emit(c,ch,'message.reply' if x.get('parent_id') else 'message.created',{'message':m});return self.sendj(201,{'message':m})
  finally:c.close()
  self.sendj(404,{'error':'not found'})
 def do_PATCH(self):
  u=auth(self.headers)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  path=urlparse(self.path).path;x=self.body();c=conn();parts=path.strip('/').split('/')
  try:
   if len(parts)==5 and parts[1]=='workspaces' and parts[3]=='groups':
    w=c.execute('select * from workspaces where slug=?',(parts[2],)).fetchone();g=c.execute('select * from groups where workspace_id=? and handle=? and deleted=0',(w['id'],parts[4])).fetchone() if w else None
    if not g:return self.sendj(404,{'error':'not found'})
    if g['creator_id']!=u['id'] and w['owner_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
    if 'name' in x:c.execute('update groups set name=? where id=?',(x['name'],g['id']))
    if 'user_ids' in x:
     c.execute('delete from group_members where group_id=?',(g['id'],))
     for uid in x['user_ids']:c.execute('insert or ignore into group_members values(?,?)',(g['id'],uid))
    c.commit();g=c.execute('select * from groups where id=?',(g['id'],)).fetchone();return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[z[0] for z in c.execute('select user_id from group_members where group_id=?',(g['id'],))]}})
   if len(parts)==4 and parts[1]=='channels' and parts[3] == 'settings':
    pass
   if len(parts)==3 and parts[1]=='channels':
    ch=c.execute('select * from channels where id=?',(parts[2],)).fetchone()
    if not ch:return self.sendj(404,{'error':'not found'})
    if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
    if 'topic' not in x or not isinstance(x.get('topic'),str) or len(x['topic'])>250:return self.sendj(400,{'error':'invalid topic'})
    c.execute('update channels set topic=? where id=?',(x['topic'],ch['id']));c.commit();ch=c.execute('select * from channels where id=?',(ch['id'],)).fetchone()
    emit(c,ch['id'],'channel.updated',{'channel':channelobj(ch)})
    return self.sendj(200,{'channel':channelobj(ch)})
   if path=='/api/users/me':
    fields=['display_name','timezone','avatar_url','status_text','status_emoji'];r=c.execute('select * from users where id=?',(u['id'],)).fetchone()
    for k in fields:
     if k in x:
      if not isinstance(x[k],str) or (k=='display_name' and not 1<=len(x[k])<=80) or (k=='status_text' and len(x[k])>100) or (k=='status_emoji' and len(x[k])>64) or (k=='avatar_url' and len(x[k])>2048):return self.sendj(400,{'error':'invalid'})
      if k=='timezone':
       try: ZoneInfo(x[k])
       except Exception: return self.sendj(400,{'error':'invalid timezone'})
      c.execute(f'update users set {k}=? where id=?',(x[k],u['id']))
    c.commit(); uu=userobj(c.execute('select * from users where id=?',(u['id'],)).fetchone())
    c.execute('insert into global_events(kind,payload,created) values(?,?,?)',('user.updated',json.dumps({'type':'user.updated','user':uu}),time.time())); c.commit()
    return self.sendj(200,{'user':uu})
   if len(parts)==3 and parts[1]=='messages':
    r=c.execute('select * from messages where id=?',(parts[2],)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    if r['author_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
    if r['deleted']:return self.sendj(409,{'error':'deleted'})
    if not str(x.get('body','')).strip():return self.sendj(400,{'error':'empty'})
    c.execute('update messages set body=?,edited_at=? where id=?',(x['body'],now(),r['id']));c.commit();m=messageobj(c,c.execute('select * from messages where id=?',(r['id'],)).fetchone());eid=emit(c,r['channel_id'],'message.updated',{'message':m});return self.sendj(200,{'message':m})
  finally:c.close()
  self.sendj(404,{'error':'not found'})
 def do_DELETE(self):
  u=auth(self.headers)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  parts=self.path.strip('/').split('/');c=conn()
  if len(parts)==5 and parts[1]=='workspaces' and parts[3]=='groups':
   w=c.execute('select * from workspaces where slug=?',(parts[2],)).fetchone()
   g=c.execute('select * from groups where workspace_id=? and handle=?',(w['id'],parts[4])).fetchone() if w else None
   if not g:return self.sendj(200,{'deleted':True})
   if g['creator_id']!=u['id'] and w['owner_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
   c.execute('update groups set deleted=1 where id=?',(g['id'],));c.commit();return self.sendj(200,{'deleted':True})
  if len(parts)==3 and parts[1]=='messages':
   r=c.execute('select * from messages where id=?',(parts[2],)).fetchone()
   if not r:return self.sendj(404,{'error':'not found'})
   if r['deleted']:return self.sendj(200,{'deleted':True})
   owner=c.execute('select w.owner_id from workspaces w join channels ch on ch.workspace_id=w.id where ch.id=?',(r['channel_id'],)).fetchone()
   if r['author_id']!=u['id'] and (not owner or owner['owner_id']!=u['id']):return self.sendj(403,{'error':'forbidden'})
   c.execute('update messages set deleted=1 where id=?',(r['id'],));eid=emit(c,r['channel_id'],'message.deleted',{'message_id':r['id']});return self.sendj(200,{'deleted':True})
  if len(parts)==5 and parts[1]=='messages' and parts[3]=='reactions':
   mid=int(parts[2]);m=c.execute('select * from messages where id=? and deleted=0',(mid,)).fetchone()
   if not m:return self.sendj(404,{'error':'not found'})
   if not c.execute('select 1 from cmembers where channel_id=? and user_id=?',(m['channel_id'],u['id'])).fetchone():return self.sendj(403,{'error':'forbidden'})
   emoji=unquote(parts[4]); row=c.execute('select 1 from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji)).fetchone()
   if row:
    c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji)); emit(c,m['channel_id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':u['id']})
   c.commit();return self.sendj(200,{'reactions':reactions_of(c,mid)})
  self.sendj(404,{'error':'not found'})
if __name__=='__main__':
 threading.Thread(target=poller,daemon=True).start()
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
