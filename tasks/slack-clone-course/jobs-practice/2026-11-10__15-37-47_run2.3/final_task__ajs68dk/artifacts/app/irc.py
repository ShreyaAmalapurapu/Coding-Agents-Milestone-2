import socket,threading,json,urllib.request,sqlite3,time,os
HOST='0.0.0.0'; PORT=6667; DB='/app/data/huddle.db'
used=set(); clients=[]; guard=threading.RLock()
def api(m,p,b,t):
 r=urllib.request.Request('http://127.0.0.1:8000/api'+p,data=json.dumps(b).encode() if b is not None else None,method=m,headers={'Content-Type':'application/json','Authorization':'Bearer '+t})
 return json.loads(urllib.request.urlopen(r,timeout=4).read())
def db():
 c=sqlite3.connect(DB,timeout=5);c.row_factory=sqlite3.Row;return c
def send(c,x):
 try:c['s'].sendall((x+'\r\n').encode())
 except: c['alive']=False
def poller():
 while True:
  time.sleep(.08)
  with guard: cs=list(clients)
  c=db()
  for x in cs:
   if not x.get('alive'): continue
   for name,cid in list(x['joined'].items()):
    rows=c.execute("SELECT e.event_id,e.payload,m.author_id,u.username FROM events e JOIN messages m ON m.id=json_extract(e.payload,'$.message.id') JOIN users u ON u.id=m.author_id WHERE e.channel_id=? AND e.event_id>? AND e.type='message.created' ORDER BY e.event_id",(cid,x['last'].get(cid,0))).fetchall()
    for r in rows:
     x['last'][cid]=r['event_id']
     if r['author_id']!=x['uid']:
      body=json.loads(r['payload'])['message']['body']
      send(x,':%s!%s@localhost PRIVMSG %s :%s'%(r['username'],r['username'],name,body))
  c.close()
def client(s):
 x={'s':s,'tok':None,'nick':None,'user':None,'joined':{},'last':{},'alive':True,'uid':None,'collision':False,'welcomed':False}
 with guard: clients.append(x)
 s.settimeout(.3)
 def out(v):send(x,v)
 try:
  while x['alive']:
   try:q=s.recv(4096)
   except socket.timeout:q=b''
   if q:
    for line in q.decode(errors='ignore').splitlines():
     a=line.split();cmd=a[0].upper() if a else ''
     if cmd=='PASS': x['tok']=a[1] if len(a)>1 else ''
     elif cmd=='NICK':
      nn=a[1] if len(a)>1 else ''
      with guard: taken=nn in used and nn!=x['nick']
      if taken:x['collision']=True;out(':irc 433 * '+nn+' :Nickname in use');continue
      x['nick']=nn
     elif cmd=='USER':x['user']=a[1] if len(a)>1 else ''
     if x['nick'] and x['user'] and not x['tok'] and not x['welcomed']:
      out(':irc 464 '+x['nick']+' :Password incorrect');x['welcomed']=True
     if x['tok'] and x['nick'] and x['user'] and not x['welcomed']:
      try:
       me=api('GET','/auth/me',None,x['tok'])['user'];x['uid']=me['id']
       with guard: used.add(x['nick'])
       for n,msg in [('001','Welcome'),('002','Your host'),('003','Created'),('004','irc'),('005','CHANTYPES=#')]:out(':irc %s %s :%s'%(n,x['nick'],msg))
       if not x['collision']:out(':irc 422 '+x['nick']+' :MOTD File is missing')
       x['welcomed']=True
      except:out(':irc 464 '+(x['nick'] or '*')+' :Password incorrect');x['tok']=None;x['welcomed']=True
     if cmd=='PING':out('PONG :'+line.split(':',1)[-1])
     elif cmd=='QUIT':return
     elif cmd=='JOIN' and len(a)>1:
      name=a[1]
      if '/' not in name:out(':irc 403 '+x['nick']+' '+name+' :No such channel');continue
      slug,ch=name[1:].split('/',1)
      try:
       w=api('GET','/workspaces/'+slug,None,x['tok']);cc=next(z for z in w['channels'] if z['name']==ch);cid=cc['id'];api('POST','/channels/%s/join'%cid,{},x['tok'])
       x['joined'][name]=cid;c=db();r=c.execute('SELECT seq FROM channels WHERE id=?',(cid,)).fetchone();x['last'][cid]=r['seq'] if r else 0;c.close()
       out(':'+x['nick']+'!'+x['user']+'@localhost JOIN '+name);out(':irc 353 '+x['nick']+' = '+name+' :'+x['nick']);out(':irc 366 '+x['nick']+' '+name+' :End of /NAMES list')
      except:out(':irc 403 '+x['nick']+' '+name+' :No such channel')
     elif cmd=='PRIVMSG' and len(a)>2:
      name=a[1];body=line.split(' :',1)[-1];cid=x['joined'].get(name)
      if cid:api('POST','/channels/%s/messages'%cid,{'body':body},x['tok'])
     elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'): 
      if cmd!='PONG':out(':irc 321 '+x['nick']+' :accepted')
     elif cmd not in ('PASS','NICK','USER') and cmd:out(':irc 421 '+x['nick']+' '+cmd+' :Unknown command')
 finally:
  with guard:
   if x.get('nick'):used.discard(x['nick'])
   if x in clients:clients.remove(x)
  try:s.close()
  except:pass
threading.Thread(target=poller,daemon=True).start()
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind((HOST,PORT));s.listen()
while True:
 c,a=s.accept();threading.Thread(target=client,args=(c,),daemon=True).start()
