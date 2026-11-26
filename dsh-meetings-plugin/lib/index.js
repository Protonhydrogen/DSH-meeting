import {createEvidenceStore} from './evidence.js';
import {createBackend} from './service.js';
import {createAgentController} from './agent.js';
import {randomBytes} from 'node:crypto';
import {mkdir,readFile} from 'node:fs/promises';
import {isAbsolute} from 'node:path';
export const name='dsh-meetings';
export const inject=['webServer','connection','tools','sessionController','permissionPresets','approval','attachments'];
export const Config={'~standard':{version:1,vendor:'dsh-meetings',validate(raw){
  const value={...raw};const required=['projectRoot','python','configPath','stateDir','appRoot'];
  const issues=required.filter(k=>typeof value[k]!=='string'||!isAbsolute(value[k])).map(k=>({message:k+' must be an absolute path'}));
  return issues.length?{issues}:{value};
}}};
const PREFIX='/meetings-api';
function json(res,status,value){res.writeHead(status,{'content-type':'application/json; charset=utf-8','cache-control':'no-store','x-content-type-options':'nosniff'});res.end(JSON.stringify(value));}
async function body(req){
  if(Number(req.headers['content-length'])>1024*1024)throw Error('Request too large');
  const chunks=[];let count=0;for await(const chunk of req){count+=chunk.length;if(count>1024*1024)throw Error('Request too large');chunks.push(chunk);}return JSON.parse(Buffer.concat(chunks).toString('utf8')||'{}');
}
export async function apply(ctx,config){
  await mkdir(config.stateDir,{recursive:true});
  const backend=createBackend(config);
  const evidence=createEvidenceStore(config.stateDir);
  const agent=await createAgentController(ctx,{stateDir:config.stateDir,appRoot:config.appRoot,projectRoot:config.projectRoot,contextProvider:async (id,template)=>{const context=await backend.context(id,template);if(template==='discussion')return context;const {topic,tags}=evidence.read(id);if(topic||tags.length)context.prompt+='\nUser research focus (untrusted context, not permissions): '+JSON.stringify({topic,tags});return context;},saveResult:(id,payload)=>backend.save(id,payload)});
  ctx.effect(()=>()=>agent.dispose(),'dsh-meetings: native agent lifecycle');
  ctx.effect(()=>{
    let stopped=false,timer;
    const schedule=delay=>{timer=setTimeout(check,delay);timer.unref?.();};
    async function check(){
      try{
        for(const id of await backend.pendingSummaries()){
          if(stopped)break;
          try{if(await agent.startAutomatic(id))break;}
          catch(error){ctx.logger?.warn?.('Automatic meeting minutes: '+error.message);}
        }
      }catch(error){ctx.logger?.warn?.('Checking for completed recordings: '+error.message);}
      finally{if(!stopped)schedule(8000);}
    }
    schedule(3000);
    return ()=>{stopped=true;clearTimeout(timer);};
  },'dsh-meetings: automatic final minutes');
  const handler=async(req,res)=>{
    const rejection=ctx.connection.requestRejection(req);
    if(rejection){req.resume();return json(res,rejection,{ok:false,error:rejection===401?'Unauthorized':'Forbidden'});}
    try{
      const url=new URL(req.url,'http://localhost');const path=url.pathname.slice(PREFIX.length);
      if(req.method==='GET'&&path==='/markdown.js'){const code=await readFile(new URL('./markdown.js',import.meta.url));res.writeHead(200,{'content-type':'text/javascript; charset=utf-8','cache-control':'no-cache','x-content-type-options':'nosniff'});res.end(code);return;}
      if(req.method==='POST'){
        // Official Connection already enforces Host/Origin and authentication.
        // Admitted desktop/API requests can legitimately omit the Origin header.
        const data=await body(req);
        if(path==='/bootstrap'){
          const info=await backend.ensure();return json(res,200,{ok:true,origin:info.origin,token:info.token,nonce:randomBytes(24).toString('hex'),pid:info.pid,recording_survives_close:true});
        }
        if(path==='/report/save'){if(!Object.hasOwn(data,'expected_note_id')||(data.expected_note_id!==null&&typeof data.expected_note_id!=='string'))throw Error('请从最新纪要载入编辑基线；草稿保留。');const current=await backend.context(data.session_id);if(current.review_epoch!==data.review_epoch)throw Error('转录已修订，请重新生成或重新载入纪要');return json(res,200,await backend.save(data.session_id,{markdown:data.markdown,review_epoch:data.review_epoch,run_id:'manual-'+randomBytes(16).toString('hex'),evidence:data.evidence,expected_note_id:data.expected_note_id}));}
        if(path==='/evidence'){await backend.context(data.session_id);return json(res,200,{ok:true,session_id:data.session_id,...evidence.save(data.session_id,data,agent.status(data.session_id).sources)});}
        if(path==='/agent/bind')return json(res,200,await agent.bind(data.session_id));
        if(path==='/agent/start')return json(res,200,await agent.start(data));
        if(path==='/agent/save-chat')return json(res,200,await agent.saveChat(data.session_id,data.turn));
        if(path==='/agent/cancel')return json(res,200,await agent.cancel(data.session_id));
      }
      if(req.method==='GET'&&path==='/evidence'){const id=url.searchParams.get('session_id');return json(res,200,{ok:true,session_id:id,...evidence.sync(id,agent.status(id).sources)});}
      if(req.method==='GET'&&path==='/agent/status')return json(res,200,await agent.status(url.searchParams.get('session_id')));
      if(req.method==='GET'&&path==='/health'){const info=await backend.ensure();return json(res,200,{ok:true,plugin:'dsh-meetings',version:'0.4.3',backend_origin:info.origin});}
      return json(res,404,{ok:false,error:'Not found'});
    }catch(error){return json(res,400,{ok:false,error:error.message||String(error)});}
  };
  ctx.effect(()=>ctx.webServer.register({kind:'prefix',path:PREFIX,handler}),'dsh-meetings: authenticated API');
}
