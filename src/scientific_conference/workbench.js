"use strict";
// Display-only components; API/storage identifiers remain the source of truth.
let playbackUrl='',playbackOffset=0,playbackSession='',playbackSequence=0,playbackPosition=0,playbackLoading=false,playbackAutoplay=false,meetingDuration=0,currentMeeting=null;
// Meeting time follows started_utc > scheduled_for > created_utc, never a session-id guess.
function meetingStamp(value,withTime){
  if(!value)return '';
  const text=String(value),match=/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(text),zoned=/(?:Z|[+-]\d{2}:?\d{2})$/.test(text);
  if(match&&!zoned)return match[1]+'-'+match[2]+'-'+match[3]+(withTime&&match[4]?' '+match[4]+':'+match[5]:'');
  const date=new Date(text);
  if(Number.isNaN(date.getTime()))return text.slice(0,16).replace('T',' ');
  const pad=number=>String(number).padStart(2,'0'),day=date.getFullYear()+'-'+pad(date.getMonth()+1)+'-'+pad(date.getDate());
  return withTime?day+' '+pad(date.getHours())+':'+pad(date.getMinutes()):day;
}
function meetingTimeOf(row){
  const value=row?.meeting_time;if(!value)return '';
  if(row.meeting_time_source==='recording_start')return '录音开始 · '+meetingStamp(value,true);
  if(row.meeting_time_source==='scheduled')return '计划 · '+meetingStamp(value,true);
  return '创建 · '+meetingStamp(value,false);
}
function renderMeetingList(){
  const query=$('meeting-search').value.trim().toLocaleLowerCase();
  const buttons=[...historySessions.values()].filter(s=>[s.title,s.presenter,s.report_title,s.original_title].join(' ').toLocaleLowerCase().includes(query)).map(s=>{
    const b=document.createElement('button');b.type='button';b.setAttribute('aria-current',String(s.session_id===sid));
    const title=document.createElement('span');const primary=s.title||(s.report_title&&s.report_title!=='none'?s.report_title:(s.presenter&&s.presenter!=='none'?s.presenter:s.session_id));title.textContent=primary;
    // Do not echo the report title when the meeting name already contains it.
    const report=(s.report_title&&s.report_title!=='none'&&!String(primary).includes(s.report_title))?s.report_title:'';
    const meta=document.createElement('small');meta.textContent=[report,s.presenter&&s.presenter!=='none'?s.presenter:'',meetingTimeOf(s),stateNames[s.state]||s.state].filter(Boolean).join(' · ');b.append(title,meta);
    b.onclick=()=>{selectSession(s.session_id);$('history').value=s.session_id;updateDeleteButton();renderMeetingList();refresh();};return b;
  });$('meeting-list').replaceChildren(...buttons);
}
function setMeetingTab(summary){
  $('transcript-pane').hidden=summary;$('summary-pane').hidden=!summary;
  $('transcript-tab').setAttribute('aria-selected',String(!summary));$('summary-tab').setAttribute('aria-selected',String(summary));
}
$('transcript-tab').onclick=()=>setMeetingTab(false);$('summary-tab').onclick=()=>setMeetingTab(true);
$('meeting-search').oninput=renderMeetingList;
$('new-meeting').onclick=()=>{if(typeof openNewMeetingDialog==='function')openNewMeetingDialog();else{selectSession('');$('meeting-heading').textContent='新的科研会议';$('title').focus();renderMeetingList();refresh();}};
function playbackClock(ms){return clock(ms)+(Number($('playback-step').value)===100?'.'+Math.floor(ms%1000/100):'');}
function setPlaybackPosition(ms){
  playbackPosition=ms;$('timeline').value=ms;$('playback-position').textContent=playbackClock(ms);
  $('timeline').setAttribute('aria-valuetext',playbackClock(ms));
}
function updatePlaybackButton(){
  const playing=playbackLoading?playbackAutoplay:!$('meeting-audio').paused,button=$('play-position');button.textContent=playing?'Ⅱ':'▶';
  button.setAttribute('aria-label',playing?'暂停':'播放');button.title=playing?'暂停':'播放';
}
function resetPlayback(){
  playbackSequence++;playbackLoading=false;playbackAutoplay=false;$('meeting-audio').pause();$('meeting-audio').removeAttribute('src');$('meeting-audio').load();
  if(playbackUrl)URL.revokeObjectURL(playbackUrl);playbackUrl='';playbackSession='';meetingDuration=0;currentMeeting=null;
  $('timeline').max=0;setPlaybackPosition(0);updatePlaybackButton();$('playback-duration').textContent='00:00';$('playback-status').textContent='';
}
function updateTimeline(d){
  // A live meeting follows the newest line by default; opening a finished meeting
  // starts at its beginning. State transitions (recording started) re-arm following.
  const live=LIVE_MEETING_STATES.includes(d.state),changed=currentMeeting?.session_id!==d.session_id,
        started=currentMeeting&&!LIVE_MEETING_STATES.includes(currentMeeting.state)&&live;
  if(changed){newSincePause=0;setFollowLatest(live);if(!live)$('transcript').scrollTop=0;}
  else if(started){newSincePause=0;setFollowLatest(true);}
  currentMeeting=d;meetingDuration=Math.max(d.live?.clocks?.capture_ms||0,...d.segments.map(s=>s.end_ms),0);$('timeline').max=meetingDuration;$('playback-duration').textContent=clock(meetingDuration);
  if(followLatest)pinTranscript();
}
async function seekMeeting(ms,play=true){
  if(!sid||!meetingDuration)return;const session=sid,sequence=++playbackSequence;
  ms=Math.max(0,Math.min(Number(ms),Math.max(0,meetingDuration-1)));$('meeting-audio').pause();playbackLoading=true;playbackAutoplay=play;playbackSession='';updatePlaybackButton();$('playback-status').textContent='加载音频…';
  $('notebook-time').value=clock(ms);notebookTimeEdited=true;setPlaybackPosition(ms);
  try{
    const response=await fetch('/api/audio?session_id='+encodeURIComponent(session)+'&start_ms='+Math.floor(ms)+'&end_ms='+Math.min(Math.floor(ms)+60000,meetingDuration),{headers:{'X-Conference-Token':token},signal:AbortSignal.timeout(15000)});
    if(!response.ok){const e=await response.json();throw Error(e.error||'音频不可用');}const blob=await response.blob();if(session!==sid||sequence!==playbackSequence)return;
    playbackLoading=false;if(playbackUrl)URL.revokeObjectURL(playbackUrl);playbackUrl=URL.createObjectURL(blob);playbackOffset=ms;playbackSession=session;$('meeting-audio').src=playbackUrl;
    $('playback-status').textContent='';updatePlaybackButton();if(playbackAutoplay)await $('meeting-audio').play();
  }catch(e){if(session===sid&&sequence===playbackSequence){playbackLoading=false;updatePlaybackButton();$('playback-status').textContent=e.message;}}
}
$('play-position').onclick=async()=>{
  const audio=$('meeting-audio');
  if(playbackLoading){playbackAutoplay=!playbackAutoplay;updatePlaybackButton();return;}
  if(!audio.paused){audio.pause();return;}
  if(playbackSession===sid&&audio.src&&!audio.ended){try{await audio.play();}catch(e){$('playback-status').textContent=e.message;}}
  else await seekMeeting(playbackPosition);
};
$('playback-step').onchange=()=>{$('timeline').step=$('playback-step').value;setPlaybackPosition(playbackPosition);};
$('timeline').oninput=()=>{playbackSequence++;playbackLoading=false;playbackAutoplay=false;$('meeting-audio').pause();$('playback-status').textContent='';playbackSession='';setPlaybackPosition(Number($('timeline').value));};
$('timeline').onchange=()=>seekMeeting(playbackPosition);
$('timeline').onkeydown=e=>{
  if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight')return;e.preventDefault();
  seekMeeting(playbackPosition+(e.key==='ArrowRight'?1:-1)*Number($('playback-step').value),playbackLoading?playbackAutoplay:!$('meeting-audio').paused);
};
$('meeting-audio').onplay=updatePlaybackButton;$('meeting-audio').onpause=updatePlaybackButton;
$('meeting-audio').ontimeupdate=()=>{if(playbackSession!==sid)return;setPlaybackPosition(playbackOffset+$('meeting-audio').currentTime*1000);};
$('meeting-audio').onended=()=>{updatePlaybackButton();const at=Math.round(playbackOffset+$('meeting-audio').duration*1000);if(playbackSession===sid&&at<meetingDuration-100)seekMeeting(at);};
// Following the newest transcript is an explicit state with its own affordance.
// It is never inferred from a bare scroll event: our own pinning, filtering,
// scroll anchoring, the pane resizer and inline editing all move the box without
// meaning "stop following". Only recent user input (wheel/touch/keys) may pause
// it, and returning to the bottom always resumes.
const LIVE_MEETING_STATES=['starting','recording','draining'];
let followLatest=true,followBeforeEdit=false,lastUserScrollAt=0,autoScrollAt=0,newSincePause=0,pinToken=0;
function atTranscriptBottom(){const box=$('transcript');return box.scrollHeight-box.clientHeight-box.scrollTop<=24;}
function syncFollowUi(){
  const toggle=$('autoscroll');if(toggle)toggle.checked=followLatest;
  const pill=$('follow-latest');
  if(pill){pill.hidden=followLatest;pill.textContent=newSincePause?'↓ 回到最新 · '+newSincePause+' 段新内容':'↓ 回到最新';}
  const pane=$('transcript-pane');if(pane)pane.dataset.follow=followLatest?'on':'off';
}
function setFollowLatest(value){
  followLatest=Boolean(value);if(followLatest)newSincePause=0;
  syncFollowUi();
  return followLatest;
}
// Pin now and once more after layout, without depending on a composited frame: a
// throttled or offscreen iframe must not leave the transcript above the newest line.
function pinTranscript(){
  if(!followLatest)return;const box=$('transcript');if(!box)return;
  const token=++pinToken,again=()=>{if(followLatest&&token===pinToken)box.scrollTop=box.scrollHeight;};
  box.scrollTop=box.scrollHeight;
  if(typeof requestAnimationFrame==='function')requestAnimationFrame(again);
  setTimeout(again,0);
}
function noteUserScrollIntent(){lastUserScrollAt=Date.now();}
// Inline editing pauses following so polling cannot move the text being edited;
// the previous state returns when the editor closes.
function pauseFollowForEdit(){followBeforeEdit=followLatest;if(followLatest)setFollowLatest(false);}
function resumeFollowAfterEdit(){if(followBeforeEdit){followBeforeEdit=false;setFollowLatest(true);pinTranscript();}}
for(const type of ['wheel','touchstart','touchmove','pointerdown','mousedown'])$('transcript').addEventListener(type,noteUserScrollIntent,{passive:true});
$('transcript').addEventListener('keydown',e=>{if(['ArrowUp','PageUp','Home','ArrowDown','PageDown','End',' '].includes(e.key))noteUserScrollIntent();});
$('transcript').addEventListener('scroll',()=>{
  const box=$('transcript');
  // Landing at the bottom (by any means) always means "follow again".
  if(atTranscriptBottom()){if(!followLatest)setFollowLatest(true);return;}
  // Away from the bottom without recent user input is our own layout work:
  // pinning, filtering, scroll anchoring or a resized pane must not pause.
  if(Date.now()-lastUserScrollAt>900)return;
  setFollowLatest(false);
},{passive:true});

