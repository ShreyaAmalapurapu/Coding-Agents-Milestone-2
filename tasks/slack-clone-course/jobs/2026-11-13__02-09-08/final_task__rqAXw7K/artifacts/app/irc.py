#!/usr/bin/env python3
import socket, threading, sqlite3, json, time
DB='/app/data/huddle.db'
clients={}
nicks={}
lock=threading.Lock()
def db():
    c=sqlite3.connect(DB,timeout=5); c.row_factory=sqlite3.Row; return c
def send(s,line):
    try:
        prefix='' if line.startswith(':') else ':huddle '
        s.sendall((prefix+line+'\r\n').encode())
    except: pass
def user_for_token(t):
    c=db(); r=c.execute('SELECT u.* FROM tokens t JOIN users u ON u.id=t.user_id WHERE t.token=?',(t,)).fetchone(); c.close(); return r
def num(s,n,nick,text): send(s,f'{n:03d} {nick} {text}')
def channel_by_name(name):
    if not name.startswith('#') or '/' not in name[1:]: return None
    slug,chname=name[1:].split('/',1); c=db()
    r=c.execute('SELECT ch.*,w.slug,w.name wname FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE w.slug=? AND ch.name=? AND ch.is_dm=0',(slug,chname)).fetchone(); c.close(); return r
def irc_name(c,ch): return '#'+ch['slug']+'/'+ch['name']
def message_obj(c,r):
    a=c.execute('SELECT * FROM users WHERE id=?',(r['author_id'],)).fetchone()
    return {'id':r['id'],'channel_id':r['channel_id'],'author_id':r['author_id'],'author':dict(a),
      'body':r['body'],'parent_id':r['parent_id'],'reply_count':0,'created_at':r['created_at'],
      'edited_at':None,'files':[],'reactions':[],'mentions':[]}
def append(c,cid,kind,payload):
    c.execute('INSERT OR IGNORE INTO event_seq(channel_id,last_id) VALUES(?,0)',(cid,))
    c.execute('UPDATE event_seq SET last_id=last_id+1 WHERE channel_id=?',(cid,))
    eid=c.execute('SELECT last_id FROM event_seq WHERE channel_id=?',(cid,)).fetchone()['last_id']
    c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(cid,eid,kind,json.dumps(payload),time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())))
    return eid
def broadcaster():
    seen={}
    while True:
        try:
            c=db(); rows=c.execute("SELECT * FROM events WHERE kind IN ('message.created','message.reply') ORDER BY rowid").fetchall()
            for r in rows:
                key=(r['channel_id'],r['event_id'])
                if key in seen: continue
                seen[key]=1
                p=json.loads(r['payload']); m=p.get('message',{}); ch=c.execute('SELECT ch.*,w.slug FROM channels ch JOIN workspaces w ON w.id=ch.workspace_id WHERE ch.id=?',(r['channel_id'],)).fetchone()
                if not ch: continue
                name=irc_name(c,ch); a=c.execute('SELECT username FROM users WHERE id=?',(m.get('author_id'),)).fetchone()
                if not a: continue
                with lock:
                    targets=list(clients.values())
                for item in targets:
                    if name in item['joined'] and item.get('registered') and item.get('user_id')!=m.get('author_id'):
                        send(item['sock'],f':{a["username"]}!{a["username"]}@huddle PRIVMSG {name} :{m.get("body","")}')
            c.close()
        except Exception: pass
        time.sleep(.08)
def welcome(s,nick,user,host):
    num(s,1,nick,f':Welcome to the Huddle IRC Network {nick}!{user["username"]}@{host}')
    num(s,2,nick,':Your host is huddle'); num(s,3,nick,':This server was created locally')
    num(s,4,nick,':huddle 1.0 o o'); num(s,5,nick,':CHANTYPES=# NETWORK=huddle'); num(s,422,nick,':MOTD File is missing')
