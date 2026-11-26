window.__ModuleLoader__.load({id:'dsh-meetings',factory:(require)=>{
 const React=require('react'),h=React.createElement,{useState,useEffect,useRef}=React;
 const inject=['slots','layout','sessions','uiConversation','configForms','locale'];
 const LIVE_RUN=new Set(['starting','running','cancelling','saving','verifying']);
 const {resolveSlotLabel}=require('@deepseek-ai/dsh-client-ui-slots');
 const css=`.dsh-meetings{height:100%;width:100%;min-width:0;min-height:0;display:flex;flex:1;flex-direction:column;background:var(--dsw-alias-bg-base,#fff);color:var(--dsw-alias-label-primary,#243047);padding-top:var(--dsh-frame-top-clearance,0)}.dsh-meetings>header{display:flex;gap:12px;align-items:center;padding:10px 18px;border-bottom:1px solid var(--dsw-alias-border-l2,#edf0f4);flex-shrink:0}.dsh-meetings>header strong{margin-right:auto}.dsh-meetings>header button,.meeting-connection-error button{border:1px solid var(--dsw-alias-border-l3,#e4e8f0);border-radius:7px;background:var(--dsw-alias-bg-layer-1,#fff);padding:6px 11px;color:var(--dsw-alias-label-secondary,#35425b);cursor:pointer}.dsh-meetings-body{display:flex;flex:1;min-height:0;min-width:0;overflow-x:auto;overflow-y:hidden}.dsh-meetings-body>iframe{border:0;flex:1 0 400px;min-width:360px;height:100%;background:var(--dsw-alias-bg-base,#fff)}.meeting-chat-resizer{width:6px;flex:0 0 6px;cursor:col-resize;background:var(--dsw-alias-bg-layer-2,#f7f9fd);touch-action:none}.dsh-meetings-native-chat[hidden],.meeting-chat-resizer[hidden]{display:none}.meeting-chat-resizer:hover,.meeting-chat-resizer:focus{background:var(--dsw-alias-interactive-bg-hover-accent,#c8c4ed);outline:0}.dsh-meetings-native-chat{width:var(--meeting-chat-width,480px);flex:0 0 var(--meeting-chat-width,480px);height:100%;min-height:0;min-width:0;display:flex;flex-direction:column;overflow:hidden;border-left:1px solid var(--dsw-alias-border-l2,#edf0f4);background:var(--dsw-alias-bg-base,#fff);color:var(--dsw-alias-label-primary,#243047)}.meeting-connection-error{padding:10px 18px;color:var(--dsw-alias-state-error-primary,#a53434);white-space:pre-wrap}.meeting-native-tabs{display:flex;gap:16px;padding:9px 16px 0;flex-shrink:0;border-bottom:1px solid var(--dsw-alias-border-l2,#edf0f4)}.meeting-native-tabs>button{background:transparent;border:0;border-bottom:2px solid transparent;padding:7px 0;color:var(--dsw-alias-label-tertiary,#838b9a);font:inherit;cursor:pointer}.meeting-native-tabs>button[aria-selected=true]{color:var(--dsw-alias-brand-primary,#4476e8);border-bottom-color:var(--dsw-alias-brand-primary,#4476e8)}.meeting-chat-empty,.meeting-empty{padding:24px;color:var(--dsw-alias-label-tertiary,#7a8494);font:14px/1.7 system-ui,sans-serif}.meeting-chat-notice{display:flex;gap:8px;align-items:center;flex-shrink:0;padding:8px 14px;border-bottom:1px solid var(--dsw-alias-border-l2,#edf0f4);background:var(--dsw-alias-state-warn-tertiary,#fff6e8);color:var(--dsw-alias-label-secondary,#35425b);font:12px/1.5 system-ui,sans-serif}.meeting-chat-notice>span{margin-right:auto}.meeting-chat-notice button{border:1px solid var(--dsw-alias-border-l3,#e4e8f0);border-radius:6px;background:var(--dsw-alias-bg-layer-1,#fff);padding:4px 9px;color:var(--dsw-alias-label-secondary,#35425b);cursor:pointer;font:inherit}`;
 async function api(path,data){const response=await fetch('meetings-api'+path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:data===undefined?undefined:JSON.stringify(data)});const value=await response.json();if(!response.ok||value.ok===false)throw Error(value.error||'Meetings request failed');return value;}
 function Workspace({ctx,renderSlot,SessionProvider}){
  const [backend,setBackend]=useState(null),[error,setError]=useState(''),[meeting,setMeeting]=useState(null),[report,setReport]=useState(null),[chatBinding,setChatBinding]=useState(null),[width,setWidth]=useState(480),[chatOpen,setChatOpen]=useState(true),[chatDismissed,setChatDismissed]=useState(null);
  const frame=useRef(null),selected=useRef(null),backendRef=useRef(null),reportRef=useRef(null),notified=useRef('');
  // Keep the last report belonging to the rendered meeting: selecting another
  // meeting clears ``report`` before effect cleanup, which must still know
  // whether the previous meeting owns a running analysis.
  if(report?.session_id===meeting?.session_id)reportRef.current=report;
  const themeProbe=useRef(null),pushTheme=useRef(()=>{});
  useEffect(()=>{try{const saved=Number(localStorage.getItem('dsh-meetings.chat-width'));if(saved>=360&&saved<=1100)setWidth(saved);setChatOpen(localStorage.getItem('dsh-meetings.chat-open')!=='false');}catch{}},[]);
  useEffect(()=>{const probe=document.createElement('span');probe.setAttribute('aria-hidden','true');probe.style.cssText='position:absolute;left:-9999px;top:0;width:0;height:0;overflow:hidden;pointer-events:none';document.body.append(probe);themeProbe.current=probe;return()=>{themeProbe.current=null;probe.remove();};},[]);
  useEffect(()=>{let last='';
   const push=force=>{const b=backendRef.current,f=frame.current;if(!b||!f||!f.contentWindow)return;const payload=readThemeTokens();if(!payload)return;const key=JSON.stringify(payload);if(!force&&key===last)return;last=key;f.contentWindow.postMessage({channel:'dsh-meetings',type:'theme',nonce:b.nonce,...payload},b.origin);};
   pushTheme.current=()=>{last='';push(true);};
   const observer=new MutationObserver(()=>push(false));
   observer.observe(document.documentElement,{attributes:true,attributeFilter:['class','style','data-ds-dark-theme','data-platform']});
   observer.observe(document.body,{attributes:true,attributeFilter:['class','style','data-ds-dark-theme','data-platform']});
   const timer=setInterval(()=>push(false),2000);const onFocus=()=>push(false),onVisibility=()=>{if(!document.hidden)push(false);};
   window.addEventListener('focus',onFocus);document.addEventListener('visibilitychange',onVisibility);push(true);
   return()=>{observer.disconnect();clearInterval(timer);window.removeEventListener('focus',onFocus);document.removeEventListener('visibilitychange',onVisibility);pushTheme.current=()=>{};};},[]);
  function post(type,fields={}){const b=backendRef.current;if(b&&frame.current)frame.current.contentWindow.postMessage({channel:'dsh-meetings',type,nonce:b.nonce,session_id:selected.current?.session_id,...fields},b.origin);}
  function readThemeTokens(){const probe=themeProbe.current;if(!probe)return null;
   const read=name=>{probe.style.color="var("+name+", rgba(0,0,0,0))";const value=getComputedStyle(probe).color;return value&&value!=="rgba(0, 0, 0, 0)"?value:null;};
   const pick=(name,fallback)=>read(name)||fallback;
   const dark=document.body.hasAttribute('data-ds-dark-theme')||document.body.classList.contains('dark')||document.documentElement.classList.contains('dark');
   const bg=pick('--dsw-alias-bg-base','#fff'),text=pick('--dsw-alias-label-primary','#243047'),accent=pick('--dsw-alias-brand-primary','#5b5bd6');
   return {dark,tokens:{
    '--wb-bg':bg,
    '--wb-bg-raised':pick('--dsw-alias-bg-layer-1',bg),
    '--wb-bg-sunken':pick('--dsw-alias-bg-layer-2',bg),
    '--wb-bg-hover':pick('--dsw-alias-interactive-bg-hover',dark?'rgba(255,255,255,.08)':'rgba(38,49,72,.06)'),
    '--wb-bg-active':pick('--dsw-alias-bg-layer-3',bg),
    '--wb-text':text,
    '--wb-text-secondary':pick('--dsw-alias-label-secondary',text),
    '--wb-text-muted':pick('--dsw-alias-label-tertiary',text),
    '--wb-text-faint':pick('--dsw-alias-label-caption',text),
    '--wb-border':pick('--dsw-alias-border-l2',dark?'rgba(255,255,255,.12)':'#eeeff2'),
    '--wb-border-strong':pick('--dsw-alias-border-l3',dark?'rgba(255,255,255,.18)':'#e2e3e9'),
    '--wb-accent':accent,
    '--wb-accent-strong':"color-mix(in srgb, "+accent+" 85%, "+text+")",
    '--wb-accent-weak':"color-mix(in srgb, "+accent+" 12%, "+bg+")",
    '--wb-accent-text':pick('--dsw-alias-link',accent),
    '--wb-on-accent':pick('--dsw-alias-label-primary-inverted','#fff'),
    '--wb-focus-ring':"color-mix(in srgb, "+accent+" 50%, "+bg+")",
    '--wb-link':pick('--dsw-alias-link',accent),
    '--wb-danger':pick('--dsw-alias-state-error-primary','#b44545'),
    '--wb-success':pick('--dsw-alias-state-success-primary','#2f8f6b'),
    '--wb-warn':pick('--dsw-alias-state-warn-primary','#b06b18'),
    '--wb-warn-weak':pick('--dsw-alias-state-warn-secondary','#e7bd75'),
    '--wb-notice-bg':pick('--dsw-alias-state-warn-tertiary',dark?'#3a2a12':'#fff6e8'),
    '--wb-shadow':dark?'rgba(0,0,0,.45)':'rgba(24,29,44,.12)',
    '--wb-shadow-strong':dark?'rgba(0,0,0,.6)':'rgba(24,29,44,.22)'
   }};}

  useEffect(()=>{if(meeting?.session_id&&report?.session_id===meeting.session_id)post('analysis-result',{report});},[report,meeting?.session_id,backend]);
  function toggleChat(){const next=!chatOpen;setChatOpen(next);try{localStorage.setItem('dsh-meetings.chat-open',String(next));}catch{}}
  function resize(value){const next=Math.max(360,Math.min(1100,value));setWidth(next);try{localStorage.setItem('dsh-meetings.chat-width',String(next));}catch{}}
  function move(e){const handle=e.currentTarget;if(!handle.setPointerCapture)return;handle.setPointerCapture(e.pointerId);const x=e.clientX,start=width;handle.onpointermove=event=>resize(start+x-event.clientX);const stop=()=>{handle.onpointermove=null;handle.onpointerup=null;handle.onpointercancel=null;};handle.onpointerup=stop;handle.onpointercancel=stop;}
  async function boot(){try{setError('');const b=await api('/bootstrap',{}),u=new URL(b.origin);u.searchParams.set('embed','dsh');u.searchParams.set('parent',location.origin);u.searchParams.set('nonce',b.nonce);const hash=new URLSearchParams({token:b.token});try{const id=localStorage.getItem('dsh-meetings.selected');if(id&&/^[A-Za-z0-9_-]{1,100}$/.test(id))hash.set('session',id);}catch{}u.hash=hash.toString();b.frame_url=u.toString();backendRef.current=b;setBackend(b);}catch(e){setError(e.message);}}
  useEffect(()=>{boot();return()=>{backendRef.current=null;};},[]);
  useEffect(()=>{const timer=setInterval(async()=>{try{const info=await api('/health');if(info.backend_origin&&backendRef.current&&info.backend_origin!==backendRef.current.origin)setError('会议后台已恢复。保存当前草稿后点击重新连接。');}catch(e){setError(e.message);}},10000);return()=>clearInterval(timer);},[]);
  // Closing/switching the Meetings panel must not dispose a native session that
  // still owns a running analysis: a released session makes the host reconcile
  // the run as interrupted and a completed answer is lost.
  function holdRetainedUntilIdle(reference,id){
   let attempts=0;
   const check=async()=>{
    attempts++;
    if(attempts>600){try{reference.release();}catch{}return;}
    try{
     const status=await api('/agent/status?session_id='+encodeURIComponent(id));
     if(!LIVE_RUN.has(status?.run?.state)){reference.release();return;}
    }catch{try{reference.release();}catch{}return;}
    const timer=setTimeout(check,1500);timer?.unref?.();
   };
   const timer=setTimeout(check,1200);timer?.unref?.();
  }
  useEffect(()=>{
   if(!meeting?.session_id)return;let live=true,reference;const id=meeting.session_id;setChatBinding(null);
   (async()=>{try{const linked=await api('/agent/bind',{session_id:id});if(!live||selected.current?.session_id!==id)return;
    if(typeof ctx.sessions?.retain!=='function'||!SessionProvider||!renderSlot)throw Error('当前 DSH 未提供原生对话插槽或会话绑定接口');
    reference=ctx.sessions.retain(linked.agent_session_id,{source:'sidebarView'});await reference.ready;if(live&&selected.current?.session_id===id){setChatBinding({meeting_id:id,reference});setReport(linked);}
   }catch(e){if(live&&selected.current?.session_id===id)setError(e.message);}})();
   return()=>{
    live=false;
    if(!reference)return;
    if(LIVE_RUN.has(reportRef.current?.run?.state))holdRetainedUntilIdle(reference,id);
    else reference.release();
   };
  },[meeting?.session_id]);
  async function start(kind,payload){const id=selected.current?.session_id;if(!id)return;setError('');if(payload?.request_id)post('analysis-accepted',{request_id:payload.request_id,kind});try{const response=await api('/agent/start',{session_id:id,kind,question:payload?.question||'',template:payload?.template});if(selected.current?.session_id===id)setReport(response);}catch(e){if(selected.current?.session_id===id){setError(e.message);post('analysis-error',{request_id:payload?.request_id,kind,error:e.message});}}}
  async function saveEmbedded(data){const id=selected.current?.session_id,summary=reportRef.current?.analyses?.summary||(reportRef.current?.run?.kind==='summary'?reportRef.current.run:null);if(!id||data.session_id!==id)return;if(data.run_id!==summary?.id){post('analysis-saved',{request_id:data.request_id,run_id:data.run_id,ok:false,error:'已生成新的纪要；当前草稿保留，请对照最新版本后保存。'});return;}try{const saved=await api('/report/save',{session_id:id,markdown:data.markdown,review_epoch:summary.review_epoch,evidence:summary.evidence,expected_note_id:data.expected_note_id});if(selected.current?.session_id===id){post('analysis-saved',{request_id:data.request_id,run_id:summary.id,ok:true,note_id:saved.note_id});post('refresh');}}catch(e){if(selected.current?.session_id===id)post('analysis-saved',{request_id:data.request_id,run_id:summary.id,ok:false,error:e.message});}}
  // A summary typed into the embedded DSH conversation is saved by the host;
  // when it could not decide that itself, the draft stays here for one click.
  async function saveChatDraft(){const id=selected.current?.session_id;if(!id)return;setError('');try{const status=await api('/agent/save-chat',{session_id:id,turn:report?.chat?.turn});if(selected.current?.session_id===id){setReport(status);post('refresh');}}catch(e){if(selected.current?.session_id===id)setError(e.message);}}
  useEffect(()=>{const listener=e=>{const b=backendRef.current;if(!b||e.source!==frame.current?.contentWindow||e.origin!==b.origin||e.data?.nonce!==b.nonce||e.data?.channel!=='dsh-meetings')return;const d=e.data;
   if(d.type==='selection-clear'){selected.current=null;setMeeting(null);setReport(null);setChatBinding(null);try{localStorage.removeItem('dsh-meetings.selected');}catch{}}
   if(d.type==='selection'&&/^[A-Za-z0-9_-]{1,100}$/.test(d.session_id)){const next={session_id:d.session_id,title:String(d.title||d.session_id),review_epoch:d.review_epoch,note_head:d.note_head??null,note_text:typeof d.note_text==='string'?d.note_text:null};if(selected.current?.session_id!==next.session_id){setReport(null);setChatBinding(null);}selected.current=next;setMeeting(next);try{localStorage.setItem('dsh-meetings.selected',next.session_id);}catch{}}
   if(d.type==='summary')return start('summary',{question:d.question||''});
   if(d.type==='analysis-request'&&['summary','research'].includes(d.kind)&&typeof d.question==='string'&&d.question.length<=12000&&d.session_id===selected.current?.session_id)return start(d.kind,{question:d.question,request_id:d.request_id,template:d.template});
   if(d.type==='analysis-save')return saveEmbedded(d);
  };window.addEventListener('message',listener);return()=>window.removeEventListener('message',listener);},[]);
  useEffect(()=>{if(!meeting?.session_id)return;let live=true;const id=meeting.session_id;async function poll(){try{const status=await api('/agent/status?session_id='+encodeURIComponent(id));if(live&&selected.current?.session_id===id){setReport(status);if(status.run?.state==='completed'&&status.run.saved&&notified.current!==status.run.id){notified.current=status.run.id;post('refresh');}}}catch(e){if(live&&selected.current?.session_id===id)setError(e.message);}}poll();const timer=setInterval(poll,1800);return()=>{live=false;clearInterval(timer);};},[meeting?.session_id]);
  const bound=chatBinding?.meeting_id===meeting?.session_id?chatBinding:null;
  const chatDraft=report?.chat&&report.chat.state==='pending'&&chatDismissed!==(report.chat.turn??report.chat.chars)?report.chat:null;
  return h('section',{className:'dsh-meetings','aria-label':'Meetings 工作区',style:{'--meeting-chat-width':width+'px'}},
   h('header',null,h('strong',null,'Meetings'),h('button',{'aria-expanded':chatOpen,'aria-controls':'dsh-meetings-native-chat',onClick:toggleChat},chatOpen?'收起 DSH 对话':'展开 DSH 对话'),h('button',{onClick:()=>ctx.layout.selectPanel(null)},'返回对话')),
   error&&h('div',{className:'meeting-connection-error',role:'alert'},error,' ',h('button',{onClick:boot},'重新连接')),
   h('div',{className:'dsh-meetings-body'},backend?h('iframe',{ref:frame,src:backend.frame_url,title:'科研会议工作区',onLoad:()=>pushTheme.current(),sandbox:'allow-scripts allow-same-origin allow-forms allow-downloads',referrerPolicy:'no-referrer'}):h('div',{className:'meeting-empty'},'正在连接会议服务…'),
    h('div',{className:'meeting-chat-resizer',hidden:!chatOpen,role:'separator',tabIndex:0,'aria-label':'调节 DSH 对话宽度','aria-orientation':'vertical','aria-valuenow':width,'aria-valuemin':360,'aria-valuemax':1100,onPointerDown:move,onKeyDown:e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();resize(width+(e.key==='ArrowLeft'?20:-20));}}}),
    h('aside',{className:'dsh-meetings-native-chat',id:'dsh-meetings-native-chat',hidden:!chatOpen,'aria-label':'DSH 原生对话'},
     chatDraft&&h('div',{className:'meeting-chat-notice',role:'status'},h('span',null,'对话已生成内容（'+chatDraft.chars+' 字）'+(chatDraft.error?' · 上次保存失败':'')),h('button',{onClick:saveChatDraft},'存为纪要'),h('button',{onClick:()=>setChatDismissed(chatDraft.turn??chatDraft.chars)},'忽略')),
     bound?h(SessionProvider,{session:bound.reference},renderSlot('meetings.conversation',{})):h('div',{className:'meeting-chat-empty',role:'status'},meeting?'正在打开本场会议的 DSH 对话…':'选择会议后，在此使用完整 DSH 对话。'))));
 }
 function NativeConversation({ctx,sessionId,renderFactorySlot,useStore,actions}){
  const view=useStore(s=>s.view),[revision,update]=useState(0);
  useEffect(()=>{const refresh=()=>update(n=>n+1),off=ctx.slots.subscribe('conversation.view',refresh),locale=ctx.locale.subscribe(refresh),settings=ctx.configForms.developerTools.enabled.subscribe(refresh);return()=>{off();locale();settings();};},[]);
  const tabs=ctx.slots.entries('conversation.view').filter(e=>e.options.id&&(e.options.id!=='trajectory'||ctx.configForms.developerTools.enabled.getSnapshot())).map(e=>({id:e.options.id,label:resolveSlotLabel(e.options.label)||e.options.id}));
  const current=tabs.some(t=>t.id===view)?view:'chat';
  useEffect(()=>{ctx.uiConversation.binding(sessionId).activate(current);},[sessionId,current,revision]);
  return h(React.Fragment,null,h('nav',{className:'meeting-native-tabs',role:'tablist','aria-label':'DSH 对话视图'},tabs.map(tab=>h('button',{key:tab.id,role:'tab','aria-selected':tab.id===current,onClick:()=>{ctx.uiConversation.binding(sessionId).activate(tab.id);actions.setView(tab.id);}},tab.label))),renderFactorySlot('conversation.content',{variant:'embedded',phase:'active',hero:false}));
 }
 function apply(ctx){ctx.effect(()=>{const style=document.createElement('style');style.textContent=css;document.head.append(style);return()=>style.remove();},'dsh-meetings: workspace styles');ctx.slots.inject('main',()=>ctx.slots.register({name:'main',key:'meetings',children:{'meetings.conversation':{kind:'single',scope:'session'}},inject:()=>({ctx})},Workspace));ctx.slots.inject('meetings.conversation',()=>{
  let mounted,held;const sync=()=>{const store=ctx.slots.entries('conversation.session')[0]?.store;if(store===held)return;mounted?.();held=store;mounted=store?ctx.slots.register({name:'meetings.conversation',store,inject:()=>({ctx})},NativeConversation):undefined;};
  const off=ctx.slots.subscribe('conversation.session',sync);sync();return()=>{off();mounted?.();};
 });ctx.slots.inject('sidebar.panellist',()=>ctx.slots.register({name:'sidebar.panellist',id:'meetings',order:5,label:'Meetings'},()=>h('svg',{width:18,height:18,viewBox:'0 0 24 24',fill:'none',stroke:'currentColor',strokeWidth:1.5},h('rect',{x:4,y:3,width:16,height:18,rx:3}),h('path',{d:'M8 8h8M8 12h8M8 16h5'}))));}
 return {inject,apply};
}});
