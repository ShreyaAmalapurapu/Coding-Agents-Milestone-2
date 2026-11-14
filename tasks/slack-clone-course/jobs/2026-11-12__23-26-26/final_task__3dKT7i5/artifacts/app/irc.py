import socket,threading,sqlite3,time,json,traceback
DB='/app/data/huddle.sqlite'; clients=[]; lock=threading.Lock(); sources={}
def db():
 c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row; c.execute('PRAGMA busy_timeout=5000'); return c
def user_for_token(t):
 c=db();r=c.execute('select u.* from tokens t join users u on u.id=t.user_id where t.token=?',(t,)).fetchone();c.close();return r
def send(s,line):
 try:
  s.sendall((line.replace('\n',' ')+'\r\n').encode())
  return True
 except Exception as e:
  open('/tmp/irc-send.log','a').write(repr(e)+' '+line+'\n')
  return False
def mapping(name):
 if not name.startswith('#') or '/' not in name:return None
 slug,cn=name[1:].split('/',1);c=db();r=c.execute('select ch.*,w.slug from channels ch join workspaces w on w.id=ch.workspace_id where w.slug=? and ch.name=?',(slug,cn)).fetchone();c.close();return r
def chname(c,ch):
 w=c.execute('select slug from workspaces where id=?',(ch['workspace_id'],)).fetchone();return '#'+w['slug']+'/'+ch['name']
