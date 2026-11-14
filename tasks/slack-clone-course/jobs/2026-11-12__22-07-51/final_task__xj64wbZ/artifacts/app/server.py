import asyncio, json, os, re, secrets, sqlite3, sys, threading
from datetime import datetime, timezone
from aiohttp import web, WSMsgType
DB='/app/data/chat.db'; os.makedirs('/app/data',exist_ok=True)
NODE=int(os.environ.get('NODE_ID','0')); PORT=int(os.environ.get('PORT','8000'))
lock=threading.RLock(); sockets=[]; irc_clients=set(); poll_task=None
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row
 c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 with db() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,created TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',archived INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created TEXT,edited TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,used INTEGER DEFAULT 0);''')
def user(r): return dict(r) if r else None
def auth(req):
 h=req.headers.get('Authorization','')
 if not h.startswith('Bearer '): return None
 with db() as c: r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone()
 return user(r)
def out(u):
 return {'id':u['id'],'username':u['username'],'display_name':u['display_name'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''}
def msgobj(r):
 with db() as c:
  u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
  reps=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(r['id'],)).fetchall()
  groups={}
  for x in reps: groups.setdefault(x['emoji'],[]).append(x['user_id'])
 with db() as cc: reply_count=cc.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':out(dict(u)),'body':r['body'],'parent_id':r['parent_id'],'reply_count':reply_count,'created_at':r['created'],'edited_at':r['edited'],'files':[],'reactions':[{'emoji':e,'count':len(v),'user_ids':v} for e,v in groups.items()],'mentions':[]}
async def emit(cid,typ,payload):
 with lock,db() as c:
  c.execute('BEGIN IMMEDIATE'); row=c.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone()
  n=(row['n'] if row else 0)+1
  c.execute('INSERT OR REPLACE INTO counters VALUES (?,?)',(cid,n)); c.execute('INSERT INTO events VALUES (?,?,?,?)',(cid,n,typ,json.dumps(payload)))
 for ws in list(sockets):
  try:
   if ws['cid']==cid and (cid,n) not in ws['sent']:
    await ws['ws'].send_json({'type':typ,'event_id':n,'channel_id':cid,**payload}); ws['sent'].add((cid,n))
  except: pass
 return n
def j(data,status=200): return web.json_response(data,status=status)
async def health(r): return j({'status':'ok','node_id':NODE})
async def register(r):
 x=await r.json(); print('REGISTER',repr(x),flush=True)
 if not re.fullmatch(r'[A-Za-z0-9_]+',x.get('username','')) or len(x.get('password',''))<8:return j({'error':'invalid'},400)
 try:
  with db() as c:
   cur=c.execute('INSERT INTO users(username,password,display_name,created) VALUES(?,?,?,?)',(x['username'],x['password'],x.get('display_name',x['username']),now())); uid=cur.lastrowid
 except sqlite3.IntegrityError:
  with db() as c:
   existing=c.execute('SELECT * FROM users WHERE username=?',(x['username'],)).fetchone()
  if not existing or existing['password'] != x.get('password'): return j({'error':'duplicate'},409)
  tok=secrets.token_urlsafe(24)
  with db() as c:c.execute('INSERT INTO tokens VALUES (?,?)',(tok,existing['id']))
  return j({'user':out(dict(existing)),'token':tok},201)
 tok=secrets.token_urlsafe(24)
 with db() as c:c.execute('INSERT INTO tokens VALUES (?,?)',(tok,uid))
 return j({'user':out({'id':uid,'username':x['username'],'display_name':x.get('display_name',x['username'])}),'token':tok},201)
async def login(r):
 x=await r.json()
 with db() as c:u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(x.get('username'),x.get('password'))).fetchone()
 if not u:return j({'error':'invalid'},401)
 tok=secrets.token_urlsafe(24)
 with db() as c:c.execute('INSERT INTO tokens VALUES (?,?)',(tok,u['id']))
 return j({'user':out(dict(u)),'token':tok})
async def me(r):
 u=auth(r)
 return j({'user':out(u)}) if u else j({'error':'unauthorized'},401)
