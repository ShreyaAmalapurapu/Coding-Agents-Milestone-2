import os,sys,json,sqlite3,secrets,re,hashlib,base64,struct,socket,threading,time
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
DB='/app/data/chat.db'; os.makedirs('/app/data',exist_ok=True)
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB)'''); c.commit(); c.close()
init()
sockets=[]; socklock=threading.Lock(); event_seen={}; ircs=[]; irclock=threading.Lock()
def userobj(r): return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def getuser(c,uid): return c.execute('select * from users where id=?',(uid,)).fetchone()
def msgobj(c,r):
 a=getuser(c,r['author_id']); rx={}
 for x in c.execute('select emoji,user_id from reactions where message_id=?',(r['id'],)): rx.setdefault(x['emoji'],[]).append(x['user_id'])
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[{'emoji':e,'count':len(u),'user_ids':u} for e,u in rx.items()],'mentions':[]}
def emit(c,ch,typ,payload,origin=None):
 c.execute('insert or ignore into counters(channel_id,n) values(?,0)',(ch,)); n=c.execute('update counters set n=n+1 where channel_id=? returning n',(ch,)).fetchone()[0]
 c.execute('insert into events values(?,?,?,?)',(ch,n,typ,json.dumps(payload))); c.commit()
 frame={'type':typ,'event_id':n,'channel_id':ch}; frame.update(payload)
 raw=json.dumps(frame)
 with socklock:
  for ws,cid in list(sockets):
   if cid==ch and event_seen.get(ws,0)<n:
    try: ws.send(raw); event_seen[ws]=n
    except Exception: pass
 with irclock:
  for q in list(ircs):
   if q is origin: continue
   if ch in q['channels'] and typ=='message.created':
    m=payload['message']; au=m['author']['username']; line=f":{au}!{au}@localhost PRIVMSG {q['names'][ch]} :{m['body']}\r\n"
    try:q['sock'].sendall(line.encode())
    except: pass
 return n
class WS:
 def __init__(self,s): self.s=s
 def send(self,x):
  b=x.encode(); n=len(b); h=bytes([129])+(bytes([n]) if n<126 else bytes([126])+struct.pack('>H',n))
  self.s.sendall(h+b)
 def recv(self):
  h=self.s.recv(2)
  if not h:return None
  n=h[1]&127
  if n==126:n=struct.unpack('>H',self.s.recv(2))[0]
  mask=self.s.recv(4); b=self.s.recv(n); return bytes(b[i]^mask[i%4] for i in range(n)).decode()
def auth(h):
 x=h.get('Authorization','')
 if not x.startswith('Bearer '): return None
 c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(x[7:],)).fetchone(); c.close(); return r
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def sendj(self,code,o):
  b=json.dumps(o).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',len(b)); self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length',0)); return json.loads(self.rfile.read(n) or '{}')
 def sendfile(self,f,ct):
  try:b=open(f,'rb').read()
  except FileNotFoundError:
   self.send_error(404); return
  self.send_response(200); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def do_GET(self):
  if self.headers.get('Upgrade','').lower()=='websocket':
   u=auth({'Authorization':'Bearer '+parse_qs(urlparse(self.path).query).get('token',[''])[0]})
   if not u:return self.send_error(401)
   key=self.headers.get('Sec-WebSocket-Key',''); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
   ws=WS(self.connection);cid=None
   with socklock:
    sockets.append((ws,None))
    event_seen[ws]=-1
   try:
    while True:
     z=ws.recv()
     if not z:break
     q=json.loads(z);cid=q.get('channel_id',cid)
     with socklock:sockets[:]=[(a,cid if a is ws else b) for a,b in sockets]
     c=db()
     if q.get('type')=='subscribe':
      h=c.execute('select coalesce(n,0) n from counters where channel_id=?',(cid,)).fetchone(); event_seen[ws]=h['n'] if h else 0
      ws.send(json.dumps({'type':'subscribed','channel_id':cid,'head_event_id':h['n'] if h else 0}))
     elif q.get('type')=='resume':
      for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,q.get('since_event_id',0))):
       f=json.loads(e['payload']);f.update(type=e['type'],event_id=e['event_id'],channel_id=cid);ws.send(json.dumps(f))
      h=c.execute('select coalesce(n,0) n from counters where channel_id=?',(cid,)).fetchone();ws.send(json.dumps({'type':'resumed','channel_id':cid,'head_event_id':h['n'] if h else 0}))
     c.close()
   except Exception:pass
   finally:
    with socklock:
     sockets[:]=[(a,b) for a,b in sockets if a is not ws]
     event_seen.pop(ws,None)
   return
  p=urlparse(self.path);path=p.path
  if path=='/':
   return self.sendfile('/app/index.html','text/html')
  if path=='/app.js':return self.sendfile('/app/app.js','text/javascript')
  if path=='/app.css':return self.sendfile('/app/app.css','text/css')
  if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':self.server.server_port-8000})
  u=auth(self.headers)
  if path=='/api/auth/me':
   return self.sendj(200,{'user':userobj(u)}) if u else self.sendj(401,{'error':'unauthorized'})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=db()
  try:
   if path=='/api/workspaces':
    rows=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall();return self.sendj(200,{'workspaces':[dict(r) for r in rows]})
   m=re.match(r'/api/workspaces/([^/]+)(?:/(.*))?$',path)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:return self.sendj(404,{})
    if not m.group(2):
     cs=[dict(r) for r in c.execute('select * from channels where workspace_id=? and is_archived=0',(w['id'],)).fetchall()];return self.sendj(200,{'workspace':dict(w),'channels':cs,'read_state':{}})
    if m.group(2)=='members':return self.sendj(200,{'members':[dict(r) for r in c.execute('select user_id,role from members where workspace_id=?',(w['id'],))]})
   if m.group(2)=='invitations':
    rows=c.execute('select code,workspace_id,creator_id,expires,max_uses,uses from invites where workspace_id=? and uses<max_uses',(w['id'],)).fetchall();return self.sendj(200,{'invitations':[dict(r) for r in rows]})
   m=re.match(r'/api/channels/(\d+)(?:/(.*))?$',path)
   if m:
    ch=int(m.group(1));tail=m.group(2);cr=c.execute('select * from channels where id=?',(ch,)).fetchone()
    if not cr:return self.sendj(404,{})
    if tail=='messages':return self.sendj(200,{'messages':[msgobj(c,r) for r in c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(ch,))],'next_cursor':None})
    if tail=='members':return self.sendj(200,{'members':[userobj(getuser(c,r['user_id'])) for r in c.execute('select user_id from cmembers where channel_id=?',(ch,))]})
    if tail=='pins':return self.sendj(200,{'pins':[],'next_cursor':None})
    if tail=='read':return self.sendj(200,{'read_state':{'channel_id':ch,'last_read_event_id':0,'unread_count':0,'mention_count':0}})
   m=re.match(r'/api/messages/(\d+)/replies$',path)
   if m:return self.sendj(200,{'replies':[msgobj(c,r) for r in c.execute('select * from messages where parent_id=? and deleted=0 order by id',(m.group(1),))],'next_cursor':None})
   m=re.match(r'/api/files/(\d+)$',path)
   if m:
    r=c.execute('select id,uploader_id,filename,content_type,size,created_at from files where id=?',(m.group(1),)).fetchone();return self.sendj(200,{'file':dict(r)}) if r else self.sendj(404,{})
   return self.sendj(404,{})
  finally:c.close()
 def do_POST(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/auth/register' or path=='/api/auth/login':
   try:x=self.body(); c=db()
   except:return self.sendj(400,{'error':'bad'})
   if path.endswith('register'):
    if not re.fullmatch(r'[A-Za-z0-9_]+',x.get('username','')) or len(x.get('password',''))<8:return self.sendj(400,{'error':'invalid'})
    try:c.execute('insert into users(username,password,display_name) values(?,?,?)',(x['username'],x['password'],x.get('display_name') or x['username']));c.commit()
    except:return self.sendj(409,{'error':'exists'})
   r=c.execute('select * from users where username=? and password=?',(x.get('username'),x.get('password'))).fetchone()
   if not r:return self.sendj(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24);c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();return self.sendj(201 if path.endswith('register') else 200,{'user':userobj(r),'token':t})
  u=auth(self.headers)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=db(); x=self.body()
  try:
   if path=='/api/workspaces':
    if not re.fullmatch(r'[a-z0-9-]{2,32}',x.get('slug','')):return self.sendj(400,{'error':'invalid'})
    try:c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(x['slug'],x['name'],u['id']));wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));ch=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(ch,u['id']));c.commit()
    except:return self.sendj(409,{'error':'exists'})
    return self.sendj(201,{'workspace':dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':dict(c.execute('select * from channels where id=?',(ch,)).fetchone())})
   m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:return self.sendj(404,{})
    if c.execute('select 1 from channels where workspace_id=? and name=?',(w['id'],x.get('name'))).fetchone():return self.sendj(409,{'error':'duplicate'})
    c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],x['name'],int(x.get('is_private',False)),x.get('topic','')));ch=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into cmembers values(?,?)',(ch,u['id']));c.commit();return self.sendj(201,{'channel':dict(c.execute('select * from channels where id=?',(ch,)).fetchone())})
   m=re.match(r'/api/channels/(\d+)/messages$',path)
   if m:
    ch=int(m.group(1)); body=x.get('body','')
    if not body.strip():return self.sendj(400,{'error':'empty'})
    parent=x.get('parent_id'); c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch,u['id'],body,parent,now()));mid=c.execute('select last_insert_rowid()').fetchone()[0];r=c.execute('select * from messages where id=?',(mid,)).fetchone(); mo=msgobj(c,r); typ='message.reply' if parent else 'message.created'; emit(c,ch,typ,{'message':mo});return self.sendj(201,{'message':mo})
   m=re.match(r'/api/channels/(\d+)/join$',path)
   if m:
    ch=int(m.group(1));c.execute('insert or ignore into cmembers values(?,?)',(ch,u['id']));c.commit();return self.sendj(200,{'channel':dict(c.execute('select * from channels where id=?',(ch,)).fetchone())})
   mi=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
   if mi:
    w=c.execute('select * from workspaces where slug=?',(mi.group(1),)).fetchone()
    if not w:return self.sendj(404,{})
    code=secrets.token_urlsafe(10);c.execute('insert into invites(code,workspace_id,creator_id,expires,max_uses) values(?,?,?,?,?)',(code,w['id'],u['id'],int(time.time())+86400,int(x.get('max_uses',1))));c.commit();return self.sendj(201,{'invitation':dict(c.execute('select code,workspace_id,creator_id,expires,max_uses,uses from invites where code=?',(code,)).fetchone())})
   if path=='/api/dms':return self.sendj(200,{'channel_id':0})
   m=re.match(r'/api/messages/(\d+)/reactions$',path)
   if m:
    mid=int(m.group(1));c.execute('insert or ignore into reactions values(?,?,?)',(mid,u['id'],x.get('emoji','👍')));c.commit(); r=c.execute('select * from messages where id=?',(mid,)).fetchone();emit(c,r['channel_id'],'reaction.added',{'message':msgobj(c,r)});return self.sendj(200,{'reactions':msgobj(c,r)['reactions']})
   m=re.match(r'/api/messages/(\d+)/pin$',path)
   if m:return self.sendj(200,{'pin':{'message_id':int(m.group(1)),'pinned_by':u['id'],'pinned_at':now()}})
   m=re.match(r'/api/invitations/([^/]+)/accept$',path)
   if m:
    inv=c.execute('select * from invites where code=?',(m.group(1),)).fetchone()
    if not inv or inv['uses']>=inv['max_uses']:return self.sendj(404,{})
    c.execute('update invites set uses=uses+1 where code=?',(m.group(1),));c.execute('insert or ignore into members values(?,?,?)',(inv['workspace_id'],u['id'],'member'));c.commit();return self.sendj(200,{'workspace':dict(c.execute('select * from workspaces where id=?',(inv['workspace_id'],)).fetchone())})
   return self.sendj(404,{})
  finally:c.close()
 def do_PATCH(self):
  u=auth(self.headers)
  if not u:return self.sendj(401,{'error':'unauthorized'})
  p=urlparse(self.path).path;x=self.body();c=db()
  try:
   m=re.match(r'/api/messages/(\d+)$',p)
   if m:
    r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
    if not r:return self.sendj(404,{})
    if r['deleted']:return self.sendj(409,{'error':'deleted'})
    if r['author_id']!=u['id']:return self.sendj(403,{})
    if not x.get('body','').strip():return self.sendj(400,{})
    c.execute('update messages set body=?,edited_at=? where id=?',(x['body'],now(),r['id']));c.commit()
    z=c.execute('select * from messages where id=?',(r['id'],)).fetchone();o=msgobj(c,z);emit(c,r['channel_id'],'message.updated',{'message':o});return self.sendj(200,{'message':o})
   m=re.match(r'/api/workspaces/([^/]+)$',p)
   if m:
    w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
    if not w:return self.sendj(404,{})
    c.execute('update workspaces set name=coalesce(?,name),join_mode=coalesce(?,join_mode) where slug=?',(x.get('name'),x.get('join_mode'),m.group(1)));c.commit();return self.sendj(200,{'workspace':dict(c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone())})
   m=re.match(r'/api/channels/(\d+)$',p)
   if m:
    c.execute('update channels set topic=? where id=?',(x.get('topic',''),m.group(1)));c.commit();return self.sendj(200,{'channel':dict(c.execute('select * from channels where id=?',(m.group(1),)).fetchone())})
   m=re.match(r'/api/users/me$',p)
   if m:
    allowed=['display_name','timezone','avatar_url','status_text','status_emoji']; vals={k:v for k,v in x.items() if k in allowed}
    if vals:c.execute('update users set '+','.join(k+'=?' for k in vals)+' where id=?',tuple(vals.values())+(u['id'],));c.commit()
    return self.sendj(200,{'user':userobj(c.execute('select * from users where id=?',(u['id'],)).fetchone())})
   return self.sendj(404,{})
  finally:c.close()
 def do_DELETE(self):
  u=auth(self.headers)
  if not u:return self.sendj(401,{})
  rp=urlparse(self.path).path
  rm=re.match(r'/api/messages/(\d+)/reactions/(.+)$',rp)
  if rm:
   c=db()
   r=c.execute('select * from messages where id=?',(rm.group(1),)).fetchone()
   if not r:c.close();return self.sendj(404,{})
   c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(rm.group(1),u['id'],rm.group(2)));c.commit()
   out=msgobj(c,r)['reactions'];c.close();return self.sendj(200,{'reactions':out})
  m=re.match(r'/api/messages/(\d+)$',rp);c=db()
  if not m:return self.sendj(404,{})
  r=c.execute('select * from messages where id=?',(m.group(1),)).fetchone()
  if not r:return self.sendj(404,{})
  w=c.execute('select w.owner_id from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(r['channel_id'],)).fetchone()
  if r['author_id']!=u['id'] and w['owner_id']!=u['id']:return self.sendj(403,{})
  if not r['deleted']:
   c.execute('update messages set deleted=1 where id=?',(r['id'],));c.execute('delete from reactions where message_id=?',(r['id'],));c.commit();emit(c,r['channel_id'],'message.deleted',{'message_id':r['id']})
  return self.sendj(200,{'deleted':True})
 def do_PUT(self): return self.do_PATCH()
 def do_UPGRADE(self):
  pass
def irc_client(conn,addr):
 q={'sock':conn,'channels':{},'names':{},'skip':set(),'user':None,'nick':None}
 with irclock:ircs.append(q)
 try:
  buf=''
  while True:
   b=conn.recv(4096)
   if not b:break
   buf+=b.decode(errors='ignore')
   while '\n' in buf:
    line,buf=buf.split('\n',1); line=line.rstrip('\r'); parts=line.split(' ',1);cmd=parts[0].upper();arg=parts[1] if len(parts)>1 else ''
    if cmd=='PASS':
     c=db();r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(arg.strip(),)).fetchone();q['user']=r
     if not r:conn.sendall(b':server 464 * :Password incorrect\r\n')
    elif cmd=='NICK':
     nick=arg.split()[0]
     with irclock:busy=any(x['nick']==nick for x in ircs if x is not q)
     if busy:conn.sendall(f':server 433 * {nick} :Nickname in use\r\n'.encode())
     else:q['nick']=nick
    elif cmd=='USER' and q['user'] and q['nick']:
     for n in range(1,6):conn.sendall(f':server {n:03d} {q["nick"]} :Welcome\r\n'.encode())
     conn.sendall(f':server 422 {q["nick"]} :MOTD File is missing\r\n'.encode())
    elif cmd=='USER' and not q['user']:
     conn.sendall(f':server 464 {q["nick"] or "*"} :Password incorrect\r\n'.encode())
    elif cmd=='PING':conn.sendall(f':server PONG server :{arg.lstrip(":")}\r\n'.encode())
    elif cmd=='QUIT':return
    elif cmd=='JOIN' and q['user']:
     name=arg.strip(); c=db();m=re.match(r'#([^/]+)/(.+)',name); row=c.execute('select ch.* from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(m.group(1),m.group(2))) if m else []
     row=row.fetchone() if m else None
     if not row:conn.sendall(f':server 403 {q["nick"]} {name} :No such channel\r\n'.encode());continue
     c.execute('insert or ignore into cmembers values(?,?)',(row['id'],q['user']['id']));c.commit();q['channels'][row['id']]=name;q['names'][row['id']]=name;conn.sendall(f':{q["nick"]}!{q["user"]["username"]}@localhost JOIN {name}\r\n:server 353 {q["nick"]} = {name} :{q["nick"]}\r\n:server 366 {q["nick"]} {name} :End of NAMES list\r\n'.encode())
    elif cmd=='PRIVMSG' and q['user']:
     target,body=arg.split(' :',1) if ' :' in arg else ('','')
     c=db();m=re.match(r'#([^/]+)/(.+)',target);row=c.execute('select ch.* from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(m.group(1),m.group(2))).fetchone() if m else None
     if row:
      c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(row['id'],q['user']['id'],body,now()));mid=c.execute('select last_insert_rowid()').fetchone()[0]; n=emit(c,row['id'],'message.created',{'message':msgobj(c,c.execute('select * from messages where id=?',(mid,)).fetchone())},origin=q);q['skip'].add((row['id'],n))
    elif cmd=='NAMES':
     conn.sendall(f':server 353 {q["nick"] or "*"} = * :\r\n:server 366 {q["nick"] or "*"} * :End of NAMES list\r\n'.encode())
    elif cmd=='WHO':
     conn.sendall(f':server 315 {q["nick"] or "*"} * :End of WHO list\r\n'.encode())
    elif cmd=='LIST':
     conn.sendall(f':server 323 {q["nick"] or "*"} :End of LIST\r\n'.encode())
    elif cmd=='TOPIC':
     conn.sendall(f':server 331 {q["nick"] or "*"} :No topic\r\n'.encode())
    elif cmd=='MODE':
     conn.sendall(f':server 324 {q["nick"] or "*"} * +\r\n'.encode())
    elif cmd=='PONG':pass
    else:conn.sendall(f':server 421 {q["nick"] or "*"} {cmd} :Unknown command\r\n'.encode())
 finally:
  with irclock:
   if q in ircs:ircs.remove(q)
  conn.close()
def event_poller():
 while True:
  time.sleep(.08)
  c=db()
  with socklock: items=list(sockets)
  for ws,cid in items:
   if cid is None: continue
   last=event_seen.get(ws,0)
   for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,last)):
    try:
     f=json.loads(e['payload']); f.update(type=e['type'],event_id=e['event_id'],channel_id=cid); ws.send(json.dumps(f)); event_seen[ws]=e['event_id']
    except Exception: pass
  c.close()
def run_http(port):
 threading.Thread(target=event_poller,daemon=True).start()
 ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
def irc_event_poller():
 seen={}
 while True:
  time.sleep(.08); c=db()
  with irclock: clients=list(ircs)
  for e in c.execute("select * from events where type='message.created' order by channel_id,event_id"):
   key=(e['channel_id'],e['event_id'])
   if key in seen: continue
   seen[key]=1
   try:
    payload=json.loads(e['payload']); m=payload['message']; au=m['author']['username']
    with irclock:
     for q in list(ircs):
      if e['channel_id'] in q['channels'] and key not in q['skip']:
       line=f":{au}!{au}@localhost PRIVMSG {q['names'][e['channel_id']]} :{m['body'].replace(chr(10),' ')}\r\n"
       try:q['sock'].sendall(line.encode())
       except Exception:pass
      q['skip'].discard(key)
   except Exception:pass
  c.close()
def run_irc():
 threading.Thread(target=irc_event_poller,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen()
 while True:
  c,a=s.accept();threading.Thread(target=irc_client,args=(c,a),daemon=True).start()
if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='irc':run_irc()
 else:run_http(int(sys.argv[1]))
