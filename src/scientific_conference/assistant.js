"use strict";
let assistantSession='',assistantSignature='',assistantGraphSignature='',assistantData=null,assistantGraph=null,assistantSending=false;
let collapsedNodes=new Set();
const assistantModeHelp={strict:'只依据本场转写与纪要，适合可溯源纪要。',expert:'允许 DeepSeek 用已有的生化知识解释，并区分会议内容与背景解释。',research:'检索 Europe PMC 学术文献，并区分会议证据与文献补充。'};
const savedAssistantMode=localStorage.getItem('conferenceAssistantMode');
$('assistant-mode').value=assistantModeHelp[savedAssistantMode]?savedAssistantMode:'strict';
$('assistant-mode-help').textContent=assistantModeHelp[$('assistant-mode').value]||assistantModeHelp.strict;
const svgNS='http://www.w3.org/2000/svg';
function svgElement(name,attrs={}){const el=document.createElementNS(svgNS,name);for(const [k,v] of Object.entries(attrs))el.setAttribute(k,String(v));return el;}
function sourceDetails(ids,data){
 const details=document.createElement('details'),head=document.createElement('summary');head.textContent='来源 · '+ids.length+' 个片段';details.append(head);
 const lookup=new Map(data.segments.map(s=>[s.segment_id,s]));
 for(const id of ids){const s=lookup.get(id);if(!s)continue;const p=document.createElement('p');p.textContent=clock(s.start_ms)+'–'+clock(s.end_ms)+' · '+s.text;details.append(p);}
 return details;
}
function showMapNode(node){const el=$('map-detail');el.replaceChildren();const title=document.createElement('strong');title.textContent=node.label;const p=document.createElement('p');p.textContent=node.detail||'未提供额外说明';el.append(title,p);if(node.source_segment_ids?.length)el.append(sourceDetails(node.source_segment_ids,assistantData));else{const hint=document.createElement('p');hint.textContent='此节点没有直接引用片段；请结合原文核对。';el.append(hint);}}
function drawMeetingMap(){
 const box=$('map-canvas');box.replaceChildren();const graph=assistantGraph;
 if(!graph?.nodes?.length){const p=document.createElement('p');p.style.padding='25px';p.textContent='点击“AI 生成 / 重组导图”，让 DeepSeek 按本场内容组织分支。';box.append(p);return;}
 const nodes=new Map(graph.nodes.map(n=>[n.id,{...n,children:[]}])) ;let root;
 for(const n of nodes.values()){if(n.parent_id==null)root=n;else nodes.get(n.parent_id)?.children.push(n);}
 if(!root)return;
 let row=0,maxDepth=0;const visible=[];
 function layout(n,depth){n.depth=depth;maxDepth=Math.max(maxDepth,depth);visible.push(n);const children=collapsedNodes.has(n.id)?[]:n.children;
  if(children.length){children.forEach(c=>layout(c,depth+1));n.y=(children[0].y+children[children.length-1].y)/2;}else n.y=55+row++*105;
  n.x=25+depth*265;
 }layout(root,0);
 const width=(maxDepth+1)*265+30,height=Math.max(360,row*105+20),zoom=Number($('map-zoom').value)/100;
 const svg=svgElement('svg',{viewBox:`0 0 ${width} ${height}`,width:width*zoom,height:height*zoom,role:'group','aria-label':graph.title||'会议导图'});
 const colors=['#087c79','#3574a0','#855ca6','#ab6c25','#b45b71'];
 for(const n of visible){if(n.parent_id){const parent=nodes.get(n.parent_id);const x1=parent.x+210,x2=n.x,mid=(x1+x2)/2;svg.append(svgElement('path',{d:`M${x1} ${parent.y} H${mid} V${n.y} H${x2}`,fill:'none',stroke:colors[n.depth%colors.length],'stroke-width':2.5,'stroke-linejoin':'round'}));}}
 for(const n of visible){const color=colors[n.depth%colors.length],g=svgElement('g',{transform:`translate(${n.x},${n.y-32})`,role:'button',tabindex:0,'aria-label':n.label+(n.children.length?'，回车展开或收起分支':''),'aria-expanded':!collapsedNodes.has(n.id)});
  g.style.cursor='pointer';g.append(svgElement('rect',{width:210,height:64,rx:12,fill:n.depth?'white':color,stroke:color,'stroke-width':1.8}));
  const chars=Array.from(n.label),lines=[];for(let i=0;i<chars.length;i+=15)lines.push(chars.slice(i,i+15).join(''));
  const text=svgElement('text',{x:12,y:lines.length>1?25:37,fill:n.depth?'#193b44':'white','font-size':14,'font-family':'Segoe UI, Microsoft YaHei, sans-serif'});
  lines.slice(0,2).forEach((line,i)=>{const span=svgElement('tspan',{x:12,dy:i?20:0});span.textContent=line+(i===1&&lines.length>2?'…':'');text.append(span);});g.append(text);
  function toggle(){if(!n.children.length)return;collapsedNodes.has(n.id)?collapsedNodes.delete(n.id):collapsedNodes.add(n.id);drawMeetingMap();}
  g.onclick=()=>showMapNode(n);g.ondblclick=toggle;g.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();showMapNode(n);toggle();}};
  if(n.children.length){const badge=svgElement('g',{transform:'translate(210,32)'});badge.append(svgElement('circle',{r:11,fill:color}));const plus=svgElement('text',{x:0,y:5,'text-anchor':'middle',fill:'white','font-size':16});plus.textContent=collapsedNodes.has(n.id)?'+':'−';badge.append(plus);badge.onclick=e=>{e.stopPropagation();toggle();};g.append(badge);}
  svg.append(g);
 }box.append(svg);
}
function showMeetingAssistant(data){
 assistantData=data;
 if(assistantSession!==data.session_id){assistantSession=data.session_id;assistantSignature='';assistantGraphSignature='';collapsedNodes=new Set();$('assistant-instruction').value='';$('assistant-feedback').textContent='';$('map-detail').textContent='点击节点查看说明与来源；双击节点展开或收起。';}
 const state=data.assistant||{messages:[]};const pending=state.messages.some(m=>['pending','leased','failed'].includes(m.state));
 $('assistant-status').textContent=pending?'DeepSeek 正在处理，可继续查看会议':state.messages.some(m=>m.state==='dead')?'部分请求失败，可点击补做重试':'基于本场会议 · 指令与结果本地保存';
 $('assistant-send').disabled=assistantSending||pending||!data.segments.length||data.agent!=='openai_compatible';$('map-generate').disabled=$('assistant-send').disabled;
 let graph=state.map;
 if(!graph){const overview=[...data.notes].reverse().find(n=>n.mode==='session');if(overview){try{graph=JSON.parse(overview.content_json).mind_map;}catch{}}}
 const graphKey=data.session_id+JSON.stringify(graph);
 if(graphKey!==assistantGraphSignature){assistantGraphSignature=graphKey;assistantGraph=graph;collapsedNodes=new Set((graph?.nodes||[]).filter(n=>n.parent_id!=null).map(n=>n.id));$('map-title').textContent=graph?(graph.title+' · '+(state.map?'自定义导图，随新指令更新':'随总体总结生成')+(graph.through_ms?' · 覆盖至 '+clock(graph.through_ms):'')):'按会议内容组织，不使用固定栏目';drawMeetingMap();}
 const signature=JSON.stringify(state.messages);
 if(signature!==assistantSignature){assistantSignature=signature;const box=$('assistant-messages');box.replaceChildren();
  for(const m of state.messages){const article=document.createElement('article');const question=document.createElement('strong');question.textContent=`你（${{strict:'严格会议',expert:'专业解释',research:'联网研究'}[m.mode]||m.mode}）：`+m.instruction;article.append(question);const answer=document.createElement('p');answer.textContent=m.result?m.result.answer:({pending:'已排队',leased:m.mode==='research'?'DeepSeek 正在检索文献并回答…':'DeepSeek 正在查阅会议…',failed:'请求失败，后台将重试。',dead:'处理失败，请点击“补做纠错与总结”。'}[m.state]||m.state);article.append(answer);
   if(m.result?.map_updated){const p=document.createElement('small');p.textContent='已更新导图';article.append(p);}if(m.result?.source_segment_ids?.length)article.append(sourceDetails(m.result.source_segment_ids,data));
   if(m.result?.literature_sources?.length){const details=document.createElement('details'),head=document.createElement('summary');head.textContent='外部文献 · '+m.result.literature_sources.length+' 篇';details.append(head);for(const source of m.result.literature_sources){const p=document.createElement('p'),a=document.createElement('a');a.textContent=`${source.id} · ${source.title}${source.year?' ('+source.year+')':''}`;a.href=source.url;a.target='_blank';a.rel='noopener noreferrer';p.append(a);details.append(p);}article.append(details);}
   const coverage=m.result?.coverage;if(coverage&&coverage.selected_segments<coverage.total_segments){const p=document.createElement('p');p.className='hint';p.textContent=`本次参考 ${coverage.selected_segments}/${coverage.total_segments} 个转写片段及现有纪要；未覆盖全文。`;article.append(p);}box.append(article);
  }box.scrollTop=box.scrollHeight;
 }
}
async function sendMeetingInstruction(instruction,updateMap){
 if(!sid||assistantSending)return;assistantSending=true;$('assistant-send').disabled=true;$('map-generate').disabled=true;$('assistant-feedback').textContent='正在提交…';const session=sid;
 try{await api('/api/assistant/query',{session_id:session,instruction,update_map:updateMap,mode:$('assistant-mode').value,request_id:crypto.randomUUID()});if(sid===session){$('assistant-instruction').value='';$('assistant-feedback').textContent='已交给独立后台处理，完成后会自动显示。';}}
 catch(e){if(sid===session)$('assistant-feedback').textContent=e.message;}finally{assistantSending=false;await refresh();}
}
$('assistant-send').onclick=()=>sendMeetingInstruction($('assistant-instruction').value,$('assistant-update-map').checked);
$('map-generate').onclick=()=>sendMeetingInstruction('请根据本场会议的实际科研内容生成思维导图。由你决定有意义的主题和层级，不套用研究问题、方法、结果等固定栏目。保留关键机制、应用方向与不确定性，并引用会议片段。',true);
$('map-expand').onclick=()=>{collapsedNodes.clear();drawMeetingMap();};
$('map-collapse').onclick=()=>{collapsedNodes=new Set((assistantGraph?.nodes||[]).filter(n=>n.parent_id!=null).map(n=>n.id));drawMeetingMap();};
$('map-zoom').oninput=drawMeetingMap;
$('assistant-mode').onchange=()=>{localStorage.setItem('conferenceAssistantMode',$('assistant-mode').value);$('assistant-mode-help').textContent=assistantModeHelp[$('assistant-mode').value];};
