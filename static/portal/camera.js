(() => {
  'use strict';
  const root=document.querySelector('#camera-control');
  const canvas=document.querySelector('#tripwire-canvas');
  if(!root||!canvas)return;
  const context=canvas.getContext('2d');
  const initial=JSON.parse(document.querySelector('#tripwire-data').textContent);
  const state={...initial};
  const form=document.querySelector('#tripwire-form');
  const status=document.querySelector('#worker-status');
  const error=document.querySelector('#camera-error');
  const editorStatus=document.querySelector('#line-editor-status');
  const alignmentStatus=document.querySelector('#alignment-status');
  const modeForm=document.querySelector('#camera-mode-form');
  let savedMode=root.dataset.mode,crossingDirection=null;
  let image=null,dragging=null,dragOrigin=null,nextPoint='a',drawing=false,dirty=false,alive=false,busy=false,previewTarget=30,streamConnected=false,lastDraw=0,actionError='';
  const clamp=value=>Math.max(0,Math.min(1,value));
  function syncInputs() {
    for(const [key,name] of [['ax','line_ax'],['ay','line_ay'],['bx','line_bx'],['by','line_by']])form.elements[name].value=state[key].toFixed(4);
  }
  function draw() {
    const w=canvas.width,h=canvas.height;
    const scale=w/(canvas.clientWidth||w);
    context.fillStyle='#242838';context.fillRect(0,0,w,h);
    if(image?.naturalWidth)context.drawImage(image,0,0,w,h);
    else {
      context.fillStyle='#9297aa';context.font=`${14*scale}px sans-serif`;context.textAlign='center';
      context.fillText('Start camera to see the live preview',w/2,h/2-50*scale);
      context.font=`${9*scale}px sans-serif`;context.fillText(savedMode==='surveillance'?'Photos are saved only when people are detected.':'Position the counting line and save.',w/2,h/2-30*scale);
    }
    if(savedMode==='surveillance')return;
    const lineColor=!dirty&&crossingDirection==='in'?'#007bff':!dirty&&crossingDirection==='out'?'#ef4444':'#b4c6d7';
    const a=[state.ax*w,state.ay*h],b=[state.bx*w,state.by*h];
    const dx=state.bx-state.ax,dy=state.by-state.ay,length=Math.hypot(dx,dy)||1;
    const nx=-dy/length,ny=dx/length,t=state.tolerance;
    context.beginPath();context.moveTo(a[0]+nx*t*w,a[1]+ny*t*h);context.lineTo(b[0]+nx*t*w,b[1]+ny*t*h);
    context.lineTo(b[0]-nx*t*w,b[1]-ny*t*h);context.lineTo(a[0]-nx*t*w,a[1]-ny*t*h);context.closePath();
    context.fillStyle=lineColor+'30';context.fill();
    context.beginPath();context.moveTo(...a);context.lineTo(...b);context.strokeStyle=lineColor;context.lineWidth=3*scale;context.stroke();
    for(const [point,label] of [[a,'A'],[b,'B']]) {
      context.beginPath();context.arc(...point,6*scale,0,Math.PI*2);context.fillStyle=lineColor;context.fill();context.strokeStyle='white';context.lineWidth=1.5*scale;context.stroke();
      context.font=`bold ${11*scale}px sans-serif`;context.textAlign='center';context.fillStyle='white';context.fillText(label,point[0],point[1]-12*scale);
    }
    const sign=state.entry_side==='positive'?1:-1;
    const center=[(a[0]+b[0])/2,(a[1]+b[1])/2];
    const normal=[nx*w,ny*h],norm=Math.hypot(...normal)||1;
    const arrow=[normal[0]/norm*sign,normal[1]/norm*sign];
    const from=[center[0]-arrow[0]*15*scale,center[1]-arrow[1]*15*scale],to=[center[0]+arrow[0]*40*scale,center[1]+arrow[1]*40*scale];
    context.beginPath();context.moveTo(...from);context.lineTo(...to);context.strokeStyle='#6adba9';context.lineWidth=2*scale;context.stroke();
    const angle=Math.atan2(arrow[1],arrow[0]);context.beginPath();context.moveTo(...to);
    context.lineTo(to[0]-9*scale*Math.cos(angle-.45),to[1]-9*scale*Math.sin(angle-.45));context.moveTo(...to);
    context.lineTo(to[0]-9*scale*Math.cos(angle+.45),to[1]-9*scale*Math.sin(angle+.45));context.stroke();
    context.fillStyle='#92ecc3';context.font=`bold ${12*scale}px sans-serif`;context.fillText('IN',to[0]+arrow[0]*16*scale,to[1]+arrow[1]*16*scale);
  }
  function location(event) {
    const rect=canvas.getBoundingClientRect();
    return {x:clamp((event.clientX-rect.left)/rect.width),y:clamp((event.clientY-rect.top)/rect.height)};
  }
  canvas.addEventListener('pointerdown',event=>{
    if(savedMode!=='tripwire')return;
    dirty=true;
    event.preventDefault();canvas.setPointerCapture(event.pointerId);
    const p=location(event),rect=canvas.getBoundingClientRect();
    const distance=which=>Math.hypot((p.x-state[which+'x'])*rect.width,(p.y-state[which+'y'])*rect.height);
    const dx=(state.bx-state.ax)*rect.width,dy=(state.by-state.ay)*rect.height;
    const fraction=Math.max(0,Math.min(1,((p.x-state.ax)*rect.width*dx+(p.y-state.ay)*rect.height*dy)/(dx*dx+dy*dy||1)));
    const lineDistance=Math.hypot((p.x-state.ax)*rect.width-fraction*dx,(p.y-state.ay)*rect.height-fraction*dy);
    dragging=drawing?nextPoint:distance('a')<24?'a':distance('b')<24?'b':lineDistance<18?'line':nextPoint;
    if(dragging==='line')dragOrigin={...p,ax:state.ax,ay:state.ay,bx:state.bx,by:state.by};
    else {
      nextPoint=dragging==='a'?'b':'a';state[dragging+'x']=p.x;state[dragging+'y']=p.y;
      if(drawing&&dragging==='b'){drawing=false;document.querySelector('#draw-line').setAttribute('aria-pressed','false');}
    }
    syncInputs();draw();editorStatus.textContent=dragging==='line'?'Moving the whole line. Save to apply.':`Point ${dragging.toUpperCase()} positioned. Save to apply the line.`;
  });
  canvas.addEventListener('pointermove',event=>{
    if(!dragging)return;const p=location(event);
    if(dragging==='line') {
      const dx=Math.max(-Math.min(dragOrigin.ax,dragOrigin.bx),Math.min(1-Math.max(dragOrigin.ax,dragOrigin.bx),p.x-dragOrigin.x));
      const dy=Math.max(-Math.min(dragOrigin.ay,dragOrigin.by),Math.min(1-Math.max(dragOrigin.ay,dragOrigin.by),p.y-dragOrigin.y));
      Object.assign(state,{ax:dragOrigin.ax+dx,ay:dragOrigin.ay+dy,bx:dragOrigin.bx+dx,by:dragOrigin.by+dy});
    }else{state[dragging+'x']=p.x;state[dragging+'y']=p.y;}
    syncInputs();draw();
  });
  const release=()=>{dragging=null;dragOrigin=null;};canvas.addEventListener('pointerup',release);canvas.addEventListener('pointercancel',release);
  canvas.addEventListener('keydown',event=>{
    if(savedMode!=='tripwire')return;
    if(!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key))return;
    dirty=true;
    event.preventDefault();const endpoint=event.shiftKey?'b':'a';
    const axis=['ArrowLeft','ArrowRight'].includes(event.key)?'x':'y';
    const sign=['ArrowLeft','ArrowUp'].includes(event.key)?-1:1;
    if(event.ctrlKey||event.metaKey){
      const delta=Math.max(-Math.min(state['a'+axis],state['b'+axis]),Math.min(1-Math.max(state['a'+axis],state['b'+axis]),sign*.01));
      state['a'+axis]+=delta;state['b'+axis]+=delta;
    }else{state[endpoint+axis]=clamp(state[endpoint+axis]+sign*.01);}
    syncInputs();draw();editorStatus.textContent='Line adjusted. Save to apply.';
  });
  form.addEventListener('change',()=>{dirty=true;editorStatus.textContent='Unsaved changes. Save to apply and align the line with this view.';});
  modeForm.elements.camera_mode.addEventListener('change',()=>{
    const surveillance=modeForm.elements.camera_mode.value==='surveillance';
    document.querySelector('#capture-interval-field').hidden=!surveillance;
    document.querySelector('#tripwire-settings').hidden=surveillance;
    document.querySelector('#mode-description').textContent=(surveillance?'Surveillance saves photos only while people are detected.':'Tripwire counts confirmed IN and OUT crossings.')+' Save camera mode to apply your selection.';
  });
  document.querySelector('#draw-line').addEventListener('click',()=>{dirty=true;drawing=true;nextPoint='a';document.querySelector('#draw-line').setAttribute('aria-pressed','true');editorStatus.textContent='Click the preview for A, then B, to draw a new line.';canvas.focus();});
  form.elements.entry_side.addEventListener('change',event=>{state.entry_side=event.target.value;draw();});
  form.elements.line_tolerance.addEventListener('input',event=>{const value=Number(event.target.value);if(Number.isFinite(value)&&value>=.005&&value<=.15){state.tolerance=value;draw();}});
  document.querySelector('#reset-line').addEventListener('click',()=>{dirty=true;Object.assign(state,{ax:.1,ay:.5,bx:.9,by:.5});nextPoint='a';drawing=false;document.querySelector('#draw-line').setAttribute('aria-pressed','false');syncInputs();draw();editorStatus.textContent='Default line restored. Save to apply.';});
  for(const id of ['start-camera-form','stop-camera-form']) {
    const control=document.querySelector('#'+id);
    control.addEventListener('submit',async event=>{
      event.preventDefault();const button=control.querySelector('button');button.disabled=true;actionError='';error.textContent='';
      try {
        const response=await fetch(control.action,{method:'POST',headers:{'X-CSRFToken':control.elements.csrfmiddlewaretoken.value,'Accept':'application/json'}});
        if(!response.headers.get('content-type')?.includes('application/json'))throw new Error('Sign in again to manage the camera.');
        const result=await response.json();if(!response.ok)throw new Error(result.error||'Unable to change camera state.');
        if(id==='stop-camera-form'){alive=false;stopStream();draw();}
        await refresh();
      }catch(exception){actionError=exception.message;error.textContent=actionError;}finally{button.disabled=id==='start-camera-form'?alive:!alive;}
    });
  }
  function stopStream() {
    if(image){image.onload=null;image.onerror=null;image.removeAttribute('src');}
    image=null;streamConnected=false;
    document.dispatchEvent(new CustomEvent('camera-preview-state',{detail:{alive:false,camera_mode:savedMode}}));
  }
  function startStream() {
    if(streamConnected||!alive||document.hidden)return;
    image=new Image();streamConnected=true;
    image.onerror=()=>{stopStream();draw();};
    image.src=root.dataset.streamUrl+'?v='+Date.now();
  }
  function renderPreview(now) {
    if(!document.hidden&&image?.naturalWidth&&now-lastDraw>=980/previewTarget){
      if(canvas.width!==image.naturalWidth||canvas.height!==image.naturalHeight){canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;}
      draw();lastDraw=now;
    }
    requestAnimationFrame(renderPreview);
  }
  document.addEventListener('visibilitychange',()=>{
    if(document.hidden)stopStream();else{startStream();refresh();}
  });
  window.addEventListener('pagehide',stopStream);
  async function refresh() {
    if(busy||document.hidden)return;busy=true;
    try {
      const response=await fetch(root.dataset.statusUrl,{cache:'no-store'});
      if(!response.ok||!response.headers.get('content-type')?.includes('application/json'))throw new Error('Unable to refresh camera status. Check your connection or sign in.');
      const result=await response.json();alive=result.alive;previewTarget=result.preview_target_fps||30;
      savedMode=result.camera_mode;crossingDirection=alive?result.last_crossing?.direction:null;
      document.dispatchEvent(new CustomEvent('camera-preview-state',{detail:result}));
      if(!dirty&&result.line){Object.assign(state,result.line);syncInputs();}
      draw();
      const messages={reposition:'Camera rotated. Reposition the counting line and save to resume counting.',locked:'Doorway aligned. The line follows small camera movements.',moving:'Camera moving or settling — counting is paused.',lost:result.line_tracking_error||'Doorway alignment lost — counting is paused. Reposition the line and save.',fixed:'Line fixed in the image. Drag it after repositioning the camera.',waiting:'Aligning the doorway — counting is paused.'};
      alignmentStatus.textContent=alive?(messages[result.line_tracking_status]||messages.waiting):'Start the camera, check the line position, then save to align it with the doorway.';
      if(dirty)alignmentStatus.textContent+=' Your draft line is unsaved; the camera still uses the saved line.';
      alignmentStatus.classList.toggle('alignment-warning',alive&&['moving','lost','waiting','reposition'].includes(result.line_tracking_status));
      status.textContent=result.status[0].toUpperCase()+result.status.slice(1);
      status.className='status-pill '+(result.status==='running'?'online':result.status==='error'?'warning':'offline');
      document.querySelector('#camera-entries').textContent=result.entries.toLocaleString();
      document.querySelector('#camera-exits').textContent=result.exits.toLocaleString();
      document.querySelector('#camera-tracks').textContent=result.visible_tracks;
      document.querySelector('#camera-fps').textContent=result.fps.toFixed(1);
      document.querySelector('#start-camera-form button').disabled=alive;
      document.querySelector('#stop-camera-form button').disabled=!alive;
      error.textContent=result.error||actionError;
      document.querySelector('#camera-preview-fps').textContent=result.preview_fps.toFixed(1);
      if(result.has_frame)startStream();
      else if(!alive){stopStream();draw();}
    }catch(exception){error.textContent=exception.message;}finally{busy=false;}
  }
  window.addEventListener('resize',draw);
  syncInputs();draw();requestAnimationFrame(renderPreview);refresh();setInterval(refresh,1000);
})();
