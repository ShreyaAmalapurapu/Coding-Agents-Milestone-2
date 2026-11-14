import os, re, json, sqlite3, hashlib, secrets, datetime, urllib.parse, base64, socket, struct, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

DB='/app/data/huddle.sqlite3'
os.makedirs('/app/data',exist_ok=True)
NODE=int(os.environ.get('NODE_ID','0'))

def db():
    c=sqlite3.connect(DB,timeout=30,isolation_level=None); c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=30000')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT 'UTC',avatar_url TEXT DEFAULT '',status_text TEXT DEFAULT '',status_emoji TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,creator_id INTEGER,expires_at REAL,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0,invited_user_id INTEGER,revoked INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
    CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,pinned_by INTEGER,pinned_at TEXT);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,event_type TEXT,payload TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE TABLE IF NOT EXISTS read_state(user_id INTEGER,channel_id INTEGER,last_read_event_id INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
    CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
    '''); return c
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def userobj(r):
    return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(h):
    x=h.get('Authorization','')
    if not x.startswith('Bearer '): return None
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(x[7:],)).fetchone(); c.close(); return r
def out(h,code,obj):
    b=json.dumps(obj).encode(); h.send_response(code); h.send_header('Content-Type','application/json'); h.send_header('Content-Length',str(len(b))); h.end_headers(); h.wfile.write(b)
def body(h):
    n=int(h.headers.get('Content-Length','0') or 0); return json.loads(h.rfile.read(n) or b'{}')
def wsobj(r): return dict(r)
def chobj(r): 
    x=dict(r); x['is_private']=bool(x['is_private']); x['is_dm']=bool(x['is_dm']); x['is_archived']=bool(x['is_archived']); return x
def pageargs(q):
    try: lim=int(q.get('limit',['50'])[0])
    except: raise ValueError()
    if lim<1 or lim>200: raise ValueError()
    cur=q.get('cursor',[None])[0]
    if cur:
        try: raw=base64.urlsafe_b64decode(cur.encode()+b'='*((4-len(cur)%4)%4)).decode(); before=int(raw)
        except: raise ValueError()
    else: before=None
    return lim,before
def cursor(v):
    return base64.urlsafe_b64encode(str(v).encode()).decode().rstrip('=')
def paged(rows,lim):
    more=len(rows)>lim; rows=rows[:lim]
    return rows, cursor(rows[-1]['id']) if more and rows else None
def message(c,r):
    if not r:return None
    x=dict(r); x['author']=userobj(c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone())
    x['parent_id']=r['parent_id']; x['reply_count']=c.execute('SELECT count(*) FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()[0]
    x['files']=[]; x['reactions']=[]
    for q in c.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
        ids=[z[0] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],q['emoji']))]
        x['reactions'].append({'emoji':q['emoji'],'count':q['n'],'user_ids':ids})
    x['mentions']=[z[0] for z in c.execute('SELECT user_id FROM message_mentions WHERE message_id=? ORDER BY user_id',(r['id'],))] if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='message_mentions'").fetchone() else []; return x
class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a): pass
    def route(self):
        p=urllib.parse.urlparse(self.path); return p.path,urllib.parse.parse_qs(p.query)
    def do_GET(self):
        p,q=self.route(); c=db()
        if p=='/api/ws':
            tok=(q.get('token') or [''])[0]
            if tok.startswith('Bearer '): tok=tok[7:]
            if not c.execute('SELECT 1 FROM tokens WHERE token=?',(tok,)).fetchone():
                self.send_error(401); return
            key=self.headers.get('Sec-WebSocket-Key')
            if not key or self.headers.get('Upgrade','').lower()!='websocket':
                self.send_error(400); return
            accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101,'Switching Protocols')
            self.send_header('Upgrade','websocket'); self.send_header('Connection','Upgrade'); self.send_header('Sec-WebSocket-Accept',accept); self.end_headers()
            def frame(obj):
                b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
                h=bytes([129,n]) if n<126 else (bytes([129,126])+struct.pack('!H',n) if n<65536 else bytes([129,127])+struct.pack('!Q',n))
                self.wfile.write(h+b); self.wfile.flush()
            def readframe():
                h=self.rfile.read(2)
                if len(h)<2:return None
                opcode=h[0]&15
                if opcode==8:return None
                ln=h[1]&127; masked=h[1]&128
                if ln==126: ln=struct.unpack('!H',self.rfile.read(2))[0]
                elif ln==127: ln=struct.unpack('!Q',self.rfile.read(8))[0]
                mask=self.rfile.read(4) if masked else b''; b=self.rfile.read(ln)
                if masked:b=bytes(v^mask[i%4] for i,v in enumerate(b))
                return b
            self.connection.settimeout(0.15)
            subs={}
            while True:
                try: b=readframe()
                except socket.timeout: b=None
                except (OSError,ConnectionError): break
                if b:
                    try:
                        x=json.loads(b); typ=x.get('type'); cid=int(x.get('channel_id'))
                        head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
                        if typ=='subscribe':
                            subs[cid]=head; frame({'type':'subscribed','channel_id':cid,'head_event_id':head})
                        elif typ=='resume':
                            since=int(x.get('since_event_id',0)); subs[cid]=since
                            for ev in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
                                z=json.loads(ev['payload']); frame({'type':ev['event_type'],'event_id':ev['event_id'],'channel_id':cid,**z}); subs[cid]=ev['event_id']
                            frame({'type':'resumed','channel_id':cid,'head_event_id':head})
                    except (ValueError,TypeError,KeyError,json.JSONDecodeError): pass
                for cid,since in list(subs.items()):
                    for ev in c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)):
                        z=json.loads(ev['payload']); frame({'type':ev['event_type'],'event_id':ev['event_id'],'channel_id':cid,**z}); subs[cid]=ev['event_id']
            self.close_connection=True
            return
        if p=='/api/health': return out(self,200,{'status':'ok','node_id':NODE})
        u=auth(self.headers)
        if p=='/' or not p.startswith('/api/'): return self.ui()
        if p=='/api/auth/me':
            return out(self,200,{'user':userobj(u)}) if u else out(self,401,{'error':'unauthorized'})
        if not u:return out(self,401,{'error':'unauthorized'})
        if p=='/api/workspaces':
            rows=c.execute('SELECT DISTINCT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
            return out(self,200,{'workspaces':[wsobj(x) for x in rows]})
        m=re.fullmatch(r'/api/workspaces/([^/]+)',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return out(self,404,{'error':'not found'})
            if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
            rows=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?) AND (is_private=0 OR id IN (SELECT channel_id FROM channel_members WHERE user_id=?))',(w['id'],q.get('include_archived',['0'])[0] in ('1','true'),u['id'])).fetchall()
            return out(self,200,{'workspace':wsobj(w),'channels':[chobj(x) for x in rows],'read_state':{}})
        m=re.fullmatch(r'/api/workspaces/([^/]+)/members',p)
        if m:
            rows=c.execute('SELECT m.*,u.username,u.display_name FROM members m JOIN workspaces w ON w.id=m.workspace_id JOIN users u ON u.id=m.user_id WHERE w.slug=?',(m.group(1),)).fetchall()
            if not rows:return out(self,404,{'error':'not found'})
            return out(self,200,{'members':[dict(x) for x in rows]})
        m=re.fullmatch(r'/api/channels/(\d+)/members',p)
        if m:
            if not c.execute('SELECT 1 FROM channels WHERE id=?',(m.group(1),)).fetchone():return out(self,404,{'error':'not found'})
            rows=c.execute('SELECT u.* FROM channel_members cm JOIN users u ON u.id=cm.user_id WHERE cm.channel_id=?',(m.group(1),)).fetchall()
            return out(self,200,{'members':[userobj(x) for x in rows]})
        m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
        if m:
            r=c.execute('SELECT m.*,ch.is_archived FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(m.group(1),)).fetchone()
            if not r:return out(self,404,{'error':'not found'})
            if r['is_archived']:return out(self,423,{'error':'archived'})
            if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
            t=now();c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r['id'],u['id'],t));c.commit(); z=c.execute('SELECT * FROM pins WHERE message_id=?',(r['id'],)).fetchone()
            return out(self,200,{'pin':{'message_id':r['id'],'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']}})
        m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
        if m:
            try: lim,before=pageargs(q)
            except ValueError:return out(self,400,{'error':'invalid pagination'})
            sql='SELECT * FROM messages WHERE channel_id=? AND deleted=0 AND parent_id IS NULL'
            args=[m.group(1)]
            if before is not None: sql+=' AND id<?';args.append(before)
            sql+=' ORDER BY id DESC LIMIT ?';args.append(lim+1)
            rows=c.execute(sql,args).fetchall(); rows,nc=paged(rows,lim)
            return out(self,200,{'messages':[message(c,x) for x in rows],'next_cursor':nc})
        m=re.fullmatch(r'/api/messages/(\d+)/replies',p)
        if m:
            try: lim,before=pageargs(q)
            except ValueError:return out(self,400,{'error':'invalid pagination'})
            sql='SELECT * FROM messages WHERE parent_id=? AND deleted=0';args=[m.group(1)]
            if before is not None: sql+=' AND id>?';args.append(before)
            sql+=' ORDER BY id LIMIT ?';args.append(lim+1)
            rows=c.execute(sql,args).fetchall(); more=len(rows)>lim; rows=rows[:lim]
            nc=cursor(rows[-1]['id']) if more and rows else None
            return out(self,200,{'replies':[message(c,x) for x in rows],'next_cursor':nc})
        m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])) .fetchone() if w else None
            if not w:return out(self,404,{'error':'not found'})
            if not mem or mem['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
            rows=c.execute("SELECT * FROM invitations WHERE workspace_id=? AND revoked=0 AND (expires_at IS NULL OR expires_at>strftime('%s','now'))",(w['id'],)).fetchall()
            return out(self,200,{'invitations':[dict(x) for x in rows]})
        m=re.fullmatch(r'/api/search',p)
        if m:
            try: lim,before=pageargs(q)
            except ValueError:return out(self,400,{'error':'invalid pagination'})
            term=q.get('q',[''])[0]; args=[u['id'],'%'+term+'%']; sql='''SELECT m.* FROM messages m JOIN channel_members cm ON cm.channel_id=m.channel_id AND cm.user_id=? JOIN channels ch ON ch.id=m.channel_id WHERE m.deleted=0 AND m.body LIKE ?'''
            if q.get('workspace_id'):
                sql+=' AND ch.workspace_id=?';args.append(q['workspace_id'])
            if before is not None:sql+=' AND m.id<?';args.append(before)
            sql+=' ORDER BY m.id DESC LIMIT ?';args.append(lim+1)
            rows=c.execute(sql,args).fetchall();rows,nc=paged(rows,lim)
            return out(self,200,{'results':[message(c,x) for x in rows],'next_cursor':nc})
        m=re.fullmatch(r'/api/channels/(\d+)/read',p)
        if m:
            cid=int(m.group(1)); head=c.execute('SELECT COALESCE(MAX(event_id),0) FROM events WHERE channel_id=?',(cid,)).fetchone()[0]
            row=c.execute('SELECT last_read_event_id FROM read_state WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone(); last=row['last_read_event_id'] if row else 0
            unread=c.execute('SELECT count(*) FROM events WHERE channel_id=? AND event_id>?',(cid,last)).fetchone()[0]
            mentions=c.execute("SELECT count(*) FROM events e JOIN messages m ON m.id=json_extract(e.payload,'$.message_id') JOIN message_mentions mm ON mm.message_id=m.id WHERE e.channel_id=? AND e.event_id>? AND mm.user_id=?",(cid,last,u['id'])).fetchone()[0]
            return out(self,200,{'read_state':{'channel_id':cid,'last_read_event_id':last,'unread_count':unread,'mention_count':mentions}})
        m=re.fullmatch(r'/api/channels/(\d+)/pins',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch:return out(self,404,{'error':'not found'})
            if ch['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
            try:lim,before=pageargs(q)
            except ValueError:return out(self,400,{'error':'invalid pagination'})
            sql='SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? AND m.deleted=0';args=[ch['id']]
            if before is not None:sql+=' AND p.message_id<?';args.append(before)
            sql+=' ORDER BY p.message_id DESC LIMIT ?';args.append(lim+1)
            rows=c.execute(sql,args).fetchall();more=len(rows)>lim;rows=rows[:lim]
            nc=cursor(rows[-1]['message_id']) if more and rows else None
            return out(self,200,{'pins':[{'message':message(c,c.execute('SELECT * FROM messages WHERE id=?',(r['message_id'],)).fetchone()),'pinned_by':r['pinned_by'],'pinned_at':r['pinned_at']} for r in rows],'next_cursor':nc})
        m=re.fullmatch(r'/api/users/(\d+)',p)
        if m:
            r=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone()
            return out(self,200,{'user':userobj(r)}) if r else out(self,404,{'error':'not found'})
        return out(self,404,{'error':'not found'})
    def do_POST(self):
        p,q=self.route(); c=db()
        if p=='/api/auth/register':
            try:d=body(self)
            except:return out(self,400,{'error':'invalid'})
            un=d.get('username'); pw=d.get('password')
            if not isinstance(un,str) or not re.fullmatch(r'[A-Za-z0-9_]+',un) or not isinstance(pw,str) or len(pw)<8:return out(self,400,{'error':'invalid'})
            try:
                cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name') or un)); uid=cur.lastrowid; tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,uid)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return out(self,201,{'user':userobj(r),'token':tok})
            except sqlite3.IntegrityError:return out(self,409,{'error':'duplicate'})
        if p=='/api/auth/login':
            d=body(self); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(str(d.get('password','')).encode()).hexdigest())).fetchone()
            if not r:return out(self,401,{'error':'invalid credentials'})
            tok=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(tok,r['id']));c.commit();return out(self,200,{'user':userobj(r),'token':tok})
        u=auth(self.headers)
        if not u:return out(self,401,{'error':'unauthorized'})
        m=re.fullmatch(r'/api/invitations/([^/]+)/accept',p)
        if m:
            inv=c.execute('SELECT * FROM invitations WHERE code=? AND revoked=0',(m.group(1),)).fetchone()
            if not inv or (inv['expires_at'] and inv['expires_at']<__import__('time').time()) or inv['uses']>=inv['max_uses']:return out(self,404,{'error':'not found'})
            if inv['invited_user_id'] and inv['invited_user_id']!=u['id']:return out(self,403,{'error':'targeted'})
            c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(inv['workspace_id'],u['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(inv['code'],));c.commit();return out(self,200,{'accepted':True})
        m=re.fullmatch(r'/api/workspaces/([^/]+)/transfer_ownership',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return out(self,404,{'error':'not found'})
            mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(); d=body(self); target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],d.get('new_owner_id'))).fetchone()
            if not mem or mem['role']!='owner':return out(self,403,{'error':'forbidden'})
            if not target or target['role']!='admin':return out(self,400,{'error':'invalid'})
            c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('admin',w['id'],u['id']));c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',('owner',w['id'],d['new_owner_id']));c.execute('UPDATE workspaces SET owner_id=? WHERE id=?',(d['new_owner_id'],w['id']));c.commit();return out(self,200,{'workspace':wsobj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
        if p=='/api/workspaces':
            d=body(self); slug=d.get('slug','')
            if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not d.get('name'):return out(self,400,{'error':'invalid'})
            try:
                cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d['name'],u['id'])); wid=cur.lastrowid;c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();return out(self,201,{'workspace':wsobj(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
            except sqlite3.IntegrityError:return out(self,409,{'error':'duplicate'})
        m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); d=body(self)
            mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w:return out(self,404,{'error':'not found'})
            if not mem or mem['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
            mu=d.get('max_uses',1)
            if not isinstance(mu,int) or mu<1:return out(self,400,{'error':'invalid'})
            target=d.get('invited_user_id')
            if not target and d.get('invited_username'):
                z=c.execute('SELECT id FROM users WHERE username=?',(d['invited_username'],)).fetchone(); target=z['id'] if z else None
            code=secrets.token_urlsafe(12); exp=__import__('time').time()+int(d['expires_in_seconds']) if d.get('expires_in_seconds') else None
            if d.get('expires_in_seconds') is not None and int(d['expires_in_seconds'])<1:return out(self,400,{'error':'invalid'})
            if target:mu=1
            c.execute('INSERT INTO invitations VALUES(?,?,?,?,?,?,?,0)',(code,w['id'],u['id'],exp,mu,0,target));c.commit()
            return out(self,201,{'invitation':dict(c.execute('SELECT * FROM invitations WHERE code=?',(code,)).fetchone())})
        m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); d=body(self)
            if not w:return out(self,404,{'error':'not found'})
            mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not mem:return out(self,403,{'error':'forbidden'})
            if mem['role']=='guest' or (d.get('is_private') and mem['role'] not in ('owner','admin')):return out(self,403,{'error':'forbidden'})
            if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')) or len(d.get('topic',''))>250:return out(self,400,{'error':'invalid'})
            try:cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=cur.lastrowid;c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.commit();return out(self,201,{'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone())})
            except sqlite3.IntegrityError:return out(self,409,{'error':'duplicate'})
        m=re.fullmatch(r'/api/channels/(\d+)/read',p)
        if m:
            cid=int(m.group(1)); d=body(self); val=int(d.get('last_read_event_id',0)); row=c.execute('SELECT last_read_event_id FROM read_state WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone(); old=row['last_read_event_id'] if row else 0; val=max(old,val); c.execute('INSERT OR REPLACE INTO read_state VALUES(?,?,?)',(u['id'],cid,val));c.commit()
            unread=c.execute('SELECT count(*) FROM events WHERE channel_id=? AND event_id>?',(cid,val)).fetchone()[0]
            return out(self,200,{'read_state':{'channel_id':cid,'last_read_event_id':val,'unread_count':unread,'mention_count':0}})
        m=re.fullmatch(r'/api/channels/(\d+)/(leave|archive|unarchive)',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch:return out(self,404,{'error':'not found'})
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if m.group(2)=='leave':
                c.execute('DELETE FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id']));c.commit();return out(self,200,{'channel':chobj(ch)})
            if not mem or mem['role']!='owner':return out(self,403,{'error':'forbidden'})
            flag=1 if m.group(2)=='archive' else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(flag,ch['id']));c.commit();return out(self,200,{'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
        m=re.fullmatch(r'/api/channels/(\d+)/join',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch:return out(self,404,{'error':'not found'})
            mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()
            if ch['is_private'] and not mem:return out(self,403,{'error':'private'})
            if mem and mem['role']=='guest':return out(self,403,{'error':'forbidden'})
            w=c.execute('SELECT join_mode FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
            if not mem and w['join_mode']=='invite_only':return out(self,403,{'error':'invite only'})
            if not mem:c.execute('INSERT INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'))
            c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id']));c.commit();return out(self,200,{'channel':chobj(ch)})
        m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
        if m:
            r=c.execute('SELECT m.*,ch.is_archived FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(m.group(1),)).fetchone()
            if not r:return out(self,404,{'error':'not found'})
            if r['is_archived']:return out(self,423,{'error':'archived'})
            if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
            t=now();c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(r['id'],u['id'],t));c.commit(); z=c.execute('SELECT * FROM pins WHERE message_id=?',(r['id'],)).fetchone()
            return out(self,200,{'pin':{'message_id':r['id'],'pinned_by':z['pinned_by'],'pinned_at':z['pinned_at']}})
        m=re.fullmatch(r'/api/messages/(\d+)/reactions',p)
        if m:
            mid=int(m.group(1)); d=body(self); emoji=d.get('emoji')
            if not isinstance(emoji,str) or not emoji:return out(self,400,{'error':'invalid'})
            r=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(mid,)).fetchone()
            if not r:return out(self,404,{'error':'not found'})
            if r['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
            exists=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).fetchone()
            if not exists:
                c.execute('INSERT INTO reactions VALUES(?,?,?)',(mid,u['id'],emoji));eid=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(r['channel_id'],)).fetchone()[0];c.execute('INSERT INTO events VALUES(?,?,?,?)',(r['channel_id'],eid,'reaction.added',json.dumps({'message_id':mid,'emoji':emoji,'user_id':u['id']})));c.commit()
            vals=[{'emoji':z['emoji'],'count':z['n'],'user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(mid,z['emoji']))]} for z in c.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(mid,))]
            return out(self,200,{'reactions':vals})
        m=re.fullmatch(r'/api/channels/(\d+)/messages',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone();d=body(self)
            if not ch:return out(self,404,{'error':'not found'})
            if not isinstance(d.get('body'),str) or not d.get('body').strip():return out(self,400,{'error':'empty'})
            if ch['is_archived']:return out(self,423,{'error':'archived'})
            if d.get('parent_id'):
                par=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(d['parent_id'],)).fetchone()
                if not par or par['channel_id']!=ch['id']:return out(self,400,{'error':'invalid parent'})
            if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():return out(self,403,{'error':'not member'})
            cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],d['body'],d.get('parent_id'),now()))
            mids=set()
            for z in re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_]+)',d['body']):
                if z=='channel':
                    mids.update(x[0] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=? AND user_id!=?',(ch['id'],u['id'])))
                elif z=='here':
                    pass
                else:
                    qx=c.execute('SELECT id FROM users WHERE username=?',(z,)).fetchone()
                    if qx and qx['id']!=u['id'] and c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],qx['id'])).fetchone():mids.add(qx['id'])
            for mid in mids:c.execute('INSERT OR IGNORE INTO message_mentions VALUES(?,?)',(cur.lastrowid,mid))
            eid=(c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(ch['id'],)).fetchone()[0]); et='message.reply' if d.get('parent_id') else 'message.created'; c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch['id'],eid,et,json.dumps({'message_id':cur.lastrowid})));c.commit();return out(self,201,{'message':message(c,c.execute('SELECT * FROM messages WHERE id=?',(cur.lastrowid,)).fetchone())})
        return out(self,404,{'error':'not found'})
    def do_PATCH(self):
        p,q=self.route();u=auth(self.headers);c=db()
        if not u:return out(self,401,{'error':'unauthorized'})
        m=re.fullmatch(r'/api/channels/(\d+)',p)
        if m:
            ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
            if not ch:return out(self,404,{'error':'not found'})
            w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone(); mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(); cm=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(); d=body(self)
            if 'topic' not in d or len(d.get('topic',''))>250:return out(self,400,{'error':'invalid'})
            if ch['is_archived']:return out(self,423,{'error':'archived'})
            if not cm and (not mem or mem['role']!='owner'):return out(self,403,{'error':'forbidden'})
            c.execute('UPDATE channels SET topic=? WHERE id=?',(d['topic'],ch['id']));c.commit();return out(self,200,{'channel':chobj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())})
        m=re.fullmatch(r'/api/workspaces/([^/]+)/members/(\d+)',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return out(self,404,{'error':'not found'})
            actor=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone(); target=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],m.group(2))).fetchone(); d=body(self); role=d.get('role')
            if not target:return out(self,404,{'error':'not found'})
            if role not in ('owner','admin','member','guest'):return out(self,400,{'error':'invalid'})
            if not actor or actor['role'] not in ('owner','admin') or int(m.group(2))==u['id'] or role=='owner':return out(self,400 if role=='owner' else 403,{'error':'forbidden'})
            if actor['role']=='admin' and target['role'] in ('owner','admin'):return out(self,403,{'error':'forbidden'})
            c.execute('UPDATE members SET role=? WHERE workspace_id=? AND user_id=?',(role,w['id'],m.group(2)));c.commit();return out(self,200,{'member':{'user_id':int(m.group(2)),'role':role}})
        m=re.fullmatch(r'/api/workspaces/([^/]+)',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone(); d=body(self); mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone() if w else None
            if not w:return out(self,404,{'error':'not found'})
            if not mem or mem['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
            if 'join_mode' in d and d['join_mode'] not in ('open','invite_only'):return out(self,400,{'error':'invalid'})
            sets=[];vals=[]
            for k in ('name','join_mode'):
                if k in d:sets.append(k+'=?');vals.append(d[k])
            if sets:c.execute('UPDATE workspaces SET '+','.join(sets)+' WHERE id=?',(*vals,w['id']));c.commit()
            return out(self,200,{'workspace':wsobj(c.execute('SELECT * FROM workspaces WHERE id=?',(w['id'],)).fetchone())})
        m=re.fullmatch(r'/api/users/me',p)
        if m:
            d=body(self); allowed={'display_name','timezone','avatar_url','status_text','status_emoji'}; vals={k:v for k,v in d.items() if k in allowed}
            if vals:
                c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),u['id']));c.commit()
            return out(self,200,{'user':userobj(c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone())})
        m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
        if m:
            c.execute('DELETE FROM pins WHERE message_id=?',(m.group(1),));c.commit();return out(self,200,{'unpinned':True})
        m=re.fullmatch(r'/api/messages/(\d+)/reactions/([^/]+)',p)
        if m:
            mid=int(m.group(1)); emoji=urllib.parse.unquote(m.group(2))
            r=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(mid,)).fetchone()
            if not r:return out(self,404,{'error':'not found'})
            if r['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
            c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));c.commit()
            return out(self,200,{'reactions':[{'emoji':z['emoji'],'count':z['n'],'user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(mid,z['emoji']))]} for z in c.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(mid,))]})
        m=re.fullmatch(r'/api/messages/(\d+)',p)
        if m:
            r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r:return out(self,404,{'error':'not found'})
            if r['deleted']:return out(self,409,{'error':'deleted'})
            if r['author_id']!=u['id']:return out(self,403,{'error':'forbidden'})
            d=body(self)
            if not isinstance(d.get('body'),str) or not d['body'].strip():return out(self,400,{'error':'empty'})
            c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(d.get('body',''),now(),r['id']));c.commit();return out(self,200,{'message':message(c,c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone())})
        return out(self,404,{'error':'not found'})
    def do_DELETE(self):
        p,q=self.route();u=auth(self.headers);c=db()
        if not u:return out(self,401,{'error':'unauthorized'})
        m=re.fullmatch(r'/api/workspaces/([^/]+)/invitations/([^/]+)',p)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:return out(self,404,{'error':'not found'})
            mem=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
            if not mem or mem['role'] not in ('owner','admin'):return out(self,403,{'error':'forbidden'})
            inv=c.execute('SELECT code FROM invitations WHERE code=? AND workspace_id=?',(m.group(2),w['id'])).fetchone()
            if not inv:return out(self,404,{'error':'not found'})
            c.execute('UPDATE invitations SET revoked=1 WHERE code=?',(m.group(2),));c.commit()
            return out(self,200,{'deleted':True})
        m=re.fullmatch(r'/api/messages/(\d+)/pin',p)
        if m:
            c.execute('DELETE FROM pins WHERE message_id=?',(m.group(1),));c.commit();return out(self,200,{'unpinned':True})
        m=re.fullmatch(r'/api/messages/(\d+)/reactions/([^/]+)',p)
        if m:
            mid=int(m.group(1)); emoji=urllib.parse.unquote(m.group(2))
            r=c.execute('SELECT m.*,ch.is_private FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(mid,)).fetchone()
            if not r:return out(self,404,{'error':'not found'})
            if r['is_private'] and not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():return out(self,403,{'error':'forbidden'})
            c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji));c.commit()
            vals=[{'emoji':z['emoji'],'count':z['n'],'user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(mid,z['emoji']))]} for z in c.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(mid,))]
            return out(self,200,{'reactions':vals})
        m=re.fullmatch(r'/api/messages/(\d+)',p)
        if m:
            r=c.execute('SELECT * FROM messages WHERE id=?',(m.group(1),)).fetchone()
            if not r:return out(self,404,{'error':'not found'})
            w=c.execute('SELECT owner_id FROM workspaces w JOIN channels ch ON ch.workspace_id=w.id WHERE ch.id=?',(r['channel_id'],)).fetchone()
            if r['deleted']:return out(self,200,{'deleted':True})
            if r['author_id']!=u['id'] and (not w or w['owner_id']!=u['id']):return out(self,403,{'error':'forbidden'})
            eid=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM events WHERE channel_id=?',(r['channel_id'],)).fetchone()[0]
            c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],));c.execute('DELETE FROM reactions WHERE message_id=?',(r['id'],));c.execute('INSERT INTO events VALUES(?,?,?,?)',(r['channel_id'],eid,'message.deleted',json.dumps({'message_id':r['id']})));c.commit();return out(self,200,{'deleted':True})
        return out(self,404,{'error':'not found'})
    def ui(self):
        with open('/app/index.html','rb') as f: b=f.read()
        self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)

def main():
    ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('PORT',8000))),H).serve_forever()
if __name__=='__main__': main()
