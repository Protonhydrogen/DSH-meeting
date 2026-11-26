import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync(new URL('../../src/scientific_conference/workspace.js',import.meta.url),'utf8');
function harness(){
 const elements=new Map(),storage=new Map(),sent=[],timers=new Map();let timerId=0;
 function node(){return {dataset:{},style:{setProperty(){}},classList:{add(){},remove(){},contains:v=>v==='is-embedded'},value:'',textContent:'',hidden:false,disabled:false,parentElement:{hidden:false},children:[],append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;this.innerHTML='';},setAttribute(k,v){this[k]=v;},getBoundingClientRect:()=>({width:900}),focus(){},click(){this.onclick?.();}};}
 const $=id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id);};
 const win=new EventTarget();win.ScientificMarkdown={css:'',render:text=>String(text)};
 const document={createElement:node,head:node(),body:node(),documentElement:node(),querySelector:selector=>$(selector)};
 win.addEventListener('conference:analysis-request',e=>sent.push(e.detail));
 vm.runInNewContext(readFileSync(new URL('../../src/scientific_conference/meeting-template.js',import.meta.url),'utf8'),{window:win});
 const scope={window:win,document,$,localStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v)},notebooks:new Map(),notebookId:'',setMeetingTab(){},CustomEvent,URL,Blob,setTimeout(fn){timers.set(++timerId,fn);return timerId;},clearTimeout(id){timers.delete(id);}};
 vm.runInNewContext(source,scope);
 const emit=(type,detail)=>win.dispatchEvent(new CustomEvent(type,{detail}));
 function select(id){emit('conference:selection',{session_id:id});emit('conference:snapshot',{session_id:id,presenter:'Test',review_epoch:1,notes:[]});emit('conference:native-analysis',{type:'analysis-result',session_id:id,report:{session_id:id,run:{id:'run-'+id,kind:'summary',state:'completed',text:'# '+id,review_epoch:1,evidence:{}},analyses:{}}});}
 return {$,select,emit,sent,scope,storage,expire(){const callbacks=[...timers.values()];timers.clear();for(const callback of callbacks)callback();}};
}
test('slow save in A cannot disable saving the next meeting B',()=>{
 const f=harness();f.select('A');f.$('native-edit').click();f.$('native-save').click();assert.equal(f.$('native-save').disabled,true);
 f.select('B');f.$('native-edit').click();assert.equal(f.$('native-save').disabled,false);f.$('native-save').click();assert.equal(f.sent.at(-1).session_id,'B');
});
test('typing after save submission preserves later draft on acknowledgement',()=>{
 const f=harness();f.select('A');f.$('native-edit').click();f.$('native-report-editor').value='# submitted';f.$('native-save').click();const request=f.sent.at(-1);
 f.$('native-report-editor').value='# later unsaved';f.emit('conference:native-analysis',{type:'analysis-saved',session_id:'A',request_id:request.request_id,run_id:'run-A',ok:true});assert.equal(f.$('native-report-editor').hidden,false);assert.equal(f.$('native-report-editor').value,'# later unsaved');assert.equal(f.$('native-save').disabled,false);
});
test('saved report remains visible when an older poll packet arrives',()=>{
 const f=harness();f.select('A');f.$('native-edit').click();f.$('native-report-editor').value='# accepted edit';f.$('native-save').click();const request=f.sent.at(-1);
 f.emit('conference:native-analysis',{type:'analysis-saved',session_id:'A',request_id:request.request_id,run_id:'run-A',ok:true});
 f.emit('conference:native-analysis',{type:'analysis-result',session_id:'A',report:{session_id:'A',run:{id:'run-A',kind:'summary',state:'completed',text:'# stale',review_epoch:1},analyses:{}}});assert.equal(f.$('native-report').innerHTML,'# accepted edit');
});
test('old meeting result and save acknowledgements cannot expose A in B',()=>{
 const f=harness();f.select('A');f.$('native-edit').click();f.$('native-save').click();const request=f.sent.at(-1);f.select('B');
 f.emit('conference:native-analysis',{type:'analysis-result',session_id:'A',report:{run:{kind:'summary',text:'SECRET_A'}}});f.emit('conference:native-analysis',{type:'analysis-saved',session_id:'A',request_id:request.request_id,ok:true});assert(!f.$('native-report').innerHTML.includes('SECRET_A'));assert.equal(f.$('native-save').disabled,false);
});

