(() => {
  'use strict';
  const root=document.getElementById('pan-tilt-control');if(!root)return;
  const form=root.querySelector('form');const buttons=[...form.querySelectorAll('button')];
  const message=root.querySelector('#hardware-message');const status=root.querySelector('#hardware-status');
  let ready=false,moving=false,busy=false,polling=false,commandError='';
  function render(result){
    ready=result.ready;moving=result.moving;
    status.textContent=ready?moving?'Moving':'Connected':result.enabled?'Unavailable':'Pi required';
    status.className='status-pill '+(ready?'online':'offline');
    message.textContent=commandError||result.error||(moving?'Camera moving. Wait before the next press.':result.message);
    root.querySelector('#pan-angle').textContent=result.pan==null?'—':result.pan+'°';
    root.querySelector('#tilt-angle').textContent=result.tilt==null?'—':result.tilt+'°';
    root.querySelector('#ambient-temperature').textContent=result.temperature==null?'—':result.temperature.toFixed(1)+' °C';
    root.querySelector('#ambient-humidity').textContent=result.humidity==null?'—':result.humidity.toFixed(0)+'%';
    root.querySelector('#sensor-message').textContent=result.sensor_error||'DHT11 · ambient air readings';
    buttons.forEach(button=>button.disabled=!ready||moving||busy);
  }
  async function refresh(){
    if(polling||busy||document.hidden)return;polling=true;
    try{
      const response=await fetch(root.dataset.statusUrl,{cache:'no-store'});
      if(!response.ok||!response.headers.get('content-type')?.includes('application/json'))throw new Error('Hardware status unavailable.');
      render(await response.json());
    }catch(error){ready=false;buttons.forEach(button=>button.disabled=true);message.textContent=error.message;}
    finally{polling=false;}
  }
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!ready||moving||busy||!event.submitter)return;
    busy=true;commandError='';buttons.forEach(button=>button.disabled=true);
    try{
      const response=await fetch(form.action,{method:'POST',headers:{'X-CSRFToken':form.elements.csrfmiddlewaretoken.value,
        'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body:new URLSearchParams({direction:event.submitter.value})});
      if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Sign in again to rotate the camera.');
      const result=await response.json();if(!response.ok)throw new Error(result.error||'Camera rotation failed.');render(result);
    }catch(error){commandError=error.message;message.textContent=commandError;}
    finally{busy=false;await refresh();}
  });
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  refresh();setInterval(refresh,1000);
})();
