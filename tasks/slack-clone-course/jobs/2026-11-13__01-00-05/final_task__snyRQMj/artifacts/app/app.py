import os,sys,json,sqlite3,socket,secrets,re,hashlib,base64,struct,time,threading,urllib.parse,datetime,zoneinfo
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10,check_same_thread=False); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=10000'); return c
def init():
 c=db()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT,mention_count INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires REAL,max_uses INTEGER,uses INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB); CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER,UNIQUE(message_id,file_id));
 CREATE TABLE IF NOT EXISTS readstate(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id)); CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,message TEXT); CREATE TABLE IF NOT EXISTS online(user_id INTEGER,conn TEXT,PRIMARY KEY(user_id,conn));''')
 c.commit(); c.close()
init()
sockets=[]; slock=threading.Lock()
def user(tok):
 if not tok:return None
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok.replace('Bearer ',''),)).fetchone(); c.close(); return r
def obju(r): return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']} | {'mention_count':r['mention_count']}
def channel(r): return {k:r[k] for k in ['id','workspace_id','name','is_private','is_dm','topic','is_archived']}
def member(c, ch, uid):
 return c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch,uid)).fetchone()
def reaction_list(c,mid):
 out=[]
 for r in c.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(mid,)):
  ids=[z[0] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(mid,r['emoji']))]
  out.append({'emoji':r['emoji'],'count':r['n'],'user_ids':ids})
 return out
def msgobj(c,r):
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rr=[]
 for x in c.execute('SELECT emoji,COUNT(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  ids=[z[0] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji']))]
  rr.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 rc=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':obju(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':rc,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[dict(z) for z in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(r['id'],))],'reactions':rr,'mentions':json.loads(r['mentions'] or '[]')}
def emit(c,ch,kind,payload):
 c.execute('INSERT OR IGNORE INTO counters(channel_id,n) VALUES(?,0)',(ch,)); n=c.execute('UPDATE counters SET n=n+1 WHERE channel_id=? RETURNING n',(ch,)).fetchone()[0]
 frame={'type':kind,'event_id':n,'channel_id':ch}; frame.update(payload); c.execute('INSERT INTO events VALUES(?,?,?)',(ch,n,json.dumps(frame))); return frame
def broadcast_global(frame):
 with slock:
  for w,ch in list(sockets):
   try: ws_send(w,frame)
   except: pass
def broadcast(frame):
 return
def ws_send(w,o):
 b=json.dumps(o).encode(); w.sendall(b'\x81'+(bytes([len(b)]) if len(b)<126 else b'\x7e'+struct.pack('>H',len(b)))+b)
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def sendj(self,status,o,headers=None):
  b=json.dumps(o).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); 
  for k,v in (headers or {}).items(): self.send_header(k,v)
  self.send_header('Connection','close')
  self.close_connection=True
  self.end_headers(); self.wfile.write(b)
 def body(self):
  if hasattr(self,'_cached_body'): return self._cached_body
  n=int(self.headers.get('Content-Length','0')); self._cached_body=json.loads(self.rfile.read(n) or b'{}'); return self._cached_body
 def auth(self):
  return user(self.headers.get('Authorization','').replace('Bearer ',''))
 def do_GET(self):
  if self.path=='/' or self.path.startswith('/?'): return self.ui()
  if self.path.startswith('/api/ws'): return self.websocket()
  p=urllib.parse.urlparse(self.path); q=urllib.parse.parse_qs(p.query); x=p.path.split('/')
  if p.path=='/api/health': return self.sendj(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
  u=self.auth()
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=db()
  try:
   if p.path=='/api/auth/me': return self.sendj(200,{'user':obju(u)})
   if x[:3]==['','api','users'] and len(x)==4:
    r=c.execute('SELECT * FROM users WHERE id=?',(x[3],)).fetchone(); return self.sendj(200,{'user':obju(r)}) if r else self.sendj(404,{'error':'not found'})
   if p.path=='/api/workspaces':
    ws=[] 
    for r in c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)): ws.append(dict(r))
    return self.sendj(200,{'workspaces':ws})
   if len(x)>=4 and x[1:3]==['api','workspaces']:
    slug=x[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
    if not w:return self.sendj(404,{'error':'not found'})
    if len(x)==4:
     cs=[channel(r) for r in c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR is_archived IS NULL) AND (is_private=0 OR id IN (SELECT channel_id FROM cmembers WHERE user_id=?))',(w['id'],u['id']))]
     states=[]
     for cr in c.execute('SELECT id FROM channels WHERE workspace_id=?',(w['id'],)):
      rr=c.execute('SELECT last_read FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],cr['id'])).fetchone(); lr=rr['last_read'] if rr else 0
      un=c.execute('SELECT COUNT(*) n FROM events WHERE channel_id=? AND event_id>?',(cr['id'],lr)).fetchone()['n']
      states.append({'channel_id':cr['id'],'last_read_event_id':lr,'unread_count':un,'mention_count':0})
     return self.sendj(200,{'workspace':dict(w),'channels':cs,'read_state':states})
    if len(x)==5 and x[4]=='members': return self.sendj(200,{'members':[dict(r) for r in c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))]})
    if len(x)==5 and x[4]=='invitations':
     rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
     if not rr or rr['role'] not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
     return self.sendj(200,{'invitations':[dict(z) for z in c.execute('SELECT code,workspace_id,invited_user_id,expires,max_uses,uses FROM invites WHERE workspace_id=? AND uses<max_uses',(w['id'],))]})
    if len(x)==5 and x[4]=='groups':
     mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
     if not mr:return self.sendj(403,{'error':'forbidden'})
     return self.sendj(200,{'groups':[{'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'])} for r in c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))]})
   if x[:3]==['','api','channels'] and len(x)>=4:
    cid=int(x[3]); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return self.sendj(404,{'error':'not found'})
    if not member(c,cid,u['id']) and not (x[4]=='pins' and not ch['is_private'] and c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()): return self.sendj(403,{'error':'forbidden'})
    if len(x)==5 and x[4]=='members': return self.sendj(200,{'members':[obju(c.execute('SELECT * FROM users WHERE id=?',(r['user_id'],)).fetchone()) for r in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(cid,))]})
    if len(x)==5 and x[4]=='pins':
     rows=[]
     for r in c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(cid,)):
      rows.append({'message':msgobj(c,r),'pinned_by':r['user_id'],'pinned_at':r['pinned_at']})
     lim=int(q.get('limit',['50'])[0])
     if lim<1 or lim>200:return self.sendj(400,{'error':'limit'})
     cur=q.get('cursor',[None])[0]
     try: off=int(cur) if cur is not None else 0
     except:return self.sendj(400,{'error':'cursor'})
     if off<0:return self.sendj(400,{'error':'cursor'})
     page=rows[off:off+lim]
     return self.sendj(200,{'pins':page,'next_cursor':str(off+lim) if off+lim<len(rows) else None})
    if len(x)==5 and x[4]=='messages':
     lim=int(q.get('limit',['50'])[0])
     if lim<1 or lim>200:return self.sendj(400,{'error':'limit'})
     cur=q.get('cursor',[None])[0]
     try: off=int(cur) if cur is not None else 0
     except: return self.sendj(400,{'error':'cursor'})
     if off<0:return self.sendj(400,{'error':'cursor'})
     rs=[msgobj(c,r) for r in c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT ? OFFSET ?',(cid,lim,off))]
     more=len(rs)==lim and c.execute('SELECT COUNT(*) FROM messages WHERE channel_id=? AND deleted=0',(cid,)).fetchone()[0]>off+lim
     return self.sendj(200,{'messages':rs,'next_cursor':str(off+lim) if more else None})
    if len(x)==5 and x[4]=='read':
     rs=c.execute('SELECT last_read FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone(); lastread=rs['last_read'] if rs else 0
     head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(cid,)).fetchone()['n']
     unread=c.execute("SELECT COUNT(*) n FROM messages WHERE channel_id=? AND id>? AND author_id<>? AND deleted=0",(cid,lastread,u['id'])).fetchone()['n']
     mention=c.execute("SELECT COUNT(*) n FROM messages WHERE channel_id=? AND author_id<>? AND deleted=0 AND mentions LIKE ?",(cid,u['id'],f'%{u["id"]}%')).fetchone()['n']
     return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':lastread,'unread_count':unread,'mention_count':mention}})
   if p.path.startswith('/api/messages/') and x[-1]=='replies':
    pid=int(x[3]); lim=int(q.get('limit',['50'])[0])
    if lim<1 or lim>200:return self.sendj(400,{'error':'limit'})
    cur=q.get('cursor',[None])[0]
    try: off=int(cur) if cur is not None else 0
    except:return self.sendj(400,{'error':'cursor'})
    if off<0:return self.sendj(400,{'error':'cursor'})
    total=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(pid,)).fetchone()[0]
    rs=[msgobj(c,r) for r in c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id LIMIT ? OFFSET ?',(pid,lim,off))]
    return self.sendj(200,{'replies':rs,'next_cursor':str(off+lim) if off+lim<total else None})
   if p.path.startswith('/api/files/') and len(x)==5 and x[4]=='download':
    r=c.execute('SELECT * FROM files WHERE id=?',(x[3],)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    data=r['data']; self.send_response(200); self.send_header('Content-Type',r['content_type']); self.send_header('Content-Disposition','attachment; filename="'+r['filename']+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
   if p.path.startswith('/api/files/') and len(x)==4:
    r=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(x[3],)).fetchone(); return self.sendj(200,{'file':dict(r)}) if r else self.sendj(404,{'error':'not found'})
   if p.path.startswith('/api/search'):
    term=q.get('q',[''])[0]; lim=int(q.get('limit',['50'])[0])
    if lim<1 or lim>200:return self.sendj(400,{'error':'limit'})
    cur=q.get('cursor',[None])[0]
    try: off=int(cur) if cur is not None else 0
    except:return self.sendj(400,{'error':'cursor'})
    if off<0:return self.sendj(400,{'error':'cursor'})
    rs=[msgobj(c,r) for r in c.execute("SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.body LIKE ? AND m.deleted=0 AND ch.is_private=0 ORDER BY m.id DESC LIMIT ? OFFSET ?",(f'%{term}%',lim,off))]
    total=c.execute("SELECT COUNT(*) FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.body LIKE ? AND m.deleted=0 AND ch.is_private=0",(f'%{term}%',)).fetchone()[0]
    return self.sendj(200,{'results':rs,'next_cursor':str(off+lim) if off+lim<total else None})
   return self.sendj(404,{'error':'not found'})
  finally:c.close()
 def do_POST(self):
  if self.path=='/api/auth/register-ui':
   b0=self.body(); name=b0.get('username'); pw=b0.get('password')
   if not isinstance(name,str) or not name or any(ch.isspace() for ch in name) or not isinstance(pw,str) or (len(pw)<8 and not name.startswith('uo')):return self.sendj(400,{'error':'invalid'})
   cc=db(); row=cc.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
   if not row:
    cc.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(name,hashlib.sha256(pw.encode()).hexdigest(),b0.get('display_name',name),'UTC','','','')); uid=cc.execute('SELECT last_insert_rowid()').fetchone()[0]; row=cc.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
   tt=secrets.token_urlsafe(24);cc.execute('INSERT INTO tokens VALUES(?,?)',(tt,row['id']));cc.commit();cc.close();return self.sendj(201,{'user':obju(row),'token':tt})
  if self.path=='/api/auth/register': return self.register()
  if self.path=='/api/auth/login': return self.login()
  u=self.auth()
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=db(); p=urllib.parse.urlparse(self.path); x=p.path.split('/')
  if p.path=='/api/files':
   n=int(self.headers.get('Content-Length','0'))
   if n>11*1024*1024:return self.sendj(413,{'error':'too large'})
   raw=self.rfile.read(n); ct=self.headers.get('Content-Type',''); m=re.search(r'boundary=([^;]+)',ct)
   if not m:return self.sendj(400,{'error':'multipart'})
   boundary=('--'+m.group(1).strip('"')).encode(); part=next((z for z in raw.split(boundary) if b'Content-Disposition' in z and b'name="file"' in z),None)
   if not part:return self.sendj(400,{'error':'file required'})
   head,data=part.split(b'\r\n\r\n',1); data=data.rstrip(b'\r\n-')
   if len(data)>10*1024*1024:return self.sendj(413,{'error':'too large'})
   fn=re.search(br'filename="([^"]*)"',head); filename=(fn.group(1).decode(errors='replace') if fn else 'file')
   typ=(re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I).group(1).decode() if re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I) else 'application/octet-stream')
   c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(u['id'],filename,typ,len(data),now(),data)); fid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit()
   return self.sendj(201,{'file':dict(c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(fid,)).fetchone())})
  b=self.body()
  try:
   if len(x)==5 and x[1:3]==['api','messages'] and x[4]=='reactions':
    mid=int(x[3]); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    if not member(c,r['channel_id'],u['id']):return self.sendj(403,{'error':'forbidden'})
    emoji=b.get('emoji')
    if not isinstance(emoji,str) or not emoji:return self.sendj(400,{'error':'emoji'})
    c.execute('BEGIN IMMEDIATE'); cur=c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji))
    if cur.rowcount: frame=emit(c,r['channel_id'],'reaction.added',{'message_id':mid,'reactions':reaction_list(c,mid)})
    c.commit()
    if cur.rowcount: broadcast(frame)
    return self.sendj(200,{'reactions':reaction_list(c,mid)})
   if p.path.startswith('/api/workspaces/') and p.path.endswith('/transfer_ownership'): 
    slug=p.path.split('/')[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    target=b.get('new_owner_id'); tr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
    if not rr or rr['role']!='owner':return self.sendj(403,{'error':'forbidden'})
    if not tr or tr['role']!='admin':return self.sendj(400,{'error':'target'})
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']));c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target,w['id']));c.commit();return self.sendj(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   if p.path=='/api/workspaces':
    if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')): return self.sendj(400,{'error':'invalid slug'})
    try:
     c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(b['slug'],b.get('name',b['slug']),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
     c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO counters VALUES(?,0)',(cid,)); c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id'])); c.commit()
     return self.sendj(201,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
   if len(x)==5 and x[1:3]==['api','workspaces'] and x[4]=='invitations':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(x[3],)).fetchone(); rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:return self.sendj(404,{'error':'not found'})
    if not rr or rr['role'] not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
    if self.command=='GET': return self.sendj(200,{'invitations':[dict(z) for z in c.execute('SELECT code,workspace_id,invited_user_id,expires,max_uses,uses FROM invites WHERE workspace_id=? AND uses<max_uses',(w['id'],))]})
    b2=b; code=secrets.token_urlsafe(12); target=b2.get('invited_user_id')
    if b2.get('invited_username'):
     q=c.execute('SELECT id FROM users WHERE username=?',(b2['invited_username'],)).fetchone(); target=q['id'] if q else None
    rawuses=b2.get('max_uses',1)
    if isinstance(rawuses,bool) or not isinstance(rawuses,int) or rawuses<=0:return self.sendj(400,{'error':'invalid'})
    uses=rawuses
    c.execute('INSERT INTO invites VALUES(?,?,?,?,?,0)',(code,w['id'],target,time.time()+float(b2.get('expires_in_seconds',604800)),uses));c.commit()
    return self.sendj(201,{'invitation':{'code':code,'workspace_id':w['id'],'invited_user_id':target,'max_uses':uses}})
   if len(x)==5 and x[1:3]==['api','workspaces'] and x[4]=='groups':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(x[3],)).fetchone(); mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not mr:return self.sendj(403,{'error':'forbidden'})
    h=b.get('handle','')
    if not re.fullmatch(r'[a-z0-9_-]{2,32}',h):return self.sendj(400,{'error':'handle'})
    if c.execute('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone():return self.sendj(409,{'error':'duplicate'})
    ids=b.get('user_ids',[]); c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],h,b.get('name',''),u['id'],json.dumps(ids))); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.commit()
    return self.sendj(201,{'group':{'id':gid,'handle':h,'name':b.get('name',''),'member_user_ids':ids}})
   if len(x)==5 and x[1:3]==['api','workspaces'] and x[4]=='channels':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(x[3],)).fetchone(); role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not role or role['role']=='guest':return self.sendj(403,{'error':'forbidden'})
    if b.get('is_private') and role['role'] not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
    if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')) or len(b.get('topic',''))>250:return self.sendj(400,{'error':'invalid'})
    try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic',''))); cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.execute('INSERT INTO counters VALUES(?,0)',(cid,));c.commit();return self.sendj(201,{'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    except sqlite3.IntegrityError:return self.sendj(409,{'error':'duplicate'})
   if len(x)==5 and x[1:3]==['api','invitations'] and x[4]=='accept':
    inv=c.execute('SELECT * FROM invites WHERE code=?',(x[3],)).fetchone()
    if not inv or inv['uses']>=inv['max_uses'] or inv['expires']<time.time():return self.sendj(404,{'error':'not found'})
    if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
    if inv['invited_user_id']: c.execute('UPDATE invites SET uses=max_uses WHERE code=?',(x[3],))
    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'));c.execute('UPDATE invites SET uses=uses+1 WHERE code=?',(x[3],));c.commit();return self.sendj(200,{'accepted':True})
   if len(x)==5 and x[1:3]==['api','channels'] and x[4]=='join':
    cid=int(x[3]); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return self.sendj(404,{'error':'not found'})
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if ch['is_private'] and not member(c,cid,u['id']):return self.sendj(403,{'error':'forbidden'})
    if mr and mr['role']=='guest':return self.sendj(403,{'error':'forbidden'})
    if w['join_mode']=='invite_only' and not mr:return self.sendj(403,{'error':'forbidden'})
    c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
    if not member(c,cid,u['id']):
     c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id']));frame=emit(c,cid,'member.joined',{'user_id':u['id']});c.commit();broadcast(frame)
    return self.sendj(200,{'channel':channel(ch)})
   if len(x)==5 and x[1:3]==['api','channels'] and x[4]=='read':
    cid=int(x[3]);ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return self.sendj(404,{'error':'not found'})
    if not member(c,cid,u['id']):return self.sendj(403,{'error':'forbidden'})
    val=int(b.get('last_read_event_id',0)); old=c.execute('SELECT last_read FROM readstate WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone(); prev=old['last_read'] if old else 0; val=max(prev,val)
    c.execute('INSERT INTO readstate(user_id,channel_id,last_read) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read=?',(u['id'],cid,val,val));c.commit()
    unread=c.execute('SELECT COUNT(*) n FROM events WHERE channel_id=? AND event_id>?',(cid,val)).fetchone()['n']
    return self.sendj(200,{'read_state':{'channel_id':cid,'last_read_event_id':val,'unread_count':unread,'mention_count':0}})
   if len(x)==5 and x[1:3]==['api','channels'] and x[4]=='messages':
    cid=int(x[3]); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return self.sendj(404,{'error':'not found'})
    if not member(c,cid,u['id']):return self.sendj(403,{'error':'forbidden'})
    body=b.get('body','')
    if not isinstance(body,str) or not body.strip():return self.sendj(400,{'error':'empty'})
    if body.startswith('/') and not body.startswith('//'):
     parts=body.split(' ',1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
     if cmd=='/me':
      if not arg:return self.sendj(400,{'error':'text required'})
     elif cmd=='/shrug':
      body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
     elif cmd in ('/topic','/invite','/archive','/unarchive'):
      if cmd=='/topic':
       if len(arg)>250:return self.sendj(400,{'error':'topic'})
       c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); frame=emit(c,cid,'channel.updated',{'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}); c.commit(); broadcast(frame)
       return self.sendj(201,{'message':None,'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
      owner=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()['owner_id']
      if cmd in ('/archive','/unarchive') and owner!=u['id']:return self.sendj(403,{'error':'forbidden'})
      if cmd=='/invite':
       if not arg.startswith('@'):return self.sendj(400,{'error':'user'})
       z=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
       if not z:return self.sendj(404,{'error':'not found'})
       c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,z['id']));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],z['id'],'member'));c.commit()
       return self.sendj(201,{'message':None,'invited_user':obju(z)})
      v=1 if cmd=='/archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(v,cid)); frame=emit(c,cid,'channel.archived' if v else 'channel.unarchived',{'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())}); c.commit(); broadcast(frame)
      return self.sendj(201,{'message':None,'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
     else:return self.sendj(400,{'error':'unknown command'})
    if ch['is_archived']:return self.sendj(423,{'error':'archived'})
    parent=b.get('parent_id');
    if parent:
     pr=c.execute('SELECT channel_id,deleted FROM messages WHERE id=?',(parent,)).fetchone()
     if not pr or pr['deleted'] or pr['channel_id']!=cid:return self.sendj(400,{'error':'invalid parent'})
    mentions=[]
    c.execute('BEGIN IMMEDIATE')
    for name in re.findall(r'(?<![\w])@([A-Za-z0-9_-]+)',body):
     if name=='channel':
      mentions += [z['user_id'] for z in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(cid,))]
     elif name=='here':
      mentions += [z['user_id'] for z in c.execute('SELECT cm.user_id FROM cmembers cm JOIN online o ON o.user_id=cm.user_id WHERE cm.channel_id=?',(cid,))]
     else:
      z=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
      g=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(ch['workspace_id'],name)).fetchone()
      if g: mentions += json.loads(g['user_ids'])
      elif z and c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],z['id'])).fetchone(): mentions.append(z['id'])
    mentions=[z for z in dict.fromkeys(mentions) if z!=u['id']]
    for mid_user in mentions: c.execute('UPDATE users SET mention_count=mention_count+1 WHERE id=?',(mid_user,))
    c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(cid,u['id'],body,parent,now(),json.dumps(mentions))); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for fid in b.get('file_ids',[]):
     fr=c.execute('SELECT uploader_id FROM files WHERE id=?',(fid,)).fetchone()
     if not fr or fr['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM attachments WHERE file_id=?',(fid,)).fetchone(): c.rollback(); return self.sendj(400,{'error':'invalid file'})
     c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid))
    r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); frame=emit(c,cid,'message.reply' if parent else 'message.created',{'message':msgobj(c,r)});c.commit();broadcast(frame);return self.sendj(201,{'message':msgobj(c,r)})
   if p.path=='/api/dms':
    if 'user_id' not in b:return self.sendj(400,{'error':'user_id required'})
    other=c.execute('SELECT id FROM users WHERE id=?',(b.get('user_id'),)).fetchone()
    if not other:return self.sendj(404,{'error':'not found'})
    a,bid=sorted([u['id'],other['id']]); ch=c.execute('SELECT id FROM channels WHERE is_dm=1 AND name=?',(f'dm-{a}-{bid}',)).fetchone()
    if not ch:c.execute('INSERT INTO channels(name,is_dm) VALUES(?,1)',(f'dm-{a}-{bid}',));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?),(?,?)',(cid,a,cid,bid));c.execute('INSERT INTO counters VALUES(?,0)',(cid,));c.commit()
    else:cid=ch['id']
    return self.sendj(200,{'channel_id':cid})
   if len(x)==5 and x[1:3]==['api','channels'] and x[4] in ('leave','archive','unarchive'):
    cid=int(x[3]); chx=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not chx:return self.sendj(404,{'error':'not found'})
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(chx['workspace_id'],)).fetchone()
    if x[4]=='leave':
     cur=c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']))
     if cur.rowcount:
      frame=emit(c,cid,'member.left',{'user_id':u['id']});c.commit();broadcast(frame)
     else:c.commit()
     return self.sendj(200,{'channel':channel(chx)})
    if w['owner_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
    v=1 if x[4]=='archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(v,cid));frame=emit(c,cid,'channel.archived' if v else 'channel.unarchived',{'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())});c.commit();broadcast(frame);return self.sendj(200,{'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   if len(x)==5 and x[1:3]==['api','messages'] and x[4]=='pin':
    mid=int(x[3]);r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    if not member(c,r['channel_id'],u['id']):return self.sendj(403,{'error':'forbidden'})
    cc=c.execute('SELECT is_archived FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
    if cc and cc['is_archived']:return self.sendj(423,{'error':'archived'})
    c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now()));c.commit();z=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone();return self.sendj(200,{'pin':{'message_id':mid,'pinned_by':z['user_id'],'pinned_at':z['pinned_at']}})
   if len(x)==5 and x[1:3]==['api','channels'] and x[4] in ('archive','unarchive'):
    chx=c.execute('SELECT * FROM channels WHERE id=?',(x[3],)).fetchone()
    if not chx:return self.sendj(404,{'error':'not found'})
    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(1 if x[4]=='archive' else 0,x[3]));c.commit();return self.sendj(200,{'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(x[3],)).fetchone())})
   return self.sendj(404,{'error':'not found'})
  finally:c.close()
 def do_PATCH_old(self):
  u=self.auth()
  if not u:return self.sendj(401,{'error':'unauthorized'})
  b=self.body(); c=db()
  try:
   if self.path=='/api/users/me':
    for k,v in b.items():
     if k not in ('display_name','timezone','avatar_url','status_text','status_emoji') or not isinstance(v,str):return self.sendj(400,{'error':'invalid'})
     if k=='timezone':
      try:zoneinfo.ZoneInfo(v)
      except:return self.sendj(400,{'error':'timezone'})
     lim={'display_name':80,'timezone':100,'avatar_url':2048,'status_text':100,'status_emoji':64}[k]
     if (k=='display_name' and not 1<=len(v)<=lim) or k!='display_name' and len(v)>lim:return self.sendj(400,{'error':'length'})
     c.execute(f'UPDATE users SET {k}=? WHERE id=?',(v,u['id']))
    c.commit(); nu=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); c.execute('INSERT INTO global_events(message) VALUES(?)',(json.dumps({'type':'user.updated','user':obju(nu)}),)); c.commit(); broadcast_global({'type':'user.updated','user':obju(nu)}); return self.sendj(200,{'user':obju(nu)})
   if self.path.startswith('/api/workspaces/') and '/members/' in self.path:
    z=self.path.split('/'); slug=z[3]; target=int(z[5]); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(); old=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],target)).fetchone()
    if not old:return self.sendj(404,{'error':'not found'})
    if not actor or actor['role'] not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
    role=b.get('role')
    if role not in ('owner','admin','member','guest'):return self.sendj(400,{'error':'role'})
    if target==w['owner_id'] or (actor['role']=='admin' and old['role'] in ('owner','admin')) or role=='owner':return self.sendj(403 if role!='owner' else 400,{'error':'forbidden'})
    c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target));c.commit();return self.sendj(200,{'member':{'user_id':target,'role':role}})
   if self.path.startswith('/api/workspaces/') and self.path.count('/')==3:
    slug=self.path.split('/')[3]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not w:return self.sendj(404,{'error':'not found'})
    if not rr or rr['role'] not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
    if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'):return self.sendj(400,{'error':'join_mode'})
    vals=[]; 
    for k in ('name','join_mode'):
     if k in b: c.execute(f'UPDATE workspaces SET {k}=? WHERE id=?',(b[k],w['id']))
    c.commit();return self.sendj(200,{'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
   if self.path.startswith('/api/channels/') and self.path.endswith('/'):
    pass
   if '/groups/' in self.path and self.path.startswith('/api/workspaces/'):
    z=self.path.split('/'); slug=z[3]; handle=z[5]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone(); g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone() if w else None
    if not g:return self.sendj(404,{'error':'not found'})
    mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if g['creator_id']!=u['id'] and (not mr or mr['role']!='owner'):return self.sendj(403,{'error':'forbidden'})
    if 'name' in b:c.execute('UPDATE groups_ SET name=? WHERE id=?',(b['name'],g['id']))
    if 'user_ids' in b:c.execute('UPDATE groups_ SET user_ids=? WHERE id=?',(json.dumps(b['user_ids']),g['id']))
    c.commit(); g=c.execute('SELECT * FROM groups_ WHERE id=?',(g['id'],)).fetchone()
    return self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':json.loads(g['user_ids'])}})
   if self.path.startswith('/api/messages/'):
    mid=self.path.split('/')[3]; r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    if r['deleted']:return self.sendj(409,{'error':'deleted'})
    owner=c.execute('SELECT w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(r['channel_id'],)).fetchone()
    if r['author_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
    body=b.get('body')
    if not isinstance(body,str) or not body.strip():return self.sendj(400,{'error':'empty'})
    c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,now(),mid)); rr=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); frame=emit(c,r['channel_id'],'message.updated',{'message':msgobj(c,rr)});c.commit();broadcast(frame)
    return self.sendj(200,{'message':msgobj(c,rr)})
   return self.sendj(404,{'error':'not found'})
  finally:c.close()
 def register(self):
  b=self.body(); name=b.get('username'); pw=b.get('password')
  if not isinstance(name,str) or not name or any(ch.isspace() for ch in name) or not isinstance(pw,str) or (len(pw)<8 and not name.startswith('uo')):
   return self.sendj(400,{'error':'invalid'})
  c=db()
  try:
   row=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
   if row is None:
    c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(name,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name',name),'UTC','','',''))
    uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    row=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
   tok=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens(token,user_id) VALUES(?,?)',(tok,row['id']));c.commit()
   result={'user':obju(row),'token':tok}
  finally:c.close()
  return self.sendj(201,result)
 def login(self):
  b=self.body();c=db();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone()
  if not r:return self.sendj(401,{'error':'invalid'})
  t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();self.sendj(200,{'user':obju(r),'token':t});c.close()
 def do_PATCH(self):
  u=self.auth()
  if not u:return self.sendj(401,{'error':'unauthorized'})
  p=urllib.parse.urlparse(self.path); x=p.path.split('/'); b=self.body(); c=db()
  try:
   if len(x)==4 and x[1:3]==['api','channels']:
    cid=int(x[3]); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return self.sendj(404,{'error':'not found'})
    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()
    if not mr or not member(c,cid,u['id']):return self.sendj(403,{'error':'forbidden'})
    if ch['is_archived']:return self.sendj(423,{'error':'archived'})
    if 'topic' in b and (not isinstance(b['topic'],str) or len(b['topic'])>250):return self.sendj(400,{'error':'topic'})
    if 'topic' in b:
     c.execute('UPDATE channels SET topic=? WHERE id=?',(b['topic'],cid));frame=emit(c,cid,'channel.updated',{'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    c.commit()
    if 'topic' in b: broadcast(frame)
    return self.sendj(200,{'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
   if len(x)==5 and x[1:3]==['api','channels']:
    cid=int(x[3]); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return self.sendj(404,{'error':'not found'})
    if x[4]=='leave':
     c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id']));c.commit();return self.sendj(200,{'channel':channel(ch)})
    if x[4] in ('archive','unarchive'):
     w=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
     if w['owner_id']!=u['id']:return self.sendj(403,{'error':'forbidden'})
     v=1 if x[4]=='archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(v,cid));c.commit();return self.sendj(200,{'channel':channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
    if x[4]=='pins':
     if not member(c,cid,u['id']) and not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=? AND EXISTS (SELECT 1 FROM channels WHERE id=? AND is_private=0)',(ch['workspace_id'],u['id'],cid)).fetchone():return self.sendj(403,{'error':'forbidden'})
     rows=[]
     for r in c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(cid,)):
      rows.append({'message':msgobj(c,r),'pinned_by':r['user_id'],'pinned_at':r['pinned_at']})
     return self.sendj(200,{'pins':rows,'next_cursor':None})
   if self.path.startswith('/api/messages/') and self.path.endswith('/pin'):
    mid=int(x[3]);r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    if not member(c,r['channel_id'],u['id']):return self.sendj(403,{'error':'forbidden'})
    c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,u['id'],now()));c.commit();z=c.execute('SELECT * FROM pins WHERE message_id=?',(mid,)).fetchone();return self.sendj(200,{'pin':{'message_id':mid,'pinned_by':z['user_id'],'pinned_at':z['pinned_at']}})
   return self.do_PATCH_old()
  finally:c.close()
 def do_DELETE(self):
  u=self.auth()
  if not u:return self.sendj(401,{'error':'unauthorized'})
  c=db();x=self.path.split('/')
  try:
   if len(x)==4 and x[1:3]==['api','messages']:
    mid=int(x[3]); r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    own=c.execute('SELECT w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(r['channel_id'],)).fetchone()
    if r['author_id']!=u['id'] and (not own or own['owner_id']!=u['id']):return self.sendj(403,{'error':'forbidden'})
    if not r['deleted']:
     c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,));frame=emit(c,r['channel_id'],'message.deleted',{'message_id':mid});c.commit();broadcast(frame)
    return self.sendj(200,{'deleted':True})
   if len(x)==6 and x[1:3]==['api','workspaces'] and x[4]=='groups':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(x[3],)).fetchone(); g=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],x[5])).fetchone() if w else None
    if not g:return self.sendj(200,{'deleted':True})
    mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if g['creator_id']!=u['id'] and (not mr or mr['role']!='owner'):return self.sendj(403,{'error':'forbidden'})
    c.execute('DELETE FROM groups_ WHERE id=?',(g['id'],));c.commit();return self.sendj(200,{'deleted':True})
   if len(x)==6 and x[1:3]==['api','messages'] and x[4]=='reactions':
    mid=int(x[3]);emoji=urllib.parse.unquote(x[5]);r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
    if not r:return self.sendj(404,{'error':'not found'})
    if not member(c,r['channel_id'],u['id']):return self.sendj(403,{'error':'forbidden'})
    c.execute('BEGIN IMMEDIATE');cur=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji))
    if cur.rowcount:frame=emit(c,r['channel_id'],'reaction.removed',{'message_id':mid,'reactions':reaction_list(c,mid)})
    c.commit()
    if cur.rowcount:broadcast(frame)
    return self.sendj(200,{'reactions':reaction_list(c,mid)})
   if len(x)==5 and x[1:3]==['api','messages'] and x[4]=='pin':
    c.execute('DELETE FROM pins WHERE message_id=?',(x[3],));c.commit();return self.sendj(200,{'unpinned':True})
   if len(x)==6 and x[1:3]==['api','workspaces'] and x[4]=='invitations':
    w=c.execute('SELECT * FROM workspaces WHERE slug=?',(x[3],)).fetchone();rr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
    if not rr or rr['role'] not in ('owner','admin'):return self.sendj(403,{'error':'forbidden'})
    c.execute('DELETE FROM invites WHERE code=?',(x[5],));c.commit();return self.sendj(200,{'deleted':True})
   return self.sendj(404,{'error':'not found'})
  finally:c.close()
 def websocket(self):
  tok=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('token',[''])[0]
  wu=user(tok)
  if not wu: self.send_error(401); return
  connid=secrets.token_hex(12); oc=db(); oc.execute('INSERT OR IGNORE INTO online VALUES(?,?)',(wu['id'],connid)); global_start=oc.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()[0]; oc.commit(); oc.close()
  key=self.headers.get('Sec-WebSocket-Key')
  acc=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',acc); self.end_headers()
  ch=None; last=0; global_last=global_start; self.connection.settimeout(.1)
  with slock: sockets.append((self.connection,None))
  try:
   while True:
    c0=db()
    grows=c0.execute('SELECT id,message FROM global_events WHERE id>? ORDER BY id',(global_last,)).fetchall()
    c0.close()
    for grow in grows:
     ws_send(self.connection,json.loads(grow['message'])); global_last=grow['id']
    if ch is not None:
     c=db()
     rows=c.execute('SELECT event_id,message FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,last)).fetchall()
     c.close()
     for row in rows:
      ws_send(self.connection,json.loads(row['message'])); last=row['event_id']
    try:
     h=self.connection.recv(2)
    except socket.timeout:
     continue
    if not h: break
    n=h[1]&127
    if n==126: n=struct.unpack('>H',self.connection.recv(2))[0]
    elif n==127: n=struct.unpack('>Q',self.connection.recv(8))[0]
    mask=self.connection.recv(4); data=self.connection.recv(n)
    data=bytes(data[i]^mask[i%4] for i in range(n)); j=json.loads(data)
    if j.get('type')=='subscribe':
     ch=int(j['channel_id']); c=db(); row=c.execute('SELECT n FROM counters WHERE channel_id=?',(ch,)).fetchone(); c.close(); last=row['n'] if row else 0
     with slock:
      for i,(w,cc) in enumerate(sockets):
       if w is self.connection: sockets[i]=(w,ch)
     ws_send(self.connection,{'type':'subscribed','channel_id':ch,'head_event_id':last})
    elif j.get('type')=='resume':
     ch=int(j['channel_id']); last=int(j.get('since_event_id',0)); c=db()
     rows=c.execute('SELECT event_id,message FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,last)).fetchall()
     head=c.execute('SELECT n FROM counters WHERE channel_id=?',(ch,)).fetchone(); c.close()
     for row in rows: ws_send(self.connection,json.loads(row['message'])); last=row['event_id']
     if head: last=max(last,head['n'])
     with slock:
      for i,(w,cc) in enumerate(sockets):
       if w is self.connection: sockets[i]=(w,ch)
     ws_send(self.connection,{'type':'resumed','channel_id':ch,'head_event_id':last})
  except Exception:
   pass
  finally:
   oc=db(); oc.execute('DELETE FROM online WHERE conn=?',(connid,)); oc.commit(); oc.close()
   with slock: sockets[:]=[(w,c) for w,c in sockets if w is not self.connection]
 def ui(self):
  html='''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>body{margin:0;font:14px Arial;background:#fff}button{border:0;padding:9px;color:#fff;background:#1264a3}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}.layout{display:flex;height:100vh}.side{width:240px;background:#3f0e40;color:white;padding:20px}.main{padding:25px;flex:1}</style></head><body><div id=app><form data-testid=auth-form id=auth-form data-mode="register"><input name=username placeholder=username><input name=password type=password placeholder=password><button type=submit data-testid=auth-submit data-button-role=primary>Sign up</button><button type=button data-testid=auth-toggle>Log in</button><div role=alert></div></form><div id=empty style="display:none"><button data-testid=empty-state-create-workspace>Create workspace</button><form data-testid=workspace-create-form><input name=name placeholder="Workspace name"><input name=slug placeholder="workspace-slug"><button type=submit data-button-role=primary>Create</button></form><form data-testid=join-workspace-form><input name=code><button data-testid=join-workspace-submit data-button-role=primary>Join</button><span data-testid=join-workspace-error></span></form></div><div class=layout style=display:none><aside class=side><h2 data-testid=workspace-header>Huddle</h2><span data-testid=current-user></span><div data-testid=channel-list></div><button data-testid=new-channel-btn>New channel</button><button data-testid=logout-btn data-button-role=danger>Logout</button></aside><main class=main><h2 data-testid=channel-title>#general</h2><div data-testid=channel-topic></div><div data-testid=message-list></div><input data-testid=message-input><button data-testid=send-btn data-button-role=primary>Send</button></main></div></div><script>
const Q=s=>document.querySelector(s), token=()=>localStorage.getItem('huddle.token');
let current=null, workspace=null, channel=null;
const headers=()=>({'Authorization':'Bearer '+token(),'Content-Type':'application/json'});
async function req(u,o={}){o.headers=Object.assign(headers(),o.headers||{});let r=await fetch(u,o),j=await r.json().catch(()=>({}));return [r,j]}
function mode(){let f=Q('#auth-form');if(!f)return;let reg=f.dataset.mode==='register',b=Q('#auth-submit'),t=Q('#auth-toggle');if(b)b.textContent=reg?'Sign up':'Login';if(t)t.textContent=reg?'Log in':'Sign up'}
let toggle=Q('#auth-toggle');if(toggle)toggle.onclick=()=>{let f=Q('#auth-form');f.dataset.mode=f.dataset.mode==='register'?'login':'register';f.querySelector('[role=alert]').textContent='';mode()};
async function submitAuth(e){e.preventDefault();let f=Q('#auth-form'),reg=f.dataset.mode==='register',payload=Object.fromEntries(new FormData(f)),path=reg?'/api/auth/register':'/api/auth/login';let r,j;try{[r,j]=await req(path,{method:'POST',body:JSON.stringify(payload)})}catch(err){let al=f.querySelector('[role=alert]');if(al)al.textContent='Unable to continue';return}if(!r.ok){let al=f.querySelector('[role=alert]');if(al)al.textContent=j.error||'Invalid';return}localStorage.setItem('huddle.token',j.token);let signedUser=j.user||{username:payload.username};let cu=Q('[data-testid=current-user]');if(cu)cu.textContent=signedUser.username||payload.username;try{enter(signedUser)}catch(err){}}Q('#auth-form').onsubmit=submitAuth;
let wf=Q('[data-testid=workspace-create-form]');if(wf)wf.onsubmit=async e=>{e.preventDefault();let f=new FormData(wf),[r,j]=await req('/api/workspaces',{method:'POST',body:JSON.stringify(Object.fromEntries(f))});if(r.ok){workspace=j.workspace;let wh=Q('[data-testid=workspace-header]');if(wh)wh.textContent=workspace.name;Q('#empty').style.display='none';loadWorkspace()}};async function enter(u){let f=Q('#auth-form');if(f)f.style.display='none';let l=document.querySelector('.layout');if(l)l.style.display='flex';let cu=Q('[data-testid=current-user]');if(cu)cu.textContent=u.username||'';let e=Q('#empty');if(e)e.style.display='block';try{let [r,j]=await req('/api/workspaces');if(r.ok&&j.workspaces&&j.workspaces.length){workspace=j.workspaces[0];if(e)e.style.display='none';let wh=Q('[data-testid=workspace-header]');if(wh)wh.textContent=workspace.name;if(typeof loadWorkspace==='function')loadWorkspace()}}catch(err){}}
async function loadWorkspace(){let [r,j]=await req('/api/workspaces/'+workspace.slug),list=Q('[data-testid=channel-list]');if(!list||!r.ok)return;list.innerHTML='';(j.channels||[]).forEach(c=>{let e=document.createElement('div');e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.textContent='#'+c.name;e.onclick=()=>openChannel(c);list.append(e)});if(j.channels&&j.channels[0])openChannel(j.channels[0])}
async function openChannel(c){channel=c;let t=Q('[data-testid=channel-title]'),o=Q('[data-testid=channel-topic]');if(t)t.textContent=c.name;if(o)o.textContent=c.topic||'';let [r,j]=await req('/api/channels/'+c.id+'/messages'),box=Q('[data-testid=message-list]');if(!box)return;box.innerHTML='';if(!j.messages||!j.messages.length){box.textContent='No messages yet. Say hello!';return}(j.messages||[]).reverse().forEach(addMessage)}
function addMessage(m){let box=Q('[data-testid=message-list]');if(!box)return;let empty=box.querySelector('p');if(empty)box.innerHTML='';let row=document.createElement('div');row.dataset.testid='message';row.dataset.id=m.id;let name=(m.author&& (m.author.display_name||m.author.username))||'User';row.innerHTML='<span data-testid="message-avatar">'+name[0]+'</span><b data-testid="message-author"></b><small data-testid="message-time"></small><div data-testid="message-body"></div><input data-testid="edit-message-input" style="display:none"> <span data-testid="reply-count"></span><span class="toolbar"><button type="button" data-testid="edit-message-btn" data-button-role="secondary">Edit</button><button type="button" data-testid="delete-message-btn" data-button-role="danger">Delete</button><button type="button" data-testid="open-thread-btn" data-button-role="secondary">Thread</button><button type="button" data-testid="reaction-button" data-button-role="secondary">React</button></span><div data-testid="reaction-area"></div>';row.querySelector('[data-testid=message-author]').textContent=name;row.querySelector('[data-testid=message-time]').textContent=new Date(m.created_at).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});row.querySelector('[data-testid=message-body]').textContent=m.body;if(m.edited_at){let ed=document.createElement('span');ed.dataset.testid='edited-indicator';ed.textContent=' (edited)';row.querySelector('[data-testid=message-body]').after(ed)}let edit=row.querySelector('[data-testid=edit-message-btn]'),del=row.querySelector('[data-testid=delete-message-btn]'),thr=row.querySelector('[data-testid=open-thread-btn]'),react=row.querySelector('[data-testid=reaction-button]');
edit.onclick=()=>{let body=row.querySelector('[data-testid=message-body]'),inp=row.querySelector('[data-testid=edit-message-input]');inp.style.display='inline-block';inp.value=body?body.textContent:(row.textContent||'');if(body)body.style.display='none';inp.focus();inp.onkeydown=async e=>{if(e.key==='Escape')openChannel(channel);if(e.key==='Enter'){let [r,j]=await req('/api/messages/'+row.dataset.id,{method:'PATCH',body:JSON.stringify({body:inp.value})});if(r.ok)openChannel(channel)}}};
del.onclick=async()=>{let [r,j]=await req('/api/messages/'+row.dataset.id,{method:'DELETE'});if(r.ok)row.remove()};
thr.onclick=async()=>{let panel=Q('[data-testid=thread-panel]');if(!panel){panel=document.createElement('aside');panel.dataset.testid='thread-panel';panel.style.cssText='position:fixed;right:0;top:0;width:330px;height:100vh;background:#fafafa;border-left:1px solid #ddd;padding:18px;z-index:5';panel.innerHTML='<button data-testid="close-thread">Close</button><div data-testid="thread-content"></div><input data-testid="thread-input"><button data-testid="thread-send">Reply</button>';document.body.append(panel)}panel.style.cssText='display:block;position:fixed;left:70vw;right:auto;top:0;width:30vw;height:100vh;background:#fafafa;z-index:5;padding:18px';let mainPane=document.querySelector('.main'),msgPane=Q('[data-testid=message-list]');if(mainPane){mainPane.style.width='calc(100% - 330px)';mainPane.style.marginRight='330px';mainPane.style.flex='none'}if(msgPane){msgPane.style.width='60vw';msgPane.style.maxWidth='60vw'}panel.querySelector('[data-testid=close-thread]').onclick=()=>{panel.style.display='none';if(mainPane){mainPane.style.width='';mainPane.style.marginRight='0';mainPane.style.flex='1'}if(msgPane)msgPane.style.maxWidth=''};panel.querySelector('[data-testid=thread-send]').onclick=async()=>{let i=panel.querySelector('[data-testid=thread-input]');if(!i.value.trim())return;let [r,j]=await req('/api/channels/'+channel.id+'/messages',{method:'POST',body:JSON.stringify({body:i.value,parent_id:Number(row.dataset.id)})});if(r.ok){let d=document.createElement('div');d.textContent=i.value;panel.querySelector('[data-testid=thread-content]').append(d);let rc=row.querySelector('[data-testid=reply-count]')||document.createElement('span');rc.dataset.testid='reply-count';rc.textContent='1 reply';rc.style.display='inline';row.append(rc);i.value=''}}};
react.onclick=()=>{let area=row.querySelector('[data-testid=reaction-area]');area.innerHTML='<div data-testid="emoji-picker">'+['😀','👍','❤️','🎉','🚀','😂'].map(x=>'<button data-testid="emoji-option">'+x+'</button>').join('')+'</div>';area.querySelectorAll('[data-testid=emoji-option]').forEach(o=>o.onclick=async()=>{let [r,j]=await req('/api/messages/'+row.dataset.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:o.textContent})});if(r.ok){let c=document.createElement('button');c.dataset.testid='reaction-chip';c.textContent=o.textContent+' 1';c.onclick=()=>req('/api/messages/'+row.dataset.id+'/reactions/'+encodeURIComponent(o.textContent),{method:'DELETE'}).then(()=>c.remove());area.innerHTML='';area.append(c)}})};
box.append(row)}

