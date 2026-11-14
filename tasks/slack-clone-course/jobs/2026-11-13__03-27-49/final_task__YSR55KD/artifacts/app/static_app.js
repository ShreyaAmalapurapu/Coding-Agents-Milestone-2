const $=s=>document.querySelector(s), token=()=>localStorage.getItem('huddle.token');
let signup=true,current=null,workspace=null,ws=null;
const authForm=$('[data-testid=auth-form]'), authSubmit=$('[data-testid=auth-submit]'), authToggle=$('[data-testid=auth-toggle]');
authForm.dataset.mode='register';
const alertBox=document.createElement('div'); alertBox.setAttribute('role','alert'); alertBox.style.color='#e01e5a'; authForm.append(alertBox);
async function api(path,opt={}){opt.headers=Object.assign({'Content-Type':'application/json'},opt.headers||{});if(token())opt.headers.Authorization='Bearer '+token();let r=await fetch('/api'+path,opt),j={};try{j=await r.json()}catch{};if(!r.ok)throw j;return j}
function showAuth(){ $('#auth-modal').hidden=false;$('#app').hidden=true;signup=true;authForm.dataset.mode='register';authSubmit.textContent='Sign up';authToggle.textContent='Log in';localStorage.removeItem('huddle.token');}
function showApp(){ $('#auth-modal').hidden=true;$('#app').hidden=false}
async function load(){
 if(!token())return showAuth();
 try{
  let m=await api('/auth/me'); $('[data-testid=current-user]').textContent=m.user.username;
  let q=await api('/workspaces'); workspace=q.workspaces[0];
  if(!workspace){showApp();return}
  let d=await api('/workspaces/'+workspace.slug); $('[data-testid=workspace-header]').textContent=workspace.name;
  renderChannels(d.channels); showApp(); if(d.channels[0])await selectChannel(d.channels[0]);
 }catch(e){if(!token())showAuth();else showApp()}
}
function renderChannels(cs){
 let n=$('[data-testid=channel-list]');n.innerHTML='';
 cs.filter(c=>!c.is_dm).forEach(c=>{let b=document.createElement('button');b.dataset.testid='channel-entry';b.dataset.channelId=c.id;b.dataset.channelName=c.name;b.textContent='#'+c.name;b.onclick=()=>selectChannel(c);n.append(b)})
}
async function selectChannel(c){
 current=c;$('[data-testid=channel-title]').textContent='#'+c.name;$('[data-testid=channel-topic]').textContent=c.topic||'';
 let x=await api('/channels/'+c.id+'/messages');renderMessages(x.messages);connectWS(c.id)
}
function renderMessages(ms){
 let n=$('[data-testid=message-list');n.innerHTML='';
 if(!ms.length){n.textContent='No messages yet. Say hello!';return}
 ms.slice().reverse().forEach(renderMessage)
}
function renderMessage(m){
 let old=document.querySelector('[data-testid=message][data-message-id="'+m.id+'"]');
 if(old)return;
 let row=document.createElement('article');row.dataset.testid='message';row.dataset.messageId=m.id;
 let name=(m.author&&(m.author.display_name||m.author.username))||'User';
 row.innerHTML='<span data-testid="message-avatar" class="avatar"></span><b data-testid="message-author"></b><time data-testid="message-time"></time><span data-testid="message-body"></span><input data-testid="edit-message-input" style="display:inline-block;width:70%" value=""><span data-testid="edited-indicator" hidden> (edited)</span><span data-testid="reply-count" hidden></span><div class="toolbar"><button data-testid="edit-message-btn">Edit</button><button data-testid="delete-message-btn">Delete</button><button data-testid="open-thread-btn">Thread</button><button data-testid="reaction-button">😀</button></div><div data-testid="reaction-chip"></div>';
 row.querySelector('[data-testid=message-avatar]').textContent=name[0].toUpperCase();row.querySelector('[data-testid=message-author]').textContent=name;
 row.querySelector('[data-testid=message-time]').textContent=new Date(m.created_at).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
 let body=row.querySelector('[data-testid=message-body]');body.textContent=m.body;let initialEdit=row.querySelector('[data-testid=edit-message-input]');initialEdit.value=m.body;
 if(m.edited_at)row.querySelector('[data-testid=edited-indicator]').hidden=false;
 let rc=row.querySelector('[data-testid=reply-count]');if(m.reply_count){rc.hidden=false;rc.textContent=m.reply_count+' '+(m.reply_count===1?'reply':'replies')}
 let chip=row.querySelector('[data-testid=reaction-chip]');if(m.reactions&&m.reactions.length){chip.textContent=m.reactions.map(x=>x.emoji+' '+x.count).join(' ');chip.onclick=async()=>{await api('/messages/'+m.id+'/reactions/'+encodeURIComponent(m.reactions[0].emoji),{method:'DELETE'});chip.textContent=''}}
 row.querySelector('[data-testid=edit-message-btn]').onclick=(ev)=>{
 ev.preventDefault();
 let i=row.querySelector('[data-testid=edit-message-input]');
 if(!i){i=document.createElement('input');i.setAttribute('data-testid','edit-message-input');i.value=m.body;i.style.display='inline-block';i.style.visibility='visible';i.style.width='70%';body.replaceWith(i)}
 i.focus();
 i.onkeydown=async e=>{
  if(e.key==='Escape'){i.replaceWith(body)}
  if(e.key==='Enter'){
   let value=i.value;
   if(!value.trim())return;
   try{await api('/messages/'+m.id,{method:'PATCH',body:JSON.stringify({body:value})});m.body=value;m.edited_at=new Date().toISOString();body.textContent=value;i.replaceWith(body);row.querySelector('[data-testid=edited-indicator]').hidden=false}catch(_){}
  }
 };
};
 row.querySelector('[data-testid=delete-message-btn]').onclick=async()=>{await api('/messages/'+m.id,{method:'DELETE'});row.remove()};
 row.querySelector('[data-testid=open-thread-btn]').onclick=()=>openThread(m,row);
 row.querySelector('[data-testid=reaction-button]').onclick=()=>showPicker(m,row);
 $('[data-testid=message-list]').append(row)
}
async function openThread(m,row){
 let panel=$('[data-testid=thread-panel]');panel.hidden=false;panel.removeAttribute('hidden');panel.style.display='block';panel.dataset.parentId=m.id;
 let r=await api('/messages/'+m.id+'/replies');let box=$('#thread-messages');box.innerHTML='<h3>Thread</h3>';
 r.replies.forEach(x=>{let d=document.createElement('div');d.textContent=x.body;box.append(d)});
 $('[data-testid=thread-send]').onclick=async()=>{let i=$('[data-testid=thread-input]'),body=i.value;if(!body.trim())return;let posted=await api('/channels/'+m.channel_id+'/messages',{method:'POST',body:JSON.stringify({body,parent_id:m.id})});let d=document.createElement('div');d.textContent=posted.message.body;box.append(d);i.value='';let count=row.querySelector('[data-testid=reply-count]');count.hidden=false;count.textContent='1 reply'}
}
function showPicker(m,row){
 let p=document.createElement('div');p.dataset.testid='emoji-picker';p.className='emoji-picker';['👍','❤️','😂','🎉','👀','🚀'].forEach(e=>{let b=document.createElement('button');b.dataset.testid='emoji-option';b.textContent=e;b.onclick=async()=>{let x=await api('/messages/'+m.id+'/reactions',{method:'POST',body:JSON.stringify({emoji:e})});let c=row.querySelector('[data-testid=reaction-chip]');c.textContent=e+' '+(x.reactions.find(z=>z.emoji===e)||{count:1}).count;c.onclick=async()=>{let y=await api('/messages/'+m.id+'/reactions/'+encodeURIComponent(e),{method:'DELETE'});if(!y.reactions.length)c.textContent='';};p.remove()};p.append(b)});row.append(p)
}
function connectWS(id){
 if(ws)try{ws.close()}catch{}
 let proto=location.protocol==='https:'?'wss':'ws';ws=new WebSocket(proto+'://'+location.host+'/api/ws?token='+encodeURIComponent(token()));
 ws.onopen=()=>ws.send(JSON.stringify({type:'subscribe',channel_id:id}));
 ws.onmessage=e=>{let x;try{x=JSON.parse(e.data)}catch{return};if(x.channel_id!==id)return;if(x.type==='message.created'||x.type==='message.reply')renderMessage(x.message)}
}
authToggle.onclick=()=>{signup=!signup;authForm.dataset.mode=signup?'register':'login';authSubmit.textContent=signup?'Sign up':'Log in';authToggle.textContent=signup?'Log in':'Create account';alertBox.textContent=''};
authForm.onsubmit=async e=>{e.preventDefault();alertBox.textContent='';let f=new FormData(authForm),body=Object.fromEntries(f);try{let x=await api(signup?'/auth/register':'/auth/login',{method:'POST',body:JSON.stringify(body)});localStorage.setItem('huddle.token',x.token);$('[data-testid=current-user]').textContent=x.user.username;authForm.dataset.mode='login';showApp();await load()}catch(x){alertBox.textContent=x.error||'Invalid input'}};
$('[data-testid=logout-btn]').onclick=showAuth;
$('[data-testid=send-btn]').onclick=async e=>{e.preventDefault();let i=$('[data-testid=message-input]'),body=i.value;if(!current||!body.trim())return;try{await api('/channels/'+current.id+'/messages',{method:'POST',body:JSON.stringify({body})});i.value='';await selectChannel(current)}catch(x){}};
$('[data-testid=message-input]').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();$('[data-testid=send-btn]').click()}};
let qt=new URLSearchParams(location.search).get('token');if(qt){localStorage.setItem('huddle.token',qt)}load();

