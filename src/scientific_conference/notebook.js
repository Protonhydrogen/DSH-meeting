'use strict';
const notebooks=new Map();let notebookId='', notebookClock=0, notebookTimeEdited=false;
const notebookUrls=[];
function notebookStatus(message){
  const labels={'Saved locally · personal_notes.md':'已自动保存','Select a meeting to add notes.':'选择会议后记录','Loading personal notes…':'正在加载笔记…','Unsaved changes…':'待保存…','Saving…':'正在保存…','Saving next changes…':'正在保存新修改…','Recovered local draft — Save to sync.':'已恢复本机草稿，请保存同步'};
  document.getElementById('notebook-status').textContent=labels[message]||message;
}
function draftKey(id){return 'conference-notebook:'+id;}
async function showNotebook(d){
  notebookClock=d.live?.clocks?.capture_ms||0;
  if(!notebookTimeEdited&&document.activeElement!==$('notebook-time'))$('notebook-time').value=clock(notebookClock);
  if(notebookId===d.session_id)return;
  notebookId=d.session_id;notebookTimeEdited=false;
  $('personal-text').disabled=true;$('personal-text').value='';$('slide-files').disabled=true;
  $('notebook-save').disabled=true;$('notebook-stamp').disabled=true;
  $('slide-gallery').replaceChildren();notebookUrls.splice(0).forEach(URL.revokeObjectURL);
  const id=notebookId;notebookStatus('Loading personal notes…');
  try{
    let state=notebooks.get(id);
    if(!state){
      const book=await api('/api/notebook?session_id='+encodeURIComponent(id));
      state={id,text:book.text,revision:book.revision,dirty:false,saving:false,conflict:false,images:book.images};
      let saved;try{saved=JSON.parse(localStorage.getItem(draftKey(id))||'null');}catch{}
      if(saved&&saved.text!==book.text){state.text=saved.text;state.dirty=true;state.conflict=saved.revision!==book.revision;}
      notebooks.set(id,state);
    }
    if(id!==notebookId)return;
    $('personal-text').value=state.text;renderNotebookTimeline(state);window.renderNotebookPreview?.(state);$('personal-text').disabled=false;$('slide-files').disabled=false;
    $('notebook-save').disabled=false;$('notebook-stamp').disabled=false;
    notebookStatus(state.conflict?'Another tab changed the notes. Draft preserved; copy it before reloading.':state.dirty?'Recovered local draft — Save to sync.':'Saved locally · personal_notes.md');
    await renderSlideGallery(state);
  }catch(e){if(id===notebookId){notebookStatus(e.message+' — refresh to retry.');notebookId='';}}
}
function rememberDraft(state){try{localStorage.setItem(draftKey(state.id),JSON.stringify({text:state.text,revision:state.revision}));}catch{notebookStatus('Browser draft unavailable; click Save now.');}}
function editNotebook(){
  const state=notebooks.get(notebookId);if(!state)return;
  state.text=$('personal-text').value;window.renderNotebookPreview?.(state);state.dirty=true;rememberDraft(state);renderNotebookTimeline(state);
  notebookStatus('Unsaved changes…');clearTimeout(state.timer);
  state.timer=setTimeout(()=>saveNotebook(state),650);
}
async function saveNotebook(state){
  if(!state||state.saving||!state.dirty)return;
  if(state.conflict){if(state.id===notebookId)notebookStatus('Conflict: copy your draft before reloading; server notes were not overwritten.');return;}
  state.saving=true;const text=state.text;
  if(state.id===notebookId)notebookStatus('Saving…');
  try{
    const book=await api('/api/notebook',{session_id:state.id,text,revision:state.revision});
    state.revision=book.revision;state.dirty=state.text!==text;
    if(state.dirty)rememberDraft(state);else localStorage.removeItem(draftKey(state.id));
    if(state.id===notebookId)notebookStatus(state.dirty?'Saving next changes…':'Saved locally · personal_notes.md');
  }catch(e){state.conflict=/another tab/i.test(e.message);rememberDraft(state);if(state.id===notebookId)notebookStatus('Save failed: '+e.message);}
  finally{state.saving=false;}
  if(state.dirty&&!state.conflict&&state.text!==text)saveNotebook(state);
}
function notebookTime(){const m=$('notebook-time').value.trim().match(/^(\d{1,5}):([0-5]\d)$/);if(!m)throw Error('Timestamp must be mm:ss');return (Number(m[1])*60+Number(m[2]))*1000;}
async function renderSlideGallery(state){
  if(state.id!==notebookId)return;
  const gallery=$('slide-gallery');gallery.replaceChildren();state.imageUrls={};notebookUrls.splice(0).forEach(URL.revokeObjectURL);
  for(const slide of state.images){
    const card=document.createElement('figure'),img=document.createElement('img'),caption=document.createElement('figcaption');
    img.alt=slide.name;img.loading='lazy';caption.textContent=clock(slide.time_ms)+' · '+(/^[a-f0-9]{24,}\./i.test(slide.name)?'PPT 图片':slide.name);
    const button=document.createElement('button');button.className='slide-preview';button.title='Open slide';button.append(img);card.append(button,caption);
    const actions=document.createElement('div');actions.className='slide-actions';const insert=document.createElement('button');insert.textContent='插入笔记';insert.onclick=()=>{const editor=$('personal-text');const label=slide.name.replace(/[\[\]\r\n]/g,' ');editor.setRangeText('\n\n!['+clock(slide.time_ms)+' · '+label+'](attachments/'+slide.id+')\n\n',editor.selectionStart,editor.selectionEnd,'end');editNotebook();editor.focus();};
    const at=document.createElement('button');at.textContent='回听 '+clock(slide.time_ms);at.onclick=()=>seekMeeting(slide.time_ms);actions.append(insert,at);card.append(actions);gallery.append(card);
    try{
      const response=await fetch('/api/notebook/image?session_id='+encodeURIComponent(state.id)+'&id='+encodeURIComponent(slide.id),{headers:{'X-Conference-Token':token}});
      if(!response.ok)throw Error('Image unavailable');const blob=await response.blob();
      if(state.id!==notebookId)return;const url=URL.createObjectURL(blob);notebookUrls.push(url);img.src=url;state.imageUrls['attachments/'+slide.id]=url;window.renderNotebookPreview?.(state);
      button.onclick=()=>{$('slide-full').src=url;$('slide-dialog').showModal();};
    }catch{caption.textContent+=' · image unavailable';}
  }
}
async function uploadSlides(files){
  const state=notebooks.get(notebookId);if(!state){notebookStatus('Select a meeting first.');return;}
  let at;try{at=notebookTime();}catch(e){notebookStatus(e.message);return;}
  for(const file of files){
    try{
      if(file.size>12*1024*1024)throw Error('Image exceeds 12 MB');
      if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw Error('Use PNG, JPEG or WebP images');
      notebookStatus('Saving slide: '+file.name);
      const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(Error('Cannot read image'));reader.readAsDataURL(file);});
      const book=await api('/api/notebook/image',{session_id:state.id,name:file.name,time_ms:at,content:encoded});
      state.images=book.images;
      if(state.id===notebookId){await renderSlideGallery(state);notebookStatus('Slide saved locally · '+(state.dirty?'text changes pending':'notes saved'));}
    }catch(e){if(state.id===notebookId)notebookStatus(file.name+': '+e.message);}
  }
}
$('personal-text').addEventListener('input',editNotebook);
$('notebook-save').onclick=()=>saveNotebook(notebooks.get(notebookId));
$('notebook-time').addEventListener('input',()=>notebookTimeEdited=true);
$('notebook-stamp').onclick=()=>{try{const stamp='['+clock(notebookTime())+'] ';const editor=$('personal-text');editor.setRangeText(stamp,editor.selectionStart,editor.selectionEnd,'end');editNotebook();editor.focus();}catch(e){notebookStatus(e.message);}};
$('slide-files').onchange=e=>{uploadSlides([...e.target.files]);e.target.value='';};
const dropzone=$('personal-notebook');
for(const kind of ['dragenter','dragover'])dropzone.addEventListener(kind,e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();dropzone.classList.add('dragging');}});
dropzone.addEventListener('dragleave',e=>{if(!dropzone.contains(e.relatedTarget))dropzone.classList.remove('dragging');});
dropzone.addEventListener('drop',e=>{e.preventDefault();dropzone.classList.remove('dragging');uploadSlides([...e.dataTransfer.files]);});
dropzone.addEventListener('paste',e=>{const files=[...e.clipboardData.files];if(files.length){e.preventDefault();uploadSlides(files);}});
$('slide-close').onclick=()=>$('slide-dialog').close();
window.addEventListener('beforeunload',e=>{if([...notebooks.values()].some(s=>s.dirty||s.saving)){e.preventDefault();e.returnValue='';}});

