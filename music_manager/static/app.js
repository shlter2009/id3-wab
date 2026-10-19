'use strict';
const $ = (selector) => document.querySelector(selector);
const state = { jobs: [], filter: 'all', editing: null, candidate: null, loading: false, allowed: [], researching: null, uploadingCover: new Set() };
const statusLabels = {queued:'대기 중', searching:'정보 찾는 중', awaiting:'정보 확인 필요', converting:'MP3 만드는 중', covering:'커버 찾는 중', completed:'정리 완료', failed:'처리 실패'};
let toastTimer;
function toast(message) { $('#toast').textContent=message; $('#toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('#toast').hidden=true,6000); }
function warning(selector, text) { $(selector).textContent=text; $(selector).hidden=!text; }
async function api(path, options={}) {
  const headers = {'X-Requested-With':'ID3Sit', ...options.headers};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type']='application/json';
  const response=await fetch(path,{...options,headers,credentials:'same-origin'});
  const data=await response.json();
  if (!response.ok) {
    if (data.login_required && !$('#login-dialog').open) $('#login-dialog').showModal();
    throw new Error(data.error || '요청을 처리하지 못했습니다.');
  }
  return data;
}
function node(tag, className, text) { const el=document.createElement(tag); if(className)el.className=className; if(text!==undefined)el.textContent=text; return el; }
function action(text, callback, className='row-button') { const button=node('button',className,text); button.addEventListener('click',callback);return button; }
function render() {
  const jobs=state.jobs;
  $('#nav-count').textContent=jobs.length; $('#track-count').textContent=jobs.length;
  $('#stat-total').replaceChildren(document.createTextNode(jobs.length+' '),node('small','','곡'));
  $('#stat-done').replaceChildren(document.createTextNode(jobs.filter(j=>j.status==='completed').length+' '),node('small','','곡'));
  $('#stat-waiting').replaceChildren(document.createTextNode(jobs.filter(j=>j.status==='awaiting').length+' '),node('small','','곡'));
  const filtered=jobs.filter(j=>state.filter==='all'||j.status===state.filter);
  const tbody=$('#tracks');
  // Preserve audio playback while a background poll updates other jobs.
  const players=new Map([...tbody.querySelectorAll('audio')].map(a=>[a.dataset.job,a]));
  const previousRows=new Map([...tbody.children].map(row=>[row.dataset.job,row]));
  const rows=[];
  filtered.forEach((job,index)=>{
    const signature=JSON.stringify(job);
    const previous=previousRows.get(job.id);
    if(previous && previous.dataset.view===signature){rows.push(previous);return;}
    const row=node('tr'); row.dataset.job=job.id;row.dataset.view=signature;
    row.append(node('td','',String(index+1).padStart(2,'0')));
    const infoCell=node('td'); const info=node('div','track-info'); const art=node('div','track-art','♫');
    if(job.cover && !job.noCover) {const img=node('img'); img.alt=''; img.src=job.cover; img.addEventListener('error',()=>{art.textContent='♫';job.noCover=true;});art.replaceChildren(img);}
    const text=node('div','track-text'); const meta=job.metadata||job.hint;
    text.append(node('strong','',meta.title||job.name),node('span','',meta.artist ? `${meta.artist}${meta.album?' · '+meta.album:''}`:job.name));
    info.append(art,text); infoCell.append(info);
    if(job.download){let audio=players.get(job.id);if(!audio){audio=node('audio','preview');audio.controls=true;audio.preload='none';audio.src=job.download+'?play=1';audio.dataset.job=job.id;}infoCell.append(audio);}
    row.append(infoCell);
    const format=node('td');format.append(node('span','format-badge',job.status==='completed'?'MP3':(job.name.split('.').pop()||'AUDIO').slice(0,5).toUpperCase()));row.append(format);
    const status=node('td');status.append(node('span','status-badge '+job.status,(job.status==='completed'?'✓ ':'')+statusLabels[job.status]));
    if(['queued','searching','converting','covering'].includes(job.status)){const wrap=node('div','job-progress');const progress=node('progress');progress.max=1;progress.value=job.progress;progress.setAttribute('aria-label','처리 진행률');wrap.append(progress);status.append(wrap);}
    if(job.error)status.append(node('div','job-error',job.error));
    if(job.duplicate)status.append(node('div','duplicate-note','중복 저장 방지됨 · 태그 갱신'));
    if(job.warning && job.status==='completed')status.append(node('div','job-error',job.warning));
    row.append(status);
    const actions=node('td');
    if(job.status==='awaiting')actions.append(action('정보 확인 →',()=>edit(job.id)));
    if(job.status==='completed'){const a=node('a','row-button download','다운로드 ↓');a.href=job.download;a.setAttribute('download','');actions.append(a,action('정보 수정',()=>reopen(job.id)));}
    if(job.status==='failed')actions.append(action('재시도 ↻',()=>mutate(`/api/jobs/${job.id}/retry`,{method:'POST',body:JSON.stringify({artists:ArtistLibrary.rules()})})));
    if(!['queued','searching','converting','covering'].includes(job.status)){const remove=action('×',()=>removeJob(job),'remove-job');remove.setAttribute('aria-label',job.name+' 삭제');actions.append(remove);}
    row.append(actions);rows.push(row);
  });
  const wanted=new Set(rows);
  [...tbody.children].forEach(row=>{if(!wanted.has(row))row.remove();});
  rows.forEach((row,index)=>{if(tbody.children[index]!==row)tbody.insertBefore(row,tbody.children[index]||null);});
  syncEditor();
  $('#empty-state').hidden=filtered.length>0;
  if(!filtered.length){$('#empty-state h3').textContent=jobs.length?'선택한 상태의 트랙이 없어요':'아직 첫 번째 트랙을 기다리고 있어요';}
}
async function refresh() {
  if(state.loading)return;
  state.loading=true;
  try {
    const data=await api('/api/jobs');state.jobs=data.jobs;state.allowed=data.allowed_hosts;
    if(data.version&&data.version!==document.documentElement.dataset.version){
      $('#update-message').textContent=`새 버전 v${data.version}이 배포됐습니다. 새로고침하여 최신 검색 기능을 사용하세요.`;
      $('#update-notice').hidden=false;
    }
    $('#app-version').title=data.revision?`배포 버전 ${data.revision}`:'';
    $('#allowed-hosts').textContent='허용 호스트: '+(state.allowed.join(', ')||'없음 · 파일 업로드를 이용해 주세요.');render();
  }
  catch(error){if(!$('#login-dialog').open)toast(error.message);}
  finally{state.loading=false;}
}
async function mutate(path,options){try{await api(path,options);await refresh();}catch(error){toast(error.message);}}
async function removeJob(job){if(!confirm(`“${job.name}” 작업과 서버 파일을 삭제할까요? 이미 다운로드한 파일은 유지됩니다.`))return;await mutate(`/api/jobs/${job.id}`,{method:'DELETE'});}
async function upload(files){
  const chosen=Array.from(files);if(!chosen.length)return;
  $('#dropzone').disabled=true;
  let added=0;
  try{
    for(const file of chosen){
      if(file.size>64*1024*1024){toast(`${file.name}: 파일당 최대 64 MB입니다.`);continue;}
      const body=new FormData();body.append('file',file);body.append('artists',JSON.stringify(ArtistLibrary.rules()));
      try{await api('/api/jobs/upload',{method:'POST',body});added++;await refresh();}catch(error){toast(`${file.name}: ${error.message}`);}
    }
    if(added)toast(`${added}개 파일을 추가했습니다. 음악 정보 확인을 기다려 주세요.`);
  }finally{$('#dropzone').disabled=false;$('#file-input').value='';}
}
function openImport(){ $('#import-panel').scrollIntoView({behavior:'smooth',block:'start'});$('#dropzone').focus({preventScroll:true});}
['#import-top','#nav-import','#empty-add'].forEach(s=>$(s).addEventListener('click',openImport));
$('#nav-library').addEventListener('click',()=>$('.library-section').scrollIntoView({behavior:'smooth'}));
$('#dropzone').addEventListener('click',()=>$('#file-input').click());
$('#file-input').addEventListener('change',e=>upload(e.target.files));
['dragenter','dragover'].forEach(event=>$('#dropzone').addEventListener(event,e=>{e.preventDefault();$('#dropzone').classList.add('dragover');}));
['dragleave','drop'].forEach(event=>$('#dropzone').addEventListener(event,e=>{e.preventDefault();$('#dropzone').classList.remove('dragover');}));
$('#dropzone').addEventListener('drop',e=>upload(e.dataTransfer.files));
window.addEventListener('dragover',e=>e.preventDefault());window.addEventListener('drop',e=>e.preventDefault());
$('#url-add').addEventListener('click',async()=>{
  const url=$('#audio-url').value.trim();if(!url){$('#audio-url').focus();return;}
  if(!$('#permission').checked){toast('다운로드·변환 권한 확인란을 선택해 주세요.');return;}
  $('#url-add').disabled=true;
  try{await api('/api/jobs/url',{method:'POST',body:JSON.stringify({url,permission:true,artists:ArtistLibrary.rules()})});$('#audio-url').value='';toast('URL을 추가했습니다. 파일을 가져오고 있습니다.');await refresh();}catch(error){toast(error.message);}finally{$('#url-add').disabled=false;}
});
$('#audio-url').addEventListener('keydown',e=>{if(e.key==='Enter')$('#url-add').click();});
$('#allowed-toggle').addEventListener('click',()=>$('#allowed-hosts').hidden=!$('#allowed-hosts').hidden);
document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{state.filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('selected',b===button));render();}));
function fillMetadata(meta,index){
  state.candidate=index;
  ['title','artist','album','year','genre'].forEach(k=>$('#metadata-form').elements.namedItem(k).value=meta[k]||'');
  document.querySelectorAll('.candidate').forEach((button,i)=>button.classList.toggle('selected',index===i));
  paintArtwork(state.editing);
}
function renderCandidates(job){
  const candidates=$('#candidates');candidates.replaceChildren();
  job.candidates.forEach((meta,i)=>{const button=node('button','candidate');button.append(node('strong','',`${meta.artist} — ${meta.title}`),node('small','',`${meta.album||'앨범 정보 없음'} · ${meta.year||'연도 미상'} · ${meta.duration?meta.duration.toFixed(1)+'초':'길이 미상'} · 검색 점수 ${meta.score}`));button.addEventListener('click',()=>{fillMetadata(meta,i);if(meta.release_id)findCover();});candidates.append(button);});
  if(!job.candidates.length)candidates.append(node('p','no-candidates',job.search_failed?'온라인 검색을 완료하지 못했습니다. 다시 검색하거나 아래에 직접 입력하세요.':'일치하는 결과가 없습니다. 제목과 가수를 고쳐 다시 검색할 수 있어요.'));
}
function edit(id){
  const job=state.jobs.find(j=>j.id===id);if(!job||job.status!=='awaiting')return;
  state.editing=job;state.candidate=null;state.researching=null;warning('#form-error','');
  $('#source-description').textContent=job.name+(job.hint.duration?` · 원본 길이 ${job.hint.duration.toFixed(1)}초`:'');
  paintSearchInfo(job);
  warning('#metadata-warning',job.warning);
  renderCandidates(job);
  fillMetadata(job.hint,null);$('#metadata-dialog').showModal();syncEditor();
}
$('#original-metadata').addEventListener('click',()=>fillMetadata(state.editing.hint,null));
['#close-dialog','#cancel-edit'].forEach(s=>$(s).addEventListener('click',()=>$('#metadata-dialog').close()));
$('#metadata-form').addEventListener('submit',async e=>{
  e.preventDefault();$('#confirm-button').disabled=true;warning('#form-error','');
  try{const metadata=Object.fromEntries(new FormData(e.target));await api(`/api/jobs/${state.editing.id}/confirm`,{method:'POST',body:JSON.stringify({metadata,candidate:state.candidate})});$('#metadata-dialog').close();toast('MP3를 만들고 있습니다. 완료 후 다운로드하세요.');await refresh();}catch(error){warning('#form-error',error.message);}finally{$('#confirm-button').disabled=false;}
});
$('#login-dialog').addEventListener('cancel',e=>e.preventDefault());
$('#login-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/login',{method:'POST',body:JSON.stringify({password:$('#site-password').value})});$('#site-password').value='';$('#login-dialog').close();warning('#login-error','');await refresh();}catch(error){warning('#login-error',error.message);}});
ArtistLibrary.initialize({api,toast});
refresh();setInterval(()=>{if(!document.hidden&&!$('#login-dialog').open)refresh();},2000);