test('a newer manual note body is always paired with its own edit head after an accepted save',()=>{
 const f=harness();f.select('A');f.$('native-edit').click();f.$('native-report-editor').value='A old accepted';f.$('native-save').click();const request=f.sent.at(-1);
 f.emit('conference:native-analysis',{type:'analysis-saved',session_id:'A',request_id:request.request_id,run_id:'run-A',note_id:'note_A',ok:true});
 f.emit('conference:snapshot',{session_id:'A',presenter:'Test',review_epoch:1,notes:[{note_id:'note_B',mode:'session',agent:'dsh_native',content_md:'B newer confirmed',updated_utc:'2026-10-18T00:00:00Z',content_json:'{}'}]});
 f.$('native-edit').click();f.$('native-save').click();assert.equal(f.sent.at(-1).markdown,'B newer confirmed');assert.equal(f.sent.at(-1).expected_note_id,'note_B');
});


test('template button sends the project academic archive structure and reports acceptance',()=>{
 const f=harness();f.select('A');f.$('native-generate').click();const request=f.sent.at(-1);
 assert.equal(request.kind,'summary');assert.match(request.question,/status_summary/);assert.match(request.question,/文献清单汇总/);
 f.emit('conference:native-analysis',{type:'analysis-accepted',session_id:'A',kind:'summary',request_id:request.request_id});
 assert.match(f.$('native-summary-status').textContent,/准备中/);
});

test('discussion selection keeps its own editable structure and sends an explicit template',()=>{
 const f=harness();f.select('A');const selector=f.$('summary-template-kind'),editor=f.$('summary-template');
 const academic=editor.value;
 selector.value='discussion';selector.onchange();
 assert.match(editor.value,/## 讨论要点/);assert.match(editor.value,/## 行动项/);
 assert.equal(f.sent.length,0);
 editor.value='## 本组讨论';editor.oninput();
 selector.value='academic_archive';selector.onchange();assert.equal(editor.value,academic);
 selector.value='discussion';selector.onchange();assert.equal(editor.value,'## 本组讨论');
 f.$('native-generate').click();assert.equal(f.sent.at(-1).template,'discussion');assert.match(f.sent.at(-1).question,/本组讨论/);
});

test('discussion template uses English for an English meeting',()=>{
 const f=harness();f.select('A');const selector=f.$('summary-template-kind');
 selector.value='discussion';selector.onchange();
 f.emit('conference:snapshot',{session_id:'A',language:'en',presenter:'Test',notes:[]});
 assert.match(f.$('summary-template').value,/## Action Items/);
 assert.doesNotMatch(f.$('summary-template').value,/行动项/);
});

test('an old host that never acknowledges shows a visible recoverable error',()=>{
 const f=harness();f.select('A');f.$('literature-generate').click();f.expire();
 assert.match(f.$('literature-status').textContent,/未确认请求/);
 assert.equal(f.sent.length,1,'timeout must not submit another native run');
});

test('acknowledged long research has no request confirmation timeout',()=>{
 const f=harness();f.select('A');f.$('literature-generate').click();const request=f.sent.at(-1);
 f.emit('conference:native-analysis',{type:'analysis-accepted',session_id:'A',kind:'research',request_id:request.request_id});f.expire();
 assert.match(f.$('literature-status').textContent,/准备中/);assert.equal(f.$('literature-generate').disabled,true);
});

test('browser and native host share the same template structures',()=>{
 const f=harness(),native=JSON.parse(readFileSync(new URL('../lib/academic-template.json',import.meta.url),'utf8'));
 assert.equal(f.scope.window.ScientificMeetingTemplate.instructions,native.instructions);assert.equal(f.$('summary-template').value,native.markdown_structure);
 const discussion=JSON.parse(readFileSync(new URL('../lib/discussion-template.json',import.meta.url),'utf8'));
 for(const language of ['zh','en'])assert.equal(f.scope.window.ScientificDiscussionTemplate[language],discussion[language].markdown_structure);
});


test('timeline-only notebook and splitter survive tab switches with the note draft intact',()=>{
 const f=harness();f.select('A');f.$('personal-text').value='[07:20] unsaved note draft';
 for(const button of ['summary-tab','literature-tab']){
  f.$(button).click();assert.equal(f.$('personal-notebook').hidden,true);
  assert.equal(f.$('reading-resizer').hidden,true);
  f.$('transcript-tab').click();assert.equal(f.$('personal-notebook').hidden,false);
  assert.equal(f.$('reading-resizer').hidden,false);
  assert.equal(f.$('personal-text').value,'[07:20] unsaved note draft');
 }
});
