// Tool evidence remains auditable. Only cited scholarly records become literature.
const PREPRINT=/^(?:10\.1101\/|10\.21203\/|10\.26434\/chemrxiv|10\.48550\/arxiv|10\.22541\/)/i;
const clean=value=>String(value||'').replace(/<[^>]*>/g,'').replace(/\s+/g,' ').trim();
function doiOf(value){let raw=String(value||'');try{raw=decodeURIComponent(raw);}catch{}const match=raw.match(/10\.\d{4,9}\/[^\s<>"?#\u3000-\u303f\uff00-\uffef\u3400-\u9fff]+/i);if(!match)return null;let doi=match[0].replace(/[.,;:\]}]+$/,'');while(doi.endsWith(')')&&(doi.match(/\)/g)||[]).length>(doi.match(/\(/g)||[]).length)doi=doi.slice(0,-1);return doi.toLowerCase();}
function keys(value){
 const out=new Set(),doi=doiOf(value);if(doi)out.add('doi:'+doi);
 try{const u=new URL(value);u.hash='';out.add('url:'+u.href.toLowerCase());
  if(u.hostname==='pubmed.ncbi.nlm.nih.gov'&&/^\/\d+\/?$/.test(u.pathname))out.add('pmid:'+u.pathname.replaceAll('/',''));
  if(u.hostname==='eutils.ncbi.nlm.nih.gov'&&u.searchParams.get('db')==='pubmed'&&/^\d+$/.test(u.searchParams.get('id')||''))out.add('pmid:'+u.searchParams.get('id'));
  if(/(?:^|\.)europepmc\.org$/.test(u.hostname)&&/^\/article\/MED\/\d+$/i.test(u.pathname))out.add('pmid:'+u.pathname.split('/').at(-1));
  if(/(?:^|\.)nature\.com$/.test(u.hostname)&&/^\/articles\/[a-z]\d+-\d+-\d+-[a-z0-9]+$/i.test(u.pathname))out.add('doi:10.1038/'+u.pathname.split('/').at(-1).toLowerCase());
 }catch{}return out;
}
export function citationSource(item){
 try{const u=new URL(item.url);if(u.protocol!=='https:'||u.username||u.password)return null;u.hash='';
  if(u.pathname==='/vulnerability-disclosure-policy/index.html'||u.pathname.includes('/grantfinder/'))return null;
  const doi=doiOf(item.doi||u.pathname),result={url:u.href,title:clean(item.title||u.hostname).slice(0,500),host:u.hostname,status:item.status==='metadata'?'metadata':'retrieved'};
  if(doi){result.doi=doi;if(u.hostname==='doi.org'||u.hostname==='dx.doi.org')result.url='https://doi.org/'+doi;}
  for(const key of ['authors','year','journal','record_source','record_url','pmid','publication_kind','milestone_stage'])if(typeof item[key]==='string')result[key]=clean(item[key]).slice(0,1000);
  if(!result.publication_kind){if(PREPRINT.test(doi||'')||/^(?:www\.)?(?:arxiv|biorxiv|medrxiv|chemrxiv)\.org$/.test(u.hostname))result.publication_kind='preprint';else if(result.status==='metadata'&&result.journal)result.publication_kind='article';else result.publication_kind='other';}
  return result;
 }catch{return null;}
}
function upsert(run,item){const next=citationSource(item);if(!next)return;run.sources??=[];const index=run.sources.findIndex(s=>s.url===next.url||next.doi&&s.doi?.toLowerCase()===next.doi);if(index<0){if(run.sources.length<200)run.sources.push(next);}else{const old=run.sources[index];run.sources[index]=old.status==='metadata'&&next.status!=='metadata'?{...next,...old}:{...old,...next};}}
function texts(value,depth=0){if(depth>8||value==null)return [];if(typeof value==='string')return [value.slice(0,1000000)];if(Array.isArray(value))return value.slice(0,200).flatMap(v=>texts(v,depth+1));if(typeof value==='object')return Object.values(value).slice(0,200).flatMap(v=>texts(v,depth+1));return [];}
function parseJson(text){const start=text.indexOf('{');if(start<0)return null;let depth=0,quoted=false,escape=false;for(let i=start;i<text.length;i++){const c=text[i];if(quoted){if(escape)escape=false;else if(c==='\\')escape=true;else if(c==='"')quoted=false;}else if(c==='"')quoted=true;else if(c==='{')depth++;else if(c==='}'&&--depth===0){try{return JSON.parse(text.slice(start,i+1));}catch{return null;}}}return null;}
function publication(types,doi,journal){const list=Array.isArray(types)?types:[types||''];const joined=list.join(' ').toLowerCase();if(/preprint|posted-content/.test(joined)||PREPRINT.test(doi||''))return 'preprint';if(/news|editorial|erratum|retraction|correction|dataset|grant|book/.test(joined))return 'other';if(/review/.test(joined))return 'review';if(/journal.article|proceedings.article|clinical trial|research.article/.test(joined)||journal)return 'article';return 'other';}
function metadata(run,row,url,source){
 const doi=doiOf(row.doi||row.DOI||row.articleids?.find(i=>i.idtype==='doi')?.value),pmid=String(row.uid||row.pmid||row.id&&row.source==='MED'&&row.id||'');
 const title=clean(Array.isArray(row.title)?row.title[0]:row.title),authors=clean(row.authorString||(row.authors||row.author||[]).map(a=>a.name||[a.given,a.family].filter(Boolean).join(' ')).join(', '));
 const date=String(row.pubYear||row.pubdate||row.published?.['date-parts']?.[0]?.[0]||''),year=date.match(/\b(?:19|20)\d{2}\b/)?.[0]||'';
 const journal=clean(row.journalTitle||row.fulljournalname||row.journalInfo?.journal?.title||row['container-title']?.[0]||'');
 if(!title||!authors||!year||(!doi&&!/^\d+$/.test(pmid)))return;
 upsert(run,{url:doi?'https://doi.org/'+doi:'https://pubmed.ncbi.nlm.nih.gov/'+pmid+'/',title,authors,year,journal,doi:doi||undefined,pmid,publication_kind:publication(row.pubtype||row.pubTypeList?.pubType||row.type,doi,journal),status:'metadata',record_source:source,record_url:url});
}
export function collectSources(run,value,call){
 if(!['web_search','web_fetch'].includes(call?.name))return;
 const parts=texts(value);let args;try{args=typeof call.arguments==='string'?JSON.parse(call.arguments):call.arguments;}catch{}
 if(parts.some(text=>/^Fetched .+\(HTTP [45]\d{2}\)/m.test(text)))return;
 if(call.name==='web_search'){
  const visit=(item,depth=0)=>{if(depth>8||item==null)return;if(Array.isArray(item)){for(const v of item.slice(0,200))visit(v,depth+1);}else if(typeof item==='object'){if(item.url)upsert(run,{url:item.url,title:item.title});for(const v of Object.values(item).slice(0,200))visit(v,depth+1);}};visit(value);
  for(const text of parts)for(const match of text.matchAll(/\[([^\n]{1,500}?)\]\((https:\/\/[^\s<>]+?)\)/g))upsert(run,{title:match[1],url:match[2]});return;
 }
 let target;try{target=new URL(args?.url);}catch{return;}upsert(run,{url:target.href});
 const europe=/(^|\.)ebi\.ac\.uk$/.test(target.hostname)&&target.pathname.includes('/europepmc/webservices/rest/'),crossref=target.hostname==='api.crossref.org',pubmed=target.hostname==='eutils.ncbi.nlm.nih.gov'&&target.searchParams.get('db')==='pubmed';
 if(europe||crossref||pubmed){
  for(const obj of [value,...parts.map(parseJson)].filter(Boolean)){
   const rows=europe?obj.resultList?.result:crossref?(obj.message?.items||[obj.message]):obj.result?.uids?.map(id=>obj.result[id]);
   if(Array.isArray(rows))for(const row of rows)if(row)metadata(run,row,target.href,europe?'Europe PMC':crossref?'Crossref':'PubMed');
  }
  if(pubmed&&target.pathname.endsWith('/efetch.fcgi'))for(const part of parts){
   const body=part.slice(part.search(/(?:^|\n)\d+\. /)),paragraphs=body.trim().split(/\n\s*\n/),head=paragraphs[0]||'',doi=doiOf(head),pmid=part.match(/PMID:\s*(\d+)/)?.[1]||target.searchParams.get('id');
   if(/^\d+\. /.test(head)&&paragraphs.length>=3&&doi&&/^\d+$/.test(pmid||'')){
    const year=head.match(/\b(?:19|20)\d{2}\b/)?.[0],journal=head.replace(/^\d+\. /,'').split(/\.\s*(?:19|20)\d{2}/)[0];
    metadata(run,{doi,uid:pmid,title:paragraphs[1],authorString:paragraphs[2].replace(/\(\d+(?:,\d+)*\)/g,''),pubYear:year,journalTitle:journal,pubtype:['Journal Article']},target.href,'PubMed');
   }
  }
 }
 // Only concrete, fetched regulator/trial milestones qualify; not grant/hope news.
 if(/(?:^|\.)(?:fda\.gov|ema\.europa\.eu|clinicaltrials\.gov)$/.test(target.hostname)){
  const body=parts.join('\n'),negative=/\b(?:may|might|could|eventually|preclinical|not yet)\b/i.test(body);
  const stage=body.match(/\b(?:FDA (?:approves|approved)|(?:entered|enters|initiated|initiates|completed|completes|began|begins|started|starts|results (?:from|of))[^.\n]{0,60}phase\s+(?:I{1,3}|[123])\b)/i)?.[0];
  if(stage&&!negative)upsert(run,{url:target.href,title:stage,publication_kind:'clinical_milestone',milestone_stage:stage});
 }
}
function citedKeys(text){const out=new Set();for(const match of String(text||'').matchAll(/https:\/\/[^\s<>"\[\]]+|10\.\d{4,9}\/[^\s<>"\[\]]+/gi)){const value=match[0].replace(/[.,;:)]+$/,'');for(const key of keys(value))out.add(key);}return out;}
function aliases(item){const all=keys(item.url);if(item.doi)all.add('doi:'+item.doi.toLowerCase());if(item.pmid)all.add('pmid:'+item.pmid);if(item.record_url){try{const record=new URL(item.record_url),extra=keys(item.record_url);for(const k of extra)if(!k.startsWith('url:')||!record.search||record.searchParams.has('id'))all.add(k);}catch{}}return all;}
export function literatureView(run){
 const view={papers:[],preprints:[],milestones:[],pending:[]};if(!run||run.automatic||run.template==='discussion'||!['summary','research'].includes(run.kind))return view;
 const cited=citedKeys(run.text),seen=new Set();
 for(const raw of run.sources||[]){const item=citationSource(raw);if(!item||![...aliases(item)].some(k=>cited.has(k)))continue;const id=item.doi?'doi:'+item.doi:item.pmid?'pmid:'+item.pmid:item.url;if(seen.has(id))continue;seen.add(id);
  if(item.publication_kind==='clinical_milestone')view.milestones.push(item);
  else if(item.status==='metadata'&&['article','review'].includes(item.publication_kind))view.papers.push(item);
  else if(item.status==='metadata'&&item.publication_kind==='preprint')view.preprints.push(item);
  else if(item.doi||item.pmid||[...keys(item.url)].some(k=>k.startsWith('pmid:')))view.pending.push(item);
 }
 for(const key of cited)if((key.startsWith('doi:')||key.startsWith('pmid:'))&&!seen.has(key)){
  // Publisher/record URL and bare DOI aliases may point at the same already verified row.
  if([...view.papers,...view.preprints,...view.pending].some(s=>aliases(s).has(key)))continue;
  const id=key.slice(key.indexOf(':')+1),item=citationSource({url:key.startsWith('doi:')?'https://doi.org/'+id:'https://pubmed.ncbi.nlm.nih.gov/'+id+'/',title:id});if(item){view.pending.push(item);seen.add(key);}
 }
 const verified=[...view.papers,...view.preprints];view.pending=view.pending.filter(item=>!verified.some(v=>[...aliases(item)].some(key=>aliases(v).has(key))));
 return view;
}
export function replaySources(run){
 const result={...run,sources:[...(run.sources||[])]};
 for(const event of run.events||[])if(event.type==='tool/result'&&!event.is_error){const call=(run.events||[]).find(e=>e.type==='tool/call'&&e.call_id===event.call_id);if(!call)continue;let value;try{value=JSON.parse(event.result);}catch{continue;}collectSources(result,value,call);}
 return result;
}

export function mergeLiteratureViews(views){
 const unique=key=>[...new Map(views.flatMap(v=>v[key]).map(item=>[item.doi||item.pmid||item.url,item])).values()];
 const result={papers:unique('papers'),preprints:unique('preprints'),milestones:unique('milestones'),pending:unique('pending')};
 const verified=new Set([...result.papers,...result.preprints].flatMap(s=>[...aliases(s)]));
 result.pending=result.pending.filter(s=>![...aliases(s)].some(key=>verified.has(key)));
 return result;
}
