(() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function init(root) {
    const input=root.querySelector('[data-ticket-file-input]'), list=root.querySelector('[data-ticket-upload-list]'), err=root.querySelector('[data-ticket-upload-error]');
    const form=root.closest('form'); if(!input||!form) return;
    let quota=root.dataset.quota ? Number(root.dataset.quota) : Infinity, uploaded=[];
    const endpoint=root.dataset.uploadEndpoint, draft=root.dataset.draftToken || '';
    const setErr = msg => { err.textContent=msg||''; err.hidden=!msg; };
    const render=()=>{ list.innerHTML=uploaded.map(x=>`<div class="ticket-upload-item"><span>📎 <strong>${esc(x.name)}</strong> <small>${(x.size/1024/1024).toFixed(2)} MB</small></span><button type="button" class="ghost" data-remove-id="${x.id}">×</button><input type="hidden" name="attachment_ids" value="${x.id}"></div>`).join(''); };
    async function upload(file){
      if(file.size>15*1024*1024) throw new Error(`${file.name}: maximum size is 15 MB`);
      if(uploaded.length>=quota) throw new Error('This ticket has reached the 5 customer attachment limit.');
      const fd=new FormData(); fd.append('file',file); if(draft) fd.append('draft_token',draft);
      const r=await fetch(endpoint,{method:'POST',body:fd,credentials:'same-origin'}); const d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.error||'Upload failed');
      uploaded.push({id:d.id,name:d.name,size:d.size}); render();
    }
    input.addEventListener('change', async()=>{ setErr(''); input.disabled=true; try{ for(const f of input.files){ await upload(f); } }catch(e){setErr(e.message)} finally{input.value='';input.disabled=false;} });
    list.addEventListener('click',async e=>{ const b=e.target.closest('[data-remove-id]'); if(!b)return; const id=Number(b.dataset.removeId); try{const r=await fetch(endpoint+'/'+id,{method:'DELETE',credentials:'same-origin'}); if(!r.ok)throw 0; uploaded=uploaded.filter(x=>x.id!==id);render();}catch(_){setErr('Could not remove that pending attachment.')}});
    form.addEventListener('submit',e=>{ if(input.disabled){e.preventDefault();setErr('Please wait for attachments to finish uploading.');} });
  }
  document.querySelectorAll('[data-ticket-uploader]').forEach(init);
})();
