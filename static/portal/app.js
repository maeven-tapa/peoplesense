(() => {
  'use strict';
  const $ = (q, root = document) => root.querySelector(q);
  const $$ = (q, root = document) => [...root.querySelectorAll(q)];
  const number = value => value == null ? '—' : Number(value).toLocaleString('en-US');
  const svgNS = 'http://www.w3.org/2000/svg';
  let toastTimer;
  function toast(message) {
    const element = $('#toast');
    if (!element) return;
    element.textContent = message;
    element.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => element.classList.remove('show'), 4500);
  }
  function openDialog(id) { const dialog = $(id); if (dialog && !dialog.open) dialog.showModal(); }
  $('#search-trigger')?.addEventListener('click', () => openDialog('#search-dialog'));
  $$('[data-open-help]').forEach(button => button.addEventListener('click', () => openDialog('#help-dialog')));
  $$('[data-close-dialog]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  $$('dialog').forEach(dialog => dialog.addEventListener('click', event => {
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  }));
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); openDialog('#search-dialog'); }
    if (event.key === 'Escape') closeSidebar();
  });
  function closeSidebar() { $('#sidebar')?.classList.remove('open'); $('#sidebar-overlay')?.classList.remove('show'); $('#menu-toggle')?.setAttribute('aria-expanded', 'false'); }
  $('#menu-toggle')?.addEventListener('click', () => {
    const opened = $('#sidebar').classList.toggle('open');
    $('#sidebar-overlay').classList.toggle('show', opened);
    $('#menu-toggle').setAttribute('aria-expanded', String(opened));
  });
  $('#sidebar-overlay')?.addEventListener('click', closeSidebar);
  $$('.dismiss-message').forEach(button => button.addEventListener('click', () => button.closest('.message').remove()));
  const cameraThumbnails=$$('[data-camera-thumbnail]');
  if(cameraThumbnails.length)setInterval(()=>{
    if(document.hidden)return;
    cameraThumbnails.filter(img=>!img.hidden).forEach(img=>img.src=img.dataset.cameraThumbnail+'?v='+Date.now());
  },2000);
  const previewStreams=$$('[data-camera-stream]');
  function syncStreams(){
    previewStreams.forEach(img=>{
      if(document.hidden||img.hidden){img.removeAttribute('src');}
      else if(!img.getAttribute('src'))img.src=img.dataset.cameraStream+'?v='+Date.now();
    });
  }
  document.addEventListener('visibilitychange',syncStreams);
  window.addEventListener('pagehide',()=>previewStreams.forEach(img=>img.removeAttribute('src')));
  previewStreams.forEach(img=>img.addEventListener('error',()=>img.removeAttribute('src')));
  setInterval(syncStreams,2000);
  // Activity filters submit together; dashboard periods refresh the chart and metrics.
  const dataElement = $('#dashboard-data');
  if (!dataElement) {
    $('#period-select')?.addEventListener('change', event => event.target.form?.requestSubmit());
    return;
  }
  let data = JSON.parse(dataElement.textContent);
  let chartMode = 'occupancy';
  let fetchController;
  let requestSequence = 0;
  let resizeTimer;
  function svgElement(tag, attributes, parent, text) {
    const element = document.createElementNS(svgNS, tag);
    Object.entries(attributes || {}).forEach(([key,value]) => element.setAttribute(key, value));
    if (text !== undefined) element.textContent = text;
    parent?.appendChild(element);
    return element;
  }
  function renderChart() {
    const container = $('#traffic-chart');
    if (!container) return;
    container.replaceChildren();
    const points = data.points;
    const width = Math.max(container.clientWidth, 260);
    const height = container.clientHeight || 220;
    const pad = {left: 40, right: 18, top: 17, bottom: 30};
    const plotWidth = width-pad.left-pad.right;
    const plotHeight = height-pad.top-pad.bottom;
    const values = points.flatMap(point => chartMode==='occupancy' ? [point.occupancy] : [point.entries,point.exits]);
    const highest = Math.max(10, ...values);
    const step = Math.max(5, Math.ceil(highest/4/10)*10);
    const maximum = Math.ceil(highest/step)*step;
    const x = i => pad.left+i/(points.length-1)*plotWidth;
    const y = v => pad.top+plotHeight-(v/maximum*plotHeight);
    const svg = svgElement('svg', {viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true'}, container);
    const defs = svgElement('defs', {}, svg);
    const gradient = svgElement('linearGradient', {id:'area-gradient',x1:0,y1:0,x2:0,y2:1}, defs);
    svgElement('stop', {offset:'0%','stop-color':'#77b8e7','stop-opacity':'.21'}, gradient);
    svgElement('stop', {offset:'100%','stop-color':'#77b8e7','stop-opacity':'.015'}, gradient);
    for (let value=0; value<=maximum; value+=step) {
      svgElement('line', {x1:pad.left,y1:y(value),x2:width-pad.right,y2:y(value),stroke:'#ecebf4','stroke-width':1,'stroke-dasharray':'3 4'}, svg);
      svgElement('text', {x:pad.left-10,y:y(value)+3,'text-anchor':'end',fill:'#b0b2c1','font-family':'DM Sans, Segoe UI, sans-serif','font-size':8}, svg, number(value));
    }
    const labelCount = width<380 ? 5 : 7;
    const indices = [...new Set(Array.from({length:labelCount}, (_,i)=>Math.round(i*(points.length-1)/(labelCount-1))))];
    indices.forEach(i => svgElement('text', {x:x(i),y:height-8,'text-anchor':i===0?'start':i===points.length-1?'end':'middle',fill:'#a8adbc','font-family':'DM Sans, Segoe UI, sans-serif','font-size':8}, svg, points[i].label));
    function pathFor(field) {
      return points.map((point,i) => `${i===0?'M':'L'}${x(i).toFixed(2)},${y(point[field]).toFixed(2)}`).join(' ');
    }
    if (chartMode==='occupancy') {
      const line = pathFor('occupancy');
      svgElement('path', {d:`${line} L${x(points.length-1)},${y(0)} L${x(0)},${y(0)} Z`,fill:'url(#area-gradient)'}, svg);
      svgElement('path', {d:line,fill:'none',stroke:'#77b8e7','stroke-width':2.4,'stroke-linecap':'round','stroke-linejoin':'round'}, svg);
      const peak = Math.max(...points.map(point=>point.occupancy));
      const peakIndex = points.findIndex(point=>point.occupancy===peak);
      svgElement('circle', {cx:x(peakIndex),cy:y(peak),r:4.5,fill:'#77b8e7',stroke:'white','stroke-width':2}, svg);
      $('#peak-label').textContent = `Peak: ${number(peak)} people · ${points[peakIndex].label}`;
    } else {
      svgElement('path', {d:pathFor('entries'),fill:'none',stroke:'#77b8e7','stroke-width':2.3,'stroke-linecap':'round','stroke-linejoin':'round'}, svg);
      svgElement('path', {d:pathFor('exits'),fill:'none',stroke:'#6db4a5','stroke-width':2,'stroke-dasharray':'4 3','stroke-linecap':'round'}, svg);
      $('#peak-label').textContent = `${number(data.entries)} entries · ${number(data.exits)} exits`;
    }
    const tooltip = document.createElement('div');
    tooltip.className = 'chart-tooltip';
    tooltip.hidden = true;
    container.appendChild(tooltip);
    const crosshair = svgElement('line', {x1:0,x2:0,y1:pad.top,y2:height-pad.bottom,stroke:'#a1c5df','stroke-width':1,'stroke-dasharray':'3 3',opacity:0}, svg);
    const hitArea = svgElement('rect', {x:pad.left,y:pad.top,width:plotWidth,height:plotHeight,fill:'transparent'}, svg);
    hitArea.addEventListener('pointermove', event => {
      const rect = svg.getBoundingClientRect();
      const position = (event.clientX-rect.left)/rect.width*width;
      const i = Math.min(points.length-1,Math.max(0,Math.round((position-pad.left)/plotWidth*(points.length-1))));
      const point = points[i];
      tooltip.hidden = false;
      tooltip.textContent = chartMode==='occupancy' ? `${point.label} · ${number(point.occupancy)} people` : `${point.label} · ${number(point.entries)} in / ${number(point.exits)} out`;
      tooltip.style.left = `${Math.max(0,Math.min(container.clientWidth-tooltip.offsetWidth,x(i)-tooltip.offsetWidth/2))}px`;
      tooltip.style.top = `${Math.max(0,y(chartMode==='occupancy'?point.occupancy:point.entries)-35)}px`;
      crosshair.setAttribute('x1',x(i));crosshair.setAttribute('x2',x(i));crosshair.setAttribute('opacity',1);
    });
    hitArea.addEventListener('pointerleave', () => {tooltip.hidden=true;crosshair.setAttribute('opacity',0);});
    container.setAttribute('aria-label', `${chartMode==='occupancy'?(data.camera_mode==='surveillance'?'Visible people':'Occupancy'):'Entries and exits'} chart. ${points.map(p=>`${p.label}: ${chartMode==='occupancy'?`${p.occupancy} people`:`${p.entries} entries, ${p.exits} exits`}`).join('; ')}`);
  }
  function renderDonut() {
    const normal = $('#donut-normal');
    const elevated = $('#donut-alert');
    if (!normal || !elevated) return;
    const circumference = 2*Math.PI*79;
    const gap = 7;
    const alertPart = data.readings ? Math.max(data.elevated>0?10:0,circumference*data.elevated/data.readings) : 0;
    const normalPart = data.readings ? circumference-alertPart-(alertPart?gap*2:gap) : 0;
    normal.style.strokeDasharray = `${Math.max(0,normalPart)} ${circumference}`;
    normal.style.strokeDashoffset = '0';
    elevated.style.strokeDasharray = `${alertPart} ${circumference}`;
    elevated.style.strokeDashoffset = `${-normalPart-gap}`;
    normal.style.opacity = data.readings && data.normal_readings ? '1' : '0';
    elevated.style.opacity = data.elevated ? '1' : '0';
  }
  function renderDeviceBars() {
    const container = $('#device-bars');
    if (!container) return;
    container.replaceChildren();
    const maximum = Math.max(1,...data.devices.map(device=>device.entries));
    if (!data.devices.length) { const empty = document.createElement('p'); empty.className='muted'; empty.textContent='No camera data available.';container.appendChild(empty); }
    data.devices.forEach(device=> {
      const row=document.createElement('div');row.className='device-bar';
      const label=document.createElement('div');label.className='device-bar-label';
      const name=document.createElement('a');name.href=`/devices/${device.id}/`;name.textContent=device.name;
      const count=document.createElement('strong');count.textContent=number(device.entries);
      label.append(name,count);
      const track=document.createElement('div');track.className='device-bar-track';
      const bar=document.createElement('span');bar.style.width=`${device.entries/maximum*100}%`;
      track.appendChild(bar);row.append(label,track);container.appendChild(row);
    });
  }
  function renderAll() {
    $$('[data-value]').forEach(element => {
      const key = element.dataset.value;
      element.textContent = key==='average_stay_display' ? data[key] : key==='temperature' ? data[key]==null?'—':Number(data[key]).toFixed(1) : number(data[key]);
    });
    if ($('#capacity-bar')) $('#capacity-bar').style.width=`${Math.min(100,data.capacity_percent)}%`;
    const livePreview=$('#overview-live-preview');
    if(livePreview){
      livePreview.hidden=!data.preview_alive;
      document.dispatchEvent(new CustomEvent('camera-preview-state',{detail:{alive:data.preview_alive,camera_mode:data.camera_mode}}));
      $('#overview-preview-empty').hidden=data.preview_alive;
      const status=$('#overview-preview-status');status.textContent=data.preview_alive?'Live':'Stopped';status.className='status-pill '+(data.preview_alive?'online':'offline');
      syncStreams();
    }
    $$('[data-alert-count]').forEach(element=>{element.textContent=data.alert_count;element.hidden=data.alert_count===0;});
    $$('[data-notification-dot]').forEach(element=>element.hidden=data.alert_count===0);
    $$('[data-notification-link]').forEach(element=>element.setAttribute('aria-label',`${data.alert_count} unread alerts`));
    $$('[data-online-devices]').forEach(element=>element.textContent=data.online);
    $$('[data-total-devices]').forEach(element=>element.textContent=data.devices_total);
    const trend=$('[data-entry-trend]');
    if(trend){trend.hidden=data.entry_change===null;trend.style.color=data.entry_change<0?'#c39176':'#4eae8b';}
    const comparison=$('[data-comparison-label]');
    if(comparison)comparison.textContent=data.entry_change===null?'Entries during selected period':'vs. previous period';
    renderChart();renderDonut();renderDeviceBars();
    // This markup is rendered and escaped by Django, including camera names.
    if ($('#recent-events') && data.recent_html) $('#recent-events').innerHTML=data.recent_html;
    const deviceFilter=new URL(location.href).searchParams.get('device');
    $$('[data-export]').forEach(link=>link.href=`/reports/export/?period=${encodeURIComponent(data.period)}`+(deviceFilter?`&device=${encodeURIComponent(deviceFilter)}`:''));
    const label = $('.subtle-label');if(label)label.textContent=data.period==='24h'?'24 HOURS':data.period.toUpperCase();
  }
  $$('[data-chart-mode]').forEach(button=>button.addEventListener('click',()=>{
    chartMode=button.dataset.chartMode;
    $$('[data-chart-mode]').forEach(item=>{item.classList.toggle('selected',item===button);item.setAttribute('aria-pressed',String(item===button));});
    const legend=$('#chart-legend');legend.replaceChildren();
    (chartMode==='occupancy'?[[data.camera_mode==='surveillance'?'Visible people':'Occupancy','#65b1e8']]:[['Entries','#65b1e8'],['Exits','#6db4a5']]).forEach(([label,color])=>{
      const span=document.createElement('span');const dot=document.createElement('i');dot.className='legend-dot';dot.style.background=color;span.append(dot,document.createTextNode(label));legend.appendChild(span);
    });
    renderChart();
  }));
  async function refresh(showFeedback=false) {
    fetchController?.abort();fetchController=new AbortController();
    const sequence=++requestSequence;
    const period=$('#period-select')?.value||data.period;
    try {
      const deviceFilter=new URL(location.href).searchParams.get('device');
      const response=await fetch(`/api/dashboard/?period=${encodeURIComponent(period)}`+(deviceFilter?`&device=${encodeURIComponent(deviceFilter)}`:''),{signal:fetchController.signal,headers:{Accept:'application/json'}});
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Unable to refresh. Please check your connection or sign in again.');
      const next=await response.json();
      if(sequence!==requestSequence)return;
      if(next.time_zone!==data.time_zone||($('#overview-camera')&&next.camera_mode!==data.camera_mode)){location.reload();return;}
      data=next;renderAll();
      if(showFeedback) {
        const url=new URL(location.href);url.searchParams.set('period',data.period);history.replaceState(null,'',url);
        toast(`Showing the last ${data.period==='24h'?'24 hours':data.period==='7d'?'7 days':'30 days'}.`);
      }
    } catch(error) {if(error.name!=='AbortError')toast(error.message);}
  }
  $('#period-select')?.addEventListener('change',()=>refresh(true));
  $('#overview-camera')?.addEventListener('change',event=>{
    const url=new URL(location.href);url.searchParams.set('device',event.target.value);url.searchParams.set('period',$('#period-select')?.value||data.period);location.assign(url);
  });
  window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(renderChart,120);});
  setInterval(()=>{if(!document.hidden)refresh();},$('#overview-camera')?5000:30000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  renderAll();
})();
