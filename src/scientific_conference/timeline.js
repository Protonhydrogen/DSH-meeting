"use strict";
let timelineSignature='';
function applyTranscriptFilters(){
 const query=$('transcript-search').value.trim().toLocaleLowerCase();let total=0,shown=0;
 for(const row of $('transcript').children){if(!row.classList.contains('segment'))continue;total++;const editing=row.contains($('review-editor'))&&!$('review-editor').hidden;const match=(!query||[...row.querySelectorAll('p')].some(p=>p.textContent.toLocaleLowerCase().includes(query)));row.hidden=!(editing||match);if(!row.hidden)shown++;}
 $('transcript-results').textContent=query?shown+' / '+total+' 段':total+' 段';
}
function updateTranscriptFilters(){applyTranscriptFilters();}
$('transcript-search').addEventListener('input',applyTranscriptFilters);
function renderMeetingTimeline(d){
 const duration=Math.max(1,meetingDuration||0,...d.segments.map(s=>s.end_ms)),items=[];
 for(const s of d.segments)items.push({kind:'speech',time:s.start_ms,label:'转录段落'});
 const book=typeof notebooks!=='undefined'?notebooks.get(d.session_id):null;
 if(book){for(const image of book.images||[])items.push({kind:'image-marker',time:image.time_ms,label:image.name});for(const match of book.text.matchAll(/\[(\d{1,5}):([0-5]\d)\]/g))items.push({kind:'note-marker',time:(Number(match[1])*60+Number(match[2]))*1000,label:'个人笔记'});}
 items.sort((a,b)=>a.time-b.time);const selected=items.filter(x=>x.time>=0&&x.time<=duration).slice(0,150),signature=JSON.stringify([d.session_id,duration,selected]);if(signature===timelineSignature)return;timelineSignature=signature;
 $('timeline-markers').replaceChildren(...selected.map(item=>{const button=document.createElement('button');button.type='button';button.className='timeline-marker '+item.kind;button.style.left=Math.min(99,100*item.time/duration)+'%';button.title=clock(item.time)+' · '+item.label;button.setAttribute('aria-label',button.title);button.onclick=()=>{seekMeeting(item.time);if(item.kind==='note-marker'||item.kind==='image-marker')$('notebook-time').value=clock(item.time);};return button;}));
}

function clearMeetingTimeline(){timelineSignature='';$('timeline-markers').replaceChildren();$('transcript-search').value='';$('transcript-results').textContent='';}
