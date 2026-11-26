import {citationSource as source} from './sources.js';
import {readFileSync,existsSync,mkdirSync,writeFileSync,renameSync} from 'node:fs';
import {join} from 'node:path';import {randomUUID} from 'node:crypto';
function validSession(id){if(typeof id!=='string'||!(/^[A-Za-z0-9_-]{1,100}$/).test(id))throw Error('Invalid session ID');}

export function createEvidenceStore(directory){
 mkdirSync(directory,{recursive:true});const file=join(directory,'evidence-workspace.json');let data=existsSync(file)?JSON.parse(readFileSync(file,'utf8')):{version:1,meetings:{}};if(data.version!==1||!data.meetings||typeof data.meetings!=='object')throw Error('Invalid evidence workspace state');
 const entry=id=>{validSession(id);return data.meetings[id]||{revision:0,topic:'',tags:[],bookmarks:[],sources:[]};};
 const scholarly=item=>{const s=source(item);return s&&s.status==='metadata'&&['article','review'].includes(s.publication_kind)?s:null;};
 function read(id,retrieved){const saved=entry(id),byUrl=new Map();for(const item of [...(retrieved===undefined?saved.sources||[]:retrieved),...saved.bookmarks]){const s=scholarly(item);if(s)byUrl.set(s.doi||s.url,s);}return structuredClone({...saved,bookmarks:saved.bookmarks.map(scholarly).filter(Boolean),sources:[...byUrl.values()]});}
 function sync(id,retrieved){const previous=entry(id),history=new Map((previous.sources||[]).map(s=>[s.url,s]));for(const item of retrieved||[]){const s=source(item);if(s)history.set(s.url,s);}const sources=[...history.values()];if(JSON.stringify(sources)!==JSON.stringify(previous.sources||[])){const replacement={...data,meetings:{...data.meetings,[id]:{...previous,sources}}},temporary=file+'.'+randomUUID()+'.tmp';writeFileSync(temporary,JSON.stringify(replacement),{encoding:'utf8',mode:0o600});renameSync(temporary,file);data=replacement;}return read(id,retrieved);}
 function save(id,update,retrieved=[]){const previous=entry(id);if(update.revision!==previous.revision)throw Error('Evidence workspace changed in another window; reload before saving');const next=structuredClone(previous);
  if(update.topic!==undefined){if(typeof update.topic!=='string'||update.topic.length>2000)throw Error('Topic must be at most 2000 characters');next.topic=update.topic.trim();}
  if(update.tags!==undefined){if(!Array.isArray(update.tags)||update.tags.length>12||update.tags.some(t=>typeof t!=='string'||t.length>60))throw Error('Invalid research tags');next.tags=[...new Set(update.tags.map(t=>t.trim()).filter(Boolean))];}
  if(update.bookmark!==undefined){const candidate=read(id,retrieved).sources.find(s=>s.url===update.bookmark);if(!candidate)throw Error('Only a retrieved source can be bookmarked');const index=next.bookmarks.findIndex(s=>s.url===candidate.url);if(index>=0)next.bookmarks.splice(index,1);else{if(next.bookmarks.length>=100)throw Error('Bookmark limit reached');next.bookmarks.push(candidate);}}
  next.revision++;const replacement={...data,meetings:{...data.meetings,[id]:next}},temporary=file+'.'+randomUUID()+'.tmp';writeFileSync(temporary,JSON.stringify(replacement),{encoding:'utf8',mode:0o600});renameSync(temporary,file);data=replacement;return read(id,retrieved);
 }
 return {read,save,sync};
}
