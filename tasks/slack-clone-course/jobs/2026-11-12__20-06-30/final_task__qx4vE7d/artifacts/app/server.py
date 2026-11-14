import sys, os, json, sqlite3, hashlib, secrets, threading, time, socket, base64, struct, re
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone

DATA='/app/data'; os.makedirs(DATA,exist_ok=True); DB=DATA+'/huddle.sqlite3'
PORT=int(sys.argv[1]) if len(sys.argv)>1 else 8000
NODE=(PORT-8000)//1_000 if PORT>=8000 else 0
if PORT in (8000,8001,8002): NODE=PORT-8000

def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def conn():
    c=sqlite3.connect(DB,timeout=5,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    c=conn()
    c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created TEXT,edited TEXT);
    CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,kind TEXT,message_id INTEGER,payload TEXT,created TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE INDEX IF NOT EXISTS ev_idx ON events(channel_id,event_id);''')
    c.commit(); c.close()
init()

def user_obj(r):
    return {'id':r['id'],'username':r['username'],'display_name':r['display_name'] or r['username'],'timezone':'UTC','avatar_url':None,'status_text':'','status_emoji':''}
def msg_obj(c,r):
    u=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
    replies=c.execute('select count(*) n from messages where parent_id=?',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created'],'edited_at':r['edited'],'files':[],'reactions':[],'mentions':[]}
def token_user(c,h):
    if not h or not h.startswith('Bearer '): return None
    r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(h[7:],)).fetchone()
    return r
def send_redis(frame):
    try:
        s=socket.create_connection(('127.0.0.1',6379),.15)
        def cmd(*a):
            x='*%d\r\n'%len(a)
            for v in a:
                v=str(v).encode(); x+='$%d\r\n'%len(v)+v.decode(errors='ignore')+'\r\n'
            s.sendall(x.encode())
        cmd('PUBLISH','huddle',json.dumps(frame,separators=(',',':'))); s.close()
    except: pass

clients=[]; clients_lock=threading.Lock()
class WS:
    def __init__(self, sock, uid): self.sock=sock; self.uid=uid; self.sub=set(); self.sent={}; self.lock=threading.Lock(); self.alive=True
    def send(self,o):
        try:
            b=json.dumps(o,separators=(',',':')).encode(); n=len(b)
            if n<126: h=bytes([0x81,n])
            elif n<65536: h=bytes([0x81,126])+struct.pack('!H',n)
            else: h=bytes([0x81,127])+struct.pack('!Q',n)
            with self.lock: self.sock.sendall(h+b)
        except: self.alive=False

def event_frame(c,e):
    r=c.execute('select * from messages where id=?',(e['message_id'],)).fetchone()
    return {'type':e['kind'],'event_id':e['event_id'],'channel_id':e['channel_id'],'message':msg_obj(c,r)}
def poller():
    while True:
        try:
            c=conn()
            with clients_lock: cs=list(clients)
            for w in cs:
                if not w.alive: continue
                for ch in list(w.sub):
                    last=w.sent.get(ch,0)
                    rows=c.execute('select * from events where channel_id=? and event_id>? order by event_id',(ch,last)).fetchall()
                    for e in rows:
                        f=event_frame(c,e); w.send(f); w.sent[ch]=e['event_id']
            c.close()
            with clients_lock:
                clients[:]=[x for x in clients if x.alive]
        except: pass
        time.sleep(.05)
threading.Thread(target=poller,daemon=True).start()

def ws_read(s):
    h=s.recv(2)
    if len(h)<2:return None
    op=h[0]&15; n=h[1]&127
    if n==126:n=struct.unpack('!H',s.recv(2))[0]
    elif n==127:n=struct.unpack('!Q',s.recv(8))[0]
    mask=s.recv(4) if h[1]&128 else b''
    b=s.recv(n); 
    if mask:b=bytes(x^mask[i%4] for i,x in enumerate(b))
    if op==8:return None
    return json.loads(b.decode()) if op==1 else None

class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a): pass
    def out(self,status,obj=None,raw=None,ctype='application/json'):
        if raw is not None: b=raw
        else: b=json.dumps(obj,separators=(',',':')).encode()
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.send_header('Access-Control-Allow-Origin','*'); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
    def auth(self):
        c=conn(); u=token_user(c,self.headers.get('Authorization')); c.close(); return u
    def do_OPTIONS(self): self.out(200,{})
    def do_GET(self):
        p=urlparse(self.path); path=p.path; q=parse_qs(p.query)
        if path=='/': return self.out(200,{},ctype='text/html')
        if path=='/api/health': return self.out(200,{'status':'ok','node_id':NODE})
        if path.startswith('/api/ws'):
            return self.websocket()
        c=conn(); u=token_user(c,self.headers.get('Authorization'))
        if not u: c.close(); return self.out(401,{'error':'unauthorized'})
        if path=='/api/auth/me': z={'user':user_obj(u)}
        elif path=='/api/workspaces':
            rs=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall(); z={'workspaces':[dict(r) for r in rs]}
        elif re.match(r'^/api/workspaces/[^/]+$',path):
            slug=path.split('/')[3]; w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
            if not w:return self.out(404,{'error':'not found'})
            ch=c.execute('select * from channels where workspace_id=?',(w['id'],)).fetchall(); ms=c.execute('select u.id user_id,u.username,u.display_name,m.role from members m join users u on u.id=m.user_id where m.workspace_id=?',(w['id'],)).fetchall()
            z={'workspace':dict(w),'channels':[dict(x) for x in ch],'members':[dict(x) for x in ms]}
        elif path.startswith('/api/channels/') and path.endswith('/messages'):
            cid=int(path.split('/')[3]); rows=c.execute('select * from messages where channel_id=? and parent_id is null order by id desc',(cid,)).fetchall()
            z={'messages':[msg_obj(c,r) for r in rows],'next_cursor':None}
        elif path.startswith('/api/channels/') and path.endswith('/members'):
            cid=int(path.split('/')[3]); rows=c.execute('select u.* from channel_members cm join users u on u.id=cm.user_id where cm.channel_id=?',(cid,)).fetchall(); z={'members':[user_obj(r) for r in rows]}
        else: c.close(); return self.out(404,{'error':'not found'})
        c.close(); self.out(200,z)
    def do_POST(self):
        p=urlparse(self.path).path; c=conn()
        if p=='/api/auth/register':
            b=self.body(); un=b.get('username','')
            if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(b.get('password',''))<8:return self.out(400,{'error':'invalid'})
            try:
                c.execute('insert into users(username,password,display_name,created) values(?,?,?,?)',(un,b['password'],b.get('display_name',un),now())); c.commit()
            except sqlite3.IntegrityError:return self.out(409,{'error':'duplicate'})
            uid=c.execute('select last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,uid)); c.commit(); u=c.execute('select * from users where id=?',(uid,)).fetchone(); c.close(); return self.out(201,{'user':user_obj(u),'token':t})
        if p=='/api/auth/login':
            b=self.body(); u=c.execute('select * from users where username=? and password=?',(b.get('username'),b.get('password'))).fetchone()
            if not u:return self.out(401,{'error':'invalid'})
            t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,u['id'])); c.commit(); c.close(); return self.out(200,{'user':user_obj(u),'token':t})
        u=token_user(c,self.headers.get('Authorization'))
        if not u:c.close();return self.out(401,{'error':'unauthorized'})
        if p=='/api/workspaces':
            b=self.body(); 
            try:
                c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(b['slug'],b['name'],u['id'])); wid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner')); c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into channel_members values(?,?)',(cid,u['id'])); c.execute('insert into seq values(?,0)',(cid,)); c.commit(); w=c.execute('select * from workspaces where id=?',(wid,)).fetchone(); ch=c.execute('select * from channels where id=?',(cid,)).fetchone(); c.close(); return self.out(201,{'workspace':dict(w),'general_channel':dict(ch)})
            except sqlite3.IntegrityError:c.rollback(); c.close();return self.out(409,{'error':'duplicate'})
        if p.startswith('/api/workspaces/') and p.endswith('/channels'):
            slug=p.split('/')[3]; b=self.body(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
            if not w:return self.out(404,{'error':'not found'})
            if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():return self.out(403,{'error':'forbidden'})
            try:c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],b['name'],int(b.get('is_private',False)),b.get('topic',''))); cid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('insert into channel_members values(?,?)',(cid,u['id'])); c.execute('insert into seq values(?,0)',(cid,)); c.commit(); r=c.execute('select * from channels where id=?',(cid,)).fetchone(); c.close(); return self.out(201,{'channel':dict(r)})
            except sqlite3.IntegrityError:c.rollback();c.close();return self.out(409,{'error':'duplicate'})
        if p.startswith('/api/channels/') and p.endswith('/join'):
            cid=int(p.split('/')[3]); r=c.execute('select * from channels where id=?',(cid,)).fetchone()
            if not r:c.close();return self.out(404,{'error':'not found'})
            c.execute('insert or ignore into channel_members values(?,?)',(cid,u['id'])); c.execute('insert or ignore into members values(?,?,?)',(r['workspace_id'],u['id'],'member')); c.commit();c.close();return self.out(200,{'channel':dict(r)})
        if p.startswith('/api/channels/') and p.endswith('/messages'):
            cid=int(p.split('/')[3]); b=self.body(); body=b.get('body','')
            if not body.strip():c.close();return self.out(400,{'error':'empty'})
            if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(cid,u['id'])).fetchone():c.close();return self.out(403,{'error':'forbidden'})
            try:
                c.execute('BEGIN IMMEDIATE'); ts=now(); c.execute('insert into messages(channel_id,author_id,body,parent_id,created) values(?,?,?,?,?)',(cid,u['id'],body,b.get('parent_id'),ts)); mid=c.execute('select last_insert_rowid()').fetchone()[0]; c.execute('update seq set n=n+1 where channel_id=?',(cid,)); eid=c.execute('select n from seq where channel_id=?',(cid,)).fetchone()['n']; r=c.execute('select * from messages where id=?',(mid,)).fetchone(); frame={'type':'message.reply' if b.get('parent_id') else 'message.created','event_id':eid,'channel_id':cid,'message':msg_obj(c,r)}; c.execute('insert into events values(?,?,?,?,?,?)',(cid,eid,frame['type'],mid,json.dumps(frame),ts)); c.commit(); c.close(); send_redis(frame); return self.out(201,{'message':frame['message']})
            except Exception as e:c.rollback();c.close();return self.out(500,{'error':str(e)})
        c.close(); return self.out(404,{'error':'not found'})
    def websocket(self):
        c=conn(); u=token_user(c,self.headers.get('Authorization') or ('Bearer '+parse_qs(urlparse(self.path).query).get('token',[''])[0])); c.close()
        if not u:return self.send_error(401)
        key=self.headers.get('Sec-WebSocket-Key'); accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
        w=WS(self.connection,u['id'])
        with clients_lock:clients.append(w)
        try:
            while True:
                x=ws_read(self.connection)
                if x is None:break
                cid=int(x.get('channel_id',0))
                cc=conn(); head=cc.execute('select coalesce(n,0) n from seq where channel_id=?',(cid,)).fetchone(); head=head['n'] if head else 0
                if x.get('type')=='subscribe': w.sub.add(cid); w.sent[cid]=head; w.send({'type':'subscribed','channel_id':cid,'head_event_id':head})
                elif x.get('type')=='resume':
                    since=int(x.get('since_event_id',0)); w.sub.add(cid)
                    for e in cc.execute('select * from events where channel_id=? and event_id>? order by event_id',(cid,since)).fetchall(): w.send(event_frame(cc,e)); w.sent[cid]=e['event_id']
                    w.send({'type':'resumed','channel_id':cid,'head_event_id':head})
                cc.close()
        except:pass
        w.alive=False
        try:self.connection.close()
        except:pass

if __name__=='__main__':
    ThreadingHTTPServer.allow_reuse_address=True
    ThreadingHTTPServer(('127.0.0.1',PORT),Handler).serve_forever()