function clearNotebook(){
  notebookId='';notebookTimeEdited=false;$('slide-dialog').close();$('note-time-links').replaceChildren();$('export-status').textContent='';
  $('personal-text').value='';$('personal-text').disabled=true;$('personal-preview')?.replaceChildren();if($('personal-preview'))delete $('personal-preview').dataset.rendered;
  $('notebook-save').disabled=true;$('notebook-stamp').disabled=true;$('slide-files').disabled=true;
  $('slide-gallery').replaceChildren();notebookUrls.splice(0).forEach(URL.revokeObjectURL);
  notebookStatus('Select a meeting to add notes.');
}

$('notebook-export').onclick=async()=>{
  const state=notebooks.get(notebookId);if(!state)return;
  if(state.saving){$('export-status').textContent='笔记正在保存，请稍后导出。';return;}
  await saveNotebook(state);if(state.dirty||state.conflict){$('export-status').textContent='请先解决保存问题，草稿已保留。';return;}
  try{const d=await api('/api/notebook/export',{session_id:state.id});if(state.id!==notebookId)return;
    const url=URL.createObjectURL(new Blob([d.markdown],{type:'text/markdown;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='meeting_notes.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);$('export-status').textContent='已保存 meeting_notes.md；图片在同目录 attachments 中。';
  }catch(e){$('export-status').textContent=e.message;}
};

function renderNotebookTimeline(state){
  const buttons=[...state.text.matchAll(/\[(\d{1,5}):([0-5]\d)\]/g)].map(m=>{
    const button=document.createElement('button');button.textContent='↗ '+m[1]+':'+m[2];button.title='回听笔记时间点';button.onclick=()=>seekMeeting((Number(m[1])*60+Number(m[2]))*1000);return button;
  });$('note-time-links').replaceChildren(...buttons);
}
