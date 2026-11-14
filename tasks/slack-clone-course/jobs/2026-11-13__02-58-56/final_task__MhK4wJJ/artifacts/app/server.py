import os, re, json, uuid, hashlib, sqlite3, threading, base64, struct, time, select
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

DB='/app/data/huddle.sqlite3'
db_lock=threading.RLock()
UI_HTML='<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><link rel="stylesheet" href="/static/app.css"></head><body>\n<div id="auth-modal"><form data-testid="auth-form" id="auth-form"><input name="username"><input name="password" type="password"><button type="submit" data-testid="auth-submit" data-button-role="primary">Log in</button><button type="button" data-testid="auth-toggle">Log in instead</button><div role="alert" style="display:none"></div></form></div>\n<div id="app">\n<header data-testid="workspace-header"><span data-testid="current-user"></span><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button data-testid="logout-btn" data-button-role="danger">Logout</button></header>\n<aside><div data-testid="channel-list"></div><div data-testid="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="empty-state-create-workspace">Create workspace</button><form data-testid="workspace-create-form"><input data-testid="workspace-name-input" name="name"><input name="slug"><button type="submit" data-testid="workspace-general-submit" data-button-role="primary">Create workspace</button></form><form data-testid="join-workspace-form"><input name="code"><span role="alert" data-testid="join-workspace-error" style="display:block"></span><button type="submit" data-testid="join-workspace-submit" data-button-role="primary">Join workspace</button></form></aside>\n<main><h2 data-testid="channel-title"></h2><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><div data-testid="message-list"></div><form><input data-testid="message-input" name="body"><button data-testid="send-btn" data-button-role="primary">Send</button></form></main>\n<section data-testid="thread-panel"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button></section>\n<div id="create-channel-modal"><form data-testid="create-channel-form"><input name="name"><span role="alert"></span><label><input type="checkbox" data-testid="create-channel-private"></label><button type="submit" data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form></div>\n<div id="channel-settings-modal"><h3 data-testid="channel-settings-title"></h3><button data-testid="channel-settings-close" data-button-role="secondary">Close</button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary">Add member</button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Save topic</button><button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button></div>\n<div id="workspace-settings-modal"><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button><button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general">General</button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><div data-testid="member-row"><span data-testid="role-badge"></span><select data-testid="role-select"></select></div><button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button data-testid="create-invitation-submit" data-button-role="primary">Create invitation</button><div data-testid="invitations-list"></div></form></div>\n</div><script src="/static/app.js"></script></body></html>'
UI_CSS='*{box-sizing:border-box}body{margin:0;font:14px Arial;color:#1d1c1d;background:#fff}#app{display:grid;grid-template-columns:240px 1fr;min-height:100vh}header{grid-column:1/3;background:#4a154b;color:white;padding:14px}aside{background:#3f0e40;color:white;padding:18px;min-height:800px}main{padding:24px;max-width:900px;min-height:800px}.channel-entry{padding:8px;cursor:pointer}.message{padding:10px;border-bottom:1px solid #eee}.thread-panel,.modal{padding:16px}.thread-panel{position:fixed;left:calc(100% - 360px);right:0;top:60px;width:360px;height:calc(100vh - 60px);background:#fff;border-left:1px solid #ddd;z-index:5}.message{position:relative}.toolbar{display:flex;gap:4px;visibility:visible;opacity:1}.toolbar button{cursor:pointer}.message button{position:relative;z-index:10}button[data-button-role=primary]{background-color:#007a5a;color:#fff}button[data-button-role=danger]{background-color:#e01e5a;color:#fff}button[data-button-role=secondary]{background-color:#1264a3;color:#fff}button{border:0;border-radius:4px;padding:8px 12px;margin:4px}input,select{padding:8px;margin:4px}#auth-modal{padding:40px}'
UI_JS="(()=>{const q=s=>document.querySelector(s),t=s=>q('[data-testid='+s+']');let token=localStorage.getItem('huddle.token'),slug='',cur=null;const api=async(p,o={})=>{o.headers={...(token?{Authorization:'Bearer '+token}:{}),'Content-Type':'application/json'};let r=await fetch('/api'+p,o),x=await r.json();if(!r.ok)throw Error(x.error||'request failed');return x};function mode(m){t('auth-form').dataset.mode=m;t('auth-submit').textContent=m==='register'?'Sign up':'Log in'}async function start(){if(!token){q('#auth-modal').style.display='block';q('#app').style.display='none';mode('register');return}let me=(await api('/auth/me')).user;q('#auth-modal').style.display='none';q('#app').style.display='grid';t('current-user').textContent=me.username;let z=await api('/workspaces');if(!z.workspaces.length)return;let w=z.workspaces[0];slug=w.slug;t('workspace-header').firstChild.textContent=w.name;let d=await api('/workspaces/'+slug),l=t('channel-list');l.innerHTML='';d.channels.forEach(c=>{let e=document.createElement('div');e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.textContent='#'+c.name;e.onclick=()=>open(c);l.append(e)});if(d.channels[0])open(d.channels[0])}async function open(c){cur=c;t('channel-title').textContent=c.name;t('channel-topic').textContent=c.topic||'';let x=await api('/channels/'+c.id+'/messages'),l=t('message-list');l.innerHTML='';x.messages.slice().reverse().forEach(m=>{let r=document.createElement('div');r.dataset.testid='message';r.textContent=m.body;l.append(r)})}t('auth-toggle').onclick=()=>mode(t('auth-form').dataset.mode==='register'?'login':'register');t('auth-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),a=q('#auth-form [role=alert]');try{let r=await fetch('/api/auth/'+(e.target.dataset.mode==='register'?'register':'login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:f.get('username'),password:f.get('password')})}),x=await r.json();if(!r.ok)throw Error(x.error);token=x.token;localStorage.setItem('huddle.token',token);start()}catch(x){a.textContent=x.message;a.style.display='block'}};t('logout-btn').onclick=()=>{localStorage.removeItem('huddle.token');location.reload()};t('workspace-create-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target);try{await api('/workspaces',{method:'POST',body:JSON.stringify({name:f.get('name'),slug:f.get('slug')})});slug=f.get('slug');await start()}catch(x){let a=e.target.querySelector('[role=alert]')||document.createElement('span');a.setAttribute('role','alert');a.textContent=x.message;e.target.append(a)}};t('join-workspace-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),a=t('join-workspace-error');try{await api('/invitations/'+f.get('code')+'/accept',{method:'POST'})}catch(x){a.textContent=x.message;a.style.display='block'}};t('new-channel-btn').onclick=()=>q('#create-channel-modal').style.display='block';t('create-channel-cancel').onclick=()=>q('#create-channel-modal').style.display='none';t('create-channel-form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target),a=e.target.querySelector('[role=alert]');try{await api('/workspaces/'+slug+'/channels',{method:'POST',body:JSON.stringify({name:f.get('name'),is_private:false})});q('#create-channel-modal').style.display='none';await start();let ne=document.querySelector('[data-testid=channel-entry][data-channel-name=\"'+f.get('name')+'\"]');if(ne)ne.click()}catch(x){a.setAttribute('role','alert');a.textContent=x.message;a.style.display='block'}};t('channel-settings-btn').onclick=()=>q('#channel-settings-modal').style.display='block';t('channel-settings-close').onclick=()=>q('#channel-settings-modal').style.display='none';t('channel-topic-submit').onclick=async()=>{await api('/channels/'+cur.id,{method:'PATCH',body:JSON.stringify({topic:t('channel-topic-input').value})});t('channel-topic').textContent=t('channel-topic-input').value};t('workspace-settings-btn').onclick=async()=>{q('#workspace-settings-modal').style.display='block';let z=await api('/workspaces/'+slug+'/members'),p=t('settings-pane-members');p.innerHTML='';z.members.forEach(m=>{let r=document.createElement('div');r.dataset.testid='member-row';let b=document.createElement('span');b.dataset.testid='role-badge';b.textContent=m.role;r.append(m.user_id,b);p.append(r)})};t('settings-tab-members').onclick=()=>{t('settings-pane-members').style.display='block'};t('settings-tab-invitations').onclick=()=>{t('settings-pane-invitations').style.display='block'};t('create-invitation-btn').onclick=()=>t('create-invitation-form').style.display='block';t('create-invitation-form').onsubmit=async e=>{e.preventDefault();let z=await api('/workspaces/'+slug+'/invitations',{method:'POST',body:JSON.stringify({})}),v=document.createElement('span');v.dataset.testid='invitation-code';v.textContent=z.invitation.code;t('invitations-list').append(v)};mode('register');start()})()"
def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def conn():
    c=sqlite3.connect(DB, timeout=30, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=30000')
    return c
def init():
    with db_lock:
      c=conn()
      c.executescript('''
      CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
      CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
      CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
      CREATE TABLE IF NOT EXISTS memberships(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
      CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
      CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
      CREATE TABLE IF NOT EXISTS dms(user_low INTEGER,user_high INTEGER,channel_id INTEGER UNIQUE,PRIMARY KEY(user_low,user_high));
      CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,created_at TEXT);
      CREATE TABLE IF NOT EXISTS online_sessions(session TEXT PRIMARY KEY,user_id INTEGER,created_at REAL);
      CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
      CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
      CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,invited_user_id INTEGER,expires_at REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,revoked INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
      CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,added_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
      CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
      CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
      CREATE INDEX IF NOT EXISTS msg_chan ON messages(channel_id,parent_id,id);
      CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
      CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
      CREATE TABLE IF NOT EXISTS channel_events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
      ''')
      c.commit(); c.close()
def user_obj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def ws_obj(r):
    return {k:r[k] for k in ('id','slug','name','owner_id','join_mode')}
def ch_obj(r):
    return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
def msg_obj(c,r):
    a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    replies=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    mids=[x['user_id'] for x in c.execute('SELECT user_id FROM message_mentions WHERE message_id=? ORDER BY user_id',(r['id'],)).fetchall()]
    files=[]
    for x in c.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)).fetchall():
        files.append({k:x[k] for k in ('id','uploader_id','filename','content_type','size','created_at')})
    reactions=[]
    for x in c.execute('SELECT emoji,MIN(added_at) first_added FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY first_added',(r['id'],)).fetchall():
        ids=[z['user_id'] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=? ORDER BY added_at,user_id',(r['id'],x['emoji'])).fetchall()]
        reactions.append({'emoji':x['emoji'],'count':len(ids),'user_ids':ids})
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':files,'reactions':reactions,'mentions':mids}
def pageval(q):
    x=parse_qs(q); lim=x.get('limit',['50'])[0]
    try: lim=int(lim)
    except: raise ValueError()
    if lim<1 or lim>200: raise ValueError()
    cur=x.get('cursor',[None])[0]
    if cur:
        try:
            if not cur.startswith('c'): raise ValueError()
            off=int(cur[1:])
        except: raise ValueError()
    else: off=0
    return lim,off
def append_event(c,channel_id,kind,payload):
    r=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(channel_id,)).fetchone()
    eid=r['n']; c.execute('INSERT INTO channel_events VALUES(?,?,?,?,?)',(channel_id,eid,kind,json.dumps(payload),now())); return eid
