'use strict';

const byId = id => document.getElementById(id);
let snapshot, guide, state, view;
let saveTimer, scrollTimer, queue = Promise.resolve(), rendering = false;
let cleanupAction=null;
let checkoutInfo=null;
let reviewActions=null;
const files = new Map();
const esc = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const attr = esc;
const fileKey = path => encodeURIComponent(path);

async function api(path, options) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || response.statusText);
  return result;
}

function persist(immediate = false) {
  byId('save-status').textContent = 'Saving…';
  clearTimeout(saveTimer);
  const save = () => {
    const body = JSON.stringify(state);
    queue = queue.catch(() => {}).then(() => api('/api/state', {method:'POST',headers:{'Content-Type':'application/json'},body,keepalive:true}))
      .then(() => { byId('save-status').textContent = 'Saved on this computer'; byId('save-status').className=''; })
      .catch(error => { byId('save-status').textContent = `Not saved: ${error.message}`; byId('save-status').className='saved-error'; });
  };
  if (immediate) save(); else saveTimer = setTimeout(save, 250);
}

function sectionState(section) { return state.sections[section.id] || {}; }
function isSeen(section) { const saved=sectionState(section); return !!saved.seen && saved.fingerprint===section.fingerprint; }
function isChanged(section) { const saved=sectionState(section); return !!saved.seen && saved.fingerprint!==section.fingerprint; }
function progress() {
  const seen=guide.sections.filter(isSeen).length;
  byId('progress-label').textContent=`${seen} of ${guide.sections.length} sections seen`;
  byId('progress').max=guide.sections.length; byId('progress').value=seen;
  byId('outline').innerHTML=guide.sections.map(s=>`<button data-section="${attr(s.id)}" ${view.type==='section'&&view.id===s.id?'aria-current="page"':''}><span class="nav-symbol">${isSeen(s)?'✓':isChanged(s)?'↻':'○'}</span><span class="nav-label">${esc(s.title)}<small>${isChanged(s)?'Changed since you looked':esc(s.subtitle||'')}</small></span></button>`).join('');
  byId('resume').hidden=!state.readingSpot || (view.type==='section' && view.id===state.readingSpot.id);
  byId('file-list').querySelector('summary').textContent=`All changed files · ${snapshot.files.length}`;
  byId('files').innerHTML=guide.sections.map(s=> {
    const members=s.files.map(path=>files.get(path)).filter(f=>f&&!f.context);
    if(!members.length) return '';
    return `<div class="file-group">${esc(s.title)}</div>${members.map(f=>`<button class="file-button" data-file="${fileKey(f.path)}" data-side="${f.head.exists?'head':'base'}" ${view.type==='file'&&view.path===f.path?'aria-current="page"':''}>${esc(f.path.split('/').at(-1))}<small>${esc(f.path.split('/').slice(0,-1).join('/'))} · ${esc(f.status)}</small></button>`).join('')}`;
  }).join('');
}

function refButton(ref, label) {
  return `<button class="ref" data-source="${fileKey(ref.path)}" data-side="${attr(ref.side||'head')}" data-line="${ref.start||1}">${esc(label||`${ref.path.split('/').at(-1)}:${ref.start||1}`)}</button>`;
}
function refsHtml(refs) { return refs?.length?`<div class="refs">${refs.map(r=>refButton(r,r.label)).join('')}</div>`:''; }

function codeRow(row, path, highlight, sourceSide) {
  const number=(line,side)=>line==null?'':`<button class="line-number" data-editor="${fileKey(path)}" data-side="${side}" data-line="${line}" aria-label="Open ${side} line ${line} in VS Code">${line}</button>`;
  if(sourceSide) return `<div class="code-row ${highlight?'focus-line':''}" data-code-line="${row.new||row.old}" data-code-side="${sourceSide}">${number(row.new||row.old,sourceSide)}<span></span><code>${esc(row.text)}</code></div>`;
  const side=row.kind==='remove'?'base':'head',line=side==='base'?row.old:row.new;
  const action=line==null?'':`<button class="row-plus" data-ra="line" data-path="${fileKey(path)}" data-side="${side}" data-line="${line}" aria-label="Actions for ${attr(path)} ${side} line ${line}" title="Ask or comment · Shift-click another + to select a range">+</button>`;
  return `<div class="code-row ${row.kind} ${highlight?'focus-line':''}" data-new-line="${row.new||''}" data-old-line="${row.old||''}"><span>${action}</span><span>${number(row.old,'base')}</span><span>${number(row.new,'head')}</span><span>${row.kind==='add'?'+':row.kind==='remove'?'−':''}</span><code>${esc(row.text)}</code></div>`;
}

