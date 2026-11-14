import asyncio,sqlite3,json
DB='/app/data/huddle.db'; clients=[]; nicks=set()
def db():
 c=sqlite3.connect(DB,timeout=10); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=10000'); return c
def user(token):
 c=db(); r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(token,)).fetchone(); c.close(); return r
def channel(name):
 if not name.startswith('#') or '/' not in name[1:]: return None
 slug,cn=name[1:].split('/',1); c=db(); r=c.execute('select ch.*,w.slug,w.owner_id from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(slug,cn)).fetchone(); c.close(); return r
async def numerics(send,nick):
 for code,text in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle 1.0 o o'),('005','CHANTYPES=# :are supported by this server'),('422','MOTD File is missing')]: await send(f':huddle {code} {nick} :{text}')
async def client(reader,writer):
 nick=None; me=None; passed=False; user_sent=False; registered=False; joined=set()
 async def send(x): writer.write((x+'\r\n').encode()); await writer.drain()
 clients.append((writer,joined))
 try:
  while not reader.at_eof():
   line=(await reader.readline()).decode(errors='replace').rstrip('\r\n')
   if not line: continue
   p=line.split(' ',2); cmd=p[0].upper()
   if cmd=='PASS':
    me=user(p[1] if len(p)>1 else ''); passed=bool(me)
    if not passed: await send(':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    nn=p[1] if len(p)>1 else ''
    if nn in nicks and nn!=nick: await send(f':huddle 433 * {nn} :Nickname is already in use'); continue
    if nick:nicks.discard(nick)
    nick=nn;nicks.add(nick)
    if passed and user_sent and not registered: registered=True; await numerics(send,nick)
   elif cmd=='USER':
    user_sent=True
    if not passed: await send(':huddle 464 * :Password required')
    elif nick and not registered: registered=True; await numerics(send,nick)
   elif cmd=='PING':
    x=p[-1].lstrip(':'); await send(f':huddle PONG huddle :{x}')
   elif cmd=='PONG': pass
   elif cmd=='JOIN':
    if not registered: await send(':huddle 451 * :You have not registered'); continue
    name=p[1] if len(p)>1 else ''; ch=channel(name)
    if not ch: await send(f':huddle 403 {nick} {name} :No such channel'); continue
    c=db(); c.execute('insert or ignore into channel_members values(?,?)',(ch['id'],me['id'])); c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],me['id'],'member')); c.commit(); c.close(); joined.add(ch['id'])
    await send(f':{nick}!{me["username"]}@localhost JOIN {name}')
    names=[x['username'] for x in db().execute('select u.username from users u join channel_members m on m.user_id=u.id where m.channel_id=?',(ch['id'],)).fetchall()]
    await send(f':huddle 353 {nick} = {name} :{" ".join(names)}'); await send(f':huddle 366 {nick} {name} :End of NAMES list')
   elif cmd=='PRIVMSG':
    if len(p)<3: continue
    name=p[1]; body=p[2].lstrip(':'); ch=channel(name)
    if not ch: await send(f':huddle 403 {nick} {name} :No such channel'); continue
    if ch['id'] not in joined: await send(f':huddle 442 {nick} {name} :You are not on that channel'); continue
    c=db(); c.execute('BEGIN IMMEDIATE'); c.execute('insert into messages(channel_id,author_id,body,created_at,mentions) values(?,?,?,?,?)',(ch['id'],me['id'],body,__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat().replace('+00:00','Z'),''))
    mid=c.execute('select last_insert_rowid()').fetchone()[0]; r=c.execute('select * from messages where id=?',(mid,)).fetchone()
    m={'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'body':r['body'],'parent_id':None,'reply_count':0,'created_at':r['created_at'],'edited_at':None,'files':[],'reactions':[],'mentions':[]}
    n=c.execute('select coalesce(max(event_id),0)+1 from events where channel_id=?',(ch['id'],)).fetchone()[0]; fr={'type':'message.created','event_id':n,'channel_id':ch['id'],'message':m,'source_user_id':me['id']}
    c.execute('insert into events values(?,?,?,?,?)',(ch['id'],n,mid,'message.created',json.dumps(fr))); c.commit(); c.close()
   elif cmd=='QUIT': break
   elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE'): await send(f':huddle 200 {nick or "*"} {cmd} accepted')
   else: await send(f':huddle 421 {nick or "*"} {cmd} :Unknown command')
 finally:
  clients[:] = [x for x in clients if x[0] is not writer]
  if nick:nicks.discard(nick)
  writer.close(); await writer.wait_closed()
async def fanout():
 seen=0
 while True:
  await asyncio.sleep(.05); c=db(); rows=c.execute('select rowid,frame from events where rowid>? order by rowid',(seen,)).fetchall(); c.close()
  for r in rows:
   seen=r['rowid']; fr=json.loads(r['frame']); ch=fr.get('channel_id'); m=fr.get('message',{}); 
   for w,j in list(clients):
    if ch not in j: continue
    if fr.get('source_user_id') and any(x[0] is w and fr['source_user_id']==user_id for x in []): continue
    try:
     c2=db(); a=c2.execute('select username from users where id=?',(m.get('author_id'),)).fetchone(); c2.close()
     w.write((f':{a["username"]}!{a["username"]}@localhost PRIVMSG #'+(db().execute('select w.slug||"/"||ch.name from channels ch join workspaces w on w.id=ch.workspace_id where ch.id=?',(ch,)).fetchone()[0])+f' :{m.get("body","")}\r\n').encode()); await w.drain()
    except: pass
async def main():
 asyncio.create_task(fanout()); s=await asyncio.start_server(client,'0.0.0.0',6667)
 async with s: await s.serve_forever()
asyncio.run(main())
