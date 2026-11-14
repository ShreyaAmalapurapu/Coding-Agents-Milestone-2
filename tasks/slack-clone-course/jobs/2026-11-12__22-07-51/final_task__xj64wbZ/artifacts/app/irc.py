import socket,threading,sqlite3,json,time
from datetime import datetime,timezone
DB='/app/data/chat.db'; clients=[]; used=set(); mu=threading.RLock(); delivered=set()
def now(): return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
def send(c,s):
 try:c.sendall((s+'\r\n').encode())
 except: pass
def channel_info(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cn=name[1:].split('/',1)
 with sqlite3.connect(DB) as d:
  d.row_factory=sqlite3.Row
  return d.execute('SELECT c.id,c.name,c.workspace_id,w.slug FROM channels c JOIN workspaces w ON w.id=c.workspace_id WHERE w.slug=? AND c.name=?',(slug,cn)).fetchone()
def event_message(cid,uid,body):
 with sqlite3.connect(DB,timeout=5) as d:
  d.row_factory=sqlite3.Row
  d.execute('BEGIN IMMEDIATE')
  d.execute('INSERT OR IGNORE INTO counters(channel_id,n) VALUES(?,0)',(cid,))
  cur=d.execute('INSERT INTO messages(channel_id,author_id,body,created) VALUES(?,?,?,?)',(cid,uid,body,now())); mid=cur.lastrowid
  row=d.execute('SELECT n FROM counters WHERE channel_id=?',(cid,)).fetchone(); n=(row['n'] if row else 0)+1
  d.execute('INSERT OR REPLACE INTO counters(channel_id,n) VALUES(?,?)',(cid,n))
  u=d.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
  payload={'message':{'id':mid,'channel_id':cid,'author_id':uid,'author':{'id':u['id'],'username':u['username'],'display_name':u['display_name'],'timezone':'UTC','avatar_url':'','status_text':'','status_emoji':''},'body':body,'parent_id':None,'reply_count':0,'created_at':d.execute('SELECT created FROM messages WHERE id=?',(mid,)).fetchone()['created'],'edited_at':None,'files':[],'reactions':[],'mentions':[]}}
  d.execute('INSERT INTO events(channel_id,event_id,type,payload) VALUES(?,?,?,?)',(cid,n,'message.created',json.dumps(payload)))
 return mid,n
def poll():
 while True:
  try:
   with sqlite3.connect(DB) as d:
    d.row_factory=sqlite3.Row
    rows=d.execute("SELECT e.channel_id,e.event_id,e.payload,c.name,w.slug FROM events e JOIN channels c ON c.id=e.channel_id JOIN workspaces w ON w.id=c.workspace_id WHERE e.type='message.created' ORDER BY e.channel_id,e.event_id").fetchall()
   with mu:
    for e in rows:
     key=(e['channel_id'],e['event_id'])
     if key in delivered:continue
     delivered.add(key); body=json.loads(e['payload'])['message']['body']; author=json.loads(e['payload'])['message']['author']['username']; name='#'+e['slug']+'/'+e['name']
     for item in list(clients):
      c,uid,nick,joined,origin=item
      if name in joined and origin!=key:
       send(c,':'+author+'!'+author+'@host PRIVMSG '+name+' :'+body)
  except Exception: pass
  time.sleep(.08)
def welcome(c,nick):
 for s in [':irc 001 '+nick+' :Welcome',':irc 002 '+nick+' :Your host',':irc 003 '+nick+' :Created',':irc 004 '+nick+' irc',':irc 005 '+nick+' :CHANTYPES=#',':irc 422 '+nick+' :MOTD File Missing']:send(c,s)
def run(c):
 token=None;nick=None;uid=None;joined={};registered=False;user_seen=False;origin=None
 try:
  while True:
   z=c.recv(4096)
   if not z:break
   for line in z.decode(errors='ignore').splitlines():
    p=line.split();cmd=p[0].upper() if p else ''
    if cmd=='PASS':
     token=p[1] if len(p)>1 else ''
     with sqlite3.connect(DB) as d:q=d.execute('SELECT user_id FROM tokens WHERE token=?',(token,)).fetchone()
     if not q:send(c,':irc 464 * :Password incorrect');token=None
     else:uid=q[0]
    elif cmd=='NICK':
     if len(p)<2:continue
     if p[1] in used:send(c,':irc 433 * '+p[1]+' :Nickname in use')
     else:
      nick=p[1]
      if user_seen and token and uid and not registered:
       with mu:used.add(nick);clients.append((c,uid,nick,joined,origin))
       registered=True;welcome(c,nick)
    elif cmd=='USER':
     user_seen=True
     if not token or not uid:send(c,':irc 464 '+(nick or '*')+' :Password required')
     elif nick and not registered:
      with mu:used.add(nick);clients.append((c,uid,nick,joined,origin))
      registered=True;welcome(c,nick)
    elif cmd=='PING':send(c,':irc PONG irc :'+(p[-1].lstrip(':') if p else ''))
    elif cmd=='QUIT':return
    elif cmd in ('PONG','NAMES','WHO','LIST','TOPIC','MODE'):pass
    elif cmd=='JOIN' and len(p)>1:
     name=p[1];ch=channel_info(name)
     if not ch:send(c,':irc 403 '+(nick or '*')+' '+name+' :No such channel');continue
     with sqlite3.connect(DB) as d:
      d.execute('INSERT OR IGNORE INTO members(workspace_id,user_id,role) SELECT workspace_id,?,\"member\" FROM channels WHERE id=?',(uid,ch['id']))
      d.execute('INSERT OR IGNORE INTO cmembers(channel_id,user_id) VALUES(?,?)',(ch['id'],uid))
     joined[name]=ch['id']
     with mu:
      for i,x in enumerate(clients):
       if x[0] is c:clients[i]=(c,uid,nick,joined,origin)
     send(c,':'+nick+'!'+nick+'@host JOIN '+name);send(c,':irc 353 '+nick+' = '+name+' :'+nick);send(c,':irc 366 '+nick+' '+name+' :End')
    elif cmd=='PRIVMSG' and len(p)>2:
     name=p[1];body=' '.join(p[2:]).lstrip(':');cid=joined.get(name)
     if cid:
      mid,evno=event_message(cid,uid,body);origin=(cid,evno)
      with mu:
       for i,x in enumerate(clients):
        if x[0] is c:clients[i]=(x[0],x[1],x[2],x[3],(cid,0))
    elif cmd:send(c,':irc 421 '+(p[0] if p else '')+' :Unknown command')
 finally:
  with mu:
   if nick:used.discard(nick)
   clients[:]=[x for x in clients if x[0] is not c]
  try:c.close()
  except:pass
threading.Thread(target=poll,daemon=True).start()
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen()
while True:
 c,_=s.accept();threading.Thread(target=run,args=(c,),daemon=True).start()
