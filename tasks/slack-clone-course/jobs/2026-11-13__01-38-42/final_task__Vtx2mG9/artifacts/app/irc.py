import asyncio, sqlite3, json, os, secrets
DB='/app/data/chat.db'; clients=[]; nicks=set()
def db():
 c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=5000'); return c
def user_for(token):
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(token,)).fetchone(); c.close(); return r
def channel_for(name):
 if not name.startswith('#') or '/' not in name[1:]: return None
 slug,cn=name[1:].split('/',1); c=db(); r=c.execute('SELECT ch.*,w.slug,w.name wsname FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,cn)).fetchone();c.close();return r
def frame_message(c,r):
 a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':{k:a[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')},'body':r['body'],'parent_id':r['parent_id'],'reply_count':0,'created_at':r['created_at'],'edited_at':None,'files':[],'reactions':[],'mentions':[]}
def irc_event(cid,uid,body):
 import datetime
 t=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z'); c=db()
 c.execute('BEGIN IMMEDIATE')
 cur=c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(cid,uid,body,t));mid=cur.lastrowid
 n=c.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone()
 if not n:c.execute('INSERT INTO counters(channel_id,n) VALUES(?,0)',(cid,)); eid=1
 else:eid=n['n']+1
 c.execute('UPDATE counters SET n=? WHERE channel_id=?',(eid,cid))
 r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); mo=frame_message(c,r)
 fr={'type':'message.created','event_id':eid,'channel_id':cid,'message':mo}
 c.execute('INSERT INTO events VALUES(?,?,?)',(cid,eid,json.dumps(fr)));c.commit();c.close();return fr
async def client(reader,writer):
 token=uid=nick=None; gotuser=False; registered=False; joined=set(); seen={}
 peer=writer.get_extra_info('peername');host=peer[0] if peer else 'localhost'
 async def send(s): writer.write((s+'\r\n').encode())
 async def num(code,text=''): await send(f':huddle {code} {nick or "*"} {text}')
 async def welcome():
  nonlocal registered
  registered=True
  for code,text in [('001',':Welcome to Huddle IRC'),('002',':Your host is huddle'),('003',':This server was created locally'),('004','huddle huddle 1'),('005',':CHANTYPES=#')]: await num(code,text)
  await num('422',':MOTD File is missing')
 state={'writer':writer,'nick':lambda:nick,'uid':lambda:uid,'joined':joined,'seen':seen,'host':host}
 clients.append(state)
 async def poll():
  while not writer.is_closing():
   try:
    c=db()
    for name in list(joined):
     ch=channel_for(name)
     if not ch: continue
     last=seen.get(ch['id'],0)
     for e in c.execute('SELECT event_id,message FROM events WHERE channel_id=? AND event_id>? ORDER BY event_id',(ch['id'],last)):
      seen[ch['id']]=e['event_id']; fr=json.loads(e['message'])
      if fr.get('type')!='message.created': continue
      mo=fr['message']; au=c.execute('SELECT username FROM users WHERE id=?',(mo['author_id'],)).fetchone()
      if au and mo['author_id']!=uid: await send(f":{au['username']}!{au['username']}@{host} PRIVMSG {name} :{mo['body']}")
    c.close();await asyncio.sleep(.05)
   except Exception: await asyncio.sleep(.1)
 task=asyncio.create_task(poll())
 try:
  while True:
   raw=await reader.readline()
   if not raw:break
   line=raw.decode(errors='replace').rstrip('\r\n'); parts=line.split(' ',1);cmd=parts[0].upper();arg=parts[1] if len(parts)>1 else ''
   if cmd=='PASS':
    token=arg[1:] if arg.startswith(':') else arg; u=user_for(token);uid=u['id'] if u else None
    if not u:await num('464',':Password incorrect')
   elif cmd=='NICK':
    new=arg.split()[0] if arg else ''
    if new in nicks and new!=nick:await num('433',f'{new} :Nickname is already in use');continue
    if nick:nicks.discard(nick)
    nick=new;nicks.add(nick)
    if uid and gotuser and not registered:await welcome()
   elif cmd=='USER':
    gotuser=True
    if uid and nick and not registered:await welcome()
    elif not uid:await num('464',':Password required')
   elif cmd=='PING':
    x=arg[1:] if arg.startswith(':') else arg;await send(f':huddle PONG huddle :{x}')
   elif cmd=='JOIN':
    name=arg.split()[0] if arg else '';ch=channel_for(name)
    if not ch or not uid:await num('403',name+' :No such channel');continue
    c=db(); ok=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],uid)).fetchone()
    if not ok:c.close();await num('403',name+' :No such channel');continue
    c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],uid));c.commit()
    row=c.execute('SELECT n FROM counters WHERE channel_id=?',(ch['id'],)).fetchone(); seen[ch['id']]=row['n'] if row else 0
    names=[x['username'] for x in c.execute('SELECT u.username FROM users u JOIN cmembers m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],))]
    c.close();joined.add(name);await send(f':{nick}!{user_for(token)["username"]}@{host} JOIN {name}');await send(f':huddle 353 {nick} = {name} :{" ".join(names)}');await send(f':huddle 366 {nick} {name} :End of NAMES list')
   elif cmd=='PRIVMSG':
    z=arg.split(' :',1); target=z[0].strip() if z else ''; text=z[1] if len(z)>1 else ''
    ch=channel_for(target)
    if ch and uid:
     c=db(); ok=c.execute('SELECT 1 FROM cmembers WHERE channel_id=? AND user_id=?',(ch['id'],uid)).fetchone();c.close()
     if ok: irc_event(ch['id'],uid,text)
   elif cmd=='QUIT':break
   elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'):pass
   elif cmd=='PASS':pass
   else:await num('421',f'{cmd} :Unknown command')
 finally:
  task.cancel();clients.remove(state) if state in clients else None
  if nick:nicks.discard(nick)
  writer.close()
  try:await writer.wait_closed()
  except:pass
async def main():
 s=await asyncio.start_server(client,'0.0.0.0',6667)
 async with s:await s.serve_forever()
asyncio.run(main())
