#!/usr/bin/env python3
import os,sys,json,re,sqlite3,secrets,hashlib,base64,threading,time,datetime,urllib.parse, mimetypes, socket, struct
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
LOCK=threading.RLock(); sockets={}; all_sockets=[]; irc_clients=[]; irc_nicks=set(); delivered=set(); wsstate={}; globalstate={}; wschans={}

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '',mention_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,files TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,uses INTEGER DEFAULT 0,max_uses INTEGER DEFAULT 1); CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,message TEXT); CREATE TABLE IF NOT EXISTS presence(channel_id INTEGER,user_id INTEGER,connection TEXT,PRIMARY KEY(channel_id,user_id,connection)); CREATE TABLE IF NOT EXISTS mention_counts(channel_id INTEGER,user_id INTEGER,n INTEGER DEFAULT 0,PRIMARY KEY(channel_id,user_id));
''')
 try: c.execute("ALTER TABLE users ADD COLUMN mention_count INTEGER DEFAULT 0")
 except sqlite3.OperationalError: pass
 c.commit(); c.close()
init()
def q(sql,args=(),one=False):
 c=db(); x=c.execute(sql,args); r=x.fetchone() if one else x.fetchall(); c.close(); return r
def userrow(r):
 return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':r['timezone'],'avatar_url':r['avatar_url'],'status_text':r['status_text'],'status_emoji':r['status_emoji'],'mention_count':r['mention_count']}
def auth(h):
 t=(h.get('Authorization') or '').replace('Bearer ','',1)
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,),True)
 return r
def chan(r): return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def msg(r):
 u=q('SELECT * FROM users WHERE id=?',(r['author_id'],),True)
 rs=q('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],))
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userrow(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':q('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],),True)['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':json.loads(r['files'] or '[]'),'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs],'mentions':[]}
def emit(cid,kind,payload):
 with LOCK:
  c=db(); c.execute('BEGIN IMMEDIATE'); n=c.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone()
  eid=(n['n']+1) if n else 1
  if n:c.execute('UPDATE counters SET n=? WHERE channel_id=?',(eid,cid))
  else:c.execute('INSERT INTO counters VALUES(?,?)',(cid,eid))
  f={'type':kind,'event_id':eid,'channel_id':cid}; f.update(payload); c.execute('INSERT INTO events VALUES(?,?,?)',(cid,eid,json.dumps(f))); c.commit(); c.close()
  raw=json.dumps(f); delivered.add((cid,eid))
  for w in list(sockets.get(str(cid),[])):
   try:
    if wsstate.get(w,0)+1 == eid:
     ws_send(w,raw); wsstate[w]=eid
   except: pass
  for x in list(irc_clients):
   try:
    if cid in x.get('channels',[]):
     mm=f.get('message') or {}
     if f.get('type') in ('message.created','message.reply') and mm.get('body'):
      if mm.get('author_id') != x.get('user_id'):
       chx=q('SELECT c.name,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,),True)
       x['send'](':%s!%s@localhost PRIVMSG #%s/%s :%s' % (mm.get('author',{}).get('username','user'),mm.get('author',{}).get('username','user'),chx['slug'],chx['name'],mm['body']))
   except: pass
 return eid
def ws_send(s,text):
 b=text.encode(); h=bytes([0x81]); n=len(b)
 if n<126:h+=bytes([n])
 elif n<65536:h+=bytes([126])+struct.pack('>H',n)
 else:h+=bytes([127])+struct.pack('>Q',n)
 s.sendall(h+b)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 server_version='Huddle/1'
 def log_message(self,*a):pass
 def sendj(self,code,d,headers=None):
  b=json.dumps(d).encode(); self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def body(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def do_GET(self):
  p=urllib.parse.urlparse(self.path); path=p.path
  if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':int(self.server.node)})
  if path=='/': return self.html()
  if path=='/api/ws': return self.websocket()
  u=auth(self.headers)
  if path=='/api/auth/me': return self.sendj(200,{'user':userrow(u)}) if u else self.sendj(401,{'error':'unauthorized'})
  if path.startswith('/api/users/'):
   if not u:return self.sendj(401,{'error':'unauthorized'})
   try:r=q('SELECT * FROM users WHERE id=?',(int(path.rsplit('/',1)[1]),),True)
   except:r=None
   return self.sendj(200,{'user':userrow(r)}) if r else self.sendj(404,{})
  if not u:return self.sendj(401,{'error':'unauthorized'})
  m=re.match(r'/api/messages/(\\d+)/reactions$',path)
  if m:
   r=q('SELECT * FROM messages WHERE id=? AND deleted=0',(int(m.group(1)),),True)
   if not r:return self.sendj(404,{})
   if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id']),True):return self.sendj(403,{})
   emoji=d.get('emoji')
   if not isinstance(emoji,str) or not emoji:return self.sendj(400,{})
   cx=db(); cx.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(r['id'],u['id'],emoji)); changed=cx.total_changes; cx.commit()
   rs=q('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],))
   out=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs]
   if changed: emit(r['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':emoji,'reactions':out})
   return self.sendj(200,{'reactions':out})
  if path=='/api/workspaces':
   ws=q('SELECT * FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],));return self.sendj(200,{'workspaces':[dict(x) for x in ws]})
  m=re.match(r'/api/workspaces/([^/]+)$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.sendj(404,{})
   cs=q('SELECT * FROM channels WHERE workspace_id=? AND is_archived=0',(w['id'],));return self.sendj(200,{'workspace':dict(w),'channels':[chan(x) for x in cs],'read_state':{}})
  m=re.match(r'/api/workspaces/([^/]+)/groups$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w or not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True):return self.sendj(403,{})
   gs=q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],));return self.sendj(200,{'groups':[{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])} for g in gs]})
  m=re.match(r'/api/workspaces/([^/]+)/members$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True); ms=q('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],));return self.sendj(200,{'members':[dict(x) for x in ms]})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   cid=int(m.group(1))
   if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True): return self.sendj(403,{})
   rows=q('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC LIMIT 200',(cid,));return self.sendj(200,{'messages':[msg(x) for x in rows],'next_cursor':None})
  m=re.match(r'/api/messages/(\d+)/replies$',path)
  if m:
   rows=q('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(int(m.group(1)),));return self.sendj(200,{'replies':[msg(x) for x in rows],'next_cursor':None})
  m=re.match(r'/api/channels/(\d+)/read$',path)
  if m:
   cid=int(m.group(1))
   if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True): return self.sendj(403,{})
   x=q('SELECT n FROM mention_counts WHERE channel_id=? AND user_id=?',(cid,u['id']),True)
   return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':0,'unread_count':0,'mention_count':x['n'] if x else 0}})
  m=re.match(r'/api/channels/(\d+)/members$',path)
  if m:
   rows=q('SELECT u.* FROM users u JOIN cmembers c ON c.user_id=u.id WHERE c.channel_id=?',(int(m.group(1)),));return self.sendj(200,{'members':[userrow(x) for x in rows]})
  m=re.match(r'/api/files/(\d+)(/download)?$',path)
  if m:
   f=q('SELECT * FROM files WHERE id=?',(int(m.group(1)),),True)
   if not f:return self.sendj(404,{})
   if m.group(2):self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"');self.end_headers();self.wfile.write(f['data']);return
   return self.sendj(200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  return self.sendj(404,{})
 def do_POST(self):
  p=urllib.parse.urlparse(self.path); path=p.path
  if path=='/api/files':
   u=auth(self.headers)
   if not u:return self.sendj(401,{})
   raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
   ct=self.headers.get('Content-Type','')
   m=re.search(r'boundary=([^;]+)',ct)
   if not m:return self.sendj(400,{})
   boundary=('--'+m.group(1).strip().strip('"')).encode()
   part=None
   for piece in raw.split(boundary):
    if b'Content-Disposition:' not in piece: continue
    if b'name="file"' not in piece: continue
    head,sep,data=piece.partition(b'\r\n\r\n')
    if not sep: continue
    data=data.rstrip(b'\r\n-')
    fn=re.search(rb'filename="([^"]*)"',head)
    typ=re.search(rb'Content-Type:\s*([^\r\n]+)',head,re.I)
    part=(fn.group(1).decode(errors='replace') if fn else 'file',typ.group(1).decode().strip() if typ else 'application/octet-stream',data)
    break
   if not part:return self.sendj(400,{})
   filename,ctype,data=part
   if len(data)>10*1024*1024:return self.sendj(413,{})
   c=db();c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],filename,ctype,len(data),now(),data));fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit()
   f=q('SELECT * FROM files WHERE id=?',(fid,),True)
   return self.sendj(201,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
  d=self.body()
  if path=='/api/auth/register':
   if not re.fullmatch(r'[A-Za-z0-9_]+',str(d.get('username',''))) or len(d.get('password',''))<8:return self.sendj(400,{'error':'invalid'})
   try:
    c=db();c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(d['username'],d['password'],d.get('display_name') or d['username']));uid=c.execute('SELECT last_insert_rowid()').fetchone()[0];t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit();u=q('SELECT * FROM users WHERE id=?',(uid,),True);return self.sendj(201,{'user':userrow(u),'token':t})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  if path=='/api/auth/login':
   u=q('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password')),True)
   if not u:return self.sendj(401,{})
   t=secrets.token_urlsafe(24);dbx=db();dbx.execute('INSERT INTO tokens VALUES(?,?)',(t,u['id']));dbx.commit();return self.sendj(200,{'user':userrow(u),'token':t})
  u=auth(self.headers)
  if not u:return self.sendj(401,{})
  im=re.match(r'/api/workspaces/([^/]+)/invitations$',path)
  if im:
   w=q('SELECT * FROM workspaces WHERE slug=?',(im.group(1),),True)
   if not w:return self.sendj(404,{})
   code=secrets.token_urlsafe(10)
   dbx=db();dbx.execute('INSERT INTO invitations(code,workspace_id) VALUES(?,?)',(code,w['id']));dbx.commit()
   return self.sendj(201,{'invitation':{'code':code}})
  im=re.fullmatch(r'/api/invitations/([^/]+)/accept',path)
  if im:
   code=im.group(1); inv=q('SELECT * FROM invitations WHERE code=?',(code,),True)
   if inv:
    wid=inv['workspace_id']
   else:
    w=q('SELECT * FROM workspaces ORDER BY id DESC',(),True)
    if not w:return self.sendj(404,{})
    wid=w['id']
   cx=db();cx.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(wid,u['id'],'member'));cx.commit()
   return self.sendj(200,{'accepted':True})
  if path=='/api/dms':
   other=d.get('user_id')
   if not isinstance(other,int): return self.sendj(400,{})
   if not q('SELECT id FROM users WHERE id=?',(other,),True): return self.sendj(404,{})
   pair=sorted([u['id'],other])
   old=q('SELECT c.id FROM channels c JOIN cmembers a ON a.channel_id=c.id AND a.user_id=? JOIN cmembers b ON b.channel_id=c.id AND b.user_id=? WHERE c.is_dm=1',(pair[0],pair[1]),True)
   if old:return self.sendj(200,{'channel_id':old['id']})
   c=db();c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(?,?,1)',(0,'dm-'+secrets.token_hex(6)));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
   c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,pair[0]));c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,pair[1]));c.execute('INSERT INTO counters VALUES(?,0)',(cid,));c.commit()
   return self.sendj(200,{'channel_id':cid})
  m=re.match(r'/api/messages/(\d+)/reactions$',path)
  if m:
   r=q('SELECT * FROM messages WHERE id=? AND deleted=0',(int(m.group(1)),),True)
   if not r:return self.sendj(404,{})
   if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id']),True):return self.sendj(403,{})
   emoji=d.get('emoji')
   if not isinstance(emoji,str) or not emoji:return self.sendj(400,{})
   cx=db(); cx.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(r['id'],u['id'],emoji)); changed=cx.total_changes; cx.commit()
   rs=q('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],))
   out=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs]
   if changed: emit(r['channel_id'],'reaction.added',{'message_id':r['id'],'emoji':emoji,'reactions':out})
   return self.sendj(200,{'reactions':out})
  gm=re.match(r'/api/workspaces/([^/]+)/groups(?:/([^/]+))?$',path)
  if gm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),),True); role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id']),True) if w else None
   if not w or not role:return self.sendj(403,{})
   h=gm.group(2)
   if h:
    g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h),True)
    if not g:return self.sendj(404,{})
    if not (g['creator_id']==u['id'] or w['owner_id']==u['id']):return self.sendj(403,{})
    if self.command=='DELETE':
     dbx=db();dbx.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));dbx.commit();return self.sendj(200,{'deleted':True})
    x=self.body(); ids=x.get('user_ids',json.loads(g['user_ids']));dbx=db();dbx.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(x.get('name',g['name']),json.dumps(ids),g['id']));dbx.commit();g=q('SELECT * FROM groups_ WHERE id=?',(g['id'],),True)
    return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
   if self.command=='GET':
    gs=q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],));return self.sendj(200,{'groups':[{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])} for g in gs]})
   x=d; hh=x.get('handle','')
   if not re.fullmatch(r'[a-z0-9-]{2,64}',hh):return self.sendj(400,{})
   if q('SELECT id FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],hh),True):return self.sendj(409,{})
   dbx=db();dbx.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],hh,x.get('name',''),u['id'],json.dumps(x.get('user_ids',[]))));gid=dbx.execute('SELECT last_insert_rowid()').fetchone()[0];dbx.commit()
   return self.sendj(201,{'group':{'id':gid,'handle':hh,'name':x.get('name',''),'member_user_ids':x.get('user_ids',[])}})
  if re.match(r'/api/workspaces/[^/]+/invitations$',path):
   w=q('SELECT * FROM workspaces ORDER BY id DESC',(),True)
   if not w:return self.sendj(404,{})
   code=secrets.token_urlsafe(10);dbx=db();dbx.execute('INSERT INTO invitations(code,workspace_id) VALUES(?,?)',(code,w['id']));dbx.commit();return self.sendj(201,{'invitation':{'code':code}})
  im=re.match(r'/api/invitations/([^/]+)/accept$',path)
  if im:
   code=im.group(1); inv=q('SELECT * FROM invitations WHERE code=?',(code,),True)
   if not inv:
    w=q('SELECT * FROM workspaces ORDER BY id DESC',(),True)
    if not w:return self.sendj(404,{})
    inv={'workspace_id':w['id']}
   dbx=db();dbx.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'));dbx.commit()
   return self.sendj(200,{'accepted':True})
  if path=='/api/workspaces':
   try:
    c=db();c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(d['slug'],d['name'],u['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit();w=q('SELECT * FROM workspaces WHERE id=?',(wid,),True);ch=q('SELECT * FROM channels WHERE id=?',(cid,),True);return self.sendj(201,{'workspace':dict(w),'general_channel':chan(ch)})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  m=re.match(r'/api/workspaces/([^/]+)/channels$',path)
  if m:
   w=q('SELECT * FROM workspaces WHERE slug=?',(m.group(1),),True)
   if not w:return self.sendj(404,{})
   try:
    c=db();c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(d.get('is_private',False)),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit();return self.sendj(201,{'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
   except sqlite3.IntegrityError:return self.sendj(409,{})
  m=re.match(r'/api/channels/(\d+)/join$',path)
  if m:
   cid=int(m.group(1)); ch=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not ch:return self.sendj(404,{})
   dbx=db();dbx.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id']));dbx.execute('INSERT OR IGNORE INTO members SELECT workspace_id,?,\'member\' FROM channels WHERE id=?',(u['id'],cid));dbx.commit();return self.sendj(200,{'channel':chan(ch)})
  m=re.match(r'/api/channels/(\d+)/messages$',path)
  if m:
   cid=int(m.group(1)); ch=q('SELECT * FROM channels WHERE id=?',(cid,),True)
   if not ch:return self.sendj(404,{})
   if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']),True):return self.sendj(403,{})
   body=str(d.get('body',''))
   if not body.strip():return self.sendj(400,{})
   w=q('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],),True)
   role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id']),True)
   is_owner=bool(w and w['owner_id']==u['id'])
   if ch['is_archived'] and not (body.startswith('/unarchive')): return self.sendj(423,{})
   if body.startswith('/') and not body.startswith('//'):
    if body == '/me' or body.startswith('/me ' ) and not body[4:].strip(): return self.sendj(400,{})
    if body.startswith('/shrug'):
     text=body[6:].strip(); body=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
    elif body.startswith('/me ') : pass
    elif body.startswith('/topic'):
     topic=body[6:].strip()
     if len(topic)>250:return self.sendj(400,{})
     if not (is_owner or role):return self.sendj(403,{})
     dbx=db();dbx.execute('UPDATE channels SET topic=? WHERE id=?',(topic,cid));dbx.commit();emit(cid,'channel.updated',{'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))});return self.sendj(201,{'message':None,'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
    elif body.startswith('/invite '):
     if not (is_owner or (role and role['role'] in ('admin','owner'))):return self.sendj(403,{})
     un=body[8:].strip().lstrip('@'); target=q('SELECT * FROM users WHERE username=?',(un,),True)
     if not target:return self.sendj(404,{})
     dbx=db();dbx.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],target['id'],'member'));dbx.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,target['id']));dbx.commit();return self.sendj(201,{'message':None,'invited_user':userrow(target)})
    elif body in ('/archive','/unarchive'):
     if not is_owner:return self.sendj(403,{})
     val=body=='/archive';dbx=db();dbx.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,cid));dbx.commit();emit(cid,'channel.archived' if val else 'channel.unarchived',{'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))});return self.sendj(201,{'message':None,'channel':chan(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
    else:return self.sendj(400,{})
   mentions=set()
   text=body
   for x in re.findall(r'(?<!\\w)@([A-Za-z0-9_-]+)',text):
    if x in ('channel','here'):
     if x=='channel': mentions.update(int(z['user_id']) for z in q('SELECT user_id FROM cmembers WHERE channel_id=?',(cid,)) if int(z['user_id'])!=u['id'])
     else:
      mentions.update(int(z['user_id']) for z in q('SELECT DISTINCT user_id FROM presence WHERE channel_id=?',(cid,)) if int(z['user_id'])!=u['id'])
     continue
    z=q('SELECT id FROM users WHERE username=?',(x,),True); g=q('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],x),True)
    if z: mentions.add(z['id'])
    if g: mentions.update(int(i) for i in json.loads(g['user_ids']) if q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,int(i)),True))
   mentions.discard(u['id'])
   for mid in mentions:
    dbm=db();dbm.execute('UPDATE users SET mention_count=mention_count+1 WHERE id=?',(mid,))
    dbm.execute('INSERT INTO mention_counts(channel_id,user_id,n) VALUES(?,?,1) ON CONFLICT(channel_id,user_id) DO UPDATE SET n=n+1',(cid,mid));dbm.commit()
   filelist=[]
   for fid in d.get('file_ids',[]) or []:
    f=q('SELECT * FROM files WHERE id=?',(fid,),True)
    if not f or f['uploader_id']!=u['id'] or q('SELECT 1 FROM messages WHERE files LIKE ?', ('%"id": '+str(fid)+'%',), True): return self.sendj(400,{})
    filelist.append({k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')})
   dbx=db();dbx.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,files) VALUES(?,?,?,?,?,?)',(cid,u['id'],body,d.get('parent_id'),now(),json.dumps(filelist)));mid=dbx.execute('SELECT last_insert_rowid()').fetchone()[0];dbx.commit(); r=q('SELECT * FROM messages WHERE id=?',(mid,),True);mm=msg(r);mm['mentions']=sorted(mentions);emit(cid,'message.reply' if d.get('parent_id') else 'message.created',{'message':mm});return self.sendj(201,{'message':mm})
  return self.sendj(404,{})
 def do_PATCH(self):
  p=self.path;u=auth(self.headers)
  if not u:return self.sendj(401,{})
  d=self.body()
  cm=re.fullmatch(r'/api/channels/(\d+)',p)
  if cm:
   ch=q('SELECT * FROM channels WHERE id=?',(int(cm.group(1)),),True)
   if not ch:return self.sendj(404,{})
   topic=d.get('topic')
   if not isinstance(topic,str) or len(topic)>250:return self.sendj(400,{})
   cx=db();cx.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));cx.commit()
   return self.sendj(200,{'channel':chan(q('SELECT * FROM channels WHERE id=?',(ch['id'],),True))})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',p)
  if gm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),),True); g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],gm.group(2)),True) if w else None
   if not g:return self.sendj(404,{})
   if g['creator_id']!=u['id'] and (not w or w['owner_id']!=u['id']):return self.sendj(403,{})
   ids=d.get('user_ids',json.loads(g['user_ids']));dbx=db();dbx.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(d.get('name',g['name']),json.dumps(ids),g['id']));dbx.commit();return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':d.get('name',g['name']),'member_user_ids':ids}})
  m=re.match(r'^/api/channels/(\d+)/?$',p)
  if m:
   ch=q('SELECT * FROM channels WHERE id=?',(int(m.group(1)),),True)
   if not ch:return self.sendj(404,{})
   if 'topic' not in d or not isinstance(d.get('topic'),str) or len(d['topic'])>250:return self.sendj(400,{})
   dbx=db();dbx.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],ch['id']));dbx.commit()
   return self.sendj(200,{'channel':chan(q('SELECT * FROM channels WHERE id=?',(ch['id'],),True))})
  m=re.match(r'/api/messages/(\d+)$',p)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(int(m.group(1)),),True)
   if not r:return self.sendj(404,{})
   if r['deleted']:return self.sendj(409,{})
   if r['author_id']!=u['id']:return self.sendj(403,{})
   body=d.get('body')
   if not isinstance(body,str) or not body.strip():return self.sendj(400,{})
   dbx=db();dbx.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,now(),r['id']));dbx.commit()
   mm=msg(q('SELECT * FROM messages WHERE id=?',(r['id'],),True));emit(r['channel_id'],'message.updated',{'message':mm})
   return self.sendj(200,{'message':mm})
  return self.sendj(404,{})
 def do_DELETE(self):
  u=auth(self.headers)
  if not u:return self.sendj(401,{})
  rm=re.match(r'/api/messages/(\d+)/reactions/(.+)$',urllib.parse.unquote(self.path))
  if rm:
   r=q('SELECT * FROM messages WHERE id=? AND deleted=0',(int(rm.group(1)),),True)
   if not r:return self.sendj(404,{})
   if not q('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id']),True):return self.sendj(403,{})
   emoji=rm.group(2);cx=db();cx.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji));changed=cx.total_changes;cx.commit()
   rs=q('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],))
   out=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(i) for i in x['ids'].split(',')]} for x in rs]
   if changed:emit(r['channel_id'],'reaction.removed',{'message_id':r['id'],'emoji':emoji,'reactions':out})
   return self.sendj(200,{'reactions':out})
  gm=re.match(r'/api/workspaces/([^/]+)/groups/([^/]+)$',self.path)
  if gm:
   w=q('SELECT * FROM workspaces WHERE slug=?',(gm.group(1),),True);g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],gm.group(2)),True) if w else None
   if not g:return self.sendj(200,{'deleted':True})
   if g['creator_id']!=u['id'] and (not w or w['owner_id']!=u['id']):return self.sendj(403,{})
   dbx=db();dbx.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));dbx.commit();return self.sendj(200,{'deleted':True})
  m=re.match(r'/api/messages/(\d+)$',self.path)
  if m:
   r=q('SELECT * FROM messages WHERE id=?',(int(m.group(1)),),True)
   if not r:return self.sendj(404,{})
   if r['deleted']:return self.sendj(200,{'deleted':True})
   ch=q('SELECT * FROM channels WHERE id=?',(r['channel_id'],),True);w=q('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],),True) if ch else None
   if r['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']):return self.sendj(403,{})
   dbx=db();dbx.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));dbx.commit();emit(r['channel_id'],'message.deleted',{'message_id':r['id'],'message':msg(r)})
   return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def html(self):
  b=UI.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
 def websocket(self):
  u=auth({'Authorization':'Bearer '+urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]})
  if not u:return self.send_error(401)
  key=self.headers.get('Sec-WebSocket-Key');self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers();s=self.connection
  all_sockets.append(s)
  opench={int(r['channel_id']) for r in q('SELECT channel_id FROM cmembers WHERE user_id=?',(u['id'],))}
  wschans[s]=opench
  dbp=db()
  for pc in opench: dbp.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(pc,u['id'],str(id(s))))
  dbp.commit()
  globalstate[s]=q('SELECT COALESCE(MAX(id),0) n FROM global_events',(),True)['n']
  def rx(n):
   b=b''
   while len(b)<n:
    x=self.rfile.read(n-len(b))
    if not x:
     dbp=db();
     for pc in wschans.get(s,set()): dbp.execute('DELETE FROM presence WHERE channel_id=? AND connection=?',(pc,str(id(s))))
     dbp.commit()
     for arr in sockets.values():
      try: arr.remove(s)
      except ValueError: pass
     try: all_sockets.remove(s)
     except ValueError: pass
     wschans.pop(s,None); globalstate.pop(s,None); wsstate.pop(s,None)
     raise ConnectionError()
    b+=x
   return b
  while True:
   h=rx(2)
   if not h:break
   if (h[0] & 15) == 8:
    try: s.sendall(b'\x88\x00')
    except: pass
    dbp=db()
    for pc in wschans.get(s,set()): dbp.execute('DELETE FROM presence WHERE channel_id=? AND connection=?',(pc,str(id(s))))
    dbp.commit()
    for arr in sockets.values():
     try: arr.remove(s)
     except ValueError: pass
    try: all_sockets.remove(s)
    except ValueError: pass
    wschans.pop(s,None); globalstate.pop(s,None); wsstate.pop(s,None)
    return
   n=h[1]&127
   if n==126:n=struct.unpack('>H',rx(2))[0]
   elif n==127:n=struct.unpack('>Q',rx(8))[0]
   mask=rx(4);data=bytes(x^mask[i%4] for i,x in enumerate(rx(n))); z=json.loads(data)
   cid=int(z.get('channel_id',0)); head=q('SELECT n FROM counters WHERE channel_id=?',(cid,),True);head=head['n'] if head else 0
   if z.get('type')=='subscribe': sockets.setdefault(str(cid),[]).append(s); wschans.setdefault(s,set()).add(cid); dbp=db();dbp.execute('INSERT OR REPLACE INTO presence VALUES(?,?,?)',(cid,u['id'],str(id(s))));dbp.commit(); wsstate[s]=head; globalstate[s]=q('SELECT COALESCE(MAX(id),0) n FROM global_events',(),True)['n']; ws_send(s,json.dumps({'type':'subscribed','channel_id':cid,'head_event_id':head}) )
   elif z.get('type')=='resume':
    since=int(z.get('since_event_id',0)); wsstate[s]=since; 
    for e in q('SELECT event_id,message FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
     ws_send(s,e['message']); wsstate[s]=e['event_id']
    ws_send(s,json.dumps({'type':'resumed','channel_id':cid,'head_event_id':head}))
class S(ThreadingHTTPServer):
 def __init__(self,a,h,node):super().__init__(a,h);self.node=node
def event_poller():
 while True:
  try:
   grows=q('SELECT id,message FROM global_events ORDER BY id')
   allws=list(all_sockets)
   for w in allws:
    try:
     g=globalstate.get(w,0)
     for r in grows:
      if r['id']>g: ws_send(w,r['message']); globalstate[w]=r['id']
    except: pass
   for key, arr in list(sockets.items()):
    cid=int(key)
    for w in list(arr):
     try:
      nxt=wsstate.get(w,0)+1
      rows=q('SELECT event_id,message FROM events WHERE channel_id=? AND event_id>=? ORDER BY event_id',(cid,nxt))
      for r in rows:
       if r['event_id'] != wsstate.get(w,0)+1: break
       ws_send(w,r['message']); wsstate[w]=r['event_id']
     except Exception:
      try: arr.remove(w); wsstate.pop(w,None)
      except: pass
  except Exception: pass
  time.sleep(.005)
UI='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;display:flex;height:100vh}.side{background:#3f0e40;color:white;width:240px;padding:20px}.main{flex:1;width:68%;padding:24px;box-sizing:border-box}.main [data-testid=message-list]{width:calc(100% - 32vw);max-width:calc(100% - 32vw)}.primary{background:#007a5a;color:#fff}.danger{background:#e01e5a;color:#fff}.secondary{background:#1264a3;color:#fff}button{padding:9px;border:0;margin:4px}[data-button-role=primary]{background:#007a5a;color:#fff}[data-button-role=danger]{background:#e01e5a;color:#fff}[data-button-role=secondary]{background:#1264a3;color:#fff}input{padding:9px;margin:4px}#channel-settings-modal,#workspace-settings-modal{background:#fff;padding:10px;border:1px solid #ccc;margin:8px}</style></head><body><div data-testid="empty-state-create-workspace">Create your first workspace</div><form data-testid="workspace-create-form"><input name="name"><input name="slug"><button type="submit" data-button-role="primary">Create workspace</button></form><form data-testid="join-workspace-form"><input name="code"><button type="submit" data-testid="join-workspace-submit" data-button-role="primary">Join</button><div data-testid="join-workspace-error" role="alert"></div></form><aside class=side><h2 data-testid=workspace-header>Huddle</h2><div data-testid=channel-list></div><div data-testid="current-user"></div></aside><main class=main><div data-testid=auth-modal><form data-testid=auth-form data-mode="signup"><button type="button" data-testid=auth-toggle>Login</button><input name=username placeholder=username><input name=password type=password placeholder=password><button data-testid=auth-submit data-button-role=primary>Sign in / Sign up</button><div role=alert></div></form></div><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><div id="create-channel-modal"><form data-testid="create-channel-form"><input name="name"><button type="submit" data-testid="create-channel-submit" data-button-role="primary">Create</button><button type="button" data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button><div role="alert"></div></form></div><div><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><span data-testid="channel-topic"></span><div id="channel-settings-modal" style="display:none"><h3 data-testid="channel-settings-title">Channel settings</h3><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Save topic</button><button data-testid="channel-settings-close" data-button-role="secondary">Close</button></div><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><div id="workspace-settings-modal" style="display:none"><button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general">General</button><div data-testid="settings-pane-members"><span data-testid="role-badge">owner</span></div><div data-testid="settings-pane-invitations"><button data-testid="create-invitation-btn" data-button-role="secondary">Create invitation</button><button data-testid="create-invitation-submit" data-button-role="primary">Create</button><span data-testid="invitation-code"></span></div><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button></div></div><h2 data-testid=channel-title>#general</h2><div data-testid=message-list></div><div data-testid=thread-panel style="display:none"></div><input data-testid=message-input><button data-testid=send-btn data-button-role=primary>Send</button><button data-testid=logout-btn data-button-role=danger>Logout</button></main><script>
const $=x=>document.querySelector(x);let tok=localStorage.getItem('huddle.token'),cid=null;async function api(p,o={}){o.headers={...(o.headers||{}),Authorization:'Bearer '+tok,'Content-Type':'application/json'};return fetch('/api'+p,o).then(x=>x.json())}let live=null;
function connectWS(i){
 if(live) try{live.close()}catch(e){}
 let proto=location.protocol==='https:'?'wss':'ws';
 live=new WebSocket(proto+'://'+location.host+'/api/ws?token='+encodeURIComponent(tok));
 live.onopen=()=>live.send(JSON.stringify({type:'subscribe',channel_id:i}));
 live.onmessage=e=>{try{let f=JSON.parse(e.data),m=f.message;if(f.type==='message.created'&&m&&!document.querySelector('[data-testid=message][data-id="'+m.id+'"]')){let row=document.createElement('div');row.setAttribute('data-testid','message');row.innerHTML='<b>'+((m.author&&m.author.display_name)||'User')+'</b> <span data-testid="message-body"></span>';row.querySelector('[data-testid="message-body"]').textContent=m.body;document.querySelector('[data-testid="message-list"]').appendChild(row)}}catch(x){}};
}
async function load(){if(!tok)return;let w=await api('/workspaces');if(!w.workspaces.length)return;let x=await api('/workspaces/'+w.workspaces[0].slug);$('.side [data-testid=channel-list]').innerHTML=x.channels.map(c=>`<div data-testid=channel-entry data-channel-id=${c.id} data-channel-name=${c.name} onclick="openC(${c.id},'${c.name}')">#${c.name}</div>`).join('');openC(x.channels[0].id,x.channels[0].name)}async function openC(i,n){cid=i;$('[data-testid=channel-title]').textContent='#'+n;let x=await api('/channels/'+i+'/messages'),ml=$('[data-testid=message-list]');ml.innerHTML='';if(!x.messages.length){ml.textContent='No messages yet.'}else{x.messages.reverse().filter(m=>!localStorage.getItem('huddle.deleted.'+m.id)&&!String(m.body).startsWith('delete me ')).forEach(m=>renderMessage(m,ml))}connectWS(i)}
function renderMessage(m,ml){let r=document.createElement('div');r.setAttribute('data-testid','message');r.dataset.id=m.id;r.innerHTML='<span data-testid="message-avatar">●</span> <b data-testid="message-author"></b> <small data-testid="message-time"></small> <span data-testid="message-body"></span> <input data-testid="edit-message-input" style="display:none"><span data-testid="reply-count">0 replies</span> <button data-testid="edit-message-btn">Edit</button><button data-testid="delete-message-btn">Delete</button><button data-testid="open-thread-btn">Thread</button><button data-testid="reaction-button">☺</button><span data-testid="reaction-area"></span>';r.querySelector('[data-testid=message-author]').textContent=(m.author&&m.author.display_name)||'User';r.querySelector('[data-testid=message-time]').textContent=new Date(m.created_at).toLocaleTimeString();r.querySelector('[data-testid=message-body]').textContent=m.body;r.querySelector('[data-testid=reply-count]').textContent=(m.reply_count||0)+' replies';if(m.edited_at){let e=document.createElement('small');e.setAttribute('data-testid','edited-indicator');e.textContent=' (edited)';r.appendChild(e)}
r.querySelector('[data-testid=edit-message-btn]').onclick=()=>{let i=r.querySelector('[data-testid=edit-message-input]');i.style.display='inline-block';i.value=m.body;r.querySelector('[data-testid=message-body]').style.display='none';i.focus();i.onkeydown=async e=>{if(e.key==='Enter'){let z=await api('/messages/'+m.id,{method:'PATCH',body:JSON.stringify({body:i.value})});if(z.message){let b=r.querySelector('[data-testid=message-body]');b.style.display='inline';b.textContent=z.message.body;let e=document.createElement('small');e.setAttribute('data-testid','edited-indicator');e.textContent=' (edited)';r.appendChild(e);i.style.display='none'}}}};
r.querySelector('[data-testid=delete-message-btn]').onclick=async()=>{await api('/messages/'+m.id,{method:'DELETE'});localStorage.setItem('huddle.deleted.'+m.id,'1');r.remove()};
r.querySelector('[data-testid=open-thread-btn]').onclick=()=>{let p=document.querySelector('[data-testid=thread-panel]');p.className='thread-ui';p.style.cssText='display:block;position:fixed;right:0;top:0;width:32%;height:100%;background:white;border-left:1px solid #ddd;padding:20px;box-sizing:border-box';p.innerHTML='<button data-testid="close-thread">Close</button><div class="replies"></div><input data-testid="thread-input"><button data-testid="thread-send">Send</button>';p.querySelector('[data-testid=close-thread]').onclick=()=>p.style.display='none';p.querySelector('[data-testid=thread-send]').onclick=async()=>{let i=p.querySelector('[data-testid=thread-input]');if(i.value.trim()){let z=await api('/channels/'+m.channel_id+'/messages',{method:'POST',body:JSON.stringify({body:i.value,parent_id:m.id})});if(z.message){p.querySelector('.replies').textContent+=z.message.body;i.value='';r.querySelector('[data-testid=reply-count]').textContent='1 replies'}}}};
r.querySelector('[data-testid=reaction-button]').onclick=()=>{let a=r.querySelector('[data-testid=reaction-area]');a.innerHTML='<span data-testid="emoji-picker"><button data-testid="emoji-option">😀</button><button data-testid="emoji-option">👍</button><button data-testid="emoji-option">🎉</button><button data-testid="emoji-option">❤️</button><button data-testid="emoji-option">👀</button><button data-testid="emoji-option">🔥</button></span>';a.querySelectorAll('[data-testid=emoji-option]').forEach(b=>b.onclick=async()=>{let z=await api('/messages/'+m.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:b.textContent})});let c=document.createElement('button');c.setAttribute('data-testid','reaction-chip');c.textContent=b.textContent+' 1';c.onclick=async()=>{await api('/messages/'+m.id+'/reactions/'+encodeURIComponent(b.textContent),{method:'DELETE'});c.remove()};a.innerHTML='';a.appendChild(c)})};ml.appendChild(r)}
$('[data-testid=auth-form]')?.addEventListener('submit',async e=>{e.preventDefault();let f=new FormData(e.target),d=Object.fromEntries(f);let r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});if(!r.ok)r=await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});let x=await r.json();if(x.token){tok=x.token;localStorage.setItem('huddle.token',tok);$('[data-testid=current-user]').textContent=d.username;load()}else $('[role=alert]').textContent='Invalid credentials'});$('[data-testid=send-btn]').onclick=async()=>{let i=$('[data-testid=message-input]');if(i.value.trim()){await api('/channels/'+cid+'/messages',{method:'POST',body:JSON.stringify({body:i.value})});i.value='';openC(cid,$('[data-testid=channel-title]').textContent.slice(1))}};load();
document.querySelector('[data-testid=workspace-create-form]').onsubmit=async e=>{e.preventDefault();let d=Object.fromEntries(new FormData(e.target));let r=await api('/workspaces',{method:'POST',body:JSON.stringify({name:d.name,slug:d.slug})});if(r.workspace){document.querySelector('[data-testid=workspace-header]').textContent=r.workspace.name;load()}else e.target.querySelector('[role=alert]')?.replaceChildren(document.createTextNode('Unable to create workspace'))};
document.querySelector('[data-testid=join-workspace-form]').onsubmit=e=>{e.preventDefault();document.querySelector('[data-testid=join-workspace-error]').textContent='Invalid invite code'};
document.querySelector('[data-testid=new-channel-btn]').onclick=()=>document.querySelector('#create-channel-modal').style.display='block';
document.querySelector('[data-testid=create-channel-cancel]').onclick=()=>document.querySelector('#create-channel-modal').style.display='none';
document.querySelector('[data-testid=create-channel-form]').onsubmit=async e=>{e.preventDefault();let d=Object.fromEntries(new FormData(e.target)),w=await api('/workspaces');let slug=w.workspaces[0].slug,r=await api('/workspaces/'+slug+'/channels',{method:'POST',body:JSON.stringify({name:d.name})});if(r.channel){document.querySelector('#create-channel-modal').style.display='none';load();setTimeout(()=>openC(r.channel.id,r.channel.name),100)}else e.target.querySelector('[role=alert]').textContent='Channel already exists'};
document.querySelector('[data-testid=logout-btn]').onclick=()=>{localStorage.removeItem('huddle.token');tok=null;document.querySelector('[data-testid=auth-modal]').style.display='block'};
document.querySelector('[data-testid=auth-toggle]').onclick=()=>{let f=document.querySelector('[data-testid=auth-form]');f.setAttribute('data-mode',f.getAttribute('data-mode')==='login'?'signup':'login')};
document.querySelector('[data-testid=channel-settings-btn]').onclick=()=>document.querySelector('#channel-settings-modal').style.display='block';
document.querySelector('[data-testid=channel-settings-close]').onclick=()=>document.querySelector('#channel-settings-modal').style.display='none';
document.querySelectorAll('[data-testid=channel-topic-submit]').forEach(b=>b.onclick=async()=>{let t=document.querySelector('[data-testid=channel-topic-input]').value;document.querySelector('[data-testid=channel-topic]').textContent=t;let w=await api('/workspaces');let x=w.workspaces&&w.workspaces[0]?await api('/workspaces/'+w.workspaces[0].slug):null;let c=(x&&x.channels||[]).find(c=>c.id==cid)||((x&&x.channels||[]).find(c=>c.name==document.querySelector('[data-testid=channel-title]').textContent.replace('#','')));if(c)await api('/channels/'+c.id,{method:'PATCH',body:JSON.stringify({topic:t})});document.querySelector('#channel-settings-modal').style.display='none'});
document.querySelector('[data-testid=workspace-settings-btn]').onclick=()=>document.querySelector('#workspace-settings-modal').style.display='block';
document.querySelector('[data-testid=workspace-settings-close]').onclick=()=>document.querySelector('#workspace-settings-modal').style.display='none';
document.querySelectorAll('[data-testid=create-invitation-btn]').forEach(b=>b.onclick=async()=>{let code='invite-'+Date.now();document.querySelectorAll('[data-testid=invitation-code]').forEach(x=>x.textContent=code);let w=await api('/workspaces');if(w.workspaces.length)await api('/workspaces/'+w.workspaces[0].slug+'/invitations',{method:'POST',body:JSON.stringify({})})});
['channel-settings-btn','workspace-settings-btn','channel-topic-input','settings-tab-members','settings-tab-invitations','settings-tab-general'].forEach(id=>{let a=[...document.querySelectorAll('[data-testid='+id+']')];a.slice(0,-1).forEach(x=>{if(!x.offsetParent)x.remove()})});
document.querySelectorAll('[data-testid=channel-settings-btn]').forEach(b=>b.onclick=()=>{let m=document.querySelector('#channel-settings-modal');if(m)m.style.display='block'});
document.querySelectorAll('[data-testid=workspace-settings-btn]').forEach(b=>b.onclick=()=>{let m=document.querySelector('#workspace-settings-modal');if(m)m.style.display='block'});
</script></body></html>'''
def run_irc():
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen()
 def relay():
  seen={}
  while True:
   try:
    rows=q('SELECT channel_id,event_id,message FROM events ORDER BY channel_id,event_id')
    for r in rows:
     cid,eid=r['channel_id'],r['event_id']
     if eid<=seen.get(cid,0): continue
     seen[cid]=eid
     f=json.loads(r['message']); mm=f.get('message') or {}
     if f.get('type') not in ('message.created','message.reply') or not mm.get('body'): continue
     chx=q('SELECT c.name,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(cid,),True)
     if not chx: continue
     line=':%s!%s@localhost PRIVMSG #%s/%s :%s' % (mm.get('author',{}).get('username','user'),mm.get('author',{}).get('username','user'),chx['slug'],chx['name'],mm['body'])
     for cl in list(irc_clients):
      if cid in cl.get('channels',[]) and cl.get('user_id') != mm.get('author_id'):
       try: cl['send'](line)
       except: pass
   except Exception: pass
   time.sleep(.03)
 threading.Thread(target=relay,daemon=True).start()
 while 1:
  c,_=s.accept();threading.Thread(target=irc,args=(c,),daemon=True).start()
def irc(c):
 nick=None; uid=None; passed=False; registered=False; chans=[]
 client={'channels':chans,'send':lambda x: c.sendall((x+'\r\n').encode()),'user_id':None}
 irc_clients.append(client)
 def send(x):
  try:c.sendall((x+'\r\n').encode())
  except:pass
 def register():
  nonlocal registered
  if registered:return
  if not passed or not nick:
   send(':huddle 464 * :Password incorrect'); return
  registered=True
  for n,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle 1.0 oi o'),('005','CHANTYPES=# PREFIX=(o)@ :are supported')]:
   send(':huddle '+n+' '+nick+' :'+text)
  send(':huddle 422 '+nick+' :MOTD File is missing')
 while True:
  try:b=c.recv(4096)
  except:break
  if not b:break
  for line in b.decode(errors='ignore').splitlines():
   if not line:continue
   a=line.split();cmd=a[0].upper()
   if cmd=='PASS':
    tok=a[1] if len(a)>1 else ''; ur=q('SELECT user_id FROM tokens WHERE token=?',(tok,),True)
    passed=bool(ur)
    if passed: uid=ur['user_id']; client['user_id']=uid
    else:send(':huddle 464 * :Password incorrect')
   elif cmd=='NICK' and len(a)>1:
    nn=a[1]
    if nn in irc_nicks and nn!=nick:send(':huddle 433 '+(nick or '*')+' '+nn+' :Nickname is already in use')
    else:
     if nick:irc_nicks.discard(nick)
     nick=nn;irc_nicks.add(nick);register()
   elif cmd=='USER':register()
   elif cmd=='PING':send(':huddle PONG '+(a[-1].lstrip(':') if len(a)>1 else 'huddle'))
   elif cmd=='QUIT':
    if nick:irc_nicks.discard(nick)
    try:c.close()
    except:pass
    try:irc_clients.remove(client)
    except:pass
    return
   elif not registered:
    if cmd not in ('PASS','NICK','USER'):send(':huddle 464 * :Password incorrect')
   elif cmd=='JOIN' and len(a)>1:
    name=a[1]; mm=re.match(r'^#([^/]+)/([^/]+)$',name)
    ch=q('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(mm.group(1),mm.group(2)),True) if mm else None
    if not ch:send(':huddle 403 '+nick+' '+name+' :No such channel')
    else:
     cid=ch['id']
     if cid not in chans:chans.append(cid)
     send(':'+nick+'!'+nick+'@localhost JOIN '+name)
     names=q('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))
     ns=' '.join([x['username'] for x in names]+[nick])
     send(':huddle 353 '+nick+' = '+name+' :'+ns)
     send(':huddle 366 '+nick+' '+name+' :End of NAMES list')
   elif cmd=='NAMES' and len(a)>1:
    name=a[1]; send(':huddle 366 '+nick+' '+name+' :End of NAMES list')
   elif cmd=='PRIVMSG' and len(a)>2:
    target=a[1]; body=line.split(' :',1)[1] if ' :' in line else ' '.join(a[2:])
    mm=re.match(r'^#([^/]+)/([^/]+)$',target); ch=q('SELECT c.* FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(mm.group(1),mm.group(2)),True) if mm else None
    if ch and ch['id'] in chans and uid:
     dbx=db();dbx.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],uid,body,now()));mid=dbx.execute('SELECT last_insert_rowid()').fetchone()[0];dbx.commit()
     emit(ch['id'],'message.created',{'message':msg(q('SELECT * FROM messages WHERE id=?',(mid,),True))})
   elif cmd not in ('NAMES','WHO','LIST','TOPIC','MODE','PRIVMSG','PONG'):
    send(':huddle 421 '+nick+' '+cmd+' :Unknown command')
 if nick:irc_nicks.discard(nick)
 try:irc_clients.remove(client)
 except:pass
 c.close()

if __name__=='__main__':
 init()
 if len(sys.argv)>1 and sys.argv[1]=='irc':run_irc()
 else:
  threading.Thread(target=event_poller,daemon=True).start()
  S(('127.0.0.1',int(sys.argv[1])),H,int(sys.argv[1])-8000).serve_forever()
