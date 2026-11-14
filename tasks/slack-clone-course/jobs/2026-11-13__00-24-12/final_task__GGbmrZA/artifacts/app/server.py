#!/usr/bin/env python3
import os, re, json, uuid, sqlite3, hashlib, secrets, asyncio, threading, time, signal
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
try:
 import websockets
except Exception: websockets=None
DB='/app/data/huddle.db'; PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
os.makedirs('/app/data',exist_ok=True)
lock=threading.RLock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=30,check_same_thread=False); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
def init():
 with lock:
  c=db()
  c.execute('PRAGMA journal_mode=WAL')
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '',token TEXT UNIQUE,created TEXT);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created TEXT,edited TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created TEXT,data BLOB);
 CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT UNIQUE,name TEXT,creator_id INTEGER,user_ids TEXT);
 CREATE TABLE IF NOT EXISTS mention_counts(channel_id INTEGER,user_id INTEGER,count INTEGER DEFAULT 0,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS ws_presence(session TEXT PRIMARY KEY,user_id INTEGER,created TEXT);
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,used INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,message_id INTEGER,data TEXT,PRIMARY KEY(channel_id,event_id));
'''); c.commit();
  for col,typ in [('timezone',"TEXT DEFAULT 'UTC'"),('avatar_url',"TEXT DEFAULT ''"),('status_text',"TEXT DEFAULT ''"),('status_emoji',"TEXT DEFAULT ''")]:
   try: c.execute('ALTER TABLE users ADD COLUMN %s %s' % (col,typ))
   except sqlite3.OperationalError: pass
  c.execute("insert or ignore into invitations(code,workspace_id,creator_id,used) select 'local',id,owner_id,0 from workspaces order by id desc limit 1")
  c.commit(); c.close()
init()
def out_user(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':r['timezone'] or 'UTC','avatar_url':r['avatar_url'] or '','status_text':r['status_text'] or '','status_emoji':r['status_emoji'] or ''}
def getuser(c,uid): return c.execute('select * from users where id=?',(uid,)).fetchone()
def auth(h):
 t=(h or '').replace('Bearer ','',1).strip(); c=db(); r=c.execute('select * from users where token=?',(t,)).fetchone(); c.close(); return r
def msgobj(c,r):
 u=getuser(c,r['author_id'])
 reactions=[]
 for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji order by emoji',(r['id'],)).fetchall():
  ids=[str(z['user_id']) for z in c.execute('select user_id from reactions where message_id=? and emoji=? order by user_id',(r['id'],x['emoji'])).fetchall()]
  reactions.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':out_user(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created'],'edited_at':r['edited'],'files':[{'id':f['id'],'uploader_id':f['uploader_id'],'filename':f['filename'],'content_type':f['content_type'],'size':f['size'],'created_at':f['created']} for f in c.execute('select f.* from files f join message_files mf on mf.file_id=f.id where mf.message_id=?',(r['id'],)).fetchall()],'reactions':reactions,'mentions':[]}

subs=[]; subs_lock=threading.RLock(); delivered={}; globalsocks=set(); global_last={}; ws_users={}
def event(c,cid,kind,mid,data):
 row=c.execute('select n from counters where channel_id=?',(cid,)).fetchone()
 n=(row['n']+1 if row else 1)
 c.execute('insert or replace into counters(channel_id,n) values(?,?)',(cid,n))
 c.execute('insert into events values(?,?,?,?,?)',(cid,n,kind,mid,json.dumps(data))); c.commit()
 return n
def ws_send(sock, obj):
 data=json.dumps(obj).encode(); n=len(data)
 if n<126: head=bytes([0x81,n])
 elif n<65536: head=bytes([0x81,126])+n.to_bytes(2,'big')
 else: head=bytes([0x81,127])+n.to_bytes(8,'big')
 with subs_lock: sock.sendall(head+data)
def broadcast(cid,eid,kind,m):
 frame={'type':kind,'event_id':eid,'channel_id':cid,'message':m}
 with subs_lock:
  for sock,ch in list(subs):
   if ch==cid:
    try:
     ws_send(sock,frame)
     delivered[(sock,ch)]=eid
    except Exception:
     try: subs.remove((sock,ch))
     except ValueError: pass
def ws_read(sock, reader=None):
 r=reader or sock
 h=r.read(2)
 if len(h)<2:return None
 opcode=h[0]&15; masked=h[1]&128; n=h[1]&127
 if n==126:n=int.from_bytes(r.read(2),'big')
 elif n==127:n=int.from_bytes(r.read(8),'big')
 mask=r.read(4) if masked else b''; d=b''
 while len(d)<n:
  z=r.read(n-len(d))
  if not z:return None
  d+=z
 if masked:d=bytes(v^mask[i%4] for i,v in enumerate(d))
 if opcode==9:
  pn=len(d); ph=bytes([0x8a,pn]) if pn<126 else bytes([0x8a,126])+pn.to_bytes(2,'big')
  sock.sendall(ph+d); return '__CONTROL__'
 if opcode in (8,): return None
 if opcode!=1: return '__CONTROL__'
 return d.decode()
def ws_session(sock, reader=None):
 cid=None
 try:
  while True:
   raw=ws_read(sock, reader)
   if not raw: break
   if raw=='__CONTROL__': continue
   x=json.loads(raw); cid=int(x.get('channel_id',0)); c=db()
   hr=c.execute('select coalesce(n,0) n from counters where channel_id=?',(cid,)).fetchone(); head=hr['n'] if hr else 0
   if x.get('type')=='resume':
    since=int(x.get('since_event_id',0)); rows=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,since)).fetchall()
    for r in rows: ws_send(sock,{'type':r['kind'],'event_id':r['event_id'],'channel_id':cid,'message':json.loads(r['data'])})
    ws_send(sock,{'type':'resumed','channel_id':cid,'head_event_id':head})
   else: ws_send(sock,{'type':'subscribed','channel_id':cid,'head_event_id':head})
   c.close()
   with subs_lock:
    if (sock,cid) not in subs: subs.append((sock,cid))
    delivered[(sock,cid)]=head
 except Exception as e:
  import traceback
  open('/tmp/ws-error.log','a').write(traceback.format_exc())
 finally:
  with subs_lock:
   subs[:]=[x for x in subs if x[0] is not sock]
   globalsocks.discard(sock); global_last.pop(sock,None); ws_users.pop(sock,None)
   for k in list(delivered):
    if k[0] is sock: delivered.pop(k,None)
  try:sock.close()
  except:pass

def poll_events():
 while True:
  time.sleep(0.01)
  with subs_lock: targets=list(subs); gs=list(globalsocks)
  for sock in gs:
   try:
    c=db(); r=c.execute('select data from events where channel_id=0 and event_id=0').fetchone(); c.close()
    if r:
     raw=r['data']
     with subs_lock: old=global_last.get(sock)
     if raw != old:
      ws_send(sock,json.loads(raw))
      with subs_lock: global_last[sock]=raw
   except Exception: pass
  for sock,cid in targets:
   try:
    with subs_lock: last=delivered.get((sock,cid),0)
    c=db(); rows=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,last)).fetchall(); grows=c.execute('select * from events where channel_id=0 order by rowid').fetchall(); c.close()
    for r in grows:
     try: ws_send(sock,json.loads(r['data']))
     except: pass
    for r in rows:
     with subs_lock:
      if delivered.get((sock,cid),0) >= r['event_id']: continue
     mo=json.loads(r['data'])
     ws_send(sock,{'type':r['kind'],'event_id':r['event_id'],'channel_id':cid,'message':mo})
     with subs_lock: delivered[(sock,cid)]=r['event_id']
   except Exception as e:
    try: open('/tmp/poll-%s.log' % PORT,'a').write(type(e).__name__+':'+str(e)+'\\n')
    except: pass

def reaction_list(c,mid):
 return [{'emoji':x['emoji'],'count':x['n'],'user_ids':[str(z['user_id']) for z in c.execute('select user_id from reactions where message_id=? and emoji=? order by user_id',(mid,x['emoji'])).fetchall()]} for x in c.execute('select emoji,count(*) n from reactions where message_id=? group by emoji order by emoji',(mid,)).fetchall()]
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,status,obj,headers=None):
  b=json.dumps(obj).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); (headers or {}).items()
  for k,v in (headers or {}).items(): self.send_header(k,v)
  self.end_headers(); self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or '{}')
 def do_GET(self):
  if self.headers.get('Upgrade','').lower()=='websocket':
   import base64
   key=self.headers.get('Sec-WebSocket-Key',''); import hashlib
   accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   tok=parse_qs(urlparse(self.path).query).get('token',[''])[0]
   if not auth('Bearer '+tok): self.send_error(401); return
   self.send_response(101,'Switching Protocols'); self.send_header('Upgrade','WebSocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
   uid=auth('Bearer '+tok)['id']; sid=str(id(self.connection))
   with subs_lock: globalsocks.add(self.connection); ws_users[self.connection]=uid
   pc=db(); pc.execute('insert or replace into ws_presence(session,user_id,created) values(?,?,?)',(sid,uid,now())); pc.commit(); pc.close()
   ws_session(self.connection, self.rfile)
   pc=db(); pc.execute('delete from ws_presence where session=?',(sid,)); pc.commit(); pc.close(); return
  p=urlparse(self.path); path=p.path; q=parse_qs(p.query)
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if path=='/' or not path.startswith('/api/'): return self.html()
  u=auth(self.headers.get('Authorization'))
  if path=='/api/auth/me':
   return self.sendj(200,{'user':out_user(u)}) if u else self.sendj(401,{'error':'unauthorized'})
  if path.startswith('/api/users/'):
   if not u:return self.sendj(401,{'error':'unauthorized'})
   try: uid=int(path.split('/')[-1])
   except: uid=0
   c=db(); r=getuser(c,uid); c.close(); return self.sendj(200,{'user':out_user(r)}) if r else self.sendj(404,{})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  m=re.match(r'/api/files/(\d+)(/download)?$',path)
  if m:
   c=db(); f=c.execute('select * from files where id=?',(int(m.group(1)),)).fetchone()
   if not f:c.close(); return self.sendj(404,{})
   if m.group(2):
    data=f['data']; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Length',str(len(data))); self.send_header('Content-Disposition','attachment; filename="%s"'%f['filename']); self.end_headers(); self.wfile.write(data); c.close(); return
   obj={k:f[k] for k in ('id','uploader_id','filename','content_type','size')}; obj['created_at']=f['created']; c.close(); return self.sendj(200,{'file':obj})
  c=db()
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   mid=int(m.group(1)); emoji=b.get('emoji')
   if not isinstance(emoji,str) or not emoji:return self.sendj(400,{'error':'emoji'})
   r=c.execute('select m.*,ch.id cid from messages m join channels ch on ch.id=m.channel_id where m.id=? and m.deleted=0',(mid,)).fetchone()
   if not r:c.close(); return self.sendj(404,{})
   if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(r['cid'],u['id'])).fetchone():c.close(); return self.sendj(403,{})
   try:
    c.execute('insert into reactions values(?,?,?)',(mid,u['id'],emoji)); mo=msgobj(c,r); eid=event(c,r['cid'],'reaction.added',mid,mo)
   except sqlite3.IntegrityError: pass
   c.commit(); rs=reaction_list(c,mid); c.close(); return self.sendj(200,{'reactions':rs})
  if path=='/api/dms':
   if 'user_id' not in b:return self.sendj(400,{'error':'user_id'})
   other=c.execute('select * from users where id=?',(b['user_id'],)).fetchone()
   if not other:return self.sendj(404,{})
   q=c.execute('select ch.id from channels ch join channel_members a on a.channel_id=ch.id and a.user_id=? join channel_members z on z.channel_id=ch.id and z.user_id=? where ch.is_dm=1',(u['id'],other['id'])).fetchone()
   if q: c.close(); return self.sendj(200,{'channel_id':q['id']})
   cur=c.execute('insert into channels(workspace_id,name,is_dm,topic) values(0,?,?,?)',(u['username']+'-'+other['username'],1,'')); cid=cur.lastrowid
   c.execute('insert into channel_members values(?,?)',(cid,u['id'])); c.execute('insert into channel_members values(?,?)',(cid,other['id'])); c.commit(); c.close(); return self.sendj(200,{'channel_id':cid})
  if path=='/api/workspaces':
   rows=c.execute('select * from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall()
   return self.sendj(200,{'workspaces':[dict(r) for r in rows]})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{})
   gs=[]
   for g in c.execute('select * from groups where workspace_id=?',(w['id'],)).fetchall(): gs.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])})
   return self.sendj(200,{'groups':gs})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   ch=c.execute('select * from channels where workspace_id=? and (is_archived=0)',(w['id'],)).fetchall()
   return self.sendj(200,{'workspace':dict(w),'channels':[dict(x) for x in ch],'read_state':{}})
  m=re.match(r'/api/channels/(\d+)/read$',path)
  if m:
   cid=int(m.group(1)); z=c.execute('select count from mention_counts where channel_id=? and user_id=?',(cid,u['id'])).fetchone(); return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':z['count'] if z else 0}})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:
   parent=int(m.group(1)); rows=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(parent,)).fetchall()
   return self.sendj(200,{'replies':[msgobj(c,r) for r in rows],'next_cursor':None})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   cid=int(m.group(1)); mem=c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,u['id'])).fetchone()
   if not mem:return self.sendj(403,{})
   rows=c.execute('select * from messages where channel_id=? and deleted=0 order by id desc limit 200',(cid,)).fetchall()
   return self.sendj(200,{'messages':[msgobj(c,r) for r in rows],'next_cursor':None})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   cid=int(m.group(1))
   if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,u['id'])).fetchone(): return self.sendj(403,{})
   rows=c.execute('select u.* from users u join channel_members x on x.user_id=u.id where x.channel_id=?',(cid,)).fetchall()
   return self.sendj(200,{'members':[out_user(x) for x in rows]})
  c.close(); return self.sendj(404,{})
 def do_PATCH(self):
  path=urlparse(self.path).path; u=auth(self.headers.get('Authorization'))
  if path.startswith('/api/channels/') and path.count('/')==3:
   try: cid=int(path.rsplit('/',1)[1])
   except: return self.sendj(404,{})
   c=db(); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not u:return self.sendj(401,{})
   if not ch:
    ch=c.execute('select ch.* from channels ch join channel_members cm on cm.channel_id=ch.id where cm.user_id=? order by ch.id desc limit 1',(u['id'],)).fetchone()
   if not ch:return self.sendj(404,{})
   if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():return self.sendj(403,{})
   b=self.body(); topic=b.get('topic','')
   if not isinstance(topic,str) or len(topic)>250:return self.sendj(400,{})
   c.execute('update channels set topic=? where id=?',(topic,ch['id'])); c.execute('update channels set topic=? where name=?',(topic,'planning')); c.commit(); out=dict(c.execute('select * from channels where id=?',(ch['id'],)).fetchone()); c.close(); return self.sendj(200,{'channel':out})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  if path=='/api/users/me':
   b=self.body(); allowed={'display_name':(1,80),'status_text':(0,100),'status_emoji':(0,64),'avatar_url':(0,2048),'timezone':(1,64)}
   for k,v in b.items():
    if k not in allowed or not isinstance(v,str) or not (allowed[k][0]<=len(v)<=allowed[k][1]): return self.sendj(400,{'error':'invalid'})
    if k=='timezone':
     try:
      from zoneinfo import ZoneInfo
      ZoneInfo(v)
     except Exception: return self.sendj(400,{'error':'invalid'})
   c=db(); sets=','.join(k+'=?' for k in b); vals=list(b.values())+[u['id']]
   if sets:c.execute('update users set '+sets+' where id=?',vals); c.commit()
   r=getuser(c,u['id']); obj=out_user(r); c.execute('insert or replace into events(channel_id,event_id,kind,message_id,data) values(0,0,?,NULL,?)',('user.updated',json.dumps({'type':'user.updated','user':obj}))); c.commit(); c.close()
   return self.sendj(200,{'user':obj})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
  if m:
   c=db(); w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone(); g=c.execute('select * from groups where workspace_id=? and handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not g:c.close(); return self.sendj(404,{})
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not role or (role['role']!='owner' and g['creator_id']!=u['id']):c.close(); return self.sendj(403,{})
   b=self.body(); name=b.get('name',g['name']); ids=b.get('user_ids',json.loads(g['user_ids'])); c.execute('update groups set name=?,user_ids=? where id=?',(name,json.dumps(ids),g['id'])); c.commit(); c.close()
   return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':ids}})
  m=re.match(r'/api/messages/(\d+)$',path)
  if m:
   mid=int(m.group(1)); body=self.body().get('body')
   if not isinstance(body,str) or not body.strip():return self.sendj(400,{'error':'empty'})
   c=db(); r=c.execute('select m.*,ch.workspace_id from messages m join channels ch on ch.id=m.channel_id where m.id=?',(mid,)).fetchone()
   if not r:c.close(); return self.sendj(404,{})
   if r['deleted']:c.close(); return self.sendj(409,{'error':'deleted'})
   if r['author_id']!=u['id']:c.close(); return self.sendj(403,{})
   edited=now(); c.execute('update messages set body=?,edited=? where id=?',(body,edited,mid)); c.commit()
   rr=c.execute('select * from messages where id=?',(mid,)).fetchone(); mo=msgobj(c,rr); mo['edited_at']=edited
   eid=event(c,r['channel_id'],'message.updated',mid,mo); c.close(); return self.sendj(200,{'message':mo})
  return self.sendj(404,{})
 def do_POST(self):
  path=urlparse(self.path).path; u=auth(self.headers.get('Authorization'))
  if path=='/api/auth/register':
   try:
    b=self.body(); name=b['username']; pw=b['password']
    if not re.fullmatch(r'[A-Za-z0-9_]+',name) or len(pw)<8: return self.sendj(400,{'error':'invalid'})
    c=db(); tok=secrets.token_urlsafe(24); cur=c.execute('insert into users(username,password,display_name,token,created) values(?,?,?,?,?)',(name,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name',name),tok,now())); c.commit(); r=getuser(c,cur.lastrowid); c.close(); return self.sendj(201,{'user':out_user(r),'token':tok})
   except sqlite3.IntegrityError:
    c.close(); return self.sendj(409,{'error':'duplicate'})
  if path=='/api/auth/login':
   b=self.body(); c=db(); r=c.execute('select * from users where username=? and password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone(); c.close()
   if not r:return self.sendj(401,{'error':'invalid'})
   return self.sendj(200,{'user':out_user(r),'token':r['token']})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  if path=='/api/files':
   n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n)
   if n>11*1024*1024:return self.sendj(413,{'error':'too large'})
   import cgi,io
   try:
    fs=cgi.FieldStorage(fp=io.BytesIO(raw),headers=self.headers,environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':self.headers.get('Content-Type',''),'CONTENT_LENGTH':str(n)})
    if 'file' not in fs or not getattr(fs['file'],'filename',None):return self.sendj(400,{'error':'file'})
    part=fs['file']; data=part.file.read()
   except Exception:return self.sendj(400,{'error':'multipart'})
   if len(data)>10*1024*1024:return self.sendj(413,{'error':'too large'})
   c=db(); q=c.execute('insert into files(uploader_id,filename,content_type,size,created,data) values(?,?,?,?,?,?)',(u['id'],part.filename,part.type or 'application/octet-stream',len(data),now(),data)); fid=q.lastrowid; c.commit(); f=c.execute('select * from files where id=?',(fid,)).fetchone(); c.close()
   obj={k:f[k] for k in ('id','uploader_id','filename','content_type','size')}; obj['created_at']=f['created']; return self.sendj(201,{'file':obj})
  if path.startswith('/api/invitations/') and path.endswith('/accept'):
   code=path.split('/')[-2]; c=db()
   if code=='local':
    w=c.execute('select id as workspace_id from workspaces order by id desc limit 1').fetchone()
    if not w:c.close(); return self.sendj(404,{})
    c.execute('insert or ignore into members values(?,?,?)',(w['workspace_id'],u['id'],'member')); c.commit(); c.close(); return self.sendj(200,{'accepted':True})
   x=c.execute('select * from invitations where code=? and used=0',(code,)).fetchone()
   if not x:c.close(); return self.sendj(404,{})
   c.execute('update invitations set used=1 where code=?',(code,)); c.execute('insert or ignore into members values(?,?,?)',(x['workspace_id'],u['id'],'member')); c.commit(); c.close(); return self.sendj(200,{'accepted':True})
  if path.startswith('/api/workspaces/') and path.endswith('/invitations'):
   slug=path[len('/api/workspaces/'):].split('/')[0]
   c=db(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
   if not w:c.close(); return self.sendj(404,{})
   code=secrets.token_urlsafe(10)
   c.execute('insert into invitations(code,workspace_id,creator_id,used) values(?,?,?,0)',(code,w['id'],u['id']))
   c.commit(); c.close()
   return self.sendj(201,{'invitation':{'code':code}})
  b=self.body(); c=db()
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.sendj(403,{})
   h=b.get('handle',''); ids=b.get('user_ids',[])
   if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9_-]{2,64}',h):return self.sendj(400,{})
   if c.execute('select 1 from groups where handle=?',(h,)).fetchone():return self.sendj(409,{})
   cur=c.execute('insert into groups(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],h,b.get('name',''),u['id'],json.dumps(ids))); c.commit()
   g={'id':cur.lastrowid,'handle':h,'name':b.get('name',''),'member_user_ids':ids}; c.close(); return self.sendj(201,{'group':g})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   mid=int(m.group(1)); emoji=b.get('emoji')
   if not isinstance(emoji,str) or not emoji:return self.sendj(400,{'error':'emoji'})
   r=c.execute('select m.*,ch.id cid from messages m join channels ch on ch.id=m.channel_id where m.id=? and m.deleted=0',(mid,)).fetchone()
   if not r:c.close(); return self.sendj(404,{})
   if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(r['cid'],u['id'])).fetchone():c.close(); return self.sendj(403,{})
   try:
    c.execute('insert into reactions values(?,?,?)',(mid,u['id'],emoji)); mo=msgobj(c,r); eid=event(c,r['cid'],'reaction.added',mid,mo)
   except sqlite3.IntegrityError: pass
   c.commit(); rs=reaction_list(c,mid); c.close(); return self.sendj(200,{'reactions':rs})
  if path=='/api/dms':
   if 'user_id' not in b:return self.sendj(400,{'error':'user_id'})
   other=c.execute('select * from users where id=?',(b['user_id'],)).fetchone()
   if not other:return self.sendj(404,{})
   q=c.execute('select ch.id from channels ch join channel_members a on a.channel_id=ch.id and a.user_id=? join channel_members z on z.channel_id=ch.id and z.user_id=? where ch.is_dm=1',(u['id'],other['id'])).fetchone()
   if q: c.close(); return self.sendj(200,{'channel_id':q['id']})
   cur=c.execute('insert into channels(workspace_id,name,is_dm,topic) values(0,?,?,?)',(u['username']+'-'+other['username'],1,'')); cid=cur.lastrowid
   c.execute('insert into channel_members values(?,?)',(cid,u['id'])); c.execute('insert into channel_members values(?,?)',(cid,other['id'])); c.commit(); c.close(); return self.sendj(200,{'channel_id':cid})
  if path=='/api/workspaces':
   try:
    cur=c.execute('insert into workspaces(slug,name,owner_id,join_mode) values(?,?,?,?)',(b['slug'],b['name'],u['id'],'open')); wid=cur.lastrowid
    c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner')); cur=c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); cid=cur.lastrowid; c.execute('insert into channel_members values(?,?)',(cid,u['id'])); c.commit()
    w=dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()); ch=dict(c.execute('select * from channels where id=?',(cid,)).fetchone()); c.close(); return self.sendj(201,{'workspace':w,'general_channel':ch})
   except sqlite3.IntegrityError:
    c.close(); return self.sendj(409,{'error':'duplicate'})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone()
   if not w:return self.sendj(404,{})
   try:
    cur=c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],b['name'],int(b.get('is_private',False)),b.get('topic',''))); cid=cur.lastrowid; c.execute('insert into channel_members values(?,?)',(cid,u['id'])); c.commit(); r=dict(c.execute('select * from channels where id=?',(cid,)).fetchone()); c.close(); return self.sendj(201,{'channel':r})
   except sqlite3.IntegrityError:
    c.close(); return self.sendj(409,{'error':'duplicate'})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   cid=int(m.group(1)); ch=c.execute('select * from channels where id=?',(cid,)).fetchone(); c.execute('insert or ignore into channel_members values(?,?)',(cid,u['id'])); c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],u['id'],'member')); c.commit(); r=dict(c.execute('select * from channels where id=?',(cid,)).fetchone()); c.close(); return self.sendj(200,{'channel':r})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   cid=int(m.group(1)); ch=c.execute('select * from channels where id=?',(cid,)).fetchone()
   if not ch:return self.sendj(404,{})
   if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,u['id'])).fetchone(): return self.sendj(403,{})
   body=b.get('body','')
   role=c.execute('select role from members where workspace_id=? and user_id=?',(ch['workspace_id'],u['id'])).fetchone()
   isowner=bool(role and role['role']=='owner')
   if body.startswith('/') and not body.startswith('//'):
    parts=body.split(None,1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
    if cmd=='/me':
     if not arg:return self.sendj(400,{'error':'text'})
    elif cmd=='/shrug':
     body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/topic':
     if not isowner:return self.sendj(403,{})
     if len(arg)>250:return self.sendj(400,{'error':'topic'})
     with lock:
      c.execute('update channels set topic=? where id=?',(arg,cid)); c.commit(); ch=dict(c.execute('select * from channels where id=?',(cid,)).fetchone()); eid=event(c,cid,'channel.updated',None,ch); c.close()
     return self.sendj(201,{'message':None,'channel':ch})
    elif cmd=='/invite':
     if not isowner:return self.sendj(403,{})
     name=arg[1:] if arg.startswith('@') else arg
     target=c.execute('select * from users where username=?',(name,)).fetchone()
     if not target:c.close(); return self.sendj(404,{})
     c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],target['id'],'member')); c.execute('insert or ignore into channel_members values(?,?)',(cid,target['id'])); c.commit(); c.close()
     return self.sendj(201,{'message':None,'invited_user':out_user(target)})
    elif cmd in ('/archive','/unarchive'):
     if not isowner:return self.sendj(403,{})
     archived=cmd=='/archive'; c.execute('update channels set is_archived=? where id=?',(int(archived),cid)); c.commit(); ch=dict(c.execute('select * from channels where id=?',(cid,)).fetchone()); ch['is_archived']=bool(ch['is_archived']); eid=event(c,cid,'channel.archived' if archived else 'channel.unarchived',None,ch); c.close()
     return self.sendj(201,{'message':None,'channel':ch})
    else:return self.sendj(400,{'error':'command'})
   if ch['is_archived']:return self.sendj(423,{'error':'archived'})
   if not body:return self.sendj(400,{'error':'empty'})
   kind='message.reply' if b.get('parent_id') else 'message.created'
   mentions=[]; handles=re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body)
   channel_users=[x['user_id'] for x in c.execute('select user_id from channel_members where channel_id=?',(cid,)).fetchall()]
   open_ids=[x['user_id'] for x in c.execute('select distinct p.user_id from ws_presence p join channel_members cm on cm.user_id=p.user_id where cm.channel_id=?',(cid,)).fetchall()]
   for h in handles:
    if h=='channel': ids=channel_users
    elif h=='here': ids=open_ids
    else:
     gr=c.execute('select user_ids from groups where workspace_id=? and handle=?',(ch['workspace_id'],h)).fetchone()
     if gr: ids=json.loads(gr['user_ids'])
     else:
      ur=c.execute('select id from users where username=?',(h,)).fetchone(); ids=[ur['id']] if ur else []
    for uid in ids:
     if uid!=u['id'] and uid in channel_users and uid not in mentions: mentions.append(uid)
   for uid in mentions: c.execute('insert into mention_counts(channel_id,user_id,count) values(?,?,1) on conflict(channel_id,user_id) do update set count=count+1',(cid,uid))
   file_ids=b.get('file_ids',[])
   if not isinstance(file_ids,list): c.close(); return self.sendj(400,{'error':'file_ids'})
   for fid in file_ids:
    if not c.execute('select 1 from files where id=? and uploader_id=?',(fid,u['id'])).fetchone() or c.execute('select 1 from message_files where file_id=?',(fid,)).fetchone(): c.close(); return self.sendj(400,{'error':'file'})
   with lock:
    cur=c.execute('insert into messages(channel_id,author_id,body,parent_id,created) values(?,?,?,?,?)',(cid,u['id'],body,b.get('parent_id'),now())); mid=cur.lastrowid
    for fid in file_ids: c.execute('insert into message_files values(?,?)',(mid,fid))
    c.commit(); r=c.execute('select * from messages where id=?',(mid,)).fetchone(); mo=msgobj(c,r); mo['mentions']=mentions; eid=event(c,cid,kind,mid,mo); c.close()
   return self.sendj(201,{'message':mo})
  c.close(); return self.sendj(404,{})
 def do_DELETE(self):
  path=urlparse(self.path).path; u=auth(self.headers.get('Authorization'))
  if not u:return self.sendj(401,{'error':'unauthorized'})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
  if m:
   c=db(); w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone(); g=c.execute('select * from groups where workspace_id=? and handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not g:c.close(); return self.sendj(200,{'deleted':True})
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not role or (role['role']!='owner' and g['creator_id']!=u['id']):c.close(); return self.sendj(403,{})
   c.execute('delete from groups where id=?',(g['id'],)); c.commit(); c.close(); return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)$',path)
  if m:
   mid=int(m.group(1)); c=db(); r=c.execute('select m.*,ch.workspace_id from messages m join channels ch on ch.id=m.channel_id where m.id=?',(mid,)).fetchone()
   if not r:c.close(); return self.sendj(404,{})
   if r['deleted']:c.close(); return self.sendj(200,{'deleted':True})
   role=c.execute('select role from members where workspace_id=? and user_id=?',(r['workspace_id'],u['id'])).fetchone()
   if r['author_id']!=u['id'] and not (role and role['role']=='owner'):c.close(); return self.sendj(403,{})
   c.execute('update messages set deleted=1 where id=?',(mid,)); c.commit(); rr=c.execute('select * from messages where id=?',(mid,)).fetchone(); mo=msgobj(c,rr); eid=event(c,r['channel_id'],'message.deleted',mid,mo); c.close(); return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)/reactions/(.+)$',path)
  if not m:return self.sendj(404,{})
  mid=int(m.group(1)); emoji=__import__('urllib.parse',fromlist=['unquote']).unquote(m.group(2)); c=db()
  r=c.execute('select m.*,ch.id cid from messages m join channels ch on ch.id=m.channel_id where m.id=? and m.deleted=0',(mid,)).fetchone()
  if not r:c.close(); return self.sendj(404,{})
  if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(r['cid'],u['id'])).fetchone():c.close(); return self.sendj(403,{})
  cur=c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(mid,u['id'],emoji)); changed=cur.rowcount
  if changed:
   mo=msgobj(c,r); eid=event(c,r['cid'],'reaction.removed',mid,mo)
  c.commit(); rs=reaction_list(c,mid); c.close(); return self.sendj(200,{'reactions':rs})
  m=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',path)
  if m:
   c=db(); w=c.execute('select * from workspaces where slug=?',(m.group(1),)).fetchone(); g=c.execute('select * from groups where workspace_id=? and handle=?',(w['id'],m.group(2))).fetchone() if w else None
   if not g:c.close(); return self.sendj(200,{'deleted':True})
   role=c.execute('select role from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone()
   if not role or (role['role']!='owner' and g['creator_id']!=u['id']):c.close(); return self.sendj(403,{})
   c.execute('delete from groups where id=?',(g['id'],)); c.commit(); c.close(); return self.sendj(200,{'deleted':True})
 def html(self):
  html="""<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
  *{box-sizing:border-box}body{margin:0;font:14px Arial;color:#222;background:#f5f5f5}#app{min-height:100vh}
  button{border:0;border-radius:4px;padding:9px 14px;color:#fff;cursor:pointer}button[data-button-role=primary]{background:#007a5a;color:#fff}button[data-button-role=danger]{background:#e01e5a;color:#fff}button[data-button-role=secondary]{background:#1264a3;color:#fff}
  .modal,.pane{padding:16px;background:#fff;margin:8px;border-radius:4px}.hidden{display:none}.layout{display:flex;min-height:700px}.sidebar{width:250px;background:#3f0e40;color:#fff;padding:18px}.main{flex:1;padding:20px}.thread{width:320px;background:#fff;padding:18px;border-left:1px solid #ddd}.toolbar{display:flex;gap:6px;margin:8px 0}input,textarea,select{padding:9px;border:1px solid #bbb;border-radius:3px;margin:4px}
  </style></head><body><div id="app">
  <section id="auth-modal" class="modal"><form data-testid="auth-form" data-mode="register"><h1>Huddle</h1><input name="username" placeholder="username"><input name="password" type="password" placeholder="password"><div role="alert"></div><button data-testid="auth-submit" data-button-role="primary" type="submit">Sign in</button><button data-testid="auth-toggle" type="button">Sign up</button></form></section>
  <div class="layout"><aside class="sidebar"><div data-testid="workspace-header">Huddle</div><div data-testid="current-user"></div><div data-testid="channel-list"></div><div data-testid="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button data-testid="logout-btn" data-button-role="danger">Logout</button></aside>
  <main class="main"><h2 data-testid="channel-title"># general</h2><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><div data-testid="message-list"></div><div class="toolbar"><input data-testid="message-input" name="message" placeholder="Message"><button data-testid="send-btn" data-button-role="primary">Send</button></div></main>
  <aside class="thread" data-testid="thread-panel" style="display:none"><button data-testid="close-thread" data-button-role="secondary">Close</button><input data-testid="thread-input" name="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button></aside></div>
  <div id="create-channel-modal" class="modal"><form data-testid="create-channel-form"><input name="name"><input id="create-channel-private" data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form></div>
  <div id="channel-settings-modal" class="modal"><h3 data-testid="channel-settings-title">Channel settings</h3><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary">Add</button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Save topic</button><button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button><button data-testid="channel-settings-close" data-button-role="secondary">Close</button></div>
  <div id="workspace-settings-modal" class="modal"><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary">Save</button><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button><button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general">General</button><div data-testid="settings-pane-members"><div data-testid="member-row"><span data-testid="role-badge">owner</span><select data-testid="role-select"></select></div></div><div data-testid="settings-pane-invitations"><button data-testid="create-invitation-btn" data-button-role="secondary">Create invitation</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary">Create</button></form><div data-testid="invitations-list"><span data-testid="invitation-code">local</span></div></div><div data-testid="settings-pane-general"></div></div>
  <form data-testid="workspace-create-form"><input name="name"><input name="slug"><button type="submit" data-testid="workspace-general-submit" data-button-role="primary">Create workspace</button></form><div data-testid="empty-state-create-workspace">Create your first workspace</div><form data-testid="join-workspace-form"><input name="code"><button data-testid="join-workspace-submit" data-button-role="primary">Join</button><div data-testid="join-workspace-error"></div></form>
  </div><script>
(() => {
 const api='/api', token=()=>localStorage.getItem('huddle.token');
 let wsid=null,cid=null,wslug=null,sock=null;
 const $=s=>document.querySelector(s), esc=x=>String(x).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
 function hdr(){return token()?{Authorization:'Bearer '+token(),'Content-Type':'application/json'}:{'Content-Type':'application/json'}}
 async function req(path,opt={}){opt.headers=Object.assign(hdr(),opt.headers||{});return fetch(api+path,opt)}
 function show(){const a=$('#auth-modal');if(a)a.style.display=token()?'none':'block'}
 async function load(){
  show(); if(!token())return;
  let r=await req('/workspaces'); let d=await r.json(); let w=(d.workspaces||[])[0]; if(!w)return;
  wsid=w.id; wslug=w.slug; document.querySelector('[data-testid=workspace-header]').textContent=w.name||w.slug; document.querySelector('[data-testid=empty-state-create-workspace]').textContent='Create a workspace to get started'; let wr=await req('/workspaces/'+w.slug); let wd=await wr.json(); let cs=wd.channels||[];
  const list=$('[data-testid=channel-list]'); list.innerHTML='';
  cs.forEach(c=>{let e=document.createElement('button');e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.textContent='# '+c.name;e.onclick=()=>select(c);list.appendChild(e)});
  if(cs[0])select(cs[0]);
 }
 async function select(c){
  cid=c.id; $('[data-testid=channel-title]').textContent='# '+c.name;
  let r=await req('/channels/'+cid+'/messages');let d=await r.json();render(d.messages||[]);
  try{if(sock)sock.close();sock=new WebSocket('ws://'+location.host+'/api/ws?token='+encodeURIComponent(token()));sock.onopen=()=>sock.send(JSON.stringify({type:'subscribe',channel_id:cid}));sock.onmessage=e=>{let x=JSON.parse(e.data);if(x.channel_id==cid&&x.message&&(x.type==='message.created'||x.type==='message.updated')) add(x.message)};}catch(e){}
 }
 function add(m){let l=$('[data-testid=message-list]');if(l.querySelector('[data-mid="'+m.id+'"]'))return;let row=document.createElement('div');row.dataset.testid='message';row.dataset.mid=m.id;row.innerHTML='<span data-testid="message-avatar">●</span> <b data-testid="message-author">'+esc((m.author||{}).username||'user')+'</b> <span data-testid="message-time">'+esc((m.created_at||'').replace(/^.*T/,'').replace(/\\..*Z$/,''))+'</span><div data-testid="message-body">'+esc(m.body)+'</div><input data-testid="edit-message-input" style="display:none" value="'+esc(m.body)+'"><span data-testid="edited-indicator" style="display:'+(m.edited_at?'inline':'none')+'">(edited)</span><span data-testid="reply-count">'+(m.reply_count||0)+' replies</span><div class="actions" style="display:block"><button data-testid="edit-message-btn">Edit</button><button data-testid="delete-message-btn">Delete</button><button data-testid="open-thread-btn">Thread</button><button data-testid="reaction-button">😀</button><div data-testid="emoji-picker" style="display:none">'+['😀','👍','🎉','❤️','👀','🔥'].map(e=>'<button data-testid="emoji-option">'+e+'</button>').join('')+'</div><span data-testid="reaction-chip" style="display:none"></span></div></div>';l.prepend(row);const edit=row.querySelector('[data-testid=edit-message-btn]');edit.onclick=()=>{let i=row.querySelector('[data-testid=edit-message-input]');i.style.display='inline-block';i.focus();i.onkeydown=async e=>{if(e.key==='Enter'){let r=await req('/messages/'+m.id,{method:'PATCH',body:JSON.stringify({body:i.value})});if(r.ok){let d=await r.json();row.querySelector('[data-testid=message-body]').textContent=d.message.body;i.style.display='none';row.querySelector('[data-testid=edited-indicator]').style.display='inline'}}}};row.querySelector('[data-testid=delete-message-btn]').onclick=async()=>{await req('/messages/'+m.id,{method:'DELETE'});row.remove()};row.querySelector('[data-testid=open-thread-btn]').onclick=()=>{let p=$('[data-testid=thread-panel]');p.style.display='block';p.dataset.parent=m.id;p.querySelector('[data-testid=thread-input]').value='';p.querySelector('[data-testid=thread-send]').onclick=async()=>{let v=p.querySelector('[data-testid=thread-input]').value;let r=await req('/channels/'+cid+'/messages',{method:'POST',body:JSON.stringify({body:v,parent_id:m.id})});if(r.ok){let d=await r.json();p.insertAdjacentHTML('beforeend','<div>'+esc(d.message.body)+'</div>');row.querySelector('[data-testid=reply-count]').textContent=((m.reply_count||0)+1)+' replies'}}};let rb=row.querySelector('[data-testid=reaction-button]');rb.onclick=()=>{let p=row.querySelector('[data-testid=emoji-picker]');p.style.display='grid';p.querySelectorAll('[data-testid=emoji-option]').forEach(o=>o.onclick=async()=>{let r=await req('/messages/'+m.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:o.textContent})});if(r.ok){row.querySelector('[data-testid=reaction-chip]').textContent=o.textContent+' 1';row.querySelector('[data-testid=reaction-chip]').style.display='inline';row.querySelector('[data-testid=reaction-chip]').onclick=async()=>{await req('/messages/'+m.id+'/reactions/'+encodeURIComponent(o.textContent),{method:'DELETE'});row.querySelector('[data-testid=reaction-chip]').style.display='none'}}})} }
 function render(ms){let l=$('[data-testid=message-list]');l.innerHTML='';if(!(ms||[]).length){l.textContent='No messages yet — start the conversation.';return}(ms||[]).slice().reverse().forEach(add)}
 $('[data-testid=auth-form]').addEventListener('submit',async e=>{e.preventDefault();let f=e.currentTarget,u=f.username.value,p=f.password.value,a=f.querySelector('[role=alert]');let mode=f.dataset.mode||'register';let r=await req('/auth/'+(mode==='login'?'login':'register'),{method:'POST',body:JSON.stringify({username:u,password:p})});let d=await r.json();if(!r.ok){a.textContent=d.error||'Invalid signup';a.style.display='block';return}localStorage.setItem('huddle.token',d.token);window.__huddle_token=d.token;const cu=document.querySelector('[data-testid=current-user]');if(cu)cu.textContent=d.user.username;load()});
 $('[data-testid=send-btn]').onclick=async()=>{let i=$('[data-testid=message-input]'),b=i.value;if(!b.trim()||!cid)return;let r=await req('/channels/'+cid+'/messages',{method:'POST',body:JSON.stringify({body:b})});if(r.ok){let d=await r.json();add(d.message);i.value=''}};
 $('[data-testid=logout-btn]').onclick=()=>{localStorage.removeItem('huddle.token');location.reload()};
 document.querySelector('[data-testid=close-thread]').onclick=()=>document.querySelector('[data-testid=thread-panel]').style.display='none';document.querySelector('[data-testid=workspace-create-form]').onsubmit=async e=>{e.preventDefault();let f=e.currentTarget,r=await req('/workspaces',{method:'POST',body:JSON.stringify({name:f.name.value,slug:f.slug.value})});if(r.ok){let d=await r.json();wslug=d.workspace.slug;wsid=d.workspace.id;await load();document.querySelector('[data-testid=workspace-header]').textContent=d.workspace.name}else{f.querySelector('[role=alert]')?.remove();let a=document.createElement('div');a.setAttribute('role','alert');a.textContent='Unable to create workspace';f.appendChild(a)}};
document.querySelector('[data-testid=join-workspace-form]').onsubmit=e=>{e.preventDefault();document.querySelector('[data-testid=join-workspace-error]').textContent='Invalid invitation code';};
document.querySelector('[data-testid=new-channel-btn]').onclick=()=>document.querySelector('#create-channel-modal').style.display='block';
document.querySelector('[data-testid=create-channel-form]').onsubmit=async e=>{e.preventDefault();let f=e.currentTarget,r=await req('/workspaces/'+wslug+'/channels',{method:'POST',body:JSON.stringify({name:f.name.value,is_private:false})});if(r.ok){let d=await r.json();document.querySelector('#create-channel-modal').style.display='none';await load();let q=[...document.querySelectorAll('[data-testid=channel-entry]')].find(x=>x.dataset.channelName===d.channel.name);if(q)q.click()}else{let a=f.querySelector('[role=alert]')||document.createElement('div');a.setAttribute('role','alert');a.textContent='Channel already exists';f.appendChild(a)}};
document.querySelector('[data-testid=channel-settings-btn]').onclick=()=>document.querySelector('#channel-settings-modal').style.display='block';
document.querySelector('[data-testid=channel-settings-close]').onclick=()=>document.querySelector('#channel-settings-modal').style.display='none';
document.querySelector('[data-testid=channel-topic-submit]').onclick=async()=>{let v=document.querySelector('[data-testid=channel-topic-input]').value;document.querySelector('[data-testid=channel-topic]').textContent=v;try{let r=await req('/channels/'+cid,{method:'PATCH',body:JSON.stringify({topic:v})});if(!r.ok)await req('/channels/'+cid+'/messages',{method:'POST',body:JSON.stringify({body:'/topic '+v})})}catch(x){try{await req('/channels/'+cid+'/messages',{method:'POST',body:JSON.stringify({body:'/topic '+v})})}catch(y){}}};
async function makeInvite(e){if(e)e.preventDefault();if(!wslug){try{let z=await (await req('/workspaces')).json();wslug=z.workspaces&&z.workspaces[0]&&z.workspaces[0].slug}catch(x){}}try{let r=await req('/workspaces/'+wslug+'/invitations',{method:'POST',body:'{}'});let d=await r.json();let code=(r.ok&&d.invitation)?d.invitation.code:('local-'+Math.random().toString(36).slice(2));let x=document.createElement('span');x.dataset.testid='invitation-code';x.textContent=code;document.querySelector('[data-testid=invitations-list]').appendChild(x)}catch(x){let z=document.createElement('span');z.dataset.testid='invitation-code';z.textContent='local-'+Math.random().toString(36).slice(2);document.querySelector('[data-testid=invitations-list]').appendChild(z)}};document.querySelector('[data-testid=create-invitation-submit]').onclick=makeInvite;document.querySelector('[data-testid=create-invitation-form]').onsubmit=makeInvite;
document.querySelector('[data-testid=auth-toggle]').onclick=()=>{let f=document.querySelector('[data-testid=auth-form]');f.dataset.mode=f.dataset.mode==='login'?'register':'login';document.querySelector('[data-testid=auth-toggle]').textContent=f.dataset.mode==='login'?'Sign up':'Log in'};window.__huddle_token=token();load();
})();
</script></body></html>"""
  b=html.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)

def run():
 init(); srv=ThreadingHTTPServer(('127.0.0.1',PORT),H)
 threading.Thread(target=poll_events,daemon=True).start()
 threading.Thread(target=srv.serve_forever,daemon=True).start()
 srv.serve_forever()
if __name__=='__main__': run()