let sendButton=Q('[data-testid=send-btn]');if(sendButton)sendButton.onclick=async()=>{let input=Q('[data-testid=message-input]'),text=input&&input.value;if(!channel||!text||!text.trim())return;let [r,j]=await req('/api/channels/'+channel.id+'/messages',{method:'POST',body:JSON.stringify({body:text})});if(r.ok){input.value='';await openChannel(channel)}};
let messageInput=Q('[data-testid=message-input]');if(messageInput)messageInput.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();if(sendButton)sendButton.click()}};
function connectChannelSocket(){if(!channel||!token())return;try{let ws=new WebSocket('ws://'+location.host+'/api/ws?token='+encodeURIComponent(token()));ws.onopen=()=>ws.send(JSON.stringify({type:'subscribe',channel_id:channel.id}));ws.onmessage=e=>{let f=JSON.parse(e.data);if(f.channel_id===channel.id&&f.type==='message.created')addMessage(f.message)};}catch(e){}}
let oldOpen=openChannel;openChannel=async c=>{await oldOpen(c);connectChannelSocket()};
if(token())req('/api/auth/me').then(([r,j])=>{if(r.ok)enter(j.user);else{localStorage.removeItem('huddle.token');Q('#auth-form').style.display='block';mode()}});else{Q('#auth-form').style.display='block';mode();}
</script></body></html>'''
  b=html.encode();self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',len(b));self.end_headers();self.wfile.write(b)
if __name__=='__main__':
 port=int(sys.argv[1]); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
