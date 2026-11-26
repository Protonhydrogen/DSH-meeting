import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {join} from 'node:path';
const read=path=>readFileSync(new URL(path,import.meta.url),'utf8');
const flush=()=>new Promise(done=>setImmediate(done));
const deferred=()=>{let resolve;const promise=new Promise(done=>resolve=done);return {promise,resolve};};
function flatten(node){if(Array.isArray(node))return node.flatMap(flatten);if(!node||typeof node!=='object')return [];return [node,...(node.children||[]).flatMap(flatten)];}
async function clientHarness(route=()=>undefined,initialStorage={}){
 const hooks=[],queued=[],listeners=new Map(),requests=[],timers=new Map(),retained=[],released=[],posted=[],navigated=[],storage=new Map(Object.entries(initialStorage)),registrations=[];let cursor=0,workspace,module,tree;
 const React={Fragment:'fragment',createElement:(type,props,...children)=>({type,props:props||{},children}),useState(initial){const i=cursor++;if(!(i in hooks))hooks[i]=initial;return [hooks[i],next=>{hooks[i]=typeof next==='function'?next(hooks[i]):next;}];},useRef(initial){const i=cursor++;return hooks[i]??={current:initial};},useEffect(callback,deps){const i=cursor++,old=hooks[i];if(!old||deps.some((v,j)=>v!==old.deps[j])){old?.cleanup?.();const entry={deps};hooks[i]=entry;queued.push(()=>{entry.cleanup=callback();});}}};
 const backend={ok:true,origin:'http://127.0.0.1:8124',token:'fixture',nonce:'a'.repeat(48)};
 const ctx={layout:{selectPanel:id=>navigated.push(id)},sessions:{retain(id,options){retained.push({id,options});return {sessionId:id,ready:Promise.resolve(),release:()=>released.push(id)};}},effect(){},slots:{entries(){return [{store:'native-conversation-store'}];},subscribe(){return ()=>{};},inject(name,callback){callback();},register(options,component){registrations.push(options);if(options.name==='main')workspace=component;}}};
 const frameWindow={postMessage:message=>posted.push(message)};
 // lib/client.js keeps the iframe theme tokens in sync through the real DOM, so the
 // vm context needs a tiny headless stub (no jsdom dependency, production code untouched).
 const domElement=()=>({style:{},textContent:'',setAttribute(){},remove(){},append(){},hasAttribute:()=>false,classList:{contains:()=>false,add(){},remove(){}}});
 const domBody=domElement();
 const scope={URL,URLSearchParams,location:{origin:'http://127.0.0.1:8123'},document:{createElement:domElement,body:domBody,documentElement:domElement(),head:domElement(),hidden:false,addEventListener(){},removeEventListener(){}},MutationObserver:class{observe(){}disconnect(){}},getComputedStyle:()=>({color:'rgba(0, 0, 0, 0)'}),localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},setTimeout,clearTimeout,setInterval:fn=>{const id=timers.size+1;timers.set(id,fn);return id;},clearInterval:id=>timers.delete(id),window:{__ModuleLoader__:{load({factory}){module=factory(()=>React);}},addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)},fetch:async(path,options)=>{const data=options?.body?JSON.parse(options.body):undefined;requests.push({path,data});let value=await route(path,data);if(value===undefined)value=path.endsWith('/bootstrap')?backend:path.endsWith('/agent/bind')?{ok:true,session_id:data.session_id,agent_session_id:'owned-'+data.session_id,run:null}:{ok:true,run:null};return {ok:true,json:async()=>value};}};
 vm.runInNewContext(read('../lib/client.js'),scope);module.apply(ctx);
 function render(){cursor=0;tree=workspace({ctx,SessionProvider:'NativeSessionProvider',renderSlot:(name,props)=>({type:'NativeConversation',props:{name,...props},children:[]})});for(const fn of queued.splice(0))fn();const iframe=flatten(tree).find(n=>n.type==='iframe');if(iframe)iframe.props.ref.current={contentWindow:frameWindow};return tree;}
 function message(data,overrides={}){return listeners.get('message')?.({source:frameWindow,origin:backend.origin,data:{channel:'dsh-meetings',nonce:backend.nonce,...data},...overrides});}
 function select(id){message({type:'selection',session_id:id,title:id,review_epoch:0});render();}
 render();await flush();render();return {render,select,message,requests,retained,released,posted,navigated,registrations,storage,nodes:()=>flatten(tree),tick:()=>Promise.all([...timers.values()].map(fn=>fn()))};
}
test('always mounts a native conversation pane with no imitation composer or evidence console',async()=>{const ui=await clientHarness();assert(ui.nodes().some(n=>n.type==='aside'&&n.props['aria-label']==='DSH 原生对话'));assert(!ui.nodes().some(n=>n.type==='textarea'));assert(!read('../lib/client.js').includes('证据与控制台'));assert.equal(ui.registrations.find(r=>r.name==='main').children['meetings.conversation'].scope,'session');});
test('meeting selection binds the official scoped conversation without replacing global navigation',async()=>{const ui=await clientHarness();ui.select('A');await flush();ui.render();const provider=ui.nodes().find(n=>n.type==='NativeSessionProvider');assert.equal(provider.props.session.sessionId,'owned-A');assert.equal(ui.nodes().find(n=>n.type==='NativeConversation').props.name,'meetings.conversation');assert.deepEqual(ui.navigated,[]);assert.equal(ui.retained[0].options.source,'sidebarView');});
test('switching meetings releases only the sidebar view and binds the next native history',async()=>{const ui=await clientHarness();ui.select('A');await flush();ui.render();ui.select('B');assert(!ui.nodes().some(n=>n.type==='NativeSessionProvider'));await flush();ui.render();assert.deepEqual(ui.released,['owned-A']);assert.equal(ui.nodes().find(n=>n.type==='NativeSessionProvider').props.session.sessionId,'owned-B');assert(!ui.requests.some(r=>r.path.includes('/cancel')));});
test('late native binding cannot expose the old meeting in a new sidebar',async()=>{const pending=deferred();const ui=await clientHarness((path,data)=>path.endsWith('/agent/bind')&&data.session_id==='A'?pending.promise:undefined);ui.select('A');ui.select('B');await flush();ui.render();pending.resolve({ok:true,session_id:'A',agent_session_id:'owned-A'});await flush();ui.render();assert.equal(ui.nodes().find(n=>n.type==='NativeSessionProvider').props.session.sessionId,'owned-B');assert(!ui.retained.some(r=>r.id==='owned-A'));});
test('only exact authenticated iframe messages may bind a meeting',async()=>{const ui=await clientHarness();for(const overrides of [{source:{}},{origin:'http://127.0.0.1:9999'},{data:{channel:'dsh-meetings',nonce:'wrong',type:'selection',session_id:'A'}}])ui.message({type:'selection',session_id:'A'},overrides);ui.render();await flush();assert.equal(ui.retained.length,0);});
test('both public embed requests reach the native Agent and receive acknowledgement',async()=>{for(const kind of ['summary','research']){const ui=await clientHarness();ui.select('A');await flush();ui.render();const handlers=new Map(),parent={postMessage:msg=>ui.message(msg)},nonce='a'.repeat(48);vm.runInNewContext(read('../../src/scientific_conference/embed.js'),{URL,URLSearchParams,parent,sid:'A',location:{search:'?embed=dsh&parent='+encodeURIComponent('http://127.0.0.1:8123')+'&nonce='+nonce},document:{body:{classList:{add(){}}},addEventListener(){},getElementById(){return null;}},window:{addEventListener:(name,fn)=>handlers.set(name,fn)}});handlers.get('conference:analysis-request')({detail:{type:'analysis-request',session_id:'A',kind,question:'TEMPLATE',request_id:'r-'+kind}});await flush();assert.equal(ui.requests.find(r=>r.path.endsWith('/agent/start')).data.kind,kind);assert(ui.posted.some(m=>m.type==='analysis-accepted'&&m.request_id==='r-'+kind));}});
test('native request failures return to the correct visible tab',async()=>{const ui=await clientHarness(path=>{if(path.endsWith('/agent/start'))throw Error('Unavailable');});ui.select('A');await flush();ui.render();await ui.message({type:'analysis-request',session_id:'A',kind:'research',question:'Sources',request_id:'r'});assert(ui.posted.some(m=>m.type==='analysis-error'&&m.kind==='research'&&m.error==='Unavailable'));});
test('selection changes do not reload the meeting iframe or discard Web input',async()=>{const ui=await clientHarness(),src=ui.nodes().find(n=>n.type==='iframe').props.src;ui.select('A');await flush();ui.render();ui.message({type:'selection-clear'});ui.render();ui.select('B');ui.render();assert.equal(ui.nodes().find(n=>n.type==='iframe').props.src,src);});
test('native chat receives no Meetings-specific button or textarea CSS overrides',()=>{const code=read('../lib/client.js');assert(!code.includes('.dsh-meetings button{'));assert(!code.includes('iframe{display:none'));});
test('switching away from a running analysis keeps its native session until idle',async()=>{
 let running=true;
 const ui=await clientHarness((path,data)=>{
  if(path.endsWith('/agent/bind')&&data.session_id==='A')return {ok:true,session_id:'A',agent_session_id:'owned-A',run:{id:'run-1',kind:'profile',state:'running'}};
  if(path.includes('/agent/status')&&path.includes('session_id=A'))return {ok:true,run:running?{id:'run-1',kind:'profile',state:'running'}:null};
  return undefined;
 });
 ui.select('A');await flush();ui.render();
 ui.select('B');
 assert(!ui.released.includes('owned-A'),'a running native session was released on panel switch');
 setTimeout(()=>{running=false;},900);
 await new Promise(done=>setTimeout(done,2600));
 assert(ui.released.includes('owned-A'),'the retained session was never released after idle');
});
test('Python stdout decoding preserves UTF-8 split across chunks',async()=>{const child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();child.stdin=new PassThrough();child.kill=()=>{};const source=read('../lib/service.js').replace(/^import .*;\r?\n/gm,'').replace(/^export /gm,'');const scope={spawn(){setImmediate(()=>{const bytes=Buffer.from(JSON.stringify({ok:true,text:'人工纪要'})),at=bytes.indexOf(Buffer.from('人'))+1;child.stdout.write(bytes.subarray(0,at));child.stdout.write(bytes.subarray(at));child.stdout.end();child.emit('close',0);});return child;},join,http:{},process:{env:{}},setTimeout,clearTimeout,Buffer,URL};vm.runInNewContext(source,scope);assert.equal((await scope.runPython({python:'fixture',configPath:'fixture',projectRoot:'.'},'context')).text,'人工纪要');});