// External links must reach the real browser. The DSH panel frames this page in a
// sandbox without ``allow-popups``, so Chromium drops every ``target=_blank`` link
// ("Blocked opening ... in a sandboxed frame"). Only in embedded mode the click is
// handed to the local service, which asks the operating system for the default
// browser; a normal browser tab keeps its native link behaviour.
let meetingToastTimer=0;
function meetingToast(text){
  let node=$('meeting-toast');
  if(!node){node=document.createElement('div');node.id='meeting-toast';node.className='meeting-toast';node.setAttribute('role','status');document.body.append(node);}
  node.textContent=text;node.classList.add('show');
  clearTimeout(meetingToastTimer);meetingToastTimer=setTimeout(()=>node.classList.remove('show'),4600);
}
async function openExternalLink(href){
  try{await api('/api/open-external',{url:href});return true;}
  catch(error){
    // Tell the reader first; the clipboard write is best effort and must never
    // hold the message back (a permission prompt can leave it pending for long).
    const stale=/not found/i.test(error.message||'');
    meetingToast(stale?'当前面板是旧进程，还不能打开浏览器；链接已复制，请重开 Meetings 面板后重试。':'无法打开浏览器；链接已复制，可粘贴到地址栏。');
    try{if(navigator.clipboard&&navigator.clipboard.writeText)navigator.clipboard.writeText(href).catch(()=>{});}catch{}
    return false;
  }
}
document.addEventListener('click',event=>{
  if(!document.body.classList.contains('is-embedded')||event.defaultPrevented)return;
  if(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;
  const target=event.target,link=target&&target.closest?target.closest('a[href]'):null;
  if(!link)return;
  const href=link.getAttribute('href')||'';
  if(!/^https?:\/\//i.test(href))return;
  event.preventDefault();event.stopPropagation();
  openExternalLink(href);
},true);
$('autoscroll').onchange=()=>{setFollowLatest($('autoscroll').checked);if(followLatest)pinTranscript();};
$('follow-latest').onclick=()=>{setFollowLatest(true);pinTranscript();};
syncFollowUi();
function renderTranscript(d){
  const raw=$('raw-view').checked,box=$('transcript');
  const signature=JSON.stringify([d.reading_blocks,d.segments,d.translations,d.translation?.enabled,raw]);if(signature===lastTranscript)return;lastTranscript=signature;
  const oldPosition=box.scrollTop;const anchor=[...box.children].find(el=>el.offsetTop-box.offsetTop+el.offsetHeight>oldPosition);const anchorKey=anchor?.dataset.key;const anchorOffset=anchor?anchor.getBoundingClientRect().top-box.getBoundingClientRect().top:0;
  const segmentMap=new Map(d.segments.map(s=>[s.segment_id,s]));
  const suspectIds=new Set((d.reading_blocks||[]).filter(b=>b.anomalous).flatMap(b=>b.sources.map(x=>x.segment_id)));
  const old=new Map([...box.children].map(el=>[el.dataset.key,el]));const data=raw?d.segments:(d.reading_blocks||d.segments);
  const activeEditor=$('review-editor'),editingArticle=activeEditor?.closest('.segment');
  if(activeEditor&&!activeEditor.hidden&&editingArticle&&!data.some(s=>(s.block_id||s.segment_id)===editingArticle.dataset.key)){
    // A raw-view switch or changed sentence anchor must never discard the editor.
    preserveReviewDrafts();hideReview();
  }
  const nodes=data.map(s=>{
    const anomalous=s.anomalous||suspectIds.has(s.segment_id);
    const key=s.block_id||s.segment_id,content=JSON.stringify([s,(s.sources||[s]).map(x=>segmentMap.get(x.segment_id)?.confirmed_translations),d.translation?.enabled,(s.sources||[s]).map(x=>d.translations?.[x.segment_id]),raw]);let el=old.get(key);
    if(el?.contains($('review-editor'))&&!$('review-editor').hidden)return el;
    if(el?.dataset.content===content)return el;
    el=el||document.createElement('article');el.dataset.key=key;el.dataset.content=content;el.className='segment'+(anomalous?' anomalous':'');
    const time=document.createElement('button');time.className='timestamp';time.textContent=clock(s.start_ms)+' – '+clock(s.end_ms);time.title=(s.sources||[s]).some(source=>segmentMap.get(source.segment_id)?.backend==='qwen3_asr')?'按录音区间回听':'回听此来源窗口；句内未估算精确词时间';time.onclick=()=>seekMeeting(s.start_ms);
    const meta=document.createElement('div');meta.className='segment-meta';meta.append(time);if(s.state==='Draft'){const draft=document.createElement('span');draft.className='draft';draft.textContent='正在接续…';meta.append(draft);}
    const p=document.createElement('p');p.textContent=raw?(s.raw_text||s.text):s.text;p.className='editable-transcript';p.tabIndex=0;p.setAttribute('role','button');p.setAttribute('aria-label','编辑转录段落');p.onclick=()=>openReview(s.sources||[s],'transcript',p);p.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();openReview(s.sources||[s],'transcript',p);}};const parts=[meta,p];
    if(anomalous){const mark=document.createElement('small');mark.className='anomaly-label';mark.textContent='疑似重复幻觉 · 不作为纪要证据';parts.push(mark);}
    const sources=s.sources||[s];el.dataset.sourceIds=sources.map(x=>x.segment_id).join(' ');
    for(const source of sources){
      const tr=d.translations?.[source.segment_id],confirmed=segmentMap.get(source.segment_id)?.confirmed_translations||{};
      const values=Object.entries(confirmed).map(([lang,text])=>({lang,text}));
      if(d.translation?.enabled&&tr?.text&&!Object.hasOwn(confirmed,tr.target_lang||d.translation_target))values.push({lang:tr.target_lang||d.translation_target,text:tr.text,stale:tr.stale_source});
      for(const value of values){const line=document.createElement('p');line.className='translation';line.textContent=value.text;if(value.stale){line.title='原文已修改，待重新翻译，可手动编辑';line.classList.add('stale-translation');}line.tabIndex=0;line.setAttribute('role','button');line.setAttribute('aria-label','编辑翻译段落');line.onclick=()=>{$('review-language').value=value.lang;openReview([source],'translation',line);};line.onkeydown=e=>{if(e.key==='Enter'){$('review-language').value=value.lang;openReview([source],'translation',line);}};parts.push(line);}
    }
    el.replaceChildren(...parts);return el;
  });
  if(!nodes.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='等待语音转录…';nodes.push(empty);}
  // Reconcile by key: unchanged nodes, open sources and selection survive polling.
  const wanted=new Set(nodes);let fresh=0;
  for(const child of [...box.children])if(!wanted.has(child))child.remove();
  nodes.forEach((node,i)=>{if(!old.has(node.dataset.key))fresh++;if(box.children[i]!==node)box.insertBefore(node,box.children[i]||null);});
  if(followLatest)pinTranscript();
  else {if(fresh)newSincePause+=fresh;const kept=nodes.find(n=>n.dataset.key===anchorKey);box.scrollTop=oldPosition;if(kept)box.scrollTop+=kept.getBoundingClientRect().top-box.getBoundingClientRect().top-anchorOffset;syncFollowUi();}
  if(typeof updateTranscriptFilters==='function')updateTranscriptFilters(d);
}

