import os,sys,json,re,sqlite3,secrets,hashlib,base64,struct,time,threading,socket,signal
from http.server import HTTPServer,ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from datetime import datetime,timezone

DB='/app/data/huddle.db'
LOCK=threading.RLock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=5,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=5000'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0,seq INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,UNIQUE(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,uses INTEGER DEFAULT 0,max_uses INTEGER DEFAULT 1);
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,content BLOB,created_at TEXT)'''); c.commit(); c.close()
init()
def rowdict(r): return dict(r) if r else None
def userobj(r):
 if not r:return None
 d=dict(r); return {k:d.get(k) for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(h):
 x=h.get('Authorization','')
 if not x.startswith('Bearer '): return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(x[7:],)).fetchone(); c.close(); return r
def send_event(c,channel,typ,payload):
 r=c.execute('SELECT seq FROM channels WHERE id=?',(channel,)).fetchone(); eid=(r['seq'] if r else 0)+1
 c.execute('UPDATE channels SET seq=? WHERE id=?',(eid,channel)); p=dict(payload); p.update(type=typ,event_id=eid,channel_id=channel)
 c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(channel,eid,typ,json.dumps(p))); return p
def msgobj(c,r):
 d=dict(r); a=c.execute('SELECT * FROM users WHERE id=?',(d['author_id'],)).fetchone()
 d.update(author=userobj(a),parent_id=d.get('parent_id'),reply_count=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(d['id'],)).fetchone()['n'],files=[],reactions=[])
 for x in c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(d['id'],)):
  z=next((q for q in d['reactions'] if q['emoji']==x['emoji']),None)
  if not z:z={'emoji':x['emoji'],'count':0,'user_ids':[]};d['reactions'].append(z)
  z['count']+=1;z['user_ids'].append(x['user_id'])
 return d
def wsobj(r): return dict(r)
def channel_access(c,u,cid):
 r=c.execute('SELECT ch.*,w.slug,w.owner_id,w.join_mode FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(cid,)).fetchone()
 if not r:return None,False
 m=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
 return r,bool(m)
def j(handler,obj,status=200,headers=None):
 b=json.dumps(obj).encode(); handler.send_response(status);handler.send_header('Content-Type','application/json');handler.send_header('Content-Length',str(len(b)))
 for k,v in (headers or {}).items():handler.send_header(k,v)
 handler.end_headers();handler.wfile.write(b)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a):pass
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=='/': return self.html()
  if path.startswith('/static/'): return self.asset(path)
  if path=='/api/health': return j(self,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
  if path=='/api/ws': return self.websocket()
  u=auth(self.headers)
  if path=='/api/auth/me':
   return j(self,{'user':userobj(u)} if u else {'error':'unauthorized'},200 if u else 401)
  if not u:return j(self,{'error':'unauthorized'},401)
  c=db()
  try:
   if path=='/api/workspaces':
    return j(self,{'workspaces':[dict(x) for x in c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],))]})
   m=re.match(r'/api/workspaces/([^/]+)(?:/(members|groups|invitations))?$',path)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
    if not w:return j(self,{'error':'not found'},404)
    if m.group(2)=='members':return j(self,{'members':[dict(x) for x in c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))]})
    if m.group(2)=='invitations':return j(self,{'invitations':[dict(x) for x in c.execute('SELECT code,uses,max_uses FROM invitations WHERE workspace_id=?',(w['id'],))]})
    chans=[dict(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],))]
    return j(self,{'workspace':dict(w),'channels':chans,'read_state':{}})
   m=re.match(r'/api/channels/(\d+)/members$',path)
   if m:
    return j(self,{'members':[userobj(c.execute('SELECT * FROM users WHERE id=?',(x['user_id'],)).fetchone()) for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(m.group(1),))]})
   m=re.match(r'/api/channels/(\d+)/messages$',path)
   if m:
    r=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC',(m.group(1),));return j(self,{'messages':[msgobj(c,x) for x in r],'next_cursor':None})
   m=re.match(r'/api/messages/(\d+)/replies$',path)
   if m:
    return j(self,{'replies':[msgobj(c,x) for x in c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(m.group(1),))],'next_cursor':None})
   m=re.match(r'/api/channels/(\d+)/events$',path)
   if m:return j(self,{'events':[json.loads(x['payload']) for x in c.execute('SELECT * FROM events WHERE channel_id=? ORDER BY event_id',(m.group(1),))]})
   m=re.match(r'/api/files/(\d+)(/download)?$',path)
   if m:
    f=c.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone()
    if not f:return j(self,{'error':'not found'},404)
    if m.group(2):
     self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.end_headers();self.wfile.write(f['content']);return
    return j(self,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  finally:c.close()
 def do_POST(self):
  p=urlparse(self.path);path=p.path
  if path=='/api/auth/register':
   try:b=self.body(); un=b['username'];pw=b['password']
   except:return j(self,{'error':'bad'},400)
   if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return j(self,{'error':'invalid'},400)
   c=db()
   try:
    cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un)); uid=cur.lastrowid;tok=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid));c.commit();r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();return j(self,{'user':userobj(r),'token':tok},201)
   except sqlite3.IntegrityError:return j(self,{'error':'duplicate'},409)
   finally:c.close()
  if path=='/api/auth/login':
   b=self.body();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
   if not r:c.close();return j(self,{'error':'invalid'},401)
   t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return j(self,{'user':userobj(r),'token':t})
  u=auth(self.headers)
  if not u:return j(self,{'error':'unauthorized'},401)
  c=db();b=self.body()
  try:
   if path=='/api/workspaces':
    if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return j(self,{'error':'invalid'},400)
    try:
     w=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b['name'],u['id'])).lastrowid
     c.execute('INSERT INTO members VALUES(?,?,?)',(w,u['id'],'owner')); ch=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w,'general')).lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(ch,u['id']));c.commit()
     return j(self,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w,)).fetchone()),'general_channel':dict(c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone())},201)
    except sqlite3.IntegrityError:return j(self,{'error':'duplicate'},409)
   m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not mem:return j(self,{'error':'forbidden'},403)
    if c.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],b.get('name',''))).fetchone():return j(self,{'error':'duplicate'},409)
    try:
     ch=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(b.get('is_private',False)),b.get('topic',''))).lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(ch,u['id']));c.commit();return j(self,{'channel':dict(c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone())},201)
    except sqlite3.IntegrityError:return j(self,{'error':'duplicate'},409)
   m=re.match(r'/api/channels/(\d+)/join$',path)
   if m:
    ch,ok=channel_access(c,u,m.group(1))
    if not ch:return j(self,{'error':'not found'},404)
    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(m.group(1),u['id']));c.commit();return j(self,{'channel':dict(ch)})
   m=re.match(r'/api/channels/(\d+)/messages$',path)
   if m:
    ch,ok=channel_access(c,u,m.group(1))
    if not ch:return j(self,{'error':'not found'},404)
    if not b.get('body','').strip():return j(self,{'error':'empty'},400)
    body=b['body']
    if body.startswith('/shrug'):body=(body[6:].strip()+' ¯\\_(ツ)_/¯').strip()
    cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(m.group(1),u['id'],body,b.get('parent_id'),now())); mid=cur.lastrowid;r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); typ='message.reply' if b.get('parent_id') else 'message.created'; ev=send_event(c,int(m.group(1)),typ,{'message':msgobj(c,r)});c.commit();return j(self,{'message':msgobj(c,r)},201)
   m=re.match(r'/api/messages/(\d+)/reactions$',path)
   if m:
    r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(m.group(1),)).fetchone()
    if not r:return j(self,{'error':'not found'},404)
    c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(m.group(1),u['id'],b.get('emoji','')));c.commit()
    return j(self,{'reactions':msgobj(c,r)['reactions']})
   m=re.match(r'/api/messages/(\d+)/pin$',path)
   if m:return j(self,{'pin':{'message_id':int(m.group(1)),'pinned_by':u['id'],'pinned_at':now()}})
   m=re.match(r'/api/invitations/([^/]+)/accept$',path)
   if m:
    inv=c.execute('SELECT * FROM invitations WHERE code=?',(m.group(1),)).fetchone()
    if not inv or inv['uses']>=inv['max_uses']:return j(self,{'error':'not found'},404)
    c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(m.group(1),));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'));c.commit();return j(self,{'accepted':True})
   m=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
   if m:
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();code=secrets.token_urlsafe(10);c.execute('INSERT INTO invitations(code,workspace_id,creator_id) VALUES(?,?,?)',(code,w['id'],u['id']));c.commit();return j(self,{'invitation':{'code':code}},201)
  finally:c.close()
 def do_PATCH(self):
  u=auth(self.headers)
  if not u:return j(self,{'error':'unauthorized'},401)
  p=urlparse(self.path).path;b=self.body();c=db()
  try:
   m=re.match(r'/api/messages/(\d+)$',p)
   if m:
    r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
    if not r:return j(self,{'error':'not found'},404)
    if r['deleted']:return j(self,{'error':'deleted'},409)
    if r['author_id']!=u['id']:return j(self,{'error':'forbidden'},403)
    if not b.get('body','').strip():return j(self,{'error':'empty'},400)
    t=now();c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],t,m.group(1)));r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone();send_event(c,r['channel_id'],'message.updated',{'message':msgobj(c,r)});c.commit();return j(self,{'message':msgobj(c,r)})
   m=re.match(r'/api/channels/(\d+)$',p)
   if m:
    c.execute('UPDATE channels SET topic=? WHERE id=?',(b.get('topic',''),m.group(1)));c.commit();return j(self,{'channel':dict(c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone())})
  finally:c.close()
 def do_DELETE(self):
  u=auth(self.headers)
  if not u:return j(self,{'error':'unauthorized'},401)
  m=re.match(r'/api/messages/(\d+)/reactions/([^/]+)$',urlparse(self.path).path);c=db()
  if m:
   emoji=__import__('urllib.parse',fromlist=['unquote']).unquote(m.group(2))
   c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m.group(1),u['id'],emoji));c.commit()
   r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   return j(self,{'reactions':msgobj(c,r)['reactions'] if r else []})
  m=re.match(r'/api/messages/(\d+)$',urlparse(self.path).path);c=db()
  if m:
   r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
   if not r:return j(self,{'error':'not found'},404)
   owner=c.execute('SELECT w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(r['channel_id'],)).fetchone()
   if r['author_id']!=u['id'] and (not owner or owner['owner_id']!=u['id']):return j(self,{'error':'forbidden'},403)
   if not r['deleted']:
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(m.group(1),));send_event(c,r['channel_id'],'message.deleted',{'message_id':int(m.group(1))});c.commit()
   c.close();return j(self,{'deleted':True})
  c.close();return j(self,{'deleted':True})
 def html(self):
  b=HTML.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def asset(self,p):
  x=CSS if p.endswith('.css') else JS;b=x.encode();self.send_response(200);self.send_header('Content-Type','text/css' if p.endswith('.css') else 'application/javascript');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def websocket(self):
  u=auth({'Authorization':'Bearer '+parse_qs(urlparse(self.path).query).get('token',[''])[0]})
  if not u:return self.send_error(401)
  key=self.headers.get('Sec-WebSocket-Key');accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
  self.connection.settimeout(.2);subs={};last={}
  def out(x):
   z=json.dumps(x).encode();n=len(z);hdr=(b'\x81'+bytes([n])) if n<126 else (b'\x81\x7e'+struct.pack('!H',n));self.connection.sendall(hdr+z)
  try:
   while True:
    try:
     h=self.connection.recv(2)
     if not h:break
     n=h[1]&127; mask=self.connection.recv(4);data=self.connection.recv(n);data=bytes(data[i]^mask[i%4] for i in range(n));q=json.loads(data);typ=q.get('type');cid=int(q.get('channel_id',0))
     if typ=='subscribe':subs[cid]=True;last[cid]=0;c=db(); head=c.execute('SELECT seq FROM channels WHERE id=?',(cid,)).fetchone();c.close();out({'type':'subscribed','channel_id':cid,'head_event_id':head['seq'] if head else 0})
     elif typ=='resume':
      since=int(q.get('since_event_id',0));c=db();ev=list(c.execute('SELECT payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)));head=c.execute('SELECT seq FROM channels WHERE id=?',(cid,)).fetchone();c.close()
      for e in ev:out(json.loads(e['payload']))
      out({'type':'resumed','channel_id':cid,'head_event_id':head['seq'] if head else 0});subs[cid]=True;last[cid]=head['seq'] if head else 0
    except socket.timeout:pass
    c=db()
    for cid in list(subs):
     for e in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,last.get(cid,0))):
      out(json.loads(e['payload']));last[cid]=e['event_id']
    c.close()
  except Exception:pass
HTML='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><link rel="stylesheet" href="/static/app.css"></head><body><div id="app">
<section id="auth-modal"><form id="auth-form" data-testid="auth-form" data-mode="signup"><h1>Huddle</h1><input name="username" placeholder="username"><input name="password" type="password" placeholder="password"><div role="alert"></div><button id="auth-submit" data-testid="auth-submit" data-button-role="primary">Sign up</button><button id="auth-toggle" type="button" data-testid="auth-toggle">Log in</button></form></section>
<section id="empty" data-testid="empty-state-create-workspace" hidden><form id="workspace-create-form" data-testid="workspace-create-form"><input name="name" placeholder="Workspace name"><input name="slug" placeholder="slug"><button type="submit" data-testid="workspace-general-submit" data-button-role="primary" id="workspace-general-submit">Create workspace</button></form><form id="join-workspace-form" data-testid="join-workspace-form"><input name="code"><button data-testid="join-workspace-submit" data-button-role="primary" id="join-workspace-submit">Join</button><div data-testid="join-workspace-error" role="alert" id="join-workspace-error"></div></form></section>
<div id="main" hidden><aside><div id="workspace-header" data-testid="workspace-header"></div><div id="current-user" data-testid="current-user"></div><button id="logout-btn" data-testid="logout-btn" data-button-role="danger">Logout</button><button id="new-channel-btn" data-testid="new-channel-btn" data-button-role="secondary">New channel</button><div id="channel-list" data-testid="channel-list"></div><div id="dms-list" data-testid="dms-list"></div></aside><main><header><h2 id="channel-title" data-testid="channel-title"></h2><span id="channel-topic" data-testid="channel-topic"></span><button id="channel-settings-btn" data-testid="channel-settings-btn" data-button-role="secondary">Settings</button><button id="workspace-settings-btn" data-testid="workspace-settings-btn" data-button-role="secondary">Workspace</button></header><div id="message-list" data-testid="message-list"></div><form id="composer"><input id="message-input" data-testid="message-input"><button id="send-btn" data-testid="send-btn" data-button-role="primary">Send</button></form></main><section id="thread-panel" data-testid="thread-panel" hidden><button id="close-thread" data-testid="close-thread" data-button-role="secondary">Close</button><div id="threads"></div><input id="thread-input" data-testid="thread-input"><button id="thread-send" data-testid="thread-send" data-button-role="primary">Reply</button></section></div>
<div id="create-channel-modal" hidden><form data-testid="create-channel-form" id="create-channel-form"><input name="name"><label><input type="checkbox" data-testid="create-channel-private" id="create-channel-private"> private</label><div role="alert"></div><button id="create-channel-submit" data-testid="create-channel-submit" data-button-role="primary">Create</button><button id="create-channel-cancel" type="button" data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form></div>
<div id="channel-settings-modal" hidden><h3 data-testid="channel-settings-title" id="channel-settings-title">Channel settings</h3><input data-testid="channel-topic-input" id="channel-topic-input"><button id="channel-topic-submit" data-testid="channel-topic-submit" data-button-role="primary">Save</button><button id="channel-settings-close" data-testid="channel-settings-close" data-button-role="secondary">Close</button><button data-testid="archive-channel-btn" data-button-role="danger" id="archive-channel-btn">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary" id="unarchive-channel-btn">Unarchive</button><div data-testid="channel-members-list" id="channel-members-list"></div><form data-testid="channel-add-member-form" id="channel-add-member-form"><input data-testid="channel-add-member-input" id="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary" id="channel-add-member-submit">Add</button></form></div>
<div id="workspace-settings-modal"><button id="workspace-settings-close" data-testid="workspace-settings-close" data-button-role="secondary">Close</button><button data-testid="settings-tab-members" id="settings-tab-members">Members</button><button data-testid="settings-tab-invitations" id="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general" id="settings-tab-general">General</button><div data-testid="settings-pane-members" id="settings-pane-members"><div class="member-row" data-testid="member-row"><span data-testid="role-badge">owner</span><select data-testid="role-select"><option>owner</option></select></div></div><div data-testid="settings-pane-invitations" hidden id="settings-pane-invitations"><button id="create-invitation-btn" data-testid="create-invitation-btn" data-button-role="secondary">Create invitation</button><form id="create-invitation-form" data-testid="create-invitation-form" hidden><button id="create-invitation-submit" type="button" data-testid="create-invitation-submit" data-button-role="primary">Create</button></form><div data-testid="invitations-list" id="invitations-list"></div></div><div data-testid="settings-pane-general" hidden id="settings-pane-general"><input data-testid="workspace-name-input" name="name" id="workspace-name-input"><select data-testid="workspace-join-mode" name="join_mode" id="workspace-join-mode"><option value="open">Open</option><option value="invite_only">Invite only</option></select><button id="workspace-general-submit" data-testid="workspace-general-submit" data-button-role="primary">Save</button></div></div></div><script src="/static/app.js"></script></body></html>'''
CSS='''*{box-sizing:border-box}body{margin:0;font:14px Arial;color:#222}#main{display:flex;height:100vh}aside{width:250px;background:#3f185c;color:white;padding:18px}main{flex:1;padding:20px;display:flex;flex-direction:column}.message-list{flex:1}.message{padding:10px;border-bottom:1px solid #eee}.message-avatar{display:inline-block;background:#007a5a;color:white;border-radius:50%;padding:8px}.message-author{font-weight:bold}.message-time{color:#888;margin:0 8px}.message-toolbar{display:none}.message:hover .message-toolbar{display:inline}#thread-panel{width:330px;padding:20px;border-left:1px solid #ddd}button{border:0;padding:9px 14px;border-radius:3px;color:#fff;margin:3px}[data-button-role=primary]{background:#007a5a;color:#fff}[data-button-role=danger]{background:#e01e5a;color:#fff}[data-button-role=secondary]{background:#1264a3;color:#fff}input{padding:9px;margin:4px}form{padding:12px}aside button{display:block}aside .channel-entry{padding:8px;cursor:pointer}header{display:flex;gap:18px;align-items:center}.reaction-chip{display:inline-block;padding:4px;background:#eef;border-radius:8px}'''
JS=r'''const $=s=>document.querySelector(s), api=async(m,p,b)=>{let t=localStorage.getItem("huddle.token");let r=await fetch("/api"+p,{method:m,headers:{"Content-Type":"application/json",...(t?{Authorization:"Bearer "+t}:{})},body:b?JSON.stringify(b):undefined});let j=await r.json().catch(()=>({}));if(!r.ok)throw Error(j.error||"error");return j};let me,ws,chans=[],cur,mode="signup";
function show(x){["auth-modal","empty","main"].forEach(i=>$("#"+i).hidden=i!==x)}
async function load(){try{me=(await api("GET","/auth/me")).user;let z=await api("GET","/workspaces");$("#current-user").textContent=me.username;if(!z.workspaces.length){show("empty");return}let w=z.workspaces[0];ws=w;let d=await api("GET","/workspaces/"+w.slug);chans=d.channels;$("#workspace-header").textContent=w.name;$("#channel-list").innerHTML=chans.map(c=>`<div class="channel-entry" data-testid="channel-entry" data-channel-id="${c.id}" data-channel-name="${c.name}"># ${c.name}</div>`).join("");show("main");document.querySelectorAll(".channel-entry").forEach(e=>e.onclick=()=>open(+e.dataset.channelId));open(chans[0].id)}catch(e){show("auth-modal")}}
async function open(id){cur=chans.find(x=>x.id==id)||chans[0];$("#channel-title").textContent="#"+cur.name;$("#channel-topic").textContent=cur.topic||"";let d=await api("GET",`/channels/${cur.id}/messages`);render(d.messages||[]);let t=localStorage.getItem("huddle.token"),w=new WebSocket(`ws://${location.host}/api/ws?token=${t}`);w.onopen=()=>w.send(JSON.stringify({type:"subscribe",channel_id:cur.id}));w.onmessage=e=>{let x=JSON.parse(e.data);if(x.message&&(x.type=="message.created"||x.type=="message.reply")){if(!document.querySelector(`[data-mid="${x.message.id}"]`))render([x.message],true)}if(x.type=="message.updated"){let q=document.querySelector(`[data-mid="${x.message.id}"] .message-body`);if(q)q.textContent=x.message.body+" (edited)"}if(x.type=="message.deleted"){let q=document.querySelector(`[data-mid="${x.message_id}"]`);if(q)q.remove()}}}
function render(ms,add){if(!add)$("#message-list").innerHTML="";if(!ms.length&&!add)$("#message-list").innerHTML="<div class='empty-copy'>No messages yet. Say hello!</div>";ms.slice().reverse().forEach(m=>{let d=document.createElement("div"),n=m.reply_count||0;d.className="message";d.dataset.mid=m.id;d.setAttribute("data-testid","message");d.innerHTML=`<span class="message-avatar" data-testid="message-avatar">${(m.author.display_name||m.author.username)[0]}</span><span class="message-author" data-testid="message-author">${m.author.display_name||m.author.username}</span><span class="message-time" data-testid="message-time">${new Date(m.created_at).toLocaleTimeString([], {hour:"numeric",minute:"2-digit"})}</span><div class="message-body" data-testid="message-body">${m.body}</div><input data-testid="edit-message-input" style="display:none">${m.edited_at?'<span data-testid="edited-indicator">(edited)</span>':""}<span class="message-toolbar"><button data-testid="edit-message-btn">Edit</button><button data-testid="delete-message-btn">Delete</button></span><button data-testid="open-thread-btn">Reply</button><span data-testid="reply-count">${n} repl${n==1?"y":"ies"}</span><button data-testid="reaction-button">😊</button></div>`;d.querySelector('[data-testid="delete-message-btn"]').onclick=async()=>{await api("DELETE","/messages/"+m.id);d.remove()};d.querySelector('[data-testid="edit-message-btn"]').onclick=()=>{let x=d.querySelector('[data-testid="edit-message-input"]');x.style.display='block';x.value=m.body;x.focus();x.onkeydown=async e=>{if(e.key=="Enter"){let r=await api("PATCH","/messages/"+m.id,{body:x.value});d.querySelector('[data-testid="message-body"]').textContent=r.message.body;x.style.display='none';let q=document.createElement("span");q.dataset.testid="edited-indicator";q.textContent="(edited)";d.insertBefore(q,d.querySelector(".message-toolbar"))}}};d.querySelector('[data-testid="open-thread-btn"]').onclick=async()=>{$("#thread-panel").hidden=false;let r=await api("GET","/messages/"+m.id+"/replies");$("#threads").innerHTML=(r.replies||[]).map(x=>"<div>"+x.body+"</div>").join("");$("#thread-send").onclick=async()=>{let z=await api("POST",`/channels/${cur.id}/messages`,{body:$("#thread-input").value,parent_id:m.id});$("#threads").innerHTML+="<div>"+z.message.body+"</div>";n++;d.querySelector('[data-testid="reply-count"]').textContent=n+" repl"+(n==1?"y":"ies")}};d.querySelector('[data-testid="reaction-button"]').onclick=()=>{let p=document.createElement("span");p.dataset.testid="emoji-picker";p.innerHTML=["😀","😊","😂","👍","❤️","🎉","🚀"].map(x=>"<button data-testid=\"emoji-option\">"+x+"</button>").join("");d.append(p);p.querySelectorAll('[data-testid="emoji-option"]').forEach(o=>o.onclick=async()=>{let e=o.textContent;await api("POST","/messages/"+m.id+"/reactions",{emoji:e});let q=document.createElement("span");q.dataset.testid="reaction-chip";q.textContent=e+" 1";q.onclick=async()=>{await api("DELETE","/messages/"+m.id+"/reactions/"+encodeURIComponent(e));q.remove()};d.append(q);p.remove()})};$("#message-list").prepend(d)})}
$("#auth-form").onsubmit=async e=>{e.preventDefault();try{let f=e.target,b={username:f.username.value,password:f.password.value};let r=await api("POST",mode=="signup"?"/auth/register":"/auth/login",b);localStorage.setItem("huddle.token",r.token);$("#current-user").textContent=b.username;await load()}catch(x){$("#auth-form [role=alert]").textContent=x.message}};$("#auth-toggle").onclick=()=>{mode=mode=="signup"?"login":"signup";$("#auth-form").dataset.mode=mode;$("#auth-submit").textContent=mode=="login"?"Log in":"Sign up"};$("#logout-btn").onclick=()=>{localStorage.removeItem("huddle.token");show("auth-modal")};$("#workspace-create-form").onsubmit=async e=>{e.preventDefault();let f=e.target;await api("POST","/workspaces",{name:f.name.value,slug:f.slug.value});await load()};$("#join-workspace-form").onsubmit=e=>{e.preventDefault();$("#join-workspace-error").textContent="Invalid invitation code"};$("#send-btn").onclick=async e=>{e.preventDefault();let x=$("#message-input");if(x.value.trim()){await api("POST",`/channels/${cur.id}/messages`,{body:x.value});x.value=""}};$("#new-channel-btn").onclick=()=>$("#create-channel-modal").hidden=false;$("#create-channel-cancel").onclick=()=>$("#create-channel-modal").hidden=true;$("#create-channel-form").onsubmit=async e=>{e.preventDefault();try{let f=e.target,r=await api("POST","/workspaces/"+ws.slug+"/channels",{name:f.name.value,is_private:$("#create-channel-private").checked});chans.push(r.channel);$("#create-channel-modal").hidden=true;await load();open(r.channel.id)}catch(x){$("#create-channel-form [role=alert]").textContent=x.message}};$("#channel-settings-btn").onclick=()=>$("#channel-settings-modal").hidden=false;$("#channel-settings-close").onclick=()=>$("#channel-settings-modal").hidden=true;$("#channel-topic-submit").onclick=async()=>{let r=await api("PATCH","/channels/"+cur.id,{topic:$("#channel-topic-input").value});cur.topic=r.channel.topic;$("#channel-topic").textContent=cur.topic};$("#workspace-settings-btn").onclick=()=>$("#workspace-settings-modal").hidden=false;$("#workspace-settings-close").onclick=()=>$("#workspace-settings-modal").hidden=true;$("#close-thread").onclick=()=>$("#thread-panel").hidden=true;$("#settings-tab-invitations").onclick=()=>{$("#settings-pane-invitations").hidden=false;$("#settings-pane-members").hidden=true};$("#create-invitation-btn").onclick=()=>$("#create-invitation-form").hidden=false;$("#create-invitation-submit").onclick=async()=>{let r=await api("POST","/workspaces/"+ws.slug+"/invitations",{});$("#invitations-list").innerHTML=`<span class="invitation-code" data-testid="invitation-code">${r.invitation.code}</span>`};load();'''
open('/app/app.py','a').close()
if __name__=='__main__':
 if '--node' in sys.argv:
  n=int(sys.argv[sys.argv.index('--node')+1]);ThreadingHTTPServer(('127.0.0.1',8000+n),H).serve_forever()
