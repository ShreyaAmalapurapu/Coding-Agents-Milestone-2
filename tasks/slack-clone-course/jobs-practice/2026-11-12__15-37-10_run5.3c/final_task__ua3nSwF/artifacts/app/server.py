import os, re, json, sqlite3, hashlib, secrets, datetime, base64, urllib.parse, cgi
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
DB='/app/data/huddle.db'
os.makedirs('/app/data',exist_ok=True)
def db():
    c=sqlite3.connect(DB,timeout=2); c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=2000'); return c
def init():
    c=db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires_at TEXT,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,invited_user_id INTEGER,revoked INTEGER DEFAULT 0,created_at TEXT);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]',created_at TEXT);
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,payload TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS read_states(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS dms(user_a INTEGER,user_b INTEGER,channel_id INTEGER UNIQUE,PRIMARY KEY(user_a,user_b));
    CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,deleted INTEGER DEFAULT 0,UNIQUE(workspace_id,handle));
    CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
    DROP TABLE IF EXISTS online_users;
    CREATE TABLE IF NOT EXISTS online_users(socket_id TEXT PRIMARY KEY,user_id INTEGER,seen_at TEXT);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,created_at TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,storage_path TEXT,created_at TEXT);
    CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
    ''');
    for col,typ in [('parent_id','INTEGER'),('edited_at','TEXT'),('deleted','INTEGER DEFAULT 0'),('mentions',"TEXT DEFAULT '[]'")]:
        try: c.execute('ALTER TABLE messages ADD COLUMN '+col+' '+typ)
        except sqlite3.OperationalError: pass
    c.commit(); c.close()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def user(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
def auth(h):
    x=h.get('Authorization','')
    if not x.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(x[7:],)).fetchone(); c.close()
    return r
def out_user(r): return user(r) if r else None
def append_event(channel_id,kind,payload):
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE')
        n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(channel_id,)).fetchone()['n']
        c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(channel_id,n,kind,json.dumps(payload),now())); c.commit()
        return n
    finally: c.close()
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a): pass
    def send(self,code,obj=None,headers=None):
        b=b'' if obj is None else json.dumps(obj).encode(); self.send_response(code)
        self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b)))
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
    def get(self,path): return parse_qs(urlparse(path).query)
    def channel(self,r):
        return {'id':r['id'],'workspace_id':r['workspace_id'],'name':r['name'],'is_private':bool(r['is_private']),'is_dm':bool(r['is_dm']),'topic':r['topic'] or '','is_archived':bool(r['is_archived'])}
    def workspace(self,r):
        return {k:r[k] for k in ('id','slug','name','owner_id','join_mode')}
    def msg(self,r,c):
        a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
        n=c.execute('SELECT COUNT(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
        try: mentions=json.loads(r['mentions'] or '[]')
        except: mentions=[]
        rx=[]
        for x in c.execute('SELECT emoji, user_id FROM reactions WHERE message_id=? ORDER BY rowid',(r['id'],)).fetchall():
            found=next((z for z in rx if z['emoji']==x['emoji']),None)
            if not found: found={'emoji':x['emoji'],'count':0,'user_ids':[]}; rx.append(found)
            found['count']+=1; found['user_ids'].append(x['user_id'])
        fs=[]
        for f in c.execute('SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(r['id'],)).fetchall():
            fs.append({k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')})
        return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(a) if a else None,'body':r['body'],'parent_id':r['parent_id'],'reply_count':n,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':rx,'mentions':mentions}
    def paging(self,q):
        try: lim=int(q.get('limit',['50'])[0])
        except: raise ValueError()
        if lim<1 or lim>200: raise ValueError()
        off=0; snap=None
        if q.get('cursor'):
            raw=base64.urlsafe_b64decode(urllib.parse.unquote(q['cursor'][0])+'='*((4-len(urllib.parse.unquote(q['cursor'][0]))%4)%4)).decode()
            a,b=raw.split(':'); snap=int(a); off=int(b)
        return lim,snap,off
    def nextcur(self,snap,off):
        return base64.urlsafe_b64encode(f'{snap}:{off}'.encode()).decode().rstrip('=')
    def ws_send(self,obj):
        data=json.dumps(obj,separators=(',',':')).encode(); n=len(data)
        head=bytes([129])+ (bytes([n]) if n<126 else bytes([126])+n.to_bytes(2,'big'))
        self.wfile.write(head+data); self.wfile.flush()
    def ws_recv(self):
        h=self.rfile.read(2)
        if len(h)<2: return None
        op=h[0]&15; n=h[1]&127
        if n==126: n=int.from_bytes(self.rfile.read(2),'big')
        elif n==127: n=int.from_bytes(self.rfile.read(8),'big')
        mask=self.rfile.read(4) if h[1]&128 else b''
        data=self.rfile.read(n)
        if mask: data=bytes(x^mask[i%4] for i,x in enumerate(data))
        if op==8:
            self.wfile.write(b'\x88\x00'); self.wfile.flush()
            self.close_connection=True
            return {'type':'__close'}
        if op==9:
            self.wfile.write(b'\x8a'+bytes([len(data)])+data); self.wfile.flush(); return {}
        try: return json.loads(data.decode())
        except: return {}
    def ws_loop(self):
        subs=set(); sent={}; c=db()
        import select
        try:
            while True:
                ready,_,_=select.select([self.connection],[],[],0.01)
                if ready:
                    q=self.ws_recv()
                    if q is None or q.get('type')=='__close': return
                    typ=q.get('type'); ch=int(q.get('channel_id',0) or 0)
                    if typ=='subscribe':
                        subs.add(ch)
                        head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']
                        sent[ch]=head
                        self.ws_send({'type':'subscribed','channel_id':ch,'head_event_id':head})
                    elif typ=='resume':
                        since=int(q.get('since_event_id',0)); subs.add(ch)
                        rows=c.execute('SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
                        for r in rows:
                            x=json.loads(r['payload']); x.update({'type':r['kind'],'event_id':r['event_id'],'channel_id':ch}); self.ws_send(x)
                        head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch,)).fetchone()['n']
                        sent[ch]=head
                        self.ws_send({'type':'resumed','channel_id':ch,'head_event_id':head})
                if not hasattr(self,'global_sent'): self.global_sent=0
                grows=c.execute('SELECT id,kind,payload FROM global_events WHERE id>? ORDER BY id',(self.global_sent,)).fetchall()
                for gr in grows:
                    self.ws_send({'type':gr['kind'],**json.loads(gr['payload'])}); self.global_sent=gr['id']
                for ch in tuple(subs):
                    after=sent.get(ch,0)
                    rows=c.execute('SELECT event_id,kind,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,after)).fetchall()
                    for r in rows:
                        x=json.loads(r['payload']); x.update({'type':r['kind'],'event_id':r['event_id'],'channel_id':ch})
                        self.ws_send(x); sent[ch]=r['event_id']
        finally:
            c.close()
    def mention_ids(self,c,chid,uid,body):
        w=c.execute('SELECT workspace_id FROM channels WHERE id=?',(chid,)).fetchone()
        if not w: return []
        ids=set()
        if re.search(r'(?<![\\w@])@channel(?![\\w])',body):
            ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(chid,)))
        members={x['user_id'] for x in c.execute('SELECT user_id FROM members WHERE workspace_id=?',(w['workspace_id'],))}
        if re.search(r'(?<![A-Za-z0-9_@])@here(?![A-Za-z0-9_@])',body):
            ids.update(x['user_id'] for x in c.execute('SELECT DISTINCT cm.user_id FROM channel_members cm JOIN online_users ou ON ou.user_id=cm.user_id WHERE cm.channel_id=?',(chid,)) if x['user_id']!=uid)
        for name in re.findall(r'(?<![A-Za-z0-9_@])@([A-Za-z0-9_-]+)(?![A-Za-z0-9_@.])',body):
            x=c.execute('SELECT id FROM users WHERE username=?',(name,)).fetchone()
            if x and x['id'] in members: ids.add(x['id'])
            g=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=? AND deleted=0',(w['workspace_id'],name)).fetchone()
            if g:
                ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)) if x['user_id'] in members and x['user_id']!=uid and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(chid,x['user_id'])).fetchone())
        ids.discard(uid)
        return sorted(ids)
    def read_state(self,c,chid,uid):
        x=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(uid,chid)).fetchone()
        last=x['last_read_event_id'] if x else 0
        rows=c.execute("SELECT e.payload FROM events e WHERE e.channel_id=? AND e.event_id>?",(chid,last)).fetchall()
        unread=0; mentions=0
        for row in rows:
            try:
                m=json.loads(row['payload']).get('message',{})
                if m.get('author_id')!=uid:
                    unread+=1
                    if uid in m.get('mentions',[]): mentions+=1
            except: pass
        return {'channel_id':chid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}
    def do_GET(self):
        p=urlparse(self.path).path; c=db()
        if p=='/' or p=='/static/index.html':
            data=open('/app/static/index.html','rb').read(); self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); c.close(); return
        if p in ('/static/app.js','/static/app.css'):
            data=open('/app/static/'+p.rsplit('/',1)[1],'rb').read(); self.send_response(200); self.send_header('Content-Type','text/javascript' if p.endswith('.js') else 'text/css'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); c.close(); return
        if p=='/api/health':
            self.send(200,{'status':'ok','node_id':int(os.environ.get('NODE_ID','0'))}); c.close(); return
        if p=='/api/ws':
            token=parse_qs(urlparse(self.path).query).get('token',[''])[0]
            if not auth({'Authorization':'Bearer '+token}):
                self.send_error(401); c.close(); return
            key=self.headers.get('Sec-WebSocket-Key')
            if not key: self.send_error(400); c.close(); return
            accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101,'Switching Protocols'); self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers(); self.close_connection=False
            wu=auth({'Authorization':'Bearer '+token}); socket_id=secrets.token_urlsafe(12); wc=db(); wc.execute('INSERT INTO online_users(socket_id,user_id,seen_at) VALUES(?,?,?)',(socket_id,wu['id'],now())); wc.commit(); wc.close()
            try: self.ws_loop()
            except Exception: pass
            wc=db(); wc.execute('DELETE FROM online_users WHERE socket_id=?',(socket_id,)); wc.commit(); wc.close()
            c.close(); return
        if p.startswith('/api/auth/me'):
            u=auth(self.headers)
            self.send(200,{'user':user(u)} if u else {'error':'unauthorized'}) if u else self.send(401,{'error':'unauthorized'})
            c.close(); return
        u=auth(self.headers)
        if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
        m=re.fullmatch(r'/api/users/(\d+)',p)
        if m:
            r=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone()
            if not r: self.send(404,{'error':'not found'}); c.close(); return
            self.send(200,{'user':user(r)}); c.close(); return
        m=re.fullmatch(r'/api/files/(\d+)/download',p)
        if m:
            f=c.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone()
            if not f or not os.path.exists(f['storage_path']): self.send(404,{'error':'not found'}); c.close(); return
            data=open(f['storage_path'],'rb').read()
            self.send_response(200); self.send_header('Content-Type',f['content_type']); self.send_header('Content-Length',str(len(data))); self.send_header('Content-Disposition','attachment; filename="'+f['filename'].replace('"','')+'"'); self.end_headers(); self.wfile.write(data); c.close(); return
        m=re.fullmatch(r'/api/files/(\d+)',p)
        if m:
            f=c.execute('SELECT * FROM files WHERE id=?',(m.group(1),)).fetchone()
            if not f: self.send(404,{'error':'not found'}); c.close(); return
            self.send(200,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}}); c.close(); return
        if p=='/api/workspaces':
            rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
            self.send(200,{'workspaces':[self.workspace(x) for x in rows]}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/members',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w: self.send(404,{'error':'not found'}); c.close(); return
            rows=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall()
            self.send(200,{'members':[dict(x) for x in rows]}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            mem=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w: self.send(404,{'error':'not found'}); c.close(); return
            if not mem: self.send(403,{'error':'forbidden'}); c.close(); return
            gs=[]
            for g in c.execute('SELECT * FROM groups WHERE workspace_id=? AND deleted=0',(w['id'],)).fetchall():
                ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],))]
                gs.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids})
            self.send(200,{'groups':gs}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w: self.send(404,{'error':'not found'}); c.close(); return
            if not actor or actor['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            rows=c.execute('SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND uses<max_uses',(w['id'],)).fetchall()
            self.send(200,{'invitations':[dict(x) for x in rows]}); c.close(); return
        m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
        if m:
            r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(m.group(1),)).fetchone()
            if not r: self.send(404,{'error':'not found'}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
            if ch['is_archived']: self.send(423,{'error':'archived'}); c.close(); return
            c.execute('INSERT OR IGNORE INTO pins(message_id,pinned_by,pinned_at) VALUES(?,?,?)',(r['id'],u['id'],now())); c.commit()
            z=c.execute('SELECT * FROM pins WHERE message_id=?',(r['id'],)).fetchone()
            self.send(200,{'pin':{'message_id':z['message_id'],'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']}}); c.close(); return
        m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
        if m:
            try: lim,snap,off=self.paging(parse_qs(urlparse(self.path).query))
            except: self.send(400,{'error':'invalid pagination'}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch: self.send(404,{'error':'not found'}); c.close(); return
            if ch['is_dm'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            if snap is None: snap=c.execute('SELECT COALESCE(MAX(id),0) n FROM messages WHERE channel_id=?',(ch['id'],)).fetchone()['n']
            rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<=? ORDER BY id DESC LIMIT ? OFFSET ?',(ch['id'],snap,lim,off)).fetchall()
            more=c.execute('SELECT 1 FROM messages WHERE channel_id=? AND parent_id IS NULL AND deleted=0 AND id<=? LIMIT 1 OFFSET ?',(ch['id'],snap,off+lim)).fetchone()
            self.send(200,{'messages':[self.msg(x,c) for x in rows],'next_cursor':self.nextcur(snap,off+lim) if more else None}); c.close(); return
        m=re.fullmatch(r'/api/messages/(\d+)/replies',p)
        if m:
            try: lim,snap,off=self.paging(parse_qs(urlparse(self.path).query))
            except: self.send(400,{'error':'invalid pagination'}); c.close(); return
            par=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(m.group(1),)).fetchone()
            if not par: self.send(404,{'error':'not found'}); c.close(); return
            if snap is None: snap=c.execute('SELECT COALESCE(MAX(id),0) n FROM messages WHERE parent_id=?',(par['id'],)).fetchone()['n']
            rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 AND id<=? ORDER BY id ASC LIMIT ? OFFSET ?',(par['id'],snap,lim,off)).fetchall()
            more=c.execute('SELECT 1 FROM messages WHERE parent_id=? AND deleted=0 AND id<=? LIMIT 1 OFFSET ?',(par['id'],snap,off+lim)).fetchone()
            self.send(200,{'replies':[self.msg(x,c) for x in rows],'next_cursor':self.nextcur(snap,off+lim) if more else None}); c.close(); return
        if p=='/api/search':
            q=parse_qs(urlparse(self.path).query)
            try: lim,snap,off=self.paging(q)
            except: self.send(400,{'error':'invalid pagination'}); c.close(); return
            if snap is None: snap=c.execute('SELECT COALESCE(MAX(id),0) n FROM messages').fetchone()['n']
            text=q.get('q',[''])[0]; wid=q.get('workspace_id',[None])[0]
            extra=''; params=[u['id'],u['id'],snap,'%'+text+'%']
            if wid: extra=' AND ch.workspace_id=?'; params.append(wid)
            params += [lim,off]
            rows=c.execute('SELECT DISTINCT m.* FROM messages m JOIN channels ch ON ch.id=m.channel_id JOIN members wm ON wm.workspace_id=ch.workspace_id AND wm.user_id=? LEFT JOIN channel_members cm ON cm.channel_id=ch.id AND cm.user_id=? WHERE m.deleted=0 AND m.id<=? AND m.body LIKE ? AND (ch.is_private=0 OR cm.user_id IS NOT NULL)'+extra+' ORDER BY m.id DESC LIMIT ? OFFSET ?',params).fetchall()
            self.send(200,{'results':[self.msg(x,c) for x in rows],'next_cursor':self.nextcur(snap,off+lim) if len(rows)==lim else None}); c.close(); return
        m=re.fullmatch(r'/api/channels/(\d+)/pins',p)
        if m:
            try: lim,snap,off=self.paging(parse_qs(urlparse(self.path).query))
            except: self.send(400,{'error':'invalid pagination'}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch: self.send(404,{'error':'not found'}); c.close(); return
            if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            rows=c.execute('SELECT p.pinned_by pb,p.pinned_at pa,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC LIMIT ? OFFSET ?',(ch['id'],lim,off)).fetchall()
            more=len(rows)==lim and c.execute('SELECT 1 FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? LIMIT 1 OFFSET ?',(ch['id'],off+lim)).fetchone()
            self.send(200,{'pins':[{'message':self.msg(x,c),'pinned_by':x['pb'],'pinned_at':x['pa']} for x in rows],'next_cursor':self.nextcur(999999999,off+lim) if more else None}); c.close(); return
        m=re.fullmatch(r'/api/channels/(\d+)/read',p)
        if m:
            ch=c.execute('SELECT id FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch: self.send(404,{'error':'not found'}); c.close(); return
            self.send(200,{'read_state':self.read_state(c,ch['id'],u['id'])}); c.close(); return
        m=re.fullmatch(r'/api/channels/(\d+)/members',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch: self.send(404,{'error':'not found'}); c.close(); return
            rows=c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],)).fetchall()
            self.send(200,{'members':[user(x) for x in rows]}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w: self.send(404,{'error':'not found'}); c.close(); return
            include=parse_qs(urlparse(self.path).query).get('include_archived',['0'])[0] in ('1','true')
            rows=c.execute('SELECT ch.* FROM channels ch LEFT JOIN channel_members cm ON cm.channel_id=ch.id AND cm.user_id=? WHERE ch.workspace_id=? AND (ch.is_private=0 OR cm.user_id IS NOT NULL) AND (ch.is_archived=0 OR ?)',(u['id'],w['id'],include)).fetchall()
            states=[self.read_state(c,x['id'],u['id']) for x in rows]; rs={'last_read_event_id':max([z['last_read_event_id'] for z in states] or [0]),'unread_count':sum(z['unread_count'] for z in states),'mention_count':sum(z['mention_count'] for z in states)}; self.send(200,{'workspace':self.workspace(w),'channels':[self.channel(x) for x in rows],'read_state':rs}); c.close(); return
        self.send(404,{'error':'not found'}); c.close()
    def do_PATCH(self):
        p=urlparse(self.path).path; c=db(); u=auth(self.headers)
        if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
        b=self.body()
        if p=='/api/users/me':
            limits={'display_name':(1,80),'status_text':(0,100),'status_emoji':(0,64),'avatar_url':(0,2048)}
            vals={}
            for k,v in b.items():
                if k not in limits and k!='timezone': self.send(400,{'error':'invalid profile'}); c.close(); return
                if not isinstance(v,str): self.send(400,{'error':'invalid profile'}); c.close(); return
                if k in limits and not limits[k][0] <= len(v) <= limits[k][1]: self.send(400,{'error':'invalid profile'}); c.close(); return
                if k=='timezone':
                    try: ZoneInfo(v)
                    except: self.send(400,{'error':'invalid timezone'}); c.close(); return
                vals[k]=v
            if vals:
                c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',list(vals.values())+[u['id']]); c.commit()
            updated=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
            c.execute('INSERT INTO global_events(kind,payload,created_at) VALUES(?,?,?)',('user.updated',json.dumps({'user':user(updated)}),now())); c.commit()
            self.send(200,{'user':user(updated)}); c.close(); return
        m=re.fullmatch(r'/api/messages/(\d+)',p)
        if m:
            r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r: self.send(404,{'error':'not found'}); c.close(); return
            if r['deleted']: self.send(409,{'error':'deleted'}); c.close(); return
            if r['author_id']!=u['id']: self.send(403,{'error':'forbidden'}); c.close(); return
            body=b.get('body')
            if not isinstance(body,str) or not body.strip(): self.send(400,{'error':'empty body'}); c.close(); return
            c.execute('BEGIN IMMEDIATE')
            edited=now(); c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(body,edited,r['id']))
            rr=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone()
            n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']
            c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(r['channel_id'],n,'message.updated',json.dumps({'message':self.msg(rr,c)}),now())); c.commit()
            self.send(200,{'message':self.msg(rr,c)}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=? AND deleted=0',(w['id'],m.group(2))).fetchone() if w else None
            if not w or not g: self.send(404,{'error':'not found'}); c.close(); return
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not actor or (actor['role']!='owner' and g['creator_id']!=u['id']): self.send(403,{'error':'forbidden'}); c.close(); return
            if 'name' in b and (not isinstance(b['name'],str) or not b['name']): self.send(400,{'error':'invalid'}); c.close(); return
            if 'name' in b: c.execute('UPDATE groups SET name=? WHERE id=?',(b['name'],g['id']))
            if 'user_ids' in b:
                if not isinstance(b['user_ids'],list): self.send(400,{'error':'invalid'}); c.close(); return
                c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
                for x in b['user_ids']:
                    if c.execute('SELECT 1 FROM users WHERE id=?',(x,)).fetchone(): c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],x))
            c.commit(); g=c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone()
            self.send(200,{'group':{'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],))]}}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w: self.send(404,{'error':'not found'}); c.close(); return
            actor=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not actor or actor['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            if 'join_mode' in b and b['join_mode'] not in ('open','invite_only'): self.send(400,{'error':'invalid join_mode'}); c.close(); return
            if 'name' in b and (not isinstance(b['name'],str) or not b['name']): self.send(400,{'error':'invalid name'}); c.close(); return
            sets=[]; vals=[]
            for key in ('name','join_mode'):
                if key in b: sets.append(key+'=?'); vals.append(b[key])
            if sets:
                vals.append(w['id']); c.execute('UPDATE workspaces SET '+','.join(sets)+' WHERE id=?',vals); c.commit()
            self.send(200,{'workspace':self.workspace(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}); c.close(); return
        m=re.fullmatch(r'/api/channels/(\d+)',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch: self.send(404,{'error':'not found'}); c.close(); return
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            if ch['is_archived']: self.send(423,{'error':'archived'}); c.close(); return
            mem=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
            owner=(w and w['owner_id']==u['id'])
            if not mem and not owner: self.send(403,{'error':'forbidden'}); c.close(); return
            topic=b.get('topic')
            if not isinstance(topic,str) or len(topic)>250: self.send(400,{'error':'invalid topic'}); c.close(); return
            c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id'])); c.commit()
            self.send(200,{'channel':self.channel(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/members/(\d+)',p)
        if not m: self.send(404,{'error':'not found'}); c.close(); return
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
        if not w: self.send(404,{'error':'not found'}); c.close(); return
        actor=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
        target=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],m.group(2))).fetchone()
        role=b.get('role')
        if not target: self.send(404,{'error':'not found'}); c.close(); return
        if role not in ('owner','admin','member','guest'): self.send(400,{'error':'invalid role'}); c.close(); return
        if role=='owner': self.send(400,{'error':'invalid role'}); c.close(); return
        if not actor or actor['role'] not in ('owner','admin') or target['user_id']==actor['user_id'] or target['role']=='owner' or (actor['role']=='admin' and target['role']=='admin'):
            self.send(403,{'error':'forbidden'}); c.close(); return
        c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],target['user_id'])); c.commit()
        self.send(200,{'member':{'user_id':target['user_id'],'role':role}}); c.close()
    def do_POST(self):
        p=urlparse(self.path).path; c=db()
        if p=='/api/files':
            u=auth(self.headers)
            if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
            ctype=self.headers.get('Content-Type','')
            if 'multipart/form-data' not in ctype: self.send(400,{'error':'multipart required'}); c.close(); return
            try:
                length=int(self.headers.get('Content-Length','0'))
                raw=self.rfile.read(length)
                boundary=ctype.split('boundary=',1)[1].strip().strip('"').encode()
                chunks=raw.split(b'--'+boundary)
                part=None
                for chunk in chunks:
                    if b'Content-Disposition:' not in chunk: continue
                    head,sep,data_part=chunk.partition(b'\r\n\r\n')
                    if not sep or b'name="file"' not in head: continue
                    fn=re.search(br'filename="([^"]*)"',head)
                    if not fn: continue
                    ct=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
                    part=(fn.group(1).decode('utf-8','replace'),(ct.group(1).decode().strip() if ct else 'application/octet-stream'),data_part.rstrip(b'\r\n'))
                    break
                if part is None: self.send(400,{'error':'missing file'}); c.close(); return
                filename,part_type,data=part
            except Exception:
                self.send(400,{'error':'invalid upload'}); c.close(); return
            if len(data)>10485760: self.send(413,{'error':'file too large'}); c.close(); return
            os.makedirs('/app/data/files',exist_ok=True)
            cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,storage_path,created_at) VALUES(?,?,?,?,?,?)',(u['id'],filename,part_type,len(data),'',now())); fid=cur.lastrowid
            path='/app/data/files/'+str(fid); open(path,'wb').write(data); c.execute('UPDATE files SET storage_path=? WHERE id=?',(path,fid)); c.commit()
            f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
            self.send(201,{'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}}); c.close(); return
        try: b=self.body()
        except: self.send(400,{'error':'bad json'}); c.close(); return
        if p=='/api/auth/register':
            un=b.get('username'); pw=b.get('password')
            if not isinstance(un,str) or not re.fullmatch(r'[A-Za-z0-9_]+',un) or not isinstance(pw,str) or len(pw)<8:
                self.send(400,{'error':'invalid'}); c.close(); return
            try:
                cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),b.get('display_name') or un)); uid=cur.lastrowid
                tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit()
                r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); self.send(201,{'user':user(r),'token':tok})
            except sqlite3.IntegrityError: self.send(409,{'error':'duplicate'})
            c.close(); return
        if p=='/api/auth/login':
            r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(str(b.get('password','')).encode()).hexdigest())).fetchone()
            if not r: self.send(401,{'error':'invalid credentials'}); c.close(); return
            tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id'])); c.commit(); self.send(200,{'user':user(r),'token':tok}); c.close(); return
        u=auth(self.headers)
        if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
        if p=='/api/dms':
            other=b.get('user_id')
            if other is None: self.send(400,{'error':'user_id required'}); c.close(); return
            target=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
            if not target: self.send(404,{'error':'not found'}); c.close(); return
            a,z=sorted((u['id'],int(other)))
            row=c.execute('SELECT channel_id FROM dms WHERE user_a=? AND user_b=?',(a,z)).fetchone()
            if row: self.send(200,{'channel_id':row['channel_id']}); c.close(); return
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT channel_id FROM dms WHERE user_a=? AND user_b=?',(a,z)).fetchone()
            if row: cid=row['channel_id']
            else:
                cid=c.execute('INSERT INTO channels(name,is_dm,is_private) VALUES(?,?,?)',(f'dm-{a}-{z}',1,1)).lastrowid
                c.execute('INSERT INTO dms VALUES(?,?,?)',(a,z,cid))
                c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,a)); c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,z))
            c.commit(); self.send(200,{'channel_id':cid}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/groups',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            mem=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w: self.send(404,{'error':'not found'}); c.close(); return
            if not mem: self.send(403,{'error':'forbidden'}); c.close(); return
            h=b.get('handle'); name=b.get('name'); ids=b.get('user_ids',[])
            if not isinstance(h,str) or not re.fullmatch(r'[a-z0-9_-]{2,32}',h) or not isinstance(name,str) or not isinstance(ids,list): self.send(400,{'error':'invalid'}); c.close(); return
            if c.execute('SELECT 1 FROM groups WHERE workspace_id=? AND handle=? AND deleted=0',(w['id'],h)).fetchone(): self.send(409,{'error':'duplicate'}); c.close(); return
            cur=c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,name,u['id'])); gid=cur.lastrowid
            for x in ids:
                if c.execute('SELECT 1 FROM users WHERE id=?',(x,)).fetchone(): c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,x))
            c.commit(); self.send(201,{'group':{'id':gid,'handle':h,'name':name,'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(gid,))]}}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w: self.send(404,{'error':'not found'}); c.close(); return
            if not actor or actor['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
            try: mx=int(b.get('max_uses',1))
            except: mx=0
            if mx<1: self.send(400,{'error':'invalid max_uses'}); c.close(); return
            target=b.get('invited_user_id')
            if target is None and b.get('invited_username'):
                rr=c.execute('SELECT id FROM users WHERE username=?',(b['invited_username'],)).fetchone()
                if rr: target=rr['id']
            exp=None
            if b.get('expires_in_seconds') is not None:
                try: exp=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=int(b['expires_in_seconds']))).isoformat().replace('+00:00','Z')
                except: self.send(400,{'error':'invalid expiry'}); c.close(); return
            if target is not None: mx=1
            code=secrets.token_urlsafe(16)
            c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?,?)',(code,w['id'],u['id'],exp,mx,0,target,0,now())); c.commit()
            self.send(201,{'invitation':dict(c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone())}); c.close(); return
        m=re.fullmatch(r'/api/invitations/([^/]+)/accept',p)
        if m:
            inv=c.execute('SELECT * FROM invitations WHERE code=?',(m.group(1),)).fetchone()
            if not inv or inv['revoked'] or inv['uses']>=inv['max_uses'] or (inv['expires_at'] and inv['expires_at']<=now()): self.send(404,{'error':'not found'}); c.close(); return
            if inv['invited_user_id'] is not None and inv['invited_user_id']!=u['id']: self.send(403,{'error':'forbidden'}); c.close(); return
            c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member')); c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(inv['code'],)); c.commit()
            self.send(200,{'workspace_id':inv['workspace_id']}); c.close(); return
        if p=='/api/workspaces':
            slug=b.get('slug'); name=b.get('name')
            if not isinstance(slug,str) or not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not name: self.send(400,{'error':'invalid'}); c.close(); return
            try:
                cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,name,u['id'])); wid=cur.lastrowid
                c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=cur.lastrowid
                c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit()
                w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                self.send(201,{'workspace':self.workspace(w),'general_channel':self.channel(ch)})
            except sqlite3.IntegrityError: self.send(409,{'error':'duplicate'})
            c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/transfer_ownership',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w: self.send(404,{'error':'not found'}); c.close(); return
            actor=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            target=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],b.get('new_owner_id'))).fetchone()
            if not actor or actor['role']!='owner': self.send(403,{'error':'forbidden'}); c.close(); return
            if not target or target['role']!='admin': self.send(400,{'error':'target must be admin'}); c.close(); return
            c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']))
            c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],target['user_id']))
            c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(target['user_id'],w['id'])); c.commit()
            self.send(200,{'workspace':self.workspace(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())}); c.close(); return
        m=re.fullmatch(r'/api/channels/(\d+)/read',p)
        if m:
            ch=c.execute('SELECT id FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch: self.send(404,{'error':'not found'}); c.close(); return
            oldr=c.execute('SELECT last_read_event_id FROM read_states WHERE user_id=? AND channel_id=?',(u['id'],ch['id'])).fetchone()
            val=max(int(b.get('last_read_event_id',0)),oldr['last_read_event_id'] if oldr else 0)
            c.execute('INSERT INTO read_states(user_id,channel_id,last_read_event_id) VALUES(?,?,?) ON CONFLICT(user_id,channel_id) DO UPDATE SET last_read_event_id=excluded.last_read_event_id',(u['id'],ch['id'],val)); c.commit()
            self.send(200,{'read_state':self.read_state(c,ch['id'],u['id'])}); c.close(); return
        m=re.fullmatch(r'/api/messages/(\d+)/reactions',p)
        if m:
            r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(m.group(1),)).fetchone()
            if not r: self.send(404,{'error':'not found'}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
            if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            emoji=b.get('emoji')
            if not isinstance(emoji,str) or not emoji: self.send(400,{'error':'invalid emoji'}); c.close(); return
            cur=c.execute('INSERT OR IGNORE INTO reactions VALUES(?,?,?,?)',(r['id'],u['id'],emoji,now()))
            c.commit()
            if cur.rowcount:
                append_event(ch['id'],'reaction.added',{'message_id':r['id'],'emoji':emoji,'user_id':u['id']})
            rr=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone()
            self.send(200,{'reactions':self.msg(rr,c)['reactions']}); c.close(); return
        m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
        if m:
            r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(m.group(1),)).fetchone()
            if not r: self.send(404,{'error':'not found'}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
            if ch['is_archived']: self.send(423,{'error':'archived'}); c.close(); return
            c.execute('INSERT OR IGNORE INTO pins(message_id,pinned_by,pinned_at) VALUES(?,?,?)',(r['id'],u['id'],now())); c.commit()
            z=c.execute('SELECT * FROM pins WHERE message_id=?',(r['id'],)).fetchone()
            self.send(200,{'pin':{'message_id':z['message_id'],'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']}}); c.close(); return
        m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch: self.send(404,{'error':'not found'}); c.close(); return
            if ch['is_dm'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            if ch['is_archived'] and not (isinstance(b.get('body'),str) and b.get('body').startswith('/unarchive')): self.send(423,{'error':'archived'}); c.close(); return
            body=b.get('body')
            if isinstance(body,str) and body.startswith('/') and not body.startswith('//'):
                parts=body.split(' ',1); cmd=parts[0]; text=parts[1].strip() if len(parts)>1 else ''
                if cmd=='/me' and not text: self.send(400,{'error':'text required'}); c.close(); return
                if cmd=='/shrug': body=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
                elif cmd=='/me': pass
                elif cmd in ('/topic','/invite','/archive','/unarchive'):
                    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
                    if cmd=='/topic':
                        if not text or len(text)>250: self.send(400,{'error':'invalid topic'}); c.close(); return
                        if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone() and w['owner_id']!=u['id']: self.send(403,{'error':'forbidden'}); c.close(); return
                        c.execute('BEGIN IMMEDIATE')
                        c.execute('UPDATE channels SET topic=? WHERE id=?',(text,ch['id']))
                        n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']
                        c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ch['id'],n,'channel.updated',json.dumps({'channel':self.channel(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}),now())); c.commit()
                        self.send(201,{'message':None,'channel':self.channel(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.close(); return
                    if cmd=='/invite':
                        name=text[1:] if text.startswith('@') else text; x=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
                        if not x: self.send(404,{'error':'not found'}); c.close(); return
                        c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],x['id'])); c.commit(); self.send(201,{'message':None,'invited_user':user(x)}); c.close(); return
                    if w['owner_id']!=u['id']: self.send(403,{'error':'forbidden'}); c.close(); return
                    flag=1 if cmd=='/archive' else 0
                    c.execute('BEGIN IMMEDIATE')
                    c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,ch['id']))
                    n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']
                    kind='channel.archived' if flag else 'channel.unarchived'
                    c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ch['id'],n,kind,json.dumps({'channel':self.channel(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}),now())); c.commit()
                    self.send(201,{'message':None,'channel':self.channel(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.close(); return
                else: self.send(400,{'error':'unknown command'}); c.close(); return
            parent=b.get('parent_id'); file_ids=b.get('file_ids',[]); mentions=self.mention_ids(c,ch['id'],u['id'],body) if isinstance(body,str) else []
            if not isinstance(body,str) or not body.strip(): self.send(400,{'error':'empty body'}); c.close(); return
            if not isinstance(file_ids,list): self.send(400,{'error':'invalid files'}); c.close(); return
            files=[]
            for fid in file_ids:
                f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
                if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM attachments WHERE file_id=?',(fid,)).fetchone(): self.send(400,{'error':'invalid file'}); c.close(); return
                files.append(f)
            if parent is not None:
                par=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(parent,)).fetchone()
                if not par: self.send(404,{'error':'parent not found'}); c.close(); return
                if par['channel_id']!=ch['id']: self.send(400,{'error':'wrong channel'}); c.close(); return
            c.execute('BEGIN IMMEDIATE')
            cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,mentions,created_at) VALUES(?,?,?,?,?,?)',(ch['id'],u['id'],body,parent,json.dumps(mentions),now()))
            rnew=c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone()
            kind='message.reply' if parent is not None else 'message.created'
            n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']
            for f in files: c.execute('INSERT INTO attachments VALUES(?,?)',(cur.lastrowid,f['id']))
            c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ch['id'],n,kind,json.dumps({'message':self.msg(rnew,c)}),now()))
            c.commit()
            self.send(201,{'message':self.msg(rnew,c)}); c.close(); return
        m=re.fullmatch(r'/api/channels/(\d+)/(leave|archive|unarchive)',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch: self.send(404,{'error':'not found'}); c.close(); return
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            action=m.group(2)
            if action=='leave':
                c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])); c.commit()
                self.send(200,{'channel':self.channel(ch)}); c.close(); return
            if not w or w['owner_id']!=u['id']: self.send(403,{'error':'forbidden'}); c.close(); return
            flag=1 if action=='archive' else 0
            c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,ch['id'])); c.commit()
            self.send(200,{'channel':self.channel(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())}); c.close(); return
        m=re.fullmatch(r'/api/channels/(\d+)/join',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch: self.send(404,{'error':'not found'}); c.close(); return
            mem=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()
            if ch['is_private'] and not mem: self.send(403,{'error':'forbidden'}); c.close(); return
            ws=c.execute('SELECT join_mode FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            if not mem and ws and ws['join_mode']=='invite_only': self.send(403,{'error':'forbidden'}); c.close(); return
            if mem and mem['role']=='guest': self.send(403,{'error':'forbidden'}); c.close(); return
            if not mem: c.execute('INSERT INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'))
            c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id'])); c.commit(); self.send(200,{'channel':self.channel(ch)}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w: self.send(404,{'error':'not found'}); c.close(); return
            actor=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not actor: self.send(403,{'error':'forbidden'}); c.close(); return
            private=bool(b.get('is_private'))
            if actor['role']=='guest' or (private and actor['role'] not in ('owner','admin')): self.send(403,{'error':'forbidden'}); c.close(); return
            name=b.get('name'); topic=b.get('topic','')
            if not isinstance(name,str) or not re.fullmatch(r'[a-z0-9-]{1,32}',name) or not isinstance(topic,str) or len(topic)>250: self.send(400,{'error':'invalid'}); c.close(); return
            try:
                cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,1 if private else 0,topic)); cid=cur.lastrowid; c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id'])); c.commit(); self.send(201,{'channel':self.channel(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
            except sqlite3.IntegrityError: self.send(409,{'error':'duplicate'})
            c.close(); return
        self.send(404,{'error':'not found'}); c.close()
    def do_DELETE(self):
        p=urlparse(self.path).path; c=db(); u=auth(self.headers)
        if not u: self.send(401,{'error':'unauthorized'}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/groups/([^/]+)',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=? AND deleted=0',(w['id'],m.group(2))).fetchone() if w else None
            if not g: self.send(200,{'deleted':True}); c.close(); return
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not actor or (actor['role']!='owner' and g['creator_id']!=u['id']): self.send(403,{'error':'forbidden'}); c.close(); return
            c.execute('UPDATE groups SET deleted=1 WHERE id=?',(g['id'],)); c.commit(); self.send(200,{'deleted':True}); c.close(); return
        m=re.fullmatch(r'/api/messages/(\d+)',p)
        if m:
            r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r: self.send(404,{'error':'not found'}); c.close(); return
            if r['deleted']: self.send(200,{'deleted':True}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone() if ch and ch['workspace_id'] else None
            if r['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']): self.send(403,{'error':'forbidden'}); c.close(); return
            c.execute('BEGIN IMMEDIATE')
            c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],)); c.execute('DELETE FROM reactions WHERE message_id=?',(r['id'],))
            n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(r['channel_id'],)).fetchone()['n']
            c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(r['channel_id'],n,'message.deleted',json.dumps({'message_id':r['id']}),now())); c.commit()
            self.send(200,{'deleted':True}); c.close(); return
        m=re.fullmatch(r'/api/messages/(\d+)/reactions/(.+)',p)
        if m:
            r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(m.group(1),)).fetchone()
            if not r: self.send(404,{'error':'not found'}); c.close(); return
            ch=c.execute('SELECT * FROM channels WHERE id=?',(r['channel_id'],)).fetchone()
            if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(): self.send(403,{'error':'forbidden'}); c.close(); return
            emoji=urllib.parse.unquote(m.group(2))
            cur=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji)); c.commit()
            if cur.rowcount:
                append_event(ch['id'],'reaction.removed',{'message_id':r['id'],'emoji':emoji,'user_id':u['id']})
            rr=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone()
            self.send(200,{'reactions':self.msg(rr,c)['reactions']}); c.close(); return
        m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
        if m:
            c.execute('DELETE FROM pins WHERE message_id=?',(m.group(1),)); c.commit(); self.send(200,{'unpinned':True}); c.close(); return
        m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations/([^/]+)',p)
        if not m: self.send(404,{'error':'not found'}); c.close(); return
        w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
        actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
        if not w: self.send(404,{'error':'not found'}); c.close(); return
        if not actor or actor['role'] not in ('owner','admin'): self.send(403,{'error':'forbidden'}); c.close(); return
        if not c.execute('SELECT 1 FROM invitations WHERE code=? AND workspace_id=?',(m.group(2),w['id'])).fetchone(): self.send(404,{'error':'not found'}); c.close(); return
        c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(m.group(2),)); c.commit(); self.send(200,{'deleted':True}); c.close()
if __name__=='__main__':
    init(); port=int(os.environ.get('PORT','8000')); ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
