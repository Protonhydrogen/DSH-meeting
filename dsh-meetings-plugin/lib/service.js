import {spawn} from 'node:child_process';
import {join} from 'node:path';
import http from 'node:http';

export function validOrigin(value) {
  try {const u=new URL(value);return u.protocol==='http:'&&['127.0.0.1','localhost','[::1]'].includes(u.hostname)&&!!u.port&&!u.username&&!u.password&&u.pathname==='/'&&!u.search&&!u.hash&&u.origin===value;}catch{return false;}
}
function health(info) {
  return new Promise((resolve,reject)=>{
    const req=http.get(info.origin+'/api/info',{headers:{'X-Conference-Token':info.token},timeout:6000},res=>{
      res.resume();res.on('end',()=>res.statusCode===200?resolve():reject(Error('Backend health check failed')));
    });req.on('error',reject);req.on('timeout',()=>req.destroy(Error('Backend health check timed out')));
  });
}
export function runPython(config,command,payload,session) {
  return new Promise((resolve,reject)=>{
    const args=['-m','scientific_conference.dsh_plugin',command,'--config',config.configPath];
    if(session)args.push('--session',session);
    const child=spawn(config.python,args,{cwd:config.projectRoot,windowsHide:true,env:{...process.env,PYTHONPATH:join(config.projectRoot,'src'),PYTHONDONTWRITEBYTECODE:'1',PYTHONIOENCODING:'utf-8'},stdio:['pipe','pipe','pipe']});
    let stdout='',stderr='',size=0,done=false;
    const finish=(error,value)=>{if(done)return;done=true;clearTimeout(timer);error?reject(error):resolve(value);};
    const timer=setTimeout(()=>{child.kill();finish(Error('Meetings backend operation timed out; recordings continue independently'));},command==='bootstrap'?45000:60000);
    child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');
    child.stdout.on('data',chunk=>{size+=Buffer.byteLength(chunk,'utf8');if(size>9*1024*1024){child.kill();finish(Error('Backend response exceeded limit'));}else stdout+=chunk.toString('utf8');});
    child.stderr.on('data',chunk=>{stderr=(stderr+chunk.toString('utf8')).slice(-4000);});
    child.on('error',e=>finish(Error('Unable to start external Python: '+e.message)));
    child.on('close',code=>{if(done)return;try {const data=JSON.parse(stdout);if(code!==0||data.ok===false)throw Error(data.error||'Backend operation failed');finish(null,data);}catch(e){finish(Error(e.message+(stdout?'':' '+stderr)));}});
    child.stdin.on('error',()=>{});child.stdin.end(payload===undefined?'':JSON.stringify(payload));
  });
}
export function createBackend(config,dependencies={}) {
  const run=dependencies.run||((command,payload,session)=>runPython(config,command,payload,session));
  const probe=dependencies.health||health;let info,pending;
  const validSession=id=>{if(typeof id!=='string'||!(/^[A-Za-z0-9_-]{1,100}$/).test(id))throw Error('Invalid session ID');};
  return {
    async ensure(){
      if(pending)return pending;
      pending=(async()=>{if(info){try{await probe(info);return info;}catch{info=undefined;}}
        const next=await run('bootstrap');if(!validOrigin(next.origin)||typeof next.token!=='string'||!next.token)throw Error('Backend must return an authenticated loopback origin');
        // bootstrap itself probes readiness; avoid another slow bootstrap when reattaching.
        info=next;return info;
      })();try{return await pending;}finally{pending=undefined;}
    },
    async context(id,template){validSession(id);return run(template==='discussion'?'discussion-context':'context',undefined,id);},
    async pendingSummaries(){const result=await run('pending-summaries');return result.sessions;},
    async save(id,payload){validSession(id);return run('save',payload,id);},
  };
}
