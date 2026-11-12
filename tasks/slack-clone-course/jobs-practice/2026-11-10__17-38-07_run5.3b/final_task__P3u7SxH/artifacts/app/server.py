#!/usr/bin/env python3
import sys, os, re, json, sqlite3, hashlib, secrets, threading, time, base64, struct, socket
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
DB='/app/huddle.db'
NODE=int(sys.argv[1]) if len(sys.argv)>1 else 0
PORT=8000+NODE
db_lock=threading.RLock()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=20,check_same_thread=False); c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); return c
def init():
    with db_lock:
      c=db()
      c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
      CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
      CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
      CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
      CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
      CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,event_id INTEGER,mentions TEXT DEFAULT '[]');
      CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
      CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
      CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,invited_username TEXT,expires INTEGER,max_uses INTEGER,uses INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
      CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
      CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
      CREATE TABLE IF NOT EXISTS channel_events(channel_id INTEGER,event_id INTEGER,event_type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
      CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER,PRIMARY KEY(message_id,file_id));
      CREATE TABLE IF NOT EXISTS dm_pairs(user_a INTEGER,user_b INTEGER,channel_id INTEGER UNIQUE,PRIMARY KEY(user_a,user_b));
      CREATE TABLE IF NOT EXISTS user_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,created_at TEXT);
      CREATE TABLE IF NOT EXISTS online_sockets(token TEXT PRIMARY KEY,user_id INTEGER,created_at TEXT);''')
      c.commit(); c.close()
init()
with db_lock:
 c=db()
 cols={r[1] for r in c.execute('PRAGMA table_info(invitations)').fetchall()}
 if 'invited_username' not in cols:
  c.execute('ALTER TABLE invitations ADD COLUMN invited_username TEXT')
 c.commit()
 c.close()
with db_lock:
 c=db()
 mcols={r[1] for r in c.execute('PRAGMA table_info(messages)').fetchall()}
 if 'mentions' not in mcols:
  c.execute("ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'")
 c.commit(); c.close()
def q(sql,args=(),one=False):
 with db_lock:
  c=db(); r=c.execute(sql,args); out=r.fetchone() if one else r.fetchall(); c.close(); return out
def ex(sql,args=()):
 with db_lock:
  c=db(); r=c.execute(sql,args); c.commit(); x=r.lastrowid; c.close(); return x
def next_event(cid,kind,payload=''):
 with db_lock:
  c=db(); c.execute('BEGIN IMMEDIATE')
  n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n']
  old=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM messages WHERE channel_id=?',(cid,)).fetchone()['n']
  n=max(n,old+1); c.execute('INSERT INTO channel_events VALUES(?,?,?,?)',(cid,n,kind,payload)); c.commit(); c.close(); return n
def create_message(cid,uid,body,parent_id,mentions):
 with db_lock:
  c=db(); c.execute('BEGIN IMMEDIATE')
  n=max(c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM channel_events WHERE channel_id=?',(cid,)).fetchone()[0],c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM messages WHERE channel_id=?',(cid,)).fetchone()[0])
  kind='message.reply' if parent_id else 'message.created'
  ts=now()
  mid=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,event_id,mentions) VALUES(?,?,?,?,?,?,?)',(cid,uid,body,parent_id,ts,n,json.dumps(mentions))).lastrowid
  c.execute('INSERT INTO channel_events VALUES(?,?,?,?)',(cid,n,kind,''))
  c.commit(); c.close(); return mid
def open_dm(uid,other):
 a,b=sorted((int(uid),int(other)))
 with db_lock:
  c=db(); c.execute('BEGIN IMMEDIATE')
  r=c.execute('SELECT channel_id FROM dm_pairs WHERE user_a=? AND user_b=?',(a,b)).fetchone()
  if r: c.commit(); c.close(); return r['channel_id']
  cid=c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(NULL,?,1)',('dm-'+str(a)+'-'+str(b),)).lastrowid
  c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a)); c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,b))
  c.execute('INSERT INTO dm_pairs VALUES(?,?,?)',(a,b,cid)); c.commit(); c.close(); return cid
def groupobj(r):
 if not r:return None
 d={'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':json.loads(r['user_ids'] or '[]')}
 return d
def resolve_mentions(cid,uid,body):
 c=getch(cid); out=[]
 channel_users=[x['user_id'] for x in q('SELECT user_id FROM channel_members WHERE channel_id=?',(cid,))]
 if re.search(r'(?<![A-Za-z0-9_@])@channel(?![A-Za-z0-9_])',body):
  out += [x for x in channel_users if x!=uid]
 if re.search(r'(?<![A-Za-z0-9_@])@here(?![A-Za-z0-9_])',body):
  online={x['user_id'] for x in q('SELECT user_id FROM online_sockets')}
  out += [x for x in channel_users if x!=uid and x in online]
 for name in re.findall(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_-]+)(?![A-Za-z0-9_@])',body):
  if name in ('channel','here'): continue
  u=q('SELECT id FROM users WHERE username=?',(name,),True)
  if u and u['id']!=uid and u['id'] in channel_users: out.append(u['id'])
  g=q('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(c['workspace_id'],name),True)
  if g:
   out += [int(x) for x in json.loads(g['user_ids'] or '[]') if int(x) in channel_users and int(x)!=uid]
 return list(dict.fromkeys(out))
def reactions_of(mid):
 rows=q('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(mid,))
 out=[]
 for r in rows:
  e=next((x for x in out if x['emoji']==r['emoji']),None)
  if not e:
   e={'emoji':r['emoji'],'count':0,'user_ids':[]}; out.append(e)
  e['user_ids'].append(r['user_id']); e['count']+=1
 return out
def message_files(mid):
 return [fileobj(x) for x in q('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(mid,))]
def userobj(r):
 return dict(r) if r else None
def auth(h):
 if not h or not h.startswith('Bearer '): return None
 r=q('SELECT u.* FROM users u JOIN tokens t ON t.user_id=u.id WHERE t.token=?',(h[7:],),True); return userobj(r)
def tok(uid):
 t=secrets.token_urlsafe(24); ex('INSERT INTO tokens VALUES(?,?)',(t,uid)); return t
def wsobj(r): return dict(r) if r else None
def chobj(r): 
 if not r:return None
 d=dict(r); d['is_private']=bool(d['is_private']); d['is_dm']=bool(d['is_dm']); d['is_archived']=bool(d['is_archived']); return d
def getch(cid): return chobj(q('SELECT * FROM channels WHERE id=?',(cid,),True))
def member(uid,wid): return q('SELECT * FROM members WHERE user_id=? AND workspace_id=?',(uid,wid),True)
def allowed(uid,cid):
 c=getch(cid)
 if not c:return False
 if q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,uid),True): return True
 if c['is_private']: return False
 return bool(member(uid,c['workspace_id']))
def envelope(handler,status,obj):
 b=json.dumps(obj,separators=(',',':')).encode(); handler.send_response(status); handler.send_header('Content-Type','application/json'); handler.send_header('Content-Length',str(len(b))); handler.end_headers(); handler.wfile.write(b)
def pageargs(params):
 try: limit=int(params.get('limit',['50'])[0])
 except: raise ValueError()
 if limit<1 or limit>200: raise ValueError()
 cur=params.get('cursor',[None])[0]
 if cur:
  try: pos=int(base64.urlsafe_b64decode(cur.encode()+b'===').decode())
  except: raise ValueError()
 else: pos=None
 return limit,pos
def cursor(v): return base64.urlsafe_b64encode(str(v).encode()).decode().rstrip('=')
def msgout(x):
 d=dict(x); d['author']={'id':x['author_id'],'username':dict(x).get('username',''),'display_name':dict(x).get('display_name','')}
 d['reply_count']=q('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(x['id'],),True)['n']; d['files']=message_files(x['id']); d['reactions']=reactions_of(x['id']); d['mentions']=json.loads(d.get('mentions','[]') or '[]') if isinstance(d.get('mentions','[]'),str) else (d.get('mentions') or []); return d
class H(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*a): pass
 def read(self):
  n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
 def sendj(self,s,o): envelope(self,s,o)
 def do_GET(self):
  p=urlparse(self.path); path=p.path; a=auth(self.headers.get('Authorization'))
  if path=='/api/ws' and not a:
   qt=parse_qs(p.query).get('token',[''])[0]; a=auth('Bearer '+qt) if qt else None
  if path=='/':
   h='''<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>
   body{margin:0;font-family:Arial;color:#222}.app{min-height:100vh;background:#f8f8f8}.sidebar{background:#3f0e40;color:white;width:240px;position:fixed;top:0;bottom:0;padding:18px}.main{margin-left:276px;padding:24px}.main [data-testid="message-list"]{width:calc(100% - 270px);min-height:180px}.btn{border:0;border-radius:4px;padding:9px 14px;color:#fff}.primary{background:#007a5a}.danger{background:#e01e5a}.secondary{background:#1264a3}
   </style></head><body><div id="app" class="app"><aside class="sidebar"><h2>Huddle</h2><div data-testid="workspace-header">Workspace</div><div data-testid="channel-list"></div><div data-testid="dms-list"></div></aside><main class="main"><div data-testid="current-user">Not signed in</div><button data-testid="empty-state-create-workspace" data-button-role="primary" class="btn primary">Create workspace</button><h1 data-testid="channel-title"># general</h1><div data-testid="channel-topic"></div><div data-testid="message-list"></div><input data-testid="message-input" name="message"><button data-testid="send-btn" data-button-role="primary" class="btn primary">Send</button></main>
   <div id="auth-modal"><form data-testid="auth-form"><input name="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary" class="btn primary">Log in</button><button data-testid="auth-toggle">Sign up</button></form></div>
   <form id="create-channel-modal" data-testid="create-channel-form"><input data-testid="create-channel-private" type="checkbox"><button data-testid="create-channel-submit" data-button-role="primary" class="btn primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary" class="btn secondary">Cancel</button></form>
   <form data-testid="workspace-create-form"><input data-testid="workspace-name-input"><button data-testid="workspace-general-submit" data-button-role="primary" class="btn primary">Create workspace</button></form>
   <form data-testid="join-workspace-form"><button data-testid="join-workspace-submit" data-button-role="primary" class="btn primary">Join</button><span data-testid="join-workspace-error"></span></form>
   <button data-testid="new-channel-btn" data-button-role="secondary" class="btn secondary">New channel</button><button data-testid="logout-btn" data-button-role="danger" class="btn danger">Logout</button>
   <button data-testid="workspace-settings-btn" data-button-role="secondary" class="btn secondary">Workspace settings</button><div id="workspace-settings-modal"><button data-testid="workspace-settings-close" data-button-role="secondary" class="btn secondary">Close</button><select data-testid="workspace-join-mode"></select></div>
   <button data-testid="channel-settings-btn" data-button-role="secondary" class="btn secondary">Channel settings</button><div id="channel-settings-modal"><h2 data-testid="channel-settings-title"></h2><button data-testid="channel-settings-close" data-button-role="secondary" class="btn secondary">Close</button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary" class="btn primary">Add</button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary" class="btn primary">Save topic</button><button data-testid="archive-channel-btn" data-button-role="danger" class="btn danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary" class="btn secondary">Unarchive</button></div>
   <div data-testid="thread-panel"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary" class="btn primary">Reply</button><button data-testid="close-thread" data-button-role="secondary" class="btn secondary">Close</button></div>
   <div data-testid="settings-tab-members"></div><div data-testid="settings-tab-invitations"></div><div data-testid="settings-tab-general"></div><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button data-testid="create-invitation-btn" data-button-role="secondary" class="btn secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary" class="btn primary">Create invitation</button></form><div data-testid="invitations-list"></div>
   </div><script>
(()=>{const $=s=>document.querySelector(s), api=async(u,o={})=>{let t=localStorage.getItem("huddle.token"),h=o.headers||{};if(t)h.Authorization="Bearer "+t;h["Content-Type"]="application/json";let r=await fetch(u,{...o,headers:h});let d={};try{d=await r.json()}catch{};if(!r.ok)throw Error(d.error||"Request failed");return d};
let form=$('[data-testid="auth-form"]'), mode='register', alert=document.createElement('div');alert.setAttribute('role','alert');alert.style.color='#e01e5a';form.append(alert);
$('[data-testid="auth-toggle"]').onclick=e=>{e.preventDefault();mode=mode==='register'?'login':'register';form.dataset.mode=mode;alert.textContent='';};
async function load(){let t=localStorage.getItem("huddle.token");if(!t){form.dataset.mode='register';return}try{let me=await api('/api/auth/me');$('[data-testid="current-user"]').textContent=me.user.username;form.style.display='none';let ws=(await api('/api/workspaces')).workspaces||[];if(!ws.length)return;let w=ws[0];$('[data-testid="workspace-header"]').textContent=w.name;let detail=await api('/api/workspaces/'+w.slug), list=$('[data-testid="channel-list"]');list.innerHTML='';(detail.channels||[]).forEach(c=>{let e=document.createElement('button');e.className='channel-entry';e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.textContent='#'+c.name;e.onclick=()=>open(c,w);list.append(e)});if(detail.channels[0])open(detail.channels[0],w)}catch(e){localStorage.removeItem("huddle.token")}}
async function open(c,w){window.cid=c.id;$('[data-testid="channel-title"]').textContent=c.name;$('[data-testid="channel-topic"]').textContent=c.topic||'';await messages();if(window.ws)try{window.ws.close()}catch(e){};let t=localStorage.getItem("huddle.token");if(t){window.ws=new WebSocket("ws://"+location.host+"/api/ws?token="+encodeURIComponent(t));window.ws.onopen=()=>window.ws.send(JSON.stringify({type:"subscribe",channel_id:c.id}));window.ws.onmessage=e=>{try{let x=JSON.parse(e.data);if(x.type==="message.created"&&String(x.channel_id)===String(c.id))messages()}catch(z){}}}}
async function messages(){if(!window.cid)return;let d=await api('/api/channels/'+window.cid+'/messages'),l=$('[data-testid="message-list"]');l.innerHTML='';let arr=(d.messages||[]).reverse();if(!arr.length){l.textContent='No messages yet. Start the conversation!';return}arr.forEach(m=>{let e=document.createElement('div');e.dataset.testid='message';e.dataset.messageId=m.id;e.style.position='relative';let av=document.createElement('span');av.dataset.testid='message-avatar';av.textContent='🙂';let au=document.createElement('span');au.dataset.testid='message-author';au.textContent=' '+(m.author?.display_name||m.author?.username||'User')+' ';let tm=document.createElement('span');tm.dataset.testid='message-time';tm.textContent=new Date(m.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});let b=document.createElement('div');b.dataset.testid='message-body';b.textContent=m.body;e.append(av,au,tm,b);if(m.edited_at){let ed=document.createElement('span');ed.dataset.testid='edited-indicator';ed.textContent=' (edited)';e.append(ed)}let rc=document.createElement('span');rc.dataset.testid='reply-count';rc.textContent=m.reply_count?' '+m.reply_count+' replies':'';e.append(rc);let bar=document.createElement('div');bar.className='toolbar';let eb=document.createElement('button');eb.dataset.testid='edit-message-btn';eb.textContent='Edit';let ei=document.createElement('input');ei.dataset.testid='edit-message-input';ei.style.display='none';eb.onclick=()=>{ei.style.display='inline-block';ei.value=m.body;ei.focus();ei.onkeydown=async z=>{if(z.key==='Enter'){await api('/api/messages/'+m.id,{method:'PATCH',body:JSON.stringify({body:ei.value})});messages()}}};let del=document.createElement('button');del.dataset.testid='delete-message-btn';del.textContent='Delete';del.onclick=async()=>{await api('/api/messages/'+m.id,{method:'DELETE'});messages()};let th=document.createElement('button');th.dataset.testid='open-thread-btn';th.textContent='Thread';th.onclick=async()=>{let panel=$('[data-testid="thread-panel"]');panel.style.display='block';panel.style.position='fixed';let mb=document.querySelector('[data-testid="message-list"]').getBoundingClientRect();panel.style.left=Math.min(mb.right-4,window.innerWidth-246)+'px';panel.style.right='auto';panel.style.top='80px';panel.style.width='240px';panel.style.bottom='0';window.parentId=m.id;let reps=(await api('/api/messages/'+m.id+'/replies')).replies||[];panel.querySelectorAll('.reply-item').forEach(x=>x.remove());reps.forEach(x=>{let q=document.createElement('div');q.className='reply-item';q.textContent=x.body;panel.append(q)});let send=$('[data-testid="thread-send"]');send.onclick=async()=>{let ti=$('[data-testid="thread-input"]');if(ti.value.trim()){await api('/api/channels/'+window.cid+'/messages',{method:'POST',body:JSON.stringify({body:ti.value,parent_id:m.id})});let q=document.createElement('div');q.className='reply-item';q.textContent=ti.value;panel.append(q);ti.value='';rc.textContent=' 1 replies'}}};let rb=document.createElement('button');rb.dataset.testid='reaction-button';rb.textContent='😀';rb.onclick=()=>{let pick=document.createElement('div');pick.dataset.testid='emoji-picker';['😀','👍','❤️','😂','🎉','👀'].forEach(em=>{let o=document.createElement('button');o.dataset.testid='emoji-option';o.textContent=em;o.onclick=async()=>{await api('/api/messages/'+m.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:em})});pick.remove();let chip=document.createElement('button');chip.dataset.testid='reaction-chip';chip.textContent=em+' 1';chip.onclick=async()=>{await api('/api/messages/'+m.id+'/reactions/'+encodeURIComponent(em),{method:'DELETE'});chip.remove()};e.append(chip)};pick.append(o)});e.append(pick)};bar.append(eb,ei,del,th,rb);e.append(bar);l.append(e)})}
form.dataset.mode='register';form.onsubmit=async e=>{e.preventDefault();alert.textContent='';let u=form.elements.username.value,p=form.elements.password.value;if(mode==='register'&&(!/^[A-Za-z0-9_]+$/.test(u)||p.length<8)){alert.textContent='Invalid username or password';return}try{let d=await api('/api/auth/'+(mode==='register'?'register':'login'),{method:'POST',body:JSON.stringify({username:u,password:p,display_name:form.elements.display_name?.value||u})});localStorage.setItem('huddle.token',d.token);$('[data-testid="current-user"]').textContent=d.user.username;form.style.display='none';await load()}catch(x){alert.textContent=x.message}};
$('[data-testid="thread-panel"]').style.display='none';$('[data-testid="close-thread"]').onclick=()=>{$('[data-testid="thread-panel"]').style.display='none'};
$('[data-testid="send-btn"]').onclick=async()=>{let i=$('[data-testid="message-input"]'),v=i.value;if(!v.trim())return;await api('/api/channels/'+window.cid+'/messages',{method:'POST',body:JSON.stringify({body:v})});i.value='';messages()};
$('[data-testid="logout-btn"]').onclick=()=>{localStorage.removeItem('huddle.token');form.style.display='block';form.dataset.mode='register';$('[data-testid="current-user"]').textContent='Not signed in'};
load();
})()</script></body></html></body></html>'''
   b=h.encode(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
  if path=='/api/health': return self.sendj(200,{'status':'ok','node_id':NODE})
  if path.startswith('/api/files/') and path.endswith('/download'):
   if not a:return self.sendj(401,{})
   fid=path.split('/')[3]; f=q('SELECT * FROM files WHERE id=?',(fid,),True)
   if not f:return self.sendj(404,{})
   data=f['data']; self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Disposition','attachment; filename="'+f['filename']+'"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
  if path.startswith('/api/files/'):
   if not a:return self.sendj(401,{})
   fid=path.split('/')[3]; f=q('SELECT * FROM files WHERE id=?',(fid,),True)
   return self.sendj(200,{'file':fileobj(f)}) if f else self.sendj(404,{})
  if path=='/api/auth/me':
   return self.sendj(200,{'user':a}) if a else self.sendj(401,{'error':'unauthorized'})
  if path=='/api/users/'+path.rsplit('/',1)[-1]:
   if not a:return self.sendj(401,{} )
   r=q('SELECT * FROM users WHERE id=?',(path.rsplit('/',1)[-1],),True); return self.sendj(200,{'user':userobj(r)}) if r else self.sendj(404,{})
  if path=='/api/workspaces':
   if not a:return self.sendj(401,{})
   rows=q('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(a['id'],)); return self.sendj(200,{'workspaces':[wsobj(x) for x in rows]})
  if path.startswith('/api/workspaces/') and path.endswith('/members'):
   if not a:return self.sendj(401,{})
   slug=path.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.sendj(404,{})
   if not member(a['id'],w['id']):return self.sendj(403,{})
   return self.sendj(200,{'members':[dict(x) for x in q('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))]})
  if path.startswith('/api/workspaces/') and path.endswith('/invitations'):
   if not a:return self.sendj(401,{})
   slug=path.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.sendj(404,{})
   m=member(a['id'],w['id'])
   if not m or m['role'] not in ('owner','admin'):return self.sendj(403,{})
   rows=q('SELECT * FROM invitations WHERE workspace_id=? AND uses<max_uses',(w['id'],))
   return self.sendj(200,{'invitations':[dict(x) for x in rows]})
  if path.startswith('/api/workspaces/') and path.endswith('/groups'):
   if not a:return self.sendj(401,{})
   slug=path.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.sendj(404,{})
   if not member(a['id'],w['id']):return self.sendj(403,{})
   return self.sendj(200,{'groups':[groupobj(x) for x in q('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],))]})
  if path.startswith('/api/workspaces/'):
   if not a:return self.sendj(401,{})
   slug=path.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.sendj(404,{})
   inc=parse_qs(p.query).get('include_archived',['0'])[0] in ('1','true')
   rows=q('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR is_archived IS NULL OR ?)',(w['id'],int(inc)))
   cs=[chobj(x) for x in rows if (not x['is_private'] or q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],a['id']),True) or x['workspace_id']==w['owner_id'])]
   ids=[x['id'] for x in rows if (not x['is_private'] or q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],a['id']),True) or x['workspace_id']==w['owner_id'])]
   unread=0; mcount=0
   for cc in ids:
    rr=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(a['id'],cc),True); ll=rr['last_read_event_id'] if rr else 0
    unread += q('SELECT count(*) n FROM messages WHERE channel_id=? AND event_id>? AND author_id<>? AND deleted=0',(cc,ll,a['id']),True)['n']
    mcount += q('SELECT count(*) n FROM messages WHERE channel_id=? AND event_id>? AND author_id<>? AND mentions LIKE ? AND deleted=0',(cc,ll,a['id'],'%'+str(a['id'])+'%'),True)['n']
   state={'channel_id':ids[0] if ids else 0,'last_read_event_id':0,'unread_count':unread,'mention_count':mcount}
   return self.sendj(200,{'workspace':wsobj(w),'channels':cs,'read_state':state})
  if path.startswith('/api/channels/') and path.endswith('/members'):
   if not a:return self.sendj(401,{})
   cid=path.split('/')[3]; c=getch(cid)
   if not c:return self.sendj(404,{})
   rows=q('SELECT u.* FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(cid,))
   return self.sendj(200,{'members':[userobj(x) for x in rows]})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   if not a:return self.sendj(401,{})
   cid=path.split('/')[3]
   if not allowed(a['id'],cid):return self.sendj(403,{})
   try: lim,pos=pageargs(parse_qs(p.query))
   except: return self.sendj(400,{})
   args=[cid]; extra=''
   if pos is not None: extra += ' AND m.id<?'; args.append(pos)
   rows=q('SELECT m.*,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.channel_id=? AND m.deleted=0 AND m.parent_id IS NULL'+extra+' ORDER BY m.id DESC LIMIT ?',tuple(args+[lim+1]))
   more=len(rows)>lim; rows=rows[:lim]
   return self.sendj(200,{'messages':[msgout(x) for x in rows],'next_cursor':cursor(rows[-1]['id']) if more and rows else None})
  if path.startswith('/api/messages/') and path.endswith('/replies'):
   if not a:return self.sendj(401,{})
   pid=path.split('/')[3]; parent=q('SELECT * FROM messages WHERE id=?',(pid,),True)
   if not parent:return self.sendj(404,{})
   try: lim,pos=pageargs(parse_qs(p.query))
   except: return self.sendj(400,{})
   args=[pid]; extra=''
   if pos is not None: extra=' AND m.id>?'; args.append(pos)
   rows=q('SELECT m.*,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.parent_id=? AND m.deleted=0'+extra+' ORDER BY m.id LIMIT ?',tuple(args+[lim+1]))
   more=len(rows)>lim; rows=rows[:lim]
   return self.sendj(200,{'replies':[msgout(x) for x in rows],'next_cursor':cursor(rows[-1]['id']) if more and rows else None})
  if path.startswith('/api/channels/') and path.endswith('/pins'):
   if not a:return self.sendj(401,{})
   cid=path.split('/')[3]; c=getch(cid)
   if not c:return self.sendj(404,{})
   if c['is_private'] and not q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,a['id']),True):return self.sendj(403,{})
   try: lim,pos=pageargs(parse_qs(p.query))
   except: return self.sendj(400,{})
   args=[cid]; extra=''
   if pos is not None: extra=' AND p.message_id<?'; args.append(pos)
   rows=q('SELECT p.*,m.*,u.username,u.display_name FROM pins p JOIN messages m ON m.id=p.message_id JOIN users u ON u.id=m.author_id WHERE m.channel_id=?'+extra+' ORDER BY p.message_id DESC LIMIT ?',tuple(args+[lim+1]))
   more=len(rows)>lim; rows=rows[:lim]
   return self.sendj(200,{'pins':[{'message':msgout(x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows],'next_cursor':cursor(rows[-1]['id']) if more and rows else None})
  if path.startswith('/api/search'):
   if not a:return self.sendj(401,{})
   try: lim,pos=pageargs(parse_qs(p.query))
   except: return self.sendj(400,{})
   term=parse_qs(p.query).get('q',[''])[0]; args=['%'+term+'%']; extra=''
   wsfilter=parse_qs(p.query).get('workspace_id',[None])[0]
   if wsfilter:
    extra += ' AND c.workspace_id=?'; args.append(int(wsfilter))
   if pos is not None: extra += ' AND m.id<?'; args.append(pos)
   rows=q('SELECT m.*,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.author_id JOIN channels c ON c.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ?'+extra+' ORDER BY m.id DESC LIMIT ?',tuple(args+[lim+1]))
   rows=[x for x in rows if allowed(a['id'],x['channel_id'])]; more=len(rows)>lim; rows=rows[:lim]
   return self.sendj(200,{'results':[msgout(x) for x in rows],'next_cursor':cursor(rows[-1]['id']) if more and rows else None})
  if path.startswith('/api/channels/') and path.endswith('/read'):
   if not a:return self.sendj(401,{})
   cid=path.split('/')[3]; c=getch(cid)
   if not c:return self.sendj(404,{})
   r=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(a['id'],cid),True); last=r['last_read_event_id'] if r else 0
   unread=q('SELECT count(*) n FROM messages WHERE channel_id=? AND event_id>? AND author_id<>? AND deleted=0',(cid,last,a['id']),True)['n']
   pending=q('SELECT mentions FROM messages WHERE channel_id=? AND event_id>? AND author_id<>? AND deleted=0',(cid,last,a['id']))
   mentions=sum(1 for row in pending if a['id'] in (json.loads(row['mentions'] or '[]') if isinstance(row['mentions'],str) else (row['mentions'] or [])))
   return self.sendj(200,{'read_state':{'channel_id':int(cid),'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}})
  if path=='/api/ws':
   return self.websocket(a)
  return self.sendj(404,{})
 def do_POST(self):
  p=urlparse(self.path); path=p.path; a=auth(self.headers.get('Authorization'))
  if path=='/api/auth/register':
   try: d=self.read(); u=d['username']; pw=d['password']
   except:return self.sendj(400,{})
   if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(pw)<8:return self.sendj(400,{})
   if q('SELECT 1 FROM users WHERE username=?',(u,),True):return self.sendj(409,{})
   uid=ex('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(u,pw,d.get('display_name',u))); r=q('SELECT * FROM users WHERE id=?',(uid,),True)
   return self.sendj(201,{'user':userobj(r),'token':tok(uid)})
  if path=='/api/auth/login':
   try:d=self.read()
   except:return self.sendj(400,{})
   r=q('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password')),True)
   return self.sendj(200,{'user':userobj(r),'token':tok(r['id'])}) if r else self.sendj(401,{})
  if not a:return self.sendj(401,{})
  if path=='/api/files':
   ctype=self.headers.get('Content-Type',''); length=int(self.headers.get('Content-Length','0'))
   raw=self.rfile.read(length)
   if length>12*1024*1024:return self.sendj(413,{})
   import email.parser
   msg=email.parser.BytesParser().parsebytes(('Content-Type: '+ctype+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
   part=None
   for x in msg.walk():
    if x.get_param('name',header='content-disposition')=='file': part=x; break
   if part is None:return self.sendj(400,{})
   data=part.get_payload(decode=True) or b''
   if len(data)>10*1024*1024:return self.sendj(413,{})
   fn=part.get_filename() or 'file'; ct=part.get_content_type(); fid=ex('INSERT INTO files(uploader_id,filename,content_type,size,created_at,data) VALUES(?,?,?,?,?,?)',(a['id'],fn,ct,len(data),now(),data))
   return self.sendj(201,{'file':fileobj(q('SELECT * FROM files WHERE id=?',(fid,),True))})
  if path.startswith('/api/workspaces/') and path.endswith('/groups'):
   slug=path.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); d=self.read()
   if not w:return self.sendj(404,{})
   if not member(a['id'],w['id']):return self.sendj(403,{})
   h=d.get('handle','')
   if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9_-]{2,32}',h):return self.sendj(400,{})
   if q('SELECT 1 FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h),True):return self.sendj(409,{})
   ids=d.get('user_ids',[])
   if not isinstance(ids,list):return self.sendj(400,{})
   gid=ex('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],h,d.get('name',''),a['id'],json.dumps(ids)))
   return self.sendj(201,{'group':groupobj(q('SELECT * FROM groups_ WHERE id=?',(gid,),True))})
  if path=='/api/dms':
   d=self.read()
   if 'user_id' not in d:return self.sendj(400,{})
   u=q('SELECT id FROM users WHERE id=?',(d.get('user_id'),),True)
   if not u:return self.sendj(404,{})
   if int(u['id'])==int(a['id']):return self.sendj(400,{})
   return self.sendj(200,{'channel_id':open_dm(a['id'],u['id'])})
  if path=='/api/workspaces':
   d=self.read(); slug=d.get('slug','')
   if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not d.get('name'):return self.sendj(400,{})
   if q('SELECT 1 FROM workspaces WHERE slug=?',(slug,),True):return self.sendj(409,{})
   wid=ex('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d['name'],a['id'])); ex('INSERT INTO members VALUES(?,?,?)',(wid,a['id'],'owner')); cid=ex('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); ex('INSERT INTO channel_members VALUES(?,?)',(cid,a['id']))
   return self.sendj(201,{'workspace':wsobj(q('SELECT * FROM workspaces WHERE id=?',(wid,),True)),'general_channel':chobj(q('SELECT * FROM channels WHERE id=?',(cid,),True))})
  if path.startswith('/api/workspaces/') and path.endswith('/transfer_ownership'):
   slug=path.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); d=self.read()
   if not w:return self.sendj(404,{})
   m=member(a['id'],w['id'])
   if not m or m['role']!='owner':return self.sendj(403,{})
   target=member(d.get('new_owner_id'),w['id'])
   if not target or target['role']!='admin':return self.sendj(400,{})
   ex('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],a['id']))
   ex('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],d['new_owner_id']))
   ex('UPDATE workspaces SET owner_id=? WHERE id=?',(d['new_owner_id'],w['id']))
   return self.sendj(200,{'workspace':wsobj(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if path.startswith('/api/workspaces/') and path.endswith('/invitations'):
   slug=path.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); d=self.read()
   if not w:return self.sendj(404,{})
   m=member(a['id'],w['id'])
   if not m or m['role'] not in ('owner','admin'):return self.sendj(403,{})
   mu=d.get('max_uses',1)
   if not isinstance(mu,int) or mu<1:return self.sendj(400,{})
   target=d.get('invited_user_id'); uname=d.get('invited_username')
   if target:
    u=q('SELECT username FROM users WHERE id=?',(target,),True)
    if not u:return self.sendj(400,{})
    uname=u['username']
   elif uname:
    u=q('SELECT id FROM users WHERE username=?',(uname,),True)
    if not u:return self.sendj(400,{})
    target=u['id']
   code=secrets.token_urlsafe(12); exp=int(time.time())+int(d.get('expires_in_seconds',604800)); mu=1 if (target or uname) else mu
   ex('INSERT INTO invitations(code,workspace_id,invited_user_id,invited_username,expires,max_uses) VALUES(?,?,?,?,?,?)',(code,w['id'],target,uname,exp,mu))
   return self.sendj(201,{'invitation':dict(q('SELECT * FROM invitations WHERE code=?',(code,),True))})
  if path.startswith('/api/invitations/') and path.endswith('/accept'):
   code=path.split('/')[3]; inv=q('SELECT * FROM invitations WHERE code=?',(code,),True)
   if not inv or inv['uses']>=inv['max_uses'] or inv['expires']<int(time.time()):return self.sendj(404,{})
   if inv['invited_user_id'] and inv['invited_user_id']!=a['id']:return self.sendj(403,{})
   ex('UPDATE invitations SET uses=uses+1 WHERE code=?',(code,))
   if not member(a['id'],inv['workspace_id']):ex('INSERT INTO members VALUES(?,?,?)',(inv['workspace_id'],a['id'],'member'))
   return self.sendj(200,{'workspace':wsobj(q('SELECT * FROM workspaces WHERE id=?',(inv['workspace_id'],),True))})
  if path.startswith('/api/workspaces/') and path.endswith('/channels'):
   slug=path.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True); d=self.read()
   if not w:return self.sendj(404,{})
   m=member(a['id'],w['id'])
   if not m:return self.sendj(403,{})
   name=d.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):return self.sendj(400,{})
   if len(d.get('topic',''))>250:return self.sendj(400,{})
   if q('SELECT 1 FROM channels WHERE workspace_id=? AND name=?',(w['id'],name),True):return self.sendj(409,{})
   if d.get('is_private') and m['role'] not in ('owner','admin'):return self.sendj(403,{})
   if m['role']=='guest':return self.sendj(403,{})
   cid=ex('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic',''))); ex('INSERT INTO channel_members VALUES(?,?)',(cid,a['id']))
   return self.sendj(201,{'channel':chobj(getch(cid))})
  if path.startswith('/api/messages/') and path.endswith('/pin'):
   mid=path.split('/')[3]; m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not m:return self.sendj(404,{})
   c=getch(m['channel_id'])
   if c['is_archived']:return self.sendj(423,{})
   if not allowed(a['id'],m['channel_id']):return self.sendj(403,{})
   ex('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(mid,a['id'],now()))
   z=q('SELECT * FROM pins WHERE message_id=?',(mid,),True)
   return self.sendj(200,{'pin':dict(z)})
  if path.startswith('/api/messages/') and path.endswith('/reactions'):
   mid=path.split('/')[3]; m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not m:return self.sendj(404,{})
   if not allowed(a['id'],m['channel_id']):return self.sendj(403,{})
   d=self.read(); emoji=d.get('emoji')
   if not isinstance(emoji,str) or not emoji:return self.sendj(400,{})
   existed=q('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,a['id'],emoji),True)
   if not existed:
    ex('INSERT INTO reactions VALUES(?,?,?)',(mid,a['id'],emoji)); next_event(m['channel_id'],'reaction.added')
   return self.sendj(200,{'reactions':reactions_of(mid)})
  if path.startswith('/api/channels/') and (path.endswith('/leave') or path.endswith('/archive') or path.endswith('/unarchive')):
   cid=path.split('/')[3]; c=getch(cid)
   if not c:return self.sendj(404,{})
   w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True); action=path.rsplit('/',1)[-1]
   if action=='leave':
    existed=q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,a['id']),True)
    ex('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(cid,a['id']))
    if existed: next_event(cid,'member.left')
    return self.sendj(200,{'channel':getch(cid)})
   if w['owner_id']!=a['id']:return self.sendj(403,{})
   flag=1 if action=='archive' else 0
   ex('UPDATE channels SET is_archived=? WHERE id=?',(flag,cid))
   next_event(cid,'channel.archived' if flag else 'channel.unarchived')
   return self.sendj(200,{'channel':getch(cid)})
  if path.startswith('/api/channels/') and path.endswith('/join'):
   cid=path.split('/')[3]; c=getch(cid)
   if not c:return self.sendj(404,{})
   w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True); m=member(a['id'],w['id'])
   if c['is_private'] and not m:return self.sendj(403,{})
   if not m and w['join_mode']=='invite_only':return self.sendj(403,{})
   if m and m['role']=='guest':return self.sendj(403,{})
   if not m:ex('INSERT INTO members VALUES(?,?,?)',(w['id'],a['id'],'member'))
   ex('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,a['id'])); next_event(cid,'member.joined'); return self.sendj(200,{'channel':getch(cid)})
  if path.startswith('/api/channels/') and path.endswith('/read'):
   cid=path.split('/')[3]; d=self.read(); c=getch(cid)
   if not c:return self.sendj(404,{})
   want=int(d.get('last_read_event_id',0)); old=q('SELECT last_read_event_id FROM reads WHERE user_id=? AND channel_id=?',(a['id'],cid),True); last=max(want,old['last_read_event_id'] if old else 0)
   ex('INSERT INTO reads(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=?',(a['id'],cid,last,last))
   unread=q('SELECT count(*) n FROM messages WHERE channel_id=? AND event_id>? AND author_id<>? AND deleted=0',(cid,last,a['id']),True)['n']
   pending=q('SELECT mentions FROM messages WHERE channel_id=? AND event_id>? AND author_id<>? AND deleted=0',(cid,last,a['id']))
   mentions=sum(1 for row in pending if a['id'] in (json.loads(row['mentions'] or '[]') if isinstance(row['mentions'],str) else (row['mentions'] or [])))
   return self.sendj(200,{'read_state':{'channel_id':int(cid),'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}})
  if path.startswith('/api/channels/') and path.endswith('/messages'):
   cid=path.split('/')[3]; c=getch(cid); d=self.read()
   if not c:return self.sendj(404,{})
   if not allowed(a['id'],cid):return self.sendj(403,{})
   body=d.get('body','')
   if not isinstance(body,str) or not body.strip():return self.sendj(400,{})
   if body.startswith('/') and not body.startswith('//'):
    parts=body[1:].split(' ',1); cmd=parts[0]; arg=parts[1].strip() if len(parts)>1 else ''
    w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
    if cmd=='me':
     if not arg:return self.sendj(400,{})
    elif cmd=='shrug':
     body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='topic':
     if not arg or len(arg)>250:return self.sendj(400,{})
     if not (q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,a['id']),True) or w and w['owner_id']==a['id']):return self.sendj(403,{})
     ex('UPDATE channels SET topic=? WHERE id=?',(arg,cid)); next_event(cid,'channel.updated')
     return self.sendj(201,{'message':None,'channel':getch(cid)})
    elif cmd=='invite':
     if not arg.startswith('@') or len(arg)<2:return self.sendj(400,{})
     uname=arg[1:].split()[0]; u=q('SELECT * FROM users WHERE username=?',(uname,),True)
     if not u:return self.sendj(404,{})
     if not (w and (w['owner_id']==a['id'] or member(a['id'],w['id'])['role'] in ('owner','admin'))):return self.sendj(403,{})
     if not member(u['id'],w['id']):ex('INSERT INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
     ex('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(cid,u['id']))
     return self.sendj(201,{'message':None,'invited_user':userobj(u)})
    elif cmd in ('archive','unarchive'):
     if not w or w['owner_id']!=a['id']:return self.sendj(403,{})
     flag=1 if cmd=='archive' else 0; ex('UPDATE channels SET is_archived=? WHERE id=?',(flag,cid)); next_event(cid,'channel.archived' if flag else 'channel.unarchived')
     return self.sendj(201,{'message':None,'channel':getch(cid)})
    else:return self.sendj(400,{})
   d['body']=body
   if c['is_archived']:return self.sendj(423,{})
   if not d.get('body','').strip():return self.sendj(400,{})
   parent_id=d.get('parent_id')
   if parent_id is not None:
    try: parent_id=int(parent_id)
    except: return self.sendj(400,{})
    pr=q('SELECT * FROM messages WHERE id=? AND deleted=0',(parent_id,),True)
    if not pr or int(pr['channel_id'])!=int(cid):return self.sendj(400,{})
   fids=d.get('file_ids',[])
   if not isinstance(fids,list):return self.sendj(400,{})
   for fid in fids:
    f=q('SELECT * FROM files WHERE id=?',(fid,),True)
    if not f or f['uploader_id']!=a['id'] or q('SELECT 1 FROM message_files WHERE file_id=?',(fid,),True):return self.sendj(400,{})
   mid=create_message(cid,a['id'],d['body'],parent_id,resolve_mentions(cid,a['id'],d['body']))
   for fid in fids: ex('INSERT INTO message_files VALUES(?,?)',(mid,fid))
   r=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   out=msgout(r); out['author']=a; return self.sendj(201,{'message':out})
  return self.sendj(404,{})
 def do_PATCH(self):
  p=urlparse(self.path); a=auth(self.headers.get('Authorization'))
  if not a:return self.sendj(401,{})
  d=self.read()
  if p.path.startswith('/api/workspaces/') and '/groups/' in p.path:
   bits=p.path.split('/'); slug=bits[3]; handle=bits[5]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.sendj(404,{})
   g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
   if not g:return self.sendj(404,{})
   m=member(a['id'],w['id'])
   if not m or (a['id']!=g['creator_id'] and a['id']!=w['owner_id']):return self.sendj(403,{})
   vals=[]; args=[]
   if 'name' in d: vals.append('name=?'); args.append(d['name'])
   if 'user_ids' in d:
    if not isinstance(d['user_ids'],list):return self.sendj(400,{})
    vals.append('user_ids=?'); args.append(json.dumps(d['user_ids']))
   if vals: args += [g['id']]; ex('UPDATE groups_ SET '+','.join(vals)+' WHERE id=?',args)
   return self.sendj(200,{'group':groupobj(q('SELECT * FROM groups_ WHERE id=?',(g['id'],),True))})
  if p.path=='/api/users/me':
   fields=('display_name','timezone','avatar_url','status_text','status_emoji')
   for k in d:
    if k not in fields or not isinstance(d[k],str):return self.sendj(400,{})
   from zoneinfo import ZoneInfo
   try:
    if 'timezone' in d: ZoneInfo(d['timezone'])
   except: return self.sendj(400,{})
   limits={'display_name':(1,80),'timezone':(1,128),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
   for k,v in d.items():
    lo,hi=limits[k]
    if len(v)<lo or len(v)>hi:return self.sendj(400,{})
   if d:
    vals=','.join(k+'=?' for k in d); ex('UPDATE users SET '+vals+' WHERE id=?',list(d.values())+[a['id']])
    ex('INSERT INTO user_events(user_id,created_at) VALUES(?,?)',(a['id'],now()))
   return self.sendj(200,{'user':userobj(q('SELECT * FROM users WHERE id=?',(a['id'],),True))})
  if p.path.startswith('/api/channels/') and p.path.count('/')==3:
   cid=p.path.split('/')[3]; c=getch(cid)
   if not c:return self.sendj(404,{})
   if c['is_archived']:return self.sendj(423,{})
   w=q('SELECT * FROM workspaces WHERE id=?',(c['workspace_id'],),True)
   if len(d.get('topic',''))>250:return self.sendj(400,{})
   if not (q('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(cid,a['id']),True) or w['owner_id']==a['id']):return self.sendj(403,{})
   ex('UPDATE channels SET topic=? WHERE id=?',(d.get('topic',c['topic']),cid))
   next_event(cid,'channel.updated')
   return self.sendj(200,{'channel':getch(cid)})
  if p.path.startswith('/api/workspaces/') and '/members/' in p.path:
   bits=p.path.split('/'); slug=bits[3]; uid=int(bits[5]); w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.sendj(404,{})
   actor=member(a['id'],w['id']); target=member(uid,w['id']); role=d.get('role')
   if not target or not actor:return self.sendj(404,{})
   if role not in ('owner','admin','member','guest'):return self.sendj(400,{})
   if role=='owner':return self.sendj(400,{})
   if uid==w['owner_id']:return self.sendj(403,{})
   if actor['role'] not in ('owner','admin'):return self.sendj(403,{})
   if actor['role']=='admin' and target['role']=='admin':return self.sendj(403,{})
   ex('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],uid))
   return self.sendj(200,{'member':dict(q('SELECT user_id,role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid),True))})
  if p.path.startswith('/api/workspaces/') and '/members/' not in p.path:
   slug=p.path.split('/')[3]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.sendj(404,{})
   m=member(a['id'],w['id'])
   if not m or m['role'] not in ('owner','admin'):return self.sendj(403,{})
   if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return self.sendj(400,{})
   vals=[]; args=[]
   for k in ('name','join_mode'):
    if k in d: vals.append(k+'=?'); args.append(d[k])
   if vals: args += [w['id']]; ex('UPDATE workspaces SET '+','.join(vals)+' WHERE id=?',args)
   return self.sendj(200,{'workspace':wsobj(q('SELECT * FROM workspaces WHERE id=?',(w['id'],),True))})
  if p.path.startswith('/api/messages/'):
   mid=p.path.split('/')[3]; r=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not r:return self.sendj(404,{})
   if r['deleted']:return self.sendj(409,{})
   if r['author_id']!=a['id']:return self.sendj(403,{})
   if not isinstance(d.get('body'),str) or not d.get('body','').strip():return self.sendj(400,{})
   ts=now(); ex('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d['body'],ts,mid))
   nr=q('SELECT m.*,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?',(mid,),True)
   eid=next_event(r['channel_id'],'message.updated',str(mid))
   return self.sendj(200,{'message':msgout(nr)})
  return self.sendj(404,{})
 def do_DELETE(self):
  a=auth(self.headers.get('Authorization'))
  if not a:return self.sendj(401,{})
  if self.path.startswith('/api/messages/') and '/reactions/' in self.path:
   from urllib.parse import unquote
   bits=self.path.split('/'); mid=bits[3]; emoji=unquote(bits[5]); m=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not m:return self.sendj(404,{})
   if not allowed(a['id'],m['channel_id']):return self.sendj(403,{})
   existed=q('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,a['id'],emoji),True)
   if existed:
    ex('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,a['id'],emoji)); next_event(m['channel_id'],'reaction.removed')
   return self.sendj(200,{'reactions':reactions_of(mid)})
  if self.path.startswith('/api/workspaces/') and '/groups/' in self.path:
   bits=self.path.split('/'); slug=bits[3]; handle=bits[5]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.sendj(404,{})
   g=q('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],handle),True)
   if not g:return self.sendj(200,{'deleted':True})
   m=member(a['id'],w['id'])
   if not m or (a['id']!=g['creator_id'] and a['id']!=w['owner_id']):return self.sendj(403,{})
   ex('DELETE FROM groups_ WHERE id=?',(g['id'],)); return self.sendj(200,{'deleted':True})
  if self.path.startswith('/api/messages/') and self.path.endswith('/pin'):
   mid=self.path.split('/')[3]; ex('DELETE FROM pins WHERE message_id=?',(mid,)); return self.sendj(200,{'unpinned':True})
  if self.path.startswith('/api/workspaces/') and '/invitations/' in self.path:
   bits=self.path.split('/'); slug=bits[3]; code=bits[5]; w=q('SELECT * FROM workspaces WHERE slug=?',(slug,),True)
   if not w:return self.sendj(404,{})
   m=member(a['id'],w['id'])
   if not m or m['role'] not in ('owner','admin'):return self.sendj(403,{})
   if not q('SELECT 1 FROM invitations WHERE code=? AND workspace_id=?',(code,w['id']),True):return self.sendj(404,{})
   ex('DELETE FROM invitations WHERE code=?',(code,)); return self.sendj(200,{'deleted':True})
  if self.path.startswith('/api/messages/'):
   mid=self.path.split('/')[3]; r=q('SELECT * FROM messages WHERE id=?',(mid,),True)
   if not r:return self.sendj(404,{})
   if r['deleted']:return self.sendj(200,{'deleted':True})
   w=q('SELECT * FROM workspaces WHERE id=(SELECT workspace_id FROM channels WHERE id=?)',(r['channel_id'],),True)
   if r['author_id']!=a['id'] and (not w or w['owner_id']!=a['id']):return self.sendj(403,{})
   ex('UPDATE messages SET deleted=1 WHERE id=?',(mid,)); ex('DELETE FROM reactions WHERE message_id=?',(mid,))
   next_event(r['channel_id'],'message.deleted',str(mid))
   return self.sendj(200,{'deleted':True})
  return self.sendj(404,{})
 def websocket(self,a):
  if not a:
   self.send_response(401); self.send_header('Connection','close'); self.send_header('Content-Length','0'); self.end_headers(); self.close_connection=True; return
  key=self.headers.get('Sec-WebSocket-Key')
  if not key:return self.sendj(400,{})
  accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
  self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
  wsid=secrets.token_urlsafe(12); ex('INSERT INTO online_sockets VALUES(?,?,?)',(wsid,a['id'],now()))
  subs={}; lock=threading.Lock(); alive=[True]; global_since=0
  def emit(cid,since):
   rows=q('SELECT * FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since))
   last=since
   for r in rows:
    if r['event_type'] in ('message.created','message.reply'):
     m=q('SELECT m.*,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.channel_id=? AND m.event_id=?',(cid,r['event_id']),True)
     if not m: continue
     obj=dict({'type':r['event_type'],'event_id':r['event_id'],'channel_id':cid},**msgout(m))
    elif r['event_type']=='message.updated':
     m=q('SELECT m.*,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?',(int(r['payload']),),True)
     obj={'type':'message.updated','event_id':r['event_id'],'channel_id':cid,'message':msgout(m)} if m else {'type':'message.updated','event_id':r['event_id'],'channel_id':cid}
    elif r['event_type']=='message.deleted':
     obj={'type':'message.deleted','event_id':r['event_id'],'channel_id':cid,'message_id':int(r['payload'])}
    else: obj={'type':r['event_type'],'event_id':r['event_id'],'channel_id':cid}
    with lock:self.wsframe(obj)
    last=r['event_id']
   return last
  def sender():
   while alive[0]:
    try:
     with lock: items=list(subs.items())
     for cid,last in items:
      n=emit(cid,last)
      with lock:
       if cid in subs: subs[cid]=n
     nonlocal_global = None
     evs=q('SELECT * FROM user_events WHERE id>? ORDER BY id',(sender.global_since,))
     for ev in evs:
      u=q('SELECT * FROM users WHERE id=?',(ev['user_id'],),True)
      with lock: self.wsframe({'type':'user.updated','user':userobj(u)})
      sender.global_since=ev['id']
     time.sleep(.03)
    except Exception: time.sleep(.05)
  sender.global_since=0
  threading.Thread(target=sender,daemon=True).start()
  def recv_exact(n):
   b=b''
   while len(b)<n:
    b += self.connection.recv(n-len(b))
   return b
  self.connection.settimeout(.15)
  try:
   while True:
    try:
     h=recv_exact(2)
    except socket.timeout:
     continue
    if not h:break
    opcode=h[0]&15; n=h[1]&127
    try:
     if n==126:n=struct.unpack('!H',recv_exact(2))[0]
     elif n==127:n=struct.unpack('!Q',recv_exact(8))[0]
     mask=recv_exact(4); data=bytearray(recv_exact(n))
    except socket.timeout:
     continue
    for i in range(n): data[i]^=mask[i%4]
    if opcode==8:
     with lock:
      try:self.wfile.write(bytes([136,0])); self.wfile.flush()
      except:pass
     break
    if opcode==9:
     with lock:
      try:self.wfile.write(bytes([138,n])+bytes(data)); self.wfile.flush()
      except:pass
     continue
    try:
     msg=json.loads(data); typ=msg.get('type'); cid=int(msg.get('channel_id'))
     if typ=='subscribe':
      head=q('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(cid,),True)['n']
      with lock: subs[cid]=head; self.wsframe({'type':'subscribed','channel_id':cid,'head_event_id':head})
     elif typ=='resume':
      since=int(msg.get('since_event_id',0)); n=emit(cid,since)
      with lock: subs[cid]=n; self.wsframe({'type':'resumed','channel_id':cid,'head_event_id':n})
    except: pass
  finally:
   alive[0]=False
   ex('DELETE FROM online_sockets WHERE token=?',(wsid,))
 def wsframe(self,obj):
  b=json.dumps(obj).encode(); n=len(b); h=bytes([129,n]) if n<126 else bytes([129,126])+struct.pack('!H',n); self.wfile.write(h+b); self.wfile.flush()
if __name__=='__main__':
 ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