let metadataDirty=false,metadataSaving=false;
function clearMeetingMetadata(){metadataDirty=false;$('presenter').value='';$('title').value='';$('save-metadata').disabled=true;$('metadata-status').textContent='';}
function updateMeetingMetadata(d){
  // A planned meeting shows its explicit name; a recorded report keeps the
  // historical reporter/report heading.
  const heading=d.state==='planned'&&d.title?d.title:(d.report_title&&d.report_title!=='none'?d.report_title:(d.presenter&&d.presenter!=='none'?d.presenter:d.title||'未命名报告'));
  $('meeting-heading').textContent=heading;
  const time=meetingTimeOf(d);
  document.querySelector('.subtitle').textContent='报告人：'+(d.presenter||'none')+(time?' · '+time:'');
  if(!metadataDirty&&![ $('presenter'),$('title') ].includes(document.activeElement)){
    $('presenter').value=d.presenter||'none';$('title').value=d.report_title||'none';
  }
  $('save-metadata').disabled=metadataSaving||!sid;
  if(!metadataDirty)$('metadata-status').textContent=d.metadata_inferred?'按原始名称识别，可修改；缺失题目为 none':'报告信息已保存';
}
for(const id of ['presenter','title'])$(id).addEventListener('input',()=>{metadataDirty=true;$('metadata-status').textContent=sid?'修改后点击保存':'开始录音时保存';});
$('save-metadata').onclick=async()=>{
  if(!sid||metadataSaving)return;const session=sid,presenter=$('presenter').value,report_title=$('title').value;metadataSaving=true;$('save-metadata').disabled=true;
  try{await api('/api/meeting/metadata',{session_id:session,presenter,report_title});
    if(sid===session){if($('presenter').value===presenter&&$('title').value===report_title)metadataDirty=false;$('metadata-status').textContent='已保存';await info();await refresh();}
  }catch(e){if(sid===session)$('metadata-status').textContent=e.message;}
  finally{metadataSaving=false;$('save-metadata').disabled=!sid;}
};