def ws_frame(data):
    b=data.encode() if isinstance(data,str) else data
    n=len(b)
    if n<126: h=bytes([0x81,n])
    elif n<65536: h=bytes([0x81,126])+struct.pack('>H',n)
    else: h=bytes([0x81,127])+struct.pack('>Q',n)
    return h+b
def token_user(h):
    if not h or not h.startswith('Bearer '): return None
    t=h[7:].strip()
    c=conn(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close()
    return r
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a): pass
    def sendj(self,status,obj):
        b=json.dumps(obj,separators=(',',':')).encode()
        self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        try:return json.loads(self.rfile.read(n) or b'{}')
        except:return {}
    def auth(self):
        u=token_user(self.headers.get('Authorization'))
        if not u:self.sendj(401,{'error':'unauthorized'}); return None
        return u
    def do_GET(self):
        parsed=urlparse(self.path); p=parsed.path; parts=p.strip('/').split('/')
        if p=='/api/ws':
            tok=parse_qs(parsed.query).get('token',[''])[0]
            c=conn(); valid=c.execute('SELECT u.id FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone(); c.close()
            if not valid:
                self.send_response(401); self.send_header('Content-Length','0'); self.end_headers(); return
            key=self.headers.get('Sec-WebSocket-Key')
            if not key or self.headers.get('Upgrade','').lower()!='websocket':
                self.send_response(400); self.send_header('Content-Length','0'); self.end_headers(); return
            accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101,'Switching Protocols')
            self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
            self.close_connection=True
            self.connection.settimeout(None)
            self.rfile=self.connection.makefile('rb',buffering=0)
            session=uuid.uuid4().hex
            cc=conn(); cc.execute('INSERT OR REPLACE INTO online_sessions VALUES(?,?,?)',(session,valid['id'],time.time())); cc.commit(); cc.close()
            subscribed={}
            global_last=0
            def send(o): self.connection.sendall(ws_frame(json.dumps(o,separators=(',',':'))))
            def events(cid,since):
                cc=conn()
                rr=cc.execute('SELECT * FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
                cc.close(); return rr
            try:
                while True:
                    try:
                        ready,_,_=select.select([self.connection],[],[],0.1)
                        if ready:
                            def readn(n):
                                z=b''
                                while len(z)<n:
                                    q=self.rfile.read(n-len(z))
                                    if not q: return b''
                                    z+=q
                                return z
                            h=readn(2)
                            if len(h)<2: break
                            b=h[1]; ln=b&127
                            if ln==126: ln=struct.unpack('>H',readn(2))[0]
                            elif ln==127: ln=struct.unpack('>Q',readn(8))[0]
                            mask=readn(4) if b&128 else b''; data=readn(ln)
                            if mask:data=bytes(x^mask[i%4] for i,x in enumerate(data))
                            if (h[0]&15)==8: break
                            if (h[0]&15)==9:self.connection.sendall(bytes([0x8A,len(data)])+data);continue
                            try: msg=json.loads(data)
                            except: continue
                            typ=msg.get('type'); cid=int(msg.get('channel_id',0))
                            if typ=='subscribe':
                                subscribed[cid]=0
                                cc=conn(); head=cc.execute('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n'];cc.close()
                                subscribed[cid]=head;send({'type':'subscribed','channel_id':cid,'head_event_id':head})
                            elif typ=='resume':
                                since=int(msg.get('since_event_id',0))
                                for r in events(cid,since):
                                    x=json.loads(r['payload']);x.update(type=r['kind'],event_id=r['event_id'],channel_id=cid);send(x)
                                cc=conn();head=cc.execute('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n'];cc.close()
                                subscribed[cid]=head;send({'type':'resumed','channel_id':cid,'head_event_id':head})
                    except TimeoutError: pass
                    cc=conn(); cc.execute('UPDATE online_sessions SET created_at=? WHERE session=?',(time.time(),session)); cc.commit(); cc.close()
                    gc=conn()
                    for r in gc.execute('SELECT * FROM global_events WHERE id>? ORDER BY id',(global_last,)).fetchall():
                        x=json.loads(r['payload']); x['type']=r['kind']; send(x); global_last=r['id']
                    gc.close()
                    for cid,last in list(subscribed.items()):
                        for r in events(cid,last):
                            x=json.loads(r['payload']);x.update(type=r['kind'],event_id=r['event_id'],channel_id=cid);send(x);subscribed[cid]=r['event_id']
            except Exception: pass
            cc=conn(); cc.execute('DELETE FROM online_sessions WHERE session=?',(session,)); cc.commit(); cc.close()
            return
        if p=='/api/health':
            self.sendj(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))}); return
        if p=='/':
            b=UI_HTML.encode(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        if p=='/static/app.css':
            b=UI_CSS.encode(); self.send_response(200); self.send_header('Content-Type','text/css'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        if p=='/static/app.js':
            b=UI_JS.replace('\\\\n','\\n').encode('utf-8','surrogatepass'); self.send_response(200); self.send_header('Content-Type','application/javascript'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        u=self.auth()
        if not u:return
        c=conn()
        if len(parts)==3 and parts[:2]==['api','files']:
            f=c.execute('SELECT * FROM files WHERE id=?',(parts[2],)).fetchone()
            if not f:self.sendj(404,{'error':'not found'});c.close();return
            self.sendj(200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}});c.close();return
        if len(parts)==4 and parts[:2]==['api','files'] and parts[3]=='download':
            f=c.execute('SELECT * FROM files WHERE id=?',(parts[2],)).fetchone()
            if not f:self.sendj(404,{'error':'not found'});c.close();return
            try:b=open(f['path'],'rb').read()
            except:self.sendj(404,{'error':'not found'});c.close();return
            self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename'].replace('"','')+'"');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);c.close();return
        if len(parts)==4 and parts[:2]==['api','channels'] and parts[3]=='read':
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            rs=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone()
            last=rs['last_read_event_id'] if rs else 0
            unread=c.execute("SELECT COUNT(*) n FROM channel_events e JOIN messages m ON m.id=CAST(json_extract(e.payload,'$.id') AS INTEGER) WHERE e.channel_id=? AND e.event_id>? AND e.kind IN ('message.created','message.reply') AND m.author_id!=?",(ch['id'],last,u['id'])).fetchone()['n']
            mentions=c.execute('SELECT COUNT(*) n FROM message_mentions mm JOIN messages m ON m.id=mm.message_id JOIN channel_events e ON e.channel_id=m.channel_id AND CAST(json_extract(e.payload,"$.id") AS INTEGER)=m.id AND e.kind IN ("message.created","message.reply") WHERE mm.user_id=? AND m.channel_id=? AND e.event_id>?',(u['id'],ch['id'],last)).fetchone()['n']
            self.sendj(200,{'read_state':{'channel_id':ch['id'],'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}});c.close();return
        if len(parts)==4 and parts[:2]==['api','channels'] and parts[3]=='messages':
            try: lim,off=pageval(parsed.query)
            except: self.sendj(400,{'error':'invalid pagination'});c.close();return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            if ch['is_dm'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():
                self.sendj(403,{'error':'forbidden'});c.close();return
            boundary=off if parsed.query.find('cursor=')>=0 else 9223372036854775807
            rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(ch['id'],boundary,lim+1)).fetchall()
            more=len(rows)>lim; rows=rows[:lim]
            self.sendj(200,{'messages':[msg_obj(c,r) for r in rows],'next_cursor':('c'+str(rows[-1]['id'])) if more and rows else None});c.close();return
        if len(parts)==4 and parts[:2]==['api','messages'] and parts[3]=='replies':
            try: lim,off=pageval(parsed.query)
            except: self.sendj(400,{'error':'invalid pagination'});c.close();return
            parent=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(parts[2],)).fetchone()
            if not parent:self.sendj(404,{'error':'not found'});c.close();return
            rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC LIMIT ? OFFSET ?',(parent['id'],lim+1,off)).fetchall()
            more=len(rows)>lim;rows=rows[:lim]
            self.sendj(200,{'replies':[msg_obj(c,r) for r in rows],'next_cursor':('c'+str(off+lim)) if more else None});c.close();return
        if p=='/api/search':
            try: lim,off=pageval(parsed.query)
            except: self.sendj(400,{'error':'invalid pagination'});c.close();return
            q=parse_qs(parsed.query).get('q',[''])[0]
            workspace_id=parse_qs(parsed.query).get('workspace_id',[None])[0]
            boundary=off if parsed.query.find('cursor=')>=0 else 9223372036854775807
            sql='SELECT DISTINCT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN memberships mb ON mb.workspace_id=ch.workspace_id AND mb.user_id=? LEFT JOIN channel_members cm ON cm.channel_id=ch.id AND cm.user_id=? WHERE m.body LIKE ? AND m.deleted=0 AND (ch.is_private=0 OR cm.user_id IS NOT NULL)'
            args=[u['id'],u['id'],'%'+q+'%']
            if workspace_id: sql+=' AND ch.workspace_id=?';args.append(workspace_id)
            sql+=' AND m.id<? ORDER BY m.id DESC LIMIT ?';args += [boundary,lim+1]
            rows=c.execute(sql,args).fetchall()
            more=len(rows)>lim;rows=rows[:lim]
            self.sendj(200,{'results':[msg_obj(c,r) for r in rows],'next_cursor':('c'+str(rows[-1]['id'])) if more and rows else None});c.close();return
        if len(parts)==4 and parts[:2]==['api','channels'] and parts[3]=='pins':
            try: lim,off=pageval(parsed.query)
            except: self.sendj(400,{'error':'invalid pagination'});c.close();return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():self.sendj(403,{'error':'forbidden'});c.close();return
            rows=c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC LIMIT ? OFFSET ?',(ch['id'],lim+1,off)).fetchall()
            more=len(rows)>lim;rows=rows[:lim]
            self.sendj(200,{'pins':[{'message':msg_obj(c,r),'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']} for r in rows],'next_cursor':('c'+str(off+lim)) if more else None});c.close();return
        if p=='/api/auth/me': self.sendj(200,{'user':user_obj(u)}); c.close(); return
        if p=='/api/workspaces':
            rows=c.execute('SELECT w.* FROM workspaces w JOIN memberships m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
            self.sendj(200,{'workspaces':[ws_obj(x) for x in rows]}); c.close(); return
        if len(parts)==4 and parts[:3]==['api','workspaces',''] : pass
        if len(parts)>=3 and parts[0:2]==['api','workspaces']:
            slug=parts[2]; w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
            if not w:self.sendj(404,{'error':'not found'}); c.close(); return
            mem=c.execute('SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if len(parts)==4 and parts[3]=='groups':
                if not mem:self.sendj(403,{'error':'forbidden'});c.close();return
                gs=c.execute('SELECT * FROM groups WHERE workspace_id=? ORDER BY id',(w['id'],)).fetchall()
                out=[]
                for g in gs:
                    ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id',(g['id'],)).fetchall()]
                    out.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids})
                self.sendj(200,{'groups':out});c.close();return
            if len(parts)==4 and parts[3]=='invitations':
                if not mem or c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()['role'] not in ('owner','admin'):
                    self.sendj(403,{'error':'forbidden'})
                else:
                    rows=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)',(w['id'],time.time())).fetchall()
                    self.sendj(200,{'invitations':[dict(x) for x in rows]})
                c.close(); return
            if len(parts)==4 and parts[3]=='members':
                if not mem:self.sendj(403,{'error':'forbidden'})
                else:
                    rows=c.execute('SELECT user_id,role FROM memberships WHERE workspace_id=?',(w['id'],)).fetchall()
                    self.sendj(200,{'members':[dict(x) for x in rows]})
                c.close(); return
            if not mem:self.sendj(403,{'error':'forbidden'}); c.close(); return
            include=urlparse(self.path).query in ('include_archived=1','include_archived=true')
            chans=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],int(include))).fetchall()
            visible=[]
            for x in chans:
                if x['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(x['id'],u['id'])).fetchone():
                    continue
                visible.append(ch_obj(x))
            states=[]
            for x in visible:
                cid=x['id']; head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM channel_events WHERE channel_id=?',(cid,)).fetchone()['n']
                rs=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
                last=rs['last_read_event_id'] if rs else 0
                unread=c.execute("SELECT COUNT(*) n FROM channel_events e JOIN messages m ON m.id=CAST(json_extract(e.payload,'$.id') AS INTEGER) WHERE e.channel_id=? AND e.event_id>? AND e.kind IN ('message.created','message.reply') AND m.author_id!=?",(cid,last,u['id'])).fetchone()['n']
                mentions=c.execute('SELECT COUNT(*) n FROM message_mentions mm JOIN messages m ON m.id=mm.message_id JOIN channel_events e ON e.channel_id=m.channel_id AND CAST(json_extract(e.payload,"$.id") AS INTEGER)=m.id AND e.kind IN ("message.created","message.reply") WHERE mm.user_id=? AND m.channel_id=? AND e.event_id>?',(u['id'],cid,last)).fetchone()['n']
                states.append({'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions})
            self.sendj(200,{'workspace':ws_obj(w),'channels':visible,'read_state':states}); c.close(); return
        if len(parts)==4 and parts[:2]==['api','channels'] and parts[3]=='members':
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            rows=c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)).fetchall()
            self.sendj(200,{'members':[user_obj(x) for x in rows]});c.close();return
        if len(parts)>=3 and parts[:2]==['api','users']:
            r=c.execute('SELECT * FROM users WHERE id=?',(parts[2],)).fetchone()
            if not r:self.sendj(404,{'error':'not found'})
            else:self.sendj(200,{'user':user_obj(r)})
            c.close(); return
        self.sendj(404,{'error':'not found'}); c.close()
    def do_PATCH(self):
        p=urlparse(self.path).path; parts=p.strip('/').split('/'); d=self.body(); c=conn(); u=token_user(self.headers.get('Authorization'))
        if not u:self.sendj(401,{'error':'unauthorized'});c.close();return
        if len(parts)==3 and parts[:2]==['api','channels']:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            m=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            member=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
            if not member and (not m or m['role']!='owner'):self.sendj(403,{'error':'forbidden'});c.close();return
            topic=d.get('topic','')
            if not isinstance(topic,str) or len(topic)>250:self.sendj(400,{'error':'invalid topic'});c.close();return
            c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));c.commit()
            self.sendj(200,{'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.close();return
        if len(parts)==3 and parts[:2]==['api','channels']:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            m=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            member=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
            if not member and (not m or m['role']!='owner'):self.sendj(403,{'error':'forbidden'});c.close();return
            topic=d.get('topic','')
            if not isinstance(topic,str) or len(topic)>250:self.sendj(400,{'error':'invalid topic'});c.close();return
            c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));c.commit()
            self.sendj(200,{'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.close();return
        if len(parts)==3 and parts[:2]==['api','workspaces']:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone()
            m=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w:self.sendj(404,{'error':'not found'});c.close();return
            if not m or m['role'] not in ('owner','admin'):self.sendj(403,{'error':'forbidden'});c.close();return
            jm=d.get('join_mode',w['join_mode'])
            if jm not in ('open','invite_only'):self.sendj(400,{'error':'invalid join_mode'});c.close();return
            c.execute('UPDATE workspaces SET name=?,join_mode=? WHERE id=?',(d.get('name',w['name']),jm,w['id']));c.commit()
            self.sendj(200,{'workspace':ws_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())});c.close();return
        self.sendj(404,{'error':'not found'});c.close()
    def do_POST(self):
        p=urlparse(self.path).path; parts=p.strip('/').split('/')
        ismultipart=self.headers.get('Content-Type','').lower().startswith('multipart/form-data')
        d={} if ismultipart else self.body()
        c=conn()
        if p=='/api/files':
            u=token_user(self.headers.get('Authorization'))
            if not u:self.sendj(401,{'error':'unauthorized'});c.close();return
            try:
                import re as _re
                raw=self.rfile.read(int(self.headers.get('Content-Length','0') or 0))
                ct=self.headers.get('Content-Type','')
                bm=_re.search(r'boundary=(?:"([^"]+)"|([^;]+))',ct)
                if not bm: raise ValueError()
                boundary=(bm.group(1) or bm.group(2)).encode()
                item=None
                for part in raw.split(b'--'+boundary):
                    if b'Content-Disposition:' not in part: continue
                    head,sep,body=part.partition(b'\r\n\r\n')
                    if not sep or b'name="file"' not in head: continue
                    fm=_re.search(br'filename="([^"]*)"',head)
                    if not fm: continue
                    item=(fm.group(1).decode('utf-8','replace'),body.rstrip(b'\r\n-'))
                    break
                if not item: raise ValueError()
                fn,data=item
                if len(data)>10485760:self.sendj(413,{'error':'file too large'});c.close();return
                cm=_re.search(r'Content-Type:\s*([^\r\n;]+)',head.decode('latin1'),_re.I)
                fct=cm.group(1) if cm else 'application/octet-stream'
                fn=os.path.basename(fn); path='/app/data/upload-'+uuid.uuid4().hex
                with open(path,'wb') as out: out.write(data)
                cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],fn,fct,len(data),now(),path));c.commit()
                f=c.execute('SELECT * FROM files WHERE id=?',(cur.lastrowid,)).fetchone()
                self.sendj(201,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
            except Exception:self.sendj(400,{'error':'missing file'})
            c.close();return
        if p=='/api/auth/register':
            un=d.get('username'); pw=d.get('password')
            if not isinstance(un,str) or not re.fullmatch(r'[A-Za-z0-9_]+',un or '') or not isinstance(pw,str) or len(pw)<8:
                self.sendj(400,{'error':'invalid registration'}); c.close(); return
            try:
                cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name') or un)); uid=cur.lastrowid
                t=uuid.uuid4().hex+uuid.uuid4().hex; c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
                r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); self.sendj(201,{'user':user_obj(r),'token':t})
            except sqlite3.IntegrityError:self.sendj(409,{'error':'duplicate username'})
            c.close(); return
        if p=='/api/auth/login':
            r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(str(d.get('password','')).encode()).hexdigest())).fetchone()
            if not r:self.sendj(401,{'error':'invalid credentials'}); c.close(); return
            t=uuid.uuid4().hex+uuid.uuid4().hex; c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); self.sendj(200,{'user':user_obj(r),'token':t}); c.close(); return
        u=token_user(self.headers.get('Authorization'))
        if not u:self.sendj(401,{'error':'unauthorized'}); c.close(); return
        if p=='/api/workspaces':
            slug=d.get('slug','')
            if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not d.get('name'): self.sendj(400,{'error':'invalid workspace'}); c.close(); return
            try:
                cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d['name'],u['id'])); wid=cur.lastrowid
                c.execute('INSERT INTO memberships VALUES(?,?,?)',(wid,u['id'],'owner')); cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=cur.lastrowid
                c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
                w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                self.sendj(201,{'workspace':ws_obj(w),'general_channel':ch_obj(ch)})
            except sqlite3.IntegrityError:self.sendj(409,{'error':'duplicate slug'})
            c.close(); return
        if p=='/api/dms':
            other=d.get('user_id')
            if other is None:self.sendj(400,{'error':'user_id required'});c.close();return
            otherrow=c.execute('SELECT * FROM users WHERE id=?',(other,)).fetchone()
            if not otherrow:self.sendj(404,{'error':'not found'});c.close();return
            lo,hi=sorted((u['id'],int(other)))
            old=c.execute('SELECT channel_id FROM dms WHERE user_low=? AND user_high=?',(lo,hi)).fetchone()
            if old:self.sendj(200,{'channel_id':old['channel_id']});c.close();return
            c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(NULL,?,?,1)',('',1))
            cid=c.execute('SELECT last_insert_rowid() x').fetchone()['x']
            c.execute('INSERT INTO dms VALUES(?,?,?)',(lo,hi,cid))
            c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,lo));c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,hi));c.commit()
            self.sendj(200,{'channel_id':cid});c.close();return
        if len(parts)==4 and parts[:2]==['api','workspaces'] and parts[3]=='groups':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone()
            mem=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w:self.sendj(404,{'error':'not found'});c.close();return
            if not mem:self.sendj(403,{'error':'forbidden'});c.close();return
            handle=d.get('handle',''); name=d.get('name',''); ids=d.get('user_ids',[])
            if not isinstance(handle,str) or not re.fullmatch(r'[a-z0-9_-]{2,32}',handle) or not isinstance(name,str) or not name or not isinstance(ids,list):
                self.sendj(400,{'error':'invalid group'});c.close();return
            try:
                cur=c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],handle,name,u['id']));gid=cur.lastrowid
                for uid in set(ids):
                    if c.execute('SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
                c.commit();self.sendj(201,{'group':{'id':gid,'handle':handle,'name':name,'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(gid,)).fetchall()]}})
            except sqlite3.IntegrityError:self.sendj(409,{'error':'duplicate handle'})
            c.close();return
        if len(parts)>=4 and parts[:2]==['api','workspaces'] and parts[3]=='channels':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone()
            if not w:self.sendj(404,{'error':'not found'}); c.close(); return
            m=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not m:self.sendj(403,{'error':'forbidden'}); c.close(); return
            private=bool(d.get('is_private'))
            if m['role']=='guest' or (private and m['role'] not in ('owner','admin')):
                self.sendj(403,{'error':'forbidden'}); c.close(); return
            name=d.get('name','')
            if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(d.get('topic',''))>250:self.sendj(400,{'error':'invalid channel'});c.close();return
            try:
                cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(private),d.get('topic',''))); cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
                self.sendj(201,{'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
            except sqlite3.IntegrityError:self.sendj(409,{'error':'duplicate channel'})
            c.close();return
        if len(parts)==4 and parts[:2]==['api','channels'] and parts[3] in ('leave','archive','unarchive'):
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            m=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if parts[3]=='leave':
                existed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
                c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id']))
                if existed: append_event(c,ch['id'],'member.left',{'user_id':u['id']})
                c.commit();self.sendj(200,{'channel':ch_obj(ch)});c.close();return
            if not m or m['role']!='owner':self.sendj(403,{'error':'forbidden'});c.close();return
            flag=1 if parts[3]=='archive' else 0
            c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,ch['id']))
            append_event(c,ch['id'],'channel.archived' if flag else 'channel.unarchived',{'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
            c.commit();self.sendj(200,{'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.close();return
        if len(parts)==4 and parts[:2]==['api','messages'] and parts[3]=='reactions':
            m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(parts[2],)).fetchone()
            if not m:self.sendj(404,{'error':'not found'});c.close();return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
            if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():self.sendj(403,{'error':'forbidden'});c.close();return
            emoji=d.get('emoji')
            if not isinstance(emoji,str) or not emoji:self.sendj(400,{'error':'emoji required'});c.close();return
            oldr=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],emoji)).fetchone()
            if not oldr:
                c.execute('INSERT INTO reactions VALUES(?,?,?,?)',(m['id'],u['id'],emoji,now()))
                append_event(c,ch['id'],'reaction.added',{'message_id':m['id'],'emoji':emoji,'user_id':u['id']});c.commit()
            self.sendj(200,{'reactions':msg_obj(c,m)['reactions']});c.close();return
        if len(parts)==4 and parts[:2]==['api','messages'] and parts[3]=='pin':
            m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(parts[2],)).fetchone()
            if not m:self.sendj(404,{'error':'not found'});c.close();return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
            if ch['is_archived']:self.sendj(423,{'error':'archived'});c.close();return
            old=c.execute('SELECT * FROM pins WHERE message_id=?',(m['id'],)).fetchone()
            if not old:
                c.execute('INSERT INTO pins VALUES(?,?,?)',(m['id'],u['id'],now()));c.commit()
                old=c.execute('SELECT * FROM pins WHERE message_id=?',(m['id'],)).fetchone()
            self.sendj(200,{'pin':{'message_id':old['message_id'],'pinned_by':old['pinned_by'],'pinned_at':old['pinned_at']}});c.close();return
        if len(parts)==4 and parts[:2]==['api','channels'] and parts[3]=='messages':
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            if ch['is_dm'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():
                self.sendj(403,{'error':'forbidden'});c.close();return
            body=d.get('body')
            if isinstance(body,str) and body.startswith('/') and not body.startswith('//'):
                command,_,text=body[1:].partition(' ')
                text=text.strip()
                w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone() if ch['workspace_id'] else None
                wm=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
                cm=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
                if command=='me':
                    if not text:self.sendj(400,{'error':'/me requires text'});c.close();return
                elif command=='shrug':
                    body=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
                elif command=='topic':
                    if not isinstance(text,str) or len(text)>250 or (not cm and (not wm or wm['role']!='owner')):
                        self.sendj(403 if not cm and (not wm or wm['role']!='owner') else 400,{'error':'forbidden' if not cm and (not wm or wm['role']!='owner') else 'invalid topic'});c.close();return
                    if ch['is_archived']:self.sendj(423,{'error':'archived'});c.close();return
                    c.execute('UPDATE channels SET topic=? WHERE id=?',(text,ch['id']))
                    append_event(c,ch['id'],'channel.updated',{'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.commit()
                    self.sendj(201,{'message':None,'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.close();return
                elif command=='invite':
                    if not wm or wm['role'] not in ('owner','admin') or not text.startswith('@'):
                        self.sendj(403,{'error':'forbidden'});c.close();return
                    target=c.execute('SELECT * FROM users WHERE username=?',(text[1:].strip(),)).fetchone()
                    if not target:self.sendj(404,{'error':'not found'});c.close();return
                    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],target['id']))
                    if w:c.execute('INSERT OR IGNORE INTO memberships VALUES(?,?,?)',(w['id'],target['id'],'member'))
                    c.commit();self.sendj(201,{'message':None,'invited_user':user_obj(target)});c.close();return
                elif command in ('archive','unarchive'):
                    if not wm or wm['role']!='owner':self.sendj(403,{'error':'forbidden'});c.close();return
                    flag=1 if command=='archive' else 0
                    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,ch['id']))
                    append_event(c,ch['id'],'channel.archived' if flag else 'channel.unarchived',{'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.commit()
                    self.sendj(201,{'message':None,'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.close();return
                else:
                    self.sendj(400,{'error':'unknown command'});c.close();return
            if ch['is_archived']:self.sendj(423,{'error':'archived'});c.close();return
            if not isinstance(body,str) or not body.strip():self.sendj(400,{'error':'empty body'});c.close();return
            file_ids=d.get('file_ids',[])
            if not isinstance(file_ids,list):self.sendj(400,{'error':'invalid files'});c.close();return
            selected=[]
            for fid in file_ids:
                f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
                if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():
                    self.sendj(400,{'error':'invalid file'});c.close();return
                selected.append(f)
            parent_id=d.get('parent_id')
            if parent_id is not None:
                parent=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(parent_id,)).fetchone()
                if not parent or parent['channel_id']!=ch['id']:
                    self.sendj(400,{'error':'invalid parent'});c.close();return
            with db_lock:
                c.execute('BEGIN IMMEDIATE')
                c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,parent_id,now())); r0=c.execute('SELECT * FROM messages WHERE id=last_insert_rowid()').fetchone()
            import re as _re
            names=_re.findall(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_-]+)(?![A-Za-z0-9_.])',body)
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            ids=set()
            for nm in names:
                if nm=='channel':
                    ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=? AND user_id!=?',(ch['id'],u['id'])).fetchall())
                elif nm=='here':
                    ids.update(x['user_id'] for x in c.execute('SELECT cm.user_id FROM channel_members cm JOIN online_sessions os ON os.user_id=cm.user_id AND os.created_at>? WHERE cm.channel_id=? AND cm.user_id!=?',(time.time()-2,ch['id'],u['id'])).fetchall())
                else:
                    z=c.execute('SELECT id FROM users WHERE username=? AND id!=?',(nm,u['id'])).fetchone()
                    if z and w and c.execute('SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],z['id'])).fetchone():
                        ids.add(z['id'])
                    if w:
                        g=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(w['id'],nm)).fetchone()
                        if g:
                            ids.update(x['user_id'] for x in c.execute('SELECT gm.user_id FROM group_members gm JOIN channel_members cm ON cm.user_id=gm.user_id WHERE gm.group_id=? AND cm.channel_id=? AND gm.user_id!=?',(g['id'],ch['id'],u['id'])).fetchall())
            for mid in ids:c.execute('INSERT OR IGNORE INTO message_mentions VALUES(?,?)',(r0['id'],mid))
            for f in selected:c.execute('INSERT INTO message_files VALUES(?,?)',(r0['id'],f['id']))
            append_event(c,ch['id'],'message.reply' if parent_id else 'message.created',msg_obj(c,r0)); c.commit()
            r=r0
            self.sendj(201,{'message':msg_obj(c,r)});c.close();return
        if len(parts)==4 and parts[:2]==['api','channels'] and parts[3]=='read':
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            try:x=int(d.get('last_read_event_id',0))
            except:self.sendj(400,{'error':'invalid event id'});c.close();return
            old=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone()
            last=max(x,old['last_read_event_id'] if old else 0)
            c.execute('INSERT INTO read_states(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=?',(u['id'],ch['id'],last,last));c.commit()
            self.sendj(200,{'read_state':{'channel_id':ch['id'],'last_read_event_id':last,'unread_count':0,'mention_count':0}});c.close();return
        if len(parts)==4 and parts[:2]==['api','channels'] and parts[3]=='join':
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            if ch['is_private']:self.sendj(403,{'error':'private channel'});c.close();return
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            existing=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not existing and w['join_mode']=='invite_only':
                self.sendj(403,{'error':'invitation required'});c.close();return
            if existing and existing['role']=='guest':
                self.sendj(403,{'error':'forbidden'});c.close();return
            before=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
            c.execute('INSERT OR IGNORE INTO memberships VALUES(?,?,?)',(w['id'],u['id'],'member'));c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id']))
            if not before: append_event(c,ch['id'],'member.joined',{'user_id':u['id']})
            c.commit();self.sendj(200,{'channel':ch_obj(ch)});c.close();return
        if len(parts)==4 and parts[:2]==['api','workspaces'] and parts[3]=='invitations':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone()
            m=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w:self.sendj(404,{'error':'not found'});c.close();return
            if not m or m['role'] not in ('owner','admin'):self.sendj(403,{'error':'forbidden'});c.close();return
            try: maxu=int(d.get('max_uses',1))
            except: maxu=0
            if maxu<1:self.sendj(400,{'error':'invalid max_uses'});c.close();return
            target=d.get('invited_user_id')
            if d.get('invited_username'):
                r=c.execute('SELECT id FROM users WHERE username=?',(d['invited_username'],)).fetchone()
                if not r:self.sendj(400,{'error':'unknown user'});c.close();return
                target=r['id']
            if target:maxu=1
            exp=None if d.get('expires_in_seconds') is None else time.time()+int(d['expires_in_seconds'])
            code=uuid.uuid4().hex
            c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],target,exp,maxu,0));c.commit()
            self.sendj(201,{'invitation':dict(c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone())});c.close();return
        if len(parts)==4 and parts[:2]==['api','invitations'] and parts[3]=='accept':
            inv=c.execute('SELECT * FROM invitations WHERE code=?',(parts[2],)).fetchone()
            if not inv or inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['expires_at'] and inv['expires_at']<=time.time()):
                self.sendj(404,{'error':'invalid invitation'});c.close();return
            if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:
                self.sendj(403,{'error':'wrong target'});c.close();return
            c.execute('INSERT OR IGNORE INTO memberships VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'))
            c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(parts[2],));c.commit()
            self.sendj(200,{'workspace_id':inv['workspace_id']});c.close();return
        if len(parts)==4 and parts[:2]==['api','workspaces'] and parts[3]=='transfer_ownership':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone()
            if not w:self.sendj(404,{'error':'not found'});c.close();return
            actor=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            target_id=d.get('new_owner_id'); target=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],target_id)).fetchone()
            if not actor or actor['role']!='owner':self.sendj(403,{'error':'forbidden'});c.close();return
            if not target or target['role']!='admin':self.sendj(400,{'error':'target must be admin'});c.close();return
            c.execute('UPDATE memberships SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
            c.execute('UPDATE memberships SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target_id))
            c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target_id,w['id']));c.commit()
            self.sendj(200,{'workspace':ws_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())});c.close();return
        self.sendj(404,{'error':'not found'});c.close()
    def do_PATCH(self):
        p=urlparse(self.path).path; parts=p.strip('/').split('/'); d=self.body(); c=conn(); u=token_user(self.headers.get('Authorization'))
        if not u:self.sendj(401,{'error':'unauthorized'});c.close();return
        if p=='/api/users/me':
            allowed={'display_name':(1,80),'status_text':(0,100),'status_emoji':(0,64),'avatar_url':(0,2048),'timezone':None}
            vals={}
            try:
                for k,v in d.items():
                    if k not in allowed or not isinstance(v,str): raise ValueError()
                    if allowed[k] and not (allowed[k][0]<=len(v)<=allowed[k][1]): raise ValueError()
                    if k=='timezone':
                        from zoneinfo import ZoneInfo
                        ZoneInfo(v)
                    vals[k]=v
            except: self.sendj(400,{'error':'invalid profile'});c.close();return
            if vals:
                c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',list(vals.values())+[u['id']])
            r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
            c.execute('INSERT INTO global_events(kind,payload,created_at) VALUES(?,?,?)',('user.updated',json.dumps({'user':user_obj(r)}),now()));c.commit()
            self.sendj(200,{'user':user_obj(r)});c.close();return
        if len(parts)==3 and parts[:2]==['api','messages']:
            m=c.execute('SELECT * FROM messages WHERE id=?',(parts[2],)).fetchone()
            if not m:self.sendj(404,{'error':'not found'});c.close();return
            if m['deleted']:self.sendj(409,{'error':'deleted'});c.close();return
            if m['author_id']!=u['id']:self.sendj(403,{'error':'forbidden'});c.close();return
            body=d.get('body')
            if not isinstance(body,str) or not body.strip():self.sendj(400,{'error':'empty body'});c.close();return
            edited=now()
            c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,edited,m['id']))
            updated=c.execute('SELECT * FROM messages WHERE id=?',(m['id'],)).fetchone()
            append_event(c,m['channel_id'],'message.updated',{'message':msg_obj(c,updated)});c.commit()
            self.sendj(200,{'message':msg_obj(c,updated)});c.close();return
        if len(parts)==5 and parts[:2]==['api','workspaces'] and parts[3]=='groups':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone()
            g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],parts[4])).fetchone() if w else None
            if not w or not g:self.sendj(404,{'error':'not found'});c.close();return
            wm=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not wm or (g['creator_id']!=u['id'] and wm['role']!='owner'):self.sendj(403,{'error':'forbidden'});c.close();return
            name=d.get('name',g['name']); ids=d.get('user_ids')
            if not isinstance(name,str) or not name:self.sendj(400,{'error':'invalid group'});c.close();return
            c.execute('UPDATE groups SET name=? WHERE id=?',(name,g['id']))
            if ids is not None:
                if not isinstance(ids,list):self.sendj(400,{'error':'invalid users'});c.close();return
                c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
                for uid in set(ids):
                    if c.execute('SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():c.execute('INSERT INTO group_members VALUES(?,?)',(g['id'],uid))
            c.commit();self.sendj(200,{'group':{'id':g['id'],'handle':g['handle'],'name':name,'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]}});c.close();return
        if len(parts)==3 and parts[:2]==['api','channels']:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(parts[2],)).fetchone()
            if not ch:self.sendj(404,{'error':'not found'});c.close();return
            if ch['is_archived']:
                self.sendj(423,{'error':'archived'});c.close();return
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            m=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            member=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
            if not member and (not m or m['role']!='owner'):
                self.sendj(403,{'error':'forbidden'});c.close();return
            topic=d.get('topic','')
            if not isinstance(topic,str) or len(topic)>250:
                self.sendj(400,{'error':'invalid topic'});c.close();return
            c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));append_event(c,ch['id'],'channel.updated',{'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.commit()
            self.sendj(200,{'channel':ch_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())});c.close();return
        if len(parts)==3 and parts[:2]==['api','workspaces']:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone()
            m=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w:self.sendj(404,{'error':'not found'});c.close();return
            if not m or m['role'] not in ('owner','admin'):self.sendj(403,{'error':'forbidden'});c.close();return
            jm=d.get('join_mode',w['join_mode'])
            if jm not in ('open','invite_only'):self.sendj(400,{'error':'invalid join_mode'});c.close();return
            c.execute('UPDATE workspaces SET name=?,join_mode=? WHERE id=?',(d.get('name',w['name']),jm,w['id']));c.commit()
            self.sendj(200,{'workspace':ws_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())});c.close();return
        if len(parts)==5 and parts[:2]==['api','workspaces'] and parts[3]=='members':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone()
            if not w:self.sendj(404,{'error':'not found'});c.close();return
            actor=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            target_id=parts[4]; target=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],target_id)).fetchone(); role=d.get('role')
            if not target:self.sendj(404,{'error':'not member'});c.close();return
            if role not in ('owner','admin','member','guest'):self.sendj(400,{'error':'invalid role'});c.close();return
            if role=='owner':self.sendj(400,{'error':'use transfer'});c.close();return
            if not actor or actor['role'] not in ('owner','admin') or int(target_id)==u['id'] or target['role']=='owner' or (actor['role']=='admin' and target['role']=='admin'):
                self.sendj(403,{'error':'forbidden'});c.close();return
            c.execute('UPDATE memberships SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target_id));c.commit()
            self.sendj(200,{'member':{'user_id':int(target_id),'role':role}});c.close();return
        self.sendj(404,{'error':'not found'});c.close()
    def do_DELETE(self):
        p=urlparse(self.path).path; parts=p.strip('/').split('/'); c=conn(); u=token_user(self.headers.get('Authorization'))
        if not u:self.sendj(401,{'error':'unauthorized'});c.close();return
        if len(parts)==3 and parts[:2]==['api','messages']:
            m=c.execute('SELECT * FROM messages WHERE id=?',(parts[2],)).fetchone()
            if not m:self.sendj(404,{'error':'not found'});c.close();return
            if m['deleted']:self.sendj(200,{'deleted':True});c.close();return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone() if ch and ch['workspace_id'] else None
            owner=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if m['author_id']!=u['id'] and (not owner or owner['role']!='owner'):
                self.sendj(403,{'error':'forbidden'});c.close();return
            c.execute('UPDATE messages SET deleted=1 WHERE id=?',(m['id'],))
            c.execute('DELETE FROM reactions WHERE message_id=?',(m['id'],))
            append_event(c,m['channel_id'],'message.deleted',{'message_id':m['id']});c.commit()
            self.sendj(200,{'deleted':True});c.close();return
        if len(parts)==4 and parts[:2]==['api','messages'] and parts[3]=='pin':
            m=c.execute('SELECT * FROM messages WHERE id=?',(parts[2],)).fetchone()
            if not m:self.sendj(404,{'error':'not found'});c.close();return
            c.execute('DELETE FROM pins WHERE message_id=?',(m['id'],));c.commit();self.sendj(200,{'unpinned':True});c.close();return
        if len(parts)==5 and parts[:2]==['api','messages'] and parts[3]=='reactions':
            import urllib.parse
            mid,emoji=parts[2],urllib.parse.unquote(parts[4])
            m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
            if not m:self.sendj(404,{'error':'not found'});c.close();return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
            if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():self.sendj(403,{'error':'forbidden'});c.close();return
            oldr=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],emoji)).fetchone()
            if oldr:
                c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],emoji))
                append_event(c,ch['id'],'reaction.removed',{'message_id':m['id'],'emoji':emoji,'user_id':u['id']});c.commit()
            self.sendj(200,{'reactions':msg_obj(c,m)['reactions']});c.close();return
        if len(parts)==5 and parts[:2]==['api','workspaces'] and parts[3]=='groups':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone()
            g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],parts[4])).fetchone() if w else None
            if not w:self.sendj(404,{'error':'not found'});c.close();return
            if not g:self.sendj(200,{'deleted':True});c.close();return
            wm=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not wm or (g['creator_id']!=u['id'] and wm['role']!='owner'):self.sendj(403,{'error':'forbidden'});c.close();return
            c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit();self.sendj(200,{'deleted':True});c.close();return
        if len(parts)==5 and parts[:2]==['api','workspaces'] and parts[3]=='invitations':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(parts[2],)).fetchone(); m=c.execute('SELECT role FROM memberships WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w:self.sendj(404,{'error':'not found'});c.close();return
            if not m or m['role'] not in ('owner','admin'):self.sendj(403,{'error':'forbidden'});c.close();return
            if not c.execute('SELECT 1 FROM invitations WHERE code=? AND workspace_id=?',(parts[4],w['id'])).fetchone():self.sendj(404,{'error':'not found'});c.close();return
            c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(parts[4],));c.commit();self.sendj(200,{'deleted':True});c.close();return
        self.sendj(404,{'error':'not found'});c.close()
def main():
    init(); port=int(os.environ.get('PORT','8000')); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
if __name__=='__main__':main()
