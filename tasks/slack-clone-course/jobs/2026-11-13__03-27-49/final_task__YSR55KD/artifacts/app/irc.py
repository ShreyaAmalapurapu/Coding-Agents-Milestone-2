import socket,threading,sqlite3,json,time
DB='/app/data/huddle.db'
clients=[]; clients_lock=threading.RLock(); used=set()
def conn():
    c=sqlite3.connect(DB,timeout=5);c.row_factory=sqlite3.Row;c.execute('PRAGMA busy_timeout=5000');return c
def out(c,line):
    try:c.sendall((':huddle '+line+'\r\n').encode())
    except OSError: pass
def raw(c,line):
    try:c.sendall((line+'\r\n').encode())
    except OSError: pass
def token_user(t):
    with conn() as c:return c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone()
def channel(name):
    if not name.startswith('#') or '/' not in name:return None
    slug,cn=name[1:].split('/',1)
    with conn() as c:return c.execute('SELECT ch.*,w.slug,w.owner_id FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=?',(slug,cn)).fetchone()
def mapping(ch):
    return '#%s/%s'%(ch['slug'],ch['name'])
def event(chid,kind,payload):
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM channel_events WHERE channel_id=?',(chid,)).fetchone()[0]
        c.execute('INSERT INTO channel_events VALUES(?,?,?,?,?)',(chid,n,kind,json.dumps(payload),time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())))
        c.commit()
        return n
def message(uid,chid,body,source=None):
    with conn() as c:
        c.execute('BEGIN IMMEDIATE')
        ts=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
        cur=c.execute('INSERT INTO messages(channel_id,author_id,body,parent_id,created_at,mentions) VALUES(?,?,?,?,?,?)',(chid,uid,body,None,ts,'[]'))
        mid=cur.lastrowid
        u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        payload={'id':mid,'channel_id':chid,'author_id':uid,'author':{k:u[k] for k in ('id','username','display_name','timezone','avatar_url','status_text','status_emoji')},'body':body,'parent_id':None,'reply_count':0,'created_at':ts,'edited_at':None,'files':[],'reactions':[],'mentions':[]}
        n=c.execute('SELECT COALESCE(MAX(event_id),0)+1 FROM channel_events WHERE channel_id=?',(chid,)).fetchone()[0]
        if source:payload['_irc_source']=source
        c.execute('INSERT INTO channel_events VALUES(?,?,?,?,?)',(chid,n,'message.created',json.dumps(payload),ts));c.commit()
    return payload
def registration(c,nick):
    for num,text in [('001','Welcome to Huddle IRC'),('002','Your host is huddle'),('003','This server was created today'),('004','huddle huddle 1.0 io'),('005','CHANTYPES=# :are supported by this server'),('422',':MOTD File is missing')]:
        out(c,'%s %s :%s'%(num,nick,text))
def bridge():
    last={}
    while True:
        try:
            with conn() as c: rows=c.execute("SELECT e.channel_id,e.event_id,e.kind,e.payload,ch.name,w.slug FROM channel_events e JOIN channels ch ON ch.id=e.channel_id JOIN workspaces w ON w.id=ch.workspace_id WHERE e.kind='message.created' ORDER BY e.channel_id,e.event_id").fetchall()
            with clients_lock:
                alive=[]
                for cl in clients:
                    try:
                        for e in rows:
                            key=(e['channel_id'],e['event_id'])
                            if last.get(id(cl),{}).get(e['channel_id'],0)>=e['event_id'] or e['channel_id'] not in cl['joined']: continue
                            p=json.loads(e['payload']); source=p.get('_irc_source')
                            if source and source==cl['nick']: continue
                            u=p['author']['username']; raw(cl['sock'],':%s!%s@host PRIVMSG #%s/%s :%s'%(u,u,e['slug'],e['name'],p['body']))
                            last.setdefault(id(cl),{})[e['channel_id']]=e['event_id']
                        alive.append(cl)
                    except Exception: pass
                clients[:]=alive
        except Exception: pass
        time.sleep(.08)
def client(s,addr):
    st={'sock':s,'nick':None,'user':None,'joined':set()};clients.append(st);tok=None;registered=False;buf=b''
    try:
      while True:
        x=s.recv(4096)
        if not x:break
        buf+=x
        while b'\n' in buf:
          packet,buf=buf.split(b'\n',1); line=packet.rstrip(b'\r').decode(errors='replace')
          if not line:continue
          z=line.split(' ',1);cmd=z[0].upper();rest=z[1] if len(z)>1 else ''
          if cmd=='PASS':
            tok=rest.strip()
            if not token_user(tok):out(s,'464 * :Password incorrect');tok=None
          elif cmd=='NICK':
            nn=rest.split()[0] if rest.split() else ''
            with clients_lock:
              if any(x is not st and x.get('nick','').lower()==nn.lower() for x in clients):out(s,'433 * %s :Nickname is already in use'%nn);continue
              st['nick']=nn;used.add(nn)
            if tok and st['user'] and not registered:registered=True;registration(s,nn)
          elif cmd=='USER':
            if not tok:out(s,'464 %s :Password required'%(st['nick'] or '*'))
            else:
              st['user']=token_user(tok)
              if st['nick'] and not registered:registered=True;registration(s,st['nick'])
          elif cmd=='PING':out(s,'PONG huddle :'+(rest[1:] if rest.startswith(':') else rest))
          elif cmd in ('PONG','MODE','WHO','LIST','TOPIC'):pass
          elif cmd=='QUIT':return
          elif cmd=='NAMES':
            ch=channel(rest.split()[0] if rest.split() else '')
            names=[]
            if ch:
              with clients_lock:names=[x['nick'] for x in clients if ch['id'] in x['joined']]
            out(s,'353 %s = %s :%s'%(st['nick'],rest.split()[0] if rest else '*',' '.join(names)));out(s,'366 %s %s :End of NAMES list'%(st['nick'],rest.split()[0] if rest else '*'))
          elif cmd=='JOIN':
            nm=rest.split()[0] if rest.split() else '';ch=channel(nm)
            if not ch:out(s,'403 %s %s :No such channel'%(st['nick'],nm));continue
            with conn() as c:
              if not c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],st['user']['id'])).fetchone():out(s,'403 %s %s :No such channel'%(st['nick'],nm));continue
              c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],st['user']['id']));c.commit()
            st['joined'].add(ch['id']);raw(s,':%s!%s@host JOIN %s'%(st['nick'],st['user']['username'],nm))
            out(s,'353 %s = %s :%s'%(st['nick'],nm,st['nick']));out(s,'366 %s %s :End of NAMES list'%(st['nick'],nm))
          elif cmd=='PRIVMSG':
            a=rest.split(' ',1)
            if len(a)<2:continue
            nm=a[0];body=a[1][1:] if a[1].startswith(':') else a[1];ch=channel(nm)
            if ch and ch['id'] in st['joined']:message(st['user']['id'],ch['id'],body,st['nick'])
          else:out(s,'421 %s %s :Unknown command'%(st['nick'] or '*',cmd))
    except (OSError,ConnectionError):pass
    finally:
      with clients_lock:
        if st in clients:clients.remove(st)
      try:s.close()
      except:pass
def main():
    threading.Thread(target=bridge,daemon=True).start()
    q=socket.socket();q.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);q.bind(('0.0.0.0',6667));q.listen(100)
    while True:
      s,a=q.accept();threading.Thread(target=client,args=(s,a),daemon=True).start()
if __name__=='__main__':main()
