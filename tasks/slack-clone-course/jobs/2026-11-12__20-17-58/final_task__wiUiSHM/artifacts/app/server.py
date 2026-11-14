import asyncio, hashlib, json, os, re, secrets, sqlite3, sys, time
from datetime import datetime, timezone
from aiohttp import web

PORT=int(os.environ.get("PORT","8000")); NODE=PORT-8000
DB="/app/data/huddle.sqlite3"; os.makedirs("/app/data",exist_ok=True)
os.makedirs("/app/static",exist_ok=True)
db_lock=asyncio.Lock(); sockets=[]; stop=asyncio.Event()

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00","Z")
def conn():
    c=sqlite3.connect(DB,timeout=5,isolation_level=None)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c
def init():
    c=conn()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT);
    CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
    CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER);
    CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
    CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
    CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,event_id INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message_id INTEGER,kind TEXT,created_at TEXT,PRIMARY KEY(channel_id,event_id));""")
    c.close()
def user_obj(r):
    return {"id":r["id"],"username":r["username"],"display_name":r["display_name"],"timezone":"UTC","avatar_url":"","status_text":"","status_emoji":""}
def msg_obj(c,r):
    a=c.execute("SELECT * FROM users WHERE id=?",(r["author_id"],)).fetchone()
    replies=c.execute("SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0",(r["id"],)).fetchone()["n"]
    return {"id":r["id"],"channel_id":r["channel_id"],"author_id":r["author_id"],"author":user_obj(a),"body":r["body"],"parent_id":r["parent_id"],"reply_count":replies,"created_at":r["created_at"],"edited_at":r["edited_at"],"files":[],"reactions":[],"mentions":[]}
async def body(req):
    try:return await req.json()
    except:return {}
def auth(req,c):
    h=req.headers.get("Authorization","")
    token=(h[7:] if h.startswith("Bearer ") else req.query.get("token",""))
    if not token: raise web.HTTPUnauthorized()
    r=c.execute("SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?",(token,)).fetchone()
    if not r: raise web.HTTPUnauthorized()
    return r
def j(x,status=200): return web.json_response(x,status=status)
async def health(req): return j({"status":"ok","node_id":NODE})
async def register(req):
    b=await body(req); u=b.get("username",""); p=b.get("password","")
    if not re.fullmatch(r"[A-Za-z0-9_]+",u) or len(p)<8: return j({"error":"invalid registration"},400)
    c=conn()
    try:
        c.execute("INSERT INTO users(username,password,display_name) VALUES(?,?,?)",(u,p,b.get("display_name") or u))
    except sqlite3.IntegrityError:return j({"error":"duplicate"},409)
    r=c.execute("SELECT * FROM users WHERE username=?",(u,)).fetchone(); t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,r["id"],)); c.close()
    return j({"user":user_obj(r),"token":t},201)
async def login(req):
    b=await body(req); c=conn(); r=c.execute("SELECT * FROM users WHERE username=? AND password=?",(b.get("username"),b.get("password"))).fetchone()
    if not r:return j({"error":"invalid credentials"},401)
    t=secrets.token_urlsafe(24); c.execute("INSERT INTO tokens VALUES(?,?)",(t,r["id"])); c.close(); return j({"user":user_obj(r),"token":t})
async def me(req):
    c=conn()
    try:r=auth(req,c)
    except web.HTTPException: c.close(); raise
    x=user_obj(r);c.close();return j({"user":x})
async def workspaces(req):
    c=conn()
    try:u=auth(req,c)
    except web.HTTPException:c.close();raise
    rows=c.execute("SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?",(u["id"],)).fetchall()
    c.close();return j({"workspaces":[dict(r) for r in rows]})
async def workspace_create(req):
    c=conn()
    try:u=auth(req,c)
    except web.HTTPException:c.close();raise
    b=await body(req); slug=b.get("slug",""); name=b.get("name","")
    if not re.fullmatch(r"[a-z0-9-]{2,32}",slug) or not name:return j({"error":"invalid"},400)
    try:
        c.execute("BEGIN IMMEDIATE"); c.execute("INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)",(slug,name,u["id"]))
        wid=c.execute("SELECT last_insert_rowid() x").fetchone()["x"]; c.execute("INSERT INTO members VALUES(?,?,?)",(wid,u["id"],"owner")); c.execute("INSERT INTO channels(workspace_id,name) VALUES(?,?)",(wid,"general")); cid=c.execute("SELECT last_insert_rowid() x").fetchone()["x"]; c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"])); c.execute("INSERT INTO counters(channel_id,event_id) VALUES(?,0)",(cid,)); c.execute("COMMIT")
    except sqlite3.IntegrityError:c.execute("ROLLBACK");return j({"error":"duplicate"},409)
    w=c.execute("SELECT * FROM workspaces WHERE id=?",(wid,)).fetchone(); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone();c.close()
    return j({"workspace":dict(w),"general_channel":dict(ch)},201)
async def workspace(req):
    c=conn()
    try:u=auth(req,c)
    except web.HTTPException:c.close();raise
    w=c.execute("SELECT * FROM workspaces WHERE slug=?",(req.match_info["slug"],)).fetchone()
    if not w:return j({"error":"not found"},404)
    mem=c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone()
    if not mem:return j({"error":"forbidden"},403)
    ch=c.execute("SELECT * FROM channels WHERE workspace_id=? AND is_archived=0",(w["id"],)).fetchall()
    ms=c.execute("SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?",(w["id"],)).fetchall()
    c.close();return j({"workspace":dict(w),"channels":[dict(x) for x in ch],"members":[dict(x) for x in ms],"read_state":{}})
async def workspace_members(req):
    c=conn()
    try: auth(req,c)
    except web.HTTPException: c.close(); raise
    w=c.execute("SELECT * FROM workspaces WHERE slug=?",(req.match_info["slug"],)).fetchone()
    if not w: c.close(); return j({"error":"not found"},404)
    rows=c.execute("SELECT m.*,u.username,u.display_name FROM members m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=?",(w["id"],)).fetchall()
    c.close()
    return j({"members":[dict(x) for x in rows]})

async def channels(req):
    c=conn()
    try:u=auth(req,c)
    except web.HTTPException:c.close();raise
    w=c.execute("SELECT * FROM workspaces WHERE slug=?",(req.match_info["slug"],)).fetchone()
    if not w:return j({"error":"not found"},404)
    if not c.execute("SELECT 1 FROM members WHERE workspace_id=? AND user_id=?",(w["id"],u["id"])).fetchone():return j({"error":"forbidden"},403)
    b=await body(req); name=b.get("name","")
    if not re.fullmatch(r"[a-z0-9-]{1,32}",name):return j({"error":"invalid"},400)
    try:
        c.execute("INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)",(w["id"],name,1 if b.get("is_private") else 0,b.get("topic","")))
        cid=c.execute("SELECT last_insert_rowid() x").fetchone()["x"];c.execute("INSERT INTO channel_members VALUES(?,?)",(cid,u["id"]));c.execute("INSERT INTO counters VALUES(?,0)",(cid,))
    except sqlite3.IntegrityError:return j({"error":"duplicate"},409)
    r=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone();c.close();return j({"channel":dict(r)},201)
async def join(req):
    c=conn()
    try:u=auth(req,c)
    except web.HTTPException:c.close();raise
    ch=c.execute("SELECT * FROM channels WHERE id=?",(req.match_info["id"],)).fetchone()
    if not ch:return j({"error":"not found"},404)
    c.execute("INSERT OR IGNORE INTO members(workspace_id,user_id,role) VALUES(?,?,?)",(ch["workspace_id"],u["id"],"member"));c.execute("INSERT OR IGNORE INTO channel_members VALUES(?,?)",(ch["id"],u["id"]));c.close();return j({"channel":dict(ch)})
async def channel_members(req):
    c=conn()
    try:auth(req,c)
    except web.HTTPException:c.close();raise
    rows=c.execute("SELECT u.* FROM channel_members cm JOIN users u ON u.id=cm.user_id WHERE cm.channel_id=?",(req.match_info["id"],)).fetchall();c.close();return j({"members":[user_obj(x) for x in rows]})
async def messages(req):
    c=conn()
    try:u=auth(req,c)
    except web.HTTPException:c.close();raise
    cid=int(req.match_info["id"]); ch=c.execute("SELECT * FROM channels WHERE id=?",(cid,)).fetchone()
    if not ch:return j({"error":"not found"},404)
    if not c.execute("SELECT 1 FROM channel_members WHERE channel_id=? AND user_id=?",(cid,u["id"])).fetchone():return j({"error":"forbidden"},403)
    rows=c.execute("SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT 100",(cid,)).fetchall()
    out=[msg_obj(c,r) for r in rows]
    if req.method=="GET":c.close();return j({"messages":out,"next_cursor":None})
    b=await body(req); text=b.get("body","")
    if not isinstance(text,str) or not text.strip():c.close();return j({"error":"empty"},400)
    async with db_lock:
        c.execute("BEGIN IMMEDIATE"); ts=now();c.execute("INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)",(cid,u["id"],text,b.get("parent_id"),ts)); mid=c.execute("SELECT last_insert_rowid() x").fetchone()["x"];c.execute("UPDATE counters SET event_id=event_id+1 WHERE channel_id=?",(cid,)); eid=c.execute("SELECT event_id FROM counters WHERE channel_id=?",(cid,)).fetchone()["event_id"];c.execute("INSERT INTO events VALUES(?,?,?,?,?)",(cid,eid,mid,"message.created",ts));c.execute("COMMIT")
    r=c.execute("SELECT * FROM messages WHERE id=?",(mid,)).fetchone(); m=msg_obj(c,r);c.close()
    asyncio.create_task(publish({"type":"message.created","event_id":eid,"channel_id":cid,"message":m}))
    return j({"message":m},201)
async def publish(frame):
    try:
        import redis
        rc=redis.Redis(host="127.0.0.1",port=6379,socket_connect_timeout=.2,socket_timeout=.2)
        rc.publish("huddle",json.dumps(frame))
    except Exception: pass
async def deliver(frame):
    for ws in list(sockets):
        if ws["channel"]==int(frame["channel_id"]) and frame["event_id"]>ws["last"]:
            try: await ws["resp"].send_json(frame);ws["last"]=frame["event_id"]
            except Exception:sockets.remove(ws) if ws in sockets else None
async def poller():
    seen={}
    while not stop.is_set():
        try:
            c=conn()
            for ws in list(sockets):
                cid=ws["channel"]; last=ws["last"]; rows=c.execute("SELECT e.*,m.* FROM events e JOIN messages m ON m.id=e.message_id WHERE e.channel_id=? AND e.event_id>? ORDER BY e.event_id",(cid,last)).fetchall()
                for r in rows:
                    if r["event_id"]>ws["last"]:
                        m=msg_obj(c,r); await deliver({"type":r["kind"],"event_id":r["event_id"],"channel_id":cid,"message":m})
            c.close()
        except Exception: pass
        await asyncio.sleep(.06)
async def websocket(req):
    c=conn()
    try:auth(req,c)
    except web.HTTPException:c.close();raise
    ws=web.WebSocketResponse();await ws.prepare(req); item={"resp":ws,"channel":0,"last":0};sockets.append(item)
    try:
        async for x in ws:
            if x.type!=web.WSMsgType.TEXT:continue
            try:b=json.loads(x.data)
            except:continue
            cid=int(b.get("channel_id",0))
            if b.get("type")=="subscribe":
                item["channel"]=cid; item["last"]=int(c.execute("SELECT event_id FROM counters WHERE channel_id=?",(cid,)).fetchone()["event_id"]);await ws.send_json({"type":"subscribed","channel_id":cid,"head_event_id":item["last"]})
            elif b.get("type")=="resume":
                item["channel"]=cid; item["last"]=int(b.get("since_event_id",0)); rows=c.execute("SELECT e.*,m.* FROM events e JOIN messages m ON m.id=e.message_id WHERE e.channel_id=? AND e.event_id>? ORDER BY e.event_id",(cid,item["last"])).fetchall()
                for r in rows: await deliver({"type":r["kind"],"event_id":r["event_id"],"channel_id":cid,"message":msg_obj(c,r)})
                head=c.execute("SELECT event_id FROM counters WHERE channel_id=?",(cid,)).fetchone()["event_id"];await ws.send_json({"type":"resumed","channel_id":cid,"head_event_id":head});item["last"]=head
    finally:
        if item in sockets: sockets.remove(item)
        c.close()
    return ws
def app():
    init()
    a=web.Application()
    a.router.add_get("/api/health",health)
    a.router.add_post("/api/auth/register",register)
    a.router.add_post("/api/auth/login",login)
    a.router.add_get("/api/auth/me",me)
    a.router.add_get("/api/workspaces",workspaces)
    a.router.add_post("/api/workspaces",workspace_create)
    a.router.add_get("/api/workspaces/{slug}",workspace)
    a.router.add_get("/api/workspaces/{slug}/members",workspace_members)
    a.router.add_post("/api/workspaces/{slug}/channels",channels)
    a.router.add_route("*","/api/channels/{id}/messages",messages)
    a.router.add_post("/api/channels/{id}/join",join)
    a.router.add_get("/api/channels/{id}/members",channel_members)
    a.router.add_get("/api/ws",websocket)
    async def startup(app):
        asyncio.create_task(poller())
    a.on_startup.append(startup)
    return a

if __name__=="__main__":web.run_app(app(),host="127.0.0.1",port=PORT)
