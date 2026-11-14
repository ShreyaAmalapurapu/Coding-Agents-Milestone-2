#!/usr/bin/env python3
import base64, hashlib, json, os, re, secrets, sqlite3, struct, threading, time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB='/app/data/huddle.sqlite3'
os.makedirs('/app/data',exist_ok=True)
NODE=int(os.environ.get('NODE_ID','0'))
PORT=8000+NODE
db_lock=threading.RLock()
subs=[]
subs_lock=threading.RLock()

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def conn():
    c=sqlite3.connect(DB,timeout=5,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA busy_timeout=5000')
    return c
def init():
    c=conn()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password TEXT,display_name TEXT,token TEXT UNIQUE);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY AUTOINCREMENT,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,UNIQUE(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,UNIQUE(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,event_id INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message_id INTEGER,kind TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));
    CREATE INDEX IF NOT EXISTS events_idx ON events(channel_id,event_id);
    '''); c.commit(); c.close()
init()

def user(row):
    return {'id':row['id'],'username':row['username'],'display_name':row['display_name'] or row['username'],'timezone':'UTC','avatar_url':None,'status_text':'','status_emoji':''}
def msg(c,r):
    a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[],'reactions':[],'mentions':[]}
def event_frame(c,channel,eid):
    e=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id=?',(channel,eid)).fetchone()
    if not e:return None
    r=c.execute('SELECT * FROM messages WHERE id=?',(e['message_id'],)).fetchone()
    return {'type':e['kind'],'event_id':eid,'channel_id':channel,'message':msg(c,r)}
def broadcast(frame):
    with subs_lock:
        for s in list(subs):
            if s.channel==frame.get('channel_id'): s.offer(frame)
class WS:
    def __init__(self, sock, user_id):
        self.sock=sock; self.user_id=user_id; self.channel=None; self.last={}
        self.q=[]; self.cv=threading.Condition(); self.state=threading.RLock(); self.send_lock=threading.Lock(); self.cursor={}; self.alive=True
    def offer(self,f):
        ch=f.get('channel_id'); eid=f.get('event_id',0)
        with self.state:
            if ch is not None and eid<=self.last.get(ch,0): return
            if ch is not None:self.last[ch]=eid
            with self.cv:self.q.append(f);self.cv.notify()
    def send(self,obj):
        b=json.dumps(obj,separators=(',',':')).encode(); n=len(b)
        if n<126:h=bytes([0x81,n])
        elif n<65536:h=bytes([0x81,126])+struct.pack('!H',n)
        else:h=bytes([0x81,127])+struct.pack('!Q',n)
        with self.send_lock:
            self.sock.sendall(h+b)
    def loop(self):
        try:
            while self.alive:
                with self.cv:
                    if not self.q:self.cv.wait(.02)
                    q=self.q[:];self.q.clear()
                for x in q:self.send(x)
        except: self.alive=False
        finally:
            with subs_lock:
                if self in subs:subs.remove(self)

def poller():
    seen={}
    while True:
        try:
            c=conn()
            rows=c.execute('SELECT channel_id,event_id FROM events ORDER BY channel_id,event_id').fetchall()
            for x in rows:
                k=x['channel_id']; seen[k]=max(seen.get(k,0),x['event_id'])
            # deliver only events newer than each socket's cursor; DB polling is Redis-independent
            with subs_lock:
                ss=list(subs)
            for s in ss:
                if not s.alive or s.channel is None:continue
                with s.state:
                    channel=s.channel
                    last=max(s.cursor.get(channel,0), s.last.get(channel,0))
                rr=c.execute('SELECT event_id FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(channel,last)).fetchall()
                for x in rr:
                    f=event_frame(c,channel,x['event_id'])
                    if f:
                        s.offer(f)
                        with s.state:
                            s.cursor[channel]=max(s.cursor.get(channel,0),x['event_id'])
            c.close()
        except Exception as ex:
            try: open('/tmp/poller-%s.log' % NODE,'a').write(repr(ex)+'\n')
            except: pass
        time.sleep(.01)
threading.Thread(target=poller,daemon=True).start()

class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*a):pass
    def sendj(self,code,obj,extra=None):
        b=json.dumps(obj).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)))
        if extra:
            for k,v in extra.items():self.send_header(k,v)
        self.end_headers();self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0'));return json.loads(self.rfile.read(n) or b'{}')
    def auth(self):
        x=self.headers.get('Authorization','')
        if not x.startswith('Bearer '):return None
        c=conn();r=c.execute('SELECT * FROM users WHERE token=?',(x[7:],)).fetchone();c.close();return r
    def require(self):
        u=self.auth()
        if not u:self.sendj(401,{'error':'unauthorized'})
        return u
    def do_GET(self):
        p=urlparse(self.path); path=p.path; q=parse_qs(p.query)
        if path=='/api/health':return self.sendj(200,{'status':'ok','node_id':NODE})
        if path=='/': return self.sendj(200,{'status':'ok'})
        if path=='/api/ws': return self.websocket()
        u=self.require()
        if not u:return
        c=conn()
        if path=='/api/auth/me':self.sendj(200,{'user':user(u)});c.close();return
        m=re.fullmatch(r'/api/users/(\d+)',path)
        if m:
            r=c.execute('SELECT * FROM users WHERE id=?',(m.group(1),)).fetchone();c.close()
            return self.sendj(200,{'user':user(r)}) if r else self.sendj(404,{'error':'not found'})
        if path=='/api/workspaces':
            rs=c.execute('SELECT * FROM workspaces WHERE id IN (SELECT workspace_id FROM members WHERE user_id=?)',(u['id'],)).fetchall();c.close()
            return self.sendj(200,{'workspaces':[dict(x) for x in rs]})
        m=re.fullmatch(r'/api/workspaces/([^/]+)',path)
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:c.close();return self.sendj(404,{'error':'not found'})
            cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND (is_archived=0 OR ?)',(w['id'],q.get('include_archived',['0'])[0] in ('1','true'))).fetchall()
            ms=c.execute('SELECT m.*,u.username FROM members m JOIN users u ON u.id=m.user_id WHERE workspace_id=?',(w['id'],)).fetchall();c.close()
            return self.sendj(200,{'workspace':dict(w),'channels':[dict(x) for x in cs],'members':[dict(x) for x in ms],'read_state':{}})
        m=re.fullmatch(r'/api/workspaces/([^/]+)/members',path)
        if m:
            w=c.execute('SELECT id FROM workspaces WHERE slug=?',(m.group(1),)).fetchone();rs=c.execute('SELECT * FROM members WHERE workspace_id=?',(w['id'],)).fetchall() if w else []
            c.close();return self.sendj(200,{'members':[dict(x) for x in rs]}) if w else self.sendj(404,{'error':'not found'})
        m=re.fullmatch(r'/api/channels/(\d+)/members',path)
        if m:
            rs=c.execute('SELECT u.* FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(m.group(1),)).fetchall();c.close();return self.sendj(200,{'members':[user(x) for x in rs]})
        m=re.fullmatch(r'/api/channels/(\d+)/messages',path)
        if m:
            rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(m.group(1),)).fetchall();out=[msg(c,x) for x in rs];c.close();return self.sendj(200,{'messages':out,'next_cursor':None})
        m=re.fullmatch(r'/api/messages/(\d+)/replies',path)
        if m:
            rs=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(m.group(1),)).fetchall();out=[msg(c,x) for x in rs];c.close();return self.sendj(200,{'replies':out,'next_cursor':None})
        c.close();self.sendj(404,{'error':'not found'})
    def do_POST(self):
        p=urlparse(self.path);path=p.path
        if path=='/api/auth/register':
            d=self.body(); un=d.get('username','');pw=d.get('password','')
            if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return self.sendj(400,{'error':'invalid'})
            c=conn()
            try:
                tok=secrets.token_urlsafe(24);c.execute('INSERT INTO users(username,password,display_name,token) VALUES(?,?,?,?)',(un,pw,d.get('display_name',un),tok));c.commit();r=c.execute('SELECT * FROM users WHERE username=?',(un,)).fetchone();c.close();return self.sendj(201,{'user':user(r),'token':tok})
            except sqlite3.IntegrityError:c.close();return self.sendj(409,{'error':'duplicate'})
        if path=='/api/auth/login':
            d=self.body();c=conn();r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),d.get('password'))).fetchone();c.close()
            return self.sendj(401,{'error':'invalid'}) if not r else self.sendj(200,{'user':user(r),'token':r['token']})
        u=self.require()
        if not u:return
        d=self.body();c=conn()
        m=re.fullmatch(r'/api/workspaces/([^/]+)/channels',path)
        if path=='/api/workspaces':
            slug=d.get('slug','');name=d.get('name','')
            if not re.fullmatch(r'[a-z0-9-]{2,32}',slug) or not name:return self.sendj(400,{'error':'invalid'})
            try:
                c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,name,u['id']));wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.execute('INSERT INTO counters(channel_id) VALUES(?)',(cid,));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone();ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.sendj(201,{'workspace':dict(w),'general_channel':dict(ch)})
            except sqlite3.IntegrityError:c.rollback();c.close();return self.sendj(409,{'error':'duplicate'})
        if m:
            w=c.execute('SELECT * FROM workspaces WHERE slug=?',(m.group(1),)).fetchone()
            if not w:c.close();return self.sendj(404,{'error':'not found'})
            if not re.fullmatch(r'[a-z0-9-]{1,32}',d.get('name','')):c.close();return self.sendj(400,{'error':'invalid'})
            try:
                c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],d['name'],int(bool(d.get('is_private'))),d.get('topic','')));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(cid,u['id']));c.execute('INSERT INTO counters(channel_id) VALUES(?)',(cid,));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();c.close();return self.sendj(201,{'channel':dict(r)})
            except sqlite3.IntegrityError:c.close();return self.sendj(409,{'error':'duplicate'})
        m=re.fullmatch(r'/api/channels/(\d+)/join',path)
        if m:
            try:c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(m.group(1),u['id']));c.commit();c.close();return self.sendj(200,{'joined':True})
            except:c.close();return self.sendj(404,{'error':'not found'})
        m=re.fullmatch(r'/api/channels/(\d+)/messages',path)
        if m:
            if not d.get('body'):c.close();return self.sendj(400,{'error':'empty'})
            try:
                c.execute('BEGIN IMMEDIATE');ch=c.execute('SELECT * FROM channels WHERE id=?',(m.group(1),)).fetchone()
                if not ch:c.rollback();c.close();return self.sendj(404,{'error':'not found'})
                if not c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone():c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id']))
                t=now();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],d['body'],d.get('parent_id'),t));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('UPDATE counters SET event_id=event_id+1 WHERE channel_id=?',(ch['id'],));eid=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(ch['id'],)).fetchone()['event_id'];c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ch['id'],eid,mid,'message.reply' if d.get('parent_id') else 'message.created',t));c.commit();r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone();frame=event_frame(c,ch['id'],eid);c.close();return self.sendj(201,{'message':msg(conn(),r)})
            except Exception as e:c.rollback();c.close();return self.sendj(500,{'error':str(e)})
        c.close();self.sendj(404,{'error':'not found'})
    def websocket(self):
        u=self.auth()
        if not u:
            qs=parse_qs(urlparse(self.path).query)
            tok=qs.get('token',[''])[0]
            if tok.startswith('Bearer '): tok=tok[7:]
            if tok:
                c=conn(); u=c.execute('SELECT * FROM users WHERE token=?',(tok,)).fetchone(); c.close()
        if not u:return self.sendj(401,{'error':'unauthorized'})
        key=self.headers.get('Sec-WebSocket-Key')
        if not key:return self.sendj(400,{'error':'websocket'})
        accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers()
        s=WS(self.connection,u['id']);subs.append(s);threading.Thread(target=s.loop,daemon=True).start()
        try:
            while True:
                h=self.rfile.read(2)
                if len(h)<2:break
                opcode=h[0]&15; masked=bool(h[1]&128); n=h[1]&127
                if n==126:n=struct.unpack('!H',self.rfile.read(2))[0]
                elif n==127:n=struct.unpack('!Q',self.rfile.read(8))[0]
                mask=self.rfile.read(4) if masked else b''
                raw=self.rfile.read(n)
                if len(raw)!=n:break
                data=bytes(x^mask[i%4] for i,x in enumerate(raw)) if masked else raw
                if opcode==8: break
                if opcode==9:
                    z=bytes([0x8a,len(data)])+data
                    with s.send_lock:s.sock.sendall(z)
                    continue
                if opcode in (10,0): continue
                if opcode!=1: continue
                d=json.loads(data);c=conn();ch=int(d['channel_id']);head=c.execute('SELECT event_id FROM counters WHERE channel_id=?',(ch,)).fetchone()['event_id']
                with s.state:
                    s.channel=ch
                    if d['type']=='subscribe':s.last[ch]=head
                if d['type']=='subscribe':s.send({'type':'subscribed','channel_id':ch,'head_event_id':head})
                elif d['type']=='resume':
                    since=int(d.get('since_event_id',0)); rows=c.execute('SELECT event_id FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
                    for x in rows:
                        f=event_frame(c,ch,x['event_id'])
                        if f:
                            with s.state:
                                s.last[ch]=x['event_id']; s.cursor[ch]=x['event_id']
                            s.send(f)
                    with s.state:s.last[ch]=head; s.cursor[ch]=head
                    s.send({'type':'resumed','channel_id':ch,'head_event_id':head})
                c.close()
        except Exception as ex:
            try: open('/tmp/ws-%s.log' % NODE,'a').write(repr(ex)+'\n')
            except: pass
        s.alive=False

if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', PORT), H).serve_forever()
