import socket,threading,time,sys,urllib.parse
sys.path.insert(0,'/app')
from app import db,user,msgobj,emit
SERVER='huddle'; clients=[]; lock=threading.Lock()
def raw(c,s):
 try:c.sendall((s+'\r\n').encode())
 except:pass
def line(c,s):
 raw(c,':'+SERVER+' '+s)
def chan(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,n=name[1:].split('/',1); c=db(); r=c.execute('SELECT ch.*,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,n)).fetchone();c.close();return r
def fullname(ch):return '#'+ch['slug']+'/'+ch['name']
def deliver(frame,source=None):
 if frame.get('type')!='message.created':return
 m=frame.get('message',{}); c=db(); a=c.execute('SELECT username FROM users WHERE id=?',(m.get('author_id'),)).fetchone();c.close()
 if not a:return
 with lock:
  for cl in list(clients):
   if cl['channel']==frame.get('channel_id') and cl is not source:
    raw(cl['sock'],':'+a['username']+'!'+a['username']+'@host PRIVMSG '+cl['name']+' :'+m.get('body',''))
def poll():
 seen={}
 while True:
  c=db()
  rows=c.execute("SELECT e.channel_id,e.event_id,e.message FROM events e JOIN channels ch ON ch.id=e.channel_id").fetchall();c.close()
  for r in rows:
   key=(r['channel_id'],r['event_id'])
   if key in seen:continue
   seen[key]=1
   try: deliver(__import__('json').loads(r['message']))
   except:pass
  time.sleep(.08)
def client(sock,addr):
 token=None; nick=None; reg=False; joined={}; source=None
 with lock: clients.append({'sock':sock,'channel':None,'name':None,'nick':None,'user':None,'source':None})
 me=clients[-1]
 try:
  buf=b''
  while True:
   z=sock.recv(4096)
   if not z:break
   buf+=z
   while b'\n' in buf:
    packet,buf=buf.split(b'\n',1); ln=packet.rstrip(b'\r').decode(errors='replace'); ps=ln.split(' ',1); cmd=ps[0].upper(); rest=ps[1] if len(ps)>1 else ''
    if cmd=='PASS':
     token=rest.strip()
     if not user(token):line(sock,'464 * :Password incorrect');token=None
    elif cmd=='NICK':
     nn=rest.split()[0] if rest else ''
     with lock: busy=any(x is not me and x.get('nick')==nn for x in clients)
     if busy:line(sock,'433 * '+nn+' :Nickname is already in use');continue
     me['nick']=nick=nn
     if reg: continue
     if token and user(token) and me.get('got_user'):
      reg=True; me['user']=user(token)
      for n,t in [('001','Welcome'),('002','Your host is huddle'),('003','Created locally'),('004','huddle 1.0 oiwsz biklmnop'),('005','CHANTYPES=#') ,('422','MOTD File is missing')]:line(sock,n+' '+nick+' :'+t)
    elif cmd=='USER':
     me['got_user']=True
     if not token or not user(token):line(sock,'464 '+(nick or '*')+' :Password incorrect');continue
     if nick and not reg:
      reg=True;me['user']=user(token)
      for n,t in [('001','Welcome'),('002','Your host is huddle'),('003','Created locally'),('004','huddle 1.0 oiwsz biklmnop'),('005','CHANTYPES=#'),('422','MOTD File is missing')]:line(sock,n+' '+nick+' :'+t)
    elif cmd=='PING':line(sock,'PONG '+SERVER+' :'+(rest[1:] if rest.startswith(':') else rest))
    elif cmd=='QUIT':return
    elif cmd=='JOIN':
     name=rest.split()[0] if rest else ''; ch=chan(name)
     if not ch:line(sock,'403 '+(nick or '*')+' '+name+' :No such channel');continue
     cu=me.get('user')
     if not cu:line(sock,'464 '+(nick or '*')+' :Password incorrect');continue
     c=db(); c.execute('INSERT OR IGNORE INTO cmembers VALUES(?,?)',(ch['id'],cu['id']));c.commit();c.close()
     me.update(channel=ch['id'],name=name)
     raw(sock,':'+nick+'!'+cu['username']+'@host JOIN '+name)
     line(sock,'353 '+nick+' = '+name+' :'+nick)
     line(sock,'366 '+nick+' '+name+' :End of NAMES')
    elif cmd=='PRIVMSG':
     if ' :' in rest: target,body=rest.split(' :',1)
     else:
      q=rest.split(' ',1);target=q[0];body=q[1] if len(q)>1 else ''
     ch=chan(target); cu=me.get('user')
     if not ch or not cu:line(sock,'404 '+(nick or '*')+' '+target+' :Cannot send');continue
     c=db(); c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],cu['id'],body, __import__('app').now())); mid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; r=c.execute('SELECT * FROM messages WHERE id=?',(mid,)).fetchone(); f=emit(c,ch['id'],'message.created',{'message':msgobj(c,r)});c.commit();c.close();deliver(f,me)
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'):pass
    else:line(sock,'421 '+(nick or '*')+' '+cmd+' :Unknown command')
 finally:
  with lock:
   if me in clients:clients.remove(me)
  try:sock.close()
  except:pass
def main():
 threading.Thread(target=poll,daemon=True).start()
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('0.0.0.0',6667));s.listen(100)
 while True:
  c,a=s.accept();threading.Thread(target=client,args=(c,a),daemon=True).start()
if __name__=='__main__':main()