test('native chat uses the reusable SDK factory without redeclaring host-owned slots',async()=>{const ui=await clientHarness();const main=ui.registrations.find(r=>r.name==='main');assert.deepEqual(Object.keys(main.children),['meetings.conversation']);assert.match(read('../lib/client.js'),/renderFactorySlot\('conversation.content'/);});

test('native view selector shares the host conversation store so tool inspection and context links still work',async()=>{const ui=await clientHarness();assert.equal(ui.registrations.find(r=>r.name==='meetings.conversation').store,'native-conversation-store');assert.match(read('../lib/client.js'),/actions.setView\(tab.id\)/);assert(!read('../lib/client.js').includes('view:current'));});


test('native chat collapses without unmounting its session or reloading the meeting iframe',async()=>{
 const ui=await clientHarness();ui.select('A');await flush();ui.render();
 const provider=ui.nodes().find(n=>n.type==='NativeSessionProvider').props.session;
 const src=ui.nodes().find(n=>n.type==='iframe').props.src,requests=ui.requests.length;
 const toggle=()=>ui.nodes().find(n=>n.type==='button'&&n.props['aria-controls']==='dsh-meetings-native-chat');
 assert.equal(toggle().props['aria-expanded'],true);toggle().props.onClick();ui.render();
 assert.equal(ui.nodes().find(n=>n.type==='aside').props.hidden,true);
 assert.equal(ui.nodes().find(n=>n.props.className==='meeting-chat-resizer').props.hidden,true);
 assert.equal(toggle().props['aria-expanded'],false);assert.equal(ui.storage.get('dsh-meetings.chat-open'),'false');
 assert.equal(ui.nodes().find(n=>n.type==='NativeSessionProvider').props.session,provider);
 toggle().props.onClick();ui.render();assert.equal(ui.nodes().find(n=>n.type==='aside').props.hidden,false);
 assert.equal(ui.nodes().find(n=>n.type==='iframe').props.src,src);assert.equal(ui.requests.length,requests);
 assert.deepEqual(ui.released,[]);
});

test('native chat restores a saved collapsed preference on the next mount',async()=>{
 const ui=await clientHarness(undefined,{'dsh-meetings.chat-open':'false','dsh-meetings.chat-width':'620'});
 assert.equal(ui.nodes().find(n=>n.type==='aside').props.hidden,true);
 assert.equal(ui.nodes().find(n=>n.props.className==='meeting-chat-resizer').props['aria-valuenow'],620);
});
