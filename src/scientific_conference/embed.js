/* Controlled DSH embedding; inactive in the independent Web app. */
(()=>{'use strict';const query=new URLSearchParams(location.search),nonce=query.get('nonce');
if(query.get('embed')!=='dsh'||parent===window||!nonce||!(/^[a-f0-9]{48}$/).test(nonce))return;
// DSH NEXT (Desktop) frames this panel from its registered application scheme
// (dsh-app://app) instead of an http loopback origin; that scheme is the only
// non-http ancestor accepted, and empty/unknown parents still disable embedding.
let target;try{const raw=query.get('parent')||'',u=new URL(raw);
 if(u.protocol==='http:'||u.protocol==='https:'){if(!['127.0.0.1','localhost','[::1]'].includes(u.hostname)||u.origin!==raw)return;target=u.origin;}
 else if(u.protocol==='dsh-app:'){target=u.origin&&u.origin!=='null'?u.origin:u.protocol+'//'+u.host;if(!target||target==='dsh-app://')return;else target=target.replace(/\/$/,'');}
 else return;}catch{return;}
const desktopShell=target.startsWith('dsh-app:'),fromShell=e=>e.origin===target||(desktopShell&&e.origin==='null');
document.body.classList.add('is-embedded');
function send(type,data={}){parent.postMessage({channel:'dsh-meetings',nonce,type,...data},target);}
window.addEventListener('conference:selection',e=>{send('selection-clear');});
window.addEventListener('conference:snapshot',e=>{const d=e.detail;if(!d?.session_id)return;send('selection',{session_id:d.session_id,title:[d.presenter,d.report_title].filter(v=>v&&v!=='none').join(' · ')||d.title,review_epoch:d.review_epoch,note_text:(d.notes||[]).filter(n=>n.mode==='session'&&n.agent==='dsh_native').sort((a,b)=>b.updated_utc.localeCompare(a.updated_utc))[0]?.content_md??null,note_head:(d.notes||[]).filter(n=>n.mode==='session'&&n.agent==='dsh_native').sort((a,b)=>b.updated_utc.localeCompare(a.updated_utc))[0]?.note_id||null});const button=document.getElementById('summary-overview');if(button){button.disabled=!d.segments.length;button.title='使用 DSH 原生 Agent 生成科研纪要';}});
window.addEventListener('conference:analysis-request',e=>{const d=e.detail;if(d?.session_id===sid&&['analysis-request','analysis-save'].includes(d.type))send(d.type,d);});
document.addEventListener('click',e=>{if(e.target.closest?.('#summary-overview')){e.preventDefault();e.stopImmediatePropagation();send('summary');}},true);
window.addEventListener('message',e=>{if(e.source!==parent||!fromShell(e)||e.data?.nonce!==nonce||e.data?.channel!=='dsh-meetings')return;if(e.data.type==='theme'&&e.data.tokens&&typeof e.data.tokens==='object'){const root=document.documentElement,keyPattern=/^--wb-[a-z0-9-]+$/,valuePattern=/^[\w#(),.%\s\/-]{1,160}$/;for(const [key,value] of Object.entries(e.data.tokens)){if(!keyPattern.test(key)||typeof value!=='string'||!valuePattern.test(value))continue;root.style.setProperty(key,value);}root.dataset.meetingTheme=e.data.dark?'dark':'light';return;}if(['analysis-result','analysis-view','analysis-saved','analysis-error','analysis-accepted'].includes(e.data.type)){window.dispatchEvent(new CustomEvent('conference:native-analysis',{detail:e.data}));return;}if(e.data.type==='refresh'){lastNotes='';refresh();}});
})();