function diffHtml(block, index, full=false) {
  const file=files.get(block.path), side=block.side||'head', field=side==='base'?'old':'new';
  const expanded=full || state.expanded[`excerpt:${view.id}:${index}`];
  const hunks=file.hunks.filter(h=>full||h.rows.some(r=>r[field]!=null && r[field]>=block.start && r[field]<=block.end));
  let content='';
  for(const hunk of hunks) {
    content+=`<div class="hunk-header">${esc(hunk.header)}</div>`;
    let lastVisible=true;
    for(let i=0;i<hunk.rows.length;i++) {
      const row=hunk.rows[i];
      let visible=expanded || (row[field]!=null && row[field]>=block.start && row[field]<=block.end);
      if(!visible && row[field]==null) {
        const neighbor=hunk.rows.slice(i+1).find(r=>r[field]!=null);
        visible=!!neighbor && neighbor[field]>=block.start && neighbor[field]<=block.end;
      }
      if(!visible) { if(lastVisible)content+='<div class="hunk-header">⋯ omitted from excerpt</div>'; lastVisible=false;continue; }
      lastVisible=true;
      content+=codeRow(row,file.path,full&&Number(view.line)===row[field]);
      if(!full && row[field]!=null) {
        for(const annotation of block.annotations||[]) if(annotation.line===row[field]) content+=`<div class="annotation">${annotation.title?`<strong>${esc(annotation.title)}</strong>`:''}${esc(annotation.text)}</div>`;
      }
    }
  }
  if(!hunks.length) content=`<pre class="raw-diff">${esc(file.diff||'No textual hunks. This file is empty, binary, or changed metadata only.')}</pre>`;
  return `<div class="diff"><div class="file-label"><span class="file-path">${esc(file.path)}</span><div class="file-actions">${full?'':`<button data-file="${fileKey(file.path)}" data-line="${block.start}" data-side="${side}">Full file diff</button>`}<button data-source="${fileKey(file.path)}" data-line="${block.start||1}" data-side="${side}">Browse source</button><button data-editor="${fileKey(file.path)}" data-line="${block.start||1}" data-side="${side}">Open in VS Code</button></div></div>${content}</div>${full?`<details class="block background"><summary>Raw Git diff · includes file metadata</summary><pre class="raw-diff">${esc(file.diff)}</pre></details>`:`<div class="diff-tools"><span class="subtle">Excerpt · ${esc(side)} lines ${block.start}–${block.end}</span><button data-expand="${index}">${expanded?'Collapse context':'Expand diff context'}</button></div>`}`;
}

function sourceHtml(block) {
  const file=files.get(block.path),side=block.side||'head';
  const rows=(file[side].text||'').split('\n').slice(block.start-1,block.end).map((text,i)=>({text,new:block.start+i}));
  return `<div class="source"><div class="file-label"><span class="file-path">${esc(block.path)} · ${side} · ${block.start}–${block.end}</span><div class="file-actions"><button data-source="${fileKey(block.path)}" data-line="${block.start}" data-side="${side}">Browse source</button></div></div>${rows.map(r=>codeRow(r,file.path,false,side)).join('')}</div>`;
}
function blockHtml(block,index) {
  const heading=block.title?`<h3>${esc(block.title)}</h3>`:'';
  const paragraphs=text=>String(text||'').split('\n\n').map(p=>`<p>${esc(p)}</p>`).join('');
  switch(block.type) {
    case 'text':return `<div class="block">${heading}${paragraphs(block.text)}${refsHtml(block.refs)}</div>`;
    case 'background':return `<details class="block background" data-details="${index}" ${state.expanded[`details:${view.id}:${index}`]?'open':''}><summary>Background · ${esc(block.title)}</summary><div class="background-content">${paragraphs(block.text)}${refsHtml(block.refs)}${block.source?sourceHtml(block.source):''}</div></details>`;
    case 'diff':return `<div class="block">${heading}${block.text?paragraphs(block.text):''}${diffHtml(block,index)}</div>`;
    case 'source':return `<div class="block">${heading}${sourceHtml(block)}</div>`;
    case 'flow':return `<div class="block">${heading}<div class="flow" aria-label="${attr(block.title||'Data flow')}">${block.nodes.map((n,i)=>`${i?'<span class="flow-arrow" aria-hidden="true">→</span>':''}<div class="flow-node"><strong>${esc(n.label)}</strong>${n.detail?`<p>${esc(n.detail)}</p>`:''}${n.ref?refsHtml([n.ref]):''}</div>`).join('')}</div></div>`;
    default:throw new Error(`Unsupported block ${block.type}`);
  }
}

