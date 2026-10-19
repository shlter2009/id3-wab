'use strict';
window.ArtistLibrary = (() => {
  const key = 'id3-sit-artists-v1';
  let rules = [], editing = null, api, toast;
  const el = id => document.getElementById(id);
  const element = (tag, className, text) => {const item=document.createElement(tag);item.className=className;if(text!==undefined)item.textContent=text;return item;};
  function error(text) {el('artist-error').textContent=text;el('artist-error').hidden=!text;}
  function reset() {editing=null;el('artist-form').reset();el('artist-save').textContent='아티스트 추가';el('artist-cancel-edit').hidden=true;}
  function load() {
    try {const value=JSON.parse(localStorage.getItem(key)||'[]');if(!Array.isArray(value)||value.some(a=>!a||typeof a.name!=='string'||!Array.isArray(a.aliases)||a.aliases.some(v=>typeof v!=='string')))throw new Error();rules=value;}
    catch(e){error('저장된 목록을 읽지 못했습니다. 브라우저 저장 설정을 확인하거나 백업 목록을 가져오세요.');rules=[];}
  }
  function render() {
    el('artist-count').textContent=rules.length;
    el('artist-chips').replaceChildren(...(rules.length?rules.slice(0,6).map(a=>element('span','artist-chip',a.name)):[element('span','artist-empty','아직 등록된 가수가 없어요. 예: Drake / 드레이크')]));
    const list=el('artists-list');list.replaceChildren();
    if(!rules.length)list.append(element('p','no-candidates','정식 이름과 자주 쓰는 별칭을 추가하세요.'));
    rules.forEach((artist,index)=>{
      const row=element('div','artist-row');const text=element('div','artist-row-text');text.append(element('strong','',artist.name),element('span','',artist.aliases.join(' · ')||'등록한 별칭 없음'));
      const edit=element('button','row-button','수정');edit.setAttribute('aria-label',artist.name+' 수정');edit.onclick=()=>{editing=index;el('artist-name').value=artist.name;el('artist-aliases').value=artist.aliases.join('\n');el('artist-save').textContent='변경 저장';el('artist-cancel-edit').hidden=false;el('artist-name').focus();};
      const remove=element('button','remove-job','×');remove.setAttribute('aria-label',artist.name+' 삭제');remove.onclick=async()=>{if(!confirm(`“${artist.name}” 아티스트를 목록에서 삭제할까요?`))return;try{await save(rules.filter((_,i)=>i!==index));reset();toast('아티스트를 삭제했습니다.');}catch(e){error(e.message);}};
      row.append(text,edit,remove);list.append(row);
    });
    el('artist-suggestions').replaceChildren(...rules.map(a=>{const option=document.createElement('option');option.value=a.name;return option;}));
  }
  async function save(next) {
    const data=await api('/api/artists/validate',{method:'POST',body:JSON.stringify({artists:next})});
    try{localStorage.setItem(key,JSON.stringify(data.artists));}catch(e){throw new Error('목록을 저장하지 못했습니다. 브라우저의 사이트 저장 공간을 확인하세요.');}
    rules=data.artists;error('');render();
  }
  function open() {render();el('artists-dialog').showModal();}
  return {
    canonical:value=>{const key=String(value||'').trim().normalize('NFKC').toLocaleLowerCase();const artist=rules.find(a=>[a.name,...a.aliases].some(v=>v.trim().normalize('NFKC').toLocaleLowerCase()===key));return artist?artist.name:value;},
    rules:()=>rules.map(a=>({name:a.name,aliases:[...a.aliases]})),
    initialize(options){
      api=options.api;toast=options.toast;load();render();
      ['manage-artists','nav-artists'].forEach(id=>el(id).onclick=open);
      el('close-artists').onclick=()=>el('artists-dialog').close();
      el('artist-cancel-edit').onclick=reset;
      el('artist-form').onsubmit=async event=>{
        event.preventDefault();el('artist-save').disabled=true;error('');
        try{const item={name:el('artist-name').value.trim(),aliases:el('artist-aliases').value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean)};const next=[...rules];if(editing===null)next.push(item);else next[editing]=item;await save(next);reset();toast('아티스트 목록을 저장했습니다. 기존 곡은 목록을 다시 적용할 수 있습니다.');}
        catch(e){error(e.message);}finally{el('artist-save').disabled=false;}
      };
      el('artist-export').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({version:1,artists:rules},null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=url;link.download='id3-sit-artists.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
      el('artist-import-button').onclick=()=>el('artist-import').click();
      el('artist-import').onchange=async event=>{const file=event.target.files[0];if(!file)return;try{if(file.size>2*1024*1024)throw new Error('목록 파일은 최대 2 MB입니다.');const value=JSON.parse(await file.text());const next=Array.isArray(value)?value:value.artists;if(!Array.isArray(next))throw new Error('아티스트 목록 JSON 파일을 선택하세요.');if(rules.length&&!confirm('현재 목록을 가져온 목록으로 바꿀까요? 필요하면 먼저 내보내세요.'))return;await save(next);reset();toast('아티스트 목록을 가져왔습니다.');}catch(e){error(e instanceof SyntaxError?'JSON 목록 파일을 읽지 못했습니다.':e.message);}finally{event.target.value='';}};
      el('artist-apply').onclick=()=>{el('artists-dialog').close();window.dispatchEvent(new Event('artistsapply'));};
      window.addEventListener('storage',event=>{if(event.key===key||event.key===null){load();reset();render();}});
    }
  };
})();