def handle(sock,addr):
    token=None; user=None; nick=None; seen_user=False; registered=False; joined=set(); buf=b''
    item={'sock':sock,'joined':joined,'registered':False,'user_id':None}
    try:
        with lock: clients[id(sock)]=item
        sock.settimeout(1)
        while True:
            try:
                x=sock.recv(4096)
                if not x: break
                buf+=x
            except socket.timeout: continue
            while b'\n' in buf:
                raw,buf=buf.split(b'\n',1); line=raw.rstrip(b'\r').decode(errors='replace')
                if not line: continue
                cmd,*rr=line.split(' ',1); cmd=cmd.upper(); rest=rr[0] if rr else ''; args=rest.split()
                if cmd=='PASS':
                    token=rest[1:] if rest.startswith(':') else rest
                    if not user_for_token(token): send(sock,'464 * :Password incorrect')
                elif cmd=='NICK':
                    new=args[0] if args else ''
                    with lock: taken=new in nicks and nicks.get(new) is not item
                    if taken: send(sock,f'433 {nick or "*"} {new} :Nickname is already in use')
                    else:
                        with lock:
                            if nick: nicks.pop(nick,None)
                            nick=new; nicks[nick]=item
                        if seen_user and token and not registered:
                            user=user_for_token(token)
                            if user: registered=True; item['registered']=True; item['user_id']=user['id']; welcome(sock,nick,user,addr[0])
                elif cmd=='USER':
                    seen_user=True
                    if not token: send(sock,'464 * :Password required')
                    elif nick and not registered:
                        user=user_for_token(token)
                        if user: registered=True; item['registered']=True; item['user_id']=user['id']; welcome(sock,nick,user,addr[0])
                elif cmd=='PING':
                    x=rest[1:] if rest.startswith(':') else rest; send(sock,f'PONG huddle :{x}')
                elif cmd=='QUIT': return
                elif cmd=='JOIN' and registered:
                    name=args[0] if args else ''; ch=channel_by_name(name)
                    if not ch: num(sock,403,nick,f'{name} :No such channel'); continue
                    c=db()
                    member=c.execute('SELECT 1 FROM members WHERE workspace_id=? AND user_id=?',(ch['workspace_id'],user['id'])).fetchone()
                    if not member: num(sock,403,nick,f'{name} :Not a member'); c.close(); continue
                    c.execute('INSERT OR IGNORE INTO channel_members VALUES(?,?)',(ch['id'],user['id'])); c.commit()
                    joined.add(name); c.close()
                    send(sock,f':{nick}!{user["username"]}@{addr[0]} JOIN {name}')
                    num(sock,353,nick,f'= {name} :{nick}'); num(sock,366,nick,f'{name} :End of NAMES list')
                elif cmd=='PRIVMSG' and registered:
                    if not args or ':' not in rest: continue
                    target=args[0]; body=rest.split(':',1)[1]; ch=channel_by_name(target)
                    if not ch: num(sock,403,nick,f'{target} :No such channel'); continue
                    if target not in joined: num(sock,442,nick,f'{target} :You are not on that channel'); continue
                    c=db(); t=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
                    cur=c.execute('INSERT INTO messages(channel_id,author_id,body,created_at) VALUES(?,?,?,?)',(ch['id'],user['id'],body,t)); mid=cur.lastrowid
                    m={'id':mid,'channel_id':ch['id'],'author_id':user['id'],'body':body,'parent_id':None,'reply_count':0,'created_at':t,'edited_at':None,'files':[],'reactions':[],'mentions':[]}
                    append(c,ch['id'],'message.created',{'message':m}); c.commit(); c.close()
                elif cmd in ('NAMES','WHO','LIST','TOPIC','MODE','PONG'):
                    if registered and cmd=='NAMES': num(sock,353,nick,'= * :'); num(sock,366,nick,'* :End of NAMES list')
                elif cmd not in ('PASS','USER'): send(sock,f'421 {nick or "*"} {cmd} :Unknown command')
    finally:
        with lock:
            clients.pop(id(sock),None)
            if nick and nicks.get(nick) is item: nicks.pop(nick,None)
        try: sock.close()
        except: pass
def main():
    threading.Thread(target=broadcaster,daemon=True).start()
    srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); srv.bind(('0.0.0.0',6667)); srv.listen(100)
    while True:
        s,a=srv.accept(); threading.Thread(target=handle,args=(s,a),daemon=True).start()
if __name__=='__main__': main()