function capturePosition() {
  if(view?.type==='section')state.readingSpot={id:view.id,scroll:window.scrollY};
}
function navigate(next, restore=false) {
  capturePosition(); clearTimeout(scrollTimer); view=next; state.view=next; render();
  const y=restore && next.type==='section'?state.readingSpot?.scroll||0:0;
  window.scrollTo(0,y);
  if(next.line) requestAnimationFrame(()=> {
    const field=next.type==='source'?'code':next.side==='base'?'old':'new';
    const selector=`[data-${field}-line="${Number(next.line)}"]`;
    byId('content').querySelector(selector)?.scrollIntoView({block:'center'});
  });
  persist();
}

function render() {
  rendering=true;progress();
  let html='';
  if(view.type==='section') {
    const index=guide.sections.findIndex(s=>s.id===view.id),section=guide.sections[index];
    html=`<div class="chapter-top"><span>SECTION ${index+1} OF ${guide.sections.length}</span></div><h2>${esc(section.title)}</h2>${section.intro?`<p class="chapter-intro">${esc(section.intro)}</p>`:''}${section.origin?`<div class="origin">${esc(section.origin)}</div>`:''}${isChanged(section)?'<div class="stale">Changed since you looked. Your notes are preserved; mark as seen again after reading.</div>':''}${section.blocks.map((block,i)=>`<section class="report-block" data-block-index="${i}">${blockHtml(block,i)}</section>`).join('')}`;
    if(section.map) html+=`<div class="block">${guide.sections.filter(s=>s.id!==section.id).map(s=>`<div class="map-item"><button data-section="${attr(s.id)}">${esc(s.title)}</button><p>${esc(s.subtitle||'')} · ${s.files.length} files</p></div>`).join('')}</div>`;
    if(section.supporting) html+=`<div class="supporting-index">${section.files.map(p=>`<button data-file="${fileKey(p)}" data-side="${files.get(p).head.exists?'head':'base'}">${esc(p)}</button>`).join('')}</div>`;
    html+=`<details class="notes" data-details="notes" ${state.expanded[`details:${view.id}:notes`]?'open':''}><summary>Private reading notes</summary><textarea id="notes" maxlength="20000" aria-label="Private reading notes" placeholder="Where did I stop? What should I revisit?">${esc(sectionState(section).notes||'')}</textarea></details><div class="chapter-bottom"><button ${index===0?'disabled':''} data-section="${attr(guide.sections[Math.max(0,index-1)].id)}">← Previous section</button><label class="seen-control"><input id="seen" type="checkbox" ${isSeen(section)?'checked':''}> Mark as seen</label><button ${index===guide.sections.length-1?'disabled':''} data-section="${attr(guide.sections[Math.min(guide.sections.length-1,index+1)].id)}">Next section →</button></div>`;
  } else {
    const file=files.get(view.path),side=view.side||'head',lines=file[side].text?.split('\n')||[];
    const revisionLabel=side==='base'?(snapshot.baseLabel||snapshot.base.slice(0,12)):(snapshot.headLabel||snapshot.head.slice(0,12));
    html=`<div class="chapter-top"><button id="return">← Back to walkthrough</button><span>${view.type==='file'?'COMPLETE FILE DIFF':`${side.toUpperCase()} SOURCE`}</span></div><h2 class="file-path">${esc(file.path)}</h2><p class="file-summary">${esc(file.status)}${file.path!==file.oldPath?` · renamed from ${esc(file.oldPath)}`:''} · ${file.context?'Unchanged background source':'All hunks included'} · ${esc(revisionLabel)}</p><div id="editor-target" class="editor-panel">Checking checkout against snapshot…</div>`;
    if(view.type==='file')html+=diffHtml({path:file.path,start:view.line||1,side},0,true);
    else html+=file[side].text===null?'<div class="empty">No text source exists on this side.</div>':`<div class="diff-tools"><span>Line numbers open VS Code</span><button data-file="${fileKey(file.path)}" data-line="${view.line||1}" data-side="${side}">Full file diff</button>${file.base.text!==null&&file.head.text!==null?`<button data-source="${fileKey(file.path)}" data-line="1" data-side="${side==='head'?'base':'head'}">Show ${side==='head'?'base':'head'} source</button>`:''}</div><div class="source">${lines.map((text,i)=>codeRow({text,new:i+1},file.path,i+1===Number(view.line),side)).join('')}</div>`;
  }
  byId('content').innerHTML=html;
  reviewActions?.afterRender();
  if(view.type!=='section') showEditorTarget(view.path,view.side||'head',Number(view.line)||1);
  rendering=false;
}

