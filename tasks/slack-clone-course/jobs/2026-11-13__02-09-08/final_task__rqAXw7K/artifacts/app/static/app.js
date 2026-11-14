(()=> {
  const $=s=>document.querySelector(s), api='/api';
  let token=localStorage.getItem('huddle.token')||new URLSearchParams(location.search).get('token')||'';
  if(token) localStorage.setItem('huddle.token',token);
  let me=null, workspace=null, channel=null, ws=null;
  const auth=$('#auth-form'), submit=$('#auth-submit'), toggle=$('#auth-toggle');
  auth.dataset.mode='register'; submit.textContent='Sign up';
  let alertBox=document.createElement('div'); alertBox.setAttribute('role','alert'); alertBox.style.color='#e01e5a'; auth.appendChild(alertBox);
  function headers(json=false){const h={}; if(token)h.Authorization='Bearer '+token;if(json)h['Content-Type']='application/json';return h}
  async function request(path,opts={}){opts.headers=Object.assign(headers(!!opts.body),opts.headers||{});return fetch(api+path,opts)}
  async function json(path,opts){const r=await request(path,opts);let j={};try{j=await r.json()}catch{};return {r,j}}
  function showApp(){ $('#auth-modal').hidden=true; $('#app').hidden=false; $('#current-user').textContent=me?me.username:'' }
  function showAuth(){ $('#auth-modal').hidden=false; $('#app').hidden=true; auth.dataset.mode='register';submit.textContent='Sign up';toggle.textContent='Log in' }
  async function loadMe(){
    if(!token){showAuth();return false}
    const x=await json('/auth/me'); if(!x.r.ok){token='';localStorage.removeItem('huddle.token');showAuth();return false}
    me=x.j.user;showApp();await loadWorkspaces();return true
  }
  async function loadWorkspaces(){
    const x=await json('/workspaces'); if(!x.r.ok)return;
    workspace=x.j.workspaces[0]; if(!workspace){$('#workspace-header').textContent='No workspace';return}
    $('#workspace-header').textContent=workspace.name;
    const d=await json('/workspaces/'+encodeURIComponent(workspace.slug));
    renderChannels(d.j.channels||[]);
    if(d.j.channels&&d.j.channels.length) selectChannel(d.j.channels[0]);
  }
  function renderChannels(channels){
    const box=$('#channel-list');box.innerHTML='';
    channels.filter(c=>!c.is_dm).forEach(c=>{
      const e=document.createElement('div');e.className='channel-entry';e.dataset.testid='channel-entry';e.dataset.channelId=c.id;e.dataset.channelName=c.name;e.textContent='#'+c.name;e.onclick=()=>selectChannel(c);box.appendChild(e)
    })
  }
  async function selectChannel(c){
    channel=c;$('#app').dataset.channelId=c.id;$('#channel-title').textContent=c.name;$('#channel-topic').textContent=c.topic||'';
    const x=await json('/channels/'+c.id+'/messages');renderMessages((x.j.messages||[]).slice().reverse());connectWS()
  }
  function makeRow(m){
    const row=document.createElement('div'); row.className='message'; row.dataset.testid='message'; row.dataset.messageId=m.id;
    const av=document.createElement('span'); av.dataset.testid='message-avatar'; av.className='message-avatar'; av.textContent=(m.author?.display_name||m.author?.username||'?')[0].toUpperCase();
    const au=document.createElement('span'); au.dataset.testid='message-author'; au.className='message-author'; au.textContent=m.author?.display_name||m.author?.username||'Unknown';
    const tm=document.createElement('span'); tm.dataset.testid='message-time'; tm.className='message-time'; tm.textContent=new Date(m.created_at).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
    const body=document.createElement('span'); body.dataset.testid='message-body'; body.className='message-body'; body.textContent=m.body;
    const editInput=document.createElement('input'); editInput.dataset.testid='edit-message-input'; editInput.value=m.body; editInput.style.display='none';
    row.append(av,au,tm,body,editInput);
    if(m.edited_at){const ed=document.createElement('span');ed.dataset.testid='edited-indicator';ed.textContent=' (edited)';row.append(ed)}
    const replies=document.createElement('button'); replies.type='button'; replies.dataset.testid='open-thread-btn'; replies.className='open-thread-btn'; replies.textContent=m.reply_count?(m.reply_count+' repl'+(m.reply_count===1?'y':'ies')):'Thread'; const replyCount=document.createElement('span'); replyCount.dataset.testid='reply-count'; replyCount.textContent=m.reply_count?(m.reply_count+' repl'+(m.reply_count===1?'y':'ies')):''; row.append(replyCount); replies.onclick=()=>openThread(m); row.append(replies);
    const react=document.createElement('button'); react.type='button'; react.dataset.testid='reaction-button'; react.textContent='☺'; react.onclick=()=>showPicker(row,m); row.append(react);
    const chips=document.createElement('div'); chips.className='reaction-chips';
    (m.reactions||[]).forEach(x=>{const ch=document.createElement('button');ch.dataset.testid='reaction-chip';ch.textContent=x.emoji+' '+x.count;ch.onclick=async()=>{await json('/messages/'+m.id+'/reactions/'+encodeURIComponent(x.emoji),{method:'DELETE'});refreshMessage(m.id)};chips.append(ch)}); row.append(chips);
    const bar=document.createElement('span');bar.className='hover-toolbar';
    const ed=document.createElement('button');ed.type='button';ed.dataset.testid='edit-message-btn';ed.textContent='Edit';ed.onclick=()=>editRow(row,m);const del=document.createElement('button');del.type='button';del.dataset.testid='delete-message-btn';del.textContent='Delete';del.onclick=async()=>{const z=await json('/messages/'+m.id,{method:'DELETE'});if(z.r.ok)row.remove()};bar.append(ed,del); row.append(bar);
    return row
  }
  function renderMessages(ms){const box=$('#message-list');box.innerHTML='';if(!ms.length){box.textContent='No messages yet. Say hello!';return}ms.forEach(m=>box.appendChild(makeRow(m)))}
  function addMessage(m){if(!channel||Number(m.channel_id)!==Number(channel.id))return;const box=$('#message-list');const old=box.querySelector('[data-message-id="'+m.id+'"]');if(old)return;box.querySelector('.empty-copy')?.remove();if(box.textContent==='No messages yet. Say hello!')box.textContent='';box.appendChild(makeRow(m))}
  async function refreshMessage(id){if(!channel)return;const x=await json('/channels/'+channel.id+'/messages');const m=(x.j.messages||[]).find(z=>Number(z.id)===Number(id));const old=document.querySelector('[data-message-id="'+id+'"]');if(m&&old)old.replaceWith(makeRow(m))}
  document.addEventListener('click',e=>{const b=e.target.closest('[data-testid="edit-message-btn"]');if(b){const row=b.closest('[data-testid="message"]');const id=Number(row.dataset.messageId);const body=row.querySelector('[data-testid="message-body"]');editRow(row,{id,body:body?body.textContent:''})}});
  function editRow(row,m){
    let input=row.querySelector('[data-testid="edit-message-input"]');
    const body=row.querySelector('[data-testid="message-body"]');
    if(!input){input=document.createElement('input');input.dataset.testid='edit-message-input';input.type='text';input.value=m.body;row.appendChild(input)}
    input.style.display='inline-block'; input.style.width='80%'; if(body)body.style.display='none'; input.focus(); input.select();
    input.onkeydown=async e=>{if(e.key==='Escape'){refreshMessage(m.id)}if(e.key==='Enter'){const x=await json('/messages/'+m.id,{method:'PATCH',body:JSON.stringify({body:input.value})});if(x.r.ok)row.replaceWith(makeRow(x.j.message))}}
  }
  function showPicker(row,m){let p=row.querySelector('[data-testid="emoji-picker"]');if(p){p.remove();return}p=document.createElement('div');p.dataset.testid='emoji-picker';['👍','❤️','😂','🎉','👀','🚀'].forEach(e=>{const b=document.createElement('button');b.dataset.testid='emoji-option';b.textContent=e;b.onclick=async()=>{await json('/messages/'+m.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:e})});p.remove();refreshMessage(m.id)};p.append(b)});row.append(p)}
  function rowForMessage(id){return document.querySelector('[data-message-id="'+id+'"] [data-testid="open-thread-btn"]')}
  async function openThread(m){
    const panel=$('#thread-panel');
    panel.hidden=false; panel.style.display='block'; panel.dataset.parentId=m.id;
    const x=await json('/messages/'+m.id+'/replies');
    $('#thread-messages').innerHTML='<div>'+m.body+'</div>'+(x.j.replies||[]).map(r=>'<div>'+r.body+'</div>').join('');
    const threadSend=document.querySelector('[data-testid="thread-send"]');
    if(threadSend) threadSend.onclick=async()=>{
      const i=document.querySelector('[data-testid="thread-input"]');
      if(!i || !i.value.trim()) return;
      const z=await json('/channels/'+channel.id+'/messages',{method:'POST',body:JSON.stringify({body:i.value,parent_id:m.id})});
      if(z.r.ok){
        i.value='';
        m.reply_count=(m.reply_count||0)+1;
        await openThread(m);
        const rb=rowForMessage(m.id);
        if(rb) rb.textContent=m.reply_count+' repl'+(m.reply_count===1?'y':'ies');
        const rc=document.querySelector('[data-message-id="'+m.id+'"] [data-testid="reply-count"]');
        if(rc) rc.textContent=m.reply_count+' repl'+(m.reply_count===1?'y':'ies');
      }
    };
  }
  const closeThread=document.querySelector('[data-testid="close-thread"]'); if(closeThread) closeThread.onclick=()=>{$('#thread-panel').hidden=true;$('#thread-panel').style.display='none'}
  function connectWS(){
    if(ws)try{ws.close()}catch{}
    const proto=location.protocol==='https:'?'wss':'ws';ws=new WebSocket(proto+'://'+location.host+'/api/ws?token='+encodeURIComponent(token));
    ws.onopen=()=>{if(channel)ws.send(JSON.stringify({type:'subscribe',channel_id:channel.id}))};
    ws.onmessage=e=>{try{const m=JSON.parse(e.data);if(m.type==='message.created'||m.type==='message.reply')addMessage(m.message);if(m.type==='message.updated'){const r=document.querySelector('[data-message-id="'+m.message.id+'"] .message-body');if(r)r.textContent=m.message.body}if(m.type==='message.deleted'){const r=document.querySelector('[data-message-id="'+m.message_id+'"]');if(r)r.remove()}}catch{}}
  }
  auth.onsubmit=async e=>{
    e.preventDefault();alertBox.textContent='';
    const f=new FormData(auth), mode=auth.dataset.mode==='register'?'register':'login';
    const body=Object.fromEntries(f);if(mode==='login')delete body.display_name;
    const x=await json('/auth/'+mode,{method:'POST',body:JSON.stringify(body)});
    if(!x.r.ok){alertBox.textContent=x.j.error||'Invalid input';return}
    token=x.j.token;localStorage.setItem('huddle.token',token);me=x.j.user;showApp();await loadWorkspaces()
  };
  toggle.onclick=()=>{const reg=auth.dataset.mode==='register';auth.dataset.mode=reg?'login':'register';submit.textContent=reg?'Log in':'Sign up';toggle.textContent=reg?'Create account':'Log in';alertBox.textContent=''};
  const logout=$('#logout-btn'); if(logout) logout.onclick=()=>{token='';localStorage.removeItem('huddle.token');if(ws)ws.close();showAuth()};
  async function send(){const input=$('#message-input'),body=input.value;if(!body.trim())return;if(!channel){const e=document.querySelector('.channel-entry');if(e)channel={id:e.dataset.channelId,name:e.dataset.channelName}}if(!channel)return;const x=await json('/channels/'+channel.id+'/messages',{method:'POST',body:JSON.stringify({body})});if(x.r.ok){addMessage(x.j.message);input.value=''}}
  const sendBtn=$('#send-btn'), msgInput=$('#message-input'); if(sendBtn) sendBtn.addEventListener('click',e=>{e.preventDefault();send()}); if(msgInput) msgInput.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();send()}}); document.addEventListener('click',e=>{if(e.target&&e.target.closest&&e.target.closest('[data-testid="send-btn"]')){e.preventDefault();send()}});
  const wf=$('[data-testid="workspace-create-form"]');
  if(wf) wf.onsubmit=async e=>{e.preventDefault();const body=Object.fromEntries(new FormData(wf));if(!body.slug||!body.name)return;const x=await json('/workspaces',{method:'POST',body:JSON.stringify(body)});if(x.r.ok){workspace=x.j.workspace;showApp();await loadWorkspaces()}else{let a=$('[data-testid="join-workspace-error"]');a.textContent=x.j.error||'Unable to create workspace'}};
  const emptyCreate=$('[data-testid="empty-state-create-workspace"]'); if(emptyCreate) emptyCreate.onclick=()=>{if(wf)wf.querySelector('input[name="slug"]').focus()};

  function modal(id,on){const x=$(id);if(x)x.style.display=on?'block':'none'}
  function formAlert(form,text){let a=form.querySelector('[role="alert"]');if(!a){a=document.createElement('div');a.setAttribute('role','alert');form.appendChild(a)}a.textContent=text||''}
  const joinForm=$('[data-testid="join-workspace-form"]');
  if(joinForm) joinForm.onsubmit=async e=>{e.preventDefault();const code=new FormData(joinForm).get('code');const x=await json('/invitations/'+encodeURIComponent(code)+'/accept',{method:'POST',body:'{}'});if(x.r.ok){await loadWorkspaces()}else{$('[data-testid="join-workspace-error"]').textContent=x.j.error||'Invalid invitation'}};
  const newBtn=$('[data-testid="new-channel-btn"]');if(newBtn)newBtn.onclick=()=>modal('#create-channel-modal',true);
  const cf=$('[data-testid="create-channel-form"]');if(cf)cf.onsubmit=async e=>{e.preventDefault();const b=Object.fromEntries(new FormData(cf));b.is_private=!!$('#create-channel-private')?.checked;const x=await json('/workspaces/'+encodeURIComponent(workspace.slug)+'/channels',{method:'POST',body:JSON.stringify(b)});if(x.r.ok){modal('#create-channel-modal',false);await loadWorkspaces();const es=[...document.querySelectorAll('[data-testid="channel-entry"]')].find(z=>z.dataset.channelName===b.name);if(es)es.click()}else formAlert(cf,x.j.error||'Unable to create channel')};
  const cancel=$('[data-testid="create-channel-cancel"]');if(cancel)cancel.onclick=()=>modal('#create-channel-modal',false);
  const cs=$('[data-testid="channel-settings-btn"]');if(cs)cs.onclick=()=>modal('#channel-settings-modal',true);
  const cclose=$('[data-testid="channel-settings-close"]');if(cclose)cclose.onclick=()=>modal('#channel-settings-modal',false);
  const topicBtn=$('[data-testid="channel-topic-submit"]');if(topicBtn)topicBtn.onclick=async()=>{const v=$('[data-testid="channel-topic-input"]').value;const x=await json('/channels/'+channel.id,{method:'PATCH',body:JSON.stringify({topic:v})});if(x.r.ok){channel=x.j.channel;$('#channel-topic').textContent=channel.topic}};
  const archive=$('[data-testid="archive-channel-btn"]');if(archive)archive.onclick=async()=>{const x=await json('/channels/'+channel.id+'/archive',{method:'POST',body:'{}'});if(x.r.ok){channel=x.j.channel;$('#channel-topic').textContent=channel.topic}};
  const unarchive=$('[data-testid="unarchive-channel-btn"]');if(unarchive)unarchive.onclick=async()=>{const x=await json('/channels/'+channel.id+'/unarchive',{method:'POST',body:'{}'});if(x.r.ok)channel=x.j.channel};
  const wsBtn=$('[data-testid="workspace-settings-btn"]');if(wsBtn)wsBtn.onclick=async()=>{modal('#workspace-settings-modal',true);const m=await json('/workspaces/'+encodeURIComponent(workspace.slug)+'/members');const pane=$('[data-testid="settings-pane-members"]');pane.innerHTML='';(m.j.members||[]).forEach(x=>{const r=document.createElement('div');r.dataset.testid='member-row';r.innerHTML='<span class="role-badge" data-testid="role-badge">'+x.role+'</span><span>'+x.user_id+'</span>';pane.appendChild(r)})};
  const wclose=$('[data-testid="workspace-settings-close"]');if(wclose)wclose.onclick=()=>modal('#workspace-settings-modal',false);
  const invBtn=$('[data-testid="create-invitation-btn"]');if(invBtn)invBtn.onclick=()=>modal('#create-invitation-form',true);
  const invForm=$('#create-invitation-form');if(invForm)invForm.onsubmit=async e=>{e.preventDefault();const x=await json('/workspaces/'+encodeURIComponent(workspace.slug)+'/invitations',{method:'POST',body:'{}'});if(x.r.ok){let z=invForm.querySelector('[data-testid="invitation-code"]');z.textContent=x.j.invitation.code}};
  const invTab=$('[data-testid="settings-tab-invitations"]');if(invTab)invTab.onclick=()=>{$('[data-testid="settings-pane-invitations"]').style.display='block'};
  const memTab=$('[data-testid="settings-tab-members"]');if(memTab)memTab.onclick=()=>{$('[data-testid="settings-pane-members"]').style.display='block'};
  loadMe();
})();