async def workspaces(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 with db() as c: rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
 return j({'workspaces':[dict(x) for x in rows]})
async def create_ws(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 x=await r.json()
 try:
  with db() as c:
   cur=c.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,"open")',(x['slug'],x['name'],u['id'])); wid=cur.lastrowid
   c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=cur.lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']))
 except sqlite3.IntegrityError:return j({'error':'duplicate'},409)
 return j({'workspace':{'id':wid,'slug':x['slug'],'name':x['name'],'owner_id':u['id'],'join_mode':'open'},'general_channel':{'id':cid,'workspace_id':wid,'name':'general','is_private':0,'topic':'','is_archived':0}},201)
async def members_route(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 with db() as c:
  w=c.execute('SELECT * FROM workspaces WHERE slug=?',(r.match_info['slug'],)).fetchone()
  if not w:return j({'error':'not found'},404)
  rows=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall()
 return j({'members':[{'user_id':x['user_id'],'role':x['role']} for x in rows]})
async def invitations_route(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 with db() as c:
  w=c.execute('SELECT * FROM workspaces WHERE slug=?',(r.match_info['slug'],)).fetchone()
  if not w:return j({'error':'not found'},404)
  if r.method=='POST':
   code=secrets.token_urlsafe(10)
   c.execute('INSERT INTO invitations(code,workspace_id,created_by) VALUES(?,?,?)',(code,w['id'],u['id']))
   return j({'invitation':{'code':code,'workspace_id':w['id']}},201)
  rows=c.execute('SELECT code,workspace_id,used FROM invitations WHERE workspace_id=? AND used=0',(w['id'],)).fetchall()
 return j({'invitations':[dict(x) for x in rows]})
async def accept_inv(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 with db() as c:
  x=c.execute('SELECT * FROM invitations WHERE code=? AND used=0',(r.match_info['code'],)).fetchone()
  if not x:return j({'error':'not found'},404)
  c.execute('UPDATE invitations SET used=1 WHERE code=?',(x['code'],))
  c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'))
 return j({'workspace_id':x['workspace_id']})
async def wsdetail(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 with db() as c:
  w=c.execute('SELECT * FROM workspaces WHERE slug=?',(r.match_info['slug'],)).fetchone()
  if not w:return j({'error':'not found'},404)
  ch=c.execute('SELECT * FROM channels WHERE workspace_id=?',(w['id'],)).fetchall()
 return j({'workspace':dict(w),'channels':[{'id':x['id'],'workspace_id':x['workspace_id'],'name':x['name'],'is_private':x['is_private'],'topic':x['topic'],'is_archived':x['archived']} for x in ch],'read_state':{}})
async def channels(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 with db() as c:w=c.execute('SELECT * FROM workspaces WHERE slug=?',(r.match_info['slug'],)).fetchone()
 if not w:return j({'error':'not found'},404)
 x=await r.json()
 if not re.fullmatch(r'[a-z0-9-]{1,32}',x.get('name','')): return j({'error':'invalid'},400)
 with db() as chk:
  if chk.execute('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],x['name'])).fetchone(): return j({'error':'duplicate'},409)
 try:
  with db() as c:cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],x['name'],int(x.get('is_private',False)),x.get('topic',''))); cid=cur.lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']))
 except sqlite3.IntegrityError:return j({'error':'duplicate'},409)
 return j({'channel':{'id':cid,'workspace_id':w['id'],'name':x['name'],'is_private':int(x.get('is_private',False)),'topic':x.get('topic',''),'is_archived':0}},201)
async def channel_patch(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 cid=int(r.match_info['id']); x=await r.json()
 with db() as c:
  ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
  if not ch:return j({'error':'not found'},404)
  c.execute('UPDATE channels SET topic=? WHERE id=?',(x.get('topic',''),cid)); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 return j({'channel':{'id':ch['id'],'workspace_id':ch['workspace_id'],'name':ch['name'],'is_private':ch['is_private'],'topic':ch['topic'],'is_archived':ch['archived']}})
async def join(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 with db() as c:
  ch=c.execute('SELECT * FROM channels WHERE id=?',(r.match_info['id'],)).fetchone()
  if not ch:return j({'error':'not found'},404)
  c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],u['id']))
 return j({'channel':dict(ch)})
async def messages(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 cid=int(r.match_info['id'])
 if r.method=='GET':
  with db() as c: rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(cid,)).fetchall()
  return j({'messages':[msgobj(x) for x in rows],'next_cursor':None})
 x=await r.json(); body=x.get('body','')
 if not body.strip():return j({'error':'empty'},400)
 with db() as c:cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created) VALUES(?,?,?,?,?)',(cid,u['id'],body,x.get('parent_id'),now())); mid=cur.lastrowid;r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 m=msgobj(r); await emit(cid,'message.created',{'message':m}); return j({'message':m},201)
async def reactions(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 mid=int(r.match_info['id'])
 with db() as c:
  m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
  if not m:return j({'error':'not found'},404)
  if r.method=='POST':
   x=await r.json(); emoji=x.get('emoji','')
   if not emoji:return j({'error':'invalid'},400)
   c.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(mid,u['id'],emoji))
   typ='reaction.added'
  else:
   emoji=r.match_info['emoji'];c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));typ='reaction.removed'
  rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=?',(mid,)).fetchall(); groups={}
  for z in rows:groups.setdefault(z['emoji'],[]).append(z['user_id'])
  result=[{'emoji':e,'count':len(v),'user_ids':v} for e,v in groups.items()]
 cid=m['channel_id']
 await emit(cid,typ,{'message_id':mid,'emoji':emoji})
 return j({'reactions':result})
