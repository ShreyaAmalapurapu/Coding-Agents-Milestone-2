import os,sys,json,re,sqlite3,uuid,hashlib,base64, socket, threading, time, datetime, urllib.parse, html
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/data/chat.db'; os.makedirs('/app/data',exist_ok=True)
lock=threading.RLock(); clients=[]; irc_clients=[]; irc_lock=threading.RLock(); ws_seen={}
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,uses INTEGER DEFAULT 0,max_uses INTEGER DEFAULT 1);
 '''); c.commit(); c.close()
init()
def userobj(r): return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def msgobj(c,r):
 u=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 rs=[]
 for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(r['id'],)):
  ids=[z[0] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(r['id'],x['emoji']))]
  rs.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':rs,'mentions':[]}
def auth(h):
 x=h.get('Authorization','')
 if not x.startswith('Bearer '): return None
 c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(x[7:],)).fetchone(); c.close(); return r
def event(c,ch,typ,payload):
 c.execute('insert or ignore into seq(channel_id,n) values(?,0)',(ch,))
 n=c.execute('update seq set n=n+1 where channel_id=? returning n',(ch,)).fetchone()[0]
 c.execute('insert into events values(?,?,?,?)',(ch,n,typ,json.dumps(payload))); return n
def emit(ch,eid,typ,payload):
 data={'type':typ,'event_id':eid,'channel_id':ch}; data.update(payload)
 raw=json.dumps(data)
 with lock:
  for s,subs in list(clients):
   if ch in subs:
    try: ws_send(s,raw); ws_seen.setdefault(s,set()).add((ch,data['event_id']))
    except: pass
 with irc_lock:
  for cl in list(irc_clients):
   if ch in cl.get('channels',set()) and typ=='message.created':
    m=payload.get('message',{}); line=f":{m.get('author',{}).get('username','user')}!{m.get('author',{}).get('username','user')}@localhost PRIVMSG {cl['names'].get(ch,'')} :{m.get('body','')}\r\n"
    try: cl['sock'].sendall(line.encode())
    except: pass
def ws_send(s,x):
 b=x.encode(); s.sendall(b'\x81'+(bytes([len(b)]) if len(b)<126 else b'\x7e'+len(b).to_bytes(2,'big'))+b)
def make_message(c,ch,uid,body,parent=None):
 t=now(); c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at) values(?,?,?,?,?)',(ch,uid,body,parent,t)); mid=c.execute('select last_insert_rowid()').fetchone()[0]; r=c.execute('select * from messages where id=?',(mid,)).fetchone(); m=msgobj(c,r); eid=event(c,ch,'message.reply' if parent else 'message.created',{'message':m}); c.commit(); emit(ch,eid,'message.reply' if parent else 'message.created',{'message':m}); return m
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def send(self,code,obj=None,typ='application/json'):
  b=((obj.encode() if isinstance(obj,str) else bytes(obj)) if obj is not None and typ!='application/json' else (json.dumps(obj).encode() if obj is not None else b'')); self.send_response(code); self.send_header('Content-Type',typ); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def read(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urllib.parse.urlparse(self.path).path
  if p=='/': return self.send(200,open('/app/index.html','rb').read().decode(), 'text/html')
  if p=='/app.js': return self.send(200,open('/app/app.js','rb').read().decode(),'application/javascript')
  if p=='/style.css': return self.send(200,open('/app/style.css','rb').read().decode(),'text/css')
  if p=='/api/health': return self.send(200,{'status':'ok','node_id':int(self.server.server_port)-8000})
  if p=='/api/ws': return self.websocket()
  u=auth(self.headers)
  if p=='/api/auth/me': return self.send(200,{'user':userobj(u)}) if u else self.send(401,{'error':'unauthorized'})
  if not u: return self.send(401,{'error':'unauthorized'})
  c=db(); parts=p.split('/')
  try:
   if p=='/api/workspaces':
    rows=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall(); return self.send(200,{'workspaces':[dict(x) for x in rows]})
   if len(parts)==4 and parts[3] and parts[2]=='workspaces':
    slug=parts[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
    if not w:return self.send(404,{'error':'not found'})
    chans=[dict(x) for x in c.execute('select * from channels where workspace_id=? and is_archived=0',(w['id'],))]
    return self.send(200,{'workspace':dict(w),'channels':chans,'read_state':{}})
   if len(parts)==5 and parts[4]=='members':
    w=c.execute('select * from workspaces where slug=?',(parts[3],)).fetchone(); rows=c.execute('select user_id,role from members where workspace_id=?',(w['id'],)).fetchall(); return self.send(200,{'members':[dict(x) for x in rows]})
   if len(parts)>=4 and parts[2]=='channels' and parts[3].isdigit():
    ch=int(parts[3])
    if p.endswith('/members'):
     rows=c.execute('select u.* from users u join channel_members m on m.user_id=u.id where m.channel_id=?',(ch,)).fetchall(); return self.send(200,{'members':[userobj(x) for x in rows]})
    rows=c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(ch,)).fetchall(); return self.send(200,{'messages':[msgobj(c,x) for x in rows],'next_cursor':None})
   if len(parts)>=4 and parts[2]=='messages' and parts[3].isdigit() and p.endswith('/replies'):
    rows=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(int(parts[3]),)).fetchall(); return self.send(200,{'replies':[msgobj(c,x) for x in rows],'next_cursor':None})
   if len(parts)==4 and parts[2]=='users':
    r=c.execute('select * from users where id=?',(parts[3],)).fetchone(); return self.send(200,{'user':userobj(r)}) if r else self.send(404,{'error':'not found'})
  finally:c.close()
  self.send(404,{'error':'not found'})
 def do_POST(self):
  p=urllib.parse.urlparse(self.path).path; body=self.read()
  if p=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_-]+',body.get('username','')) or len(body.get('password',''))<8:return self.send(400,{'error':'invalid'})
   c=db()
   try:c.execute('insert into users(username,password,display_name) values(?,?,?)',(body['username'],body['password'],body.get('display_name') or body['username'])); c.commit()
   except:return self.send(409,{'error':'duplicate'})
   r=c.execute('select * from users where username=?',(body['username'],)).fetchone(); t=uuid.uuid4().hex+uuid.uuid4().hex;c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();c.close();return self.send(201,{'user':userobj(r),'token':t})
  if p=='/api/auth/login':
   c=db();r=c.execute('select * from users where username=? and password=?',(body.get('username'),body.get('password'))).fetchone()
   if not r:return self.send(401,{'error':'invalid'})
   t=uuid.uuid4().hex+uuid.uuid4().hex;c.execute('insert into tokens values(?,?)',(t,r['id']));c.commit();c.close();return self.send(200,{'user':userobj(r),'token':t})
  u=auth(self.headers)
  if not u:return self.send(401,{'error':'unauthorized'})
  c=db()
  if p=='/api/workspaces':
   try:
    c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(body['slug'],body['name'],u['id'])); wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general'));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into channel_members values(?,?)',(cid,u['id']));c.commit();return self.send(201,{'workspace':dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':dict(c.execute('select * from channels where id=?',(cid,)).fetchone())})
   except:return self.send(409,{'error':'duplicate'})
  if re.match(r'/api/workspaces/[^/]+/channels$',p):
   slug=p.split('/')[3];w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   try:c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],body['name'],int(body.get('is_private',False)),body.get('topic','')));cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into channel_members values(?,?)',(cid,u['id']));c.commit();return self.send(201,{'channel':dict(c.execute('select * from channels where id=?',(cid,)).fetchone())})
   except:return self.send(409,{'error':'duplicate'})
  if re.match(r'/api/channels/\d+/join$',p):
   ch=int(p.split('/')[3]); c.execute('insert or ignore into channel_members values(?,?)',(ch,u['id'])); c.commit();return self.send(200,{'joined':True})
  if re.match(r'/api/workspaces/[^/]+/invitations$',p):
   slug=p.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:return self.send(404,{'error':'not found'})
   code=uuid.uuid4().hex[:12]; c.execute('insert into invitations(code,workspace_id) values(?,?)',(code,w['id'],)); c.commit(); return self.send(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':1}})
  if re.match(r'/api/invitations/[^/]+/accept$',p):
   code=p.split('/')[3]; inv=c.execute('select * from invitations where code=?',(code,)).fetchone()
   if not inv:return self.send(404,{'error':'not found'})
   c.execute('insert or ignore into members values(?,?,?)',(inv['workspace_id'],u['id'],'member'));c.commit();return self.send(200,{'accepted':True})
  if re.match(r'/api/channels/\d+/messages$',p):
   ch=int(p.split('/')[3]); text=body.get('body','')
   if not text.strip():return self.send(400,{'error':'empty'})
   return self.send(201,{'message':make_message(c,ch,u['id'],text,body.get('parent_id'))})
  if re.match(r'/api/messages/\d+/reactions$',p):
   mid=int(p.split('/')[3]);c.execute('insert or ignore into reactions values(?,?,?)',(mid,u['id'],body.get('emoji','😀')));c.commit(); rows=c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(mid,)).fetchall();return self.send(200,{'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[z[0] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(mid,x['emoji']))]} for x in rows]})
  c.close();return self.send(404,{'error':'not found'})
 def do_PATCH(self):
  p=self.path.split('?')[0];u=auth(self.headers)
  if not u:return self.send(401,{'error':'unauthorized'})
  c=db()
  if re.match(r'/api/channels/\d+$',p):
   cid=int(p.split('/')[3]);r=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not r:return self.send(404,{'error':'not found'})
   b=self.read();c.execute('update channels set topic=? where id=?',(b.get('topic',''),cid));c.commit();return self.send(200,{'channel':dict(c.execute('select * from channels where id=?',(cid,)).fetchone())})
  if re.match(r'/api/messages/\d+$',p):
   mid=int(p.split('/')[3]);r=c.execute('select * from messages where id=?',(mid,)).fetchone()
   if not r:return self.send(404,{'error':'not found'})
   if r['deleted']:return self.send(409,{'error':'deleted'})
   if r['author_id']!=u['id']:return self.send(403,{'error':'forbidden'})
   b=self.read().get('body','')
   if not b.strip():return self.send(400,{'error':'empty'})
   c.execute('update messages set body=?,edited_at=? where id=?',(b,now(),mid));r=c.execute('select * from messages where id=?',(mid,)).fetchone();m=msgobj(c,r);eid=event(c,r['channel_id'],'message.updated',{'message':m});c.commit();emit(r['channel_id'],eid,'message.updated',{'message':m});return self.send(200,{'message':m})
  return self.send(404,{'error':'not found'})
 def do_DELETE(self):
  p=self.path.split('?')[0];u=auth(self.headers)
  if not u:return self.send(401,{'error':'unauthorized'})
  if re.match(r'/api/messages/\d+/reactions/',p):
   mid=int(p.split('/')[3]); emoji=urllib.parse.unquote(p.split('/')[5]); c=db(); c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji)); c.commit(); rows=c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji',(mid,)).fetchall(); return self.send(200,{'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[z[0] for z in c.execute('select user_id from reactions where message_id=? and emoji=?',(mid,x['emoji']))]} for x in rows]})
  c=db();mid=int(p.split('/')[3]);r=c.execute('select * from messages where id=?',(mid,)).fetchone()
  if not r:return self.send(404,{'error':'not found'})
  if r['deleted']:return self.send(200,{'deleted':True})
  owner=c.execute('select w.owner_id from channels x join workspaces w on w.id=x.workspace_id where x.id=?',(r['channel_id'],)).fetchone()
  if r['author_id']!=u['id'] and owner['owner_id']!=u['id']:return self.send(403,{'error':'forbidden'})
  c.execute('update messages set deleted=1 where id=?',(mid,));c.execute('delete from reactions where message_id=?',(mid,));eid=event(c,r['channel_id'],'message.deleted',{'message_id':mid});c.commit();emit(r['channel_id'],eid,'message.deleted',{'message_id':mid});return self.send(200,{'deleted':True})
 def websocket(self):
  u=auth({'Authorization':'Bearer '+urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]})
  if not u:return self.send(401,{'error':'unauthorized'})
  key=self.headers.get('Sec-WebSocket-Key'); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers();s=self.connection;subs=set()
  with lock: clients.append((s,subs)); ws_seen[s]=set()
  try:
   while True:
    h=s.recv(2)
    if not h:break
    n=h[1]&127
    if n==126:n=int.from_bytes(s.recv(2),'big')
    mask=s.recv(4);d=s.recv(n);d=bytes(x^mask[i%4] for i,x in enumerate(d));o=json.loads(d)
    if o.get('type') in ('subscribe','resume'):
     ch=int(o['channel_id']);subs.add(ch);c=db();head=c.execute('select n from seq where channel_id=?',(ch,)).fetchone();head=head[0] if head else 0
     if o['type']=='resume':
      for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(ch,int(o.get('since_event_id',0)))):
       z=json.loads(e['payload']);z.update(type=e['type'],event_id=e['event_id'],channel_id=ch);ws_send(s,json.dumps(z))
      ws_send(s,json.dumps({'type':'resumed','channel_id':ch,'head_event_id':head}))
     else:ws_send(s,json.dumps({'type':'subscribed','channel_id':ch,'head_event_id':head}))
  except:pass
  with lock:
   try: clients.remove((s,subs)); ws_seen.pop(s,None)
   except: pass
class S(ThreadingHTTPServer): allow_reuse_address=True


def event_poller():
 while True:
  time.sleep(.08)
  with lock: active=list(clients)
  if not active: continue
  c=db()
  for s,subs in active:
   for ch in list(subs):
    last=max([eid for cc,eid in ws_seen.get(s,set()) if cc==ch] or [0])
    for e in c.execute('select * from events where channel_id=? and event_id>? order by event_id',(ch,last)):
     try:
      z=json.loads(e['payload']); z.update(type=e['type'],event_id=e['event_id'],channel_id=ch); ws_send(s,json.dumps(z)); ws_seen.setdefault(s,set()).add((ch,e['event_id']))
     except: pass
  c.close()
threading.Thread(target=event_poller,daemon=True).start()
