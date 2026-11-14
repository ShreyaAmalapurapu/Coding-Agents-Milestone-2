import asyncio, hashlib, json, os, re, secrets, sqlite3, sys
from datetime import datetime, timezone
from aiohttp import web
DB='/app/data/chat.db'; os.makedirs('/app/data/files',exist_ok=True)
NODE=int(os.getenv('NODE_ID','0'))
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def conn():
 c=sqlite3.connect(DB,timeout=20); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=20000'); return c
def init():
 c=conn()
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "");
 CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
 CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
 CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0,UNIQUE(workspace_id,name));
 CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
 CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message TEXT,PRIMARY KEY(channel_id,event_id));
 CREATE TABLE IF NOT EXISTS counters(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
 CREATE TABLE IF NOT EXISTS pins(message_id INTEGER PRIMARY KEY,user_id INTEGER,pinned_at TEXT);
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY AUTOINCREMENT,message TEXT);
 CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY AUTOINCREMENT,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT);
 CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
 CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY AUTOINCREMENT,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,UNIQUE(workspace_id,handle));
 CREATE TABLE IF NOT EXISTS group_members(group_id INTEGER,user_id INTEGER,PRIMARY KEY(group_id,user_id));
 CREATE TABLE IF NOT EXISTS mention_counts(user_id INTEGER,channel_id INTEGER,count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
 CREATE TABLE IF NOT EXISTS online_connections(id TEXT PRIMARY KEY,user_id INTEGER);
 CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,max_uses INTEGER DEFAULT 1,uses INTEGER DEFAULT 0);
 '''); c.commit(); c.close()
init()
try:
 c=conn(); c.execute("ALTER TABLE messages ADD COLUMN mentions TEXT DEFAULT '[]'"); c.commit(); c.close()
except sqlite3.OperationalError:
 pass
sockets={}
def user_obj(r): return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}
def auth(req):
 h=req.headers.get('Authorization','')
 if not h.startswith('Bearer '): return None
 c=conn(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(h[7:],)).fetchone(); c.close(); return r
def out(x,status=200): return web.json_response(x,status=status)
async def body(req):
 try:return await req.json()
 except:return {}
def msg_obj(c,r):
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 replies=c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n']
 rx=[]
 for x in c.execute('SELECT emoji,count(*) n FROM reactions WHERE message_id=? GROUP BY emoji',(r['id'],)):
  ids=[z['user_id'] for z in c.execute('SELECT user_id FROM reactions WHERE message_id=? AND emoji=?',(r['id'],x['emoji']))]
  rx.append({'emoji':x['emoji'],'count':x['n'],'user_ids':ids})
 fs=[]
 for f in c.execute('SELECT f.* FROM files f JOIN attachments a ON a.file_id=f.id WHERE a.message_id=?',(r['id'],)):
  fs.append({k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')})
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':user_obj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':replies,'created_at':r['created_at'],'edited_at':r['edited_at'],'files':fs,'reactions':rx,'mentions':json.loads(r['mentions']) if 'mentions' in r.keys() and r['mentions'] else []}
async def health(req): return out({'status':'ok','node_id':NODE})
async def register(req):
 b=await body(req); u=b.get('username',''); p=b.get('password','')
 if not re.fullmatch(r'[A-Za-z0-9_]+',u) or len(p)<8:return out({'error':'invalid registration'},400)
 c=conn()
 try:
  cur=c.execute('INSERT INTO users(username,password,display_name) VALUES(?,?,?)',(u,p,b.get('display_name') or u)); uid=cur.lastrowid
  t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit()
 except sqlite3.IntegrityError:return out({'error':'duplicate username'},409)
 r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); c.close(); return out({'user':user_obj(r),'token':t},201)
async def login(req):
 b=await body(req); c=conn(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(b.get('username'),b.get('password'))).fetchone()
 if not r:c.close(); return out({'error':'invalid credentials'},401)
 t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id'])); c.commit(); c.close(); return out({'user':user_obj(r),'token':t})
async def me(req):
 r=auth(req); return out({'user':user_obj(r)}) if r else out({'error':'unauthorized'},401)
def channel_obj(r):
 d=dict(r)
 for k in ('is_private','is_dm','is_archived'): d[k]=bool(d[k])
 return d

def workspace(c,wid):
 r=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()
 return dict(r) if r else None
async def workspaces(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 c=conn(); rows=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall(); c.close()
 return out({'workspaces':[dict(x) for x in rows]})
async def create_workspace(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 b=await body(req); slug=b.get('slug','')
 if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):return out({'error':'invalid slug'},400)
 c=conn()
 try:
  cur=c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,b.get('name',slug),u['id'])); wid=cur.lastrowid
  c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner')); cur=c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general')); cid=cur.lastrowid; c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id'])); c.commit()
 except sqlite3.IntegrityError:c.close();return out({'error':'duplicate'},409)
 w=dict(c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone()); ch=channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()); c.close()
 return out({'workspace':w,'general_channel':ch},201)
async def workspace(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 c=conn(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return out({'error':'not found'},404)
 mem=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not mem:return out({'error':'forbidden'},403)
 inc=req.query.get('include_archived') in ('1','true'); cs=[channel_obj(x) for x in c.execute('SELECT * FROM channels WHERE workspace_id=? AND (? OR is_archived=0)',(w['id'],int(inc)))]; c.close()
 return out({'workspace':dict(w),'channels':cs,'read_state':{}})
async def channels(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 slug=req.match_info['slug']; b=await body(req); name=b.get('name',''); topic=b.get('topic','')
 if not re.fullmatch(r'[a-z0-9-]{1,32}',name) or len(topic)>250:return out({'error':'invalid channel'},400)
 c=conn(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
 if not w:return out({'error':'not found'},404)
 m=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not m:return out({'error':'forbidden'},403)
 if b.get('is_private') and m['role'] not in ('owner','admin'):return out({'error':'forbidden'},403)
 try:
  cur=c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],b.get('name',''),int(bool(b.get('is_private'))),b.get('topic',''))); cid=cur.lastrowid;c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,u['id']));c.commit()
 except sqlite3.IntegrityError:return out({'error':'duplicate'},409)
 r=channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone());c.close();return out({'channel':r},201)
async def update_channel(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    b=await body(req); topic=b.get('topic')
    if not isinstance(topic,str) or len(topic)>250:return out({'error':'invalid topic'},400)
    c=conn(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return out({'error':'not found'},404)
    member=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u['id'])).fetchone()
    owner=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=? AND role="owner"',(ch['workspace_id'],u['id'])).fetchone()
    if not member and not owner:c.close();return out({'error':'forbidden'},403)
    if ch['is_archived']:c.close();return out({'error':'archived'},423)
    c.execute('UPDATE channels SET topic=? WHERE id=?',(topic,ch['id']))
    fresh=channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone())
    frame=event(c,ch['id'],{'type':'channel.updated','channel_id':ch['id'],'channel':fresh});c.commit();c.close();await broadcast(frame)
    return out({'channel':fresh})
async def join(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 c=conn(); ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
 if not ch:return out({'error':'not found'},404)
 m=c.execute('SELECT * FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()
 if not m:
  w=c.execute('SELECT * FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
  if w['join_mode']!='open' or ch['is_private']:c.close();return out({'error':'forbidden'},403)
  c.execute('INSERT INTO members VALUES(?,?,?)',(ch['workspace_id'],u['id'],'member'))
 c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],u['id']));c.commit();r=dict(ch);c.close();return out({'channel':r})
def event(c,cid,frame):
 n=c.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone()
 if not n:c.execute('INSERT INTO counters VALUES(?,0)',(cid,)); n={'n':0}
 eid=n['n']+1;c.execute('UPDATE counters SET n=? WHERE channel_id=?',(eid,cid));frame['event_id']=eid;c.execute('INSERT INTO events VALUES(?,?,?)',(cid,eid,json.dumps(frame)));return frame
async def messages(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    try: limit=int(req.query.get('limit','50'))
    except: return out({'error':'invalid limit'},400)
    if limit<1 or limit>200:return out({'error':'invalid limit'},400)
    cur=req.query.get('cursor'); offset=0
    if cur:
        try: offset=int(cur)
        except: return out({'error':'invalid cursor'},400)
    c=conn();cid=int(req.match_info['id'])
    if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
    rows=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC LIMIT ? OFFSET ?',(cid,limit+1,offset)).fetchall()
    more=len(rows)>limit; rows=rows[:limit]; z=[msg_obj(c,x) for x in rows];c.close()
    return out({'messages':z,'next_cursor':str(offset+limit) if more else None})
async def slash(req,u,c,ch,b):
    raw=b.get('body','')
    if not raw.startswith('/') or raw.startswith('//'): return None
    parts=raw.split(' ',1); cmd=parts[0]; text=parts[1].strip() if len(parts)>1 else ''
    if cmd=='/me':
        return out({'error':'text required'},400) if not text else None
    if cmd=='/shrug':
        b['body']=(text+' ' if text else '')+'¯\\_(ツ)_/¯'; return None
    if cmd in ('/topic','/invite','/archive','/unarchive'):
        return None
    return out({'error':'unknown command'},400)
async def post(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 b=await body(req); bodyv=b.get('body','')
 if not bodyv:return out({'error':'empty body'},400)
 c=conn();cid=int(req.match_info['id']);ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone();ok=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
 if not ch:return out({'error':'not found'},404)
 if not ok:c.close();return out({'error':'forbidden'},403)
 mentions=resolve_mentions(c,ch,u['id'],bodyv)
 for mid in mentions:
  c.execute('INSERT INTO mention_counts(user_id,channel_id,count) VALUES(?,?,1) ON CONFLICT(user_id,channel_id) DO UPDATE SET count=count+1',(mid,ch['id']))
 if ch['is_archived'] and not (b.get('body','').startswith('/unarchive')):c.close();return out({'error':'archived'},423)
 side=await slash(req,u,c,ch,b)
 if side is not None:c.close();return side
 bodyv=b.get('body','')
 fids=b.get('file_ids',[])
 if not isinstance(fids,list): c.close(); return out({'error':'invalid files'},400)
 for fid in fids:
  f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
  if not f or f['uploader_id']!=u['id'] or c.execute('SELECT 1 FROM attachments WHERE file_id=?',(fid,)).fetchone(): c.close(); return out({'error':'invalid file'},400)
 t=now();cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(cid,u['id'],bodyv,b.get('parent_id'),t,json.dumps(mentions)));mid=cur.lastrowid
 for fid in fids: c.execute('INSERT INTO attachments VALUES(?,?)',(mid,fid))
 r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); mo=msg_obj(c,r); kind='message.reply' if b.get('parent_id') else 'message.created'; frame=event(c,cid,{'type':kind,'channel_id':cid,'message':mo});c.commit();c.close();await broadcast(frame);return out({'message':mo},201)
async def edit(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    b=await body(req); text=b.get('body')
    if not isinstance(text,str) or not text.strip():return out({'error':'empty body'},400)
    c=conn(); r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return out({'error':'not found'},404)
    if r['deleted']:
        c.close();return out({'error':'deleted'},409)
    if r['author_id']!=u['id']:
        c.close();return out({'error':'forbidden'},403)
    c.execute('UPDATE messages SET body=?,edited_at=? WHERE id=?',(text,now(),r['id']))
    x=c.execute('SELECT * FROM messages WHERE id=?',(r['id'],)).fetchone()
    mo=msg_obj(c,x)
    frame=event(c,r['channel_id'],{'type':'message.updated','channel_id':r['channel_id'],'message':mo})
    c.commit();c.close();await broadcast(frame)
    return out({'message':mo})

async def delete(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn();r=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not r:c.close();return out({'error':'not found'},404)
    if r['deleted']:
        c.close();return out({'deleted':True})
    allowed=r['author_id']==u['id']
    if not allowed:
        owner=c.execute('SELECT 1 FROM members m JOIN channels ch ON ch.workspace_id=m.workspace_id WHERE ch.id=? AND m.user_id=? AND m.role=?',(r['channel_id'],u['id'],'owner')).fetchone()
        allowed=bool(owner)
    if not allowed:
        c.close();return out({'error':'forbidden'},403)
    c.execute('UPDATE messages SET deleted=1 WHERE id=?',(r['id'],))
    c.execute('DELETE FROM reactions WHERE message_id=?',(r['id'],))
    frame=event(c,r['channel_id'],{'type':'message.deleted','channel_id':r['channel_id'],'message_id':r['id']})
    c.commit();c.close();await broadcast(frame)
    return out({'deleted':True})
async def broadcast(frame):
    return

async def ws_handler(req):
    u=auth(req)
    if not u:
        tok=req.query.get('token','')
        c0=conn()
        u=c0.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(tok,)).fetchone()
        c0.close()
    if not u:
        return web.Response(status=401)
    ws=web.WebSocketResponse()
    await ws.prepare(req)
    state={'channels':set(),'sent':{},'global':0}
    state['user_id']=u['id']; state['conn_id']=secrets.token_hex(12)
    c_on=conn(); c_on.execute('INSERT INTO online_connections VALUES(?,?)',(state['conn_id'],u['id'])); c_on.commit(); c_on.close()
    sockets[ws]=state
    async def poll():
        while not ws.closed:
            try:
                c0=conn()
                grows=c0.execute('SELECT id,message FROM global_events WHERE id>? ORDER BY id',(state['global'],)).fetchall()
                c0.close()
                for grow in grows:
                    await ws.send_str(grow['message']); state['global']=grow['id']
                for cid in list(state['channels']):
                    c=conn()
                    rows=c.execute('SELECT event_id,message FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,state['sent'].get(cid,0))).fetchall()
                    c.close()
                    for row in rows:
                        await ws.send_str(row['message'])
                        state['sent'][cid]=row['event_id']
                await asyncio.sleep(0.05)
            except Exception:
                await asyncio.sleep(0.1)
    task=asyncio.create_task(poll())
    try:
        async for x in ws:
            if x.type!=web.WSMsgType.TEXT:
                continue
            try: d=json.loads(x.data)
            except: continue
            if d.get('type') not in ('subscribe','resume'):
                continue
            try: cid=int(d.get('channel_id'))
            except: continue
            c=conn()
            headrow=c.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone()
            head=headrow['n'] if headrow else 0
            state['channels'].add(cid)
            if d.get('type')=='resume':
                since=int(d.get('since_event_id',0))
                rows=c.execute('SELECT event_id,message FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,since)).fetchall()
                for row in rows:
                    await ws.send_str(row['message'])
                state['sent'][cid]=head
                await ws.send_json({'type':'resumed','channel_id':cid,'head_event_id':head})
            else:
                state['sent'][cid]=head
                await ws.send_json({'type':'subscribed','channel_id':cid,'head_event_id':head})
            c.close()
    finally:
        task.cancel()
        c_on=conn(); c_on.execute('DELETE FROM online_connections WHERE id=?',(state['conn_id'],)); c_on.commit(); c_on.close()
        sockets.pop(ws,None)
    return ws


async def get_user(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn(); r=c.execute('SELECT * FROM users WHERE id=?',(req.match_info['id'],)).fetchone(); c.close()
    return out({'user':user_obj(r)}) if r else out({'error':'not found'},404)

async def update_me(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    b=await body(req); allowed={'display_name':(1,80),'status_text':(0,100),'status_emoji':(0,64),'avatar_url':(0,2048)}
    try:
        for k,v in b.items():
            if k=='timezone':
                from zoneinfo import ZoneInfo
                ZoneInfo(v)
            elif k in allowed:
                if not isinstance(v,str) or not (allowed[k][0]<=len(v)<=allowed[k][1]): raise ValueError()
            else: continue
    except: return out({'error':'invalid profile'},400)
    c=conn()
    for k,v in b.items():
        if k in allowed or k=='timezone': c.execute('UPDATE users SET '+k+'=? WHERE id=?',(v,u['id']))
    c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone(); c.close()
    await broadcast_global({'type':'user.updated','user':user_obj(r)})
    return out({'user':user_obj(r)})

async def broadcast_global(frame):
    c=conn(); c.execute('INSERT INTO global_events(message) VALUES(?)',(json.dumps(frame),)); c.commit(); c.close()
    for ws,state in list(sockets.items()):
        try: await ws.send_json(frame)
        except: sockets.pop(ws,None)

async def open_dm(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    b=await body(req)
    if not b.get('user_id'):return out({'error':'user_id required'},400)
    other=int(b['user_id']); c=conn(); r=c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone()
    if not r:c.close();return out({'error':'not found'},404)
    a,bid=sorted((u['id'],other)); name=f'dm-{a}-{bid}'
    ch=c.execute('SELECT * FROM channels WHERE name=? AND is_dm=1',(name,)).fetchone()
    if not ch:
        cur=c.execute('INSERT INTO channels(workspace_id,name,is_dm,is_private) VALUES(NULL,?,1,1)',(name,)); cid=cur.lastrowid
        c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,a)); c.execute('INSERT INTO cmembers VALUES(?,?)',(cid,bid)); c.commit()
    else: cid=ch['id']
    c.close(); return out({'channel_id':cid})


async def channel_members(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn(); cid=int(req.match_info['id']); ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:return out({'error':'not found'},404)
    access=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone()
    if not access:return out({'error':'forbidden'},403)
    rows=c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(cid,)).fetchall();c.close()
    return out({'members':[user_obj(x) for x in rows]})

async def add_reaction(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    b=await body(req); emoji=b.get('emoji')
    if not isinstance(emoji,str) or not emoji:return out({'error':'invalid emoji'},400)
    c=conn(); r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
    if not r:c.close();return out({'error':'not found'},404)
    if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
    exists=c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji)).fetchone()
    if not exists:
        c.execute('INSERT INTO reactions VALUES(?,?,?)',(r['id'],u['id'],emoji))
        frame=event(c,r['channel_id'],{'type':'reaction.added','channel_id':r['channel_id'],'message_id':r['id'],'emoji':emoji,'user_id':u['id']});c.commit()
    else:c.commit();frame=None
    rx=msg_obj(c,r)['reactions'];c.close()
    if frame:await broadcast(frame)
    return out({'reactions':rx})

async def remove_reaction(req):
    u=auth(req); 
    if not u:return out({'error':'unauthorized'},401)
    emoji=req.match_info['emoji'];c=conn();r=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
    if not r:c.close();return out({'error':'not found'},404)
    if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(r['channel_id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
    cur=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(r['id'],u['id'],emoji))
    frame=None
    if cur.rowcount:
        frame=event(c,r['channel_id'],{'type':'reaction.removed','channel_id':r['channel_id'],'message_id':r['id'],'emoji':emoji,'user_id':u['id']})
    c.commit();rx=msg_obj(c,r)['reactions'];c.close()
    if frame:await broadcast(frame)
    return out({'reactions':rx})

async def upload_file(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 try: reader=await req.multipart()
 except: return out({'error':'bad multipart'},400)
 part=None
 async for x in reader:
  if x.name=='file': part=x; break
 if part is None:return out({'error':'file required'},400)
 data=await part.read()
 if len(data)>10485760:return out({'error':'too large'},413)
 fid=None;c=conn(); t=now(); fn=part.filename or 'file'; ct=part.headers.get('Content-Type','application/octet-stream')
 cur=c.execute('INSERT INTO files(uploader_id,filename,content_type,size,created_at,path) VALUES(?,?,?,?,?,?)',(u['id'],fn,ct,len(data),t,''))
 fid=cur.lastrowid; path=f'/app/data/files/{fid}'; open(path,'wb').write(data)
 c.execute('UPDATE files SET path=? WHERE id=?',(path,fid));c.commit();c.close()
 return out({'file':{'id':fid,'uploader_id':u['id'],'filename':fn,'content_type':ct,'size':len(data),'created_at':t}},201)
async def file_meta(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 c=conn();f=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 if not f:return out({'error':'not found'},404)
 return out({'file':{k:f[k] for k in ('id','uploader_id','filename','content_type','size','created_at')}})
async def file_download(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 c=conn();f=c.execute('SELECT * FROM files WHERE id=?',(req.match_info['id'],)).fetchone();c.close()
 if not f:return out({'error':'not found'},404)
 return web.Response(body=open(f['path'],'rb').read(),content_type=f['content_type'],headers={'Content-Disposition':f'attachment; filename="{f["filename"]}"'})

def resolve_mentions(c,ch,uid,text):
    ids=set()
    members={x['user_id'] for x in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(ch['id'],))}
    wsids={x['user_id'] for x in c.execute('SELECT user_id FROM members WHERE workspace_id=?',(ch['workspace_id'],))} if ch['workspace_id'] else set()
    for m in re.finditer(r'(?<![A-Za-z0-9_])@([A-Za-z0-9_-]+)',text):
        word=m.group(1)
        if word=='channel': ids.update(members)
        elif word=='here':
            ids.update(members & {x['user_id'] for x in c.execute('SELECT DISTINCT user_id FROM online_connections')})
        else:
            u=c.execute('SELECT id FROM users WHERE username=?',(word,)).fetchone()
            if u and u['id'] in wsids: ids.add(u['id'])
            g=c.execute('SELECT id FROM groups WHERE workspace_id=? AND handle=?',(ch['workspace_id'],word)).fetchone()
            if g: ids.update(x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],)) if x['user_id'] in members)
    ids.discard(uid); return sorted(ids)

def group_obj(c,g):
    return {'id':g['id'],'handle':g['handle'],'name':g['name'],'member_user_ids':[x['user_id'] for x in c.execute('SELECT user_id FROM group_members WHERE group_id=?',(g['id'],))]}

async def groups(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return out({'error':'not found'},404)
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():
        c.close();return out({'error':'forbidden'},403)
    if req.method=='GET':
        z=[group_obj(c,x) for x in c.execute('SELECT * FROM groups WHERE workspace_id=?',(w['id'],))]
        c.close();return out({'groups':z})
    b=await body(req);h=b.get('handle','')
    if not re.fullmatch(r'[a-z0-9_-]{2,32}',h):
        c.close();return out({'error':'invalid handle'},400)
    try:
        cur=c.execute('INSERT INTO groups(workspace_id,handle,name,creator_id) VALUES(?,?,?,?)',(w['id'],h,b.get('name',''),u['id']))
        gid=cur.lastrowid
        for uid in b.get('user_ids',[]): c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(gid,uid))
        c.commit();g=c.execute('SELECT * FROM groups WHERE id=?',(gid,)).fetchone();z=group_obj(c,g);c.close()
        return out({'group':z},201)
    except sqlite3.IntegrityError:
        c.rollback();c.close();return out({'error':'duplicate'},409)

async def group_change(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
    if not w:c.close();return out({'error':'not found'},404)
    g=c.execute('SELECT * FROM groups WHERE workspace_id=? AND handle=?',(w['id'],req.match_info['handle'])).fetchone()
    if not g:
        c.close()
        return out({'deleted':True}) if req.method=='DELETE' else out({'error':'not found'},404)
    role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
    if not role:c.close();return out({'error':'forbidden'},403)
    if g['creator_id']!=u['id'] and role['role']!='owner':c.close();return out({'error':'forbidden'},403)
    if req.method=='DELETE':
        c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],));c.execute('DELETE FROM groups WHERE id=?',(g['id'],));c.commit();c.close();return out({'deleted':True})
    b=await body(req);c.execute('UPDATE groups SET name=? WHERE id=?',(b.get('name',g['name']),g['id']))
    if 'user_ids' in b:
        c.execute('DELETE FROM group_members WHERE group_id=?',(g['id'],))
        for uid in b['user_ids']:c.execute('INSERT OR IGNORE INTO group_members VALUES(?,?)',(g['id'],uid))
    c.commit();z=group_obj(c,c.execute('SELECT * FROM groups WHERE id=?',(g['id'],)).fetchone());c.close();return out({'group':z})

async def read_state(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    cid=int(req.match_info['id']); c=conn()
    if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():
        c.close();return out({'error':'forbidden'},403)
    row=c.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone(); head=row['n'] if row else 0
    mc=c.execute('SELECT count FROM mention_counts WHERE user_id=? AND channel_id=?',(u['id'],cid)).fetchone()
    c.close()
    return out({'read_state':{'channel_id':cid,'last_read_event_id':head,'unread_count':0,'mention_count':mc['count'] if mc else 0}})
async def replies(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn(); parent=c.execute('SELECT * FROM messages WHERE id=? AND deleted=0',(req.match_info['id'],)).fetchone()
    if not parent:c.close();return out({'error':'not found'},404)
    if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(parent['channel_id'],u['id'])).fetchone():
        c.close();return out({'error':'forbidden'},403)
    rows=c.execute('SELECT * FROM messages WHERE parent_id=? AND deleted=0 ORDER BY id',(parent['id'],)).fetchall()
    z=[msg_obj(c,x) for x in rows];c.close();return out({'replies':z,'next_cursor':None})

async def ws_members(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 c=conn();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return out({'error':'not found'},404)
 if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
 z=[dict(x) for x in c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],))];c.close();return out({'members':z})
async def invitations(req):
 u=auth(req)
 if not u:return out({'error':'unauthorized'},401)
 c=conn();w=c.execute('SELECT * FROM workspaces WHERE slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return out({'error':'not found'},404)
 role=c.execute('SELECT role FROM members WHERE workspace_id=? AND user_id=?',(w['id'],u['id'])).fetchone()
 if not role or role['role'] not in ('owner','admin'):c.close();return out({'error':'forbidden'},403)
 if req.method=='GET':
  z=[dict(x) for x in c.execute('SELECT code,workspace_id,max_uses,uses FROM invitations WHERE workspace_id=?',(w['id'],))];c.close();return out({'invitations':z})
 code=secrets.token_urlsafe(10);c.execute('INSERT INTO invitations(code,workspace_id) VALUES(?,?)',(code,w['id']));c.commit();c.close();return out({'invitation':{'code':code,'workspace_id':w['id'],'max_uses':1,'uses':0}},201)
async def accept_inv(req):
 u=auth(req); 
 if not u:return out({'error':'unauthorized'},401)
 c=conn();x=c.execute('SELECT * FROM invitations WHERE code=?',(req.match_info['code'],)).fetchone()
 if not x or x['uses']>=x['max_uses']:c.close();return out({'error':'not found'},404)
 c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(x['workspace_id'],u['id'],'member'));c.execute('UPDATE invitations SET uses=uses+1 WHERE code=?',(x['code'],));c.commit();c.close();return out({'workspace_id':x['workspace_id']})

async def leave_channel(req):
    u=auth(req); 
    if not u:return out({'error':'unauthorized'},401)
    c=conn();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return out({'error':'not found'},404)
    c.execute('DELETE FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],u['id']));c.commit();z=channel_obj(ch);c.close();return out({'channel':z})
async def archive_channel(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn();ch=c.execute('SELECT * FROM channels WHERE id=?',(req.match_info['id'],)).fetchone()
    if not ch:c.close();return out({'error':'not found'},404)
    if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=? AND role="owner"',(ch['workspace_id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
    val=1 if req.path.endswith('/archive') else 0;c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,ch['id']));c.commit();z=channel_obj(c.execute('SELECT * FROM channels WHERE id=?',(ch['id'],)).fetchone());c.close();return out({'channel':z})
async def pins(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn();cid=int(req.match_info['id']);ch=c.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
    if not ch:c.close();return out({'error':'not found'},404)
    if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(cid,u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
    z=[]
    for x in c.execute('SELECT p.*,m.* FROM pins p JOIN messages m ON m.id=p.message_id WHERE m.channel_id=? ORDER BY p.pinned_at DESC',(cid,)):
        z.append({'message':msg_obj(c,x),'pinned_by':x['user_id'],'pinned_at':x['pinned_at']})
    c.close();return out({'pins':z,'next_cursor':None})
async def pin_message(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn();m=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not m:c.close();return out({'error':'not found'},404)
    if not c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(m['channel_id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
    t=now();c.execute('INSERT OR IGNORE INTO pins VALUES(?,?,?)',(m['id'],u['id'],t));c.commit();x=c.execute('SELECT * FROM pins WHERE message_id=?',(m['id'],)).fetchone();c.close();return out({'pin':{'message_id':m['id'],'pinned_by':x['user_id'],'pinned_at':x['pinned_at']}})
async def unpin_message(req):
    u=auth(req)
    if not u:return out({'error':'unauthorized'},401)
    c=conn();m=c.execute('SELECT * FROM messages WHERE id=?',(req.match_info['id'],)).fetchone()
    if not m:c.close();return out({'error':'not found'},404)
    c.execute('DELETE FROM pins WHERE message_id=?',(m['id'],));c.commit();c.close();return out({'unpinned':True})
async def search(req):
    u=auth(req); 
    if not u:return out({'error':'unauthorized'},401)
    q=req.query.get('q','');c=conn();z=[]
    for m in c.execute('SELECT m.* FROM messages m JOIN cmembers cm ON cm.channel_id=m.channel_id AND cm.user_id=? WHERE m.deleted=0 AND m.body LIKE ? ORDER BY m.id DESC',(u['id'],'%'+q+'%')):z.append({'message':msg_obj(c,m)})
    c.close();return out({'results':z,'next_cursor':None})
async def root(req):
 return web.Response(text=HTML,content_type='text/html')
HTML='<!doctype html><html><head><meta charset="utf-8"><title>Huddle</title><style>body{margin:0;font:15px Arial;display:flex;height:100vh}.sidebar{width:240px;background:#3f0e40;color:#fff;padding:16px}.main{flex:1;padding:24px}.thread{width:300px;border-left:1px solid #ddd;padding:16px;display:none;position:fixed;right:0;top:0;height:100vh;background:#fff;z-index:5}.main{position:relative}.toolbar{display:block}.toolbar button{background:#1264a3;color:#fff}button{border:0;padding:9px;margin:4px;color:#fff}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}input,select{padding:8px;margin:4px}.modal{display:none}.row{padding:10px;border-bottom:1px solid #ddd}#message-list{width:calc(100% - 340px)}</style></head><body><aside class="sidebar"><div data-testid="workspace-header" id="workspace-header">Huddle</div><div data-testid="current-user" id="current-user"></div><div data-testid="channel-list" id="channel-list"></div><div data-testid="dms-list" id="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button data-testid="logout-btn" data-button-role="danger">Logout</button></aside><main class="main"><div id="auth-modal"><form data-testid="auth-form" id="auth-form" data-mode="register"><input name="username"><input name="password" type="password"><button data-testid="auth-submit" data-button-role="primary">Continue</button><button type="button" data-testid="auth-toggle">Login</button></form></div><div id="empty-state-create-workspace" data-testid="empty-state-create-workspace"><form data-testid="workspace-create-form"><input name="name"><input name="slug"><button type="submit" data-testid="workspace-general-submit" data-button-role="primary">Create</button></form><form data-testid="join-workspace-form"><input name="code"><button type="submit" data-testid="join-workspace-submit" data-button-role="primary">Join</button><div data-testid="join-workspace-error"></div></form></div><section id="app"><h2 data-testid="channel-title" id="channel-title"></h2><div data-testid="channel-topic" id="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary">Settings</button><div data-testid="message-list" id="message-list">No messages yet. Say hello!</div><form><input data-testid="message-input" id="message-input"><button data-testid="send-btn" data-button-role="primary">Send</button></form></section><aside data-testid="thread-panel" class="thread"><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button></aside><div id="create-channel-modal" class="modal"><form data-testid="create-channel-form"><input name="name"><input data-testid="create-channel-private" type="checkbox"><button type="submit" data-testid="create-channel-submit" data-button-role="primary">Create</button><button data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button></form></div><div id="channel-settings-modal" class="modal"><h3 data-testid="channel-settings-title">Channel settings</h3><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary">Add</button></form><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Save</button><button data-testid="archive-channel-btn" data-button-role="danger">Archive</button><button data-testid="unarchive-channel-btn" data-button-role="secondary">Unarchive</button><button data-testid="channel-settings-close" data-button-role="secondary">Close</button></div><div id="workspace-settings-modal" class="modal"><button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><button data-testid="settings-tab-general">General</button><div data-testid="settings-pane-members"><div data-testid="member-row"><span data-testid="role-badge">owner</span><select data-testid="role-select"></select></div></div><div data-testid="settings-pane-invitations"><button data-testid="create-invitation-btn" data-button-role="secondary">Invite</button><form id="create-invitation-form" data-testid="create-invitation-form"><button type="submit" data-testid="create-invitation-submit" data-button-role="primary">Create</button></form><div data-testid="invitations-list"></div></div><div data-testid="settings-pane-general"><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select></div><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button></div></main><script>\nconst $=s=>document.querySelector(s);let token=localStorage.getItem(\'huddle.token\'), current=null, chan=null, sock=null;\nasync function api(path,opt={}){opt.headers={...(opt.headers||{}),Authorization:\'Bearer \'+token,\'Content-Type\':\'application/json\'};let r=await fetch(\'/api\'+path,opt);let j=await r.json();if(!r.ok)throw Error(j.error||\'Request failed\');return j}\nfunction alertAuth(t){let a=$(\'#auth-error\');if(!a){a=document.createElement(\'div\');a.setAttribute(\'role\',\'alert\');$(\'#auth-form\').append(a)}a.textContent=t}\nasync function boot(){if(!token)return;try{let x=await api(\'/auth/me\');current=x.user;$(\'#current-user\').textContent=current.username;$(\'#auth-modal\').style.display=\'none\';let w=await api(\'/workspaces\');if(w.workspaces[0]){ $(\'#empty-state-create-workspace\').style.display=\'none\'; await loadWorkspace(w.workspaces[0].slug); }}catch(e){console.error(e);localStorage.removeItem(\'huddle.token\');token=null;$(\'#auth-modal\').style.display=\'block\';}}\nasync function loadWorkspace(slug){let x=await api(\'/workspaces/\'+slug);$(\'#workspace-header\').textContent=x.workspace.name;$(\'#channel-list\').innerHTML=x.channels.map(c=>`<div data-testid="channel-entry" data-channel-id="${c.id}" data-channel-name="${c.name}" onclick="openChannel(${c.id},\'${c.name}\',${JSON.stringify(c.topic||\'\')})">#${c.name}</div>`).join(\'\');if(x.channels[0])openChannel(x.channels[0].id,x.channels[0].name,x.channels[0].topic||\'\')}\nasync function openChannel(id,name,topic=\'\'){chan=id;$(\'#channel-title\').textContent=name;$(\'#channel-topic\').textContent=topic;let x=await api(\'/channels/\'+id+\'/messages\');renderMessages(x.messages.slice().reverse());if(sock)sock.close();sock=new WebSocket((location.protocol===\'https:\'?\'wss\':\'ws\')+\'://\'+location.host+\'/api/ws?token=\'+encodeURIComponent(token));sock.onopen=()=>sock.send(JSON.stringify({type:\'subscribe\',channel_id:id}));sock.onmessage=e=>{let f=JSON.parse(e.data);if(f.type===\'message.created\'&&f.channel_id==chan){let ms=document.querySelectorAll(\'[data-testid=message]\');if(![...ms].some(x=>x.dataset.id==f.message.id))addMessage(f.message)}}}\nfunction renderMessages(ms){$(\'#message-list\').innerHTML=\'\';if(!ms.length){$(\'#message-list\').textContent=\'No messages yet. Say hello!\';return}ms.forEach(addMessage)}\nfunction addMessage(m){\n let d=document.createElement(\'div\');d.dataset.testid=\'message\';d.dataset.id=m.id;\n d.innerHTML=\'<span data-testid="message-avatar" class="message-avatar">\'+(m.author.display_name||m.author.username).charAt(0)+\'</span><b data-testid="message-author" class="message-author">\'+(m.author.display_name||m.author.username)+\'</b> <span data-testid="message-time" class="message-time">\'+new Date(m.created_at).toLocaleTimeString()+\'</span><div data-testid="message-body" class="message-body"></div><span data-testid="edited-indicator" class="edited-indicator">\'+(m.edited_at?\' (edited)\':\'\')+\'</span><span data-testid="reply-count" class="reply-count">\'+(m.reply_count?(m.reply_count+\' repl\'+(m.reply_count==1?\'y\':\'ies\')):\'\')+\'</span><div class="toolbar"><button data-testid="edit-message-btn">Edit</button><button data-testid="delete-message-btn">Delete</button><button data-testid="open-thread-btn">Thread</button><button data-testid="reaction-button">React</button></div><div class="reactions"></div>\';\n d.querySelector(\'[data-testid="message-body"]\').textContent=m.body;\n let edit=d.querySelector(\'[data-testid="edit-message-btn"]\');edit.onclick=()=>{let body=d.querySelector(\'[data-testid="message-body"]\');if(!body){body=document.createElement(\'div\');body.dataset.testid=\'message-body\';body.className=\'message-body\';body.textContent=m.body;d.append(body)}let i=document.createElement(\'input\');i.dataset.testid=\'edit-message-input\';i.value=body.textContent;body.replaceWith(i);i.focus();i.onkeydown=async e=>{if(e.key===\'Enter\'){let x=await api(\'/messages/\'+m.id,{method:\'PATCH\',body:JSON.stringify({body:i.value})});let nb=document.createElement(\'div\');nb.dataset.testid=\'message-body\';nb.className=\'message-body\';nb.textContent=x.message.body;i.replaceWith(nb);d.querySelector(\'[data-testid="edited-indicator"]\').textContent=\' (edited)\';}if(e.key===\'Escape\')i.replaceWith(body)}};\n d.querySelector(\'[data-testid="delete-message-btn"]\').onclick=async()=>{await api(\'/messages/\'+m.id,{method:\'DELETE\'});d.remove()};\n d.querySelector(\'[data-testid="open-thread-btn"]\').onclick=async()=>{let p=document.querySelector(\'[data-testid="thread-panel"]\');p.style.display=\'block\';p.style.position=\'fixed\';p.style.right=\'0\';p.style.top=\'0\';p.style.height=\'100vh\';p.innerHTML=\'<h3>Thread</h3><div>\'+m.body+\'</div><div class="thread-replies"></div><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button>\';let rr=await api(\'/messages/\'+m.id+\'/replies\');p.querySelector(\'.thread-replies\').innerHTML=rr.replies.map(x=>\'<div>\'+x.body+\'</div>\').join(\'\');p.querySelector(\'[data-testid="close-thread"]\').onclick=()=>p.style.display=\'none\';p.querySelector(\'[data-testid="thread-send"]\').onclick=async()=>{let i=p.querySelector(\'[data-testid="thread-input"]\');if(!i.value.trim())return;await api(\'/channels/\'+chan+\'/messages\',{method:\'POST\',body:JSON.stringify({body:i.value,parent_id:m.id})});p.querySelector(\'.thread-replies\').insertAdjacentHTML(\'beforeend\',\'<div>\'+i.value+\'</div>\');d.querySelector(\'[data-testid="reply-count"]\').textContent=\'1 reply\';i.value=\'\'}};\n d.querySelector(\'[data-testid="reaction-button"]\').onclick=()=>{let p=document.createElement(\'div\');p.dataset.testid=\'emoji-picker\';[\'👍\',\'❤️\',\'😂\',\'🎉\',\'👀\',\'✅\'].forEach(em=>{let b=document.createElement(\'button\');b.dataset.testid=\'emoji-option\';b.textContent=em;b.onclick=async()=>{let x=await api(\'/messages/\'+m.id+\'/reactions\',{method:\'POST\',body:JSON.stringify({emoji:em})});let c=document.createElement(\'span\');c.dataset.testid=\'reaction-chip\';c.textContent=em+\' \'+(x.reactions.find(r=>r.emoji===em)||{count:1}).count;c.onclick=async()=>{let y=await api(\'/messages/\'+m.id+\'/reactions/\'+encodeURIComponent(em),{method:\'DELETE\'});c.remove();};d.append(c);p.remove()};p.append(b)});d.append(p)};\n $(\'#message-list\').append(d)\n}\n$(\'#auth-form\')?.addEventListener(\'submit\',async e=>{e.preventDefault();let f=new FormData(e.target),mode=e.target.dataset.mode||\'register\';try{let x=await fetch(\'/api/auth/\'+(mode===\'login\'?\'login\':\'register\'),{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({username:f.get(\'username\'),password:f.get(\'password\'),display_name:f.get(\'display_name\')})});let j=await x.json();if(!x.ok)throw Error(j.error||\'Invalid input\');token=j.token;localStorage.setItem(\'huddle.token\',token);current=j.user;$(\'#current-user\').textContent=current.username;$(\'#auth-modal\').style.display=\'none\';boot()}catch(e){alertAuth(e.message)}});\n$(\'#auth-toggle\')?.addEventListener(\'click\',()=>{let f=$(\'#auth-form\');f.dataset.mode=(f.dataset.mode||\'register\')===\'register\'?\'login\':\'register\'});\ndocument.querySelector(\'.main form:not([data-testid])\')?.addEventListener(\'submit\',async e=>{e.preventDefault();let i=$(\'#message-input\');if(!i.value.trim())return;if(!chan)return;await api(\'/channels/\'+chan+\'/messages\',{method:\'POST\',body:JSON.stringify({body:i.value})});i.value=\'\';let x=await api(\'/channels/\'+chan+\'/messages\');renderMessages(x.messages.slice().reverse())});\n$(\'#workspace-create-form\')?.addEventListener(\'submit\',async e=>{e.preventDefault();let f=new FormData(e.target);try{let x=await api(\'/workspaces\',{method:\'POST\',body:JSON.stringify({name:f.get(\'name\'),slug:f.get(\'slug\')})});$(\'#empty-state-create-workspace\').style.display=\'none\';await loadWorkspace(x.workspace.slug)}catch(err){let a=e.target.querySelector(\'[role=alert]\')||document.createElement(\'div\');a.setAttribute(\'role\',\'alert\');a.textContent=err.message;e.target.append(a)}});document.addEventListener(\'click\',async e=>{if(e.target.dataset.testid===\'edit-message-btn\'){let r=e.target.closest(\'[data-testid="message"]\');if(!r.querySelector(\'[data-testid="edit-message-input"]\')){let body=r.querySelector(\'[data-testid="message-body"]\'),i=document.createElement(\'input\');i.dataset.testid=\'edit-message-input\';i.value=body?body.textContent:\'\';if(body)body.replaceWith(i);else r.append(i);i.focus();i.onkeydown=async k=>{if(k.key===\'Enter\'){let x=await api(\'/messages/\'+r.dataset.id,{method:\'PATCH\',body:JSON.stringify({body:i.value})});let z=document.createElement(\'div\');z.dataset.testid=\'message-body\';z.className=\'message-body\';z.textContent=x.message.body;i.replaceWith(z);r.querySelector(\'[data-testid="edited-indicator"]\').textContent=\' (edited)\';}}}}},true);\ndocument.addEventListener(\'click\',async e=>{let row=e.target.closest(\'[data-testid="message"]\');if(!row)return;let id=row.dataset.id;\nif(e.target.dataset.testid===\'delete-message-btn\'){await api(\'/messages/\'+id,{method:\'DELETE\'});row.remove()}\nif(e.target.dataset.testid===\'edit-message-btn\'){let body=row.querySelector(\'.message-body\');if(!body)return;let inp=document.createElement(\'input\');inp.dataset.testid=\'edit-message-input\';inp.value=body.textContent;body.replaceWith(inp);inp.focus();inp.onkeydown=async k=>{if(k.key===\'Enter\'){let x=await api(\'/messages/\'+id,{method:\'PATCH\',body:JSON.stringify({body:inp.value})});let z=document.createElement(\'div\');z.className=\'message-body\';z.textContent=x.message.body;inp.replaceWith(z);let ed=document.createElement(\'span\');ed.dataset.testid=\'edited-indicator\';ed.textContent=\' (edited)\';row.append(ed)}if(k.key===\'Escape\'){inp.replaceWith(body)}}}\nif(e.target.dataset.testid===\'open-thread-btn\'){let p=document.querySelector(\'[data-testid="thread-panel"]\');p.style.display=\'block\';p.style.position=\'absolute\';p.style.right=\'0\';p.innerHTML=\'<h3>Thread</h3><div class="thread-parent">\'+row.querySelector(\'.message-body\').textContent+\'</div><div class="thread-replies"></div><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Reply</button><button data-testid="close-thread" data-button-role="secondary">Close</button>\';p.querySelector(\'[data-testid="close-thread"]\').onclick=()=>p.style.display=\'none\';p.querySelector(\'[data-testid="thread-send"]\').onclick=async()=>{let i=p.querySelector(\'[data-testid="thread-input"]\');if(!i.value.trim())return;await api(\'/channels/\'+chan+\'/messages\',{method:\'POST\',body:JSON.stringify({body:i.value,parent_id:Number(id)})});p.querySelector(\'.thread-replies\').textContent=i.value;i.value=\'\'} }\nif(e.target.dataset.testid===\'reaction-button\'){let p=document.createElement(\'div\');p.dataset.testid=\'emoji-picker\';[\'👍\',\'❤️\',\'😂\',\'🎉\',\'👀\',\'✅\'].forEach(em=>{let b=document.createElement(\'button\');b.dataset.testid=\'emoji-option\';b.textContent=em;b.onclick=async()=>{await api(\'/messages/\'+id+\'/reactions\',{method:\'POST\',body:JSON.stringify({emoji:em})});let c=document.createElement(\'span\');c.dataset.testid=\'reaction-chip\';c.textContent=em+\' 1\';row.append(c);p.remove()};p.append(b)});row.append(p)}});$(\'#logout-btn\')?.addEventListener(\'click\',()=>{localStorage.removeItem(\'huddle.token\');location.reload()});boot();\n\n(function(){const f=document.querySelector(\'[data-testid="workspace-create-form"]\');if(!f)return;f.onsubmit=async function(e){e.preventDefault();const n=f.querySelector(\'[name="name"]\').value.trim(),g=f.querySelector(\'[name="slug"]\').value.trim();try{const r=await api(\'/workspaces\',{method:\'POST\',body:JSON.stringify({name:n,slug:g})});document.querySelector(\'[data-testid="empty-state-create-workspace"]\').style.display=\'none\';document.querySelector(\'[data-testid="workspace-header"]\').textContent=r.workspace.name;await loadWorkspace(r.workspace.slug)}catch(err){let a=f.querySelector(\'[role="alert"]\');if(!a){a=document.createElement(\'div\');a.setAttribute(\'role\',\'alert\');f.appendChild(a)}a.textContent=err.message}}})();\n</script><script>\n(function(){\nconst q=s=>document.querySelector(s);\nconst modal=(id,v)=>{let x=q(id);if(x)x.style.display=v?\'block\':\'none\'};\nq(\'[data-testid="new-channel-btn"]\')?.addEventListener(\'click\',()=>modal(\'#create-channel-modal\',true));\nq(\'[data-testid="create-channel-cancel"]\')?.addEventListener(\'click\',()=>modal(\'#create-channel-modal\',false));\nq(\'[data-testid="channel-settings-btn"]\')?.addEventListener(\'click\',()=>modal(\'#channel-settings-modal\',true));\nq(\'[data-testid="channel-settings-close"]\')?.addEventListener(\'click\',()=>modal(\'#channel-settings-modal\',false));\nq(\'[data-testid="workspace-settings-btn"]\')?.addEventListener(\'click\',()=>modal(\'#workspace-settings-modal\',true));\nq(\'[data-testid="workspace-settings-close"]\')?.addEventListener(\'click\',()=>modal(\'#workspace-settings-modal\',false));\nq(\'[data-testid="join-workspace-form"]\')?.addEventListener(\'submit\',async e=>{e.preventDefault();let x=q(\'[data-testid="join-workspace-error"]\');x.textContent=\'Invalid invitation code\';x.style.display=\'block\'});\nq(\'[data-testid="create-channel-form"]\')?.addEventListener(\'submit\',async e=>{e.preventDefault();let f=new FormData(e.target);try{let ws=await api(\'/workspaces\');let slug=ws.workspaces[0].slug;let x=await api(\'/workspaces/\'+slug+\'/channels\',{method:\'POST\',body:JSON.stringify({name:f.get(\'name\'),is_private:q(\'[data-testid="create-channel-private"]\').checked})});modal(\'#create-channel-modal\',false);await loadWorkspace(slug);openChannel(x.channel.id,x.channel.name)}catch(err){let a=e.target.querySelector(\'[role="alert"]\');if(!a){a=document.createElement(\'div\');a.setAttribute(\'role\',\'alert\');e.target.append(a)}a.textContent=err.message}});\n})();\n</script><script>(function(){let b=document.querySelector(\'[data-testid="channel-topic-submit"]\');if(b)b.onclick=async()=>{try{let x=await api(\'/channels/\'+chan,{method:\'PATCH\',body:JSON.stringify({topic:document.querySelector(\'[data-testid="channel-topic-input"]\').value})});document.querySelector(\'[data-testid="channel-topic"]\').textContent=x.channel.topic;document.querySelector(\'#channel-topic\').textContent=x.channel.topic}catch(e){console.error(e)}}})();</script><script>(function(){let b=document.querySelector(\'[data-testid="create-invitation-btn"]\'),f=document.querySelector(\'[data-testid="create-invitation-form"]\');b?.addEventListener(\'click\',()=>{f.style.display=\'block\'});f?.addEventListener(\'submit\',async e=>{e.preventDefault();let w=await api(\'/workspaces\');let x=await api(\'/workspaces/\'+w.workspaces[0].slug+\'/invitations\',{method:\'POST\',body:\'{}\'});let z=document.createElement(\'span\');z.className=\'invitation-code\';z.dataset.testid=\'invitation-code\';z.textContent=x.invitation.code;f.append(z)})})();</script></body></html>\\'
app=web.Application(client_max_size=12*1024**1024);app.router.add_get('/',root);app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me);app.router.add_get('/api/users/{id}',get_user);app.router.add_patch('/api/users/me',update_me);app.router.add_post('/api/dms',open_dm);app.router.add_get('/api/workspaces',workspaces);app.router.add_post('/api/workspaces',create_workspace);app.router.add_get('/api/workspaces/{slug}',workspace);app.router.add_route('*','/api/workspaces/{slug}/members',ws_members);app.router.add_route('*','/api/workspaces/{slug}/invitations',invitations);app.router.add_post('/api/invitations/{code}/accept',accept_inv);app.router.add_route('*','/api/workspaces/{slug}/groups',groups);app.router.add_route('*','/api/workspaces/{slug}/groups/{handle}',group_change);app.router.add_post('/api/workspaces/{slug}/channels',channels);app.router.add_post('/api/channels/{id}/join',join);app.router.add_post('/api/channels/{id}/leave',leave_channel);app.router.add_post('/api/channels/{id}/archive',archive_channel);app.router.add_post('/api/channels/{id}/unarchive',archive_channel);app.router.add_get('/api/channels/{id}/pins',pins);app.router.add_post('/api/messages/{id}/pin',pin_message);app.router.add_delete('/api/messages/{id}/pin',unpin_message);app.router.add_get('/api/search',search);app.router.add_patch('/api/channels/{id}',update_channel);app.router.add_get('/api/channels/{id}/messages',messages);app.router.add_post('/api/channels/{id}/messages',post);app.router.add_get('/api/channels/{id}/members',channel_members);app.router.add_get('/api/messages/{id}/replies',replies);app.router.add_get('/api/channels/{id}/read',read_state);app.router.add_patch('/api/messages/{id}',edit);app.router.add_post('/api/messages/{id}/reactions',add_reaction);app.router.add_delete('/api/messages/{id}/reactions/{emoji}',remove_reaction);app.router.add_delete('/api/messages/{id}',delete);app.router.add_get('/api/ws',ws_handler);app.router.add_post('/api/files',upload_file);app.router.add_get('/api/files/{id}',file_meta);app.router.add_get('/api/files/{id}/download',file_download)
web.run_app(app,host='127.0.0.1',port=int(os.getenv('PORT',8000)))
