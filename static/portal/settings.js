(() => {
  'use strict';
  const checkbox=document.querySelector('#id_ap_password_enabled');
  const password=document.querySelector('#id_wifi_ap_password');
  if(checkbox&&password){
    const field=password.closest('.form-field');
    function update(){field.hidden=!checkbox.checked;password.disabled=!checkbox.checked;}
    checkbox.addEventListener('change',update);
    update();
  }
  const panel=document.querySelector('[data-hotspot-status-url]');
  if(!panel)return;
  let busy=false;
  async function refresh(){
    if(document.hidden||busy)return;
    busy=true;
    try{
      const response=await fetch(panel.dataset.hotspotStatusUrl,{cache:'no-store',headers:{Accept:'application/json'}});
      if(!response.ok)return;
      const state=await response.json();
      const label=panel.querySelector('[data-hotspot-label]');
      label.textContent=state.label;
      label.classList.toggle('online',state.active);
      label.classList.toggle('offline',!state.active);
      const error=panel.querySelector('[data-hotspot-error]');
      error.textContent=state.error;
      error.hidden=!state.error;
    }catch(_){/* Wi-Fi may disconnect while the AP changes. Keep its address visible. */}
    finally{busy=false;}
  }
  refresh();
  setInterval(refresh,3000);
  document.addEventListener('visibilitychange',refresh);
})();
