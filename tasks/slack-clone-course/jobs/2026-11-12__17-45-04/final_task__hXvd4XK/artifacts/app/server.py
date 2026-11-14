import os,sys,json,sqlite3,secrets,hashlib,re,threading,time,base64,struct,io
from email.parser import BytesParser
from email.policy import default
from datetime import datetime,timezone
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote
from zoneinfo import ZoneInfo

DATA='/app/data'; os.makedirs(DATA,exist_ok=True); DB=DATA+'/huddle.sqlite'
NODE=int(os.environ.get('NODE_ID','0')); PORT=int(os.environ.get('PORT','8000'))
clients=[]; clients_lock=threading.Lock(); seen_events=set(); seen_global=0
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=5,isolation_level=None); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=5000'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT,created_at TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,dm_key TEXT UNIQUE);
 CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
 CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS group_users(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
 CREATE TABLE IF NOT EXISTS online_sockets(user_id INTEGER,token TEXT PRIMARY KEY,updated_at TEXT);
 CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,event_id INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id)); CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);'''); c.commit(); c.close()
def user_obj(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def token_user(c,t):
 r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t or '',)).fetchone(); return r
def auth(h):
 a=h.headers.get('Authorization','')
 if not a.startswith('Bearer '): return None
 c=db(); r=token_user(c,a[7:]); c.close(); return r
def body(h):
 n=int(h.headers.get('Content-Length','0')); return json.loads(h.rfile.read(n) or b'{}')
def file_obj(r):
 return {k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}
def files_of(c,message_id):
 return [file_obj(r) for r in c.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(message_id,))]
def reactions_of(c,message_id):
 out=[]
 for e in c.execute('SELECT emoji,COUNT(*) AS n FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(rowid)',(message_id,)):
  out.append({'emoji':e['emoji'],'count':e['n'],'user_ids':[x[0] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=? ORDER BY rowid',(message_id,e['emoji']))]})
 return out
def msg_obj(c,r):
 u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':files_of(c,r['id']),'reactions':reactions_of(c,r['id']),'mentions':[x[0] for x in c.execute('SELECT user_id FROM message_mentions WHERE message_id=?',(r['id'],))]}
def members(c,ch):
 return [x[0] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(ch,))]
def emit(c,ch,typ,payload):
 c.execute('INSERT OR IGNORE INTO counters(channel_id,event_id) VALUES(?,0)',(ch,))
 c.execute('UPDATE counters SET event_id=event_id+1 WHERE channel_id=?',(ch,))
 eid=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(ch,)).fetchone()[0]
 p={'type':typ,'event_id':eid,'channel_id':ch}; p.update(payload)
 c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch,eid,typ,json.dumps(p))); c.commit(); seen_events.add((ch,eid)); broadcast(p); return eid
def sendws(s,obj):
 try:
  b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
  if n < 126: head=b'\x81'+bytes([n])
  elif n < 65536: head=b'\x81\x7e'+struct.pack('!H',n)
  else: head=b'\x81\x7f'+struct.pack('!Q',n)
  s.sendall(head+b)
 except: pass
def broadcast(obj):
 with clients_lock:
  for s,u in list(clients):
   if obj.get('type')=='user.updated' or obj.get('channel_id') in u: sendws(s,obj)
def ws_client(s,uid):
 with clients_lock: clients.append((s,set()))
 presence_token=secrets.token_hex(12)
 c0=db(); c0.execute('INSERT OR REPLACE INTO online_sockets VALUES(?,?,?)',(uid,presence_token,now())); c0.commit(); c0.close()
 try:
  while True:
   h=s.recv(2)
   if not h: break
   opcode=h[0]&15; masked=bool(h[1]&128); ln=h[1]&127
   if ln==126: ln=struct.unpack('!H',s.recv(2))[0]
   elif ln==127: ln=struct.unpack('!Q',s.recv(8))[0]
   mask=s.recv(4) if masked else b''
   data=s.recv(ln)
   if masked: data=bytes(data[i]^mask[i%4] for i in range(ln))
   if opcode==8: break
   if opcode==9:
    s.sendall(b'\x8a'+bytes([len(data)])+data); continue
   if opcode!=1: continue
   x=json.loads(data)
   c=db(); ch=int(x.get('channel_id',0))
   if x.get('type')=='subscribe':
    with clients_lock:
     for i,(ss,u) in enumerate(clients):
      if ss is s: u.add(ch)
    head=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(ch,)).fetchone(); sendws(s,{'type':'subscribed','channel_id':ch,'head_event_id':head[0] if head else 0})
   elif x.get('type')=='resume':
    since=int(x.get('since_event_id',0))
    for r in c.execute('SELECT payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)): sendws(s,json.loads(r[0]))
    head=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(ch,)).fetchone(); sendws(s,{'type':'resumed','channel_id':ch,'head_event_id':head[0] if head else 0})
   c.close()
 except: pass
 with clients_lock: clients[:]=[(ss,u) for ss,u in clients if ss is not s]
 try:
  c0=db(); c0.execute('DELETE FROM online_sockets WHERE token=?',(presence_token,)); c0.commit(); c0.close()
 except: pass
 try:s.close()
 except:pass
class H(BaseHTTPRequestHandler):
 protocol_version="HTTP/1.1"
 def log_message(self,*a): pass
 def reply(self,code,obj=None,headers={}):
  data=json.dumps(obj).encode() if obj is not None else b''
  self.send_response(code); self.send_header('Content-Type','application/json')
  self.send_header('Content-Length',str(len(data)))
  for k,v in headers.items(): self.send_header(k,v)
  self.end_headers()
  if data:self.wfile.write(data)
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/health': return self.reply(200,{'status':'ok','node_id':NODE})
  if path.startswith('/api/ws'):
   qs=parse_qs(p.query); tv=qs.get('token',[''])[0]
   if tv.startswith('Bearer '): tv=tv[7:]
   c=db(); wr=token_user(c,tv); c.close()
   if not wr: self.send_response(401); self.end_headers(); return
   key=self.headers.get('Sec-WebSocket-Key')
   if not key: return self.reply(426,{'error':'websocket required'})
   import hashlib,base64
   accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
   self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
   ws_client(self.connection,wr['id']); return
  u=auth(self)
  if path.startswith('/api/auth/me') or path.startswith('/api/users/'):
   if not u:return self.reply(401,{'error':'unauthorized'})
   c=db()
   if path=='/api/auth/me': out={'user':user_obj(u)}
   else:
    r=c.execute('SELECT * FROM users WHERE id=?',(path.split('/')[-1],)).fetchone()
    if not r:return self.reply(404,{'error':'not found'})
    out={'user':user_obj(r)}
   c.close(); return self.reply(200,out)
  if not u:return self.reply(401,{'error':'unauthorized'})
  c=db()
  fm=re.fullmatch(r'/api/files/(\d+)(/download)?',path)
  if fm:
   fr=c.execute('SELECT * FROM files WHERE id=?',(int(fm.group(1)),)).fetchone()
   if not fr:c.close();return self.reply(404,{'error':'not found'})
   if fm.group(2):
    try:
     data=open(fr['path'],'rb').read()
    except: c.close();return self.reply(404,{'error':'not found'})
    self.send_response(200);self.send_header('Content-Type',fr['content_type']);self.send_header('Content-Disposition','attachment; filename="'+fr['filename'].replace('"','')+'"');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);c.close();return
   out={'file':file_obj(fr)};c.close();return self.reply(200,out)
  if path=='/api/workspaces':
   rows=c.execute('SELECT * FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
   return self.reply(200,{'workspaces':[dict(x) for x in rows]})
  gm=re.fullmatch(r'/api/workspaces/([^/]+)/groups',path)
  if gm:
   w=c.execute('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),)).fetchone()
   if not w:return self.reply(404,{'error':'not found'})
   if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.reply(403,{'error':'forbidden'})
   gs=[]
   for g in c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)):
    gs.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x[0] for x in c.execute('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],))]})
   c.close();return self.reply(200,{'groups':gs})
  rm=re.fullmatch(r'/api/channels/(\d+)/read',path)
  if rm:
   ch=int(rm.group(1)); r=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,u['id'])).fetchone()
   if not r:c.close();return self.reply(403,{'error':'forbidden'})
   rs=c.execute('SELECT * FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],ch)).fetchone()
   last=rs['last_read_event_id'] if rs else 0; mention=rs['mention_count'] if rs else 0
   head=c.execute('SELECT COALESCE(event_id,0) FROM counters WHERE channel_id=?',(ch,)).fetchone()[0]
   c.close();return self.reply(200,{'read_state':{'channel_id':ch,'last_read_event_id':last,'unread_count':max(0,head-last),'mention_count':mention}})
  if path.startswith('/api/workspaces/'):
   slug=path.split('/')[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:return self.reply(404,{'error':'not found'})
   if path.endswith('/members'):
    return self.reply(200,{'members':[dict(x) for x in c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))]})
   chans=[dict(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_dm=0',(w['id'],))]
   return self.reply(200,{'workspace':dict(w),'channels':chans,'read_state':{}})
  if path.startswith('/api/channels/') and path.endswith('/members'):
   ch=path.split('/')[3]; r=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
   if not r:return self.reply(404,{'error':'not found'})
   if r['is_dm'] and u['id'] not in members(c,r['id']):return self.reply(403,{'error':'forbidden'})
   return self.reply(200,{'members':[user_obj(c.execute('SELECT * FROM users WHERE id=?',(x,)).fetchone()) for x in members(c,r['id'])]})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   ch=int(path.split('/')[3]); r=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
   if not r:return self.reply(404,{'error':'not found'})
   if u['id'] not in members(c,ch):return self.reply(403,{'error':'forbidden'})
   rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(ch,)).fetchall()
   return self.reply(200,{'messages':[msg_obj(c,x) for x in rows],'next_cursor':None})
  c.close(); return self.reply(404,{'error':'not found'})
 def do_PATCH(self):
  p=urlparse(self.path); path=p.path; u=auth(self)
  if not u:return self.reply(401,{'error':'unauthorized'})
  gm=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([a-z0-9_-]+)',path)
  if gm:
   x=body(self); c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),)).fetchone(); g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],gm.group(2))).fetchone() if w else None
   if not g:c.close();return self.reply(404,{'error':'not found'})
   if u['id'] not in (g['creator_id'],w['owner_id']):c.close();return self.reply(403,{'error':'forbidden'})
   if 'name' in x:
    if not isinstance(x['name'],str):c.close();return self.reply(400,{'error':'invalid'})
    c.execute('UPDATE groups SET name=? WHERE id=?',(x['name'],g['id']))
   if 'user_ids' in x:
    if not isinstance(x['user_ids'],list):c.close();return self.reply(400,{'error':'invalid'})
    c.execute('DELETE FROM group_users WHERE group_id=?',(g['id'],))
    for uid in x['user_ids']:c.execute('INSERT OR IGNORE INTO group_users VALUES(?,?)',(g['id'],uid))
   c.commit(); gg=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone();out={'id':gg['id'],'handle':gg['handle'],'name':gg['name'],'member_user_ids':[z[0] for z in c.execute('SELECT user_id FROM group_users WHERE group_id=?',(g['id'],))]};c.close();return self.reply(200,{'group':out})
  if path!='/api/users/me':return self.reply(404,{'error':'not found'})
  try:x=body(self)
  except:return self.reply(400,{'error':'bad body'})
  allowed={'display_name':(1,80),'timezone':(1,128),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
  if any(k not in allowed for k in x):return self.reply(400,{'error':'invalid field'})
  for k,v in x.items():
   if not isinstance(v,str) or not (allowed[k][0]<=len(v)<=allowed[k][1]):return self.reply(400,{'error':'invalid'})
   if k=='timezone':
    try:ZoneInfo(v)
    except:return self.reply(400,{'error':'invalid timezone'})
  c=db(); sets=','.join(k+'=?' for k in x); vals=list(x.values())+[u['id']]
  if sets:c.execute('UPDATE users SET '+sets+' WHERE id=?',vals);c.commit()
  r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); out=user_obj(r); frame={'type':'user.updated','user':out}; c.execute('INSERT INTO global_events(payload) VALUES(?)',(json.dumps(frame),)); c.commit(); global seen_global; seen_global=max(seen_global,c.execute('SELECT last_insert_rowid()').fetchone()[0]); broadcast(frame); c.close()
  return self.reply(200,{'user':out})
 def do_DELETE(self):
  p=urlparse(self.path); path=p.path; u=auth(self)
  if not u:return self.reply(401,{'error':'unauthorized'})
  gm=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([a-z0-9_-]+)',path)
  if gm:
   c=db(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),)).fetchone()
   g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],gm.group(2))).fetchone() if w else None
   if g and u['id'] not in (g['creator_id'],w['owner_id']):c.close();return self.reply(403,{'error':'forbidden'})
   if g:c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit()
   c.close();return self.reply(200,{'deleted':True})
  m=re.fullmatch(r'/api/messages/(\d+)/reactions/(.+)',path)
  if not m:return self.reply(404,{'error':'not found'})
  mid=int(m.group(1)); emoji=unquote(m.group(2))
  if not emoji:return self.reply(400,{'error':'invalid emoji'})
  c=db(); r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
  if not r:c.close();return self.reply(404,{'error':'not found'})
  ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
  if u['id'] not in members(c,ch['id']):c.close();return self.reply(403,{'error':'forbidden'})
  old=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
  if old:
   c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
   emit(c,ch['id'],'reaction.removed',{'message_id':mid,'emoji':emoji,'user_id':u['id']})
  out=reactions_of(c,mid);c.close();return self.reply(200,{'reactions':out})
 def do_POST(self):
  p=urlparse(self.path); path=p.path
  if path=='/api/files':
   u=auth(self)
   if not u:return self.reply(401,{'error':'unauthorized'})
   try:
    n=int(self.headers.get('Content-Length','0'))
    raw=self.rfile.read(n)
    ct=self.headers.get('Content-Type','')
    if 'multipart/form-data' not in ct: return self.reply(400,{'error':'multipart required'})
    bm=re.search(r'boundary=(?:"([^"]+)"|([^;]+))',ct)
    if not bm:return self.reply(400,{'error':'multipart required'})
    boundary=(bm.group(1) or bm.group(2)).encode()
    parts=raw.split(b'--'+boundary)
    data=None; filename='file'; typ='application/octet-stream'
    for part in parts:
     if b'Content-Disposition:' not in part or b'name="file"' not in part: continue
     head,sep,val=part.partition(b'\r\n\r\n')
     if not sep: head,sep,val=part.partition(b'\n\n')
     if not sep: continue
     dm=re.search(br'filename="([^"]*)"',head,re.I)
     if dm: filename=dm.group(1).decode('utf-8','replace')
     tm=re.search(br'Content-Type:\s*([^\r\n;]+)',head,re.I)
     if tm: typ=tm.group(1).decode('ascii','replace')
     data=val
     if data.endswith(b'\r\n'): data=data[:-2]
     elif data.endswith(b'\n'): data=data[:-1]
     break
    if data is None:return self.reply(400,{'error':'missing file'})
    if len(data)>10485760:return self.reply(413,{'error':'file too large'})
    c=db(); c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],filename,typ,len(data),'', '')); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    pathf=os.path.join(DATA,'file-'+str(fid));open(pathf,'wb').write(data)
    c.execute('UPDATE files SET path=?,created_at=? WHERE id=?',(pathf,now(),fid));c.commit(); fr=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return self.reply(201,{'file':file_obj(fr)})
   except Exception:return self.reply(400,{'error':'bad multipart'})
  if path=='/api/auth/register':
   try:x=body(self); un=x['username']; pw=x['password']
   except:return self.reply(400,{'error':'bad body'})
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.reply(400,{'error':'invalid'})
   c=db()
   try:
    c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji,created_at) VALUES(?,?,?,?,?,?,?,?)',(un,pw,x.get('display_name') or un,'UTC','','','',now())); c.commit()
   except sqlite3.IntegrityError:c.close();return self.reply(409,{'error':'duplicate'})
   r=c.execute('SELECT * FROM users WHERE username=?',(un,)).fetchone(); t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'],));c.commit();c.close();return self.reply(201,{'user':user_obj(r),'token':t})
  if path=='/api/auth/login':
   x=body(self);c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),x.get('password'))).fetchone()
   if not r:c.close();return self.reply(401,{'error':'invalid'})
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'],));c.commit();c.close();return self.reply(200,{'user':user_obj(r),'token':t})
  u=auth(self)
  if not u:return self.reply(401,{'error':'unauthorized'})
  c=db()
  fm=re.fullmatch(r'/api/files/(\d+)(/download)?',path)
  if fm:
   fr=c.execute('SELECT * FROM files WHERE id=?',(int(fm.group(1)),)).fetchone()
   if not fr:c.close();return self.reply(404,{'error':'not found'})
   if fm.group(2):
    try:
     data=open(fr['path'],'rb').read()
    except: c.close();return self.reply(404,{'error':'not found'})
    self.send_response(200);self.send_header('Content-Type',fr['content_type']);self.send_header('Content-Disposition','attachment; filename="'+fr['filename'].replace('"','')+'"');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);c.close();return
   out={'file':file_obj(fr)};c.close();return self.reply(200,out)
  if path=='/api/workspaces':
   x=body(self); slug=x.get('slug','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not x.get('name'):return self.reply(400,{'error':'invalid'})
   try:
    c.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(slug,x['name'],u['id'],'open')); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
   except sqlite3.IntegrityError:return self.reply(409,{'error':'duplicate'})
   return self.reply(201,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':dict(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
  m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',path)
  if m:
   slug=m.group(1); x=body(self); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
   if not w:c.close();return self.reply(404,{'error':'not found'})
   if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():c.close();return self.reply(403,{'error':'forbidden'})
   name=x.get('name','')
   if not isinstance(name,str) or not re.fullmatch(r'[a-z0-9-]{1,32}',name):c.close();return self.reply(400,{'error':'invalid'})
   if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name)).fetchone():c.close();return self.reply(409,{'error':'duplicate'})
   private=1 if x.get('is_private') else 0
   c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,private,x.get('topic','') or '')); ch=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute('INSERT INTO channel_members VALUES(?,?)',(ch,u['id']));c.commit()
   out=dict(c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone());c.close();return self.reply(201,{'channel':out})
  m=re.fullmatch(r'/api/channels/(\d+)/join',path)
  if m:
   ch=int(m.group(1)); r=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
   if not r:c.close();return self.reply(404,{'error':'not found'})
   if r['is_private']:c.close();return self.reply(403,{'error':'forbidden'})
   if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,u['id'])).fetchone():
    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch,u['id'],))
    if r['workspace_id']: c.execute('INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)',(r['workspace_id'],u['id'],'member'))
    c.commit()
   out=dict(r);c.close();return self.reply(200,{'channel':out})
  rm=re.fullmatch(r'/api/channels/(\d+)/read',path)
  if rm:
   ch=int(rm.group(1)); x=body(self)
   if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch,u['id'])).fetchone():c.close();return self.reply(403,{'error':'forbidden'})
   try: target=int(x['last_read_event_id'])
   except: c.close();return self.reply(400,{'error':'invalid'})
   cur=c.execute('SELECT last_read_event_id,mention_count FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],ch)).fetchone()
   old=cur['last_read_event_id'] if cur else 0; mention=cur['mention_count'] if cur else 0
   last=max(old,target)
   if target>old: mention=0
   c.execute('INSERT OR REPLACE INTO read_states(user_id,channel_id,last_read_event_id,mention_count) VALUES(?,?,?,?)',(u['id'],ch,last,mention));c.commit()
   head=c.execute('SELECT COALESCE(event_id,0) FROM counters WHERE channel_id=?',(ch,)).fetchone()[0]
   c.close();return self.reply(200,{'read_state':{'channel_id':ch,'last_read_event_id':last,'unread_count':max(0,head-last),'mention_count':mention}})
  gm=re.fullmatch(r'/api/workspaces/([^/]+)/groups',path)
  if gm:
   x=body(self); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),)).fetchone()
   if not w:return self.reply(404,{'error':'not found'})
   if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return self.reply(403,{'error':'forbidden'})
   h=x.get('handle',''); name=x.get('name','')
   if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9_-]{2,32}',h) or not isinstance(name,str):return self.reply(400,{'error':'invalid'})
   if c.execute('SELECT 1 FROM groups WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone():return self.reply(409,{'error':'duplicate'})
   c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,u['id'])); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   for uid in x.get('user_ids',[]): c.execute('INSERT OR IGNORE INTO group_users VALUES(?,?)',(gid,uid))
   c.commit(); g=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone(); out={'id':gid,'handle':h,'name':name,'member_user_ids':[z[0] for z in c.execute('SELECT user_id FROM group_users WHERE group_id=?',(gid,))]};c.close();return self.reply(201,{'group':out})
  if path=='/api/dms':
   x=body(self); oid=x.get('user_id')
   if not oid:return self.reply(400,{'error':'bad body'})
   if not c.execute('SELECT 1 FROM users WHERE id=?',(oid,)).fetchone():return self.reply(404,{'error':'not found'})
   if int(oid)==u['id']:return self.reply(400,{'error':'invalid'})
   key=':'.join(map(str,sorted([u['id'],int(oid)]))); r=c.execute('SELECT id FROM channels WHERE dm_key=?',(key,)).fetchone()
   if r: c.close();return self.reply(200,{'channel_id':r[0]})
   c.execute('INSERT INTO channels(name,is_dm,dm_key) VALUES(?,?,?)',('dm',1,key));ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?),(?,?)',(ch,u['id'],ch,int(oid)));c.commit();c.close();return self.reply(200,{'channel_id':ch})
  m=re.fullmatch(r'/api/messages/(\d+)/reactions',path)
  if m:
   try:x=body(self); emoji=x.get('emoji')
   except: c.close();return self.reply(400,{'error':'bad body'})
   if not isinstance(emoji,str) or not emoji:c.close();return self.reply(400,{'error':'invalid emoji'})
   mid=int(m.group(1)); r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
   if not r:c.close();return self.reply(404,{'error':'not found'})
   ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
   if u['id'] not in members(c,ch['id']):c.close();return self.reply(403,{'error':'forbidden'})
   added=False
   try:
    c.execute('INSERT INTO reactions(message_id,user_id,emoji,created_at) VALUES(?,?,?,?)',(mid,u['id'],emoji,now())); added=True
   except sqlite3.IntegrityError: pass
   if added: emit(c,ch['id'],'reaction.added',{'message_id':mid,'emoji':emoji,'user_id':u['id']})
   out=reactions_of(c,mid);c.close();return self.reply(200,{'reactions':out})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   ch=int(path.split('/')[3]);x=body(self);r=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
   if not r:return self.reply(404,{'error':'not found'})
   if u['id'] not in members(c,ch):return self.reply(403,{'error':'forbidden'})
   text=x.get('body')
   if not isinstance(text,str) or not text:return self.reply(400,{'error':'empty'})
   if text.startswith('/') and not text.startswith('//'):
    bits=text[1:].split(' ',1); cmd=bits[0].lower(); arg=bits[1].strip() if len(bits)>1 else ''
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(r['workspace_id'],)).fetchone()
    owner=(w and w['owner_id']==u['id'])
    if cmd=='me':
     if not arg:return self.reply(400,{'error':'text required'})
     text='/me '+arg
    elif cmd=='shrug':
     text=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='topic':
     if len(arg)>250:return self.reply(400,{'error':'topic too long'})
     c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,ch)); c.commit()
     updated=dict(c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()); emit(c,ch,'channel.updated',{'channel':updated});c.close()
     return self.reply(201,{'message':None,'channel':updated})
    elif cmd=='invite':
     if not arg:return self.reply(400,{'error':'username required'})
     username=arg[1:] if arg.startswith('@') else arg
     target=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
     if not target:c.close();return self.reply(404,{'error':'user not found'})
     c.execute('INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)',(r['workspace_id'],target['id'],'member'))
     c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch,target['id']));c.commit()
     emit(c,ch,'member.joined',{'user_id':target['id']})
     c.close()
     return self.reply(201,{'message':None,'invited_user':user_obj(target)})
    elif cmd in ('archive','unarchive'):
     if not owner:c.close();return self.reply(403,{'error':'forbidden'})
     archived=1 if cmd=='archive' else 0
     c.execute('UPDATE channels SET is_archived=? WHERE id=?',(archived,ch));c.commit()
     updated=dict(c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()); emit(c,ch,'channel.archived' if archived else 'channel.unarchived',{'channel':updated});c.close()
     return self.reply(201,{'message':None,'channel':updated})
    else:
     c.close();return self.reply(400,{'error':'unknown command'})
   if r['is_archived']:return self.reply(423,{'error':'channel archived'})
   x['body']=text
   ids=x.get('file_ids',[])
   if not isinstance(ids,list):return self.reply(400,{'error':'invalid files'})
   for fid in ids:
    fr=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not fr or fr['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():return self.reply(400,{'error':'invalid file'})
   names=re.findall(r'(?<![A-Za-z0-9_.])@([A-Za-z0-9_-]+)',text)
   mids=set()
   for nm in names:
    if nm=='channel': mids.update(v for v in members(c,ch) if v!=u['id'])
    elif nm=='here':
     online={v[0] for v in c.execute('SELECT user_id FROM online_sockets')}; mids.update(v for v in members(c,ch) if v in online and v!=u['id'])
    else:
     ur=c.execute('SELECT id FROM users WHERE username=?',(nm,)).fetchone()
     if ur and c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(r['workspace_id'],ur['id'])).fetchone() and ur['id']!=u['id']:mids.add(ur['id'])
     gr=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(r['workspace_id'],nm)).fetchone()
     if gr:mids.update(v[0] for v in c.execute('SELECT user_id FROM group_users WHERE group_id=? AND user_id IN (SELECT user_id FROM channel_members WHERE channel_id=?) AND user_id!=?',(gr['id'],ch,u['id'])))
   c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch,u['id'],x['body'],x.get('parent_id'),now()));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   for uid in mids:c.execute('INSERT INTO message_mentions VALUES(?,?)',(mid,uid))
   for uid in mids:
    c.execute('INSERT OR IGNORE INTO read_states(user_id,channel_id,last_read_event_id,mention_count) VALUES(?,?,0,0)',(uid,ch))
    c.execute('UPDATE read_states SET mention_count=mention_count+1 WHERE user_id=? AND channel_id=?',(uid,ch))
   for fid in ids:c.execute('INSERT INTO message_files VALUES(?,?)',(mid,fid))
   mr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); emit(c,ch,'message.created',{'message':msg_obj(c,mr)});out=msg_obj(c,mr);c.close();return self.reply(201,{'message':out})
  c.close();return self.reply(404,{'error':'not found'})
def poll_events():
 global seen_global
 while True:
  try:
   c=db()
   for r in c.execute('SELECT channel_id,event_id,payload FROM events ORDER BY channel_id,event_id'):
    k=(r['channel_id'],r['event_id'])
    if k not in seen_events:
     seen_events.add(k); broadcast(json.loads(r['payload']))
   for r in c.execute('SELECT id,payload FROM global_events WHERE id>? ORDER BY id',(seen_global,)):
    seen_global=max(seen_global,r['id']); broadcast(json.loads(r['payload']))
   c.close()
  except Exception: pass
  time.sleep(.08)

def run():
 init()
 global seen_global
 c=db()
 seen_events.update((r['channel_id'],r['event_id']) for r in c.execute('SELECT channel_id,event_id FROM events'))
 row=c.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()
 seen_global=row[0] if row else 0
 c.close()
 threading.Thread(target=poll_events,daemon=True).start()
 class S(ThreadingHTTPServer): allow_reuse_address=True
 http=S(('127.0.0.1',PORT),H)
 http.serve_forever()
if __name__=='__main__': run()
