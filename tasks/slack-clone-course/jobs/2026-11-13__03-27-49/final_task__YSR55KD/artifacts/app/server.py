#!/usr/bin/env python3
import os, re, json, sqlite3, hashlib, secrets, threading, base64, socket, uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs,unquote

DB='/app/data/huddle.db'
os.makedirs('/app/data',exist_ok=True)
lock=threading.RLock()

def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def db():
    c=sqlite3.connect(DB,timeout=5)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    with db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
        CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
        CREATE TABLE IF NOT EXISTS online_users(user_id INTEGER PRIMARY KEY,updated_at REAL);
        CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
        CREATE TABLE IF NOT EXISTS file_links(file_id INTEGER PRIMARY KEY,message_id INTEGER);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
        CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
        CREATE TABLE IF NOT EXISTS dms(user_a INTEGER,user_b INTEGER,channel_id INTEGER UNIQUE,PRIMARY KEY(user_a,user_b));
        CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
        CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires_at REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
        CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS channel_events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));\n        CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
        CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);\n        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
        ''')
init()
with db() as _c:
    try: _c.execute("ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'"); _c.commit()
    except sqlite3.OperationalError: pass

def user_obj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def workspace_obj(r):
    return {k:r[k] for k in ('id','slug','name','owner_id','join_mode')}
def channel_obj(r):
    d={k:r[k] for k in ('id','workspace_id','name','topic')}
    d.update(is_private=bool(r['is_private']),is_dm=bool(r['is_dm']),is_archived=bool(r['is_archived']))
    return d
def cursor_encode(pos):
    return base64.urlsafe_b64encode(json.dumps({'p':int(pos)}).encode()).decode().rstrip('=')
def cursor_decode(x):
    try:
        if not x: return 0
        v=json.loads(base64.urlsafe_b64decode(x+'='*((4-len(x)%4)%4)))
        if not isinstance(v,dict) or not isinstance(v.get('p'),int) or v['p']<0: raise ValueError()
        return v['p']
    except Exception: raise ValueError()
def page_args(qs):
    lim=int(qs.get('limit',['50'])[0])
    if lim<1 or lim>200: raise ValueError()
    return lim,cursor_decode(qs.get('cursor',[None])[0])
def resolve_mentions(c,ch,author_id,body):
    w=c.execute('SELECT workspace_id FROM channels WHERE id=?',(ch,)).fetchone()
    ids=[]
    if '@channel' in body:
        ids=[x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=? AND user_id!=?',(ch,author_id)).fetchall()]
    if '@here' in body:
        ids += [x['user_id'] for x in c.execute('SELECT cm.user_id FROM channel_members cm JOIN online_users o ON o.user_id=cm.user_id WHERE cm.channel_id=? AND cm.user_id!=?',(ch,author_id)).fetchall()]
    for token in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body):
        if token in ('channel','here'): continue
        x=c.execute('SELECT u.id FROM users u JOIN members wm ON wm.user_id=u.id WHERE u.username=? AND wm.workspace_id=?',(token,w['workspace_id'])).fetchone()
        if x and x['id']!=author_id: ids.append(x['id'])
        g=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(w['workspace_id'],token)).fetchone()
        if g:
            ids += [z['user_id'] for z in c.execute('SELECT user_id FROM group_members WHERE group_id=? AND user_id!=? AND user_id IN (SELECT user_id FROM channel_members WHERE channel_id=?)',(g['id'],author_id,ch)).fetchall()]
    return list(dict.fromkeys(ids))

def read_state(c,ch,uid):
    r=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(uid,ch)).fetchone()
    last=r['last_read_event_id'] if r else 0
    unread=c.execute("SELECT COUNT(*) FROM channel_events e JOIN messages m ON m.id=json_extract(e.payload,'$.id') WHERE e.channel_id=? AND e.event_id>? AND m.author_id!=?",(ch,last,uid)).fetchone()[0]
    mentions=c.execute("SELECT COUNT(*) FROM channel_events e WHERE e.channel_id=? AND e.event_id>? AND json_extract(e.payload,'$.mentions') LIKE ?",(ch,last,'%'+str(uid)+'%')).fetchone()[0]
    return {'channel_id':int(ch),'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
def reactions_of(c,message_id):
    rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY rowid',(message_id,)).fetchall()
    out=[]
    for r in rows:
        x=next((z for z in out if z['emoji']==r['emoji']),None)
        if not x:
            x={'emoji':r['emoji'],'count':0,'user_ids':[]};out.append(x)
        x['user_ids'].append(r['user_id']);x['count']+=1
    return out
def message_obj(c,r):
    au=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    n=c.execute('SELECT COUNT(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
    fs=[dict(x) for x in c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id IN (SELECT file_id FROM file_links WHERE message_id=?)',(r['id'],)).fetchall()]
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(au),'body':r['body'],'parent_id':r['parent_id'],'reply_count':n,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':reactions_of(c,r['id']),'mentions':json.loads(r['mentions'] or '[]')}

def append_event(channel_id,kind,payload):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM channel_events WHERE channel_id=?',(channel_id,)).fetchone()[0]
        c.execute('INSERT INTO channel_events VALUES(?,?,?,?,?)',(channel_id,n,kind,json.dumps(payload),now()))
        c.commit()
        return n
def ws_frame(obj):
    b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
    if n<126: h=bytes([129,n])
    elif n<65536: h=bytes([129,126])+n.to_bytes(2,'big')
    else: h=bytes([129,127])+n.to_bytes(8,'big')
    return h+b
def ws_read(sock):
    h=sock.recv(2)
    if len(h)<2:return None
    n=h[1]&127
    if n==126:n=int.from_bytes(sock.recv(2),'big')
    elif n==127:n=int.from_bytes(sock.recv(8),'big')
    if h[1]&128:
        key=sock.recv(4); b=bytearray()
        while len(b)<n:
            x=sock.recv(n-len(b))
            if not x: return None
            b.extend(x)
        for i in range(n): b[i]^=key[i%4]
        return json.loads(bytes(b))
    b=sock.recv(n); return json.loads(b)
def send(h,status,obj,headers=None):
    b=json.dumps(obj).encode()
    h.send_response(status); h.send_header('Content-Type','application/json'); h.send_header('Content-Length',str(len(b)))
    for k,v in (headers or {}).items(): h.send_header(k,str(v))
    h.end_headers(); h.wfile.write(b)
def auth(h,c):
    x=h.headers.get('Authorization','')
    if not x.startswith('Bearer '): return None
    r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(x[7:],)).fetchone()
    return r
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='Huddle/1'
    def log_message(self,*a): pass
    def body(self):
        try:
            n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
        except: return {}
    def route(self):
        return urlparse(self.path).path.strip('/').split('/')
    def do_GET(self):
        q=urlparse(self.path)
        if q.path=='/':
            try:b=open('/app/index.html','rb').read()
            except FileNotFoundError:b=b'<h1>Huddle</h1>'
            self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        if q.path=='/static/app.js':
            b=open('/app/static_app.js','rb').read();self.send_response(200);self.send_header('Content-Type','application/javascript');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        if q.path=='/static/app.css':
            b=open('/app/static_app.css','rb').read();self.send_response(200);self.send_header('Content-Type','text/css');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b);return
        if q.path=='/api/ws':
            tok=parse_qs(q.query).get('token',[''])[0]
            with db() as c: valid=bool(tok and c.execute('SELECT 1 FROM tokens WHERE token=?',(tok,)).fetchone())
            if not valid:self.send_response(401);self.end_headers();return
            key=self.headers.get('Sec-WebSocket-Key')
            if not key:self.send_response(400);self.end_headers();return
            accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
            sock=self.connection; sock.settimeout(.2); channel=None; sent=0
            with db() as oc:
                global_sent=oc.execute('SELECT COALESCE(MAX(id),0) FROM global_events').fetchone()[0]
            with db() as oc:
                ou=oc.execute('SELECT user_id FROM tokens WHERE token=?',(tok,)).fetchone()
                if ou: oc.execute('INSERT OR REPLACE INTO online_users VALUES(?,?)',(ou['user_id'],__import__('time').time()));oc.commit()
            online_uid=ou['user_id'] if ou else None
            try:
                while True:
                    try:
                        msg=ws_read(sock)
                        if msg:
                            typ=msg.get('type'); channel=int(msg.get('channel_id',0))
                            with db() as c:
                                head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM channel_events WHERE channel_id=?',(channel,)).fetchone()[0]
                                if typ=='subscribe':
                                    sent=head; sock.sendall(ws_frame({'type':'subscribed','channel_id':channel,'head_event_id':head}))
                                elif typ=='resume':
                                    since=int(msg.get('since_event_id',0)); rows=c.execute('SELECT event_id,kind,payload FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel,since)).fetchall()
                                    for e in rows:
                                        payload=json.loads(e['payload'])
                                        x={'type':e['kind'],'event_id':e['event_id'],'channel_id':channel}
                                        if e['kind'] in ('message.created','message.reply','message.updated'): x['message']=payload
                                        else: x.update(payload)
                                        sock.sendall(ws_frame(x));sent=e['event_id']
                                    sock.sendall(ws_frame({'type':'resumed','channel_id':channel,'head_event_id':head}))
                    except socket.timeout: pass
                    with db() as c:
                        grows=c.execute('SELECT id,kind,payload FROM global_events WHERE id>? ORDER BY id',(global_sent,)).fetchall()
                    for ge in grows:
                        sock.sendall(ws_frame({'type':ge['kind'],'user':json.loads(ge['payload'])})); global_sent=ge['id']
                    if channel:
                        with db() as c:
                            rows=c.execute('SELECT event_id,kind,payload FROM channel_events WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel,sent)).fetchall()
                        for e in rows:
                            payload=json.loads(e['payload'])
                            x={'type':e['kind'],'event_id':e['event_id'],'channel_id':channel}
                            if e['kind'] in ('message.created','message.reply','message.updated'): x['message']=payload
                            else: x.update(payload)
                            sock.sendall(ws_frame(x));sent=e['event_id']
            except (ConnectionError,OSError,ValueError): pass
            if online_uid:
                with db() as oc: oc.execute('DELETE FROM online_users WHERE user_id=?',(online_uid,));oc.commit()
            return
        p=self.route()
        with lock, db() as c:
            if p==['api','health']:
                return send(self,200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))})
            u=auth(self,c)
            if p[:3]==['api','auth','me']:
                return send(self,200,{'user':user_obj(u)}) if u else send(self,401,{'error':'unauthorized'})
            if not u: return send(self,401,{'error':'unauthorized'})
            if len(p)==3 and p[0:2]==['api','users']:
                r=c.execute('SELECT * FROM users WHERE id=?',(p[2],)).fetchone()
                if not r:return send(self,404,{'error':'not found'})
                return send(self,200,{'user':user_obj(r)})
            if len(p) in (3,4) and p[:2]==['api','files']:
                f=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at,path FROM files WHERE id=?',(p[2],)).fetchone()
                if not f:return send(self,404,{'error':'not found'})
                if len(p)==4 and p[3]=='download':
                    try:
                        raw=open(f['path'],'rb').read()
                    except FileNotFoundError:return send(self,404,{'error':'not found'})
                    self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename'].replace('"','')+'"');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
                return send(self,200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
            if p==['api','workspaces']:
                rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
                return send(self,200,{'workspaces':[workspace_obj(x) for x in rows]})
            if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='members':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                m=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall()
                return send(self,200,{'members':[dict(x) for x in m]})
            if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='invitations':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                mr=w and c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                if not mr or mr['role'] not in ('owner','admin'):return send(self,403,{'error':'forbidden'})
                rows=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses AND (expires_at IS NULL OR expires_at>?)',(w['id'],__import__('time').time())).fetchall()
                return send(self,200,{'invitations':[dict(x) for x in rows]})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='messages':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                if ch['is_dm'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return send(self,403,{'error':'forbidden'})
                try: lim,pos=page_args(parse_qs(q.query))
                except Exception:return send(self,400,{'error':'invalid pagination'})
                if pos:
                    rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(ch['id'],pos,lim+1)).fetchall()
                else:
                    rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(ch['id'],lim+1)).fetchall()
                more=len(rows)>lim; shown=rows[:lim]
                return send(self,200,{'messages':[message_obj(c,x) for x in shown],'next_cursor':cursor_encode(shown[-1]['id']) if more and shown else None})
            if len(p)==4 and p[:2]==['api','messages'] and p[3]=='replies':
                par=c.execute('SELECT * FROM messages WHERE id=?',(p[2],)).fetchone()
                if not par:return send(self,404,{'error':'not found'})
                try: lim,pos=page_args(parse_qs(q.query))
                except Exception:return send(self,400,{'error':'invalid pagination'})
                rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC LIMIT ? OFFSET ?',(par['id'],lim+1,pos)).fetchall()
                more=len(rows)>lim
                return send(self,200,{'replies':[message_obj(c,x) for x in rows[:lim]],'next_cursor':cursor_encode(pos+lim) if more else None})
            if p[:2]==['api','search']:
                try: lim,pos=page_args(parse_qs(q.query))
                except Exception:return send(self,400,{'error':'invalid pagination'})
                qs=parse_qs(q.query); term=unquote(qs.get('q',[''])[0]); wid=qs.get('workspace_id',[None])[0]
                extra=' AND ch.workspace_id=?' if wid else ''
                args=['%'+term+'%',u['id']]
                if wid: args.append(wid)
                args.extend([lim+1,pos])
                rows=c.execute('SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ? AND (ch.is_private=0 OR ch.id IN (SELECT channel_id FROM channel_members WHERE user_id=?))'+extra+' ORDER BY m.id DESC LIMIT ? OFFSET ?',args).fetchall()
                more=len(rows)>lim
                return send(self,200,{'results':[message_obj(c,x) for x in rows[:lim]],'next_cursor':cursor_encode(pos+lim) if more else None})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='read':
                ch=c.execute('SELECT id FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                return send(self,200,{'read_state':read_state(c,ch['id'],u['id'])})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='pins':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return send(self,403,{'error':'forbidden'})
                try: lim,pos=page_args(parse_qs(q.query))
                except Exception:return send(self,400,{'error':'invalid pagination'})
                rows=c.execute('SELECT p.pinned_by,p.pinned_at,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC LIMIT ? OFFSET ?',(ch['id'],lim+1,pos)).fetchall()
                more=len(rows)>lim
                out=[{'message':message_obj(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows[:lim]]
                return send(self,200,{'pins':out,'next_cursor':cursor_encode(pos+lim) if more else None})
            if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='groups':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return send(self,403,{'error':'forbidden'})
                out=[]
                for g in c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall():
                    ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]
                    out.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids})
                return send(self,200,{'groups':out})
            if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='groups':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return send(self,403,{'error':'forbidden'})
                out=[]
                for g in c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],)).fetchall():
                    ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]
                    out.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids})
                return send(self,200,{'groups':out})
            if len(p)==3 and p[:2]==['api','workspaces']:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                inc=parse_qs(urlparse(self.path).query).get('include_archived',['0'])[0] in ('1','true')
                cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (? OR is_archived=0) AND (is_private=0 OR id IN (SELECT channel_id FROM channel_members WHERE user_id=?))',(w['id'],int(inc),u['id'])).fetchall()
                return send(self,200,{'workspace':workspace_obj(w),'channels':[channel_obj(x) for x in cs],'read_state':[read_state(c,x['id'],u['id']) for x in cs]})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='members':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                rows=c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)).fetchall()
                return send(self,200,{'members':[user_obj(x) for x in rows]})
            return send(self,404,{'error':'not found'})
    def do_POST_OLD(self):
        p=self.route()
        if len(p)==2 and p[:2]==['api','files']:
            with lock, db() as c:
                u=auth(self,c)
                if not u:return send(self,401,{'error':'unauthorized'})
                n=int(self.headers.get('Content-Length','0')); raw=self.rfile.read(n)
                if n>11000000:return send(self,413,{'error':'file too large'})
                from email.parser import BytesParser
                msg=BytesParser().parsebytes(('Content-Type: '+self.headers.get('Content-Type','')).encode()+b'\r\n\r\n'+raw)
                part=msg.get_payload(0) if msg.is_multipart() else None
                if not part or part.get_param('name',header='Content-Disposition')!='file':return send(self,400,{'error':'missing file'})
                blob=part.get_payload(decode=True) or b''
                if len(blob)>10485760:return send(self,413,{'error':'file too large'})
                filename=part.get_filename() or 'file'; ctype=part.get_content_type(); path='/app/data/file-'+uuid.uuid4().hex
                open(path,'wb').write(blob)
                cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],filename,ctype,len(blob),now(),path));c.commit()
                f=c.execute('SELECT id,uploader_id,filename,content_type,size,created_at FROM files WHERE id=?',(cur.lastrowid,)).fetchone()
                return send(self,201,{'file':dict(f)})
        data=self.body()
        with lock, db() as c:
            if p==['api','auth','register']:
                un=data.get('username'); pw=data.get('password')
                if not isinstance(un,str) or not re.fullmatch(r'[A-Za-z0-9_]+',un or '') or not isinstance(pw,str) or len(pw)<8:
                    return send(self,400,{'error':'invalid registration'})
                try:
                    cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),data.get('display_name') or un))
                    uid=cur.lastrowid; tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
                    r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
                    return send(self,201,{'user':user_obj(r),'token':tok})
                except sqlite3.IntegrityError:return send(self,409,{'error':'duplicate username'})
            if p==['api','auth','login']:
                r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(data.get('username'),hashlib.sha256(str(data.get('password','')).encode()).hexdigest())).fetchone()
                if not r:return send(self,401,{'error':'invalid credentials'})
                tok=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id']));c.commit()
                return send(self,200,{'user':user_obj(r),'token':tok})
            u=auth(self,c)
            if not u:return send(self,401,{'error':'unauthorized'})
            if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='groups':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return send(self,403,{'error':'forbidden'})
                h=data.get('handle'); name=data.get('name',''); ids=data.get('user_ids',[])
                if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9_-]{2,32}',h):return send(self,400,{'error':'invalid handle'})
                if not isinstance(name,str) or not isinstance(ids,list):return send(self,400,{'error':'invalid group'})
                try:
                    gid=c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,u['id'])).lastrowid
                    for uid in ids:
                        if c.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone():c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
                    c.commit()
                    got=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(gid,)).fetchall()]
                    return send(self,201,{'group':{'id':gid,'handle':h,'name':name,'member_user_ids':got}})
                except sqlite3.IntegrityError:return send(self,409,{'error':'duplicate handle'})
            if p==['api','dms']:
                other=data.get('user_id')
                if other is None:return send(self,400,{'error':'user_id required'})
                target=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
                if not target:return send(self,404,{'error':'user not found'})
                if int(other)==u['id']:return send(self,400,{'error':'invalid user'})
                a,b=sorted((u['id'],int(other)))
                row=c.execute('SELECT channel_id FROM dms WHERE user_a=? AND user_b=?',(a,b)).fetchone()
                if row:return send(self,200,{'channel_id':row['channel_id']})
                cid=c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(NULL,?,?,1)',(f'dm-{a}-{b}',1)).lastrowid
                c.execute('INSERT INTO dms VALUES(?,?,?)',(a,b,cid))
                c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a))
                c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,b));c.commit()
                return send(self,200,{'channel_id':cid})
            if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='invitations':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if not mr or mr['role'] not in ('owner','admin'):return send(self,403,{'error':'forbidden'})
                mu=data.get('invited_user_id')
                if data.get('invited_username'):
                    x=c.execute('SELECT id FROM users WHERE username=?',(data['invited_username'],)).fetchone()
                    if not x:return send(self,404,{'error':'user not found'})
                    mu=x['id']
                mx=data.get('max_uses',1)
                if not isinstance(mx,int) or mx<1:return send(self,400,{'error':'invalid max_uses'})
                import time
                exp=None if data.get('expires_in_seconds') is None else time.time()+float(data['expires_in_seconds'])
                code=secrets.token_urlsafe(18)
                c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],exp,1 if mu else mx,0,mu));c.commit()
                return send(self,201,{'invitation':dict(c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone())})
            if len(p)==4 and p[:2]==['api','invitations'] and p[3]=='accept':
                inv=c.execute('SELECT * FROM invitations WHERE code=?',(p[2],)).fetchone()
                import time
                if not inv or inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['expires_at'] is not None and inv['expires_at']<=time.time()):return send(self,404,{'error':'invalid invitation'})
                if inv['invited_user_id'] is not None and inv['invited_user_id']!=u['id']:return send(self,403,{'error':'wrong target'})
                if c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(inv['workspace_id'],u['id'])).fetchone():return send(self,404,{'error':'already member'})
                c.execute('INSERT INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'))
                c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(p[2],));c.commit()
                return send(self,200,{'workspace':workspace_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(inv['workspace_id'],)).fetchone())})
            if p==['api','workspaces']:
                slug=data.get('slug'); name=data.get('name')
                if not isinstance(slug,str) or not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not isinstance(name,str) or not name:
                    return send(self,400,{'error':'invalid workspace'})
                try:
                    w=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,name,u['id'])).lastrowid
                    ch=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(w,'general')).lastrowid
                    c.execute('INSERT INTO members VALUES(?,?,?)',(w,u['id'],'owner')); c.execute('INSERT INTO channel_members VALUES(?,?)',(ch,u['id']));c.commit()
                    wr=c.execute('SELECT * FROM workspaces WHERE id=?',(w,)).fetchone(); cr=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone()
                    return send(self,201,{'workspace':workspace_obj(wr),'general_channel':channel_obj(cr)})
                except sqlite3.IntegrityError:return send(self,409,{'error':'duplicate slug'})
            if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='channels':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if not mr:return send(self,403,{'error':'forbidden'})
                if mr['role']=='guest':return send(self,403,{'error':'forbidden'})
                name=data.get('name')
                if not isinstance(name,str) or not re.fullmatch(r'[a-z0-9-]{1,32}',name):return send(self,400,{'error':'invalid channel'})
                topic=data.get('topic','')
                if not isinstance(topic,str) or len(topic)>250:return send(self,400,{'error':'invalid topic'})
                private=1 if data.get('is_private',False) else 0
                if private and mr['role'] not in ('owner','admin'):return send(self,403,{'error':'forbidden'})
                try:
                    cid=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,private,topic)).lastrowid
                    c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit()
                    return send(self,201,{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
                except sqlite3.IntegrityError:return send(self,409,{'error':'duplicate channel'})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='read':
                ch=c.execute('SELECT id FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                old=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone()
                val=data.get('last_read_event_id',0)
                if not isinstance(val,int):return send(self,400,{'error':'invalid read state'})
                if not old or val>old['last_read_event_id']:
                    c.execute('INSERT INTO read_states(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id',(u['id'],ch['id'],val));c.commit()
                return send(self,200,{'read_state':read_state(c,ch['id'],u['id'])})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='messages':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return send(self,403,{'error':'forbidden'})
                body=data.get('body')
                if not isinstance(body,str) or not body.strip():return send(self,400,{'error':'empty message'})
                if body.startswith('/') and not body.startswith('//'):
                    bits=body.split(' ',1); cmd=bits[0]; text=bits[1].strip() if len(bits)>1 else ''
                    if cmd=='/me':
                        if not text:return send(self,400,{'error':'/me needs text'})
                    elif cmd=='/shrug':
                        body=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
                    elif cmd=='/topic':
                        if len(text)>250:return send(self,400,{'error':'invalid topic'})
                        w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
                        mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                        if not mr or (mr['role']!='owner' and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()):return send(self,403,{'error':'forbidden'})
                        c.execute('UPDATE channels SET topic=? WHERE id=?',(text,ch['id']));c.commit()
                        fresh=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone();append_event(ch['id'],'channel.updated',{'channel':channel_obj(fresh)})
                        return send(self,201,{'message':None,'channel':channel_obj(fresh)})
                    elif cmd=='/invite':
                        name=text[1:] if text.startswith('@') else text
                        target=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
                        if not target:return send(self,404,{'error':'user not found'})
                        if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],target['id'])).fetchone():return send(self,404,{'error':'user not found'})
                        cur=c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],target['id']));c.commit()
                        if cur.rowcount:append_event(ch['id'],'member.joined',{'user_id':target['id']})
                        return send(self,201,{'message':None,'invited_user':user_obj(target)})
                    elif cmd in ('/archive','/unarchive'):
                        w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
                        if w['owner_id']!=u['id']:return send(self,403,{'error':'forbidden'})
                        flag=1 if cmd=='/archive' else 0
                        c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,ch['id']));c.commit()
                        fresh=c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone()
                        append_event(ch['id'],'channel.archived' if flag else 'channel.unarchived',{'channel':channel_obj(fresh)})
                        return send(self,201,{'message':None,'channel':channel_obj(fresh)})
                    else:
                        return send(self,400,{'error':'unknown command'})
                if ch['is_archived']:return send(self,423,{'error':'channel archived'})
                parent=data.get('parent_id')
                if parent is not None:
                    pr=c.execute('SELECT channel_id FROM messages WHERE id=? AND deleted=0',(parent,)).fetchone()
                    if not pr:return send(self,404,{'error':'parent not found'})
                    if pr['channel_id']!=ch['id']:return send(self,400,{'error':'wrong channel'})
                file_ids=data.get('file_ids',[])
                if not isinstance(file_ids,list):return send(self,400,{'error':'invalid files'})
                for fid in file_ids:
                    f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
                    if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM file_links WHERE file_id=?',(fid,)).fetchone():return send(self,400,{'error':'invalid attachment'})
                mentions=resolve_mentions(c,ch['id'],u['id'],body)
                cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(ch['id'],u['id'],body,parent,now(),json.dumps(mentions)))
                for fid in file_ids:c.execute('INSERT INTO file_links(file_id,message_id) VALUES(?,?)',(fid,cur.lastrowid))
                c.commit(); r=c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone()
                au=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
                msg=message_obj(c,r)
                append_event(ch['id'],'message.reply' if parent is not None else 'message.created',msg)
                return send(self,201,{'message':msg})
            if len(p)==4 and p[:2]==['api','messages'] and p[3]=='reactions':
                m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(p[2],)).fetchone()
                if not m:return send(self,404,{'error':'not found'})
                emoji=data.get('emoji')
                if not isinstance(emoji,str) or not emoji:return send(self,400,{'error':'invalid emoji'})
                ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
                if ch['is_archived']:return send(self,423,{'error':'channel archived'})
                if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return send(self,403,{'error':'forbidden'})
                cur=c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?,?)',(m['id'],u['id'],emoji,now()));c.commit()
                if cur.rowcount:
                    append_event(ch['id'],'reaction.added',{'message_id':m['id'],'emoji':emoji,'user_id':u['id']})
                return send(self,200,{'reactions':reactions_of(c,m['id'])})
            if len(p)==4 and p[:2]==['api','messages'] and p[3]=='pin':
                m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(p[2],)).fetchone()
                if not m:return send(self,404,{'error':'not found'})
                ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
                if ch['is_archived']:return send(self,423,{'error':'channel archived'})
                if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():
                    return send(self,403,{'error':'forbidden'})
                existing=c.execute('SELECT * FROM pins WHERE message_id=?',(m['id'],)).fetchone()
                if existing: pin=existing
                else:
                    c.execute('INSERT INTO pins VALUES(?,?,?)',(m['id'],u['id'],now()));c.commit()
                    pin=c.execute('SELECT * FROM pins WHERE message_id=?',(m['id'],)).fetchone()
                return send(self,200,{'pin':{'message_id':pin['message_id'],'pinned_by':pin['pinned_by'],'pinned_at':pin['pinned_at']}})
            if len(p)==4 and p[:2]==['api','channels'] and p[3] in ('leave','archive','unarchive'):
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
                if p[3]=='leave':
                    cur=c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id']));c.commit()
                    if cur.rowcount: append_event(ch['id'],'member.left',{'user_id':u['id']})
                    return send(self,200,{'channel':channel_obj(ch)})
                mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if not mr or mr['role']!='owner':return send(self,403,{'error':'forbidden'})
                flag=1 if p[3]=='archive' else 0
                c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,ch['id']));c.commit()
                append_event(ch['id'],'channel.archived' if flag else 'channel.unarchived',{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
                return send(self,200,{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='join':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                if ch['is_private']:return send(self,403,{'error':'private channel'})
                w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
                existing=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if existing and existing['role']=='guest':return send(self,403,{'error':'forbidden'})
                if not existing:
                    if w['join_mode']!='open':return send(self,403,{'error':'invite required'})
                    c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(w['id'],u['id'],'member'))
                cur=c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id']));c.commit()
                if cur.rowcount: append_event(ch['id'],'member.joined',{'user_id':u['id']})
                return send(self,200,{'channel':channel_obj(ch)})
            return send(self,404,{'error':'not found'})
    def do_PATCH(self):
        p=self.route(); data=self.body()
        with lock, db() as c:
            u=auth(self,c)
            if not u:return send(self,401,{'error':'unauthorized'})
            if len(p)==3 and p[:2]==['api','messages']:
                m=c.execute('SELECT * FROM messages WHERE id=?',(p[2],)).fetchone()
                if not m:return send(self,404,{'error':'not found'})
                if m['deleted']:return send(self,409,{'error':'message deleted'})
                if m['author_id']!=u['id']:return send(self,403,{'error':'forbidden'})
                body=data.get('body')
                if not isinstance(body,str) or not body.strip():return send(self,400,{'error':'empty body'})
                edited=now()
                c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,edited,m['id']));c.commit()
                fresh=c.execute('SELECT * FROM messages WHERE id=?',(m['id'],)).fetchone(); obj=message_obj(c,fresh)
                append_event(m['channel_id'],'message.updated',obj)
                return send(self,200,{'message':obj})
            if p==['api','users','me']:
                fields=('display_name','timezone','avatar_url','status_text','status_emoji')
                vals={}
                for k in fields:
                    if k in data:
                        if not isinstance(data[k],str):return send(self,400,{'error':'invalid profile'})
                        limits={'display_name':(1,80),'timezone':(1,128),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
                        lo,hi=limits[k]
                        if len(data[k])<lo or len(data[k])>hi:return send(self,400,{'error':'invalid profile'})
                        vals[k]=data[k]
                if 'timezone' in vals:
                    try:
                        from zoneinfo import ZoneInfo
                        ZoneInfo(vals['timezone'])
                    except Exception:return send(self,400,{'error':'invalid timezone'})
                if vals:
                    c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],));c.commit()
                r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
                c.execute('INSERT INTO global_events(kind,payload,created_at) VALUES(?,?,?)',('user.updated',json.dumps(user_obj(r)),now()));c.commit()
                return send(self,200,{'user':user_obj(r)})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='read':
                ch=c.execute('SELECT id FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                old=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone()
                val=data.get('last_read_event_id',0)
                if not isinstance(val,int):return send(self,400,{'error':'invalid read state'})
                if not old or val>old['last_read_event_id']:
                    c.execute('INSERT INTO read_states(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id',(u['id'],ch['id'],val));c.commit()
                return send(self,200,{'read_state':read_state(c,ch['id'],u['id'])})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='messages':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                try: lim,pos=page_args(parse_qs(q.query))
                except Exception:return send(self,400,{'error':'invalid pagination'})
                if pos:
                    rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<? ORDER BY id DESC LIMIT ?',(ch['id'],pos,lim+1)).fetchall()
                else:
                    rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 ORDER BY id DESC LIMIT ?',(ch['id'],lim+1)).fetchall()
                more=len(rows)>lim; shown=rows[:lim]
                return send(self,200,{'messages':[message_obj(c,x) for x in shown],'next_cursor':cursor_encode(shown[-1]['id']) if more and shown else None})
            if len(p)==4 and p[:2]==['api','messages'] and p[3]=='replies':
                par=c.execute('SELECT * FROM messages WHERE id=?',(p[2],)).fetchone()
                if not par:return send(self,404,{'error':'not found'})
                try: lim,pos=page_args(parse_qs(q.query))
                except Exception:return send(self,400,{'error':'invalid pagination'})
                rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id ASC LIMIT ? OFFSET ?',(par['id'],lim+1,pos)).fetchall()
                more=len(rows)>lim
                return send(self,200,{'replies':[message_obj(c,x) for x in rows[:lim]],'next_cursor':cursor_encode(pos+lim) if more else None})
            if p[:2]==['api','search']:
                try: lim,pos=page_args(parse_qs(q.query))
                except Exception:return send(self,400,{'error':'invalid pagination'})
                qs=parse_qs(q.query); term=unquote(qs.get('q',[''])[0]); wid=qs.get('workspace_id',[None])[0]
                extra=' AND ch.workspace_id=?' if wid else ''
                args=['%'+term+'%',u['id']]
                if wid: args.append(wid)
                args.extend([lim+1,pos])
                rows=c.execute('SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ? AND (ch.is_private=0 OR ch.id IN (SELECT channel_id FROM channel_members WHERE user_id=?))'+extra+' ORDER BY m.id DESC LIMIT ? OFFSET ?',args).fetchall()
                more=len(rows)>lim
                return send(self,200,{'results':[message_obj(c,x) for x in rows[:lim]],'next_cursor':cursor_encode(pos+lim) if more else None})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='read':
                ch=c.execute('SELECT id FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                return send(self,200,{'read_state':read_state(c,ch['id'],u['id'])})
            if len(p)==4 and p[:2]==['api','channels'] and p[3]=='pins':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return send(self,403,{'error':'forbidden'})
                try: lim,pos=page_args(parse_qs(q.query))
                except Exception:return send(self,400,{'error':'invalid pagination'})
                rows=c.execute('SELECT p.pinned_by,p.pinned_at,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC LIMIT ? OFFSET ?',(ch['id'],lim+1,pos)).fetchall()
                more=len(rows)>lim
                out=[{'message':message_obj(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pinned_at']} for x in rows[:lim]]
                return send(self,200,{'pins':out,'next_cursor':cursor_encode(pos+lim) if more else None})
            if len(p)==5 and p[:2]==['api','workspaces'] and p[3]=='groups':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],p[4])).fetchone()
                if not g:return send(self,404,{'error':'not found'})
                mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if not mr or (g['creator_id']!=u['id'] and w['owner_id']!=u['id']):return send(self,403,{'error':'forbidden'})
                vals=[]
                if 'name' in data:
                    if not isinstance(data['name'],str):return send(self,400,{'error':'invalid name'})
                    c.execute('UPDATE groups SET name=? WHERE id=?',(data['name'],g['id']))
                if 'user_ids' in data:
                    if not isinstance(data['user_ids'],list):return send(self,400,{'error':'invalid user_ids'})
                    c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
                    for uid in data['user_ids']:
                        if c.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone():c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],uid))
                c.commit();g=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone()
                ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)).fetchall()]
                return send(self,200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids}})
            if len(p)==3 and p[:2]==['api','workspaces']:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                mr=w and c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                if not mr or mr['role'] not in ('owner','admin'):return send(self,403,{'error':'forbidden'})
                jm=data.get('join_mode',w['join_mode'])
                if jm not in ('open','invite_only'):return send(self,400,{'error':'invalid join_mode'})
                name=data.get('name',w['name'])
                c.execute('UPDATE workspaces SET name=?,join_mode=? WHERE id=?',(name,jm,w['id']));c.commit()
                return send(self,200,{'workspace':workspace_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
            if len(p)==3 and p[:2]==['api','channels']:
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
                if not ch:return send(self,404,{'error':'not found'})
                w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
                mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                cm=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
                topic=data.get('topic')
                if ch['is_archived']:return send(self,423,{'error':'channel archived'})
                if not isinstance(topic,str) or len(topic)>250:return send(self,400,{'error':'invalid topic'})
                if not cm and (not mr or mr['role']!='owner'):return send(self,403,{'error':'forbidden'})
                c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']));c.commit()
                append_event(ch['id'],'channel.updated',{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
                return send(self,200,{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
            if len(p)==5 and p[:2]==['api','workspaces'] and p[3]=='members':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                target=c.execute('SELECT user_id,role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],p[4])).fetchone()
                role=data.get('role')
                if not target:return send(self,404,{'error':'member not found'})
                if role not in ('owner','admin','member','guest'):return send(self,400,{'error':'invalid role'})
                if role=='owner':return send(self,400,{'error':'cannot assign owner'})
                if not actor or actor['role'] not in ('owner','admin'):return send(self,403,{'error':'forbidden'})
                if target['role']=='owner' or target['user_id']==u['id']:return send(self,403,{'error':'forbidden'})
                if actor['role']=='admin' and target['role']=='admin':return send(self,403,{'error':'forbidden'})
                c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],p[4]));c.commit()
                return send(self,200,{'member':{'user_id':target['user_id'],'role':role}})
            return send(self,404,{'error':'not found'})
    def _post_transfer(self):
        p=self.route(); data=self.body()
        with lock, db() as c:
            if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='transfer_ownership':
                u=auth(self,c)
                if not u:return send(self,401,{'error':'unauthorized'})
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],data.get('new_owner_id'))).fetchone()
                if not actor or actor['role']!='owner':return send(self,403,{'error':'forbidden'})
                if not target or target['role']!='admin':return send(self,400,{'error':'target must be admin'})
                c.execute('UPDATE members SET role=CASE WHEN user_id=? THEN ? WHEN user_id=? THEN ? ELSE role END WHERE workspace_id=?',(u['id'],'admin',data.get('new_owner_id'),'owner',w['id']))
                c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(data.get('new_owner_id'),w['id']));c.commit()
                return send(self,200,{'workspace':workspace_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
    def do_POST(self):
        p=self.route()
        if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='transfer_ownership':
            return self._transfer()
        return self.do_POST_OLD()
    def _transfer(self):
        return H._post_transfer(self)
    def do_DELETE(self):
        p=self.route()
        with lock, db() as c:
            u=auth(self,c)
            if not u:return send(self,401,{'error':'unauthorized'})
            if len(p)==3 and p[:2]==['api','messages']:
                m=c.execute('SELECT * FROM messages WHERE id=?',(p[2],)).fetchone()
                if not m:return send(self,404,{'error':'not found'})
                if m['deleted']:return send(self,200,{'deleted':True})
                ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
                w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
                if m['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']):return send(self,403,{'error':'forbidden'})
                c.execute('UPDATE messages SET deleted=1 WHERE id=?',(m['id'],))
                c.execute('DELETE FROM reactions WHERE message_id=?',(m['id'],));c.commit()
                append_event(m['channel_id'],'message.deleted',{'message_id':m['id']})
                return send(self,200,{'deleted':True})
            if len(p)==5 and p[:2]==['api','messages'] and p[3]=='reactions':
                from urllib.parse import unquote
                mid=p[2]; emoji=unquote(p[4])
                m=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(mid,)).fetchone()
                if not m:return send(self,404,{'error':'not found'})
                ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
                if ch['is_archived']:return send(self,423,{'error':'channel archived'})
                cur=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));c.commit()
                if cur.rowcount:
                    append_event(ch['id'],'reaction.removed',{'message_id':int(mid),'emoji':emoji,'user_id':u['id']})
                return send(self,200,{'reactions':reactions_of(c,mid)})
            if len(p)==4 and p[:2]==['api','messages'] and p[3]=='pin':
                m=c.execute('SELECT * FROM messages WHERE id=?',(p[2],)).fetchone()
                if not m:return send(self,404,{'error':'not found'})
                c.execute('DELETE FROM pins WHERE message_id=?',(m['id'],));c.commit()
                return send(self,200,{'unpinned':True})
            if len(p)==5 and p[:2]==['api','workspaces'] and p[3]=='groups':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                g=w and c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],p[4])).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                if not g:return send(self,200,{'deleted':True})
                mr=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if not mr or (g['creator_id']!=u['id'] and w['owner_id']!=u['id']):return send(self,403,{'error':'forbidden'})
                c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit()
                return send(self,200,{'deleted':True})
            if len(p)==5 and p[:2]==['api','workspaces'] and p[3]=='invitations':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
                mr=w and c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
                if not w:return send(self,404,{'error':'not found'})
                if not mr or mr['role'] not in ('owner','admin'):return send(self,403,{'error':'forbidden'})
                cur=c.execute('UPDATE invitations SET revoked=1 WHERE workspace_id=? AND code=?',(w['id'],p[4]))
                if not cur.rowcount:return send(self,404,{'error':'not found'})
                c.commit();return send(self,200,{'deleted':True})
            return send(self,404,{'error':'not found'})
    def do_OPTIONS(self): self.send_response(204);self.end_headers()
if __name__=='__main__':
    import sys
    port=int(sys.argv[1]); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