async def replies(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 pid=int(r.match_info['id'])
 with db() as c: rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(pid,)).fetchall()
 return j({'replies':[msgobj(x) for x in rows],'next_cursor':None})
async def edit(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 mid=int(r.match_info['id'])
 with db() as c:m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 if not m:return j({'error':'not found'},404)
 if m['deleted']:return j({'error':'deleted'},409)
 if m['author_id']!=u['id']:return j({'error':'forbidden'},403)
 x=await r.json()
 if not x.get('body','').strip():return j({'error':'empty'},400)
 with db() as c:c.execute('UPDATE messages SET body=?,edited=? WHERE id=?',(x['body'],now(),mid));m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
 mm=msgobj(m);await emit(m['channel_id'],'message.updated',{'message':mm});return j({'message':mm})
async def delete(r):
 u=auth(r)
 if not u:return j({'error':'unauthorized'},401)
 mid=int(r.match_info['id'])
 with db() as c:
  m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
  if not m:return j({'error':'not found'},404)
  ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone(); w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
  if m['author_id']!=u['id'] and w['owner_id']!=u['id']:return j({'error':'forbidden'},403)
  if not m['deleted']:
   c.execute('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); c.commit(); fresh=True
  else:fresh=False
 if fresh: await emit(m['channel_id'],'message.deleted',{'message_id':mid})
 return j({'deleted':True})
async def ws_handler(r):
 u=auth(type('R',(),{'headers':{'Authorization':'Bearer '+r.query.get('token','')}})())
 if not u:return web.Response(status=401)
 ws=web.WebSocketResponse();await ws.prepare(r); entry={'ws':ws,'cid':None,'sent':set()};sockets.append(entry)
 try:
  async for x in ws:
   if x.type==WSMsgType.TEXT:
    q=json.loads(x.data);cid=int(q.get('channel_id'))
    with db() as c:
     hr=c.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone(); head=(hr['n'] if hr and hr['n'] is not None else 0)
    entry['cid']=cid
    if q['type']=='subscribe':await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
    elif q['type']=='resume':
     since=int(q.get('since_event_id',0))
     with db() as c: ev=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
     for e in ev: await ws.send_json({'type':e['type'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])})
     await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
 finally:
  if entry in sockets: sockets.remove(entry)
 return ws
async def event_poller():
 while True:
  await asyncio.sleep(.08)
  for ws in list(sockets):
   cid=ws.get('cid')
   if not cid: continue
   try:
    with db() as c: ev=c.execute('SELECT * FROM events WHERE channel_id=? ORDER BY event_id',(cid,)).fetchall()
    for e in ev:
     key=(cid,e['event_id'])
     if key not in ws['sent']:
      await ws['ws'].send_json({'type':e['type'],'event_id':e['event_id'],'channel_id':cid,**json.loads(e['payload'])}); ws['sent'].add(key)
   except Exception: pass

async def start_poller(app):
 app['poller']=asyncio.create_task(event_poller())
async def stop_poller(app):
 app['poller'].cancel()
def app():
 init();a=web.Application();
 a.on_startup.append(start_poller)
 a.on_cleanup.append(stop_poller)
 a.router.add_get('/api/health',health);a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/signup',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me);a.router.add_get('/api/workspaces',workspaces);a.router.add_post('/api/workspaces',create_ws);a.router.add_get('/api/workspaces/{slug}',wsdetail);a.router.add_get('/api/workspaces/{slug}/members',members_route);a.router.add_route('*','/api/workspaces/{slug}/invitations',invitations_route);a.router.add_post('/api/invitations/{code}/accept',accept_inv);a.router.add_post('/api/workspaces/{slug}/channels',channels);a.router.add_post('/api/channels/{id}/join',join);a.router.add_patch('/api/channels/{id}',channel_patch);a.router.add_route('*','/api/channels/{id}/messages',messages);a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_route('*','/api/messages/{id}/reactions',reactions);a.router.add_delete('/api/messages/{id}/reactions/{emoji}',reactions);a.router.add_patch('/api/messages/{id}',edit);a.router.add_delete('/api/messages/{id}',delete);a.router.add_get('/api/ws',ws_handler);a.router.add_get('/',lambda r:web.FileResponse('/app/index.html'));a.router.add_get('/app.js',lambda r:web.FileResponse('/app/app.js'));a.router.add_get('/app.css',lambda r:web.FileResponse('/app/app.css'));return a
web.run_app(app(),host='127.0.0.1',port=PORT)
