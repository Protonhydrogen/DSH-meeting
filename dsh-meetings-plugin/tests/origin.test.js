import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import vm from 'node:vm';
import {readFile} from 'node:fs/promises';
import {randomBytes} from 'node:crypto';
import {isAbsolute} from 'node:path';
const source=await readFile(new URL('../lib/index.js',import.meta.url),'utf8');
test('official Connection admits desktop bootstrap without Origin while rejecting foreign and unauthenticated callers',async t=>{
 let handler,starts=0,saved=0;const backend={ensure:async()=>{starts++;return {origin:'http://127.0.0.1:12345',token:'test-only-token',pid:1}},context:async()=>({review_epoch:1}),save:async()=>{saved++}};
 const timers=new Set(),attempts=[];
 backend.pendingSummaries=async()=>['already-handled','final-transcript','later'];
 const sandbox={URL,Buffer,Object,randomBytes,isAbsolute,mkdir:async()=>{},readFile,
  setTimeout(fn){timers.add(fn);return fn;},clearTimeout(fn){timers.delete(fn);},
  createBackend:()=>backend,createEvidenceStore:()=>({}),createAgentController:async()=>({
   dispose(){},startAutomatic:async id=>{attempts.push(id);return id==='final-transcript';}
  })};
 vm.createContext(sandbox);vm.runInContext(source.replace(/^import .*;\r?$/gm,'').replace(/^export /gm,'').replaceAll('import.meta.url',JSON.stringify('file:///test-only/index.js')),sandbox);
 const effects=[];
 const ctx={connection:{requestRejection(req){if(req.headers['sec-fetch-site']==='cross-site')return 403;if(req.headers.origin){try{if(new URL(req.headers.origin).host!==new URL('http://'+req.headers.host).host)return 403}catch{return 403}}return req.headers['x-test-auth']==='yes'?null:401}},webServer:{register:r=>{handler=r.handler;return()=>{}}},effect:fn=>effects.push(fn())};
 await sandbox.apply(ctx,{stateDir:'/test-only'});
 const server=http.createServer((q,s)=>handler(q,s));await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>new Promise(r=>server.close(r)));
 const base='http://127.0.0.1:'+server.address().port,headers={'x-test-auth':'yes','Content-Type':'application/json'},post=(path,h= headers,body={})=>fetch(base+'/meetings-api'+path,{method:'POST',headers:h,body:JSON.stringify(body)});
 assert.equal((await post('/bootstrap',{'Content-Type':'application/json'})).status,401);assert.equal(starts,0);
 assert.equal((await post('/bootstrap',{...headers,Origin:'https://untrusted.example'})).status,403);assert.equal(starts,0);
 assert.equal((await post('/bootstrap',{...headers,'Sec-Fetch-Site':'cross-site'})).status,403);assert.equal(starts,0);
 const response=await post('/bootstrap');assert.equal(response.status,200);assert.equal((await response.json()).recording_survives_close,true);assert.equal(starts,1);
 assert.equal((await post('/bootstrap',{...headers,Origin:base})).status,200);
 const conflict=await post('/report/save',headers,{session_id:'fixture',review_epoch:1});assert.equal(conflict.status,400);assert.match((await conflict.json()).error,/expected_note_id|编辑基线/);assert.equal(saved,0);
 const [scheduled]=timers;timers.delete(scheduled);await scheduled();
 assert.deepEqual(attempts,['already-handled','final-transcript']);
 for(const dispose of effects.reverse())await dispose();
 assert.equal(timers.size,0);
});
