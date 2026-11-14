#!/usr/bin/env python3
import os, re, json, sqlite3, hashlib, secrets, base64, struct, socket, threading, time, cgi
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DB='/app/data/huddle.db'
os.makedirs('/app/data',exist_ok=True)
NODE=int(os.environ.get('NODE_ID','0'))
PORT=int(os.environ.get('PORT','8000'))
db_lock=threading.RLock()
sockets=[]
subscriptions={}
sock_lock=threading.RLock()

def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def conn():
    c=sqlite3.connect(DB,timeout=5,check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=5000')
    for _ in range(20):
        try:
            c.execute('PRAGMA journal_mode=WAL')
            break
        except sqlite3.OperationalError:
            time.sleep(0.1)
    return c
def init():
    for attempt in range(30):
        try:
            with conn() as c:
                c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT,mention_count INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
        CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT);
        CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
        CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,dm_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS reactions(id INTEGER PRIMARY KEY AUTOINCREMENT,message_id INTEGER,user_id INTEGER,emoji TEXT,UNIQUE(message_id,user_id,emoji));
        CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
        CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id INTEGER UNIQUE);
        CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
        CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
        CREATE TABLE IF NOT EXISTS message_mentions(message_id INTEGER,user_id INTEGER,PRIMARY KEY(message_id,user_id));
        CREATE TABLE IF NOT EXISTS channel_mentions(channel_id INTEGER,user_id INTEGER,count INTEGER DEFAULT 0,PRIMARY KEY(channel_id,user_id));
        CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,UNIQUE(channel_id,event_id));
                CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,payload TEXT);
        CREATE TABLE IF NOT EXISTS online_sessions(conn_key TEXT PRIMARY KEY,user_id INTEGER,updated_at REAL);
                ''')
            try: c.execute('ALTER TABLE users ADD COLUMN mention_count INTEGER DEFAULT 0')
            except sqlite3.OperationalError: pass
            return
        except sqlite3.OperationalError:
            time.sleep(0.2)
    raise RuntimeError('database initialization failed')
def userobj(r):
    return {k:r[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji','mention_count')}
def auth(h):
    a=h.headers.get('Authorization','')
    if not a.startswith('Bearer '): return None
    with conn() as c:
        r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(a[7:],)).fetchone()
    return r
def jerr(h,status,msg):
    h.send_json({'error':msg},status)
def event(c,channel,typ,payload):
    with db_lock:
        n=c.execute('SELECT n FROM counters WHERE channel_id=?',(channel,)).fetchone()
        eid=(n['n'] if n else 0)+1
        c.execute('INSERT OR REPLACE INTO counters(channel_id,n) VALUES(?,?)',(channel,eid))
        c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(channel,eid,typ,json.dumps(payload)))
        c.commit()
    return eid
def msgobj(c,r):
    u=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    replies=c.execute('SELECT count(*) x FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['x']
    fs=c.execute('SELECT f.* FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(r['id'],)).fetchall()
    filelist=[{k:x[k] for k in ('id','uploader_id','filename','content_type','size','created_at')} for x in fs]
    mids=[x['user_id'] for x in c.execute('SELECT user_id FROM message_mentions WHERE message_id=? ORDER BY user_id',(r['id'],)).fetchall()]
    rr=c.execute('SELECT emoji,COUNT(*) count FROM reactions WHERE message_id=? GROUP BY emoji ORDER BY MIN(id)',(r['id'],)).fetchall()
    reactions=[]
    for x in rr:
        ids=[z['user_id'] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=? ORDER BY id',(r['id'],x['emoji'])).fetchall()]
        reactions.append({'emoji':x['emoji'],'count':x['count'],'user_ids':ids})
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(u),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':filelist,'reactions':reactions,'mentions':mids}
def broadcast(frame):
    raw=wsframe(json.dumps(frame))
    channel=frame.get('channel_id')
    with sock_lock:
        for s in list(sockets):
            if channel is not None and channel not in subscriptions.get(s,set()):
                continue
            try: s.sendall(raw)
            except:
                try:
                    sockets.remove(s)
                    subscriptions.pop(s,None)
                except: pass
def wsframe(x):
    b=x.encode(); n=len(b)
    if n<126: return bytes([0x81,n])+b
    if n<65536:return bytes([0x81,126])+struct.pack('!H',n)+b
    return bytes([0x81,127])+struct.pack('!Q',n)+b
def poller():
    with conn() as c:
        last_event=c.execute('SELECT COALESCE(MAX(id),0) x FROM events').fetchone()['x']
        last_global=c.execute('SELECT COALESCE(MAX(id),0) x FROM global_events').fetchone()['x']
    while True:
        try:
            with conn() as c:
                globals_=c.execute('SELECT id,type,payload FROM global_events WHERE id>? ORDER BY id',(last_global,)).fetchall()
                events=c.execute('SELECT id,channel_id,event_id,type,payload FROM events WHERE id>? ORDER BY id',(last_event,)).fetchall()
                for r in globals_:
                    last_global=r['id']
                    broadcast(json.loads(r['payload']))
                for r in events:
                    last_event=r['id']
                    payload=json.loads(r['payload'])
                    payload.update(type=r['type'],channel_id=r['channel_id'],event_id=r['event_id'])
                    broadcast(payload)
        except Exception:
            pass
        time.sleep(.08)

class H(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def send_json(self,x,status=200):
        b=json.dumps(x).encode(); self.send_response(status); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n) or b'{}')
    def route(self): return urlparse(self.path).path.split('/')
    def do_GET(self):
        p=self.route(); q=parse_qs(urlparse(self.path).query)
        if p==['','api','health']: return self.send_json({'status':'ok','node_id':NODE})
        if p==['','api','ws']: return self.websocket(q.get('token',[''])[0])
        u=auth(self)
        if not u:return jerr(self,401,'unauthorized')
        with conn() as c:
            if len(p)==4 and p[2]=='users':
                r=c.execute('SELECT * FROM users WHERE id=?',(p[3],)).fetchone()
                return self.send_json({'user':userobj(r)} if r else {'error':'not found'},200 if r else 404)
            if p==['','api','auth','me']: return self.send_json({'user':userobj(u)})
            if len(p)==4 and p[2]=='files':
                f=c.execute('SELECT * FROM files WHERE id=?',(p[3],)).fetchone()
                if not f:return jerr(self,404,'not found')
                return self.send_json({'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
            if len(p)==5 and p[2]=='files' and p[4]=='download':
                f=c.execute('SELECT * FROM files WHERE id=?',(p[3],)).fetchone()
                if not f:return jerr(self,404,'not found')
                try:data=open(f['path'],'rb').read()
                except:return jerr(self,404,'not found')
                self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename'].replace('"','')+'"');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            if len(p)==5 and p[2]=='workspaces' and p[4]=='groups':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
                if not w:return jerr(self,404,'not found')
                if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return jerr(self,403,'forbidden')
                gs=c.execute('SELECT * FROM groups WHERE workspace_id=? ORDER BY id',(w['id'],)).fetchall()
                out=[]
                for g in gs:
                    ids=[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id',(g['id'],)).fetchall()]
                    out.append({'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':ids})
                return self.send_json({'groups':out})
            if p==['','api','workspaces']:
                rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall()
                return self.send_json({'workspaces':[dict(r) for r in rs]})
            if len(p)==4 and p[2]=='workspaces':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
                if not w:return jerr(self,404,'not found')
                cs=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_dm=0 AND (is_archived=0 OR ?)',(w['id'],q.get('include_archived',['0'])[0] in ('1','true'))).fetchall()
                return self.send_json({'workspace':dict(w),'channels':[dict(x) for x in cs],'read_state':{}})
            if len(p)==5 and p[2]=='workspaces' and p[4]=='members':
                w=c.execute('SELECT id FROM workspaces WHERE slug=?',(p[3],)).fetchone(); rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall()
                return self.send_json({'members':[dict(x) for x in rs]})
            if len(p)==4 and p[2]=='channels':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[3],)).fetchone()
                if not ch:return jerr(self,404,'not found')
                if ch['is_dm'] and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone() is None:return jerr(self,403,'forbidden')
                return self.send_json({'channel':dict(ch)})
            if len(p)==5 and p[2]=='channels' and p[4]=='members':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[3],)).fetchone()
                if not ch:return jerr(self,404,'not found')
                if ch['is_dm'] and c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone() is None:return jerr(self,403,'forbidden')
                rs=c.execute('SELECT u.* FROM channel_members m JOIN users u ON u.id=m.user_id WHERE m.channel_id=?',(p[3],)).fetchall()
                return self.send_json({'members':[userobj(x) for x in rs]})
            if len(p)==5 and p[2]=='channels' and p[4]=='read':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[3],)).fetchone()
                if not ch:return jerr(self,404,'not found')
                if c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone() is None:return jerr(self,403,'forbidden')
                head=c.execute('SELECT COALESCE(MAX(event_id),0) n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']
                mc=c.execute('SELECT COALESCE(count,0) n FROM channel_mentions WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone(); return self.send_json({'read_state':{'channel_id':ch['id'],'last_read_event_id':head,'unread_count':0,'mention_count':mc['n'] if mc else 0}})
            if len(p)==5 and p[2]=='channels' and p[4]=='messages':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[3],)).fetchone()
                if not ch:return jerr(self,404,'not found')
                if c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone() is None:return jerr(self,403,'forbidden')
                rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(p[3],)).fetchall()
                return self.send_json({'messages':[msgobj(c,x) for x in rs],'next_cursor':None})
        return jerr(self,404,'not found')
    def do_POST(self):
        p=self.route(); u=auth(self)
        if p==['','api','auth','register'] or p==['','api','auth','login']:
            try:b=self.body()
            except:return jerr(self,400,'bad body')
            if p[-1]=='register':
                if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8:return jerr(self,400,'invalid')
                try:
                    with conn() as c:
                        c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(b['username'],hashlib.sha256(b['password'].encode()).hexdigest(),b.get('display_name') or b['username'],'UTC','','','')); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit()
                except sqlite3.IntegrityError:return jerr(self,409,'duplicate')
            else:
                with conn() as c:r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),hashlib.sha256(b.get('password','').encode()).hexdigest())).fetchone()
                if not r:return jerr(self,401,'invalid')
                uid=r['id']
            with conn() as c:
                r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid));c.commit()
            return self.send_json({'user':userobj(r),'token':t},201 if p[-1]=='register' else 200)
        if not u:return jerr(self,401,'unauthorized')
        if p==['','api','files']:
            ct=self.headers.get('Content-Type','')
            if 'multipart/form-data' not in ct:return jerr(self,400,'multipart required')
            n=int(self.headers.get('Content-Length','0'))
            raw=self.rfile.read(n)
            m=re.search(r'boundary=(?:"([^"]+)"|([^;]+))',ct)
            if not m:return jerr(self,400,'multipart required')
            boundary=(m.group(1) or m.group(2)).encode()
            part=None
            for chunk in raw.split(b'--'+boundary):
                if b'Content-Disposition:' not in chunk: continue
                head,sep,data=chunk.partition(b'\r\n\r\n')
                if not sep: continue
                disp=re.search(br'name="([^"]+)"(?:;\s*filename="([^"]*)")?',head,re.I)
                if disp and disp.group(1)==b'file' and disp.group(2) is not None:
                    filename=disp.group(2).decode('utf-8','replace')
                    ctm=re.search(br'Content-Type:\s*([^\r\n]+)',head,re.I)
                    content_type=(ctm.group(1).decode().strip() if ctm else 'application/octet-stream')
                    part=(filename,content_type,data[:-2] if data.endswith(b'\r\n') else data); break
            if part is None:return jerr(self,400,'file required')
            filename,content_type,data=part
            if len(data)>10485760:return jerr(self,413,'file too large')
            fid=secrets.token_hex(12); path='/app/data/upload-'+fid
            open(path,'wb').write(data)
            with conn() as c:
                c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],filename,content_type,len(data),now(),path)); f=c.execute('SELECT * FROM files WHERE id=last_insert_rowid()').fetchone();c.commit()
            return self.send_json({'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}},201)
        try:b=self.body()
        except:return jerr(self,400,'bad body')
        with conn() as c:
            if len(p)==4 and p[2]=='files':
                f=c.execute('SELECT * FROM files WHERE id=?',(p[3],)).fetchone()
                if not f:return jerr(self,404,'not found')
                return self.send_json({'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
            if len(p)==5 and p[2]=='files' and p[4]=='download':
                f=c.execute('SELECT * FROM files WHERE id=?',(p[3],)).fetchone()
                if not f:return jerr(self,404,'not found')
                try:data=open(f['path'],'rb').read()
                except:return jerr(self,404,'not found')
                self.send_response(200);self.send_header('Content-Type',f['content_type']);self.send_header('Content-Disposition','attachment; filename="'+f['filename'].replace('"','')+'"');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            if p==['','api','workspaces']:
                if not re.fullmatch(r'[a-z0-9-]{2,32}',b.get('slug','')):return jerr(self,400,'invalid')
                try:
                    c.execute('INSERT INTO workspaces(slug,name,owner_id,join_mode) VALUES(?,?,?,?)',(b['slug'],b.get('name',''),u['id'],'open')); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm) VALUES(?,?,0,0)',(wid,'general')); ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(ch,u['id']));c.execute('INSERT INTO counters VALUES(?,0)',(ch,));c.commit()
                except sqlite3.IntegrityError:return jerr(self,409,'duplicate')
                return self.send_json({'workspace':dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()),'general_channel':dict(c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone())},201)
            if len(p)==5 and p[2]=='workspaces' and p[4]=='groups':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
                if not w:return jerr(self,404,'not found')
                if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():return jerr(self,403,'forbidden')
                handle=b.get('handle',''); name=b.get('name','')
                if not isinstance(handle,str) or not re.fullmatch(r'[a-z0-9_-]{2,32}',handle) or not isinstance(name,str):return jerr(self,400,'invalid')
                if c.execute('SELECT 1 FROM groups WHERE workspace_id=? AND handle=?',(w['id'],handle)).fetchone():return jerr(self,409,'duplicate')
                ids=b.get('user_ids',[])
                if not isinstance(ids,list):return jerr(self,400,'invalid')
                c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],handle,name,u['id'])); gid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                for uid in set(ids):
                    if c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone(): c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
                c.commit(); return self.send_json({'group':{'id':gid,'handle':handle,'name':name,'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(gid,)).fetchall()]}},201)
            if len(p)==5 and p[2]=='workspaces' and p[4]=='channels':
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone()
                if not w:return jerr(self,404,'not found')
                c.execute('INSERT INTO channels(workspace_id,name,is_private,is_dm,topic) VALUES(?,?,?,?,?)',(w['id'],b.get('name'),int(b.get('is_private',False)),0,b.get('topic',''))); ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?)',(ch,u['id']));c.execute('INSERT INTO counters VALUES(?,0)',(ch,));c.commit();return self.send_json({'channel':dict(c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone())},201)
            if len(p)==5 and p[2]=='channels' and p[4]=='join':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[3],)).fetchone()
                if not ch:return jerr(self,404,'not found')
                c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id']))
                if not ch['is_private'] and not ch['is_dm'] and ch['workspace_id']:
                    c.execute('INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'))
                c.commit();return self.send_json({'channel':dict(ch)})
            if len(p)==5 and p[2]=='messages' and p[4]=='reactions':
                mid=p[3]; emoji=b.get('emoji')
                if not isinstance(emoji,str) or not emoji:return jerr(self,400,'invalid emoji')
                m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                if not m:return jerr(self,404,'not found')
                ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
                if c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone() is None:return jerr(self,403,'forbidden')
                added=False
                try:
                    c.execute('INSERT INTO reactions(message_id,user_id,emoji) VALUES(?,?,?)',(mid,u['id'],emoji)); added=True
                except sqlite3.IntegrityError: pass
                frame={'message_id':int(mid),'emoji':emoji,'user_id':u['id'],'channel_id':ch['id']}
                if added:
                    eid=event(c,ch['id'],'reaction.added',{'message_id':int(mid),'emoji':emoji,'user_id':u['id']})
                    c.commit()
                return self.send_json({'reactions':msgobj(c,m)['reactions']})
            if len(p)==5 and p[2]=='channels' and p[4]=='messages':
                ch=c.execute('SELECT * FROM channels WHERE id=?',(p[3],)).fetchone()
                if not ch:return jerr(self,404,'not found')
                if c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone() is None:return jerr(self,403,'forbidden')
                body=b.get('body')
                if not isinstance(body,str) or not body:return jerr(self,400,'empty')
                if body.startswith('/') and not body.startswith('//'):
                    pieces=body.split(' ',1); cmd=pieces[0].lower(); text=pieces[1].strip() if len(pieces)>1 else ''
                    w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
                    owner=(w and w['owner_id']==u['id'])
                    if cmd=='/me':
                        if not text:return jerr(self,400,'text required')
                    elif cmd=='/shrug':
                        body=(text+' ' if text else '')+'¯\\_(ツ)_/¯'
                    elif cmd=='/topic':
                        if len(text)>250:return jerr(self,400,'topic too long')
                        c.execute('UPDATE channels SET topic=? WHERE id=?',(text,ch['id']))
                        fresh=dict(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())
                        eid=event(c,ch['id'],'channel.updated',{'channel':fresh}); c.commit()
                        return self.send_json({'message':None,'channel':fresh},201)
                    elif cmd=='/invite':
                        target=text[1:] if text.startswith('@') else text
                        target_user=c.execute('SELECT * FROM users WHERE username=?',(target,)).fetchone()
                        if not target_user:return jerr(self,404,'user not found')
                        if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],target_user['id'])).fetchone():return jerr(self,404,'user not found')
                        existed=c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],target_user['id'])).fetchone() is not None
                        c.execute('INSERT OR IGNORE INTO channel_members(channel_id,user_id) VALUES(?,?)',(ch['id'],target_user['id']))
                        if not existed:
                            event(c,ch['id'],'member.joined',{'user_id':target_user['id']})
                        c.commit()
                        return self.send_json({'message':None,'invited_user':userobj(target_user)},201)
                    elif cmd in ('/archive','/unarchive'):
                        if not owner:return jerr(self,403,'forbidden')
                        archived=cmd=='/archive'
                        c.execute('UPDATE channels SET is_archived=? WHERE id=?',(int(archived),ch['id']))
                        fresh=dict(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())
                        eid=event(c,ch['id'],'channel.archived' if archived else 'channel.unarchived',{'channel':fresh}); c.commit()
                        return self.send_json({'message':None,'channel':fresh},201)
                    else:return jerr(self,400,'unknown command')
                if ch['is_archived']:return jerr(self,423,'channel archived')
                b['body']=body
                ids=b.get('file_ids',[])
                if not isinstance(ids,list):return jerr(self,400,'invalid file_ids')
                for fid in ids:
                    f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
                    if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM message_files WHERE file_id=?',(fid,)).fetchone():return jerr(self,400,'invalid attachment')
                # Resolve username, group, channel and online here mentions.
                mention_ids=set()
                toks=re.findall(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)(?![A-Za-z0-9_.@-])',body)
                members=[x['user_id'] for x in c.execute('SELECT user_id FROM channel_members WHERE channel_id=?',(ch['id'],)).fetchall()]
                for tok in toks:
                    if tok=='channel': mention_ids.update(members)
                    elif tok=='here':
                        online=[x['user_id'] for x in c.execute('SELECT DISTINCT user_id FROM online_sessions').fetchall()]
                        mention_ids.update(set(members).intersection(online))
                    else:
                        ur=c.execute('SELECT id FROM users WHERE username=?',(tok,)).fetchone()
                        if ur: mention_ids.add(ur['id'])
                        gr=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(ch['workspace_id'],tok)).fetchone()
                        if gr: mention_ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(gr['id'],)).fetchall())
                mention_ids.discard(u['id']); mention_ids.intersection_update(members)
                t=now();c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],u['id'],body,b.get('parent_id'),t)); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
                for uid in mention_ids:
                    c.execute('INSERT INTO message_mentions VALUES(?,?)',(mid,uid))
                    try:c.execute('UPDATE users SET mention_count=COALESCE(mention_count,0)+1 WHERE id=?',(uid,))
                    except sqlite3.OperationalError:pass
                    c.execute('INSERT INTO channel_mentions(channel_id,user_id,count) VALUES(?,?,1) ON CONFLICT(channel_id,user_id) DO UPDATE SET count=count+1',(ch['id'],uid))
                for fid in ids:c.execute('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',(mid,fid))
                r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); m=msgobj(c,r); eid=event(c,ch['id'],'message.created',m);c.commit();m['event_id']=eid;return self.send_json({'message':m},201)
            if p==['','api','dms']:
                oid=b.get('user_id')
                if oid is None:return jerr(self,400,'user_id required')
                other=c.execute('SELECT * FROM users WHERE id=?',(oid,)).fetchone()
                if not other:return jerr(self,404,'not found')
                key=':'.join(map(str,sorted((u['id'],oid))))
                ch=c.execute('SELECT * FROM channels WHERE dm_key=?',(key,)).fetchone()
                if not ch:
                    c.execute('INSERT INTO channels(name,is_private,is_dm,dm_key) VALUES(?,?,1,?)',('dm',1,key));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO channel_members VALUES(?,?),(?,?)',(cid,u['id'],cid,oid));c.execute('INSERT INTO counters VALUES(?,0)',(cid,));c.commit()
                else:cid=ch['id']
                return self.send_json({'channel_id':cid})
        return jerr(self,404,'not found')
    def _delete_reaction(self):
        u=auth(self)
        if not u:return jerr(self,401,'unauthorized')
        p=self.route()
        if len(p)==6 and p[2]=='messages' and p[4]=='reactions':
            mid=p[3]; emoji=unquote(p[5])
            with conn() as c:
                m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                if not m:return jerr(self,404,'not found')
                ch=c.execute('SELECT * FROM channels WHERE id=?',(m['channel_id'],)).fetchone()
                if c.execute('SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone() is None:return jerr(self,403,'forbidden')
                deleted=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],emoji)).rowcount
                if deleted:
                    eid=event(c,ch['id'],'reaction.removed',{'message_id':int(mid),'emoji':emoji,'user_id':u['id']})
                    c.commit()
                r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()
                return self.send_json({'reactions':msgobj(c,r)['reactions']})
        return jerr(self,404,'not found')

    def _group_obj(self,c,g):
        return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id',(g['id'],)).fetchall()]}
    def do_DELETE(self):
        if len(self.route())==6 and self.route()[2]=='messages' and self.route()[4]=='reactions':
            return self._delete_reaction()
        u=auth(self)
        if not u:return jerr(self,401,'unauthorized')
        p=self.route()
        if len(p)==6 and p[2]=='workspaces' and p[4]=='groups':
            with conn() as c:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone(); g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],p[5])).fetchone() if w else None
                if not g:return self.send_json({'deleted':True})
                if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return jerr(self,403,'forbidden')
                c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit();return self.send_json({'deleted':True})
        return jerr(self,404,'not found')
    def do_PATCH(self):
        u=auth(self)
        if not u:return jerr(self,401,'unauthorized')
        p=self.route()
        if len(p)==6 and p[2]=='workspaces' and p[4]=='groups':
            try:b=self.body()
            except:return jerr(self,400,'bad body')
            with conn() as c:
                w=c.execute('SELECT * FROM workspaces WHERE slug=?',(p[3],)).fetchone(); g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],p[5])).fetchone() if w else None
                if not g:return jerr(self,404,'not found')
                if u['id']!=g['creator_id'] and u['id']!=w['owner_id']:return jerr(self,403,'forbidden')
                if 'name' in b:
                    if not isinstance(b['name'],str):return jerr(self,400,'invalid')
                    c.execute('UPDATE groups SET name=? WHERE id=?',(b['name'],g['id']))
                if 'user_ids' in b:
                    c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
                    for uid in set(b['user_ids']):
                        if c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],uid)).fetchone():c.execute('INSERT INTO group_members VALUES(?,?)',(g['id'],uid))
                c.commit();return self.send_json({'group':self._group_obj(c,c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone())})
        if p==['','api','users','me']:
            try:b=self.body()
            except:return jerr(self,400,'bad body')
            allowed={'display_name':(1,80),'timezone':(1,200),'avatar_url':(0,2048),'status_text':(0,100),'status_emoji':(0,64)}
            for k,v in b.items():
                if k not in allowed or not isinstance(v,str) or not(allowed[k][0]<=len(v)<=allowed[k][1]):return jerr(self,400,'invalid')
                if k=='timezone':
                    try:ZoneInfo(v)
                    except:return jerr(self,400,'invalid timezone')
            with conn() as c:
                if b:
                    sets=','.join(k+'=?' for k in b)
                    c.execute('UPDATE users SET '+sets+' WHERE id=?',(*b.values(),u['id']))
                    r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
                    c.execute('INSERT INTO global_events(type,payload) VALUES(?,?)',('user.updated',json.dumps({'type':'user.updated','user':userobj(r)})))
                    c.commit()
                else:
                    r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
            return self.send_json({'user':userobj(r)})
        return jerr(self,404,'not found')
    def websocket(self,token):
        if token.startswith('Bearer '): token=token[7:]
        with conn() as c:r=c.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()
        if not r:return self.send_error(401); 
        key=self.headers.get('Sec-WebSocket-Key')
        if not key:return self.send_error(400)
        self.send_response(101,'Switching Protocols');self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode());self.end_headers()
        s=self.connection
        with sock_lock:
            sockets.append(s)
            subscriptions[s]=set()
            with conn() as c:
                conn_key=secrets.token_hex(16)
                c.execute('INSERT INTO online_sessions(conn_key,user_id,updated_at) VALUES(?,?,?)',(conn_key,r['user_id'],time.time())); c.commit()
        try:
            while True:
                h=s.recv(2)
                if not h:break
                n=h[1]&127
                if n==126:n=struct.unpack('!H',s.recv(2))[0]
                elif n==127:n=struct.unpack('!Q',s.recv(8))[0]
                mask=s.recv(4);data=bytes(x^mask[i%4] for i,x in enumerate(s.recv(n)))
                x=json.loads(data)
                if x.get('type')=='subscribe':
                    with sock_lock: subscriptions.setdefault(s,set()).add(int(x['channel_id']))
                    with conn() as c:
                        z=c.execute('SELECT max(event_id) n FROM events WHERE channel_id=?',(x['channel_id'],)).fetchone()['n'] or 0
                    s.sendall(wsframe(json.dumps({'type':'subscribed','channel_id':x['channel_id'],'head_event_id':z})))
                elif x.get('type')=='resume':
                    with conn() as c:
                        rs=c.execute('SELECT event_id,type,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(x['channel_id'],x.get('since_event_id',0))).fetchall()
                    for z in rs:
                        q=json.loads(z['payload']);q.update(type=z['type'],channel_id=x['channel_id'],event_id=z['event_id']);s.sendall(wsframe(json.dumps(q)))
                    s.sendall(wsframe(json.dumps({'type':'resumed','channel_id':x['channel_id'],'head_event_id':rs[-1]['event_id'] if rs else x.get('since_event_id',0)})))
        except:pass
        finally:
            with sock_lock:
                if s in sockets:sockets.remove(s)
                subscriptions.pop(s,None)
                try:
                    with conn() as c:
                        c.execute('DELETE FROM online_sessions WHERE conn_key=?',(conn_key,)); c.commit()
                except: pass
                with conn() as c:
                    c.execute('DELETE FROM online_sessions WHERE conn_key=?',(conn_key,)); c.commit()
    def log_message(self,*a):pass
if __name__=='__main__':
    init();threading.Thread(target=poller,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1',PORT),H).serve_forever()