def append_irc(u,ch,body,src):
 c=db()
 try:
  c.execute('BEGIN IMMEDIATE'); c.execute('insert into messages(channel_id,author_id,body,created_at) values(?,?,?,?)',(ch['id'],u['id'],body,time.strftime('%Y-%m-%dT%H:%M:%S.000Z',time.gmtime())))
  mid=c.execute('select last_insert_rowid()').fetchone()[0]
  c.execute('insert or ignore into counters(channel_id,n) values(?,0)',(ch['id'],));c.execute('update counters set n=n+1 where channel_id=?',(ch['id'],))
  eid=c.execute('select n from counters where channel_id=?',(ch['id'],)).fetchone()[0]
  au={k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')}
  m={'id':mid,'channel_id':ch['id'],'author_id':u['id'],'author':au,'body':body,'parent_id':None,'reply_count':0,'created_at':c.execute('select created_at from messages where id=?',(mid,)).fetchone()[0],'edited_at':None,'files':[],'reactions':[],'mentions':[]}
  c.execute('insert into events values(?,?,?,?,?)',(ch['id'],eid,'message.created',json.dumps({'message':m}),time.time()));c.commit();sources[(ch['id'],eid)]=src;return eid
 except: c.rollback();return None
 finally:c.close()
def poll():
 while True:
  try:
   c=db(); rows=c.execute('select rowid,channel_id,event_id,payload from events where kind="message.created" and created>? order by rowid',(time.time()-30,)).fetchall()
   for r in rows:
    m=json.loads(r['payload'])['message']; ch=c.execute('select * from channels where id=?',(r['channel_id'],)).fetchone()
    if not ch or not ch['workspace_id']: continue
    name=chname(c,ch); src=sources.get((r['channel_id'],r['event_id'])); au=m.get('author',{}); line=':'+au.get('username','user')+'!'+au.get('username','user')+'@host PRIVMSG '+name+' :'+m.get('body','')
    with lock:
     open('/tmp/irc-debug.log','a').write('event '+str(r['channel_id'])+' '+name+' clients '+str([(z.nick,list(z.joined)) for z in clients])+'\\n')
     for cl in list(clients):
      key=(r['channel_id'],r['event_id'])
      if r['channel_id'] in cl.joined and cl is not src and key not in cl.sent:
       try:
        if send(cl.sock,line): cl.sent.add(key)
       except:
        pass
   c.close()
  except Exception as e:
   traceback.print_exc(file=open('/tmp/irc.log','a'))
   try: c.close()
   except: pass
   time.sleep(.05)
  time.sleep(.05)
class Client:
 def __init__(self,s,a):self.sock=s;self.addr=a;self.nick=None;self.user=None;self.passed=False;self.user_seen=False;self.reg=False;self.joined=set();self.sent=set()
def welcome(x):
 for n,t in [('001','Welcome to Huddle'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1 0'),('005','CHANTYPES=# :are supported'),('422','MOTD File is missing')]:send(x.sock,':huddle '+n+' '+x.nick+' :'+t)
def handle(x):
 buf=''; registered_nicks=set()
 try:
  while True:
   b=x.sock.recv(4096)
   if not b:break
   buf+=b.decode('utf8','replace')
   while '\n' in buf:
    line,buf=buf.split('\n',1);line=line.rstrip('\r'); a=line.split(' ',1);cmd=a[0].upper();rest=a[1] if len(a)>1 else ''
    if cmd=='PASS':
     x.user=user_for_token(rest.lstrip(':').strip())
     if x.user:x.passed=True
     else:send(x.sock,':huddle 464 * :Password incorrect')
    elif cmd=='NICK':
     nn=rest.split()[0] if rest.split() else ''
     with lock:
      taken=any(z is not x and z.nick==nn for z in clients)
     if taken:send(x.sock,':huddle 433 * '+nn+' :Nickname is already in use');continue
     x.nick=nn
     if x.passed and x.user and x.user_seen and not x.reg:x.reg=True;welcome(x)
    elif cmd=='USER':
     if not x.passed:send(x.sock,':huddle 464 * :Password required');continue
     x.user_seen=True
     if x.nick and not x.reg:x.reg=True;welcome(x)
    elif cmd=='PING':send(x.sock,':huddle PONG huddle :'+rest.lstrip(':'))
    elif cmd=='QUIT':return
    elif cmd=='JOIN':
     name=rest.split()[0] if rest.split() else '';ch=mapping(name)
     if not ch or not x.reg:send(x.sock,':huddle 403 '+(x.nick or '*')+' '+name+' :No such channel');continue
     c=db(); member=c.execute('select 1 from cmembers where channel_id=? and user_id=?',(ch['id'],x.user['id'])).fetchone()
     wm=c.execute('select 1 from members where workspace_id=? and user_id=?',(ch['workspace_id'],x.user['id'])).fetchone()
     if not member:
      if not wm:c.execute('insert or ignore into members values(?,?,?)',(ch['workspace_id'],x.user['id'],'member'))
      c.execute('insert or ignore into cmembers values(?,?)',(ch['id'],x.user['id']));c.commit()
     nicks=[z.nick for z in clients if ch['id'] in z.joined and z.nick]
     x.joined.add(ch['id']);c.close();send(x.sock,':'+x.nick+'!'+x.user['username']+'@host JOIN '+name);send(x.sock,':huddle 353 '+x.nick+' = '+name+' :'+(' '.join(nicks+[x.nick])));send(x.sock,':huddle 366 '+x.nick+' '+name+' :End of NAMES list')
    elif cmd=='PRIVMSG':
     if ' :' not in rest:continue
     target,body=rest.split(' :',1);ch=mapping(target.strip())
     if ch and x.reg and ch['id'] in x.joined:
      eid=append_irc(x.user,ch,body,x); 
     else:send(x.sock,':huddle 404 '+(x.nick or '*')+' '+target+' :Cannot send to channel')
    elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'):pass
    else:send(x.sock,':huddle 421 '+(x.nick or '*')+' '+cmd+' :Unknown command')
 finally:
  with lock:
   if x in clients:clients.remove(x)
  try:x.sock.close()
  except:pass
def main():
 threading.Thread(target=poll,daemon=True).start();srv=socket.socket();srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);srv.bind(('0.0.0.0',6667));srv.listen(100)
 while True:
  s,a=srv.accept();x=Client(s,a)
  with lock:clients.append(x)
  threading.Thread(target=handle,args=(x,),daemon=True).start()
if __name__=='__main__':main()