function currentMetadata(){return Object.fromEntries(new FormData($('#metadata-form')));}
function metadataMatches(first,second){return first&&second&&['title','artist','album','year'].every(k=>String((k==='artist'?ArtistLibrary.canonical(first[k]):first[k])||'').trim().normalize('NFKC').toLocaleLowerCase()===String((k==='artist'?ArtistLibrary.canonical(second[k]):second[k])||'').trim().normalize('NFKC').toLocaleLowerCase());}
function paintArtwork(job){
  if(!job)return;
  const artwork=job.artwork||{};
  const compatible=artwork.source==='직접 올린 이미지'||metadataMatches(currentMetadata(),artwork.metadata);
  const show=artwork.available&&compatible&&!artwork.disabled;
  const image=$('#cover-preview');image.hidden=!show;$('#cover-placeholder').hidden=show;
  if(show){const src=`/api/jobs/${job.id}/cover?v=${artwork.version}`;if(image.getAttribute('src')!==src)image.src=src;}
  let description=show?artwork.source:'앨범을 선택해 커버를 찾거나 이미지를 직접 올리세요.';
  if(job.status==='covering')description='앨범 커버를 찾고 있습니다…';
  else if(artwork.disabled)description='커버 없이 저장하도록 선택했습니다.';
  else if(artwork.available&&!compatible)description='곡 정보가 바뀌었습니다. 커버를 다시 찾거나 이미지를 선택해 주세요.';
  else if(artwork.message)description=artwork.message;
  $('#cover-description').textContent=description;
}
function syncEditor(){
  if(!$('#metadata-dialog').open||!state.editing)return;
  const job=state.jobs.find(j=>j.id===state.editing.id)||state.editing;
  state.editing=job;
  paintSearchInfo(job);
  if(state.researching===job.id&&job.status==='awaiting'){
    state.researching=null;state.candidate=null;renderCandidates(job);fillMetadata(job.hint,null);warning('#metadata-warning',job.warning);
  }
  const busy=job.status!=='awaiting'||state.uploadingCover.has(job.id);
  ['#confirm-button','#research-artists','#research-fields','#cover-search','#cover-upload-button','#cover-remove'].forEach(s=>$(s).disabled=busy);
  document.querySelectorAll('.candidate').forEach(button=>button.disabled=busy);
  if(state.researching)warning('#metadata-warning','입력한 정보로 다시 검색하고 있습니다…');
  paintArtwork(job);
}
function paintSearchInfo(job){
  const info=job.search_info||{};
  const parts=[`제목: ${info.title||job.hint.title||'미상'}`,`가수: ${info.artist||job.hint.artist||'미상'}`];
  if(info.album)parts.push(`앨범 힌트: ${info.album}`);
  parts.push(`MusicBrainz · 후보 ${job.candidates.length}개`);
  $('#search-description').textContent=parts.join(' / ');
}
function acceptJob(job){
  state.jobs=state.jobs.map(item=>item.id===job.id?job:item);if(state.editing?.id===job.id)state.editing=job;render();
}
async function research(byFields){
  if(!state.editing)return;
  const job=state.editing;
  const body={artists:ArtistLibrary.rules()};
  if(byFields)body.metadata=currentMetadata();
  warning('#form-error','');
  try{const data=await api(`/api/jobs/${job.id}/research`,{method:'POST',body:JSON.stringify(body)});if(state.editing?.id===job.id){state.researching=job.id;state.candidate=null;}acceptJob(data.job);await refresh();}
  catch(error){warning('#form-error',error.message);}
}
async function reopen(id){
  try{
    const job=state.jobs.find(item=>item.id===id);
    const data=await api(`/api/jobs/${id}/research`,{method:'POST',body:JSON.stringify({artists:ArtistLibrary.rules(),metadata:job.metadata})});
    acceptJob(data.job);toast('곡 정보를 다시 검색합니다. 정보 확인 버튼에서 수정해 주세요.');
  }catch(error){toast(error.message);}
}
async function findCover(){
  if(!state.editing)return;
  warning('#form-error','');
  try{const data=await api(`/api/jobs/${state.editing.id}/artwork/search`,{method:'POST',body:JSON.stringify({metadata:currentMetadata(),candidate:state.candidate})});acceptJob(data.job);await refresh();}
  catch(error){warning('#form-error',error.message);}
}
$('#research-artists').addEventListener('click',()=>research(false));
$('#reload-version').addEventListener('click',()=>window.location.reload());
$('#research-fields').addEventListener('click',()=>research(true));
$('#cover-search').addEventListener('click',findCover);
$('#cover-upload-button').addEventListener('click',()=>$('#cover-file').click());
$('#cover-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file||!state.editing)return;
  const jobId=state.editing.id;
  warning('#form-error','');
  try{
    if(file.size>10*1024*1024)throw new Error('커버 이미지는 최대 10 MB입니다.');
    state.uploadingCover.add(jobId);syncEditor();const body=new FormData();body.append('file',file);
    const data=await api(`/api/jobs/${jobId}/artwork/upload`,{method:'POST',body});acceptJob(data.job);toast('커버 이미지를 추가했습니다.');
  }catch(error){warning('#form-error',error.message);}
  finally{state.uploadingCover.delete(jobId);event.target.value='';syncEditor();}
});
$('#cover-remove').addEventListener('click',async()=>{
  if(!state.editing)return;
  try{const data=await api(`/api/jobs/${state.editing.id}/artwork`,{method:'DELETE'});acceptJob(data.job);}catch(error){warning('#form-error',error.message);}
});
$('#metadata-form').addEventListener('input',()=>paintArtwork(state.editing));
window.addEventListener('artistsapply',async()=>{
  const waiting=state.jobs.filter(j=>j.status==='awaiting');
  if(!waiting.length){toast('목록을 적용할 정보 확인 대기 곡이 없습니다. 새 파일부터 적용됩니다.');return;}
  $('#metadata-dialog').close();state.researching=null;
  let count=0;
  for(const job of waiting){try{await api(`/api/jobs/${job.id}/research`,{method:'POST',body:JSON.stringify({artists:ArtistLibrary.rules()})});count++;}catch(error){toast(error.message);}}
  await refresh();toast(`${count}개 곡에 아티스트 목록을 적용하고 다시 검색합니다.`);
});
