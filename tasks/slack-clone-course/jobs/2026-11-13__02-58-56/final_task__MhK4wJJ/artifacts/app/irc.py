import socket, threading, sqlite3, os, json, time, uuid
DB='/app/data/huddle.sqlite3'
lock=threading.RLock()
clients={}
def db():
 c=sqlite3.connect(DB,timeout=30); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=30000'); return c
def user_for_token(t):
 c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def send(s,line):
 try:s.sendall((line.replace('\n',' ')+'\r\n').encode())
 except:pass
def chan(c,name):
 if not name.startswith('#') or '/' not in name:return None
 slug,nm=name[1:].split('/',1)
 return c.execute('SELECT ch.*,w.slug,w.name wname FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,nm)).fetchone()
def irc_name(r): return '#'+r['slug']+'/'+r['name']
def prefix(cl): return ':'+cl['nick']+'!'+cl['user']['username']+'@localhost'
def event_message(c,r,author_id):
 author=c.execute('SELECT * FROM users WHERE id=?',(author_id,)).fetchone()
 return {'id':r['id'],'channel_id':r['channel_id'],'author_id':author_id,'author':dict(author),'body':r['body'],'parent_id':None,'reply_count':0,'created_at':r['created_at'],'edited_at':None,'files':[],'reactions':[],'mentions':[]}
def emit(c,channel_id,kind,payload):
 n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 n FROM channel_events WHERE channel_id=?',(channel_id,)).fetchone()['n']
 c.execute('INSERT INTO channel_events VALUES(?,?,?,?,?)',(channel_id,n,kind,json.dumps(payload),time.strftime('%Y-%m-%dT%H:%M:%SZ'))); return n
def broadcast(channel_id,line,exclude=None):
 with lock:
  for cl in list(clients.values()):
   if channel_id in cl['joined'] and cl is not exclude: send(cl['sock'],line)
def poll():
 seen={}
 while True:
  try:
   c=db()
   rows=c.execute("SELECT e.*,ch.name,w.slug FROM channel_events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE e.kind IN ('message.created','message.reply') ORDER BY e.rowid").fetchall()
   for r in rows:
    key=(r['channel_id'],r['event_id'])
    if key in seen:continue
    seen[key]=1
    p=json.loads(r['payload']); m=p.get('message',p)
    body=m.get('body',''); author=m.get('author',{})
    line=':'+author.get('username','user')+'!'+author.get('username','user')+'@localhost PRIVMSG #'+r['slug']+'/'+r['name']+' :'+body
    broadcast(r['channel_id'],line)
   c.close()
  except: pass
  time.sleep(.05)
def client(sock,addr):
 cl={'sock':sock,'nick':None,'user':None,'joined':set()}; token=None; registered=False; buf=b''
 with lock: clients[id(sock)]=cl
 try:
  while True:
   x=sock.recv(4096)
   if not x:break
   buf+=x
   while b'\n' in buf:
    raw,buf=buf.split(b'\n',1); line=raw.rstrip(b'\r').decode('utf8','replace')
    if not line:continue
    a=line.split(' ',1); cmd=a[0].upper(); rest=a[1] if len(a)>1 else ''
    if cmd=='PASS':
     token=rest.strip();cl['user']=user_for_token(token)
     if not cl['user']:send(sock,':huddle 464 * :Password incorrect')
    elif cmd=='NICK':
     nn=rest.strip().lstrip(':')
     with lock:
      taken=any(x is not cl and x['nick']==nn for x in clients.values())
     if taken:send(sock,':huddle 433 * '+nn+' :Nickname is already in use')
     else:cl['nick']=nn
    elif cmd=='USER':
     if not cl['user']:send(sock,':huddle 464 * :Password required');continue
     if not cl['nick']:cl['nick']=rest.split(' ',1)[0]
     if not registered:
      registered=True
      for n,t in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1.0 oi'),('005','CHANTYPES=#')]:send(sock,':huddle '+n+' '+cl['nick']+' :'+t)
      send(sock,':huddle 422 '+cl['nick']+' :MOTD File is missing')
    elif cmd=='PING':send(sock,':huddle PONG huddle :'+(rest[1:] if rest.startswith(':') else rest))
    elif cmd=='QUIT':break
    elif cmd=='JOIN':
     name=rest.split(',')[0].strip(); c=db(); ch=chan(c,name)
     if not ch:send(sock,':huddle 403 '+(cl['nick'] or '*')+' '+name+' :No such channel');c.close();continue
     if not c.execute('SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],cl['user']['id'])).fetchone():
      send(sock,':huddle 403 '+cl['nick']+' '+name+' :Not a member');c.close();continue
     c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],cl['user']['id']));c.commit();cl['joined'].add(ch['id'])
     send(sock,prefix(cl)+' JOIN '+name)
     rows=c.execute('SELECT u.username FROM users u JOIN channel_members m ON m.user_id=u.id WHERE m.channel_id=?',(ch['id'],)).fetchall()
     send(sock,':huddle 353 '+cl['nick']+' = '+name+' :'+ ' '.join(x['username'] for x in rows));send(sock,':huddle 366 '+cl['nick']+' '+name+' :End of NAMES list');c.close()
    elif cmd=='PRIVMSG':
     if ' :' in rest: target,text=rest.split(' :',1)
     else:
      z=rest.split(' ',1);target=z[0];text=z[1] if len(z)>1 else ''
     c=db();ch=chan(c,target)
     if not ch or ch['id'] not in cl['joined']:send(sock,':huddle 404 '+cl['nick']+' '+target+' :Cannot send to channel');c.close();continue
     c.execute('BEGIN IMMEDIATE'); ts=time.strftime('%Y-%m-%dT%H:%M:%SZ')
     cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at) VALUES(?,?,?,?,?)',(ch['id'],cl['user']['id'],text,None,ts)); mid=cur.lastrowid
     m=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); msg=event_message(c,m,cl['user']['id']); emit(c,ch['id'],'message.created',{'message':msg});c.commit();c.close()
     broadcast(ch['id'],prefix(cl)+' PRIVMSG '+target+' :'+text,cl)
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'): 
     if cmd=='NAMES':send(sock,':huddle 366 '+(cl['nick'] or '*')+' * :End of NAMES list')
    else:send(sock,':huddle 421 '+(cl['nick'] or '*')+' '+cmd+' :Unknown command')
 finally:
  with lock: clients.pop(id(sock),None)
  try:sock.close()
  except:pass
def main():
 threading.Thread(target=poll,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  x,a=s.accept();threading.Thread(target=client,args=(x,a),daemon=True).start()
if __name__=='__main__':main()
