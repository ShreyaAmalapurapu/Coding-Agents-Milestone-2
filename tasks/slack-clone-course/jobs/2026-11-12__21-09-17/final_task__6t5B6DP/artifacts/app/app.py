import asyncio, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from aiohttp import web, WSMsgType

DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
PORT=int(sys.argv[1]) if len(sys.argv)>1 else 8000
NODE=PORT-8000
SCHEMA='''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',dm_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS globals(id INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT);'''
db=sqlite3.connect(DB,check_same_thread=False); db.row_factory=sqlite3.Row
db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA busy_timeout=5000'); db.executescript(SCHEMA); db.execute("ALTER TABLE channels ADD COLUMN is_archived INTEGER DEFAULT 0") if 'is_archived' not in [x['name'] for x in db.execute('PRAGMA table_info(channels)').fetchall()] else None; db.execute('CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,UNIQUE(message_id,user_id,emoji))'); db.execute('CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle))'); db.execute('CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,UNIQUE(group_id,user_id))'); db.execute('CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,UNIQUE(message_id,user_id))'); db.execute('CREATE TABLE IF NOT EXISTS online(user_id INTEGER PRIMARY KEY,connections INTEGER DEFAULT 0)'); db.execute('CREATE TABLE IF NOT EXISTS channel_reads(channel_id INTEGER,user_id INTEGER,last_read_event_id INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,UNIQUE(channel_id,user_id))'); db.execute('CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT)'); db.execute('CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE)'); db.commit()
lock=asyncio.Lock(); sockets=set(); socket_subs={}
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def q(sql,args=()): return db.execute(sql,args)
def userobj(r): return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def token_user(req):
 t=req.headers.get('Authorization','')
 if not t.startswith('Bearer '): return None
 r=q('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t[7:],)).fetchone()
 return r
def out(x,status=200): return web.json_response(x,status=status)
async def auth(req):
 u=token_user(req)
 if not u: raise web.HTTPUnauthorized()
 return u
async def event(cid,kind,payload):
 async with lock:
  db.execute('BEGIN IMMEDIATE')
  r=q('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone()
  n=(r['n'] if r else 0)+1
  q('INSERT OR REPLACE INTO counters(channel_id,n) VALUES(?,?)',(cid,n))
  payload=dict(payload)
  payload['event_id']=n
  q('INSERT INTO events VALUES(?,?,?,?)',(cid,n,kind,json.dumps(payload)))
  db.commit()
 return n
def reactions_of(message_id):
 rows=q('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(message_id,)).fetchall()
 result=[]
 for r in rows:
  x=next((z for z in result if z['emoji']==r['emoji']),None)
  if not x:
   x={'emoji':r['emoji'],'count':0,'user_ids':[]}; result.append(x)
  x['user_ids'].append(r['user_id']); x['count']+=1
 return result
def fileobj(r):
 return {k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}
def files_of(message_id):
 return [fileobj(x) for x in q('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=? ORDER BY mf.file_id',(message_id,)).fetchall()]
def msgobj(r):
 a=q('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':q('SELECT count(*) c FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['c'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':files_of(r['id']),'reactions':reactions_of(r['id']),'mentions':[x['user_id'] for x in q('SELECT user_id FROM message_mentions WHERE message_id=? ORDER BY user_id',(r['id'],)).fetchall()]}
async def require_channel(req,cid,u,write=False):
 c=q('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not c: raise web.HTTPNotFound()
 if c['is_dm']:
  ok=q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
 else: ok=q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
 if not ok: raise web.HTTPForbidden()
 return c
async def register(req):
 b=await req.json()
 if not re.fullmatch(r'[A-Za-z0-9_]+',str(b.get('username',''))) or len(b.get('password',''))<8: return out({'error':'invalid'},400)
 try:
  cur=q('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(b['username'],b['password'],b.get('display_name') or b['username'],'UTC','','','')); db.commit()
 except sqlite3.IntegrityError:return out({'error':'duplicate'},409)
 return await login_user(cur.lastrowid)
async def login_user(uid):
 r=q('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); t=secrets.token_urlsafe(24); q('INSERT INTO tokens VALUES(?,?)',(t,uid)); db.commit()
 return out({'user':userobj(r),'token':t},201)
async def login(req):
 b=await req.json(); r=q('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
 if not r: return out({'error':'invalid'},401)
 x=await login_user(r['id']); x.set_status(200); return x
async def me(req): return out({'user':userobj(await auth(req))})
async def getuser(req):
 await auth(req); r=q('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone()
 return out({'user':userobj(r)}) if r else out({'error':'not found'},404)
async def profile(req):
 u=await auth(req); b=await req.json(); allowed={'display_name':(1,80),'timezone':(1,100),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
 for k,v in b.items():
  if k not in allowed or not isinstance(v,str) or not (allowed[k][0]<=len(v)<=allowed[k][1]) or (k=='timezone' and (not v or not _tz(v))): return out({'error':'invalid'},400)
 if not set(b)<=set(allowed): return out({'error':'invalid'},400)
 if b:
  q('UPDATE users SET '+','.join(k+'=?' for k in b)+' WHERE id=?',tuple(b.values())+(u['id'],)); db.commit()
  r=q('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); payload={'type':'user.updated','user':userobj(r)}; q('INSERT INTO globals(payload) VALUES(?)',(json.dumps(payload),)); db.commit()
 return out({'user':userobj(q('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone())})
def _tz(x):
 try: ZoneInfo(x); return True
 except: return False
async def workspaces(req):
 u=await auth(req); rows=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
 return out({'workspaces':[dict(r) for r in rows]})
async def workspace_create(req):
 u=await auth(req); b=await req.json(); slug=b.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'): return out({'error':'invalid'},400)
 try:
  cur=q('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b['name'],u['id'])); wid=cur.lastrowid
  q('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); q('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=q('SELECT last_insert_rowid() x').fetchone()['x']; q('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); db.commit()
 except sqlite3.IntegrityError:return out({'error':'duplicate'},409)
 return out({'workspace':dict(q('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':dict(q('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())},201)
async def workspace(req):
 u=await auth(req); w=q('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return out({},404)
 cs=q('SELECT * FROM channels WHERE workspace_id=? AND is_dm=0',(w['id'],)).fetchall(); return out({'workspace':dict(w),'channels':[dict(x) for x in cs],'read_state':{}})
async def channels_create(req):
 u=await auth(req); w=q('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone(); b=await req.json()
 if not w:return out({},404)
 if not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():raise web.HTTPForbidden()
 if not re.fullmatch(r'[a-z0-9-]{1,32}',b.get('name','')):return out({},400)
 try:q('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b['name'],int(bool(b.get('is_private'))),b.get('topic',''))); cid=q('SELECT last_insert_rowid() x').fetchone()['x'];q('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));db.commit()
 except sqlite3.IntegrityError:return out({},409)
 return out({'channel':dict(q('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())},201)
async def join(req):
 u=await auth(req); c=q('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not c: return out({},404)
 q('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(c['id'],u['id']));
 if c['workspace_id'] and not c['is_dm']: q('INSERT OR IGNORE INTO members VALUES(?,?,?)',(c['workspace_id'],u['id'],'member'))
 db.commit(); return out({'channel':channel_obj(c)})
async def members(req):
 u=await auth(req); await require_channel(req,int(req.match_info['id']),u); rows=q('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(req.match_info['id'],)).fetchall(); return out({'members':[userobj(x) for x in rows]})
def resolve_mentions(cid,author,body):
 c=q('SELECT workspace_id FROM channels WHERE id=?',(cid,)).fetchone(); ifnot=[]
 members={x['id'] for x in q('SELECT u.id FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(cid,)).fetchall()}
 online={x['user_id'] for x in q('SELECT user_id FROM online WHERE connections>0').fetchall()}
 found=set()
 for tok in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body):
  if tok=='channel': found |= members
  elif tok=='here': found |= (members & online)
  else:
   u=q('SELECT id FROM users WHERE username=?',(tok,)).fetchone()
   if u and u['id'] in members: found.add(u['id'])
   g=q('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(c['workspace_id'],tok)).fetchone()
   if g: found |= {x['user_id'] for x in q('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()} & members
 return sorted(found-{author})
async def groups(req):
 u=await auth(req); w=q('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w or not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): raise web.HTTPForbidden()
 outg=[]
 for g in q('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall():
  outg.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in q('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]})
 return out({'groups':outg})
async def group_create(req):
 u=await auth(req); w=q('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w or not q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): raise web.HTTPForbidden()
 b=await req.json(); h=b.get('handle','')
 if not re.fullmatch(r'[a-z0-9_-]{2,32}',h) or not b.get('name'): return out({},400)
 try:
  cur=q('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,b['name'],u['id'])); gid=cur.lastrowid
  q('DELETE FROM group_members WHERE group_id=?',(gid,))
  for uid in b.get('user_ids',[]):
   if isinstance(uid,int) and q('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone(): q('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
  db.commit()
 except sqlite3.IntegrityError:return out({},409)
 return out({'group':{'id':gid,'handle':h,'name':b['name'],'member_user_ids':[x['user_id'] for x in q('SELECT user_id FROM group_members WHERE group_id=?',(gid,)).fetchall()]}},201)
async def group_change(req):
 u=await auth(req); w=q('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone(); g=q('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])) if w else None; g=g.fetchone() if g else None
 if not g:
  if req.method=='DELETE': return out({'deleted':True})
  return out({},404)
 role=q('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not role or (g['creator_id']!=u['id'] and role['role']!='owner'):raise web.HTTPForbidden()
 if req.method=='DELETE':
  q('DELETE FROM group_members WHERE group_id=?',(g['id'],)); q('DELETE FROM groups WHERE id=?',(g['id'],)); db.commit(); return out({'deleted':True})
 b=await req.json()
 if 'name' in b:q('UPDATE groups SET name=? WHERE id=?',(b['name'],g['id']))
 if 'user_ids' in b:q('DELETE FROM group_members WHERE group_id=?',(g['id'],));[q('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],x)) for x in b['user_ids']]
 db.commit(); g=q('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone()
 return out({'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in q('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]}})
async def post(req):
 u=await auth(req); cid=int(req.match_info['id']); c=await require_channel(req,cid,u,True); b=await req.json()
 if c['is_archived'] and not (isinstance(b.get('body'),str) and b['body'].startswith('/unarchive')): return out({'error':'archived'},423)
 if isinstance(b.get('body'),str) and b['body'].startswith('/') and not b['body'].startswith('//'):
  return await slash(c,u,b['body'])
 if not isinstance(b.get('body'),str) or not b['body'].strip(): return out({},400)
 fids=b.get('file_ids',[])
 if not isinstance(fids,list) or any(not isinstance(x,int) for x in fids): return out({},400)
 if len(set(fids)) != len(fids): return out({},400)
 for fid in fids:
  f=q('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
  if not f or f['uploader_id']!=u['id'] or q('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone(): return out({},400)
 mentions=resolve_mentions(cid,u['id'],b['body'])
 t=now(); cur=q('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],b['body'],b.get('parent_id'),t)); mid=cur.lastrowid
 for uid in mentions:
  q('INSERT OR IGNORE INTO message_mentions VALUES(?,?)',(mid,uid)); q('INSERT INTO channel_reads(channel_id,user_id,mention_count) VALUES(?,?,1) ON CONFLICT(channel_id,user_id) DO UPDATE SET mention_count=mention_count+1',(cid,uid))
 for fid in fids:q('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',(mid,fid))
 db.commit()
 m=msgobj(q('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone()); n=await event(cid,'message.created',{'type':'message.created','channel_id':cid,'message':m})
 return out({'message':m},201)
def channel_obj(c):
 x=dict(c)
 if 'is_archived' in x: x['is_archived']=bool(x['is_archived'])
 if 'is_private' in x: x['is_private']=bool(x['is_private'])
 if 'is_dm' in x: x['is_dm']=bool(x['is_dm'])
 return x
async def slash(c,u,body):
 parts=body.split(' ',1); cmd=parts[0]; text=parts[1].strip() if len(parts)>1 else ''
 cid=c['id']; wid=c['workspace_id']
 if cmd=='/me':
  if not text:return out({},400)
  return await normal_post(c,u,body,{})
 if cmd=='/shrug':
  return await normal_post(c,u,(text+' ' if text else '')+'¯\\_(ツ)_/¯',{})
 if cmd=='/topic':
  if len(text)>250:return out({},400)
  q('UPDATE channels SET topic=? WHERE id=?',(text,cid)); db.commit(); cc=q('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
  n=await event(cid,'channel.updated',{'type':'channel.updated','channel_id':cid,'channel':channel_obj(cc)})
  return out({'message':None,'channel':channel_obj(cc)},201)
 if cmd=='/invite':
  name=text[1:] if text.startswith('@') else text; target=q('SELECT * FROM users WHERE username=?',(name,)).fetchone()
  if not target:return out({},404)
  q('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,target['id'])); db.commit()
  return out({'message':None,'invited_user':userobj(target)},201)
 if cmd in ('/archive','/unarchive'):
  w=q('SELECT owner_id FROM workspaces WHERE id=?',(wid,)).fetchone()
  if w['owner_id']!=u['id']: raise web.HTTPForbidden()
  val=1 if cmd=='/archive' else 0
  q('UPDATE channels SET is_archived=? WHERE id=?',(val,cid)); db.commit(); cc=q('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
  kind='channel.archived' if val else 'channel.unarchived'
  await event(cid,kind,{'type':kind,'channel_id':cid,'channel':channel_obj(cc)})
  return out({'message':None,'channel':channel_obj(cc)},201)
 return out({},400)
async def normal_post(c,u,body,b):
 cid=c['id']; t=now(); cur=q('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],body,b.get('parent_id'),now())); db.commit()
 m=msgobj(q('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone()); await event(cid,'message.created',{'type':'message.created','channel_id':cid,'message':m})
 return out({'message':m},201)
async def listmsgs(req):
 u=await auth(req); cid=int(req.match_info['id']); await require_channel(req,cid,u); rs=q('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 200',(cid,)).fetchall()
 return out({'messages':[msgobj(x) for x in rs],'next_cursor':None})
async def dm(req):
 u=await auth(req); b=await req.json()
 if 'user_id' not in b:return out({},400)
 other=q('SELECT * FROM users WHERE id=?',(b['user_id'],)).fetchone()
 if not other:return out({},404)
 a,bid=sorted((u['id'],other['id'])); key=f'{a}:{bid}'
 c=q('SELECT * FROM channels WHERE dm_key=?',(key,)).fetchone()
 if not c:
  try:
   q('INSERT INTO channels(name,is_dm,dm_key) VALUES(?,?,?)',(f'dm-{a}-{bid}',1,key)); cid=q('SELECT last_insert_rowid() x').fetchone()['x']; q('INSERT INTO channel_members VALUES(?,?)',(cid,a));q('INSERT INTO channel_members VALUES(?,?)',(cid,bid));db.commit()
  except sqlite3.IntegrityError:
   db.rollback(); c=q('SELECT * FROM channels WHERE dm_key=?',(key,)).fetchone(); cid=c['id']
 else: cid=c['id']
 return out({'channel_id':cid})
async def upload(req):
 u=await auth(req)
 try:
  reader=await req.multipart(); part=None
  while True:
   x=await reader.next()
   if x is None: break
   if x.name=='file': part=x; break
  if part is None:return out({'error':'missing file'},400)
  data=bytearray()
  while True:
   chunk=await part.read_chunk(1024*1024)
   if not chunk:break
   data.extend(chunk)
   if len(data)>10485760:return out({'error':'too large'},413)
 except web.HTTPException: raise
 except Exception:return out({'error':'invalid multipart'},400)
 filename=part.filename or 'file'; ctype=part.headers.get('Content-Type','application/octet-stream')
 cur=q('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],filename,ctype,len(data),now(),'')); fid=cur.lastrowid
 path=f'/app/data/file-{fid}.bin'
 with open(path,'wb') as f:f.write(data)
 q('UPDATE files SET path=? WHERE id=?',(path,fid)); db.commit()
 return out({'file':fileobj(q('SELECT * FROM files WHERE id=?',(fid,)).fetchone())},201)
async def file_meta(req):
 await auth(req); f=q('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone()
 return out({'file':fileobj(f)}) if f else out({},404)
async def file_download(req):
 await auth(req); f=q('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone()
 if not f:return out({},404)
 return web.FileResponse(f['path'],headers={'Content-Type':f['content_type'],'Content-Disposition':f'attachment; filename="{f["filename"]}"'})
async def add_reaction(req):
 u=await auth(req); mid=int(req.match_info['id']); m=q('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
 if not m:return out({},404)
 await require_channel(req,m['channel_id'],u)
 try:b=await req.json()
 except:return out({},400)
 emoji=b.get('emoji') if isinstance(b,dict) else None
 if not isinstance(emoji,str) or not emoji:return out({},400)
 cur=q('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji,created_at) VALUES(?,?,?,?)',(mid,u['id'],emoji,now())); db.commit()
 if cur.rowcount:
  payload={'type':'reaction.added','channel_id':m['channel_id'],'message_id':mid,'emoji':emoji,'user_id':u['id']}
  n=await event(m['channel_id'],'reaction.added',payload)
  payload['event_id']=n
 return out({'reactions':reactions_of(mid)})
async def remove_reaction(req):
 u=await auth(req); mid=int(req.match_info['id']); m=q('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
 if not m:return out({},404)
 await require_channel(req,m['channel_id'],u)
 from urllib.parse import unquote
 emoji=unquote(req.match_info['emoji'])
 cur=q('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)); db.commit()
 if cur.rowcount:
  payload={'type':'reaction.removed','channel_id':m['channel_id'],'message_id':mid,'emoji':emoji,'user_id':u['id']}
  n=await event(m['channel_id'],'reaction.removed',payload); payload['event_id']=n
 return out({'reactions':reactions_of(mid)})
async def read_state(req):
 u=await auth(req); cid=int(req.match_info['id']); await require_channel(req,cid,u)
 r=q('SELECT * FROM channel_reads WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
 if not r:
  q('INSERT OR IGNORE INTO channel_reads(channel_id,user_id) VALUES(?,?)',(cid,u['id'])); db.commit()
  r=q('SELECT * FROM channel_reads WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
 return out({'read_state':{'channel_id':cid,'last_read_event_id':r['last_read_event_id'],'unread_count':0,'mention_count':r['mention_count']}})
async def health(req): return out({'status':'ok','node_id':NODE})
async def fanout(frame):
 dead=[]
 for ws in list(sockets):
  try: await ws.send_json(frame)
  except: dead.append(ws)
 for ws in dead:sockets.discard(ws)
async def ws_handler(req):
 t=req.query.get('token',''); u=q('SELECT user_id FROM tokens WHERE token=?',(t,)).fetchone()
 if not u: raise web.HTTPUnauthorized()
 ws=web.WebSocketResponse(); await ws.prepare(req); sockets.add(ws); q('INSERT INTO online(user_id,connections) VALUES(?,1) ON CONFLICT(user_id) DO UPDATE SET connections=connections+1',(u['user_id'],)); db.commit(); subs=set(); socket_subs[ws]=subs; last={}
 try:
  async for z in ws:
   if z.type!=WSMsgType.TEXT: continue
   b=json.loads(z.data); typ=b.get('type'); cid=int(b.get('channel_id',0) or 0)
   if typ in ('subscribe','resume'):
    c=q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,u['user_id'])).fetchone()
    if not c: continue
    head=q('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone(); head=head['n'] if head else 0
    if typ=='resume':
     since=int(b.get('since_event_id',0))
     for e in q('SELECT event_id,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall():
      await ws.send_str(e['payload'])
     await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
    else:
     subs.add(cid); socket_subs[ws]=subs; last[cid]=head; await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
 finally:
  sockets.discard(ws); socket_subs.pop(ws,None); q('UPDATE online SET connections=MAX(0,connections-1) WHERE user_id=?',(u['user_id'],)); db.commit()
 return ws
async def poll():
 global_seen=0; event_seen=0
 while True:
  try:
   for e in q('SELECT rowid AS rid,payload FROM globals WHERE rowid>? ORDER BY rowid',(global_seen,)).fetchall():
    global_seen=e[0]
    try: await fanout(json.loads(e[1]))
    except Exception as ex: print('global fanout',repr(ex),flush=True)
   for e in q('SELECT rowid AS rid,channel_id,event_id,payload FROM events WHERE rowid>? ORDER BY rowid',(event_seen,)).fetchall():
    event_seen=e[0]; frame=json.loads(e[3])
    for ws in list(sockets):
     if e[1] in socket_subs.get(ws,set()):
      try: await ws.send_json(dict(frame,event_id=e[2]))
      except Exception:
       sockets.discard(ws); socket_subs.pop(ws,None); q('UPDATE online SET connections=MAX(0,connections-1) WHERE user_id=?',(u['user_id'],)); db.commit()
  except Exception as ex:
   print('poll error',repr(ex), 'globals', [dict(x) for x in q('SELECT rowid AS rid,payload FROM globals ORDER BY rowid DESC LIMIT 1').fetchall()], 'events', [dict(x) for x in q('SELECT rowid AS rid,channel_id,event_id,payload FROM events ORDER BY rowid DESC LIMIT 1').fetchall()], flush=True)
  await asyncio.sleep(.05)

def app():
 a=web.Application(client_max_size=12*1024*1024)
 a.router.add_get('/api/health',health); a.router.add_post('/api/auth/register',register); a.router.add_post('/api/auth/login',login); a.router.add_get('/api/auth/me',me)
 a.router.add_get('/api/users/{id}',getuser); a.router.add_patch('/api/users/me',profile)
 a.router.add_get('/api/workspaces/{slug}/groups',groups); a.router.add_post('/api/workspaces/{slug}/groups',group_create); a.router.add_patch('/api/workspaces/{slug}/groups/{handle}',group_change); a.router.add_delete('/api/workspaces/{slug}/groups/{handle}',group_change); a.router.add_get('/api/workspaces',workspaces); a.router.add_post('/api/workspaces',workspace_create); a.router.add_get('/api/workspaces/{slug}',workspace); a.router.add_post('/api/workspaces/{slug}/channels',channels_create)
 a.router.add_post('/api/channels/{id}/join',join); a.router.add_get('/api/channels/{id}/read',read_state); a.router.add_get('/api/channels/{id}/members',members); a.router.add_post('/api/channels/{id}/messages',post); a.router.add_get('/api/channels/{id}/messages',listmsgs)
 a.router.add_post('/api/files',upload); a.router.add_get('/api/files/{id}',file_meta); a.router.add_get('/api/files/{id}/download',file_download); a.router.add_post('/api/dms',dm); a.router.add_post('/api/messages/{id}/reactions',add_reaction); a.router.add_delete('/api/messages/{id}/reactions/{emoji}',remove_reaction); a.router.add_get('/api/ws',ws_handler); return a
def setup(a):
 async def start(app):
  app['poll_task']=asyncio.create_task(poll())
 async def stop(app):
  app['poll_task'].cancel()
 a.on_startup.append(start); a.on_cleanup.append(stop)
 return a
if __name__=='__main__': web.run_app(setup(app()),host='127.0.0.1',port=PORT)
