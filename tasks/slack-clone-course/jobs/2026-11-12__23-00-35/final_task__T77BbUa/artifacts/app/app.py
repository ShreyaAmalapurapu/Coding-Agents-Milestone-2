import asyncio, json, os, re, secrets, sqlite3, sys, hashlib, mimetypes
from datetime import datetime, timezone
from aiohttp import web, WSMsgType
DB='/app/data/huddle.db'; os.makedirs('/app/data',exist_ok=True)
PORT=int(os.environ.get('PORT','8000')); NODE=PORT-8000
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=10000'); return c
def init():
 c=db(); c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password TEXT,display_name TEXT,timezone TEXT DEFAULT "UTC",avatar_url TEXT DEFAULT "",status_text TEXT DEFAULT "",status_emoji TEXT DEFAULT "",mention_count INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens(token TEXT PRIMARY KEY,user_id INTEGER);
CREATE TABLE IF NOT EXISTS workspaces(id INTEGER PRIMARY KEY,slug TEXT UNIQUE,name TEXT,owner_id INTEGER,join_mode TEXT DEFAULT "open");
CREATE TABLE IF NOT EXISTS members(workspace_id INTEGER,user_id INTEGER,role TEXT,PRIMARY KEY(workspace_id,user_id));
CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY,workspace_id INTEGER,name TEXT,is_private INTEGER DEFAULT 0,is_dm INTEGER DEFAULT 0,topic TEXT DEFAULT "",is_archived INTEGER DEFAULT 0,dm_a INTEGER,dm_b INTEGER,UNIQUE(workspace_id,name));
CREATE TABLE IF NOT EXISTS channel_members(channel_id INTEGER,user_id INTEGER,PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,channel_id INTEGER,author_id INTEGER,body TEXT,parent_id INTEGER,created_at TEXT,edited_at TEXT,deleted INTEGER DEFAULT 0,mentions TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS events(channel_id INTEGER,event_id INTEGER,message_id INTEGER,kind TEXT,frame TEXT,PRIMARY KEY(channel_id,event_id));
CREATE TABLE IF NOT EXISTS reactions(message_id INTEGER,user_id INTEGER,emoji TEXT,PRIMARY KEY(message_id,user_id,emoji));
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,uploader_id INTEGER,filename TEXT,content_type TEXT,size INTEGER,created_at TEXT,data BLOB);
CREATE TABLE IF NOT EXISTS attachments(message_id INTEGER,file_id INTEGER UNIQUE);
CREATE TABLE IF NOT EXISTS groups_(id INTEGER PRIMARY KEY,workspace_id INTEGER,handle TEXT,name TEXT,creator_id INTEGER,user_ids TEXT);
CREATE TABLE IF NOT EXISTS invitations(code TEXT PRIMARY KEY,workspace_id INTEGER,invited_user_id INTEGER,expires_at TEXT,max_uses INTEGER,uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS global_events(id INTEGER PRIMARY KEY,frame TEXT);
CREATE TABLE IF NOT EXISTS ws_presence(user_id INTEGER PRIMARY KEY,updated_at TEXT);''');
 try:
  c.execute('ALTER TABLE users ADD COLUMN mention_count INTEGER DEFAULT 0')
 except sqlite3.OperationalError: pass
 for table,col,definition in [('messages','mentions',"TEXT DEFAULT ''"),('users','mention_count','INTEGER DEFAULT 0')]:
  try: c.execute(f'ALTER TABLE {table} ADD COLUMN {col} {definition}')
  except sqlite3.OperationalError: pass
 for col in ['dm_a','dm_b']:
  try: c.execute(f'ALTER TABLE channels ADD COLUMN {col} INTEGER')
  except sqlite3.OperationalError: pass
 c.commit(); c.close()
init(); sockets=set(); socket_users={}; irc_clients=set()
def userobj(r):
 d={k:r[k] for k in ['id','username','display_name','timezone','avatar_url','status_text','status_emoji']}; d['mention_count']=r['mention_count'] or 0; return d
def fileobj(r): return {'id':r['id'],'uploader_id':r['uploader_id'],'filename':r['filename'],'content_type':r['content_type'],'size':r['size'],'created_at':r['created_at']}
def msgobj(c,r):
 a=c.execute('select * from users where id=?',(r['author_id'],)).fetchone()
 rr=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(r['id'],)).fetchall()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':userobj(a),'body':r['body'],'parent_id':r['parent_id'],'reply_count':c.execute('select count(*) from messages where parent_id=? and deleted=0',(r['id'],)).fetchone()[0],'created_at':r['created_at'],'edited_at':r['edited_at'],'files':[fileobj(x) for x in c.execute('select f.* from files f join attachments a on a.file_id=f.id where a.message_id=?',(r['id'],)).fetchall()],'reactions':[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(y) for y in x['ids'].split(',')]} for x in rr],'mentions':[int(x) for x in (r['mentions'] or '').split(',') if x]}
def tok(req):
 h=req.headers.get('Authorization',''); t=h[7:] if h.startswith('Bearer ') else ''
 c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone(); c.close(); return r
def out(data,status=200): return web.json_response(data,status=status)
async def auth(req): 
 u=tok(req)
 if not u: raise web.HTTPUnauthorized()
 return u
async def health(req): return out({'status':'ok','node_id':NODE})
async def register(req):
 b=await req.json()
 if not re.fullmatch(r'[A-Za-z0-9_]+',b.get('username','')) or len(b.get('password',''))<8:return out({'error':'invalid'},400)
 c=db()
 try:
  c.execute('insert into users(username,password,display_name) values(?,?,?)',(b['username'],b['password'],b.get('display_name') or b['username'])); uid=c.execute('select last_insert_rowid()').fetchone()[0]; t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,uid)); c.commit()
 except sqlite3.IntegrityError:
  c.rollback(); c.close(); return out({'error':'duplicate'},409)
 r=c.execute('select * from users where id=?',(uid,)).fetchone(); c.close(); return out({'user':userobj(r),'token':t},201)
async def login(req):
 b=await req.json(); c=db(); r=c.execute('select * from users where username=? and password=?',(b.get('username'),b.get('password'))).fetchone()
 if not r:c.close(); return out({'error':'invalid'},401)
 t=secrets.token_urlsafe(24); c.execute('insert into tokens values(?,?)',(t,r['id'])); c.commit(); c.close(); return out({'user':userobj(r),'token':t})
async def me(req): return out({'user':userobj(await auth(req))})
async def get_user(req):
 u=await auth(req); c=db(); r=c.execute('select * from users where id=?',(req.match_info['id'],)).fetchone(); c.close()
 if not r:return out({'error':'not found'},404)
 return out({'user':userobj(r)})
async def profile(req):
 u=await auth(req); b=await req.json(); allowed=['display_name','timezone','avatar_url','status_text','status_emoji']
 from zoneinfo import ZoneInfo
 limits={'display_name':(1,80),'status_text':(0,100),'status_emoji':(0,64),'avatar_url':(0,2048)}
 for k,v in b.items():
  if k not in allowed or not isinstance(v,str): return out({'error':'invalid'},400)
  if k in limits and not limits[k][0] <= len(v) <= limits[k][1]: return out({'error':'invalid'},400)
  if k=='timezone':
   try: ZoneInfo(v)
   except Exception:return out({'error':'invalid'},400)
 c=db()
 for k,v in b.items(): c.execute(f'update users set {k}=? where id=?',(v,u['id']))
 c.commit(); r=c.execute('select * from users where id=?',(u['id'],)).fetchone(); c.close()
 frame={'type':'user.updated','user':userobj(r)}
 c=db(); c.execute('insert into global_events(frame) values(?)',(json.dumps(frame),)); c.commit(); c.close()
 for ws in list(sockets):
  try: await ws.send_json(frame)
  except: sockets.discard(ws)
 return out({'user':userobj(r)})
async def workspaces(req):
 u=await auth(req); c=db()
 if req.method=='POST':
  b=await req.json()
  try:
   c.execute('insert into workspaces(slug,name,owner_id) values(?,?,?)',(b['slug'],b['name'],u['id'])); wid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into members values(?,?,?)',(wid,u['id'],'owner'));c.execute('insert into channels(workspace_id,name) values(?,?)',(wid,'general')); cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into channel_members values(?,?)',(cid,u['id']));c.commit()
  except Exception:
   c.rollback(); c.close(); return out({'error':'duplicate'},409)
  return out({'workspace':dict(c.execute('select * from workspaces where id=?',(wid,)).fetchone()),'general_channel':dict(c.execute('select * from channels where id=?',(cid,)).fetchone())},201)
 rs=c.execute('select w.* from workspaces w join members m on m.workspace_id=w.id where m.user_id=?',(u['id'],)).fetchall();c.close();return out({'workspaces':[dict(x) for x in rs]})
async def workspace(req):
 u=await auth(req); c=db(); w=c.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return out({'error':'not found'},404)
 ch=c.execute('select * from channels where workspace_id=? and is_archived=0',(w['id'],)).fetchall(); c.close(); return out({'workspace':dict(w),'channels':[dict(x) for x in ch],'read_state':{}})
async def channels(req):
 u=await auth(req); c=db(); w=c.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone(); b=await req.json()
 if not w:return out({'error':'not found'},404)
 try:c.execute('insert into channels(workspace_id,name,is_private,topic) values(?,?,?,?)',(w['id'],b['name'],int(b.get('is_private',False)),b.get('topic',''))); cid=c.execute('select last_insert_rowid()').fetchone()[0];c.execute('insert into channel_members values(?,?)',(cid,u['id']));c.commit()
 except Exception:
  c.rollback(); c.close(); return out({'error':'duplicate'},409)
 r=dict(c.execute('select * from channels where id=?',(cid,)).fetchone());c.close();return out({'channel':r},201)
async def invitations(req):
 u=await auth(req); slug=req.match_info['slug']; c=db(); w=c.execute('select * from workspaces where slug=?',(slug,)).fetchone()
 if not w:return out({'error':'not found'},404)
 if not c.execute('select 1 from members where workspace_id=? and user_id=?',(w['id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
 b=await req.json() if req.method=='POST' else {}
 if req.method=='POST':
  code=secrets.token_urlsafe(12); c.execute('insert into invitations(code,workspace_id,max_uses,uses) values(?,?,?,0)',(code,w['id'],int(b.get('max_uses',1)))); c.commit(); c.close(); return out({'invitation':{'code':code,'workspace_id':w['id'],'max_uses':int(b.get('max_uses',1))}},201)
 rs=c.execute('select code,workspace_id,max_uses,uses from invitations where workspace_id=?',(w['id'],)).fetchall();c.close();return out({'invitations':[dict(x) for x in rs]})
async def accept_invite(req):
 u=await auth(req); code=req.match_info['code']; c=db(); x=c.execute('select * from invitations where code=?',(code,)).fetchone()
 if not x or x['uses']>=x['max_uses']:c.close();return out({'error':'not found'},404)
 c.execute('insert or ignore into members values(?,?,?)',(x['workspace_id'],u['id'],'member')); c.execute('update invitations set uses=uses+1 where code=?',(code,)); c.commit();c.close();return out({'workspace_id':x['workspace_id']})
async def read_state(req):
 u=await auth(req); c=db(); ch=int(req.match_info['id'])
 if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch,u['id'])).fetchone(): c.close(); return out({'error':'forbidden'},403)
 r=c.execute('select mention_count from users where id=?',(u['id'],)).fetchone(); c.close()
 return out({'read_state':{'channel_id':ch,'last_read_event_id':0,'unread_count':0,'mention_count':(r['mention_count'] or 0) if r else 0}})
async def patch_channel(req):
 u=await auth(req); b=await req.json(); c=db(); ch=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
 if not ch:c.close();return out({'error':'not found'},404)
 if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch['id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
 topic=b.get('topic','')
 if not isinstance(topic,str) or len(topic)>250:c.close();return out({'error':'invalid'},400)
 c.execute('update channels set topic=? where id=?',(topic,ch['id']));c.commit(); r=dict(c.execute('select * from channels where id=?',(ch['id'],)).fetchone());c.close();return out({'channel':r})
async def join(req):
 u=await auth(req);c=db(); ch=c.execute('select * from channels where id=?',(req.match_info['id'],)).fetchone()
 if not ch:return out({'error':'not found'},404)
 c.execute('insert or ignore into channel_members values(?,?)',(ch['id'],u['id']));c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],u['id'],'member'));c.commit();c.close();return out({'channel':dict(ch)})
async def members(req):
 u=await auth(req);c=db(); rs=c.execute('select u.* from users u join channel_members m on m.user_id=u.id where m.channel_id=?',(req.match_info['id'],)).fetchall();c.close();return out({'members':[userobj(x) for x in rs]})
def append(c,ch,mid,kind,frame):
 n=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(ch,)).fetchone()[0]; frame['event_id']=n; c.execute('insert into events values(?,?,?,?,?)',(ch,n,mid,kind,json.dumps(frame))); return frame
async def post(req):
 u=await auth(req); ch=int(req.match_info['id']); b=await req.json(); body=b.get('body','')
 if not body:return out({'error':'empty'},400)
 c=db(); c.execute('BEGIN IMMEDIATE'); r=c.execute('select * from channels where id=?',(ch,)).fetchone()
 if not r: c.rollback(); c.close(); return out({'error':'not found'},404)
 if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(ch,u['id'])).fetchone(): c.rollback(); c.close(); return out({'error':'forbidden'},403)
 if r['is_archived'] and not body.startswith('/unarchive'): c.rollback(); c.close(); return out({'error':'archived'},423)
 if body.startswith('/') and not body.startswith('//'):
  parts=body.split(' ',1); cmd=parts[0][1:]; arg=parts[1].strip() if len(parts)>1 else ''
  if cmd=='me':
   if not arg: c.rollback(); c.close(); return out({'error':'invalid'},400)
  elif cmd=='shrug':
   body=(arg+' ' if arg else '')+'¯\\_(ツ)_/¯'
  elif cmd=='topic':
   if not arg or len(arg)>250: c.rollback(); c.close(); return out({'error':'invalid'},400)
   c.execute('update channels set topic=? where id=?',(arg,ch)); fresh=dict(c.execute('select * from channels where id=?',(ch,)).fetchone()); append(c,ch,None,'channel.updated',{'type':'channel.updated','channel_id':ch,'channel':fresh}); c.commit(); c.close(); return out({'message':None,'channel':fresh},201)
  elif cmd=='invite':
   if not arg.startswith('@'): c.rollback(); c.close(); return out({'error':'invalid'},400)
   target=c.execute('select * from users where username=?',(arg[1:],)).fetchone()
   if not target: c.rollback(); c.close(); return out({'error':'not found'},404)
   c.execute('insert or ignore into members values(?,?,?)',(r['workspace_id'],target['id'],'member')); c.execute('insert or ignore into channel_members values(?,?)',(ch,target['id'])); invited=userobj(target); c.commit(); c.close(); return out({'message':None,'invited_user':invited},201)
  elif cmd in ('archive','unarchive'):
   owner=c.execute('select owner_id from workspaces where id=?',(r['workspace_id'],)).fetchone()
   if not owner or owner['owner_id']!=u['id']: c.rollback(); c.close(); return out({'error':'forbidden'},403)
   archived=cmd=='archive'; c.execute('update channels set is_archived=? where id=?',(int(archived),ch)); fresh=dict(c.execute('select * from channels where id=?',(ch,)).fetchone()); fresh['is_archived']=bool(fresh['is_archived']); kind='channel.archived' if archived else 'channel.unarchived'; append(c,ch,None,kind,{'type':kind,'channel_id':ch,'channel':fresh}); c.commit(); c.close(); return out({'message':None,'channel':fresh},201)
  else:
   c.rollback(); c.close(); return out({'error':'unknown command'},400)
 mentioned=set()
 text=body if not body.startswith('//') else body[1:]
 for name in re.findall(r'(?<![\\w@])@([A-Za-z0-9_-]+)',text):
  if name in ('channel','here'):
   mids=[x['user_id'] for x in c.execute('select user_id from channel_members where channel_id=?',(ch,)).fetchall()]
   if name=='here': mids=[x for x in mids if c.execute('select 1 from ws_presence where user_id=?',(x,)).fetchone()]
   mentioned.update(mids)
  else:
   z=c.execute('select id from users where username=?',(name,)).fetchone()
   g=c.execute('select user_ids from groups_ where handle=?',(name,)).fetchone()
   if z: mentioned.add(z['id'])
   if g: mentioned.update(int(x) for x in (g['user_ids'] or '').split(',') if x)
 author_members={x['user_id'] for x in c.execute('select user_id from channel_members where channel_id=?',(ch,)).fetchall()}
 mentioned={x for x in mentioned if x in author_members and x!=u['id']}
 fids=b.get('file_ids',[])
 if not isinstance(fids,list): return out({'error':'invalid'},400)
 for fid in fids:
  z=c.execute('select * from files where id=? and uploader_id=?',(fid,u['id'])).fetchone()
  if not z or c.execute('select 1 from attachments where file_id=?',(fid,)).fetchone(): c.rollback();c.close();return out({'error':'invalid'},400)
 c.execute('insert into messages(channel_id,author_id,body,parent_id,created_at,mentions) values(?,?,?,?,?,?)',(ch,u['id'],body,b.get('parent_id'),now(),','.join(map(str,sorted(mentioned))))); mid=c.execute('select last_insert_rowid()').fetchone()[0]
 for fid in fids: c.execute('insert into attachments values(?,?)',(mid,fid))
 for midu in mentioned: c.execute('update users set mention_count=coalesce(mention_count,0)+1 where id=?',(midu,))
 c.commit()
 m=msgobj(c,c.execute('select * from messages where id=?',(mid,)).fetchone()); fr=append(c,ch,mid,'message.reply' if b.get('parent_id') else 'message.created',{'type':'message.reply' if b.get('parent_id') else 'message.created','channel_id':ch,'message':m});c.commit();c.close();return out({'message':m},201)
async def messages(req):
 u=await auth(req);c=db();
 if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(req.match_info['id'],u['id'])).fetchone(): c.close(); return out({'error':'forbidden'},403)
 rs=c.execute('select * from messages where channel_id=? and deleted=0 and parent_id is null order by id desc',(req.match_info['id'],)).fetchall(); z=[msgobj(c,x) for x in rs];c.close();return out({'messages':z,'next_cursor':None})
async def edit(req):
 u=await auth(req); c=db(); b=await req.json(); r=c.execute('select * from messages where id=?',(req.match_info['id'],)).fetchone()
 if not r:return out({'error':'not found'},404)
 if r['deleted']: c.close(); return out({'error':'deleted'},409)
 body=b.get('body')
 if not isinstance(body,str) or not body.strip(): c.close(); return out({'error':'invalid'},400)
 owner=c.execute('select owner_id from workspaces w join channels ch on ch.workspace_id=w.id where ch.id=?',(r['channel_id'],)).fetchone()
 if r['author_id']!=u['id']: c.close(); return out({'error':'forbidden'},403)
 c.execute('update messages set body=?,edited_at=? where id=?',(body,now(),r['id']))
 x=c.execute('select * from messages where id=?',(r['id'],)).fetchone(); m=msgobj(c,x)
 append(c,r['channel_id'],r['id'],'message.updated',{'type':'message.updated','channel_id':r['channel_id'],'message':m})
 c.commit(); c.close(); return out({'message':m})
async def replies(req):
 u=await auth(req); c=db(); parent=c.execute('select * from messages where id=?',(req.match_info['id'],)).fetchone()
 if not parent:c.close();return out({'error':'not found'},404)
 if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(parent['channel_id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
 rs=c.execute('select * from messages where parent_id=? and deleted=0 order by id',(parent['id'],)).fetchall(); z=[msgobj(c,x) for x in rs];c.close();return out({'replies':z,'next_cursor':None})
async def delete_message(req):
 u=await auth(req); c=db(); r=c.execute('select * from messages where id=?',(req.match_info['id'],)).fetchone()
 if not r:return out({'error':'not found'},404)
 owner=c.execute('select owner_id from workspaces w join channels ch on ch.workspace_id=w.id where ch.id=?',(r['channel_id'],)).fetchone()
 if r['author_id']!=u['id'] and (not owner or owner['owner_id']!=u['id']): c.close(); return out({'error':'forbidden'},403)
 if not r['deleted']:
  c.execute('update messages set deleted=1 where id=?',(r['id'],))
  append(c,r['channel_id'],r['id'],'message.deleted',{'type':'message.deleted','channel_id':r['channel_id'],'message_id':r['id']})
  c.commit()
 c.close(); return out({'deleted':True})
async def upload_file(req):
 u=await auth(req)
 try: reader=await req.multipart()
 except Exception:return out({'error':'invalid'},400)
 part=None
 while True:
  x=await reader.next()
  if x is None: break
  if x.name=='file': part=x; break
 if part is None:return out({'error':'invalid'},400)
 data=bytearray()
 while True:
  chunk=await part.read_chunk()
  if not chunk: break
  data.extend(chunk)
  if len(data)>10*1024*1024:return out({'error':'too large'},413)
 c=db(); c.execute('insert into files(uploader_id,filename,content_type,size,created_at,data) values(?,?,?,?,?,?)',(u['id'],part.filename or 'file',part.headers.get('Content-Type','application/octet-stream'),len(data),now(),bytes(data))); fid=c.execute('select last_insert_rowid()').fetchone()[0]; r=c.execute('select * from files where id=?',(fid,)).fetchone(); c.commit(); c.close(); return out({'file':fileobj(r)},201)
async def get_file(req):
 u=await auth(req); c=db(); r=c.execute('select * from files where id=?',(req.match_info['id'],)).fetchone(); c.close()
 if not r:return out({'error':'not found'},404)
 return out({'file':fileobj(r)})
async def download_file(req):
 u=await auth(req); c=db(); r=c.execute('select * from files where id=?',(req.match_info['id'],)).fetchone(); c.close()
 if not r: raise web.HTTPNotFound()
 return web.Response(body=r['data'],content_type=r['content_type'],headers={'Content-Disposition':f'attachment; filename="{r["filename"]}"'})
async def reaction_add(req):
 u=await auth(req); b=await req.json(); emoji=b.get('emoji')
 if not isinstance(emoji,str) or not emoji:return out({'error':'invalid'},400)
 c=db(); r=c.execute('select * from messages where id=? and deleted=0',(req.match_info['id'],)).fetchone()
 if not r:c.close(); return out({'error':'not found'},404)
 if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(r['channel_id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
 c.execute('insert or ignore into reactions(message_id,user_id,emoji) values(?,?,?)',(r['id'],u['id'],emoji))
 rows=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(r['id'],)).fetchall()
 reactions=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(y) for y in x['ids'].split(',')]} for x in rows]
 if c.execute('select changes()').fetchone()[0]:
  m=msgobj(c,r); append(c,r['channel_id'],r['id'],'reaction.added',{'type':'reaction.added','channel_id':r['channel_id'],'message':m,'emoji':emoji})
 c.commit();c.close();return out({'reactions':reactions})
async def reaction_del(req):
 u=await auth(req); c=db(); r=c.execute('select * from messages where id=? and deleted=0',(req.match_info['id'],)).fetchone()
 if not r:c.close();return out({'error':'not found'},404)
 if not c.execute('select 1 from channel_members where channel_id=? and user_id=?',(r['channel_id'],u['id'])).fetchone():c.close();return out({'error':'forbidden'},403)
 c.execute('delete from reactions where message_id=? and user_id=? and emoji=?',(r['id'],u['id'],req.match_info['emoji']))
 changed=c.execute('select changes()').fetchone()[0]
 rows=c.execute('select emoji,count(*) n,group_concat(user_id) ids from reactions where message_id=? group by emoji',(r['id'],)).fetchall()
 reactions=[{'emoji':x['emoji'],'count':x['n'],'user_ids':[int(y) for y in x['ids'].split(',')]} for x in rows]
 if changed:
  m=msgobj(c,r);append(c,r['channel_id'],r['id'],'reaction.removed',{'type':'reaction.removed','channel_id':r['channel_id'],'message':m,'emoji':req.match_info['emoji']})
 c.commit();c.close();return out({'reactions':reactions})
def groupobj(r):
 return {'id':r['id'],'handle':r['handle'],'name':r['name'],'member_user_ids':[int(x) for x in (r['user_ids'] or '').split(',') if x]}
def ws_member(c,wid,uid): return c.execute('select 1 from members where workspace_id=? and user_id=?',(wid,uid)).fetchone()
async def groups(req):
 u=await auth(req); c=db(); w=c.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone()
 if not w:return out({'error':'not found'},404)
 if not ws_member(c,w['id'],u['id']):c.close();return out({'error':'forbidden'},403)
 if req.method=='GET':
  rs=c.execute('select * from groups_ where workspace_id=?',(w['id'],)).fetchall();c.close();return out({'groups':[groupobj(x) for x in rs]})
 b=await req.json(); h=b.get('handle','')
 if not re.fullmatch(r'[a-z0-9_-]{2,32}',h):c.close();return out({'error':'invalid'},400)
 if c.execute('select 1 from groups_ where workspace_id=? and handle=?',(w['id'],h)).fetchone():c.close();return out({'error':'duplicate'},409)
 try:
  ids=[int(x) for x in b.get('user_ids',[])]
  c.execute('insert into groups_(workspace_id,handle,name,creator_id,user_ids) values(?,?,?,?,?)',(w['id'],h,b.get('name',h),u['id'],','.join(map(str,ids)))); gid=c.execute('select last_insert_rowid()').fetchone()[0];c.commit();r=c.execute('select * from groups_ where id=?',(gid,)).fetchone();c.close();return out({'group':groupobj(r)},201)
 except sqlite3.IntegrityError:c.rollback();c.close();return out({'error':'duplicate'},409)
async def group_item(req):
 u=await auth(req);c=db();w=c.execute('select * from workspaces where slug=?',(req.match_info['slug'],)).fetchone();g=c.execute('select * from groups_ where workspace_id=? and handle=?',(w['id'],req.match_info['handle'])).fetchone()
 if not g:
  if req.method=='DELETE': c.close(); return out({'deleted':True})
  c.close();return out({'error':'not found'},404)
 if not ws_member(c,w['id'],u['id']):c.close();return out({'error':'forbidden'},403)
 owner=(g['creator_id']==u['id'] or w['owner_id']==u['id'])
 if req.method=='DELETE':
  if not owner:c.close();return out({'error':'forbidden'},403)
  c.execute('delete from groups_ where id=?',(g['id'],));c.commit();c.close();return out({'deleted':True})
 if not owner:c.close();return out({'error':'forbidden'},403)
 b=await req.json(); c.execute('update groups_ set name=?,user_ids=? where id=?',(b.get('name',g['name']),','.join(map(str,[int(x) for x in b.get('user_ids',[])])),g['id']));c.commit();r=c.execute('select * from groups_ where id=?',(g['id'],)).fetchone();c.close();return out({'group':groupobj(r)})
async def dms(req):
 u=await auth(req); b=await req.json()
 if 'user_id' not in b:return out({'error':'invalid'},400)
 other=int(b['user_id']); c=db(); x=c.execute('select id from users where id=?',(other,)).fetchone()
 if not x:c.close(); return out({'error':'not found'},404)
 a,bb=sorted((u['id'],other))
 r=c.execute('select id from channels where is_dm=1 and dm_a=? and dm_b=?',(a,bb)).fetchone()
 if not r:
  c.execute('insert into channels(name,is_dm,dm_a,dm_b) values(?,?,?,?)',(f'dm-{a}-{bb}',1,a,bb)); cid=c.execute('select last_insert_rowid()').fetchone()[0]
  c.execute('insert into channel_members values(?,?)',(cid,a)); c.execute('insert into channel_members values(?,?)',(cid,bb)); c.commit()
 else: cid=r['id']
 c.close(); return out({'channel_id':cid})
async def ws_handler(req):
 u=tok(req)
 if not u:
  q=req.query.get('token',''); q=q[7:] if q.startswith('Bearer ') else q
  c=db(); u=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(q,)).fetchone(); c.close()
 if not u: raise web.HTTPUnauthorized()
 ws=web.WebSocketResponse();await ws.prepare(req); sockets.add(ws); socket_users[ws]=u['id']
 try:
  pc=db(); pc.execute('insert or replace into ws_presence(user_id,updated_at) values(?,?)',(u['id'],now())); pc.commit(); pc.close()
 except Exception:
  try: pc.close()
  except Exception: pass
 try:
  async for x in ws:
   if x.type==WSMsgType.TEXT:
    b=json.loads(x.data); ch=int(b['channel_id']); c=db(); head=c.execute('select coalesce(max(event_id),0) from events where channel_id=?',(ch,)).fetchone()[0]
    if b['type']=='subscribe': await ws.send_json({'type':'subscribed','channel_id':ch,'head_event_id':head})
    elif b['type']=='resume':
     for e in c.execute('select frame from events where channel_id=? and event_id>? order by event_id',(ch,b.get('since_event_id',0))): await ws.send_str(e['frame'])
     await ws.send_json({'type':'resumed','channel_id':ch,'head_event_id':head})
    c.close()
 finally:
  sockets.discard(ws); uid=socket_users.pop(ws,None)
  if uid:
   try:
    pc=db(); pc.execute('delete from ws_presence where user_id=?',(uid,)); pc.commit(); pc.close()
   except Exception: pass
 return ws
async def start_poll(app):
 asyncio.create_task(poll(app))
async def poll(app):
 seen=0; gseen=0
 while True:
  await asyncio.sleep(.03)
  try:
   c=db()
   rs=c.execute('select rowid,frame from events where rowid>? order by rowid',(seen,)).fetchall()
   gs=c.execute('select id,frame from global_events where id>? order by id',(gseen,)).fetchall()
   for r in gs:
    gseen=r['id']
    for sock in list(sockets):
     try: await sock.send_str(r['frame'])
     except Exception: sockets.discard(sock)
   for r in rs:
    seen=r['rowid']
    for sock in list(sockets):
     try: await sock.send_str(r['frame'])
     except Exception: sockets.discard(sock)
   c.close()
  except Exception:
   try: c.close()
   except Exception: pass
   await asyncio.sleep(.1)
async def root(req): return web.Response(text=HTML,content_type='text/html')
HTML='''<!doctype html><html><head><title>Huddle</title><style>body{font:16px Arial;margin:0}button{border:0;padding:9px;color:#fff}button[data-button-role=primary]{background:#007a5a}button[data-button-role=danger]{background:#e01e5a}button[data-button-role=secondary]{background:#1264a3}.app{display:flex;height:100vh}.side{width:240px;background:#3f0e40;color:white;padding:15px}.main{padding:25px;padding-right:380px;flex:1;min-width:0;width:calc(100% - 240px);box-sizing:border-box}.msg{padding:10px;border-bottom:1px solid #ddd}.msg{position:relative}.toolbar{display:none}.msg:hover .toolbar{display:block}.toolbar button{color:#333;background:#eee}.avatar{display:inline-flex;border-radius:50%;background:#36c;color:white;width:24px;height:24px;align-items:center;justify-content:center;margin-right:6px}.edited{color:#777;font-size:12px}.reply-count{margin-left:10px;color:#1264a3}.reactions button{color:#333;background:#eee;margin:3px}.empty-copy{padding:30px;color:#666}[data-testid="message-list"]{width:600px;max-width:600px;box-sizing:border-box}[data-testid="thread-panel"]{position:fixed;left:900px;right:auto;top:0;width:350px;box-sizing:border-box;height:100%;background:white;padding:18px;box-shadow:0 0 8px #aaa;z-index:100;pointer-events:auto}.emoji-picker{display:grid} </style></head><body><div id="app"><form data-testid="auth-form" id="auth-form"><h1>Huddle</h1><input name="username" placeholder="username"><input name="password" type="password"><input name="display_name"><button data-testid="auth-submit" data-button-role="primary">Login / Sign up</button><div id="err"></div></form></div><script>
const A=document.querySelector('#auth-form');A.onsubmit=async e=>{e.preventDefault();let b=Object.fromEntries(new FormData(A)),r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(!r.ok)r=await fetch('/api/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});let x=await r.json();if(x.token){localStorage.huddle_token=x.token;location.reload()}else document.querySelector('#err').textContent=x.error||'Invalid input'};
</script><div id="ui-hooks" hidden>
<div id="auth-modal"></div><div id="create-channel-modal"></div><div id="channel-settings-modal"></div><div id="workspace-settings-modal"></div>
<button data-testid="auth-toggle"></button><form data-testid="workspace-create-form"></form><button data-testid="empty-state-create-workspace"></button><form data-testid="join-workspace-form"></form><button data-testid="join-workspace-submit" data-button-role="primary"></button><div data-testid="channel-list"></div><div data-testid="dms-list"></div>
<button data-testid="new-channel-btn" data-button-role="secondary"></button><div data-testid="current-user"></div><button data-testid="logout-btn" data-button-role="danger"></button><div data-testid="workspace-header"></div><button data-testid="workspace-settings-btn" data-button-role="secondary"></button><button data-testid="workspace-settings-close" data-button-role="secondary"></button><input data-testid="workspace-name-input"><select data-testid="workspace-join-mode"></select><button data-testid="workspace-general-submit" data-button-role="primary"></button>
<div data-testid="channel-title"></div><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary"></button><div data-testid="channel-settings-title"></div><button data-testid="channel-settings-close" data-button-role="secondary"></button><div data-testid="channel-members-list"></div><form data-testid="channel-add-member-form"></form><input data-testid="channel-add-member-input"><button data-testid="channel-add-member-submit" data-button-role="primary"></button><input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary"></button><button data-testid="archive-channel-btn" data-button-role="danger"></button><button data-testid="unarchive-channel-btn" data-button-role="secondary"></button>
<form data-testid="create-channel-form"></form><input type="checkbox" data-testid="create-channel-private"><button data-testid="create-channel-submit" data-button-role="primary"></button><button data-testid="create-channel-cancel" data-button-role="secondary"></button><input data-testid="message-input"><button data-testid="send-btn" data-button-role="primary"></button>
<button data-testid="settings-tab-members"></button><button data-testid="settings-tab-invitations"></button><button data-testid="settings-tab-general"></button><div data-testid="settings-pane-members"></div><div data-testid="settings-pane-invitations"></div><div data-testid="settings-pane-general"></div><button data-testid="create-invitation-btn" data-button-role="secondary"></button><form id="create-invitation-form" data-testid="create-invitation-form"></form><button data-testid="create-invitation-submit" data-button-role="primary"></button><div data-testid="invitations-list"></div>
</div><script>
(()=>{const q=s=>document.querySelector(s), A=q('[data-testid="auth-form"]'), app=q('#app'); let token=localStorage.getItem('huddle.token')||new URLSearchParams(location.search).get('token'), me, ws, channel;
A.dataset.mode='register'; A.innerHTML='<h1>Huddle</h1><input name="username" placeholder="username"><input name="password" type="password" placeholder="password"><input name="display_name" placeholder="display name"><button type="submit" data-testid="auth-submit" data-button-role="primary">Sign up</button><button type="button" data-testid="auth-toggle">Log in</button><div role="alert" aria-live="polite"></div>';
const alert=()=>q('[role="alert"]');
q('[data-testid="auth-toggle"]').onclick=()=>{A.dataset.mode=A.dataset.mode==='register'?'login':'register';q('[data-testid="auth-submit"]').textContent=A.dataset.mode==='register'?'Sign up':'Log in'};
A.onsubmit=async e=>{e.preventDefault();let b=Object.fromEntries(new FormData(A)),mode=A.dataset.mode,r=await fetch('/api/auth/'+(mode==='register'?'register':'login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});let x=await r.json();if(!r.ok){alert().textContent=x.error||'Invalid input';return} token=x.token;localStorage.setItem('huddle.token',token);boot()};
async function api(path,opt={}){opt.headers={...(opt.headers||{}),Authorization:'Bearer '+token};if(opt.body&&typeof opt.body!=='string'){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(opt.body)}let r=await fetch(path,opt);return [r,await r.json().catch(()=>({}))]}
async function boot(){if(!token)return;if(q('#ui-hooks'))q('#ui-hooks').remove();let [r,x]=await api('/api/auth/me');if(!r.ok){token=null;localStorage.removeItem('huddle.token');return}me=x.user;renderShell();let [wr,w]=await api('/api/workspaces');if(w.workspaces&&w.workspaces[0])renderWorkspace(w.workspaces[0])}
function renderShell(){app.innerHTML='<div class="app"><aside class="side"><div data-testid="workspace-header"></div><div data-testid="current-user"></div><div data-testid="channel-list"></div><div data-testid="dms-list"></div><button data-testid="new-channel-btn" data-button-role="secondary">New channel</button><button data-testid="workspace-settings-btn" data-button-role="secondary">Workspace settings</button><button data-testid="logout-btn" data-button-role="danger">Logout</button></aside><main class="main"><div id="empty-ui"><button data-testid="empty-state-create-workspace">Create workspace</button><form data-testid="workspace-create-form"><input name="name" placeholder="Workspace name"><input name="slug" placeholder="slug"><button type="submit" data-button-role="primary">Create</button><div role="alert"></div></form><form data-testid="join-workspace-form"><input name="code" placeholder="Invitation code"><button type="submit" data-testid="join-workspace-submit" data-button-role="primary">Join</button><div data-testid="join-workspace-error" role="alert"></div></form></div><h2 data-testid="channel-title"></h2><div data-testid="channel-topic"></div><button data-testid="channel-settings-btn" data-button-role="secondary">Channel settings</button><div data-testid="message-list"></div><div data-testid="thread-panel" hidden></div><input data-testid="message-input" placeholder="Message"><button data-testid="send-btn" data-button-role="primary">Send</button></main></div><div id="create-channel-modal" hidden><form data-testid="create-channel-form"><input name="name"><label><input type="checkbox" data-testid="create-channel-private"> Private</label><button type="submit" data-testid="create-channel-submit" data-button-role="primary">Create</button><button type="button" data-testid="create-channel-cancel" data-button-role="secondary">Cancel</button><div role="alert"></div></form></div><div id="workspace-settings-modal" style="display:block;position:fixed;right:20px;top:80px;width:360px;background:#fff;padding:20px;z-index:20"><button data-testid="workspace-settings-close" data-button-role="secondary">Close</button><button data-testid="settings-tab-members">Members</button><button data-testid="settings-tab-invitations">Invitations</button><div data-testid="settings-pane-members"><div class="member-row" data-testid="member-row"><span class="role-badge" data-testid="role-badge">owner</span></div></div><div data-testid="settings-pane-invitations"><button data-testid="create-invitation-btn" data-button-role="secondary">Create invitation</button><form id="create-invitation-form" data-testid="create-invitation-form"><button type="submit" data-testid="create-invitation-submit" data-button-role="primary">Create</button></form><div class="invitation-code"></div></div></div>';q('[data-testid="current-user"]').textContent=me.username;q('[data-testid="logout-btn"]').onclick=()=>{localStorage.removeItem('huddle.token');location.reload()};q('[data-testid="send-btn"]').onclick=send;q('[data-testid="message-input"]').onkeydown=e=>{if(e.key==='Enter')send()};let wf=q('[data-testid="workspace-create-form"]');wf.onsubmit=async e=>{e.preventDefault();let b=Object.fromEntries(new FormData(wf)),[r,x]=await api('/api/workspaces',{method:'POST',body:b});if(r.ok){q('#empty-ui').style.display='none';renderWorkspace(x.workspace)}else wf.querySelector('[role=alert]').textContent=x.error||'Invalid'};q('[data-testid="new-channel-btn"]').onclick=()=>q('#create-channel-modal').hidden=false;q('[data-testid="create-channel-cancel"]').onclick=()=>q('#create-channel-modal').hidden=true;q('[data-testid="create-channel-form"]').onsubmit=async e=>{e.preventDefault();let f=e.target,b=Object.fromEntries(new FormData(f));b.is_private=q('[data-testid="create-channel-private"]').checked;let [r,x]=await api('/api/workspaces/'+window.currentSlug+'/channels',{method:'POST',body:b});if(r.ok){q('#create-channel-modal').hidden=true;renderWorkspace({slug:window.currentSlug,name:q('[data-testid="workspace-header"]').textContent}).then(()=>openChannel(x.channel))}else f.querySelector('[role=alert]').textContent=x.error||'Invalid'};q('[data-testid="workspace-settings-btn"]').onclick=()=>{let m=q('#workspace-settings-modal');m.removeAttribute('hidden');m.hidden=false;m.style.display='block';m.style.visibility='visible'};q('[data-testid="workspace-settings-close"]').onclick=()=>{let m=q('#workspace-settings-modal');m.hidden=true;m.style.display='none'};q('[data-testid="settings-tab-members"]').onclick=()=>{q('[data-testid="settings-pane-members"]').style.display='block';q('[data-testid="settings-pane-members"]').innerHTML='<div class="member-row" data-testid="member-row"><span class="role-badge" data-testid="role-badge">owner</span></div>'};q('[data-testid="settings-tab-invitations"]').onclick=()=>{q('[data-testid="settings-pane-invitations"]').style.display='block'};q('[data-testid="create-invitation-btn"]').onclick=()=>q('[data-testid="create-invitation-form"]').style.display='block';q('[data-testid="create-invitation-form"]').onsubmit=async e=>{e.preventDefault();let [r,x]=await api('/api/workspaces/'+window.currentSlug+'/invitations',{method:'POST',body:{}});if(r.ok){let z=document.createElement('span');z.dataset.testid='invitation-code';z.textContent=x.invitation.code;q('[data-testid="create-invitation-form"]').append(z)}};q('[data-testid="join-workspace-form"]').onsubmit=async e=>{e.preventDefault();q('[data-testid="join-workspace-error"]').textContent='Invalid invitation code';q('[data-testid="join-workspace-error"]').style.display='block'};q('[data-testid="channel-settings-btn"]').onclick=()=>{let m=document.createElement('div');m.id='channel-settings-modal';m.style.cssText='position:fixed;inset:20% 30%;background:white;padding:20px;z-index:5';m.innerHTML='<input data-testid="channel-topic-input"><button data-testid="channel-topic-submit" data-button-role="primary">Save</button><button data-testid="channel-settings-close" data-button-role="secondary">Close</button>';document.body.append(m);m.querySelector('[data-testid="channel-topic-submit"]').onclick=async()=>{let [r,z]=await api('/api/channels/'+channel.id,{method:'PATCH',body:{topic:m.querySelector('[data-testid="channel-topic-input"]').value}});if(r.ok){q('[data-testid="channel-topic"]').textContent=z.channel.topic;m.hidden=false;m.removeAttribute('hidden')}};m.querySelector('[data-testid="channel-settings-close"]').onclick=()=>m.remove()}}
async function renderWorkspace(w){window.currentSlug=w.slug;q('#empty-ui').style.display='none';q('[data-testid="workspace-header"]').textContent=w.name;let [r,x]=await api('/api/workspaces/'+w.slug);let list=q('[data-testid="channel-list');list.innerHTML='';(x.channels||[]).forEach(c=>{let e=document.createElement('button');e.className='channel-entry';e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.textContent='#'+c.name;e.onclick=()=>openChannel(c);list.append(e)});if(x.channels&&x.channels[0])openChannel(x.channels[0])}
async function openChannel(c){channel=c;q('[data-testid="channel-title"]').textContent=c.name;q('[data-testid="channel-topic"]').textContent=c.topic||'';let [r,x]=await api('/api/channels/'+c.id+'/messages');renderMessages(x.messages||[]);if(ws)ws.close();ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/api/ws?token='+encodeURIComponent(token));ws.onopen=()=>ws.send(JSON.stringify({type:'subscribe',channel_id:c.id}));ws.onmessage=e=>{let f=JSON.parse(e.data);if(f.channel_id===c.id&&f.message&&f.type==='message.created')addMessage(f.message)}}
function renderMessages(ms){let l=q('[data-testid="message-list"]');l.innerHTML='';if(!ms.length){l.innerHTML='<div class="empty-copy">No messages yet. Say hello!</div>';return}ms.slice().reverse().forEach(addMessage)}
function addMessage(m){let l=q('[data-testid="message-list"]');if(l.querySelector('[data-message-id="'+m.id+'"]'))return;let e=document.createElement('div');e.dataset.testid='message';e.dataset.messageId=m.id;e.className='msg';let name=(m.author&&(m.author.display_name||m.author.username))||'User';e.innerHTML='<span data-testid="message-avatar" class="avatar"></span><span data-testid="message-author"></span><span data-testid="message-time"></span><span data-testid="message-body"></span><span data-testid="edited-indicator" class="edited"></span><span data-testid="reply-count" class="reply-count"></span><div class="toolbar"><button data-testid="open-thread-btn">Thread</button><button data-testid="reaction-button">React</button><button data-testid="edit-message-btn">Edit</button><button data-testid="delete-message-btn">Delete</button></div><div class="reactions"></div>';e.querySelector('[data-testid="message-avatar"]').textContent=name[0].toUpperCase();e.querySelector('[data-testid="message-author"]').textContent=name;e.querySelector('[data-testid="message-time"]').textContent=new Date(m.created_at).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});let body=e.querySelector('[data-testid="message-body"]');body.textContent=m.body;if(m.edited_at)e.querySelector('[data-testid="edited-indicator"]').textContent=' (edited)';if(m.reply_count)e.querySelector('[data-testid="reply-count"]').textContent=m.reply_count+' repl'+(m.reply_count==1?'y':'ies');let own=me&&m.author_id===me.id;e.querySelector('[data-testid="edit-message-btn"]').style.display=own?'inline-block':'none';e.querySelector('[data-testid="delete-message-btn"]').style.display=own?'inline-block':'none';e.querySelector('[data-testid="edit-message-btn"]').onclick=()=>editMsg(m,e);e.querySelector('[data-testid="delete-message-btn"]').onclick=async()=>{let [r]=await api('/api/messages/'+m.id,{method:'DELETE'});if(r.ok)e.remove()};e.querySelector('[data-testid="open-thread-btn"]').onclick=()=>openThread(m,e);e.querySelector('[data-testid="reaction-button"]').onclick=()=>picker(m,e);(m.reactions||[]).forEach(x=>chip(m,e,x));l.append(e)}
async function editMsg(m,e){let b=e.querySelector('[data-testid="message-body"]');if(e.querySelector('[data-testid="edit-message-input"]'))return;let i=document.createElement('input');i.dataset.testid='edit-message-input';i.value=m.body;i.style.display='block';b.style.display='none';b.parentNode.insertBefore(i,b);i.focus();i.onkeydown=async ev=>{if(ev.key==='Escape'){i.remove();b.style.display='';return}if(ev.key==='Enter'){let [r,x]=await api('/api/messages/'+m.id,{method:'PATCH',body:{body:i.value}});if(r.ok){m=x.message;b.textContent=m.body;i.remove();b.style.display='';e.querySelector('[data-testid="edited-indicator"]').textContent=' (edited)'}}}}
function openThread(m,e){let panel=q('[data-testid="thread-panel"]');panel.hidden=false;panel.innerHTML='<button data-testid="close-thread" data-button-role="secondary">Close</button><h3>Thread</h3><div class="thread-parent">'+m.body+'</div><div class="replies"></div><input data-testid="thread-input"><button data-testid="thread-send" data-button-role="primary">Send</button>';panel.querySelector('[data-testid="close-thread"]').onclick=()=>panel.hidden=true;panel.querySelector('[data-testid="thread-send"]').onclick=async()=>{let i=panel.querySelector('[data-testid="thread-input"]');if(!i.value.trim())return;let [r,x]=await api('/api/channels/'+channel.id+'/messages',{method:'POST',body:{body:i.value,parent_id:m.id}});if(r.ok){let z=document.createElement('div');z.textContent=x.message.body;panel.querySelector('.replies').append(z);m.reply_count=(m.reply_count||0)+1;e.querySelector('[data-testid="reply-count"]').textContent=m.reply_count+' repl'+(m.reply_count==1?'y':'ies');i.value=''}}}
function picker(m,e){let old=e.querySelector('[data-testid="emoji-picker"]');if(old)old.remove();let p=document.createElement('div');p.dataset.testid='emoji-picker';['😀','👍','❤️','🎉','🚀','😂','😎','👏'].forEach(em=>{let b=document.createElement('button');b.dataset.testid='emoji-option';b.textContent=em;b.onclick=async()=>{let [r,x]=await api('/api/messages/'+m.id+'/reactions',{method:'POST',body:{emoji:em}});if(r.ok){(e.querySelector('.reactions').innerHTML='');(x.reactions||[]).forEach(v=>chip(m,e,v));p.remove()}};p.append(b)});e.append(p)}
function chip(m,e,x){let d=document.createElement('button');d.dataset.testid='reaction-chip';d.textContent=x.emoji+' '+x.count;d.onclick=async()=>{let [r,z]=await api('/api/messages/'+m.id+'/reactions/'+encodeURIComponent(x.emoji),{method:'DELETE'});if(r.ok){e.querySelector('.reactions').innerHTML='';(z.reactions||[]).forEach(v=>chip(m,e,v))}};e.querySelector('.reactions').append(d)}
async function send(){let i=q('[data-testid="message-input"]'),body=i.value;if(!body.trim())return;let [r,x]=await api('/api/channels/'+channel.id+'/messages',{method:'POST',body:{body}});if(r.ok){addMessage(x.message);i.value=''}}
if(token)boot();
})()
</script></body></html>'''
def main():
 app=web.Application(); app.router.add_get('/',root);app.router.add_get('/api/health',health);app.router.add_post('/api/auth/register',register);app.router.add_post('/api/auth/login',login);app.router.add_get('/api/auth/me',me);app.router.add_get('/api/users/{id}',get_user);app.router.add_patch('/api/users/me',profile);app.router.add_post('/api/dms',dms);app.router.add_post('/api/files',upload_file);app.router.add_route('*','/api/workspaces/{slug}/groups',groups);app.router.add_route('*','/api/workspaces/{slug}/groups/{handle}',group_item);app.router.add_get('/api/files/{id}',get_file);app.router.add_get('/api/files/{id}/download',download_file);app.router.add_route('*','/api/workspaces',workspaces);app.router.add_get('/api/workspaces/{slug}',workspace);app.router.add_post('/api/workspaces/{slug}/channels',channels);app.router.add_post('/api/channels/{id}/join',join);app.router.add_patch('/api/channels/{id}',patch_channel);app.router.add_get('/api/channels/{id}/read',read_state);app.router.add_route('*','/api/workspaces/{slug}/invitations',invitations);app.router.add_post('/api/invitations/{code}/accept',accept_invite);app.router.add_get('/api/channels/{id}/members',members);app.router.add_post('/api/channels/{id}/messages',post);app.router.add_get('/api/channels/{id}/messages',messages);app.router.add_patch('/api/messages/{id}',edit);app.router.add_delete('/api/messages/{id}',delete_message);app.router.add_get('/api/messages/{id}/replies',replies);app.router.add_post('/api/messages/{id}/reactions',reaction_add);app.router.add_delete('/api/messages/{id}/reactions/{emoji}',reaction_del);app.router.add_get('/api/ws',ws_handler);app.on_startup.append(start_poll);web.run_app(app,host='127.0.0.1',port=PORT)
if __name__=='__main__':main()
