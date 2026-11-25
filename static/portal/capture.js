(() => {
  'use strict';
  document.querySelectorAll('[data-preview-capture]').forEach(form=>{
    const button=form.querySelector('button');
    const feedback=form.closest('.preview-toolbar').querySelector('.capture-feedback');
    let live=false,busy=false;
    document.addEventListener('camera-preview-state',event=>{
      live=Boolean(event.detail.alive&&(event.detail.has_frame??true));
      button.disabled=!live||busy;
      button.title=live?'Save the latest camera frame':'Start the camera to capture a photo';
    });
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(!live||busy)return;
      busy=true;button.disabled=true;feedback.hidden=false;feedback.classList.remove('capture-error');
      feedback.textContent='Saving photo…';
      try{
        const response=await fetch(form.action,{method:'POST',headers:{
          'X-CSRFToken':form.elements.csrfmiddlewaretoken.value,'Accept':'application/json'}});
        if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Sign in again to capture a photo.');
        const result=await response.json();
        if(!response.ok)throw new Error(result.error||'Unable to save the photo.');
        const photo=document.createElement('a');photo.href=result.image_url;photo.target='_blank';photo.rel='noopener';photo.textContent='View photo';photo.className='text-link';
        const gallery=document.createElement('a');gallery.href=result.captures_url;gallery.textContent='All captures';gallery.className='text-link';
        feedback.replaceChildren(document.createTextNode('Photo saved. '),photo,document.createTextNode(' · '),gallery);
      }catch(error){feedback.textContent=error.message;feedback.classList.add('capture-error');}
      finally{busy=false;button.disabled=!live;}
    });
  });
})();
