import asyncio,sqlite3,os,re
DB='/app/data/huddle.db'; clients={}; joins={}; lock=asyncio.Lock()
def con(): c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
def usertok(t):
 c=con(); r=c.execute('select id,username from users where token=?',(t,)).fetchone(); c.close(); return r
def chan(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,n=name[1:].split('/',1); c=con(); r=c.execute('select ch.*,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(slug,n)).fetchone(); c.close(); return r
async def send(w,x):
 try:w.write((x+'\r\n').encode()); await w.drain()
 except:pass
def nickline(n): return n+'!'+n+'@localhost'
async def poll():
 seen={}
 while True:
  await asyncio.sleep(.05)
  c=con(); rows=c.execute("select e.*,m.author_id,u.username,ch.name cname,w.slug from events e join messages m on m.id=e.message_id join users u on u.id=m.author_id join channels ch on ch.id=e.channel_id join workspaces w on w.id=ch.workspace_id where e.kind='message.created' order by e.rowid").fetchall(); c.close()
  for r in rows:
   name='#%s/%s'%(r['slug'],r['cname']); body=jsonbody(r['data'])
   for nick,w in list(clients.items()):
    key=(w,r['channel_id'])
    if r['event_id']<=seen.get(key,0):continue
    if r['channel_id'] in joins.get(w,set()) and r['author_id']!=getattr(w,'irc_uid',None):
     await send(w,':%s PRIVMSG %s :%s'%(nickline(r['username']),name,body.get('body','')))
    seen[key]=r['event_id']
def jsonbody(x):
 import json
 try:return json.loads(x)
 except:return {}
async def client(r,w):
 nick=None; uid=None; token=None; registered=False
 try:
  while True:
   line=await r.readline()
   if not line:break
   z=line.decode(errors='ignore').strip()
   if not z:continue
   cmd,*rest=z.split(' ',1); cmd=cmd.upper(); arg=rest[0] if rest else ''
   if cmd=='PASS':
    token=arg.lstrip(':')
    if not usertok(token):await send(w,':irc 464 * :Password incorrect')
   elif cmd=='NICK':
    nn=arg.split()[0].lstrip(':') if arg else ''
    if nn in clients and clients[nn] is not w: await send(w,':irc 433 * %s :Nickname is already in use'%nn)
    else:
     if nick in clients:clients.pop(nick,None)
     nick=nn
     if uid and token and not registered:
      u=usertok(token)
      if u:
       uid=u['id']; w.irc_uid=uid; clients[nick]=w; registered=True
       for n,t in [('001','Welcome'),('002','Your host is huddle'),('003','Created today'),('004','huddle huddle oiwsz biklmnop'),('005','CHANTYPES=#'),('422','No MOTD')]:await send(w,':irc %s %s :%s'%(n,nick,t))
   elif cmd=='USER':
    if not token or not usertok(token):await send(w,':irc 464 * :Password required')
    else:
     uid=usertok(token)['id']
     if nick and not registered:
      w.irc_uid=uid; clients[nick]=w; registered=True
      for n,t in [('001','Welcome'),('002','Your host is huddle'),('003','Created today'),('004','huddle huddle oiwsz biklmnop'),('005','CHANTYPES=#'),('422','No MOTD')]:await send(w,':irc %s %s :%s'%(n,nick,t))
   elif cmd=='PING':await send(w,':irc PONG irc :'+(arg.split(':',1)[-1]))
   elif cmd=='JOIN':
    name=arg.split()[0]; ch=chan(name)
    if not ch:await send(w,':irc 403 %s %s :No such channel'%(nick,name));continue
    joins.setdefault(w,set()).add(ch['id'])
    await send(w,':%s JOIN %s'%(nickline(nick),name))
    names=[x['username'] for x in con().execute('select u.username from users u join channel_members cm on cm.user_id=u.id where cm.channel_id=?',(ch['id'],)).fetchall()]
    await send(w,':irc 353 %s = %s :%s'%(nick,name,' '.join(names))); await send(w,':irc 366 %s %s :End of NAMES list'%(nick,name))
   elif cmd=='PRIVMSG':
    if ' :' not in arg:continue
    target,body=arg.split(' :',1); ch=chan(target)
    if not ch:continue
    c=con()
    async with lock:
     cur=c.execute('insert into messages(channel_id,author_id,body,created) values(?,?,?,datetime("now"))',(ch['id'],uid,body)); mid=cur.lastrowid
     n=(c.execute('select coalesce(n,0) n from counters where channel_id=?',(ch['id'],)).fetchone()['n']+1)
     mo={'id':mid,'channel_id':ch['id'],'author_id':uid,'body':body,'parent_id':None,'reply_count':0,'created_at':'','edited_at':None,'files':[],'reactions':[],'mentions':[]}
     c.execute('insert into counters(channel_id,n) values(?,?) on conflict(channel_id) do update set n=n+1',(ch['id'],n))
     c.execute('insert into events values(?,?,?,?,?)',(ch['id'],n,'message.created',mid,__import__('json').dumps(mo))); c.commit()
    c.close()
   elif cmd=='QUIT':break
   elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'):continue
   elif not registered:await send(w,':irc 464 * :You must authenticate')
   else:await send(w,':irc 421 %s %s :Unknown command'%(nick,cmd))
 finally:
  if nick in clients and clients[nick] is w:clients.pop(nick,None)
  joins.pop(w,None)
  try:w.close();await w.wait_closed()
  except:pass
async def main():
 s=await asyncio.start_server(client,'0.0.0.0',6667); asyncio.create_task(poll())
 async with s:await s.serve_forever()
asyncio.run(main())
