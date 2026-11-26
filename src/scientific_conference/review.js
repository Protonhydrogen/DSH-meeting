"use strict";
let reviewSession='',reviewIds=[],reviewData=null,reviewDirty=false,reviewBusy=false;
function lockReview(busy){reviewBusy=busy;for(const el of $('review-editor').querySelectorAll('button,input,select,textarea')){if(busy){el.dataset.previousDisabled=el.disabled?'1':'0';el.disabled=true;}else{if(el.dataset.previousDisabled!==undefined)el.disabled=el.dataset.previousDisabled==='1';delete el.dataset.previousDisabled;}}}
function discardReviewDrafts(){for(const id of reviewIds){const prefix='conference-review:'+reviewSession+':'+id+':';for(let i=localStorage.length-1;i>=0;i--){const key=localStorage.key(i);if(key.startsWith(prefix))localStorage.removeItem(key);}}}
function currentReviewField(){return $('review-field').value==='translation'?'translation:'+$('review-language').value.trim().toLowerCase():$('review-field').value;}
function reviewDraftKey(id,field){return 'conference-review:'+reviewSession+':'+id+':'+field;}
function preserveReviewDrafts(){if(!reviewSession)return;for(const node of $('review-fields').querySelectorAll('[data-edit-segment]'))try{localStorage.setItem(reviewDraftKey(node.dataset.editSegment,node.dataset.field),JSON.stringify({value:node.value,revision:Number(node.dataset.revision)}));}catch{}}
function hideReview(){
  const editor=$('review-editor'),article=editor.closest('.segment');
  if(article){article.classList.remove('review-active');for(const el of article.querySelectorAll('[data-review-hidden]')){el.hidden=false;delete el.dataset.reviewHidden;}}
  editor.hidden=true;$('review-parking').append(editor);reviewDirty=false;reviewSession='';reviewIds=[];reviewData=null;lastTranscript='';
  if(typeof resumeFollowAfterEdit==='function')resumeFollowAfterEdit();
}
function clearReview(){preserveReviewDrafts();hideReview();}
async function openReview(sources,field='transcript',anchor){
  if(!sid||reviewBusy||!anchor)return;
  preserveReviewDrafts();hideReview();reviewSession=sid;reviewIds=[...new Set(sources.map(x=>x.segment_id))];
  $('review-field').value=field;$('review-title').textContent=field==='translation'?'编辑译文':'编辑转录';
  $('review-fields').textContent='正在加载…';$('review-status').textContent='';$('review-reload').hidden=true;
  const article=anchor.closest('.segment');article.classList.add('review-active');article.append($('review-editor'));$('review-editor').hidden=false;
  if(anchor.matches('p')){anchor.hidden=true;anchor.dataset.reviewHidden='1';}
  if(typeof pauseFollowForEdit==='function')pauseFollowForEdit();else $('autoscroll').checked=false;
  await reloadReview(false);
  $('review-fields').querySelector('textarea,select')?.focus();
}
async function reloadReview(keep){
  if(!reviewSession||reviewBusy)return;lockReview(true);if(keep)preserveReviewDrafts();const session=reviewSession;
  try{const d=await api('/api/review/read',{session_id:session,segment_ids:reviewIds});if(session!==reviewSession||sid!==session)return;reviewData=d;renderReviewFields(keep);}
  catch(e){$('review-status').textContent=e.message;}finally{lockReview(false);}
}
function renderReviewFields(rebase=false){
  if(!reviewData)return;const field=currentReviewField(),translation=field.startsWith('translation:');
  $('review-lang-label').hidden=!translation;$('review-help').hidden=reviewData.segments.length<2;
  const nodes=reviewData.segments.map(s=>{
    const section=document.createElement('section');section.className='review-item';const label=document.createElement('label');label.textContent=reviewData.segments.length>1?clock(s.start_ms)+'–'+clock(s.end_ms):'';
    const h=s.heads[field]||{revision:0,value:null};
    const input=document.createElement('textarea');input.value=translation?(s.translations.find(t=>t.target_lang===field.split(':')[1])?.text||''):s.text;input.maxLength=30000;input.rows=Math.max(2,Math.min(9,Math.ceil(input.value.length/65)));
    input.setAttribute('aria-label',(translation?'人工译文':'人工转录')+' '+clock(s.start_ms));input.dataset.editSegment=s.segment_id;input.dataset.field=field;input.dataset.revision=h.revision;
    let draft;try{draft=JSON.parse(localStorage.getItem(reviewDraftKey(s.segment_id,field))||'null');}catch{}
    if(draft){input.value=draft.value;input.dataset.revision=rebase?h.revision:draft.revision;reviewDirty=true;$('review-status').textContent=rebase?'草稿保留，最新版本已载入；请对照后保存。':'已恢复本机草稿；若有冲突请载入最新版本对照。';}
    input.oninput=()=>{reviewDirty=true;preserveReviewDrafts();};label.append(input);section.append(label);
    return section;
  });$('review-fields').replaceChildren(...nodes);for(const el of $('review-fields').querySelectorAll('button,input,select,textarea')){if(reviewBusy){el.dataset.previousDisabled=el.disabled?'1':'0';el.disabled=true;}}$('review-save').disabled=reviewBusy;
}
$('review-field').onchange=()=>{preserveReviewDrafts();renderReviewFields();};
$('review-language').onchange=()=>{preserveReviewDrafts();renderReviewFields();};
$('review-reload').onclick=()=>reloadReview(true);
$('review-save').onclick=async()=>{
  if(reviewBusy||!reviewData)return;const session=reviewSession;
  const edits=[...$('review-fields').querySelectorAll('[data-edit-segment]')].map(el=>({segment_id:el.dataset.editSegment,field:el.dataset.field,value:el.value,revision:Number(el.dataset.revision)}));
  lockReview(true);
  try{await api('/api/review/save',{session_id:session,edits});for(const e of edits)localStorage.removeItem('conference-review:'+session+':'+e.segment_id+':'+e.field);if(session!==sid||session!==reviewSession)return;hideReview();lastNotes='';await refresh();}
  catch(e){preserveReviewDrafts();$('review-status').textContent=e.message;$('review-reload').hidden=false;}finally{lockReview(false);$('review-save').disabled=false;}
};
$('review-cancel').onclick=()=>{if(reviewBusy)return;discardReviewDrafts();hideReview();refresh();};
$('review-editor').addEventListener('keydown',e=>{if(e.key==='Escape'&&!reviewBusy){preserveReviewDrafts();hideReview();refresh();}});
window.addEventListener('beforeunload',e=>{if(reviewDirty){preserveReviewDrafts();e.preventDefault();e.returnValue='';}});
