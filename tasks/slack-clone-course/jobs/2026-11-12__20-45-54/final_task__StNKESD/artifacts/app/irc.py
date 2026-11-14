import asyncio,json,sqlite3
DB='/app/data/huddle.db'; clients=[]
def db():
 c=sqlite3.connect(DB,timeout=5);c.row_factory=sqlite3.Row;return c
def tok(t):
 c=db();r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone();c.close();return r
def chan(n):
 q=n[1:] if n.startswith('#') else n
 if '/' not in q:return None
 s,n=q.split('/',1);c=db();r=c.execute('SELECT c.*,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(s,n)).fetchone();c.close();return r
async def client(r,w):
 st={'w':w,'nick':None,'user':None,'joined':{},'seen':{}};clients.append(st)
 async def send(x):w.write((x+'\r\n').encode());await w.drain()
 async def poll():
  while not w.is_closing():
   await asyncio.sleep(.1);c=db()
   for cid,name in list(st['joined'].items()):
    for e in c.execute('SELECT event_id,payload FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(cid,st['seen'].get(cid,0))):
     st['seen'][cid]=e['event_id'];x=json.loads(e['payload']);m=x.get('message',{});a=c.execute('SELECT username FROM users WHERE id=?',(m.get('author_id'),)).fetchone()
     if a and not (x.get('irc_author')==st['user']['id'] and m.get('author_id')==st['user']['id']):await send(f':{a["username"]}!{a["username"]}@host PRIVMSG {name} :{m.get("body","")}')
   c.close()
 task=asyncio.create_task(poll())
 try:
  while True:
   line=await r.readline()
   if not line:break
   z=line.decode(errors='ignore').strip('\r\n');p=z.split(' ',1);cmd=p[0].upper();arg=p[1] if len(p)>1 else ''
   if cmd=='PASS':
    st['user']=tok(arg.strip())
    if not st['user']:await send(':irc 464 * :Password incorrect')
   elif cmd=='NICK':
    n=arg.split()[0] if arg else ''
    if any(x['nick']==n and x is not st for x in clients):await send(f':irc 433 {st["nick"] or "*"} {n} :Nickname in use')
    else:st['nick']=n
   elif cmd=='USER':
    if not st['user']:await send(f':irc 464 {st["nick"] or "*"} :Password incorrect')
    elif st['nick']:
     for q in ('001','002','003','004','005'):await send(f':irc {q} {st["nick"]} :Welcome')
     await send(f':irc 422 {st["nick"]} :MOTD File is missing')
   elif cmd=='PING':await send(f':irc PONG irc :{arg.lstrip(":")}')
   elif cmd=='QUIT':break
   elif cmd in ('PONG','WHO','LIST','TOPIC','MODE'):pass
   elif cmd=='NAMES':await send(f':irc 353 {st["nick"]} = {arg} :{st["nick"]}');await send(f':irc 366 {st["nick"]} {arg} :End of NAMES')
   elif cmd=='JOIN':
    name=arg.split()[0] if arg else '';ch=chan(name)
    if not ch:await send(f':irc 403 {st["nick"] or "*"} {name} :No such channel')
    elif not st['user']:await send(f':irc 464 {st["nick"] or "*"} :Password incorrect')
    else:
     c=db();c.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(ch['workspace_id'],st['user']['id'],'member'));c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],st['user']['id']));c.commit();c.close();st['joined'][ch['id']]=name;st['seen'][ch['id']]=0
     await send(f':{st["nick"]}!{st["user"]["username"]}@host JOIN {name}');await send(f':irc 353 {st["nick"]} = {name} :{st["nick"]}');await send(f':irc 366 {st["nick"]} {name} :End of NAMES')
   elif cmd=='PRIVMSG':
    pp=arg.split(' :',1);ch=chan(pp[0]);text=pp[1] if len(pp)>1 else ''
    if ch and st['user']:
     c=db();c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,datetime("now"))',(ch['id'],st['user']['id'],text));mid=c.execute('SELECT last_insert_rowid()').fetchone()[0];c.execute('INSERT OR IGNORE INTO counters VALUES(?,0)',(ch['id'],));c.execute('UPDATE counters SET n=n+1 WHERE channel_id=?',(ch['id'],));eid=c.execute('SELECT n FROM counters WHERE channel_id=?',(ch['id'],)).fetchone()[0];m=dict(c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone());m.update(author=dict(st['user']),parent_id=None,reply_count=0,edited_at=None,files=[],reactions=[],mentions=[]);c.execute('INSERT INTO events VALUES(?,?,?,?)',(ch['id'],eid,'message.created',json.dumps({'type':'message.created','event_id':eid,'channel_id':ch['id'],'message':m,'irc_author':st['user']['id']})));c.commit();c.close()
   else:await send(f':irc 421 {st["nick"] or "*"} {cmd} :Unknown command')
 finally:
  task.cancel()
  if st in clients:clients.remove(st)
  w.close()
async def main():
 s=await asyncio.start_server(client,'0.0.0.0',6667)
 async with s:await s.serve_forever()
asyncio.run(main())
