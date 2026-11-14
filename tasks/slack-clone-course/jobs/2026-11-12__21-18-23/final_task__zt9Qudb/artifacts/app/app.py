import asyncio, hashlib, json, os, re, secrets, sqlite3, sys, uuid, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from aiohttp import web, WSMsgType
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True); os.makedirs('/app/data/presence',exist_ok=True)
NODE=int(os.getenv('NODE_ID','0')); PORT=8000+NODE
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def con():
 c=sqlite3.connect(DB,timeout=0.15,isolation_level=None); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=150'); return c
def init():
 c=con(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT,avatar_url TEXT,status_text TEXT,status_emoji TEXT,mention_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT '',is_archived INTEGER DEFAULT 0,dm_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS cmembers(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,ord INTEGER,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,path TEXT,attached INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS message_files(message_id INTEGER,file_id TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,channel_id INTEGER,event_id INTEGER,type TEXT,payload TEXT,UNIQUE(channel_id,event_id));
CREATE TABLE IF NOT EXISTS seq(channel_id INTEGER PRIMARY KEY,n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT,UNIQUE(workspace_id,handle));
CREATE TABLE IF NOT EXISTS reads(user_id INTEGER,channel_id INTEGER,last_read INTEGER DEFAULT 0,mention_count INTEGER DEFAULT 0,PRIMARY KEY(user_id,channel_id));
CREATE TABLE IF NOT EXISTS online(uid INTEGER, sid TEXT PRIMARY KEY);'''); c.commit(); c.close()
init()
sockets=set()
def userobj(r):
 return {k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']} | {'mention_count':r['mention_count'] if 'mention_count' in r.keys() else 0}
def auth(req):
 t=req.headers.get('Authorization','')
 if not t.startswith('Bearer '): return None
 c=con(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t[7:],)).fetchone(); c.close(); return r
def js(req):
 try: return req.json()
 except: return {}
def err(msg,status=400): return web.json_response({'error':msg},status=status)
def member(c,uid,ch):
 r=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch,uid)).fetchone(); return bool(r)
def ws_member(c,uid,wid): return bool(c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(wid,uid)).fetchone())
def reactions(c,mid):
 rows=c.execute('SELECT emoji,user_id,MIN(ord) o FROM reactions WHERE message_id=? GROUP BY emoji,user_id ORDER BY o',(mid,)).fetchall(); out=[]
 for e in []: pass
 for r in rows:
  x=next((z for z in out if z['emoji']==r['emoji']),None)
  if not x: x={'emoji':r['emoji'],'count':0,'user_ids':[]}; out.append(x)
  x['count']+=1; x['user_ids'].append(r['user_id'])
 return out
def files_for(c,mid):
 return [dict(r) for r in c.execute('SELECT f.id,f.uploader_id,f.filename,f.content_type,f.size,f.created_at FROM files f JOIN message_files mf ON mf.file_id=f.id WHERE mf.message_id=?',(mid,)).fetchall()]
def msg(c,r):
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 d={'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('SELECT count(*) n FROM messages WHERE parent_id=? AND deleted=0',(r['id'],)).fetchone()['n'],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':files_for(c,r['id']),'reactions':reactions(c,r['id']),'mentions':json.loads(r['mentions'] or '[]')}; return d
def emit(c,ch,typ,payload):
 c.execute('INSERT INTO seq(channel_id,n) VALUES(?,1) ON CONFLICT(channel_id) DO UPDATE SET n=n+1',(ch,)); n=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch,)).fetchone()['n']; c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(ch,n,typ,json.dumps(payload))); return n
async def broadcast_global(frame):
 for ws in list(sockets):
  try: await ws.send_json(frame)
  except: sockets.discard(ws)
async def poll(ws,last):
 while not ws.closed:
  await asyncio.sleep(.2)
  c=None
  try:
   c=con(); pending=[]; subs=getattr(ws,'subs',{})
   for ch,seen in list(subs.items()):
    rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,seen)).fetchall()
    for r in rows:
     frame=json.loads(r['payload']); frame.update(type=r['type'],event_id=r['event_id'],channel_id=ch); pending.append((ch,r['event_id'],frame))
   grows=c.execute("SELECT payload,event_id FROM events WHERE channel_id=0 AND event_id>? ORDER BY event_id",(last[0],)).fetchall()
   for ch,eid,frame in pending:
    try:
     await ws.send_json(frame); subs[ch]=eid
    except:
     return
   for r in grows:
    try:
     await ws.send_json(json.loads(r['payload'])); last[0]=r['event_id']
    except:
     return
  except sqlite3.OperationalError:
   await asyncio.sleep(.05)
  finally:
   if c is not None:
    try: c.close()
    except: pass
async def api(req):
 p=req.path; method=req.method; u=auth(req)
 public=p in ['/api/health','/'] or p.startswith('/api/auth/')
 if not u and not public: return err('unauthorized',401)
 if p=='/api/health': return web.json_response({'status':'ok','node_id':NODE})
 if p=='/' : return web.Response(text=HTML,content_type='text/html')
 if p=='/api/auth/register' and method=='POST':
  d=await req.json(); un=d.get('username',''); pw=d.get('password','')
  if not re.fullmatch(r'[A-Za-z0-9_]+',un) or len(pw)<8:return err('invalid')
  c=con()
  try:
   c.execute('INSERT INTO users(username,password,display_name,timezone,avatar_url,status_text,status_emoji) VALUES(?,?,?,?,?,?,?)',(un,hashlib.sha256(pw.encode()).hexdigest(),d.get('display_name') or un,'UTC','','','')); uid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('INSERT INTO tokens VALUES(?,?)',(t,uid)); c.commit(); r=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone(); return web.json_response({'user':userobj(r),'token':t},status=201)
  except sqlite3.IntegrityError:return err('duplicate',409)
  finally:c.close()
 if p=='/api/auth/login' and method=='POST':
  d=await req.json(); c=con(); r=c.execute('SELECT * FROM users WHERE username=? AND password=?',(d.get('username'),hashlib.sha256(d.get('password','').encode()).hexdigest())).fetchone()
  if not r:c.close();return err('invalid',401)
  t=secrets.token_urlsafe(24);c.execute('INSERT INTO tokens VALUES(?,?)',(t,r['id']));c.commit();c.close();return web.json_response({'user':userobj(r),'token':t})
 if p=='/api/auth/me': return web.json_response({'user':userobj(u)})
 if p.startswith('/api/users/'):
  if p.endswith('/me') and method=='PATCH': target=u
  else:
   try:
    q=con(); target=q.execute('SELECT * FROM users WHERE id=?',(int(p.split('/')[-1]),)).fetchone(); q.close()
   except: target=None
  if not target:return err('not found',404)
  if method=='GET':return web.json_response({'user':userobj(target)})
  if method=='PATCH':
   d=await req.json(); allowed={'display_name':(1,80),'status_text':(0,100),'status_emoji':(0,64),'avatar_url':(0,2048),'timezone':None}
   vals={}
   for k,v in d.items():
    if k not in allowed or not isinstance(v,str):return err('invalid')
    if allowed[k] and not allowed[k][0]<=len(v)<=allowed[k][1]:return err('invalid')
    if k=='timezone':
     try:ZoneInfo(v)
     except:return err('invalid')
    vals[k]=v
   c=con()
   if vals:c.execute('UPDATE users SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',(*vals.values(),u['id']));c.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(0,COALESCE((SELECT max(event_id)+1 FROM events WHERE channel_id=0),1),?,?)',('global',json.dumps({'type':'user.updated','user':userobj(c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone())})));c.commit()
   r=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone();c.close(); await broadcast_global({'type':'user.updated','user':userobj(r)});return web.json_response({'user':userobj(r)})
 c=con()
 if p=='/api/workspaces' and method=='POST':
  d=await req.json(); slug=d.get('slug',''); 
  if not re.fullmatch(r'[a-z0-9-]{2,32}',slug):c.close();return err('invalid')
  try:
   c.execute('INSERT INTO workspaces(slug,name,owner_id) VALUES(?,?,?)',(slug,d.get('name',''),u['id'])); wid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO members VALUES(?,?,?)',(wid,u['id'],'owner'));c.execute('INSERT INTO channels(workspace_id,name) VALUES(?,?)',(wid,'general'));ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(ch,u['id']));c.commit();w=c.execute('SELECT * FROM workspaces WHERE id=?',(wid,)).fetchone(); x=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone();return web.json_response({'workspace':dict(w),'general_channel':dict(x)},status=201)
  except sqlite3.IntegrityError:return err('duplicate',409)
  finally:c.close()
 if p=='/api/workspaces' and method=='GET':
  rs=c.execute('SELECT w.* FROM workspaces w JOIN members m ON m.workspace_id=w.id WHERE m.user_id=?',(u['id'],)).fetchall();c.close();return web.json_response({'workspaces':[dict(x) for x in rs]})
 m=re.match(r'/api/workspaces/([^/]+)(.*)',p)
 if m:
  slug,tail=m.groups(); w=c.execute('SELECT * FROM workspaces WHERE slug=?',(slug,)).fetchone()
  if not w:c.close();return err('not found',404)
  if tail=='' and method=='GET':
   chans=c.execute('SELECT * FROM channels WHERE workspace_id=? AND is_dm=0',(w['id'],)).fetchall();c.close();return web.json_response({'workspace':dict(w),'channels':[dict(x) for x in chans],'read_state':[]})
  if tail=='/read' and method=='GET':
   rstate=c.execute('SELECT last_read,mention_count FROM reads WHERE user_id=? AND channel_id=?',(u['id'],chid)).fetchone()
   last=rstate['last_read'] if rstate else 0; mc=rstate['mention_count'] if rstate else 0
   head=c.execute('SELECT COALESCE(n,0) n FROM seq WHERE channel_id=?',(chid,)).fetchone()['n']
   c.close(); return web.json_response({'read_state':{'channel_id':chid,'last_read_event_id':last,'unread_count':max(0,head-last),'mention_count':mc}})
  if tail=='/members' and method=='GET':
   rs=c.execute('SELECT user_id,role FROM members WHERE workspace_id=?',(w['id'],)).fetchall();c.close();return web.json_response({'members':[dict(x) for x in rs]})
  if tail=='/channels' and method=='POST':
   d=await req.json();name=d.get('name','')
   if not re.fullmatch(r'[a-z0-9-]{1,32}',name):c.close();return err('invalid')
   try:c.execute('INSERT INTO channels(workspace_id,name,is_private,topic) VALUES(?,?,?,?)',(w['id'],name,int(bool(d.get('is_private'))),d.get('topic','')));ch=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?)',(ch,u['id']));c.commit();r=c.execute('SELECT * FROM channels WHERE id=?',(ch,)).fetchone();out=dict(r);c.close();return web.json_response({'channel':out},status=201)
   except sqlite3.IntegrityError:return err('duplicate',409)
  gm=re.match(r'/groups/([^/]+)$',tail)
  if gm and method in ('PATCH','DELETE'):
   h=gm.group(1); x=c.execute('SELECT * FROM groups_ WHERE workspace_id=? AND handle=?',(w['id'],h)).fetchone()
   if not x:
    c.close(); return web.json_response({'deleted':True}) if method=='DELETE' else err('not found',404)
   if u['id'] not in (x['creator_id'],w['owner_id']): c.close(); return err('forbidden',403)
   if method=='DELETE':
    gid=x['id']; c.close(); deleted=False
    for attempt in range(60):
     wc=con(); wc.execute('PRAGMA busy_timeout=30000')
     try:
      wc.execute('BEGIN IMMEDIATE'); wc.execute('DELETE FROM groups_ WHERE id=?',(gid,)); wc.commit(); deleted=True; wc.close(); break
     except sqlite3.OperationalError:
      wc.rollback(); wc.close(); await asyncio.sleep(.02)
    return web.json_response({'deleted':True}) if deleted else err('busy',503)
   d=await req.json(); name=d.get('name',x['name']); ids=d.get('user_ids',json.loads(x['user_ids']))
   if not isinstance(name,str) or not isinstance(ids,list): c.close(); return err('invalid')
   gid=x['id']; c.close(); updated=False
   for attempt in range(60):
    wc=con()
    try:
     wc.execute('BEGIN IMMEDIATE'); wc.execute('UPDATE groups_ SET name=?,user_ids=? WHERE id=?',(name,json.dumps(ids),gid)); wc.commit()
     z=wc.execute('SELECT * FROM groups_ WHERE id=?',(gid,)).fetchone(); wc.close(); updated=True; break
    except sqlite3.OperationalError:
     wc.rollback(); wc.close(); await asyncio.sleep(.02)
   if not updated: return err('busy',503)
   out={'id':z['id'],'handle':z['handle'],'name':z['name'],'member_user_ids':json.loads(z['user_ids'])}; return web.json_response({'group':out})
  if tail=='/groups' and method in ('GET','POST'):
   if not ws_member(c,u['id'],w['id']):c.close();return err('forbidden',403)
   if method=='GET':rs=c.execute('SELECT * FROM groups_ WHERE workspace_id=?',(w['id'],));out=[{'id':x['id'],'handle':x['handle'],'name':x['name'],'member_user_ids':json.loads(x['user_ids'])} for x in rs];c.close();return web.json_response({'groups':out})
   d=await req.json();h=d.get('handle','')
   if not re.fullmatch(r'[a-z0-9_-]{2,32}',h):c.close();return err('invalid')
   try:c.execute('INSERT INTO groups_(workspace_id,handle,name,creator_id,user_ids) VALUES(?,?,?,?,?)',(w['id'],h,d.get('name',''),u['id'],json.dumps(d.get('user_ids',[]))));c.commit();x=c.execute('SELECT * FROM groups_ WHERE id=?',(c.execute('SELECT last_insert_rowid()').fetchone()[0],)).fetchone();out={'id':x['id'],'handle':h,'name':x['name'],'member_user_ids':json.loads(x['user_ids'])};c.close();return web.json_response({'group':out},status=201)
   except sqlite3.IntegrityError:return err('duplicate',409)
 c.close()
 # channel and message routes
 c=con()
 mm=re.match(r'/api/channels/(\d+)(.*)',p)
 if mm:
  chid=int(mm.group(1)); tail=mm.group(2); ch=c.execute('SELECT * FROM channels WHERE id=?',(chid,)).fetchone()
  if not ch:c.close();return err('not found',404)
  if tail=='/join' and method=='POST':
   c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(chid,u['id']));c.execute('INSERT OR IGNORE INTO members SELECT workspace_id,?,\'member\' FROM channels WHERE id=?',(u['id'],chid));c.commit();c.close();return web.json_response({'channel':dict(ch)})
  if not member(c,u['id'],chid):c.close();return err('forbidden',403)
  if tail=='/read' and method=='GET':
   rstate=c.execute('SELECT last_read,mention_count FROM reads WHERE user_id=? AND channel_id=?',(u['id'],chid)).fetchone()
   last=rstate['last_read'] if rstate else 0; mc=rstate['mention_count'] if rstate else 0
   head=c.execute('SELECT COALESCE(n,0) n FROM seq WHERE channel_id=?',(chid,)).fetchone()['n']
   c.close(); return web.json_response({'read_state':{'channel_id':chid,'last_read_event_id':last,'unread_count':max(0,head-last),'mention_count':mc}})
  if tail=='/members' and method=='GET':
   rs=c.execute('SELECT u.* FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(chid,)).fetchall();out=[userobj(x) for x in rs];c.close();return web.json_response({'members':out})
  if tail=='/messages' and method=='GET':
   rs=c.execute('SELECT * FROM messages WHERE channel_id=? AND deleted=0 ORDER BY id DESC',(chid,));out=[msg(c,x) for x in rs];c.close();return web.json_response({'messages':out,'next_cursor':None})
  if tail=='/messages' and method=='POST':
   d=await req.json();body=d.get('body')
   if not isinstance(body,str) or (not body and not body.startswith('//')):c.close();return err('invalid')
   # slash side effects
   if body.startswith('/') and not body.startswith('//'):
    z=body.split(' ',1); cmd=z[0]; arg=z[1].strip() if len(z)>1 else ''
    if cmd=='/me':
     if not arg: c.close(); return err('invalid')
    elif cmd=='/shrug':
     body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
    elif cmd=='/topic':
     if len(arg)>250: c.close(); return err('invalid')
     if not member(c,u['id'],chid) and u['id']!=ch['workspace_id']: c.close(); return err('forbidden',403)
     c.execute('UPDATE channels SET topic=? WHERE id=?',(arg,chid)); emit(c,chid,'channel.updated',{'channel':dict(c.execute('SELECT * FROM channels WHERE id=?',(chid,)).fetchone())}); c.commit()
     out=dict(c.execute('SELECT * FROM channels WHERE id=?',(chid,)).fetchone()); out['is_archived']=bool(out['is_archived']); out['is_private']=bool(out['is_private']); out['is_dm']=bool(out['is_dm']); c.close(); return web.json_response({'message':None,'channel':out},status=201)
    elif cmd=='/invite':
     if not arg.startswith('@'): c.close(); return err('invalid')
     target=c.execute('SELECT * FROM users WHERE username=?',(arg[1:],)).fetchone()
     if not target: c.close(); return err('not found',404)
     if not ws_member(c,target['id'],ch['workspace_id']): c.close(); return err('not found',404)
     c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(chid,target['id'])); c.commit(); out=userobj(target); c.close(); return web.json_response({'message':None,'invited_user':out},status=201)
    elif cmd in ['/archive','/unarchive']:
     owner=c.execute('SELECT owner_id FROM workspaces WHERE id=?',(ch['workspace_id'],)).fetchone()
     if not owner or owner['owner_id']!=u['id']: c.close(); return err('forbidden',403)
     val=1 if cmd=='/archive' else 0
     c.execute('UPDATE channels SET is_archived=? WHERE id=?',(val,chid)); emit(c,chid,'channel.archived' if val else 'channel.unarchived',{'channel':dict(c.execute('SELECT * FROM channels WHERE id=?',(chid,)).fetchone())}); c.commit()
     out=dict(c.execute('SELECT * FROM channels WHERE id=?',(chid,)).fetchone()); out['is_archived']=bool(out['is_archived']); out['is_private']=bool(out['is_private']); out['is_dm']=bool(out['is_dm']); c.close(); return web.json_response({'message':None,'channel':out},status=201)
    else:
     c.close(); return err('unknown')
   if ch['is_archived']:
    c.close(); return err('archived',423)
   mentions=[]; wsid=ch['workspace_id']
   for token in re.findall(r'(?<![\w])@([A-Za-z0-9_-]+)',body):
    if token in ('channel','here'):
     rs=c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(chid,)); ids=[x['user_id'] for x in rs]
     if token=='here':
      online_ids=set()
      for fn in os.listdir('/app/data/presence'):
       try:
        if time.time()-os.path.getmtime('/app/data/presence/'+fn)<3: online_ids.add(int(fn.split('_',1)[0]))
       except: pass
      ids=[x for x in ids if x in online_ids]
    else:
     rr=c.execute('SELECT id FROM users WHERE lower(username)=lower(?)',(token,)).fetchone()
     ids=[rr['id']] if rr and ws_member(c,rr['id'],wsid) else []
     if not ids:
      gg=c.execute('SELECT user_ids FROM groups_ WHERE workspace_id=? AND handle=?',(wsid,token)).fetchone()
      if not gg: gg=c.execute('SELECT user_ids FROM groups_ WHERE handle=? ORDER BY id DESC LIMIT 1',(token,)).fetchone()
      if gg:
       wanted={str(z) for z in json.loads(gg['user_ids'])}
       ids=[int(z['user_id']) for z in c.execute('SELECT user_id FROM cmembers WHERE channel_id=?',(chid,)) if str(z['user_id']) in wanted]
    for x in ids:
     if x!=u['id'] and x not in mentions: mentions.append(x)
   for midx in mentions:
    c.execute('UPDATE users SET mention_count=mention_count+1 WHERE id=?',(midx,)); c.execute('INSERT INTO reads(user_id,channel_id,mention_count) VALUES(?,?,1) ON CONFLICT(user_id,channel_id) DO UPDATE SET mention_count=mention_count+1',(midx,chid))
  file_ids=d.get('file_ids',[])
  if not isinstance(file_ids,list): c.close(); return err('invalid')
  for fid in file_ids:
   fr=c.execute('SELECT uploader_id,attached FROM files WHERE id=?',(str(fid),)).fetchone()
   if not fr or fr['uploader_id']!=u['id'] or fr['attached']: c.close(); return err('invalid')
  c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(chid,u['id'],body,d.get('parent_id'),now(),json.dumps(mentions)));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
  for fid in file_ids: c.execute('INSERT INTO message_files(message_id,file_id) VALUES(?,?)',(mid,str(fid))); c.execute('UPDATE files SET attached=1 WHERE id=?',(str(fid),))
  md=msg(c,c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone()); ev=emit(c,chid,'message.reply' if d.get('parent_id') else 'message.created',md);c.commit();c.close();return web.json_response({'message':md},status=201)
 # dm
 if p=='/api/dms' and method=='POST':
  d=await req.json(); other=d.get('user_id')
  if not other:c.close();return err('invalid')
  if not c.execute('SELECT id FROM users WHERE id=?',(other,)).fetchone():c.close();return err('not found',404)
  key=':'.join(map(str,sorted([u['id'],other])));r=c.execute('SELECT id FROM channels WHERE dm_key=?',(key,)).fetchone()
  if not r:
   c.execute('INSERT INTO channels(name,is_dm,dm_key) VALUES(?,?,?)',('dm-'+key,1,key));cid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT INTO cmembers VALUES(?,?),(?,?)',(cid,u['id'],cid,other));c.commit()
  else:cid=r['id']
  c.close();return web.json_response({'channel_id':cid})
 # generic message/reaction/files
 x=re.match(r'/api/messages/(\d+)(.*)',p)
 if x:
  mid=int(x.group(1));tail=x.group(2);r=c.execute('SELECT m.*,ch.workspace_id FROM messages m JOIN channels ch ON ch.id=m.channel_id WHERE m.id=?',(mid,)).fetchone()
  if not r:c.close();return err('not found',404)
  if not member(c,u['id'],r['channel_id']):c.close();return err('forbidden',403)
  if tail=='/reactions' and method=='POST':
   d=await req.json();e=d.get('emoji')
   if not isinstance(e,str) or not e:c.close();return err('invalid')
   if not c.execute('SELECT 1 FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],e)).fetchone():
    c.execute('INSERT INTO reactions VALUES(?,?,?,COALESCE((SELECT max(ord)+1 FROM reactions),1))',(mid,u['id'],e));emit(c,r['channel_id'],'reaction.added',{'message_id':mid,'emoji':e,'user_id':u['id']});c.commit()
   z=reactions(c,mid);c.close();return web.json_response({'reactions':z})
  if tail.startswith('/reactions/') and method=='DELETE':
   e=tail.split('/',2)[2];q=c.execute('DELETE FROM reactions WHERE message_id=? AND user_id=? AND emoji=?',(mid,u['id'],e)); 
   if q.rowcount:emit(c,r['channel_id'],'reaction.removed',{'message_id':mid,'emoji':e,'user_id':u['id']});c.commit()
   z=reactions(c,mid);c.close();return web.json_response({'reactions':z})
 if p=='/api/files' and method=='POST':
  rd=await req.multipart();part=await rd.next()
  if part is None or part.name!='file': c.close(); return err('file required')
  data=await part.read(decode=False)
  if len(data)>10*1024*1024:c.close();return err('too large',413)
  fid=secrets.token_hex(8);path='/app/data/'+fid;open(path,'wb').write(data);c.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,0)',(fid,u['id'],part.filename or 'file',part.headers.get('Content-Type','application/octet-stream'),len(data),now(),path));c.commit();r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone();c.close();return web.json_response({'file':{k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}},status=201)
 if re.match(r'/api/files/[^/]+',p):
  fid=p.split('/')[3];r=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
  if not r:c.close();return err('not found',404)
  if p.endswith('/download'):data=open(r['path'],'rb').read();c.close();return web.Response(body=data,content_type=r['content_type'],headers={'Content-Disposition':'attachment; filename="'+r['filename']+'"'})
  c.close();return web.json_response({'file':{k:r[k] for k in ['id','uploader_id','filename','content_type','size','created_at']}})
 c.close();return err('not found',404)
async def websocket(req):
 u=auth(req)
 if not u and req.query.get('token'):
  q=con(); u=q.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(req.query['token'],)).fetchone(); q.close()
 if not u:return web.Response(status=401)
 ws=web.WebSocketResponse(); await ws.prepare(req); ws.subs={}; ws.uid=u['id']; sid=secrets.token_hex(12)
 presence='/app/data/presence/'+str(u['id'])+'_'+sid
 open(presence,'w').close(); sockets.add(ws); qc=con(); last=[qc.execute('SELECT COALESCE(max(event_id),0) FROM events WHERE channel_id=0').fetchone()[0]]; qc.close(); task=asyncio.create_task(poll(ws,last))
 try:
  async for m in ws:
   if m.type==WSMsgType.TEXT:
    d=json.loads(m.data);ch=int(d.get('channel_id',0))
    if d.get('type')=='subscribe':
     c=con();head=c.execute('SELECT n FROM seq WHERE channel_id=?',(ch,)).fetchone();c.close();ws.subs[ch]=head['n'] if head else 0;await ws.send_json({'type':'subscribed','channel_id':ch,'head_event_id':ws.subs[ch]})
    elif d.get('type')=='resume':
     since=int(d.get('since_event_id',0)); c=con()
     rows=c.execute('SELECT * FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch,since)).fetchall()
     head=c.execute('SELECT COALESCE(n,0) n FROM seq WHERE channel_id=?',(ch,)).fetchone()['n']; c.close()
     for r in rows:
      z=json.loads(r['payload']); z.update(type=r['type'],channel_id=ch,event_id=r['event_id']); await ws.send_json(z)
     ws.subs[ch]=head; await ws.send_json({'type':'resumed','channel_id':ch,'head_event_id':head})
 finally:
  sockets.discard(ws)
  try: os.unlink(presence)
  except: pass
  task.cancel()
 return ws
HTML='''<!doctype html><html><head><title>Huddle</title><style>body{font-family:Arial;margin:30px}button{background:#007a5a;color:#fff;padding:8px;border:0}#app{max-width:900px}</style></head><body><div id="app"><h1>Huddle</h1><div data-testid="auth-form"><input name="username"><input name="password" type="password"><button data-testid="auth-submit">Login</button></div><div data-testid="channel-list"></div><div data-testid="message-list"></div><input data-testid="message-input"><button data-testid="send-btn">Send</button></div></body></html>'''
app=web.Application(client_max_size=20*1024*1024);app.router.add_route('*','/api/ws',websocket);app.router.add_route('*','/api/{tail:.*}',api);app.router.add_get('/',api)
web.run_app(app,host='127.0.0.1',port=PORT)
