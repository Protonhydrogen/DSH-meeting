'use strict';
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
const token = fragment.get('token') || sessionStorage.getItem('conferenceToken') || '';
sessionStorage.setItem('conferenceToken', token);
let selectionChosen=false;
let captureReady=false;
let liveAsr=null;
const asrNames={faster_whisper:'Whisper · 分块',qwen3_asr:'Qwen3-ASR 0.6B · 流式'};
function renderAsr(d=null){
  liveAsr=d;
  const value=$('asr-backend').value;
  const active=d?.recorder_alive&&['starting','recording','draining'].includes(d.state);
  const current=d?.asr_backend;
  const switchState=d?.live?.asr_switch;
  const pending=['preparing','waiting_boundary'].includes(switchState?.state);
  const switchable=active&&d.state==='recording'&&Object.hasOwn(asrNames,current);
  $('asr-backend').disabled=busy||pending||(active&&!switchable);
  $('switch-asr').hidden=!switchable;
  $('switch-asr').disabled=busy||!switchable||pending||!value||value===current;
  $('asr-options').hidden=!!active;
  $('whisper-settings').hidden=value!=='faster_whisper';
  $('qwen-settings').hidden=value!=='qwen3_asr';
  for(const id of ['whisper-word-times','qwen-language','qwen-chunk'])$(id).disabled=busy||active;
  let status=active?'当前使用 '+(asrNames[current]||current||'正在加载'):'';
  if(pending)status+=switchState.state==='preparing'?' · 正在准备目标模型':' · 将在下一话语边界切换';
  if(active&&switchState?.state==='rejected')status+=' · 切换失败：'+switchState.error;
  if(active&&d.live?.transcriber?.last_error&&current==='qwen3_asr')status+=' · 推理异常，录音继续：'+d.live.transcriber.last_error;
  if(!active&&value==='qwen3_asr')status='讲话中显示草稿，定稿后可按录音区间回听。';
  if($('asr-status').textContent!==status)$('asr-status').textContent=status;
  const showCaption=active&&current==='qwen3_asr';
  const draft=showCaption?d.live?.asr_partial||'':'';
  const caption=$('asr-draft'),captionText=$('asr-draft-text');
  caption.hidden=!showCaption;
  caption.classList.toggle('is-empty',!draft);
  if(captionText.textContent!==draft){
    captionText.textContent=draft;
    captionText.scrollTop=captionText.scrollHeight;
  }
}
let sid = fragment.get('session') || '', busy = false, lastTranscript = '', lastNotes = '', refreshing = false, translationOn = false, historySessions = new Map(), deleteTarget = null;
const stateNames = {planned:'筹备中（未录音）',starting:'正在加载模型',recording:'● 正在录音',draining:'正在处理剩余音频',closed:'录音已结束',failed:'录音失败',interrupted:'进程已退出'};
const agentNames = {dsh_native:'DSH 原生 Agent',external:'DSH / 外部 Agent（需保持会话运行）',offline_extractive:'离线规则抽取',cli:'Agent 科研分析',openai_compatible:'模型科研分析'};
function clock(ms) { const s=Math.max(0,Math.floor((ms||0)/1000)); return (Math.floor(s/60)).toString().padStart(2,'0')+':'+(s%60).toString().padStart(2,'0'); }
function error(text) { $('error').textContent=text; $('error').classList.toggle('show',!!text); }
async function api(route, data) {
  const response = await fetch(route,{method:data?'POST':'GET',headers:{'X-Conference-Token':token,'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined,signal:AbortSignal.timeout(15000)});
  const result=await response.json(); if(!response.ok||result.ok===false) throw Error(result.error||'请求失败'); return result;
}
function selectSession(value,automatic=false) { if(!automatic)selectionChosen=true; if(sid!==value&&typeof clearMeetingTimeline==='function')clearMeetingTimeline();window.dispatchEvent(new CustomEvent('conference:selection',{detail:{session_id:value}})); if(sid!==value&&typeof resetPlayback==='function'){resetPlayback();clearMeetingMetadata();if(typeof clearReview==='function')clearReview();} if(typeof clearNotebook==='function'&&sid!==value)clearNotebook();sid=value; lastTranscript=''; lastNotes=''; const p=new URLSearchParams({token}); if(sid)p.set('session',sid); history.replaceState(null,'','#'+p); if(typeof renderMeetingList==='function')renderMeetingList(); }
async function info() {
  const d=await api('/api/info');
  $('capture-source').options[1].disabled=!d.system_audio_available;
  if(!captureReady){
    $('meeting-language').value=d.language?.startsWith('zh')?'zh':'en';
    const defaults=d.asr_defaults||{};
    $('asr-backend').options[0].textContent='Whisper '+(defaults.whisper_model==='turbo'?'Turbo':defaults.whisper_model||'Turbo')+' · 分块';
    if(defaults.backend&&!Object.hasOwn(asrNames,defaults.backend)){
      $('asr-backend').add(new Option('沿用已配置后端 · '+defaults.backend,''));
      $('asr-backend').value='';
    }else $('asr-backend').value=defaults.backend||'faster_whisper';
    $('whisper-word-times').checked=defaults.word_timestamps!==false;
    $('qwen-language').value=defaults.qwen_language||'inherit';
    $('qwen-chunk').value=defaults.qwen_chunk_seconds||2;
    renderAsr();
  }
  captureReady=true;
  if(document.activeElement!==$('summary-interval'))$('summary-interval').value=d.summary_interval_seconds||30;
  $('privacy').textContent=`麦克风：${d.device} · 音频${d.cloud_audio?'允许云端转写':'本地处理'} · 总结${d.cloud_notes?'允许云端模型':'按本地配置处理'} · 保存：${d.output_root}`;
  $('agent').textContent=agentNames[d.agent]||d.agent;
  $('translate').hidden=d.translation_backend==='none';
  if(d.summary_credential_ready===false)$('privacy').textContent+=' · Summary setup needed: configure a summary credential first.';
  if(!sid && !selectionChosen && d.active) selectSession(d.active,true);
  const list=$('history'); list.replaceChildren(new Option('选择历史会议',''));
  historySessions=new Map(d.sessions.map(s=>[s.session_id,s]));
  for(const s of d.sessions){const time=typeof meetingTimeOf==='function'?meetingTimeOf(s):'';list.add(new Option(`${s.title||s.session_id}${time?' · '+time:''} · ${stateNames[s.state]||s.state}`,s.session_id));}
  list.value=sid;
  updateDeleteButton();
  if(typeof renderMeetingList==='function')renderMeetingList();
}
function updateDeleteButton(){
  const selected=historySessions.get($('history').value);
  $('delete-history').disabled=busy||!selected||['starting','recording','draining'].includes(selected.state);
  $('delete-history').title=selected&&['starting','recording','draining'].includes(selected.state)?'请先停止录音并等待处理完成':'删除后移入本地回收站';
}
function renderSegments(d) { renderTranscript(d); }
function renderNotes(d) {
  const signature=JSON.stringify(d.notes); if(signature===lastNotes)return; lastNotes=signature;
  const box=$('notes'), position=box.scrollTop;
  const segments=new Map(d.segments.map(s=>[s.segment_id,s]));
  const nodes=[...d.notes].reverse().sort((a,b)=>({session:0,talk:1,turn:2}[a.mode]??3)-({session:0,talk:1,turn:2}[b.mode]??3)).map(n=>{
    const el=document.createElement('article');el.className='note';
    let note={};try{note=JSON.parse(n.content_json||'{}');}catch{}
    const title=document.createElement('h3');
    title.textContent=`${{turn:note.provisional?'实时纪要（持续更新）':'分段最终纪要',talk:'报告总结',session:note.provisional?'阶段性总体总结':'最终总体总结'}[n.mode]||n.mode} · ${n.target_id||''} · 第 ${n.version_count||1} 版`;
    const meta=document.createElement('small');meta.textContent=agentNames[n.agent]||n.agent||'';
    if(n.updated_utc)meta.textContent+=' · 更新于 '+new Date(n.updated_utc).toLocaleTimeString();
    if(note.summary_through_ms)meta.textContent+=' · 覆盖至 '+clock(note.summary_through_ms);
    const summary=document.createElement('p');summary.className='note-summary';summary.textContent=(n.stale_review?'【转录已修订，请重新生成此纪要】\n':'')+(note.summary||'');
    if(window.ScientificMarkdown){summary.classList.add('meeting-markdown');summary.innerHTML=window.ScientificMarkdown.render(summary.textContent);}
    const details=document.createElement('details');const toggle=document.createElement('summary');
    toggle.textContent='完整纪要';const body=document.createElement('pre');body.textContent=n.content_md||'Notes pending';
    details.append(toggle,body);el.append(title,meta,summary);

      el.append(details);
    const findings=(note.findings||[]).filter(f=>(f.source_segment_ids||[]).length);
    if(findings.length){
      const sources=document.createElement('details');sources.className='note-sources';
      const label=document.createElement('summary');label.textContent=`Sources · ${findings.length} findings`;
      sources.append(label);
      for(const finding of findings){
        const row=document.createElement('div');row.className='source-row';
        const claim=document.createElement('p');claim.textContent=finding.claim||'Supporting evidence';row.append(claim);
        for(const id of [...new Set(finding.source_segment_ids)]){
          const source=segments.get(id), link=document.createElement('button');link.className='source-link';
          link.textContent=source?clock(source.start_ms)+'–'+clock(source.end_ms):'Source unavailable';
          link.disabled=!source;link.title=source?.text||id;
          link.onclick=()=>{
            setMeetingTab(false);if(typeof setFollowLatest==='function')setFollowLatest(false);else $('autoscroll').checked=false;if(typeof applyTranscriptFilters==='function'){$('transcript-search').value='';applyTranscriptFilters();}
            if($('raw-view').checked){$('raw-view').checked=false;lastTranscript='';renderSegments(d);if(typeof renderMeetingTimeline==='function')renderMeetingTimeline(d);}
            const target=document.querySelector('[data-source-ids~="'+CSS.escape(id)+'"]');
            if(target){target.scrollIntoView({behavior:'smooth',block:'center'});target.classList.add('active');setTimeout(()=>target.classList.remove('active'),2000);}
          };
          row.append(link);
        }
        sources.append(row);
      }
      el.append(sources);
    }
    return el;
  });
  if(!nodes.length){const el=document.createElement('div');el.className='empty';el.textContent='等待发言结束，或长发言的下一次临时总结。';nodes.push(el);}
  box.replaceChildren(...nodes);box.scrollTop=$('autoscroll').checked?0:position;
}

async function refresh() {
  if(refreshing||busy)return;refreshing=true;
  try {
    if(!sid){
      $('connection').textContent='已连接本地服务 · 就绪';$('state').textContent='待开始';
      $('clock').textContent='00:00 / —';$('counts').textContent='0 / 0';$('output').textContent='';
      $('start').disabled=busy;for(const id of ['stop','turn','talk'])$(id).disabled=true;
      $('capture-source').disabled=busy;$('meeting-language').disabled=busy;renderAsr();
      $('transcript').textContent='开始录音后，稳定转写将在这里持续显示。';
      $('notes').textContent='每轮结束后生成纪要，长发言定期生成临时笔记。';
      $('queue').textContent='等待发言';error('');return;
    }
    const d=await api('/api/snapshot?session_id='+encodeURIComponent(sid));
    if(d.session_id!==sid)return;

    const recordedActive=['starting','recording','draining'].includes(d.state);
    const active=recordedActive&&d.recorder_alive;
    let state=recordedActive&&!d.recorder_alive?'进程已退出':stateNames[d.state]||d.state;
    if(d.state==='starting'&&d.recorder_alive)state=({opening_microphone:'正在打开麦克风',opening_system_audio:'正在打开系统声音',loading_models:'音源可用 · 加载模型',starting_capture:'正在启动采集'}[d.live.startup_phase]||'正在准备录音');
    if(active)state+=' · '+(d.capture_source==='system'?'系统声音':'麦克风')+' · '+(d.language?.startsWith('zh')?'中文':'英文');
    if($('state').textContent!==state)$('state').textContent=state;
    $('capture-source').disabled=busy||active;$('meeting-language').disabled=busy||active;renderAsr(d);
    const clocks=d.live.clocks||{}; $('clock').textContent=clock(clocks.capture_ms||Math.max(0,...d.segments.map(s=>s.end_ms)))+' / '+Math.max(0,Math.round(((clocks.capture_ms||0)-(clocks.asr_ms||0))/1000))+' 秒';
    $('counts').textContent=`${d.segments.length} / ${d.notes.length}`;
    $('agent').textContent=agentNames[d.agent]||d.agent;
    const tr=d.translation||{};
    const trLabel={none:'未配置后端',whisper_translate:'Whisper 离线译英',openai_compatible:'模型翻译',cli:'Agent 翻译'}[tr.backend]||tr.backend||'—';
    $('translation').textContent=tr.enabled?`开 · ${trLabel} → ${tr.target_language||''}`:`关 · ${trLabel}`;
    translationOn=!!tr.enabled;
    $('translate').textContent=(tr.enabled?'🌐 关闭翻译':'🌐 实时翻译');
    $('translate').disabled=busy||!sid||(!recordedActive&&d.state!=='closed');
    $('start').disabled=busy||active;
    for(const id of ['stop','turn','talk'])$(id).disabled=busy||!active||d.state==='draining';
    const pending=(d.tasks.pending||0)+(d.tasks.failed||0)+(d.tasks.leased||0);
    $('queue').textContent=(d.tasks.dead||0)?`${d.tasks.dead} 个任务失败，请检查 runner.log`:pending?`${pending} 项待处理`:(active?'Waiting for new scientific content (about '+Math.round(d.summary_interval_ms/1000)+' seconds)':d.notes.length?'已完成 · '+d.notes.length+' 份纪要':'暂无纪要 · 点击补做纠错与总结');
    if(d.agent==='external')$('queue').textContent='External Agent required — no independent model runner';
    else if(!d.runner_alive&&active)$('queue').textContent='Summary worker is not running';
    else if(d.tasks.leased){const working=d.working_tasks||{};const labels=[];if(Object.keys(working).some(k=>k.startsWith('note.')))labels.push('正在生成科研总结');if(working['transcript.review'])labels.push('正在检查转写纠错');if(working['assistant.query'])labels.push('正在处理会议助手指令');$('queue').textContent=labels.join(' · ')||'后台正在处理';}
    else if(d.tasks.dead)$('queue').textContent='Summary stopped after repeated errors — click 补做纠错与总结';
      else if(d.tasks.failed){const when=d.summary_retry?.available_at_utc;const seconds=when?Math.max(0,Math.ceil((Date.parse(when)-Date.now())/1000)):0;$('queue').textContent=d.runner_alive?'Network/API retry in '+seconds+'s':'Summary worker stopped — click 补做纠错与总结';}
      $('summary-interval-status').textContent=(active?'当前录音：'+Math.round(d.summary_interval_ms/1000)+' 秒 · ':'')+'保存后用于后续会议；模型响应耗时另计';
      $('summary-overview').disabled=busy||d.agent!=='openai_compatible'||!d.segments.length;
      $('summary-overview').title='会议中生成截至当前的阶段性总览；结束后生成最终总览。';
      $('summary-retry').disabled=busy||d.agent!=='openai_compatible';
      $('summary-retry').title='从已保存转写补做纠错与总结，并重试失败任务；不会重新录音。';
    window.dispatchEvent(new CustomEvent('conference:snapshot',{detail:d}));
    // Keep the side list's meeting clock truthful while a recording starts:
    // "planned" must turn into "recording_start" without waiting for a manual refresh.
    const row=historySessions.get(d.session_id);
    if(row){
      const changed=['state','meeting_time','meeting_time_source','title','started_utc','scheduled_for'].some(key=>row[key]!==d[key]);
      if(changed){
        Object.assign(row,{state:d.state,meeting_time:d.meeting_time,meeting_time_source:d.meeting_time_source,title:d.title,started_utc:d.started_utc,scheduled_for:d.scheduled_for});
        if(typeof renderMeetingList==='function')renderMeetingList();
        const option=[...$('history').options].find(item=>item.value===d.session_id);
        if(option)option.textContent=(d.title||d.session_id)+' · '+(stateNames[d.state]||d.state);
      }
    }
    const warnings=[];if(d.error)warnings.push(d.error);if(recordedActive&&!d.recorder_alive)warnings.push('此会议的录音进程已退出，下面显示的是已保存内容。');if(active&&!d.runner_alive&&d.agent!=='external')warnings.push('纪要后台未运行，请检查 runner.log。');if(d.tasks.dead)warnings.push('部分纪要生成失败，请检查 runner.log。');
    error(warnings.join('\n'));
    $('output').textContent='会议：'+sid+' · 文件：'+d.output_dir;
    updateMeetingMetadata(d);
    updateTimeline(d);if(typeof updateReviewState==='function')updateReviewState(d);renderSegments(d);if(typeof renderMeetingTimeline==='function')renderMeetingTimeline(d);renderNotes(d);if(typeof showMeetingAssistant==='function')showMeetingAssistant(d);if(typeof showNotebook==='function')showNotebook(d);
    $('connection').textContent='已连接 · 已接收 '+(d.live.audio?.source_blocks||0)+' 个音频块 · 最近更新 '+new Date().toLocaleTimeString();
  } catch(e){error(e.message);$('connection').textContent='连接中断，将自动重试';}
  finally{refreshing=false;}
}
async function action(route,payload) {
  if(busy)return;busy=true;for(const id of ['start','stop','turn','talk'])$(id).disabled=true;
  let failure;
  try {const d=await api(route,payload);if(d.session_id&&d.session_id!==sid)selectSession(d.session_id);await info();}
  catch(e){failure=e.message;}finally{busy=false;$('start').disabled=false;await refresh();if(failure)error(failure);}
}
$('capture-source').onchange=()=>{$('capture-hint').hidden=$('capture-source').value!=='system';};
$('asr-backend').onchange=()=>renderAsr(liveAsr);
$('switch-asr').onclick=()=>action('/api/asr/switch',{session_id:sid,backend:$('asr-backend').value});
$('start').onclick=()=>{
  // A planned meeting is started in place: reuse its session_id and keep the stored meeting name.
  const planned=Boolean(sid)&&historySessions.get(sid)?.state==='planned';
  const reportTitle=$('title').value.trim();
  if(!$('qwen-settings').hidden&&!$('qwen-chunk').reportValidity())return;
  const payload={consent:true,source:$('capture-source').value,
    asr_backend:$('asr-backend').value||undefined,word_timestamps:$('whisper-word-times').checked,
    qwen_language:$('qwen-language').value,qwen_chunk_seconds:$('qwen-chunk').validity.valid?$('qwen-chunk').valueAsNumber:undefined,
    language:$('meeting-language').value,dsh_auto_summary:document.body.classList.contains('is-embedded')};
  if(planned){
    payload.session_id=sid;
    // Carry edited report fields into the started meeting; the meeting name itself is never replaced.
    if(reportTitle&&reportTitle!=='none'){payload.presenter=$('presenter').value||'none';payload.report_title=reportTitle;}
  }else{
    payload.title=$('title').value;payload.report_title=$('title').value||'none';payload.presenter=$('presenter').value||'none';
  }
  action('/api/start',payload);
};
$('raw-view').onchange=()=>{lastTranscript='';refresh();};
$('stop').onclick=()=>action('/api/stop',{session_id:sid});
$('turn').onclick=()=>action('/api/boundary',{session_id:sid,kind:'turn'});
$('talk').onclick=()=>action('/api/boundary',{session_id:sid,kind:'talk'});
$('translate').onclick=()=>action('/api/translation',{session_id:sid,enabled:!translationOn});
$('history').onchange=()=>{selectSession($('history').value);updateDeleteButton();refresh();};
$('delete-history').onclick=()=>{
  const selected=historySessions.get($('history').value);if(!selected)return;
  deleteTarget={session_id:selected.session_id,title:selected.title||selected.session_id};
  $('delete-dialog-message').textContent=`将“${deleteTarget.title}”从历史会议中删除？`;
  $('delete-error').textContent='';$('delete-error').classList.remove('show');
  $('delete-dialog').showModal();
};
$('delete-cancel').onclick=()=>{$('delete-dialog').close();$('delete-error').textContent='';$('delete-error').classList.remove('show');deleteTarget=null;};
$('delete-confirm').onclick=async()=>{
  if(!deleteTarget||busy)return;const target=deleteTarget;busy=true;$('delete-confirm').disabled=true;
  try{
    while(refreshing)await new Promise(resolve=>setTimeout(resolve,25));
    await api('/api/history/delete',{session_id:target.session_id,confirm_session_id:target.session_id});
    $('delete-dialog').close();deleteTarget=null;if(sid===target.session_id)selectSession('');
    await info();
  }catch(e){const message=e.message||'删除失败';error(message);$('delete-error').textContent=message;$('delete-error').classList.add('show');}
  finally{busy=false;$('delete-confirm').disabled=false;updateDeleteButton();await refresh();}
};
let newMeetingBusy=false;
function showNewMeetingError(message){const box=$('new-meeting-error');box.textContent=message||'';box.classList.toggle('show',Boolean(message));}
function openNewMeetingDialog(){
  showNewMeetingError('');
  if(!$('new-meeting-dialog').open)$('new-meeting-dialog').showModal();
  $('new-meeting-name').focus();
}
$('new-meeting-cancel').onclick=()=>{$('new-meeting-dialog').close();showNewMeetingError('');};
$('new-meeting-create').onclick=async()=>{
  if(newMeetingBusy)return;
  const title=$('new-meeting-name').value.trim();
  if(!title){showNewMeetingError('请填写会议名称。');$('new-meeting-name').focus();return;}
  newMeetingBusy=true;$('new-meeting-create').disabled=true;
  try{
    // Planned meetings only create records and directories; no microphone or recorder process.
    const result=await api('/api/meetings/create',{title,agenda:$('new-meeting-agenda').value.trim(),presenter:$('new-meeting-presenter').value.trim()||'none',report_title:$('new-meeting-report-title').value.trim()||'none',scheduled_for:$('new-meeting-scheduled').value,location:$('new-meeting-location').value.trim()});
    $('new-meeting-dialog').close();showNewMeetingError('');
    for(const id of ['new-meeting-name','new-meeting-presenter','new-meeting-report-title','new-meeting-scheduled','new-meeting-location','new-meeting-agenda'])$(id).value='';
    try{
      await info();
      selectSession(result.session_id);
      $('history').value=result.session_id;updateDeleteButton();
      if(typeof renderMeetingList==='function')renderMeetingList();
      await refresh();
    }catch(e){error(e.message||'会议已创建，但列表刷新失败。');}
  }catch(e){showNewMeetingError(e.message||'创建会议失败。');}
  finally{newMeetingBusy=false;$('new-meeting-create').disabled=false;}
};
window.addEventListener('hashchange',()=>{selectSession(new URLSearchParams(location.hash.slice(1)).get('session')||'');info().then(refresh).catch(e=>error(e.message));});
info().then(refresh).catch(e=>error(e.message));setInterval(refresh,1000);


$('summary-retry').onclick=()=>{if(sid)action('/api/summary/retry',{session_id:sid});};
$('save-summary-interval').onclick=()=>action('/api/summary/interval',{session_id:sid,seconds:Number($('summary-interval').value)});

$('summary-overview').onclick=()=>{if(sid)action('/api/summary/overview',{session_id:sid});};
