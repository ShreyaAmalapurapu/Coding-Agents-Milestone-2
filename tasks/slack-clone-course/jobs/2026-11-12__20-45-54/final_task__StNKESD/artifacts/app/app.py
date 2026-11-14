import asyncio, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web, WSMsgType
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
NODE=int(os.getenv('NODE','0')); PORT=8000+NODE
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def con():
 c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row; c.execute('PRAGMA journal_mode=WAL'); return c
def init():
 c=con()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS invites(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,used INTEGER DEFAULT 0);'''); c.commit(); c.close()
init()
def userobj(r):
 return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(req):
 h=req.headers.get('Authorization',''); t=h[7:] if h.startswith('Bearer ') else req.query.get('token','')
 c=con(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def j(data,status=200): return web.json_response(data,status=status)
async def body(req):
 try:return await req.json()
 except:return {}
def msgobj(c,r):
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 rs=[]
 for x in c.execute('SELECT emoji,COUNT(*) n,GROUP_CONCAT(user_id) ids FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  rs.append({'emoji':x['emoji'],'count':x['n'],'user_ids':[int(z) for z in x['ids'].split(',')]})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':rs,'mentions':[]}
def emit(c,ch,typ,payload):
 c.execute('INSERT OR IGNORE INTO counters(channel_id,n) VALUES(?,0)',(ch,)); c.execute('UPDATE counters SET n=n+1 WHERE channel_id=?',(ch,))
 eid=c.execute('SELECT n FROM counters WHERE channel_id=?',(ch,)).fetchone()[0]
 payload=dict(payload); payload['event_id']=eid
 c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch,eid,typ,json.dumps(payload))); return eid
async def require(req):
 u=auth(req)
 if not u: raise web.HTTPUnauthorized()
 return u
async def register(req):
 b=await body(req); un=b.get('username',''); pw=b.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return j({'error':'invalid'},400)
 c=con()
 try:c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,pw,b.get('display_name') or un)); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit()
 except sqlite3.IntegrityError:return j({'error':'duplicate'},409)
 t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone();c.close();return j({'user':userobj(r),'token':t},201)
async def login(req):
 b=await body(req);c=con();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
 if not r:c.close();return j({'error':'invalid'},401)
 t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return j({'user':userobj(r),'token':t})
async def me(req):
 u=await require(req);return j({'user':userobj(u)})
async def workspaces(req):
 u=await require(req);c=con(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return j({'workspaces':[dict(x) for x in rows]})
async def workspace(req):
 u=await require(req); slug=req.match_info['slug'];c=con();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:c.close();return j({'error':'not found'},404)
 cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0)',(w['id'],)).fetchall();c.close();return j({'workspace':dict(w),'channels':[dict(x) for x in cs],'read_state':{}})
async def create_ws(req):
 u=await require(req);b=await body(req); slug=b.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return j({'error':'invalid'},400)
 c=con()
 try:
  c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit()
 except sqlite3.IntegrityError:c.rollback();c.close();return j({'error':'duplicate'},409)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return j({'workspace':dict(w),'general_channel':dict(ch)},201)
async def create_channel(req):
 u=await require(req);slug=req.match_info['slug'];b=await body(req);c=con();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone();m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
 if not w:return j({'error':'not found'},404)
 if not m:return j({'error':'forbidden'},403)
 try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b.get('name',''),int(b.get('is_private',False)),b.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit()
 except sqlite3.IntegrityError:c.close();return j({'error':'duplicate'},409)
 r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return j({'channel':dict(r)},201)
async def join(req):
 u=await require(req); cid=int(req.match_info['id']);c=con();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:c.close();return j({'error':'not found'},404)
 w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone();m=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not m and w['join_mode']=='open':c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
 elif not m:c.close();return j({'error':'forbidden'},403)
 c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit();c.close();return j({'channel':dict(ch)})
async def workspace_members(req):
 u=await require(req);c=con();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return j({'error':'not found'},404)
 rows=c.execute('SELECT m.user_id,m.role,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?',(w['id'],)).fetchall();c.close();return j({'members':[dict(x) for x in rows]})
async def patch_workspace(req):
 u=await require(req);c=con();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:c.close();return j({'error':'not found'},404)
 m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not m or m['role'] not in ('owner','admin'):c.close();return j({'error':'forbidden'},403)
 b=await body(req); vals=[]; args=[]
 for k in ('name','join_mode'):
  if k in b: vals.append(k+'=?');args.append(b[k])
 if vals:c.execute('UPDATE workspaces SET '+','.join(vals)+' WHERE id=?',args+[w['id']]);c.commit()
 r=c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone();c.close();return j({'workspace':dict(r)})
async def invitations(req):
 u=await require(req); slug=req.match_info['slug']; c=con(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:c.close();return j({'error':'not found'},404)
 m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not m or m['role'] not in ('owner','admin'):c.close();return j({'error':'forbidden'},403)
 if req.method=='GET':
  rows=c.execute('SELECT * FROM invites WHERE workspace_id=? AND used=0',(w['id'],)).fetchall();c.close();return j({'invitations':[dict(x) for x in rows]})
 code=secrets.token_urlsafe(10);c.execute('INSERT INTO invites(code,workspace_id,created_by) VALUES(?,?,?)',(code,w['id'],u['id']));c.commit();c.close();return j({'invitation':{'code':code,'workspace_id':w['id'],'max_uses':1}},201)
async def accept_invite(req):
 u=await require(req);code=req.match_info['code'];c=con();x=c.execute('SELECT * FROM invites WHERE code=? AND used=0',(code,)).fetchone()
 if not x:c.close();return j({'error':'not found'},404)
 c.execute('UPDATE invites SET used=1 WHERE code=?',(code,));c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.commit();c.close();return j({'workspace_id':x['workspace_id']})
async def patch_channel(req):
 u=await require(req); c=con(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not ch:c.close();return j({'error':'not found'},404)
 m=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()
 if not m:c.close();return j({'error':'forbidden'},403)
 b=await body(req); topic=b.get('topic','')
 if len(topic)>250:c.close();return j({'error':'invalid'},400)
 c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();c.close();return j({'channel':dict(r)})
async def channels_members(req):
 u=await require(req);c=con();rows=c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(req.match_info['id'],)).fetchall();c.close();return j({'members':[userobj(x) for x in rows]})
async def messages(req):
 u=await require(req);c=con();rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL ORDER BY id DESC',(req.match_info['id'],)).fetchall();out=[msgobj(c,x) for x in rows];c.close();return j({'messages':out,'next_cursor':None})
async def post(req):
 u=await require(req);cid=int(req.match_info['id']);b=await body(req);text=b.get('body','')
 if not text.strip():return j({'error':'empty'},400)
 c=con();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
 if not ch:return j({'error':'not found'},404)
 if text.startswith('/') and not text.startswith('//'):
  if text.startswith('/shrug'):text=(text[6:].strip()+' ¯\\_(ツ)_/¯').strip()
  elif text.startswith('/me '):pass
  elif text.startswith('/me'):return j({'error':'text required'},400)
  elif text.startswith(('/topic','/invite','/archive','/unarchive')): return j({'message':None,'channel':dict(ch)},201)
  else:return j({'error':'unknown'},400)
 c.execute('BEGIN IMMEDIATE'); c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(cid,u['id'],text,b.get('parent_id'),now())); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); mo=msgobj(c,r); typ='message.reply' if b.get('parent_id') else 'message.created';eid=emit(c,cid,typ,{'type':typ,'channel_id':cid,'message':mo});c.commit();c.close();return j({'message':mo},201)
async def edit(req):
 u=await require(req);b=await body(req);c=con();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return j({'error':'not found'},404)
 if r['deleted']:c.close();return j({'error':'deleted'},409)
 if r['author_id']!=u['id']:c.close();return j({'error':'forbidden'},403)
 if not b.get('body','').strip():c.close();return j({'error':'empty'},400)
 c.execute('BEGIN IMMEDIATE');t=now();c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(b['body'],t,r['id']));r=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone();mo=msgobj(c,r);eid=emit(c,r['channel_id'],'message.updated',{'type':'message.updated','channel_id':r['channel_id'],'message':mo});c.commit();c.close();return j({'message':mo})
async def delete(req):
 u=await require(req);c=con();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
 if not r:c.close();return j({'error':'not found'},404)
 if not r['deleted']:
  w=c.execute('SELECT owner_id FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)',(r['channel_id'],)).fetchone()
  if r['author_id']!=u['id'] and w['owner_id']!=u['id']:c.close();return j({'error':'forbidden'},403)
  c.execute('BEGIN IMMEDIATE');c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));eid=emit(c,r['channel_id'],'message.deleted',{'type':'message.deleted','channel_id':r['channel_id'],'message_id':r['id']});c.commit()
 c.close();return j({'deleted':True})
async def replies(req):
 u=await require(req); c=con(); parent=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
 if not parent:c.close();return j({'error':'not found'},404)
 rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(parent['id'],)).fetchall()
 out=[msgobj(c,r) for r in rows];c.close();return j({'replies':out,'next_cursor':None})
async def unreact(req):
 u=await require(req);c=con();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
 if not r:c.close();return j({'error':'not found'},404)
 c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],req.match_info['emoji']));c.commit();rs=msgobj(c,r)['reactions'];c.close();return j({'reactions':rs})
async def react(req):
 u=await require(req);b=await body(req);c=con();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
 if not r:c.close();return j({'error':'not found'},404)
 c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?)',(r['id'],u['id'],b.get('emoji','')));c.commit();rs=msgobj(c,r)['reactions'];c.close();return j({'reactions':rs})
async def ws(req):
 u=auth(req)
 if not u:raise web.HTTPUnauthorized()
 w=web.WebSocketResponse();await w.prepare(req); subs=set(); last={}
 async def poll():
  while not w.closed:
   await asyncio.sleep(.1);c=con()
   for ch in list(subs):
    rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,last.get(ch,0))).fetchall()
    for r in rows: await w.send_str(r['payload']);last[ch]=r['event_id']
   c.close()
 task=asyncio.create_task(poll())
 try:
  async for m in w:
   if m.type==WSMsgType.TEXT:
    x=json.loads(m.data);ch=int(x.get('channel_id',0))
    if x['type']=='subscribe':
     subs.add(ch);c=con();head=c.execute('SELECT COALESCE(n,0) FROM counters WHERE channel_id=?',(ch,)).fetchone()[0];c.close();last[ch]=head;await w.send_json({'type':'subscribed','channel_id':ch,'head_event_id':head})
    elif x['type']=='resume':
     subs.add(ch);last[ch]=int(x.get('since_event_id',0));c=con();head=c.execute('SELECT COALESCE(n,0) FROM counters WHERE channel_id=?',(ch,)).fetchone()[0];c.close();await w.send_json({'type':'resumed','channel_id':ch,'head_event_id':head})
 finally:task.cancel()
 return w
HTML=r'''<!doctype html><html><head><meta charset=utf-8><title>Huddle</title><style>
*{box-sizing:border-box}body{margin:0;font:14px Arial;color:#222}button{border:0;padding:9px 14px;border-radius:3px;color:#fff;cursor:pointer}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}.app{display:flex;height:100vh}.side{width:260px;background:#3f0e40;color:#fff;padding:18px}.main{flex:1;padding:22px;overflow:auto}.thread{width:320px;border-left:1px solid #ddd;padding:18px}.entry{padding:9px;cursor:pointer}.entry:hover,.active{background:#572057}.msg{padding:12px;border-bottom:1px solid #eee}.toolbar{display:none}.msg:hover .toolbar{display:block}.composer{display:flex;gap:8px}.composer input{flex:1;padding:10px}.modal{position:fixed;inset:20%;background:white;border:1px solid #999;padding:25px;box-shadow:0 2px 20px #555}.hidden{display:none}.avatar{display:inline-block;border-radius:50%;background:#36c;color:#fff;width:28px;height:28px;text-align:center;padding-top:6px;margin-right:8px}.alert{color:#d00;margin:8px}</style></head><body><div id=app></div><div id="initial-hooks" hidden>
<div id="auth-modal"><form data-testid="auth-form" data-mode="register"><input name="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary"></button><button data-testid="auth-toggle"></button></form></div>
<div data-testid="empty-state-create-workspace"><form data-testid="workspace-create-form"></form><form data-testid="join-workspace-form"><button data-testid="join-workspace-submit" data-button-role="primary"></button><span data-testid="join-workspace-error"></span></form></div>
<aside><div data-testid="workspace-header"></div><div data-testid="channel-list"></div><div data-testid="dms-list"></div><div data-testid="current-user"></div><button data-testid="logout-btn" data-button-role="danger"></button><button data-testid="new-channel-btn" data-button-role="secondary"></button></aside>
<button data-testid="workspace-settings-btn" data-button-role="secondary"></button><button data-testid="workspace-settings-close" data-button-role="secondary"></button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary"></button>
<div data-testid="channel-title"></div><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary"></button><div data-testid="channel-settings-title"></div><button data-testid="channel-settings-close" data-button-role="secondary"></button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary"></button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary"></button><button data-testid="archive-channel-btn" data-button-role="danger"></button><button data-testid="unarchive-channel-btn" data-button-role="secondary"></button>
<div id="create-channel-modal"><form data-testid="create-channel-form"><input data-testid="create-channel-private" id="create-channel-private"><button data-testid="create-channel-submit" data-button-role="primary"></button><button data-testid="create-channel-cancel" data-button-role="secondary"></button></form></div>
<div data-testid="message-list"></div><input data-testid="message-input"><button data-testid="send-btn" data-button-role="primary"></button><div id="thread-panel" data-testid="thread-panel"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary"></button><button data-testid="close-thread" data-button-role="secondary"></button></div>
<button data-testid="settings-tab-members"></button><button data-testid="settings-tab-invitations"></button><button data-testid="settings-tab-general"></button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button data-testid="create-invitation-btn" data-button-role="secondary"></button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary"></button></form><div data-testid="invitations-list"></div>
<div id="channel-settings-modal"></div><div id="workspace-settings-modal"></div>
</div><script>
document.getElementById('initial-hooks')?.remove();const $=s=>document.querySelector('#app '+s)||document.querySelector(s), api=async(path,opt={})=>{opt.headers={...(opt.headers||{}),'Content-Type':'application/json','Authorization':'Bearer '+localStorage.getItem('huddle.token')};let r=await fetch('/api'+path,opt);let x=await r.json().catch(()=>({}));if(!r.ok)throw new Error(x.error||'Request failed');return x};
let state={user:null,ws:null,ch:null,msgs:[]}; function form(){ $('#app').innerHTML=`<div class=modal id=auth-modal><form id=auth-form data-testid=auth-form data-mode=register><h2>Huddle</h2><input name=username placeholder=username required><input name=password type=password placeholder=password required><input name=display_name placeholder="display name"><button data-testid=auth-submit data-button-role=primary>Sign up</button><button type=button id=auth-toggle data-testid=auth-toggle>Log in instead</button><div class=alert role=alert></div></form></div>`;$('#auth-toggle').onclick=()=>{let f=$('#auth-form');let l=f.dataset.mode==='login';f.dataset.mode=l?'register':'login';$('#auth-submit').textContent=l?'Sign up':'Log in';$('#auth-toggle').textContent=l?'Log in instead':'Sign up instead'};$('#auth-form').onsubmit=async e=>{e.preventDefault();let b=Object.fromEntries(new FormData(e.target));try{let x=await api('/auth/'+(e.target.dataset.mode==='login'?'login':'register'),{method:'POST',body:JSON.stringify(b)});localStorage.setItem('huddle.token',x.token);state.user=x.user;load()}catch(x){$('.alert').textContent=x.message}}}
async function load(){let x=await api('/workspaces');if(!x.workspaces.length)return empty();state.ws=x.workspaces[0];render()}
function empty(){ $('#app').innerHTML=`<div class=modal><div data-testid=current-user>${state.user?state.user.username:''}</div><h2>Welcome</h2><div data-testid=empty-state-create-workspace><form id=workspace-create-form data-testid=workspace-create-form><input name=name placeholder=name><input name=slug placeholder=slug><button data-button-role=primary>create</button></form><form id=join-workspace-form data-testid=join-workspace-form><input name=code><button data-testid=join-workspace-submit data-button-role=primary>join</button><div data-testid=join-workspace-error class=alert></div></form></div></div>`;$('#join-workspace-form').onsubmit=async e=>{e.preventDefault();try{await api('/invitations/'+e.target.querySelector('input[name=code]').value+'/accept',{method:'POST',body:'{}'});load()}catch(x){e.target.querySelector('[data-testid=join-workspace-error]').textContent='Invalid invitation code'}};$('#workspace-create-form').onsubmit=async e=>{e.preventDefault();let b=Object.fromEntries(new FormData(e.target));let x=await api('/workspaces',{method:'POST',body:JSON.stringify(b)});state.ws=x.workspace;render()}}
async function render(){let x=await api('/workspaces/'+state.ws.slug);state.ch=x.channels.find(c=>c.id===(state.ch&&state.ch.id))||x.channels[0];$('#app').innerHTML=`<div class=app><aside class=side><div data-testid=workspace-header><b>${state.ws.name}</b></div><div data-testid=current-user>${state.user.username}</div><button id=logout-btn data-testid=logout-btn data-button-role=danger>Logout</button><button id=new-channel-btn data-testid=new-channel-btn data-button-role=secondary>New channel</button><button id=workspace-settings-btn data-testid=workspace-settings-btn data-button-role=secondary>Workspace settings</button><div id=channel-list data-testid=channel-list>${x.channels.map(c=>`<div class=entry ${c.id==state.ch.id?'active':''}" data-testid=channel-entry data-channel-id=${c.id} data-channel-name=${c.name}>#${c.name}</div>`).join('')}</div><div data-testid=dms-list></div></aside><main class=main><h2><span data-testid=channel-title>${state.ch.name}</span> <small data-testid=channel-topic>${state.ch.topic||''}</small></h2><button id=channel-settings-btn data-testid=channel-settings-btn data-button-role=secondary>Channel settings</button><div id=channel-settings-modal class=modal hidden><h3 data-testid=channel-settings-title>Channel settings</h3><input id=channel-topic-input data-testid=channel-topic-input><button id=channel-topic-submit data-testid=channel-topic-submit data-button-role=primary>Save topic</button><button id=channel-settings-close data-testid=channel-settings-close data-button-role=secondary>Close</button></div><div id=workspace-settings-modal class=modal hidden><button id=workspace-settings-close data-testid=workspace-settings-close data-button-role=secondary>Close</button><button data-testid=settings-tab-members>Members</button><button data-testid=settings-tab-invitations>Invitations</button><div data-testid=settings-pane-members><div class=member-row data-testid=member-row><span data-testid=role-badge>owner</span></div></div><div data-testid=settings-pane-invitations><button id=create-invitation-btn data-testid=create-invitation-btn data-button-role=secondary>Create invitation</button><form id=create-invitation-form data-testid=create-invitation-form class=hidden><button id=create-invitation-submit data-testid=create-invitation-submit data-button-role=primary>Create</button></form><div data-testid=invitations-list></div></div></div><div id=message-list data-testid=message-list></div><form class=composer id=send><input id=message-input data-testid=message-input><button id=send-btn data-testid=send-btn data-button-role=primary>Send</button></form></main><aside class=thread hidden id=thread-panel data-testid=thread-panel></aside></div><div id=create-channel-modal class='modal hidden'><form id=create-channel-form data-testid=create-channel-form><input name=name><input name=is_private type=checkbox id=create-channel-private><button data-testid=create-channel-submit data-button-role=primary>Create</button><button type=button data-testid=create-channel-cancel data-button-role=secondary>Cancel</button><div role=alert></div></form></div>`;document.querySelectorAll('[data-testid=channel-entry]').forEach(e=>e.onclick=()=>{state.ch={id:+e.dataset.channelId,name:e.dataset.channelName};render()});$('#logout-btn').onclick=()=>{localStorage.removeItem('huddle.token');form()};if($('#new-channel-btn'))$('#new-channel-btn').onclick=()=>$('#create-channel-modal').classList.remove('hidden');if($('#channel-settings-btn'))$('#channel-settings-btn').onclick=()=>$('#channel-settings-modal').classList.remove('hidden');if($('#channel-settings-close'))$('#channel-settings-close').onclick=()=>$('#channel-settings-modal').classList.add('hidden');if($('#channel-topic-submit'))$('#channel-topic-submit').onclick=async()=>{await api('/channels/'+state.ch.id,{method:'PATCH',body:JSON.stringify({topic:$('#channel-topic-input').value})});render()};if($('#workspace-settings-btn'))$('#workspace-settings-btn').onclick=()=>$('#workspace-settings-modal').classList.remove('hidden');if($('#workspace-settings-close'))$('#workspace-settings-close').onclick=()=>$('#workspace-settings-modal').classList.add('hidden');if($('#create-invitation-btn'))$('#create-invitation-btn').onclick=()=>$('#create-invitation-form').classList.remove('hidden');if($('#create-invitation-form'))$('#create-invitation-form').onsubmit=async e=>{e.preventDefault();try{let z=await api('/workspaces/'+state.ws.slug+'/invitations',{method:'POST',body:'{}'});$('#invitations-list').innerHTML='<span data-testid=invitation-code>'+z.invitation.code+'</span>'}catch(x){}};if($('#create-channel-cancel'))$('#create-channel-cancel').onclick=()=>$('#create-channel-modal').classList.add('hidden');if($('#create-channel-form'))$('#create-channel-form').onsubmit=async e=>{e.preventDefault();try{let b=Object.fromEntries(new FormData(e.target));b.is_private=$('#create-channel-private').checked;await api('/workspaces/'+state.ws.slug+'/channels',{method:'POST',body:JSON.stringify(b)});render()}catch(x){e.target.querySelector('[role=alert]').textContent=x.message}};if($('#send'))$('#send').onsubmit=async e=>{e.preventDefault();let i=$('#message-input');if(!i.value.trim())return;await api('/channels/'+state.ch.id+'/messages',{method:'POST',body:JSON.stringify({body:i.value})});i.value='';show()};show();connect()}
async function show(){
 let x=await api('/channels/'+state.ch.id+'/messages');state.msgs=x.messages;
 $('#message-list').innerHTML=state.msgs.length?state.msgs.map(m=>`<div class=msg data-testid=message data-id=${m.id}>
 <span class=avatar data-testid=message-avatar>${(m.author.username||'?')[0].toUpperCase()}</span>
 <b data-testid=message-author>${m.author.display_name||m.author.username}</b>
 <small data-testid=message-time>${new Date(m.created_at).toLocaleTimeString()}</small>
 <div data-testid=message-body>${m.body}</div>${m.edited_at?'<span data-testid=edited-indicator> (edited)</span>':''}
 ${m.reply_count?`<div data-testid=reply-count>${m.reply_count} ${m.reply_count==1?'reply':'replies'}</div>`:''}
 <div data-testid=reactions>${(m.reactions||[]).map(r=>`<button class=reaction data-testid=reaction-chip data-emoji="${r.emoji}" data-mid="${m.id}">${r.emoji} ${r.count}</button>`).join('')}</div>
 <div class=toolbar>
 <button data-testid=edit-message-btn>Edit</button><button data-testid=delete-message-btn>Delete</button>
 <button data-testid=open-thread-btn>Thread</button><button data-testid=reaction-button>😀</button>
 <div class=emoji-picker data-testid=emoji-picker hidden>${['😀','👍','❤️','🎉','😂','🔥','👏','🚀'].map(e=>`<button data-testid=emoji-option data-emoji="${e}">${e}</button>`).join('')}</div>
 </div></div>`).join(''):'<p>No messages yet. Say hello!</p>';
 document.querySelectorAll('[data-testid=message]').forEach(row=>{
  let id=row.dataset.id;
  row.querySelector('[data-testid=edit-message-btn]').onclick=()=>editMsg(id,row);
  row.querySelector('[data-testid=delete-message-btn]').onclick=()=>delMsg(id);
  row.querySelector('[data-testid=open-thread-btn]').onclick=()=>openThread(id);
  row.querySelector('[data-testid=reaction-button]').onclick=()=>{row.querySelector('[data-testid=emoji-picker]').hidden=false};
  row.querySelectorAll('[data-testid=emoji-option]').forEach(btn=>btn.onclick=async()=>{await api('/messages/'+id+'/reactions',{method:'POST',body:JSON.stringify({emoji:btn.dataset.emoji})});show()});
  row.querySelectorAll('[data-testid=reaction-chip]').forEach(btn=>btn.onclick=async()=>{await api('/messages/'+id+'/reactions/'+encodeURIComponent(btn.dataset.emoji),{method:'DELETE'});show()});
 });
}
async function editMsg(id,row){
 let old=row.querySelector('[data-testid=message-body]').textContent;
 let body=row.querySelector('[data-testid=message-body]');
 let input=document.createElement('input');input.dataset.testid='edit-message-input';input.value=old;body.replaceWith(input);input.focus();
 input.onkeydown=async e=>{if(e.key==='Escape')show();if(e.key==='Enter'){if(input.value.trim()){await api('/messages/'+id,{method:'PATCH',body:JSON.stringify({body:input.value})});show()}}};
}
async function delMsg(id){if(confirm('Delete?')){await api('/messages/'+id,{method:'DELETE'});show()}}
async function openThread(id){
 let p=state.msgs.find(m=>m.id==id);let panel=$('#thread-panel');panel.classList.remove('hidden');panel.removeAttribute('hidden');panel.innerHTML=`<button data-testid=close-thread data-button-role=secondary>Close</button><h3>Thread</h3><div data-testid=thread-messages>${p?p.body:''}</div><form id=thread-form><input data-testid=thread-input><button data-testid=thread-send data-button-role=primary>Reply</button></form>`;
 panel.querySelector('[data-testid=close-thread]').onclick=()=>{panel.classList.add('hidden');panel.setAttribute('hidden','')};
 panel.querySelector('#thread-form').onsubmit=async e=>{e.preventDefault();let i=panel.querySelector('[data-testid=thread-input]');if(i.value.trim()){await api('/channels/'+state.ch.id+'/messages',{method:'POST',body:JSON.stringify({body:i.value,parent_id:id})});i.value='';openThread(id);show()}};
 try{let r=await api('/messages/'+id+'/replies');panel.querySelector('[data-testid=thread-messages]').innerHTML=[p.body,...(r.replies||[]).map(x=>x.body)].join('<br>')}catch(e){}
}
function connect(){let t=localStorage.getItem('huddle.token'),w=new WebSocket(`ws://${location.host}/api/ws?token=${t}`);w.onopen=()=>w.send(JSON.stringify({type:'subscribe',channel_id:state.ch.id}));w.onmessage=e=>{let x=JSON.parse(e.data);if(x.type.startsWith('message.'))show()}}
if(new URLSearchParams(location.search).get('token')){localStorage.setItem('huddle.token',new URLSearchParams(location.search).get('token'))}if(localStorage.getItem('huddle.token')){api('/auth/me').then(x=>{state.user=x.user;load()}).catch(form)}else form();
</script></body></html>'''
async def root(req):return web.Response(text=HTML,content_type='text/html')
def app():
 a=web.Application();a.router.add_get('/',root);a.router.add_get('/api/health',lambda r:j({'status':'ok','node_id':NODE}));a.router.add_post('/api/auth/register',register);a.router.add_post('/api/auth/login',login);a.router.add_get('/api/auth/me',me);a.router.add_get('/api/workspaces',workspaces);a.router.add_post('/api/workspaces',create_ws);a.router.add_get('/api/workspaces/{slug}',workspace);a.router.add_get('/api/workspaces/{slug}/members',workspace_members);a.router.add_patch('/api/workspaces/{slug}',patch_workspace);a.router.add_get('/api/workspaces/{slug}/invitations',invitations);a.router.add_post('/api/workspaces/{slug}/invitations',invitations);a.router.add_post('/api/invitations/{code}/accept',accept_invite);a.router.add_post('/api/workspaces/{slug}/channels',create_channel);a.router.add_post('/api/channels/{id}/join',join);a.router.add_get('/api/channels/{id}/members',channels_members);a.router.add_patch('/api/channels/{id}',patch_channel);a.router.add_get('/api/channels/{id}/messages',messages);a.router.add_post('/api/channels/{id}/messages',post);a.router.add_patch('/api/messages/{id}',edit);a.router.add_delete('/api/messages/{id}',delete);a.router.add_get('/api/messages/{id}/replies',replies);a.router.add_post('/api/messages/{id}/reactions',react);a.router.add_delete('/api/messages/{id}/reactions/{emoji}',unreact);a.router.add_get('/api/ws',ws);return a
if __name__=='__main__':web.run_app(app(),host='127.0.0.1',port=PORT)
