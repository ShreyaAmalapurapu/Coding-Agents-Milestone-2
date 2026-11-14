import socket,threading,sys,os,sqlite3,json,time
sys.path.insert(0,'/app'); from app import db,auth,make_message,irc_clients,irc_lock
nick_lock=threading.RLock(); nick_users=set()
def send(s,x): s.sendall((':srv '+x+'\r\n').encode())
def client(s):
 tok=None;nick=None;uid=None;ircuser='user';registered=False;user_sent=False; chans=set(); names={}
 with irc_lock: irc_clients.append({'sock':s,'channels':chans,'names':names})
 try:
  buf=b''
  while True:
   q=s.recv(4096)
   if not q:break
   buf+=q
   while b'\n' in buf:
    line,buf=buf.split(b'\n',1); line=line.decode(errors='ignore').strip(); a=line.split(' ',1);cmd=a[0].upper(); rest=a[1] if len(a)>1 else ''
    if cmd=='PASS':
     c=db();r=c.execute('select user_id from tokens where token=?',(rest.strip(),)).fetchone();uid=r[0] if r else None;c.close()
     if not uid:send(s,'464 * :Password incorrect')
    elif cmd=='NICK':
     n=rest.split()[0]
     with nick_lock:
      if n.lower() in nick_users and n.lower() != (nick or '').lower():
       send(s,'433 '+(nick or '*')+' '+n+' :Nickname is already in use'); continue
      nick=n
      if uid and user_sent and not registered:
       with nick_lock: nick_users.add(nick.lower())
       registered=True
       for x in ['001','002','003','004','005']:send(s,f'{x} {nick} :Welcome')
       send(s,'422 '+nick+' :MOTD File is missing')

    elif cmd=='USER':
     user_sent=True
     z=rest.split(); ircuser=z[0] if z else 'user'
     if not uid:send(s,'464 * :Password incorrect')
     elif nick and not registered:
      with nick_lock:
       if nick.lower() in nick_users: send(s,'433 '+nick+' :Nickname is already in use'); continue
       nick_users.add(nick.lower())
      registered=True
      for x in ['001','002','003','004','005']:send(s,f'{x} {nick} :Welcome')
      send(s,'422 '+nick+' :MOTD File is missing')
    elif cmd=='PING':send(s,'PONG '+rest)
    elif cmd=='QUIT':return
    elif cmd=='JOIN':
     if not registered:continue
     target=rest.split()[0]; c=db();r=c.execute('select ch.id,ch.name,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where \"#\"||w.slug||\"/\"||ch.name=?',(target,)).fetchone()
     if not r:send(s,'403 '+target+' :No such channel');continue
     chans.add(r[0]);names[r[0]]=target;c.execute('insert or ignore into channel_members values(?,?)',(r[0],uid));c.commit(); webuser=c.execute('select username from users where id=?',(uid,)).fetchone()[0]; s.sendall(f':{nick}!{webuser}@localhost JOIN {target}\r\n'.encode());send(s,f'353 {nick} = {target} :{nick}');send(s,f'366 {nick} {target} :End of NAMES')
    elif cmd=='PRIVMSG':
     if not registered:continue
     q=rest.split(' ',1);target=q[0];text=q[1].lstrip(':') if len(q)>1 else '';c=db();r=c.execute('select ch.id from channels ch join workspaces w on w.id=ch.workspace_id where \"#\"||w.slug||\"/\"||ch.name=?',(target,)).fetchone()
     if r: make_message(c,r[0],uid,text)
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'): pass
    else:send(s,'421 '+cmd+' :Unknown command')
 finally:
  with nick_lock:
   if nick: nick_users.discard(nick.lower())
  with irc_lock:
   for x in irc_clients:
    if x['sock'] is s:irc_clients.remove(x)
  s.close()
def event_poller():
 seen={}
 while True:
  time.sleep(.1)
  c=db()
  with irc_lock: active=list(irc_clients)
  for e in c.execute("select channel_id,event_id,type,payload from events where type='message.created' order by channel_id,event_id"):
   ch,ei,typ,payload=e
   if ei <= seen.get(ch,0): continue
   seen[ch]=ei
   m=json.loads(payload).get('message',{})
   for cl in active:
    if ch in cl.get('channels',set()):
     target=cl.get('names',{}).get(ch)
     if target:
      nick=m.get('author',{}).get('username','user')
      try: cl['sock'].sendall(f":{nick}!{nick}@localhost PRIVMSG {target} :{m.get('body','').replace(chr(10),' ')}\r\n".encode())
      except: pass
  c.close()
threading.Thread(target=event_poller,daemon=True).start()
def main():
 z=socket.socket();z.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);z.bind(('0.0.0.0',6667));z.listen()
 while True:threading.Thread(target=client,args=(z.accept()[0],),daemon=True).start()
if __name__=='__main__':main()