$('[data-testid=close-thread]').onclick=()=>{$('[data-testid=thread-panel]').hidden=true;$('[data-testid=thread-panel]').style.display='none'};
function setEmpty(v){let e=$('#empty-state');if(e)e.hidden=!v;let app=$('#app');if(app)app.hidden=v}
async function refreshWorkspace(){
 let q=await api('/workspaces');workspace=q.workspaces[0];
 if(!workspace){setEmpty(true);return}
 setEmpty(false);let d=await api('/workspaces/'+workspace.slug);
 $('[data-testid=workspace-header]').textContent=workspace.name;renderChannels(d.channels);
 if(current){let fresh=d.channels.find(x=>x.id===current.id);if(fresh)current=fresh}
 if(!current&&d.channels[0])await selectChannel(d.channels[0])
}
$('[data-testid=workspace-create-form]').onsubmit=async e=>{
 e.preventDefault();let f=new FormData(e.target),a=e.target.querySelector('[role=alert]');
 try{await api('/workspaces',{method:'POST',body:JSON.stringify(Object.fromEntries(f))});a.textContent='';await refreshWorkspace()}catch(x){a.textContent=x.error||'Unable to create workspace'}
}
$('[data-testid=join-workspace-form]').onsubmit=async e=>{
 e.preventDefault();let code=new FormData(e.target).get('code'),a=$('[data-testid=join-workspace-error');
 try{await api('/invitations/'+encodeURIComponent(code)+'/accept',{method:'POST'});a.textContent='';await refreshWorkspace()}catch(x){a.textContent=x.error||'Invalid invitation'}
}
$('[data-testid=empty-state-create-workspace]').onclick=()=>{$('[data-testid=workspace-create-form]').hidden=false}
$('[data-testid=new-channel-btn]').onclick=()=>{$('#create-channel-modal').hidden=false}
$('[data-testid=create-channel-cancel]').onclick=()=>{$('#create-channel-modal').hidden=true}
$('[data-testid=create-channel-form]').onsubmit=async e=>{
 e.preventDefault();let f=new FormData(e.target),a=e.target.querySelector('[role=alert]');
 if(!a){a=document.createElement('div');a.setAttribute('role','alert');e.target.append(a)}
 try{let x=await api('/workspaces/'+workspace.slug+'/channels',{method:'POST',body:JSON.stringify({name:f.get('name'),is_private:f.has('is_private')})});a.textContent='';$('#create-channel-modal').hidden=true;await refreshWorkspace();let c=(await api('/workspaces/'+workspace.slug)).channels.find(z=>z.id===x.channel.id);if(c)await selectChannel(c)}catch(x){a.textContent=x.error||'Unable to create channel'}
}
$('[data-testid=channel-settings-btn]').onclick=async()=>{$('#channel-settings-modal').hidden=false}
$('[data-testid=channel-settings-close]').onclick=()=>{$('#channel-settings-modal').hidden=true}
$('[data-testid=channel-topic-submit]').onclick=async()=>{
 try{let x=await api('/channels/'+current.id,{method:'PATCH',body:JSON.stringify({topic:$('[data-testid=channel-topic-input]').value})});current=x.channel;$('[data-testid=channel-topic]').textContent=current.topic}catch(x){}
}
$('[data-testid=archive-channel-btn]').onclick=async()=>{try{let x=await api('/channels/'+current.id+'/archive',{method:'POST'});current=x.channel}catch(x){}}
$('[data-testid=unarchive-channel-btn]').onclick=async()=>{try{let x=await api('/channels/'+current.id+'/unarchive',{method:'POST'});current=x.channel}catch(x){}}
$('[data-testid=workspace-settings-btn]').onclick=()=>{$('#workspace-settings-modal').hidden=false}
$('[data-testid=workspace-settings-close]').onclick=()=>{$('#workspace-settings-modal').hidden=true}
$('[data-testid=settings-tab-members]').onclick=async()=>{let x=await api('/workspaces/'+workspace.slug+'/members'),p=$('[data-testid=settings-pane-members]');p.hidden=false;p.innerHTML='';x.members.forEach(m=>{let r=document.createElement('div');r.dataset.testid='member-row';r.innerHTML='<span data-testid="role-badge"></span><select data-testid="role-select"><option>owner</option><option>admin</option><option>member</option><option>guest</option></select>';r.querySelector('[data-testid=role-badge]').textContent=m.role;p.append(r)})}
$('[data-testid=settings-tab-invitations]').onclick=async()=>{$('[data-testid=settings-pane-invitations]').hidden=false}
$('[data-testid=create-invitation-btn]').onclick=()=>{$('[data-testid=create-invitation-form]').hidden=false}
$('[data-testid=create-invitation-form]').onsubmit=async e=>{e.preventDefault();try{let x=await api('/workspaces/'+workspace.slug+'/invitations',{method:'POST',body:'{}'});let el=document.querySelector('[data-testid=invitation-code]');el.textContent=x.invitation.code;el.hidden=false}catch(x){let a=e.target.querySelector('[role=alert]')||document.createElement('div');a.setAttribute('role','alert');a.textContent=x.error||'Unable to create invitation';if(!a.parentNode)e.target.append(a)}}
const oldLoad=load;load=async()=>{await oldLoad();if(token())await refreshWorkspace()}