async function showEditorTarget(path,side,line) {
  const panel=byId('editor-target');
  try {
    const target=await api(`/api/editor?${new URLSearchParams({path,side,line})}`);
    if(!panel?.isConnected)return;
    panel.innerHTML=`<a href="${attr(target.url)}">${esc(target.label)} · line ${line}</a><small>${target.checkout?'This file matches the reviewed revision.':'Your checkout differs. This opens the exact reviewed file in a snapshot folder.'}</small>`;
  }catch(error){if(panel?.isConnected)panel.textContent=error.message;}
}
async function openEditor(path,side,line) {
  try {
    const target=await api(`/api/editor?${new URLSearchParams({path,side,line})}`);
    const link=document.createElement('a');
    link.href=target.url;
    link.click();
  }catch(error){byId('notice').hidden=false;byId('notice').textContent=`Unable to open VS Code: ${error.message}`;}
}

function updateCheckout(info) {
  checkoutInfo=info;
  byId('checkout-toolbar').hidden=!info.pr;
  byId('checkout-label').textContent=info.active?(info.owned?'Code browsing: managed PR worktree':'Code browsing: reused checkout · never removed by this plugin'):'Review finished · guide and notes retained';
  byId('finish-review').hidden=!info.active;
  byId('finish-review').textContent=info.owned?'Finish review & clean up…':'Finish review…';
  byId('reopen-checkout').hidden=info.active;
}

function cleanupItems(items) {
  byId('cleanup-items').innerHTML=items.length?items.map(item=>`<div class="cleanup-item"><strong>${item.removed?'Removed':item.eligible?'Can remove':'Kept'}</strong><code>${esc(item.path||item.review)}</code><p>${esc(item.reason)}</p></div>`).join(''):'<p>No managed worktrees need cleanup.</p>';
}

async function showCleanup(action) {
  cleanupAction=action;
  const finish=action==='finish';
  byId('cleanup-title').textContent=finish?'Finish review & clean up?':'Clean up unused reviews';
  byId('cleanup-description').textContent=finish?'Save and close the review checkout in VS Code first. This ends this viewer’s code-browsing session and removes only clean, unchanged worktrees created for this review. Reused checkouts are kept. The guide, notes, reading progress, and captured source remain available.':'Only clean, unchanged worktrees created by Human Review and not opened for 14 days are eligible. Active reviews and your own checkouts are kept. Nothing is removed automatically.';
  if(finish)byId('cleanup-items').innerHTML=`<div class="cleanup-item"><strong>${checkoutInfo.owned?'Managed checkout':'Reused checkout · will be kept'}</strong><code>${esc(checkoutInfo.path||'No checkout')}</code><p>Ownership, revision, file, and session checks are repeated before removal. Bazel repositories are cleaned before Git removes the worktree.</p></div>`;
  else byId('cleanup-items').textContent='Checking worktrees…';
  byId('confirm-cleanup').hidden=false;
  byId('confirm-cleanup').disabled=!finish;
  byId('confirm-cleanup').textContent=finish?'Finish & clean up':'Remove eligible worktrees';
  byId('cancel-cleanup').textContent='Cancel';
  byId('cleanup-dialog').showModal();
  if(!finish) {
    try {
      const items=await api('/api/cleanup-plan');
      cleanupItems(items);
      byId('confirm-cleanup').disabled=!items.some(item=>item.eligible);
    }catch(error){byId('cleanup-items').textContent=error.message;}
  }
}

