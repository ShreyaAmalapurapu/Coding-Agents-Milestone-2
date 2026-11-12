import os,sys,json,time,sqlite3,secrets,hashlib,base64,threading,socket,struct,subprocess
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from datetime import datetime,timezone

PORT=int(os.environ.get("PORT","8000")); NODE=PORT-8000
DB="/app/data/cluster.db"; os.makedirs("/app/data",exist_ok=True)
db_init=threading.Lock()
def conn():
 c=sqlite3.connect(DB,timeout=5,isolation_level=None,check_same_thread=False)
 c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA busy_timeout=5000"); return c
with db_init:
 c=conn()
 c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,created_at TEXT,parent_id INTEGER);
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,event_id INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message_id INTEGER,frame TEXT,PRIMARY KEY(channel_id,event_id));""")
 c.close()

def now(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
send_locks={}
send_locks_guard=threading.Lock()
def jsend(sock,obj):
 b=json.dumps(obj,separators=(",",":")).encode()
 frame=b'\x81'+(bytes([len(b)]) if len(b)<126 else b'\x7e'+struct.pack(">H",len(b)))+b
 with send_locks_guard:
  lk=send_locks.setdefault(sock,threading.Lock())
 with lk:
  previous=sock.gettimeout()
  try:
   sock.settimeout(0.5)
   sock.sendall(frame)
  finally:
   try: sock.settimeout(previous)
   except: pass
def recvn(sock,n):
 out=b""
 while len(out)<n:
  part=sock.recv(n-len(out))
  if not part: raise ConnectionError("closed")
  out+=part
 return out
def redis_publish(payload):
 try:
  s=socket.create_connection(("127.0.0.1",6379),.4)
  p=payload.encode(); cmd=b"*3\r\n$4\r\nPUBLISH\r\n$7\r\nhuddle\r\n$"+str(len(p)).encode()+b"\r\n"+p+b"\r\n"; s.sendall(cmd); s.close()
 except: pass

subs=[]; subs_lock=threading.Lock()

def message_obj(c,mid):
 r=c.execute("SELECT m.id,m.channel_id,m.author_id,m.body,m.created_at,m.parent_id,u.username,u.display_name FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?",(mid,)).fetchone()
 if not r:return None
 return {"id":r[0],"channel_id":r[1],"author_id":r[2],"author":{"id":r[2],"username":r[6],"display_name":r[7] or r[6]},"body":r[3],"parent_id":r[5],"reply_count":c.execute("SELECT count(*) FROM messages WHERE parent_id=?",(mid,)).fetchone()[0],"created_at":r[4],"edited_at":None,"files":[],"reactions":[],"mentions":[]}
def emit(cid,mid):
 c=conn()
 try:
  c.execute("BEGIN IMMEDIATE")
  row=c.execute("SELECT event_id FROM counters WHERE channel_id=?",(cid,)).fetchone()
  if not row:
   c.execute("INSERT INTO counters VALUES(?,0)",(cid,)); row=(0,)
  eid=row[0]+1
  c.execute("UPDATE counters SET event_id=? WHERE channel_id=?",(eid,cid))
  m=message_obj(c,mid)
  frame={"type":"message.created","event_id":eid,"channel_id":cid,"message":m}
  c.execute("INSERT INTO events VALUES(?,?,?,?)",(cid,eid,mid,json.dumps(frame)))
  c.execute("COMMIT")
 except Exception:
  try: c.execute("ROLLBACK")
  except: pass
  c.close()
  raise
 c.close()
 redis_publish(json.dumps(frame))

def deliver(frame):
 target=frame["event_id"]; channel=frame["channel_id"]
 with subs_lock:
  current=list(subs)
  for s,cid,last in current:
   if str(cid)!=str(channel) or target<=last:
    continue
   try:
    c=conn()
    rows=c.execute("SELECT frame FROM events WHERE channel_id=? AND event_id>? AND event_id<=? ORDER BY event_id",(cid,last,target)).fetchall()
    c.close()
    latest=last
    for (raw,) in rows:
     f=json.loads(raw)
     if f["event_id"]!=latest+1:
      break
     jsend(s,f); latest=f["event_id"]
    for i,item in enumerate(subs):
     if item[0] is s and item[1]==cid:
      subs[i]=(s,cid,latest); break
   except Exception:
    try:
     subs.remove((s,cid,last))
    except ValueError:
     pass

def watch_socket(sock,cid,head):
 last=int(head)
 while True:
  try:
   c=conn()
   rows=c.execute("SELECT event_id,frame FROM events WHERE channel_id=? ORDER BY event_id",(int(cid),)).fetchall()
   c.close()
   for eid,raw in rows:
    eid=int(eid)
    if eid != last+1:
     continue
    jsend(sock,json.loads(raw))
    last=eid
    with subs_lock:
     for i,x in enumerate(subs):
      if x[0] is sock and int(x[1])==int(cid):
       subs[i]=(sock,cid,last)
       break
  except Exception as e:
   time.sleep(.05)
  time.sleep(.02)

def poller():
 while True:
  time.sleep(0.1)

threading.Thread(target=poller,daemon=True).start()

class H(BaseHTTPRequestHandler):
 protocol_version="HTTP/1.1"
 def log_message(self,*a): pass
 def sendj(self,status,obj):
  b=json.dumps(obj).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
 def auth(self):
  t=self.headers.get("Authorization","").replace("Bearer ",""); c=conn(); r=c.execute("SELECT u.id,u.username,u.display_name FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(t,)).fetchone(); c.close(); return r
 def body(self):
  n=int(self.headers.get("Content-Length","0")); return json.loads(self.rfile.read(n) or b"{}")
 def do_GET(self):
  p=urlparse(self.path); path=p.path
  if path=="/api/health": return self.sendj(200,{"status":"ok","node_id":NODE})
  if path=="/api/ws": return self.ws()
  u=self.auth()
  if not u:return self.sendj(401,{"error":"unauthorized"})
  c=conn()
  if path=="/api/auth/me": return self.sendj(200,{"user":{"id":u[0],"username":u[1],"display_name":u[2] or u[1]}})
  if path=="/api/workspaces":
   rows=c.execute("SELECT w.id,w.slug,w.name,w.owner_id FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u[0],)).fetchall()
   return self.sendj(200,{"workspaces":[{"id":x[0],"slug":x[1],"name":x[2],"owner_id":x[3],"join_mode":"open"} for x in rows]})
  if "/messages" in path:
   cid=int(path.split("/")[3]); rows=c.execute("SELECT id FROM messages WHERE channel_id=? AND parent_id IS NULL ORDER BY id DESC LIMIT 200",(cid,)); return self.sendj(200,{"messages":[message_obj(c,x[0]) for x in rows],"next_cursor":None})
  if path.startswith("/api/workspaces/"):
   slug=path.split("/")[3]; w=c.execute("SELECT id,slug,name,owner_id FROM workspaces WHERE slug=?",(slug,)).fetchone()
   if not w:return self.sendj(404,{"error":"not found"})
   if path.endswith("/members"):
    rows=c.execute("SELECT user_id,role FROM members WHERE workspace_id=?",(w[0],)).fetchall(); return self.sendj(200,{"members":[{"user_id":x[0],"role":x[1]} for x in rows]})
   ch=c.execute("SELECT id,name,is_private,topic FROM channels WHERE workspace_id=?",(w[0],)).fetchall()
   return self.sendj(200,{"workspace":{"id":w[0],"slug":w[1],"name":w[2],"owner_id":w[3],"join_mode":"open"},"channels":[{"id":x[0],"workspace_id":w[0],"name":x[1],"is_private":bool(x[2]),"is_dm":False,"topic":x[3],"is_archived":False} for x in ch],"read_state":{}})
  return self.sendj(404,{"error":"not found"})
 def do_POST(self):
  p=urlparse(self.path); path=p.path
  if path=="/api/auth/register":
   b=self.body(); un=b.get("username",""); pw=b.get("password","")
   if not un.replace("_","").isalnum() or len(pw)<8:return self.sendj(400,{"error":"invalid"})
   c=conn()
   try:
    c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(un,pw,b.get("display_name",un))); uid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,uid)); c.close(); return self.sendj(201,{"user":{"id":uid,"username":un,"display_name":b.get("display_name",un)},"token":t})
   except sqlite3.IntegrityError:return self.sendj(409,{"error":"duplicate"})
  if path=="/api/auth/login":
   b=self.body(); c=conn(); r=c.execute("SELECT id,username,display_name FROM users WHERE username=? AND password=?",(b.get("username"),b.get("password"))).fetchone()
   if not r:return self.sendj(401,{"error":"invalid"})
   t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,r[0])); c.close(); return self.sendj(200,{"user":{"id":r[0],"username":r[1],"display_name":r[2] or r[1]},"token":t})
  u=self.auth()
  if not u:return self.sendj(401,{"error":"unauthorized"})
  b=self.body(); c=conn()
  if path=="/api/workspaces":
   try:
    c.execute("BEGIN IMMEDIATE"); c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(b["slug"],b["name"],u[0])); wid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u[0],"owner")); c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u[0])); c.execute("INSERT INTO counters VALUES(?,0)",(cid,)); c.execute("COMMIT"); return self.sendj(201,{"workspace":{"id":wid,"slug":b["slug"],"name":b["name"],"owner_id":u[0],"join_mode":"open"},"general_channel":{"id":cid,"workspace_id":wid,"name":"general","is_private":False,"is_dm":False,"topic":"","is_archived":False}})
   except sqlite3.IntegrityError:c.execute("ROLLBACK"); return self.sendj(409,{"error":"duplicate"})
  if "/channels" in path and path.startswith("/api/workspaces/"):
   slug=path.split("/")[3]; w=c.execute("SELECT id FROM workspaces WHERE slug=?",(slug,)).fetchone()
   if not w:return self.sendj(404,{"error":"not found"})
   c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w[0],b["name"],int(b.get("is_private",False)),b.get("topic",""))); cid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u[0])); c.execute("INSERT INTO counters VALUES(?,0)",(cid,)); return self.sendj(201,{"channel":{"id":cid,"workspace_id":w[0],"name":b["name"],"is_private":bool(b.get("is_private",False)),"is_dm":False,"topic":b.get("topic",""),"is_archived":False}})
  if path.endswith("/join"):
   cid=int(path.split("/")[3]); c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(cid,u[0])); return self.sendj(200,{"channel_id":cid})
  if path.startswith("/api/channels/") and path.endswith("/messages"):
   cid=int(path.split("/")[3]); text=b.get("body","")
   if not text:return self.sendj(400,{"error":"empty"})
   c.execute("BEGIN IMMEDIATE"); c.execute("INSERT INTO messages(channel_id,author_id,body,created_at,parent_id) VALUES(?,?,?,?,?)",(cid,u[0],text,now(),b.get("parent_id"))); mid=c.execute("SELECT last_insert_rowid()").fetchone()[0]; c.execute("COMMIT"); emit(cid,mid); obj=message_obj(c,mid); c.close(); return self.sendj(201,{"message":obj})
  return self.sendj(404,{"error":"not found"})
 def ws(self):
  u=self.auth_token_q()
  if not u:return self.send_error(401); 
  s=self.connection; key=self.headers.get("Sec-WebSocket-Key"); accept=base64.b64encode(hashlib.sha1((key+"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode(); self.send_response(101); self.send_header("Upgrade","websocket"); self.send_header("Connection","Upgrade"); self.send_header("Sec-WebSocket-Accept",accept); self.end_headers(); s.settimeout(None); cid=None
  while True:
   h=recvn(s,2)
   if not h: break
   opcode=h[0]&15
   n=h[1]&127
   if n==126:n=struct.unpack(">H",recvn(s,2))[0]
   elif n==127:n=struct.unpack(">Q",recvn(s,8))[0]
   mask=recvn(s,4) if h[1]&128 else b"\\x00"*4
   raw=recvn(s,n); data=bytes(x^mask[i%4] for i,x in enumerate(raw))
   if opcode==8:
    with subs_lock:
     subs[:]=[x for x in subs if x[0] is not s]
    break
   if opcode==9:
    with send_locks_guard:
     lk=send_locks.setdefault(s,threading.Lock())
    with lk:
     s.sendall(b"\x8a"+bytes([len(data)])+data)
    continue
   if opcode!=1: continue
   try: q=json.loads(data.decode())
   except: continue
   if q["type"]=="subscribe":
    cid=int(q["channel_id"]); c=conn(); head=c.execute("SELECT event_id FROM counters WHERE channel_id=?",(cid,)).fetchone()[0]; c.close()
    with subs_lock: subs.append((s,cid,head))
    jsend(s,{"type":"subscribed","channel_id":cid,"head_event_id":head})
    threading.Thread(target=watch_socket,args=(s,cid,head),daemon=True).start()
   elif q["type"]=="resume":
    cid=int(q["channel_id"]); c=conn(); rows=c.execute("SELECT frame FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id",(cid,int(q.get("since_event_id",0)))); [jsend(s,json.loads(x[0])) for x in rows]; head=c.execute("SELECT event_id FROM counters WHERE channel_id=?",(cid,)).fetchone()[0]; c.close(); jsend(s,{"type":"resumed","channel_id":cid,"head_event_id":head})
 def auth_token_q(self):
  t=parse_qs(urlparse(self.path).query).get("token",[""])[0]; c=conn(); r=c.execute("SELECT user_id FROM tokens WHERE token=?",(t,)).fetchone(); c.close(); return r
if __name__=="__main__":
 ThreadingHTTPServer(("127.0.0.1",PORT),H).serve_forever()
