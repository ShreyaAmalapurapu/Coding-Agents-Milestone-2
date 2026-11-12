import socket,threading,sqlite3,json,time,secrets
DB='/app/huddle.db'; lock=threading.RLock(); clients=set(); joined={}; cursors={}
def db():
 c=sqlite3.connect(DB,timeout=20,check_same_thread=False); c.row_factory=sqlite3.Row; return c
def q(sql,args=(),one=False):
 with lock:
  c=db(); r=c.execute(sql,args); z=r.fetchone() if one else r.fetchall(); c.close(); return z
def ex(sql,args=()):
 with lock:
  c=db(); r=c.execute(sql,args); c.commit(); z=r.lastrowid; c.close(); return z
def uid_token(t):
 r=q('select user_id from tokens where token=?',(t,),True); return r['user_id'] if r else None
def user(uid): return q('select * from users where id=?',(uid,),True)
def chan(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,n=name[1:].split('/',1); return q('select c.*,w.slug from channels c join workspaces w on w.id=c.workspace_id where w.slug=? and c.name=? and c.is_dm=0',(slug,n),True)
def ircname(c): return '#'+c['slug']+'/'+c['name']
def send(s,x):
 try:s.sendall((x+'\r\n').encode())
 except:pass
def event(cid,kind,payload=''):
 with lock:
  c=db(); c.execute('BEGIN IMMEDIATE')
  n=c.execute('select coalesce(max(event_id),0)+1 n from channel_events where channel_id=?',(cid,)).fetchone()['n']
  n=max(n,c.execute('select coalesce(max(event_id),0)+1 n from messages where channel_id=?',(cid,)).fetchone()['n'])
  c.execute('insert into channel_events values(?,?,?,?)',(cid,n,kind,payload)); c.commit(); c.close(); return n
def post(cid,uid,body,origin):
 with lock:
  c=db(); c.execute('BEGIN IMMEDIATE')
  n=max(c.execute('select coalesce(max(event_id),0)+1 from channel_events where channel_id=?',(cid,)).fetchone()[0],c.execute('select coalesce(max(event_id),0)+1 from messages where channel_id=?',(cid,)).fetchone()[0])
  mid=c.execute('insert into messages(channel_id,author_id,body,created_at,event_id,mentions) values(?,?,?,?,?,?)',(cid,uid,body,time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime()),n,'[]')).lastrowid
  c.execute('insert into channel_events values(?,?,?,?)',(cid,n,'message.created',origin)); c.commit(); c.close(); return mid
def broadcaster():
 while True:
  try:
   for cid in list(set(joined.values())):
    last=cursors.get(cid,0); rows=q('select * from channel_events where channel_id=? and event_id>? order by event_id',(cid,last))
    for e in rows:
     cursors[cid]=e['event_id']
     if e['event_type']!='message.created':continue
     m=q('select m.*,u.username from messages m join users u on u.id=m.author_id where m.channel_id=? and m.event_id=?',(cid,e['event_id']),True)
     if not m:continue
     c=q('select c.*,w.slug from channels c join workspaces w on w.id=c.workspace_id where c.id=?',(cid,),True); name=ircname(c)
     for s in list(clients):
      if joined.get(s)!=cid or e['payload']==str(id(s)):continue
      send(s,':%s!%s@localhost PRIVMSG %s :%s'%(m['username'],m['username'],name,m['body']))
  except: pass
  time.sleep(.05)
def reg(s,uid,nick):
 for n,t in [('001','Welcome'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1 1'),('005','CHANTYPES=#')]:send(s,':huddle '+n+' '+nick+' :'+t)
 send(s,':huddle 422 '+nick+' :MOTD File is missing')
def client(s,addr):
 uid=None; nick=None; collision=False; registered=False; f=s.makefile('r',encoding='utf8',errors='replace')
 clients.add(s)
 try:
  for line in f:
   line=line.rstrip('\r\n'); z=line.split(' ',1); cmd=z[0].upper(); arg=z[1] if len(z)>1 else ''
   if cmd=='PASS':
    uid=uid_token(arg.lstrip(':'))
    if not uid:send(s,':huddle 464 * :Password incorrect')
   elif cmd=='NICK':
    nn=arg.split()[0] if arg else ''
    with lock:
     if nn in nicks and nicks[nn] is not s: collision=True; send(s,':huddle 433 * '+nn+' :Nickname is already in use'); continue
     if nick:nicks.pop(nick,None)
     nick=nn;nicks[nick]=s
    if uid and not registered:registered=True; reg(s,uid,nick)
   elif cmd=='USER':
    if not uid:send(s,':huddle 464 * :Password required')
    elif nick and not registered:registered=True;reg(s,uid,nick)
   elif cmd=='PING':send(s,':huddle PONG huddle :'+arg.lstrip(':'))
   elif cmd=='QUIT':break
   elif cmd=='JOIN':
    c=chan(arg.split()[0]) if arg else None
    if not c:send(s,':huddle 403 '+(nick or '*')+' '+arg+' :No such channel');continue
    if not q('select 1 from members where workspace_id=? and user_id=?',(c['workspace_id'],uid),True):send(s,':huddle 403 '+nick+' '+arg+' :Not a member');continue
    joined[s]=c['id']; ex('insert or ignore into channel_members values(?,?)',(c['id'],uid))
    send(s,':%s!%s@localhost JOIN %s'%(nick,user(uid)['username'],ircname(c)))
    rows=q('select u.username from users u join channel_members m on m.user_id=u.id where m.channel_id=?',(c['id'],)); names=' '.join(x['username'] for x in rows)
    send(s,':huddle 353 %s = %s :%s'%(nick,ircname(c),names));send(s,':huddle 366 %s %s :End of NAMES list'%(nick,ircname(c)))
   elif cmd=='PRIVMSG':
    if not registered or ':' not in arg:continue
    target,text=arg.split(' :',1); c=chan(target)
    if not c or joined.get(s)!=c['id']:continue
    post(c['id'],uid,text,str(id(s)))
   elif cmd=='PONG':pass
   elif cmd=='NAMES':send(s,':huddle 366 '+(nick or '*')+' '+arg+' :End of NAMES list')
   elif cmd=='WHO':send(s,':huddle 315 '+(nick or '*')+' '+arg+' :End of WHO list')
   elif cmd=='LIST':send(s,':huddle 323 '+(nick or '*')+' :End of LIST')
   elif cmd=='TOPIC':send(s,':huddle 331 '+(nick or '*')+' '+arg+' :No topic is set')
   elif cmd=='MODE':send(s,':huddle 324 '+(nick or '*')+' '+arg+' +')
   else:send(s,':huddle 421 '+(nick or '*')+' '+cmd+' :Unknown command')
 finally:
  clients.discard(s);joined.pop(s,None)
  if nick:nicks.pop(nick,None)
  try:s.close()
  except:pass
nicks={}
srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);srv.bind(('0.0.0.0',6667));srv.listen(100)
threading.Thread(target=broadcaster,daemon=True).start()
while True:
 s,a=srv.accept();threading.Thread(target=client,args=(s,a),daemon=True).start()
