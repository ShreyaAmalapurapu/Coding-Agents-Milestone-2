#!/usr/bin/env python3
import os, re, json, sqlite3, secrets, hashlib, datetime, time, base64, struct, socket, email, mimetypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB='/app/data/huddle.db'
os.makedirs('/app/data', exist_ok=True)

def db():
    c=sqlite3.connect(DB, timeout=5, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=5000')
    return c

def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE NOT NULL,password TEXT NOT NULL,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE NOT NULL,name TEXT NOT NULL,owner_id INTEGER NOT NULL,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT DEFAULT 'member',PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT NOT NULL,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,created_by INTEGER,expires_at REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,target_user_id INTEGER,revoked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,author_id INTEGER,body TEXT NOT NULL,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,first_id INTEGER PRIMARY KEY AUTOINCREMENT,UNIQUE(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
    CREATE TABLE IF NOT EXISTS reads(channel_id INTEGER,user_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS event_seq(channel_id INTEGER PRIMARY KEY,last_id INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS dm_pairs(user_low INTEGER,user_high INTEGER,channel_id INTEGER UNIQUE,PRIMARY KEY(user_low,user_high));
    CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
    CREATE TABLE IF NOT EXISTS online_users(user_id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
    CREATE TABLE IF NOT EXISTS file_links(file_id INTEGER PRIMARY KEY,message_id INTEGER);
    ''')
    c.commit(); c.close()

def user_obj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}

def workspace_obj(r):
    return {k:r[k] for k in ('id','slug','name','owner_id','join_mode')}

def channel_obj(r):
    d={k:r[k] for k in ('id','workspace_id','name','topic')}
    d['is_archived']=bool(r['is_archived']); d['is_private']=bool(r['is_private']); d['is_dm']=bool(r['is_dm'])
    return d

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')

def append_event(c,channel_id,kind,payload):
    c.execute('INSERT OR IGNORE INTO event_seq(channel_id,last_id) VALUES(?,0)',(channel_id,))
    c.execute('UPDATE event_seq SET last_id=last_id+1 WHERE channel_id=?',(channel_id,))
    eid=c.execute('SELECT last_id FROM event_seq WHERE channel_id=?',(channel_id,)).fetchone()['last_id']
    c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(channel_id,eid,kind,json.dumps(payload),now()))
    return eid
def ws_frame(obj):
    b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
    if n<126: h=bytes([0x81,n])
    elif n<65536: h=bytes([0x81,126])+struct.pack('!H',n)
    else: h=bytes([0x81,127])+struct.pack('!Q',n)
    return h+b
def ws_read(sock):
    h=sock.recv(2)
    if len(h)<2: return None
    ln=h[1]&127; masked=h[1]&128
    if ln==126: ln=struct.unpack('!H',sock.recv(2))[0]
    elif ln==127: ln=struct.unpack('!Q',sock.recv(8))[0]
    mask=sock.recv(4) if masked else b''; data=sock.recv(ln)
    if masked: data=bytes(x^mask[i%4] for i,x in enumerate(data))
    return data.decode()

def cursor_encode(n):
    import base64
    return base64.urlsafe_b64encode(json.dumps({'p':int(n)}).encode()).decode().rstrip('=')
def cursor_decode(x):
    import base64
    try:
        z=x+'='*((4-len(x)%4)%4); d=json.loads(base64.urlsafe_b64decode(z.encode()))
        if not isinstance(d,dict) or int(d['p'])<0: raise ValueError()
        return int(d['p'])
    except Exception: raise ValueError()
def reactions_of(c,message_id):
    rows=c.execute('SELECT emoji,user_id FROM reactions WHERE message_id=? ORDER BY first_id',(message_id,)).fetchall()
    out=[]; by={}
    for x in rows:
        if x['emoji'] not in by:
            by[x['emoji']]={'emoji':x['emoji'],'count':0,'user_ids':[]}; out.append(by[x['emoji']])
        by[x['emoji']]['count']+=1; by[x['emoji']]['user_ids'].append(x['user_id'])
    return out
def group_obj(c,g):
    return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id',(g['id'],)).fetchall()]}
def file_obj(r):
    return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def msg_obj(c,r):
    a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    rc=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(a),
      'body':r['body'],'parent_id':r['parent_id'],'reply_count':rc,'created_at':r['created_at'],
      'edited_at':r['edited_at'],'files':[file_obj(x) for x in c.execute('SELECT f.* FROM files f JOIN file_links l ON l.file_id=f.id WHERE l.message_id=?',(r['id'],)).fetchall()],'reactions':reactions_of(c,r['id']),'mentions':[x['user_id'] for x in c.execute('SELECT user_id FROM message_mentions WHERE message_id=? ORDER BY user_id',(r['id'],)).fetchall()]}
def read_state(c,channel_id,user_id):
    r=c.execute('SELECT last_read_event_id FROM reads WHERE channel_id=? AND user_id=?',(channel_id,user_id)).fetchone()
    last=int(r['last_read_event_id']) if r else 0
    rows=c.execute("SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(channel_id,last)).fetchall()
    unread=0; mentions=0
    for e in rows:
        if e['kind'] not in ('message.created','message.reply'):
            continue
        try:
            m=json.loads(e['payload']).get('message',{})
            if int(m.get('author_id',-1))==int(user_id):
                continue
            unread += 1
            if int(user_id) in [int(x) for x in m.get('mentions',[])]: mentions += 1
        except Exception:
            pass
    return {'channel_id':int(channel_id),'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
def page_params(qs):
    try:
        lim=int(qs.get('limit',['50'])[0])
        if lim<1 or lim>200: raise ValueError()
    except: raise ValueError()
    cur=qs.get('cursor',[None])[0]
    return lim, (cursor_decode(cur) if cur is not None else None)

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='Huddle/1.0'
    def log_message(self,*a): pass
    def send(self,status,obj=None,headers=None):
        self.send_response(status)
        payload=b'' if obj is None else json.dumps(obj,separators=(',',':')).encode()
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(payload)))
        self.send_header('Connection','close')
        if headers:
            for k,v in headers.items(): self.send_header(k,v)
        self.end_headers()
        if payload: self.wfile.write(payload)
    def body(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        raw=self.rfile.read(n)
        ct=self.headers.get('Content-Type','')
        if ct.startswith('multipart/form-data'):
            if n>10485760+1048576: return {'__too_large':True}
            try:
                msg=email.message_from_bytes(b'Content-Type: '+ct.encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+raw)
                for part in msg.walk():
                    field=part.get_param('name',header='content-disposition')
                    if field=='file' and part.get_filename() is not None:
                        data=part.get_payload(decode=True) or b''
                        return {'__upload':(part.get_filename(),part.get_content_type(),data)}
                return {'__no_file':True}
            except Exception: return {'__no_file':True}
        try: return json.loads(raw or b'{}')
        except: return None
    def auth(self,c):
        h=self.headers.get('Authorization','')
        if not h.startswith('Bearer '): return None
        r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone()
        return r
    def pathparts(self): return [x for x in urlparse(self.path).path.split('/') if x]
    def websocket(self):
        qs=parse_qs(urlparse(self.path).query); token=qs.get('token',[''])[0]
        c=db(); u=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(token,)).fetchone()
        if not u:
            self.send_response(401); self.end_headers(); return
        key=self.headers.get('Sec-WebSocket-Key')
        if not key: self.send_response(400); self.end_headers(); return
        accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
        sock=self.connection; sock.settimeout(0.15); channel=None; sent=0
        c.execute('INSERT OR IGNORE INTO online_users(user_id) VALUES(?)',(u['id'],)); c.commit()
        gtail=c.execute('SELECT COALESCE(MAX(id),0) n FROM global_events').fetchone()['n']; global_sent=gtail
        try:
            while True:
                try:
                    raw=ws_read(sock)
                    if raw:
                        q=json.loads(raw); typ=q.get('type')
                        if typ=='subscribe':
                            channel=int(q['channel_id']); row=c.execute('SELECT last_id FROM event_seq WHERE channel_id=?',(channel,)).fetchone(); sent=row['last_id'] if row else 0
                            sock.sendall(ws_frame({'type':'subscribed','channel_id':channel,'head_event_id':sent}))
                        elif typ=='resume':
                            channel=int(q['channel_id']); sent=int(q.get('since_event_id',0))
                            rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel,sent)).fetchall()
                            for r in rows:
                                z=json.loads(r['payload']); z.update({'type':r['kind'],'event_id':r['event_id'],'channel_id':r['channel_id']}); sock.sendall(ws_frame(z)); sent=r['event_id']
                            row=c.execute('SELECT last_id FROM event_seq WHERE channel_id=?',(channel,)).fetchone(); head=row['last_id'] if row else sent
                            sock.sendall(ws_frame({'type':'resumed','channel_id':channel,'head_event_id':head})); sent=head
                except socket.timeout: pass
                except (ConnectionError,OSError,ValueError): break
                grows=c.execute('SELECT * FROM global_events WHERE id>? ORDER BY id',(global_sent,)).fetchall()
                for ge in grows:
                    sock.sendall(ws_frame(json.loads(ge['payload']))); global_sent=ge['id']
                if channel is not None:
                    rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel,sent)).fetchall()
                    for r in rows:
                        z=json.loads(r['payload']); z.update({'type':r['kind'],'event_id':r['event_id'],'channel_id':r['channel_id']}); sock.sendall(ws_frame(z)); sent=r['event_id']
        finally:
            c.execute('DELETE FROM online_users WHERE user_id=?',(u['id'],)); c.commit(); c.close()
    def do_GET(self):
        if urlparse(self.path).path=='/api/ws':
            self.websocket(); return
        if self.path=='/' or urlparse(self.path).path=='/static/app.js' or urlparse(self.path).path=='/static/app.css':
            path='/app/static/index.html' if self.path=='/' else '/app'+urlparse(self.path).path
            try:
                data=open(path,'rb').read()
                ct='text/html' if path.endswith('.html') else ('application/javascript' if path.endswith('.js') else 'text/css')
                self.send_response(200); self.send_header('Content-Type',ct); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
            except FileNotFoundError: self.send_response(404); self.end_headers()
            return
        p=self.pathparts(); c=db()
        if p==['api','health']:
            self.send(200,{'status':'ok','node_id':int(os.getenv('NODE_ID','0'))}); c.close(); return
        u=self.auth(c)
        if len(p)==4 and p[:2]==['api','files'] and p[3]=='download':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            f=c.execute('SELECT * FROM files WHERE id=?',(p[2],)).fetchone()
            if not f: self.send(404,{'error':'not_found'}); c.close(); return
            data=open(f['path'],'rb').read()
            self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Length',str(len(data))); self.send_header('Content-Disposition','attachment; filename="'+f['filename'].replace('"','')+'"'); self.end_headers(); self.wfile.write(data); c.close(); return
        if len(p)==3 and p[:2]==['api','files']:
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            f=c.execute('SELECT * FROM files WHERE id=?',(p[2],)).fetchone()
            if not f: self.send(404,{'error':'not_found'}); c.close(); return
            self.send(200,{'file':file_obj(f)}); c.close(); return
        if len(p)==3 and p[:2]==['api','users']:
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            r=c.execute('SELECT * FROM users WHERE id=?',(p[2],)).fetchone()
            if not r: self.send(404,{'error':'not_found'}); c.close(); return
            self.send(200,{'user':user_obj(r)}); c.close(); return
        if p[:3]==['api','auth','me']:
            if not u: self.send(401,{'error':'unauthorized'})
            else: self.send(200,{'user':user_obj(u)})
            c.close(); return
        if p==['api','workspaces']:
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=? ORDER BY w.id DESC',(u['id'],)).fetchall()
            self.send(200,{'workspaces':[workspace_obj(x) for x in rows]}); c.close(); return
        if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='invitations':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not m or m['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            import time
            rows=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND (expires_at IS NULL OR expires_at>?) AND uses<max_uses',(w['id'],time.time())).fetchall()
            self.send(200,{'invitations':[dict(x) for x in rows]}); c.close(); return
        if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='members':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            m=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall()
            self.send(200,{'members':[dict(x) for x in m]}); c.close(); return
        if len(p)==4 and p[:2]==['api','channels'] and p[3]=='read':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            if not c.execute('SELECT 1 FROM channels WHERE id=?',(p[2],)).fetchone(): self.send(404,{'error':'not_found'}); c.close(); return
            self.send(200,{'read_state':read_state(c,int(p[2]),u['id'])}); c.close(); return
        if len(p)==4 and p[:2]==['api','channels'] and p[3]=='messages':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
            if not ch: self.send(404,{'error':'not_found'}); c.close(); return
            if ch['is_dm'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            try: lim,cur=page_params(parse_qs(urlparse(self.path).query))
            except: self.send(400,{'error':'invalid_pagination'}); c.close(); return
            where='channel_id=? AND parent_id IS NULL AND deleted=0'; args=[p[2]]
            if cur is not None: where+=' AND id<?'; args.append(cur)
            rows=c.execute('SELECT * FROM messages WHERE '+where+' ORDER BY id DESC LIMIT ?',args+[lim+1]).fetchall()
            more=len(rows)>lim; rows=rows[:lim]; nxt=cursor_encode(rows[-1]['id']) if more and rows else None
            self.send(200,{'messages':[msg_obj(c,x) for x in rows],'next_cursor':nxt}); c.close(); return
        if len(p)==4 and p[:2]==['api','messages'] and p[3]=='replies':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            parent=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(p[2],)).fetchone()
            if not parent: self.send(404,{'error':'not_found'}); c.close(); return
            try: lim,cur=page_params(parse_qs(urlparse(self.path).query))
            except: self.send(400,{'error':'invalid_pagination'}); c.close(); return
            args=[p[2]]; extra=''
            if cur is not None: extra=' AND id>?'; args.append(cur)
            rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0'+extra+' ORDER BY id ASC LIMIT ?',args+[lim+1]).fetchall()
            more=len(rows)>lim; rows=rows[:lim]; nxt=cursor_encode(rows[-1]['id']) if more and rows else None
            self.send(200,{'replies':[msg_obj(c,x) for x in rows],'next_cursor':nxt}); c.close(); return
        if p[:2]==['api','search']:
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            qs=parse_qs(urlparse(self.path).query); q=qs.get('q',[''])[0]
            try: lim,cur=page_params(qs)
            except: self.send(400,{'error':'invalid_pagination'}); c.close(); return
            args=[u['id'],u['id'],'%'+q+'%']; extra=''
            wsfilter=''
            if qs.get('workspace_id'):
                try: wsfilter=' AND ch.workspace_id=?'; args.append(int(qs['workspace_id'][0]))
                except: self.send(400,{'error':'invalid_workspace_id'}); c.close(); return
            if cur is not None: extra=' AND m.id<?'; args.append(cur)
            rows=c.execute('SELECT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN members wm ON wm.workspace_id=ch.workspace_id AND wm.user_id=? LEFT JOIN channel_members cm ON cm.channel_id=ch.id AND cm.user_id=? WHERE m.deleted=0 AND m.body LIKE ? AND (ch.is_private=0 OR cm.user_id IS NOT NULL)'+wsfilter+extra+' ORDER BY m.id DESC LIMIT ?', args+[lim+1]).fetchall()
            more=len(rows)>lim; rows=rows[:lim]; nxt=cursor_encode(rows[-1]['id']) if more and rows else None
            self.send(200,{'results':[msg_obj(c,x) for x in rows],'next_cursor':nxt}); c.close(); return
        if len(p)==4 and p[:2]==['api','channels'] and p[3]=='pins':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
            if not ch: self.send(404,{'error':'not_found'}); c.close(); return
            if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(p[2],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            try: lim,cur=page_params(parse_qs(urlparse(self.path).query))
            except: self.send(400,{'error':'invalid_pagination'}); c.close(); return
            args=[p[2]]; extra=''
            if cur is not None: extra=' AND p.message_id<?'; args.append(cur)
            rows=c.execute('SELECT p.*,m.*,p.pinned_at pa FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND m.deleted=0'+extra+' ORDER BY p.pinned_at DESC LIMIT ?',args+[lim+1]).fetchall()
            more=len(rows)>lim; rows=rows[:lim]; nxt=cursor_encode(rows[-1]['message_id']) if more and rows else None
            self.send(200,{'pins':[{'message':msg_obj(c,x),'pinned_by':x['pinned_by'],'pinned_at':x['pa']} for x in rows],'next_cursor':nxt}); c.close(); return
        if len(p)==4 and p[:2]==['api','channels'] and p[3]=='members':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
            if not ch: self.send(404,{'error':'not_found'}); c.close(); return
            rows=c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(p[2],)).fetchall()
            self.send(200,{'members':[user_obj(x) for x in rows]}); c.close(); return
        if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='groups':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            m=w and c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            if not m: self.send(403,{'error':'forbidden'}); c.close(); return
            self.send(200,{'groups':[group_obj(c,x) for x in c.execute('SELECT * FROM groups WHERE workspace_id=? ORDER BY id',(w['id'],)).fetchall()]}); c.close(); return
        if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='groups':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            h=str(b.get('handle',''))
            if not re.fullmatch(r'[a-z0-9_-]{2,32}',h) or not isinstance(b.get('name'),str): self.send(400,{'error':'invalid_input'}); c.close(); return
            try:
                cur=c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,b['name'],u['id']))
                gid=cur.lastrowid
                for uid in b.get('user_ids',[]): c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
                c.commit(); self.send(201,{'group':group_obj(c,c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone())})
            except sqlite3.IntegrityError: self.send(409,{'error':'duplicate'})
            c.close(); return
        if p==['api','files']:
            if isinstance(b,dict) and b.get('__too_large'): self.send(413,{'error':'too_large'}); c.close(); return
            if not isinstance(b,dict) or '__upload' not in b: self.send(400,{'error':'missing_file'}); c.close(); return
            filename,ctype,data=b['__upload']
            if len(data)>10485760: self.send(413,{'error':'too_large'}); c.close(); return
            fid=c.execute('SELECT COALESCE(MAX(id),0)+1 n FROM files').fetchone()['n']; path=f'/app/data/file-{fid}'
            with open(path,'wb') as fh: fh.write(data)
            cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],filename,ctype,len(data),now(),path)); c.commit()
            self.send(201,{'file':file_obj(c.execute('SELECT * FROM files WHERE id=?',(cur.lastrowid,)).fetchone())}); c.close(); return
        if p==['api','dms']:
            if not isinstance(b,dict) or b.get('user_id') is None: self.send(400,{'error':'invalid_input'}); c.close(); return
            other=c.execute('SELECT id FROM users WHERE id=?',(b.get('user_id'),)).fetchone()
            if not other: self.send(404,{'error':'not_found'}); c.close(); return
            if int(other['id'])==int(u['id']): self.send(400,{'error':'invalid_user'}); c.close(); return
            lo,hi=sorted((int(u['id']),int(other['id'])))
            row=c.execute('SELECT channel_id FROM dm_pairs WHERE user_low=? AND user_high=?',(lo,hi)).fetchone()
            if row: self.send(200,{'channel_id':row['channel_id']}); c.close(); return
            try:
                c.execute('BEGIN IMMEDIATE')
                cur=c.execute('INSERT INTO channels(workspace_id,name,is_dm) VALUES(0,?,1)',(f'dm-{lo}-{hi}',))
                cid=cur.lastrowid
                c.execute('INSERT INTO dm_pairs VALUES(?,?,?)',(lo,hi,cid))
                c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,lo))
                c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,hi))
                c.commit()
            except Exception as e:
                c.rollback()
                self.send(500,{'error':'dm_creation_failed'}); c.close(); return
            self.send(200,{'channel_id':cid}); c.close(); return
        if p==['api','workspaces']:
            slug=str(b.get('slug',''))
            if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not b.get('name'): self.send(400,{'error':'invalid_input'}); c.close(); return
            try:
                cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b['name'],u['id'])); wid=cur.lastrowid
                c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'))
                cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=cur.lastrowid
                c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
                self.send(201,{'workspace':workspace_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
            except sqlite3.IntegrityError: self.send(409,{'error':'duplicate'})
            c.close(); return
        if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='invitations':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not m or m['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            import time
            try: mx=int(b.get('max_uses',1))
            except: mx=0
            if mx<1: self.send(400,{'error':'invalid_max_uses'}); c.close(); return
            target=b.get('invited_user_id')
            if not target and b.get('invited_username'):
                q=c.execute('SELECT id FROM users WHERE username=?',(b['invited_username'],)).fetchone()
                if not q: self.send(400,{'error':'unknown_user'}); c.close(); return
                target=q['id']
            if target: mx=1
            exp=None
            if b.get('expires_in_seconds') is not None:
                try: exp=time.time()+int(b['expires_in_seconds'])
                except: self.send(400,{'error':'invalid_expiry'}); c.close(); return
            code=secrets.token_urlsafe(12)
            c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],exp,mx,0,target)); c.commit()
            self.send(201,{'invitation':{'code':code,'workspace_id':w['id'],'max_uses':mx,'uses':0,'invited_user_id':target,'expires_at':exp}})
            c.close(); return
        if len(p)==4 and p[:2]==['api','invitations'] and p[3]=='accept':
            import time
            inv=c.execute('SELECT * FROM invitations WHERE code=?',(p[2],)).fetchone()
            if not inv or inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['expires_at'] and inv['expires_at']<=time.time()):
                self.send(404,{'error':'not_found'}); c.close(); return
            if inv['target_user_id'] and inv['target_user_id']!=u['id']:
                self.send(403,{'error':'forbidden'}); c.close(); return
            c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'))
            c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(p[2],)); c.commit()
            self.send(200,{'invitation':{'code':p[2],'workspace_id':inv['workspace_id']}}); c.close(); return
        if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='channels':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not m: self.send(403,{'error':'forbidden'}); c.close(); return
            if m['role']=='guest' or (bool(b.get('is_private')) and m['role']=='member'):
                self.send(403,{'error':'forbidden'}); c.close(); return
            name=str(b.get('name',''))
            if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(str(b.get('topic','')))>250: self.send(400,{'error':'invalid_input'}); c.close(); return
            try:
                cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(b.get('is_private'))),b.get('topic','')))
                cid=cur.lastrowid; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
                self.send(201,{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
            except sqlite3.IntegrityError: self.send(409,{'error':'duplicate'})
            c.close(); return
        if len(p)==4 and p[:2]==['api','channels'] and p[3] in ('leave','archive','unarchive'):
            ch=c.execute('SELECT c.*,w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(p[2],)).fetchone()
            if not ch: self.send(404,{'error':'not_found'}); c.close(); return
            if p[3]=='leave':
                old=c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id']));
                if old.rowcount: append_event(c,ch['id'],'member.left',{'user_id':u['id']})
                c.commit()
                self.send(200,{'channel':channel_obj(ch)}); c.close(); return
            if ch['owner_id']!=u['id']: self.send(403,{'error':'forbidden'}); c.close(); return
            flag=1 if p[3]=='archive' else 0
            c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,ch['id'])); append_event(c,ch['id'],'channel.archived' if flag else 'channel.unarchived',{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.commit()
            self.send(200,{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.close(); return
        if len(p)==4 and p[:2]==['api','channels'] and p[3]=='read':
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            if not c.execute('SELECT 1 FROM channels WHERE id=?',(p[2],)).fetchone(): self.send(404,{'error':'not_found'}); c.close(); return
            try: x=max(0,int(b.get('last_read_event_id',0)))
            except: self.send(400,{'error':'invalid_event_id'}); c.close(); return
            old=c.execute('SELECT last_read_event_id FROM reads WHERE channel_id=? AND user_id=?',(p[2],u['id'])).fetchone()
            if not old: c.execute('INSERT INTO reads VALUES(?,?,?)',(p[2],u['id'],x))
            elif x>old['last_read_event_id']: c.execute('UPDATE reads SET last_read_event_id=? WHERE channel_id=? AND user_id=?',(x,p[2],u['id']))
            c.commit(); self.send(200,{'read_state':read_state(c,int(p[2]),u['id'])}); c.close(); return
        if len(p)==4 and p[:2]==['api','channels'] and p[3]=='messages':
            ch=c.execute('SELECT * FROM channels WHERE id=?',(p[2],)).fetchone()
            if not ch: self.send(404,{'error':'not_found'}); c.close(); return
            if ch['is_dm'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            body=b.get('body') if isinstance(b,dict) else None
            if not isinstance(body,str) or not body.strip(): self.send(400,{'error':'invalid_body'}); c.close(); return
            if body.startswith('/') and not body.startswith('//'):
                bits=body.split(' ',1); cmd=bits[0][1:]; text=bits[1].strip() if len(bits)>1 else ''
                if cmd=='me':
                    if not text: self.send(400,{'error':'invalid_command'}); c.close(); return
                elif cmd=='shrug':
                    body=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
                elif cmd in ('topic','invite','archive','unarchive'):
                    if cmd=='topic':
                        if len(text)>250: self.send(400,{'error':'invalid_topic'}); c.close(); return
                        own=c.execute('SELECT w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(ch['id'],)).fetchone()
                        member=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
                        if not member and (not own or own['owner_id']!=u['id']): self.send(403,{'error':'forbidden'}); c.close(); return
                        c.execute('UPDATE channels SET topic=? WHERE id=?',(text,ch['id']))
                        append_event(c,ch['id'],'channel.updated',{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.commit()
                        self.send(201,{'message':None,'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.close(); return
                    if cmd=='invite':
                        if not text.startswith('@') or len(text)<2: self.send(400,{'error':'invalid_command'}); c.close(); return
                        target=c.execute('SELECT * FROM users WHERE username=?',(text[1:].strip(),)).fetchone()
                        if not target: self.send(404,{'error':'not_found'}); c.close(); return
                        c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],target['id'])); c.commit()
                        self.send(201,{'message':None,'invited_user':user_obj(target)}); c.close(); return
                    owner=c.execute('SELECT owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(ch['id'],)).fetchone()
                    if not owner or owner['owner_id']!=u['id']: self.send(403,{'error':'forbidden'}); c.close(); return
                    flag=1 if cmd=='archive' else 0
                    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,ch['id']))
                    append_event(c,ch['id'],'channel.archived' if flag else 'channel.unarchived',{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.commit()
                    self.send(201,{'message':None,'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.close(); return
                else:
                    self.send(400,{'error':'unknown_command'}); c.close(); return
            if ch['is_archived']: self.send(423,{'error':'archived'}); c.close(); return
            parent=b.get('parent_id')
            if parent:
                pr=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(parent,)).fetchone()
                if not pr: self.send(404,{'error':'not_found'}); c.close(); return
                if pr['channel_id']!=ch['id']: self.send(400,{'error':'invalid_parent'}); c.close(); return
            fids=b.get('file_ids',[]) if isinstance(b,dict) else []
            if not isinstance(fids,list): self.send(400,{'error':'invalid_files'}); c.close(); return
            frows=[]
            for fid in fids:
                f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
                if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM file_links WHERE file_id=?',(fid,)).fetchone(): self.send(400,{'error':'invalid_file'}); c.close(); return
                frows.append(f)
            t=now(); cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,parent,t))
            mid=cur.lastrowid
            for f in frows: c.execute('INSERT INTO file_links VALUES(?,?)',(f['id'],mid))
            ids=set()
            if re.search(r'(?<![\w@])@channel(?![\w])',body):
                ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=? AND user_id!=?',(ch['id'],u['id'])).fetchall())
            for nm in re.findall(r'(?<![\w@])@([A-Za-z0-9_]+)(?![\w@.])',body):
                q=c.execute('SELECT u.id FROM users u JOIN members m ON m.user_id=u.id WHERE m.workspace_id=? AND u.username=? AND u.id!=?',(ch['workspace_id'],nm,u['id'])).fetchone()
                if q: ids.add(q['id'])
            for handle in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',body):
                g=c.execute('SELECT g.id FROM groups g WHERE g.workspace_id=? AND g.handle=?',(ch['workspace_id'],handle)).fetchone()
                if g:
                    ids.update(x['user_id'] for x in c.execute('SELECT gm.user_id FROM group_members gm JOIN channel_members cm ON cm.user_id=gm.user_id WHERE gm.group_id=? AND cm.channel_id=? AND gm.user_id!=?',(g['id'],ch['id'],u['id'])).fetchall())
            if re.search(r'(?<![A-Za-z0-9_])@here(?![A-Za-z0-9_])',body):
                ids.update(x['user_id'] for x in c.execute('SELECT cm.user_id FROM channel_members cm JOIN online_users o ON o.user_id=cm.user_id WHERE cm.channel_id=? AND cm.user_id!=?',(ch['id'],u['id'])).fetchall())
            for midx in ids: c.execute('INSERT OR IGNORE INTO message_mentions VALUES(?,?)',(mid,midx))
            append_event(c,ch['id'],'message.reply' if parent else 'message.created',{'message':msg_obj(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone())})
            c.commit()
            self.send(201,{'message':msg_obj(c,c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone())}); c.close(); return
        if len(p)==4 and p[:2]==['api','messages'] and p[3]=='reactions':
            m=c.execute('SELECT m.*,ch.is_private,ch.id cid FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(p[2],)).fetchone()
            if not m: self.send(404,{'error':'not_found'}); c.close(); return
            if m['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(m['cid'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            emoji=b.get('emoji') if isinstance(b,dict) else None
            if not isinstance(emoji,str) or not emoji: self.send(400,{'error':'invalid_emoji'}); c.close(); return
            cur=c.execute('INSERT OR IGNORE INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(m['id'],u['id'],emoji))
            if cur.rowcount:
                append_event(c,m['channel_id'],'reaction.added',{'message_id':m['id'],'emoji':emoji,'user_id':u['id']})
                c.commit()
            self.send(200,{'reactions':reactions_of(c,m['id'])}); c.close(); return
        if len(p)==4 and p[:2]==['api','messages'] and p[3]=='pin':
            m=c.execute('SELECT m.*,ch.is_archived FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(p[2],)).fetchone()
            if not m: self.send(404,{'error':'not_found'}); c.close(); return
            if m['is_archived']: self.send(423,{'error':'archived'}); c.close(); return
            t=now(); c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(m['id'],u['id'],t)); c.commit()
            z=c.execute('SELECT * FROM pins WHERE message_id=?',(m['id'],)).fetchone()
            self.send(200,{'pin':{'message_id':z['message_id'],'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']}}); c.close(); return
        if len(p)==4 and p[:2]==['api','channels'] and p[3]=='join':
            ch=c.execute('SELECT c.*,w.join_mode,w.id wid FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(p[2],)).fetchone()
            if not ch: self.send(404,{'error':'not_found'}); c.close(); return
            if ch['is_private']: self.send(403,{'error':'forbidden'}); c.close(); return
            mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(ch['wid'],u['id'])).fetchone()
            if mem and mem['role']=='guest': self.send(403,{'error':'forbidden'}); c.close(); return
            if ch['join_mode']=='invite_only' and not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['wid'],u['id'])).fetchone():
                self.send(403,{'error':'forbidden'}); c.close(); return
            if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['wid'],u['id'])).fetchone():
                c.execute('INSERT INTO members VALUES(?,?,?)',(ch['wid'],u['id'],'member'))
            ins=c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id']))
            if ins.rowcount: append_event(c,ch['id'],'member.joined',{'user_id':u['id']})
            c.commit()
            self.send(200,{'channel':channel_obj(ch)}); c.close(); return
        if len(p)==4 and p[:2]==['api','workspaces'] and p[3]=='transfer_ownership':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],b.get('new_owner_id'))).fetchone()
            if not actor or actor['role']!='owner': self.send(403,{'error':'forbidden'}); c.close(); return
            if not target or target['role']!='admin': self.send(400,{'error':'target_must_be_admin'}); c.close(); return
            c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
            c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],b['new_owner_id']))
            c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(b['new_owner_id'],w['id'])); c.commit()
            self.send(200,{'workspace':workspace_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}); c.close(); return
        self.send(404,{'error':'not_found'}); c.close()
    def do_DELETE(self):
        c=db(); u=self.auth(c); p=self.pathparts()
        if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
        if len(p)==5 and p[:2]==['api','workspaces'] and p[3]=='invitations':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            inv=w and c.execute('SELECT code FROM invitations WHERE code=? AND workspace_id=?',(p[4],w['id'])).fetchone()
            m=w and c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not inv: self.send(404,{'error':'not_found'}); c.close(); return
            if not m or m['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(p[4],)); c.commit(); self.send(200,{'deleted':True}); c.close(); return
        if len(p)==5 and p[:2]==['api','workspaces'] and p[3]=='groups':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            g=w and c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],p[4])).fetchone()
            if not w or not g: self.send(200,{'deleted':True}); c.close(); return
            role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not role or (g['creator_id']!=u['id'] and role['role']!='owner'): self.send(403,{'error':'forbidden'}); c.close(); return
            c.execute('DELETE FROM groups WHERE id=?',(g['id'],)); c.commit(); self.send(200,{'deleted':True}); c.close(); return
        if len(p)==3 and p[:2]==['api','messages']:
            m=c.execute('SELECT m.*,ch.workspace_id,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id LEFT JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(p[2],)).fetchone()
            if not m: self.send(404,{'error':'not_found'}); c.close(); return
            if m['deleted']:
                self.send(200,{'deleted':True}); c.close(); return
            if m['author_id']!=u['id'] and m['owner_id']!=u['id']:
                self.send(403,{'error':'forbidden'}); c.close(); return
            c.execute('UPDATE messages SET deleted=1 WHERE id=?',(m['id'],))
            c.execute('DELETE FROM reactions WHERE message_id=?',(m['id'],))
            append_event(c,m['channel_id'],'message.deleted',{'message_id':m['id']}); c.commit()
            self.send(200,{'deleted':True}); c.close(); return
        if len(p)==5 and p[:2]==['api','messages'] and p[3]=='reactions':
            from urllib.parse import unquote
            emoji=unquote(p[4])
            m=c.execute('SELECT m.*,ch.is_private,ch.id cid FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=? AND m.deleted=0',(p[2],)).fetchone()
            if not m: self.send(404,{'error':'not_found'}); c.close(); return
            if m['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(m['cid'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            cur=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(m['id'],u['id'],emoji))
            if cur.rowcount:
                append_event(c,m['channel_id'],'reaction.removed',{'message_id':m['id'],'emoji':emoji,'user_id':u['id']})
                c.commit()
            self.send(200,{'reactions':reactions_of(c,m['id'])}); c.close(); return
        if len(p)==4 and p[:2]==['api','messages'] and p[3]=='pin':
            c.execute('DELETE FROM pins WHERE message_id=?',(p[2],)); c.commit(); self.send(200,{'unpinned':True}); c.close(); return
        self.send(404,{'error':'not_found'}); c.close()
    def do_PATCH(self):
        c=db(); u=self.auth(c); p=self.pathparts(); b=self.body()
        if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
        if len(p)==5 and p[:2]==['api','workspaces'] and p[3]=='members':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],p[4])).fetchone()
            role=b.get('role') if isinstance(b,dict) else None
            if not target: self.send(404,{'error':'not_found'}); c.close(); return
            if role=='owner': self.send(400,{'error':'invalid_role'}); c.close(); return
            if role not in ('admin','member','guest'): self.send(400,{'error':'invalid_role'}); c.close(); return
            if not actor or actor['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            if int(p[4])==u['id'] or (actor['role']=='admin' and target['role'] in ('owner','admin')): self.send(403,{'error':'forbidden'}); c.close(); return
            c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],p[4])); c.commit()
            self.send(200,{'member':{'user_id':int(p[4]),'role':role}}); c.close(); return
        if len(p)==3 and p[:2]==['api','workspaces']:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not actor or actor['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'): self.send(400,{'error':'invalid_join_mode'}); c.close(); return
            fields=[]; args=[]
            for k in ('name','join_mode'):
                if k in b: fields.append(k+'=?'); args.append(b[k])
            if fields:
                args.append(w['id']); c.execute('UPDATE workspaces SET '+','.join(fields)+' WHERE id=?',args); c.commit()
            self.send(200,{'workspace':workspace_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}); c.close(); return
        if len(p)==5 and p[:2]==['api','workspaces'] and p[3]=='members':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],p[4])).fetchone()
            role=b.get('role') if isinstance(b,dict) else None
            if not target: self.send(404,{'error':'not_found'}); c.close(); return
            if role=='owner' or role not in ('admin','member','guest'): self.send(400,{'error':'invalid_role'}); c.close(); return
            if not actor or actor['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            if int(p[4])==u['id'] or (actor['role']=='admin' and target['role'] in ('owner','admin')): self.send(403,{'error':'forbidden'}); c.close(); return
            c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],p[4])); c.commit()
            self.send(200,{'member':{'user_id':int(p[4]),'role':role}}); c.close(); return
        if len(p)==3 and p[:2]==['api','workspaces']:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            if not w: self.send(404,{'error':'not_found'}); c.close(); return
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not actor or actor['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'): self.send(400,{'error':'invalid_join_mode'}); c.close(); return
            fields=[]; args=[]
            for k in ('name','join_mode'):
                if k in b: fields.append(k+'=?'); args.append(b[k])
            if fields:
                args.append(w['id']); c.execute('UPDATE workspaces SET '+','.join(fields)+' WHERE id=?',args); c.commit()
            self.send(200,{'workspace':workspace_obj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}); c.close(); return
        if len(p)==5 and p[:2]==['api','workspaces'] and p[3]=='groups':
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[2],)).fetchone()
            g=w and c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],p[4])).fetchone()
            if not w or not g: self.send(404,{'error':'not_found'}); c.close(); return
            role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not role or (g['creator_id']!=u['id'] and role['role']!='owner'): self.send(403,{'error':'forbidden'}); c.close(); return
            vals=[]
            if 'name' in b: vals.append(('name',b['name']))
            c.execute('UPDATE groups SET '+','.join(k+'=?' for k,v in vals)+' WHERE id=?',[v for k,v in vals]+[g['id']]) if vals else None
            if 'user_ids' in b:
                c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
                for uid in b['user_ids']: c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],uid))
            c.commit(); self.send(200,{'group':group_obj(c,c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone())}); c.close(); return
        if len(p)==3 and p[:2]==['api','messages']:
            m=c.execute('SELECT m.*,ch.workspace_id,w.owner_id FROM messages m JOIN channels ch ON ch.id=m.channel_id LEFT JOIN workspaces w ON w.id=ch.workspace_id WHERE m.id=?',(p[2],)).fetchone()
            if not m: self.send(404,{'error':'not_found'}); c.close(); return
            if m['deleted']: self.send(409,{'error':'deleted'}); c.close(); return
            if m['author_id']!=u['id']: self.send(403,{'error':'forbidden'}); c.close(); return
            body=b.get('body') if isinstance(b,dict) else None
            if not isinstance(body,str) or not body.strip(): self.send(400,{'error':'invalid_body'}); c.close(); return
            edited=now()
            c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,edited,m['id']))
            updated=c.execute('SELECT * FROM messages WHERE id=?',(m['id'],)).fetchone()
            append_event(c,m['channel_id'],'message.updated',{'message':msg_obj(c,updated)}); c.commit()
            self.send(200,{'message':msg_obj(c,updated)}); c.close(); return
        if len(p)==3 and p[:2]==['api','channels']:
            ch=c.execute('SELECT c.*,w.owner_id FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE c.id=?',(p[2],)).fetchone()
            if not ch: self.send(404,{'error':'not_found'}); c.close(); return
            if ch['is_archived']: self.send(423,{'error':'archived'}); c.close(); return
            mem=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
            if not mem and ch['owner_id']!=u['id']: self.send(403,{'error':'forbidden'}); c.close(); return
            topic=b.get('topic','')
            if len(str(topic))>250: self.send(400,{'error':'invalid_input'}); c.close(); return
            c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id'])); append_event(c,ch['id'],'channel.updated',{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.commit()
            self.send(200,{'channel':channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.close(); return
        if p==['api','users','me']:
            allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}
            if not isinstance(b,dict): self.send(400,{'error':'invalid_input'}); c.close(); return
            vals={k:b[k] for k in b if k in allowed}
            try:
                from zoneinfo import ZoneInfo
                for k,v in vals.items():
                    if not isinstance(v,str): raise ValueError()
                    lim={'display_name':80,'timezone':255,'avatar_url':2048,'status_text':100,'status_emoji':64}[k]
                    if k=='display_name' and not 1<=len(v)<=lim: raise ValueError()
                    if k!='display_name' and len(v)>lim: raise ValueError()
                    if k=='timezone': ZoneInfo(v)
            except Exception:
                self.send(400,{'error':'invalid_input'}); c.close(); return
            if vals:
                c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(u['id'],)); c.commit(); c.execute('INSERT INTO global_events(kind,payload,created_at) VALUES(?,?,?)',('user.updated',json.dumps({'type':'user.updated','user':user_obj(c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone())}),now())); c.commit()
            self.send(200,{'user':user_obj(c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone())}); c.close(); return
        self.send(404,{'error':'not_found'}); c.close()
init()
port=int(os.getenv('PORT','8000'))
ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