document.addEventListener('click',event=> {
  const button=event.target.closest('button');if(!button)return;
  if(button.dataset.section)navigate({type:'section',id:button.dataset.section});
  if(button.dataset.file)navigate({type:'file',path:decodeURIComponent(button.dataset.file),side:button.dataset.side||'head',line:Number(button.dataset.line)||null});
  if(button.dataset.source)navigate({type:'source',path:decodeURIComponent(button.dataset.source),side:button.dataset.side||'head',line:Number(button.dataset.line)||1});
  if(button.dataset.editor)openEditor(decodeURIComponent(button.dataset.editor),button.dataset.side,Number(button.dataset.line));
  if(button.dataset.expand!==undefined){const key=`excerpt:${view.id}:${button.dataset.expand}`;state.expanded[key]=!state.expanded[key];const y=window.scrollY;render();window.scrollTo(0,y);persist();}
  if(button.id==='return'||button.id==='resume'){const spot=state.readingSpot||{id:guide.sections[0].id,scroll:0};navigate({type:'section',id:spot.id},true);}
  if(button.id==='finish-review')showCleanup('finish');
  if(button.id==='cleanup-unused')showCleanup('cleanup');
  if(button.id==='cancel-cleanup')byId('cleanup-dialog').close();
  if(button.id==='confirm-cleanup'){
    button.disabled=true;
    byId('cancel-cleanup').disabled=true;
    byId('cleanup-items').textContent='Checking and cleaning worktrees…';
    api(`/api/${cleanupAction}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:true})})
      .then(result=>{updateCheckout(result.checkout);cleanupItems(result.results);byId('confirm-cleanup').hidden=true;byId('cancel-cleanup').textContent='Close';if(view.type!=='section')render();})
      .catch(error=>{byId('cleanup-items').textContent=error.message;button.disabled=false;})
      .finally(()=>{byId('cancel-cleanup').disabled=false;});
  }
  if(button.id==='reopen-checkout'){
    button.disabled=true;
    api('/api/checkout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:true})})
      .then(result=>{updateCheckout(result.checkout);if(view.type!=='section')render();})
      .catch(error=>{byId('notice').hidden=false;byId('notice').textContent=error.message;})
      .finally(()=>{button.disabled=false;});
  }
});
document.addEventListener('change',event=> {
  if(event.target.id==='seen'){
    const index=guide.sections.findIndex(s=>s.id===view.id),section=guide.sections[index];
    const seen=event.target.checked;
    state.sections[section.id]={...sectionState(section),seen,fingerprint:section.fingerprint};
    if(seen && index+1<guide.sections.length)navigate({type:'section',id:guide.sections[index+1].id});
    else {progress();if(seen)byId('content').querySelector('.stale')?.remove();}
    persist(true);
  }
});
document.addEventListener('input',event=> {
  if(event.target.id==='notes'){state.sections[view.id]={...state.sections[view.id],notes:event.target.value};persist();}
});
document.addEventListener('toggle',event=> {
  const details=event.target;
  if(rendering||!details.isConnected||!(details instanceof HTMLDetailsElement)||!guide)return;
  if(details.id==='file-list')state.fileListOpen=details.open;
  else if(details.dataset.details!==undefined)state.expanded[`details:${view.id}:${details.dataset.details}`]=details.open;
  else return;
  persist();
},true);
window.addEventListener('scroll',()=>{clearTimeout(scrollTimer);scrollTimer=setTimeout(()=>{if(view?.type==='section'){capturePosition();persist();}},350);},{passive:true});
window.addEventListener('pagehide',()=>{if(state){capturePosition();state.view=view;persist(true);}});

(async()=> {
  try {
    const [review,saved,checkout]=await Promise.all([api('/api/review'),api('/api/state'),api('/api/checkout')]);
    snapshot=review.snapshot;guide=review.guide;
    updateCheckout(checkout);
    for(const f of [...snapshot.files,...snapshot.contextFiles])files.set(f.path,f);
    state={sections:{},expanded:{},...saved};
    if(!state.sections||typeof state.sections!=='object')state.sections={};
    if(!state.expanded||typeof state.expanded!=='object')state.expanded={};
    view=state.view||{type:'section',id:guide.sections[0].id};
    if(!['section','file','source'].includes(view.type)||(view.type==='section'&&!guide.sections.some(s=>s.id===view.id))||(view.type!=='section'&&!files.has(view.path)))view={type:'section',id:guide.sections[0].id};
    if(state.readingSpot&&!guide.sections.some(s=>s.id===state.readingSpot.id))state.readingSpot=null;
    document.title=`${guide.title||snapshot.title} · Human Review`;
    byId('title').textContent=guide.title||snapshot.title;
    const meta=snapshot.metadata;
    byId('comparison').textContent=meta?`PR #${meta.number} · ${meta.baseRefName} ← ${meta.headRefName} · ${snapshot.head.slice(0,12)} · ${snapshot.files.length} files`:`${snapshot.mode} · ${snapshot.baseLabel||snapshot.base.slice(0,12)} → ${snapshot.headLabel||snapshot.head.slice(0,12)} · ${snapshot.files.length} files`;
    if(meta){byId('pr-link').hidden=false;byId('pr-link').href=meta.url;}
    byId('file-list').open=!!state.fileListOpen;
    if(guide.notice){byId('notice').textContent=guide.notice;byId('notice').hidden=false;}
    reviewActions=await window.HumanReviewActions({snapshot,guide,api,esc,getView:()=>view,getReadingSpot:()=>state.readingSpot});
    render();if(view.type==='section'&&state.readingSpot?.id===view.id)requestAnimationFrame(()=>window.scrollTo(0,state.readingSpot.scroll||0));
  }catch(error){byId('title').textContent='Unable to open review';byId('content').textContent=error.message;}
})();
