'use strict';
// Shared reading surfaces. Business data stays in the existing store; native
// requests cross only the authenticated nonce/origin bridge in embed.js.
(()=>{
 const native=()=>document.body.classList.contains('is-embedded');
 let snapshot=null,report=null,view='timeline',editing=null,saving=null,accepted=null,shown='',sourcesKey='';
 const defaultTemplate=window.ScientificMeetingTemplate?.markdown_structure||'';
 const englishTemplate=['## Summary','## Speaker and meeting details','## Research question','## Methods and evidence','## Findings and limitations','## Questions and discussion','## Actions and owners','## Verified references','## Items to verify'].join('\n\n');
 let pending=null,requestTimer=null,requestError=null;
 function statusNode(kind){return $(({summary:'native-summary-status',research:'literature-status'})[kind]);}
 function clearPending(){if(requestTimer)clearTimeout(requestTimer);requestTimer=null;pending=null;}
 const renderer=window.ScientificMarkdown;
 const style=document.createElement('style');style.textContent=renderer?.css||'';document.head.append(style);
 function stored(key,fallback=''){try{return localStorage.getItem(key)??fallback;}catch{return fallback;}}
 function store(key,value){try{localStorage.setItem(key,value);}catch{}}
 function noteHead(){return snapshot?.notes?.filter(n=>n.mode==='session'&&n.agent==='dsh_native').sort((a,b)=>b.updated_utc.localeCompare(a.updated_utc))[0]?.note_id||null;}
 const images=()=>typeof notebooks!=='undefined'&&snapshot?notebooks.get(snapshot.session_id)?.imageUrls||{}:{};
 function paint(id,text){const target=$(id);if(!target)return;const key=String(text||'')+'\0'+Object.values(images()).join('|');if(target.dataset.rendered===key)return;target.dataset.rendered=key;if(renderer)target.innerHTML=renderer.render(text||'',{images:images()});else target.textContent=text||'';}
 function summary(){return report?.run?.kind==='summary'&&['starting','running','saving','cancelling','verifying'].includes(report.run.state)?report.run:report?.analyses?.summary||(report?.run?.kind==='summary'?report.run:null);}
 function summaryDocument(){
  const run=summary();
  const latest=snapshot?.notes?.filter(n=>n.agent==='dsh_native'&&n.mode==='session').sort((a,b)=>b.updated_utc.localeCompare(a.updated_utc))[0];
  if(!run)return {text:latest?.content_md||'',note_id:latest?.note_id||null};
  if(accepted&&accepted.run_id===run.id&&accepted.session_id===snapshot?.session_id){
    if(!latest||latest.note_id===accepted.expected_note_id||latest.note_id===accepted.note_id)return {text:accepted.text,note_id:accepted.note_id};
    accepted=null; // Another window published a newer body/head pair.
  }
  if(latest&&run.state==='completed'&&(latest.note_id===run.note_id||!run.started_at||latest.updated_utc>=run.started_at))return {text:latest.content_md||'',note_id:latest.note_id};
  return {text:run.text||'',note_id:run.note_id??null};
 }
 function summaryText(){return summaryDocument().text;}
 function rememberEditing(){if(editing)store('conference-summary-draft:'+editing.session_id,JSON.stringify({...editing,text:$('native-report-editor').value}));}
 function clearAnalysis(){rememberEditing();clearPending();requestError=null;snapshot=null;report=null;editing=null;saving=null;accepted=null;shown='';sourcesKey='';for(const id of ['native-report','literature-report','meeting-references']){$(id).replaceChildren();delete $(id).dataset.rendered;}$('native-rebase').hidden=true;$('native-save').disabled=false;$('native-report-editor').hidden=true;$('native-report').hidden=false;$('native-save').hidden=true;$('native-cancel').hidden=true;$('native-edit').hidden=false;updateButtons();}
 function setView(next){view=next;document.body.dataset.meetingView=view;$('personal-notebook').hidden=view!=='timeline';$('reading-resizer').hidden=view!=='timeline';for(const [name,id] of [['timeline','transcript-pane'],['summary',native()?'native-summary-pane':'summary-pane'],['research','literature-pane']])$(id).hidden=name!==view;if(native())$('summary-pane').hidden=true;else $('native-summary-pane').hidden=true;for(const [name,id] of [['timeline','transcript-tab'],['summary','summary-tab'],['research','literature-tab']])$(id).setAttribute('aria-selected',String(view===name));$('autoscroll').parentElement.hidden=view!=='timeline';render();if(view==='timeline'&&typeof pinTranscript==='function')pinTranscript();}
 setMeetingTab=summary=>setView(summary?'summary':'timeline');
 $('transcript-tab').onclick=()=>setView('timeline');$('summary-tab').onclick=()=>setView('summary');$('literature-tab').onclick=()=>setView('research');
 const templateSelector=$('summary-template-kind');
 const templateKind=()=>templateSelector.value==='discussion'?'discussion':'academic_archive';
 const templateLanguage=()=>snapshot?.language==='en'?'en':'zh';
 const templateKey=()=>`conference-summary-template:${templateKind()}${templateLanguage()==='en'?':en':''}`;
 function loadTemplate(){
  const language=templateLanguage();
  const structure=templateKind()==='discussion'?window.ScientificDiscussionTemplate[language]:(language==='en'?englishTemplate:defaultTemplate);
  $('summary-template').value=stored(templateKey(),structure);
 }
 templateSelector.value=stored('conference-summary-template-kind')==='discussion'?'discussion':'academic_archive';
 templateSelector.onchange=()=>{store('conference-summary-template-kind',templateKind());loadTemplate();};
 loadTemplate();$('summary-template').oninput=()=>store(templateKey(),$('summary-template').value);
 function emit(type,data){window.dispatchEvent(new CustomEvent('conference:analysis-request',{detail:{type,session_id:snapshot?.session_id,...data}}));}
 function request(kind,detail={}){if(!snapshot||pending)return false;if(!native()){$('native-summary-status').textContent='请在 DSH Meetings 中运行原生 Agent；独立 Web 的纪要功能保持原配置。';return false;}const question=detail.question||(kind==='summary'?'Use this requested Markdown structure for the meeting report:\n'+$('summary-template').value:'Extract papers explicitly mentioned by this speaker/meeting, with links and primary-source metadata checks. Separately describe potential unpublished results; distinguish documented preprints, unpublished meeting claims and unknown publication status. Do not fabricate titles, authors or DOIs. Contextual recommendations must be marked as supplementary.');requestError=null;const request_id='analysis-'+Date.now()+'-'+Math.random().toString(36).slice(2);pending={kind,request_id};setView(kind);requestTimer=setTimeout(()=>{if(pending?.request_id!==request_id)return;clearPending();requestError={kind,error:'DSH 插件未确认请求。请重新连接或重启 DSH 以载入配套插件；当前会议和草稿已保留。'};render();},15000);emit('analysis-request',{kind,question,request_id,template:kind==='summary'?templateKind():undefined});render();return true;}
 $('native-generate').onclick=()=>request('summary');$('literature-generate').onclick=()=>request('research');
 function updateButtons(){const busy=Boolean(pending)||['starting','running','saving','cancelling','verifying'].includes(report?.run?.state);for(const id of ['native-generate','literature-generate'])$(id).disabled=!snapshot||busy;const run=summary();$('native-edit').disabled=!run?.text||busy||Boolean(editing);$('native-download').disabled=!summaryText();}
 function render(){updateButtons();const run=summary();const live=report?.run;const labels={starting:'准备中',running:'正在分析',saving:'正在保存',verifying:'正在核实引用文献',completed:'已完成',failed:'分析失败',stale:'会议已修订，请重新生成',cancelled:'已停止',interrupted:'上次运行中断'};
  for(const kind of ['summary','research']){const current=live?.kind===kind?live:report?.analyses?.[kind];statusNode(kind).textContent=requestError?.kind===kind?requestError.error:pending?.kind===kind?'正在请求 DSH 原生 Agent…':current?(labels[current.state]||current.state)+(current.error?' · '+current.error:''):'选择会议后，点击上方按钮开始。';}
  if(!pending&&requestError?.kind!=='summary'&&!run&&snapshot?.auto_summary){
   if(summaryText())$('native-summary-status').textContent='纪要已保存';
   else if(snapshot.state==='closed')$('native-summary-status').textContent=snapshot.segments?.length?'录音已结束，正在安排自动生成纪要。':'录音已结束，没有可用于生成纪要的转录。';
   else if(['starting','recording','draining'].includes(snapshot.state))$('native-summary-status').textContent='录音结束、转录处理完成后将自动生成纪要。';
  }
  if(requestError?.kind!=='summary'&&pending?.kind!=='summary'&&live?.kind==='summary'&&['running','saving','verifying'].includes(live.state))$('native-summary-status').textContent='正在生成纪要（可离开此页面，完成后自动保存到会议）';
  if(!editing){paint('native-report',summaryText()||'尚无 DSH 纪要。点击“按模板生成纪要”开始。');shown=run?.id||'';}
  paint('literature-report',report?.analyses?.research?.text||(live?.kind==='research'?live.text:'')||(report?.sources?.length?'以下学术文献已随会议纪要完成书目信息核实。需要追加检索时可点击上方按钮。':'尚无已核实的相关学术文献。'));
  for(const [key,id,label] of [['preprints','preprint-references','预印本 · 未经正式同行评审'],['milestones','clinical-references','临床与获批进展 · 非学术论文'],['pending_sources','pending-references','待核实学术线索 · 不计入已核实文献']]){const rows=report?.[key]||[];$(id).hidden=!rows.length;paint(id,rows.length?'### '+label+'\n'+rows.map(row=>'- ['+(row.title||row.doi||row.url)+']('+row.url+')').join('\n'):'');}
  const sources=report?.sources||[],key=JSON.stringify(sources);if(key!==sourcesKey){sourcesKey=key;$('meeting-references').replaceChildren(...sources.map(source=>{const card=document.createElement('article');card.className='reference-card';const a=document.createElement('a');a.href=source.url;a.target='_blank';a.rel='noopener noreferrer';a.textContent=source.title||source.url;const meta=document.createElement('small');meta.textContent=[source.authors,source.year,source.journal,source.status==='metadata'?'书目信息已核实':'待核实学术线索'].filter(Boolean).join(' · ');card.append(a,meta);return card;}));}
 }
 $('native-edit').onclick=()=>{const run=summary();if(!snapshot||!run||editing)return;$('native-save').disabled=false;editing={session_id:snapshot.session_id,run_id:run.id,expected_note_id:summaryDocument().note_id,text:summaryDocument().text};let draft;try{draft=JSON.parse(stored('conference-summary-draft:'+snapshot.session_id,'null'));}catch{}if(draft?.run_id===run.id){editing.text=draft.text;editing.expected_note_id=draft.expected_note_id??null;}$('native-report-editor').value=editing.text;$('native-report-editor').hidden=false;$('native-report').hidden=true;$('native-save').hidden=false;$('native-cancel').hidden=false;$('native-edit').hidden=true;$('native-report-editor').focus();};
 $('native-report-editor').oninput=rememberEditing;
 $('native-rebase').onclick=()=>{const run=summary();if(!editing||!snapshot||!run)return;accepted=null;editing.expected_note_id=noteHead();editing.run_id=run.id;rememberEditing();$('native-report').hidden=false;paint('native-report',summaryText());$('native-rebase').hidden=true;$('native-summary-status').textContent='最新稿已在下方展示；草稿仍保留，请对照后保存。来源已变化时须先重新生成纪要。';};
 function exitEditor(){editing=null;$('native-rebase').hidden=true;$('native-save').disabled=false;$('native-report-editor').hidden=true;$('native-report').hidden=false;$('native-save').hidden=true;$('native-cancel').hidden=true;$('native-edit').hidden=false;render();}
 $('native-cancel').onclick=()=>{rememberEditing();exitEditor();};
 $('native-save').onclick=()=>{if(!editing||saving)return;rememberEditing();saving={...editing,text:$('native-report-editor').value,request_id:'save-'+Date.now()};$('native-save').disabled=true;emit('analysis-save',{...saving,markdown:saving.text});};
 $('native-download').onclick=()=>{const text=editing?$('native-report-editor').value:summaryText();const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download='meeting_summary.md';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
 window.addEventListener('conference:selection',()=>{clearAnalysis();});
 window.addEventListener('conference:snapshot',e=>{const next=e.detail;if(!next?.session_id)return;const changed=snapshot?.session_id!==next.session_id,languageChanged=snapshot?.language!==next.language;snapshot=next;if(changed||languageChanged)loadTemplate();render();});
 window.addEventListener('conference:native-analysis',e=>{const d=e.detail;if(d.session_id!==snapshot?.session_id)return;if(d.type==='analysis-accepted'&&pending?.request_id===d.request_id){clearPending();report={...report,run:{kind:d.kind,state:'starting'}};render();}if(d.type==='analysis-result'){report=d.report;render();}if(d.type==='analysis-view')setView(d.kind==='question'?'summary':d.kind);if(d.type==='analysis-error'){if(!pending||!d.request_id||pending.request_id===d.request_id)clearPending();requestError={kind:d.kind,error:d.error||'请求失败'};render();}
  if(d.type==='analysis-saved'&&saving?.request_id===d.request_id){const submitted=saving;saving=null;$('native-save').disabled=false;if(!d.ok){$('native-summary-status').textContent=d.error||'保存失败；草稿保留。';$('native-rebase').hidden=false;return;}accepted={...submitted,note_id:d.note_id};if(editing)editing.expected_note_id=d.note_id||editing.expected_note_id;if(editing?.session_id===submitted.session_id&&$('native-report-editor').value===submitted.text){store('conference-summary-draft:'+submitted.session_id,'null');paint('native-report',submitted.text);exitEditor();}else{$('native-summary-status').textContent='已保存提交版本；之后输入的草稿仍保留。';}}
 });
 window.renderNotebookPreview=state=>{if(!state||state.id!==notebookId)return;paint('personal-preview',state.text);render();};
 function notebookView(preview){$('personal-text').hidden=preview;$('personal-preview').hidden=!preview;$('note-preview-tab').setAttribute('aria-pressed',String(preview));$('note-edit-tab').setAttribute('aria-pressed',String(!preview));const state=notebooks.get(notebookId);if(state)window.renderNotebookPreview(state);}
 $('note-edit-tab').onclick=()=>notebookView(false);$('note-preview-tab').onclick=()=>notebookView(true);
 function splitter(element,{key,property,axis,min,max,initial,measure}){const root=document.documentElement;let size=Math.max(min,Math.min(max,Number(stored(key,initial))||initial));function set(value){size=Math.max(min,Math.min(max,value));element.setAttribute('aria-valuenow',String(size));element.setAttribute('aria-valuemin',String(min));element.setAttribute('aria-valuemax',String(max));root.style.setProperty(property,size+(property==='--note-share'?'%':'px'));store(key,String(size));}set(size);element.onpointerdown=e=>{e.preventDefault();element.setPointerCapture(e.pointerId);const start=axis==='x'?e.clientX:e.clientY,old=size;document.body.classList.add('resizing');element.onpointermove=event=>{const delta=(axis==='x'?event.clientX:event.clientY)-start;set(old+(measure?delta/measure()*100:delta));};const stop=()=>{document.body.classList.remove('resizing');element.onpointermove=null;element.onpointerup=null;element.onpointercancel=null;};element.onpointerup=stop;element.onpointercancel=stop;};element.onkeydown=e=>{if(['ArrowLeft','ArrowUp','ArrowRight','ArrowDown'].includes(e.key)){e.preventDefault();set(size+(['ArrowLeft','ArrowUp'].includes(e.key)?-1:1)*(property==='--note-share'?3:20));}};}
 const listResizer=document.createElement('div');listResizer.className='meeting-list-resizer';listResizer.tabIndex=0;listResizer.role='separator';listResizer.setAttribute('aria-label','调节会议列表宽度');listResizer.setAttribute('aria-orientation','vertical');document.querySelector('.sidebar').append(listResizer);
 splitter(listResizer,{key:'conference-list-width',property:'--meeting-list-width',axis:'x',min:150,max:380,initial:195});
 splitter($('reading-resizer'),{key:'conference-transcript-height',property:'--transcript-height',axis:'y',min:120,max:800,initial:200});
 splitter($('notebook-resizer'),{key:'conference-notebook-height',property:'--notebook-height',axis:'y',min:130,max:800,initial:260});
 splitter($('notebook-column-resizer'),{key:'conference-note-share',property:'--note-share',axis:'x',min:30,max:75,initial:55,measure:()=>document.querySelector('.notebook-layout').getBoundingClientRect().width});
 if(!native()){$('presenter-tab').hidden=true;$('literature-tab').hidden=true;}
 setView('timeline');
})();
