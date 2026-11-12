import socket,threading,sqlite3,json,time,os
DB='/app/data/huddle.db'; clients=[]; lock=threading.Lock(); nicks=set()
def db():
 c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=5000'); return c
def token_user(t):
 c=db(); r=c.execute('SELECT u.id,u.username FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def rawsend(x,line):
 try: x.sendall((line+'\r\n').encode())
 except: pass
def reply(x,n,text): rawsend(x,':huddle '+text.replace('{nick}',n or '*'))
def lookup(name):
 if not name.startswith('#') or '/' not in name: return None
 slug,chname=name[1:].split('/',1); c=db(); r=c.execute('SELECT c.*,w.slug,w.name wname FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,chname)).fetchone(); c.close(); return r
def broadcast_event(chid,eid,payload,source=None):
 c=db(); r=c.execute('SELECT m.body,u.username FROM messages m JOIN users u ON u.id=m.author_id WHERE m.id=?',(payload.get('message',{}).get('id'),)).fetchone(); c.close()
 if not r:return
 with lock: peers=list(clients)
 for cl in peers:
  if chid in cl['joined'] and cl is not source:
   rawsend(cl['sock'],':%s!%s@host PRIVMSG %s :%s'%(r['username'],r['username'],cl['names'].get(chid,'#'),r['body']))
def bridge():
 last={}
 while True:
  try:
   c=db(); rows=c.execute("SELECT channel_id,event_id,kind,payload FROM events WHERE kind='message.created' ORDER BY channel_id,event_id").fetchall(); c.close()
   for r in rows:
    key=(r['channel_id'],r['event_id'])
    if key in last: continue
    last[key]=1; broadcast_event(r['channel_id'],r['event_id'],json.loads(r['payload']))
  except: pass
  time.sleep(.05)
def client(s,addr):
 state={'sock':s,'joined':set(),'names':{}}; nick=None; u=None; passed=False; usercmd=False; registered=False; buf=b''
 with lock: clients.append(state)
 try:
  while True:
   data=s.recv(4096)
   if not data: break
   buf+=data
   while b'\n' in buf:
    raw,buf=buf.split(b'\n',1); line=raw.rstrip(b'\r').decode(errors='replace'); ps=line.split(' ',1); cmd=ps[0].upper(); arg=ps[1] if len(ps)>1 else ''
    if cmd=='PASS':
     u=token_user(arg.lstrip(':')); passed=bool(u)
     if not passed: reply(s,nick,'464 {nick} :Password incorrect')
    elif cmd=='NICK':
     nn=arg.lstrip(':').split()[0] if arg else ''
     with lock: busy=nn in nicks and nn!=nick
     if busy: reply(s,nn,'433 {nick} '+nn+' :Nickname is already in use')
     elif nn:
      with lock:
       if nick:nicks.discard(nick)
       nicks.add(nn)
      nick=nn
    elif cmd=='USER':
     usercmd=True
    elif cmd=='PING':
     rawsend(s,':huddle PONG huddle :'+arg.lstrip(':'))
    elif cmd=='QUIT': return
    elif cmd=='JOIN':
     name=arg.split()[0] if arg else ''; ch=lookup(name)
     if not ch: reply(s,nick,'403 {nick} '+name+' :No such channel'); continue
     c=db(); member=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],u['id'])).fetchone()
     if not member: c.close(); reply(s,nick,'403 {nick} '+name+' :No such channel'); continue
     c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],u['id'])); c.commit()
     names=[x['username'] for x in c.execute('SELECT u.username FROM users u JOIN channel_members cm ON cm.user_id=u.id WHERE cm.channel_id=?',(ch['id'],))]
     c.close(); state['joined'].add(ch['id']); state['names'][ch['id']]=name
     rawsend(s,':%s!%s@host JOIN %s'%(nick,u['username'],name)); reply(s,nick,'353 {nick} = '+name+' :'+(' '.join(names))); reply(s,nick,'366 {nick} '+name+' :End of NAMES list')
    elif cmd=='PRIVMSG':
     if ' :' not in arg: continue
     target,text=arg.split(' :',1); ch=lookup(target.strip())
     if not ch or ch['id'] not in state['joined']: reply(s,nick,'404 {nick} '+target+' :Cannot send to channel'); continue
     c=db(); c.execute('BEGIN IMMEDIATE'); cur=c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],u['id'],text.replace('\n',' '),time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))); mid=cur.lastrowid; eid=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM events WHERE channel_id=?',(ch['id'],)).fetchone()['n']; msg={'id':mid,'channel_id':ch['id'],'author_id':u['id'],'body':text,'parent_id':None,'reply_count':0,'created_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'edited_at':None,'files':[],'reactions':[],'mentions':[]}; c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(ch['id'],eid,'message.created',json.dumps({'message':msg}),msg['created_at'])); c.commit(); c.close()
     with lock:
      for peer in list(clients):
       if peer is not state and ch['id'] in peer['joined']: rawsend(peer['sock'],':%s!%s@host PRIVMSG %s :%s'%(nick,u['username'],target.strip(),text))
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'): pass
    else: reply(s,nick,'421 '+cmd+' :Unknown command')
    if not registered and passed and nick and usercmd:
     registered=True
     for n,t in [('001','Welcome'),('002','Your host is huddle'),('003','Created today'),('004','huddle huddle 1.0 o o'),('005','CHANTYPES=# :are supported'),('422','MOTD File is missing')]: rawsend(s,':huddle '+n+' '+nick+' :'+t)
    elif not registered and nick and usercmd and not passed:
     reply(s,nick,'464 {nick} :Password required'); registered=True
 finally:
  with lock:
   if nick:nicks.discard(nick)
   if state in clients:clients.remove(state)
  s.close()
def main():
 threading.Thread(target=bridge,daemon=True).start(); srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); srv.bind(('0.0.0.0',6667)); srv.listen()
 while True:
  s,a=srv.accept(); threading.Thread(target=client,args=(s,a),daemon=True).start()
if __name__=='__main__': main()
