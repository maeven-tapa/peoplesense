(() => {
  'use strict';
  const ns='http://www.w3.org/2000/svg';
  document.querySelectorAll('[data-movement-url]').forEach(button=>{
    const target=document.getElementById(button.dataset.movementTarget);
    if(!target)return;
    const wrapper=button.closest('[data-movement-debug]');
    const hint=wrapper.querySelector('.movement-hint');
    const overlay=document.createElementNS(ns,'svg');
    overlay.classList.add('movement-overlay');overlay.setAttribute('aria-hidden','true');
    target.parentElement.appendChild(overlay);
    let enabled=false,controller=null,timer=null,receivedAt=0;
    function clear(){overlay.replaceChildren();receivedAt=0;}
    function resize(){overlay.style.width=target.clientWidth+'px';overlay.style.height=target.clientHeight+'px';}
    new ResizeObserver(resize).observe(target);resize();
    function stop(){clearTimeout(timer);timer=null;controller?.abort();controller=null;clear();}
    function draw(result){
      clear();
      if(!result.alive||result.camera_mode!=='tripwire')return;
      const w=result.width,h=result.height;
      if(!(w>0&&h>0&&w<=640&&h<=480))return;
      overlay.setAttribute('viewBox',`0 0 ${w} ${h}`);
      // Overview images use object-fit:contain; the editor stretches its own
      // canvas to its intrinsic frame size. Match each preview's geometry.
      overlay.setAttribute('preserveAspectRatio',target.tagName==='IMG'?'xMidYMid meet':'none');
      for(const trail of (result.trails||[]).slice(0,50)){
        const points=trail.points;
        if(!Array.isArray(points)||points.length<2||points.length>64||
          !points.every(p=>Array.isArray(p)&&p.length===2&&p.every(v=>Number.isFinite(v)&&v>=0&&v<=1)))continue;
        const path=document.createElementNS(ns,'polyline');
        path.setAttribute('points',points.map(([x,y])=>`${x*w},${y*h}`).join(' '));
        path.setAttribute('vector-effect','non-scaling-stroke');
        overlay.appendChild(path);
      }
      receivedAt=performance.now();
    }
    async function poll(){
      if(!enabled||document.hidden||wrapper.hidden)return;
      const request=new AbortController();controller=request;
      const timeout=setTimeout(()=>request.abort(),2000);
      try{
        const response=await fetch(button.dataset.movementUrl,{cache:'no-store',signal:request.signal});
        if(!response.ok||!response.headers.get('content-type')?.includes('application/json'))throw new Error();
        const result=await response.json();
        if(controller!==request||!enabled||document.hidden)return;
        wrapper.hidden=result.camera_mode!=='tripwire';
        if(wrapper.hidden){enabled=false;button.setAttribute('aria-pressed','false');stop();return;}
        draw(result);
        hint.textContent=!result.alive?'Start the camera to see movement paths.':
          overlay.childElementCount?'Yellow lines show recent tracked movement.':'Waiting for tracked movement…';
      }catch(error){
        if(controller===request){clear();hint.textContent='Movement preview unavailable. Checking again…';}
      }finally{
        clearTimeout(timeout);
        if(controller===request){controller=null;if(enabled&&!document.hidden&&!wrapper.hidden)timer=setTimeout(poll,250);}
      }
    }
    button.addEventListener('click',()=>{
      enabled=!enabled;button.setAttribute('aria-pressed',String(enabled));stop();
      hint.textContent=enabled?'Waiting for tracked movement…':'Show each person’s recent path in yellow.';
      if(enabled)poll();
    });
    document.addEventListener('visibilitychange',()=>{stop();if(!document.hidden&&enabled)poll();});
    window.addEventListener('pagehide',stop);
    // Clear paths promptly if the preview stops or mode changes between polls.
    document.addEventListener('camera-preview-state',event=>{
      wrapper.hidden=event.detail.camera_mode!=='tripwire';
      if(!event.detail.alive||wrapper.hidden){stop();if(wrapper.hidden){enabled=false;button.setAttribute('aria-pressed','false');}}
      else if(enabled&&!controller&&!timer)poll();
    });
    setInterval(()=>{if(receivedAt&&performance.now()-receivedAt>2000)clear();},500);
  });
})();
