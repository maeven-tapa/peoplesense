import csv
import json
from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Q, Sum, Max
from django.http import HttpResponse, JsonResponse, FileResponse, Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.debug import sensitive_post_parameters

from .forms import TimeZoneForm, WifiSetupForm, AdministratorPasswordForm, FactoryResetForm, TripwireForm, CameraModeForm, CameraSelectionForm
from .models import Alert, Detection, Device, PortalSettings, PersonStay, AreaObservation
from . import appliance
from .forms import ApplianceCameraForm

def portal_access(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.user.is_staff:
            return view(request, *args, **kwargs)
        if request.user.is_authenticated:
            return HttpResponse('Staff access required.', status=403)
        return login_required(view)(request, *args, **kwargs)
    return wrapped

def selected_period(request):
    period = request.GET.get('period', '24h')
    if period not in ['24h', '7d', '30d']:
        period = '24h'
    return period, timezone.now() - timedelta(days={'24h': 1, '7d': 7, '30d': 30}[period])

def device_filter(request):
    value = request.GET.get('device', '')
    if not value:
        return None
    if not value.isascii() or not value.isdigit() or len(value)>19 or not 0<int(value)<=9223372036854775807:
        return 0  # An invalid filter yields an empty result, never a server error.
    return int(value)

def filtered_events(request):
    period, start = selected_period(request)
    events = appliance.events().filter(timestamp__gte=start, timestamp__lte=timezone.now()).select_related('device')
    device = device_filter(request)
    if device is not None:
        events = events.filter(device_id=device)
    return period, start, events

def summary(request):
    period, start, events = filtered_events(request)
    now = timezone.now()
    config = PortalSettings.load()
    totals = {row['direction']: row['total'] or 0 for row in events.values('direction').annotate(total=Sum('count'))}
    previous = appliance.events().filter(timestamp__gte=start - (now-start), timestamp__lt=start)
    device_id = device_filter(request)
    cumulative = appliance.events().filter(timestamp__lte=now)
    devices_qs = appliance.cameras()
    if device_id is not None:
        previous = previous.filter(device_id=device_id)
        cumulative = cumulative.filter(device_id=device_id)
        devices_qs = devices_qs.filter(pk=device_id)
    prior_entries = previous.filter(direction='in').aggregate(total=Sum('count'))['total'] or 0
    cum_totals = {row['direction']: row['total'] or 0 for row in cumulative.values('direction').annotate(total=Sum('count'))}
    occupancy = max(0, cum_totals.get('in', 0)-cum_totals.get('out', 0))
    entries = totals.get('in', 0)
    exits = totals.get('out', 0)
    avg_temp = events.aggregate(avg=Avg('temperature'))['avg']
    temp_events = events.exclude(temperature=None)
    elevated = temp_events.filter(temperature__gte=config.temperature_threshold).count()
    readings = temp_events.count()
    bucket_count = {'24h': 24, '7d': 7, '30d': 30}[period]
    duration = (now-start).total_seconds()/bucket_count
    points = [{'label': timezone.localtime(start+timedelta(seconds=i*duration)).strftime('%H:%M' if period=='24h' else '%b %d'),
               'entries': 0, 'exits': 0, 'occupancy': 0, 'temperature': None} for i in range(bucket_count)]
    earlier = cumulative.filter(timestamp__lt=start)
    earlier_totals = {r['direction']: r['total'] or 0 for r in earlier.values('direction').annotate(total=Sum('count'))}
    running = earlier_totals.get('in', 0)-earlier_totals.get('out', 0)
    temperatures = [[] for _ in points]
    for event in events.order_by('timestamp'):
        i = min(bucket_count-1, max(0, int((event.timestamp-start).total_seconds()/duration)))
        if event.direction == 'in':
            points[i]['entries'] += event.count
        elif event.direction == 'out':
            points[i]['exits'] += event.count
        if event.temperature is not None:
            temperatures[i].append(float(event.temperature))
    for i, point in enumerate(points):
        running += point['entries']-point['exits']
        point['occupancy'] = max(0,running)
        if temperatures[i]:
            point['temperature'] = round(sum(temperatures[i])/len(temperatures[i]), 1)
    device_rows = []
    selected_camera = devices_qs.first()
    camera_mode = selected_camera.camera_mode if selected_camera else 'tripwire'
    current_people = selected_camera.visible_tracks if selected_camera and selected_camera.worker_alive and selected_camera.worker_status=='running' else 0
    average_stay = None
    stay_count = 0
    if camera_mode=='surveillance':
        stays = PersonStay.objects.filter(device=selected_camera,last_seen_at__gte=start,last_seen_at__lte=now)
        stay_metrics = stays.aggregate(seconds=Avg('duration_seconds'))
        average_stay = round(stay_metrics['seconds'],1) if stay_metrics['seconds'] is not None else None
        stay_count = stays.count()
        # Surveillance history uses observed people, never cumulative crossings.
        for point in points:point['occupancy']=0
        observations = AreaObservation.objects.filter(device=selected_camera,timestamp__gte=start,timestamp__lte=now)
        for i,point in enumerate(points):
            bucket_start=start+timedelta(seconds=i*duration)
            bucket_end=start+timedelta(seconds=(i+1)*duration)
            point['occupancy']=observations.filter(timestamp__gte=bucket_start,timestamp__lt=bucket_end).aggregate(peak=Max('count'))['peak'] or 0
    device_entries = {row['device_id']:row['n'] for row in events.filter(direction='in').values('device_id').annotate(n=Sum('count'))}
    for device in devices_qs:
        count = device_entries.get(device.pk,0)
        device_rows.append({'id': device.pk, 'name': device.name, 'location': device.location, 'camera_mode':device.camera_mode,
                            'status': device.effective_status, 'entries': count})
    return {'period': period, 'occupancy': occupancy, 'capacity': config.capacity,
            'camera_mode':camera_mode,'selected_device':selected_camera.pk if selected_camera else None,
            'current_people':current_people,'average_stay_seconds':average_stay,'stay_count':stay_count,
            'average_stay_display':format_stay(average_stay),
            'preview_alive':bool(selected_camera and selected_camera.worker_alive and selected_camera.worker_status=='running'),
            'capacity_percent': round(100*occupancy/config.capacity, 1), 'entries': entries, 'exits': exits,
            'entry_change': round((entries-prior_entries)/prior_entries*100, 1) if prior_entries else None,
            'temperature': round(float(avg_temp), 1) if avg_temp is not None else None,
            'elevated': elevated, 'readings': readings, 'normal_readings': readings-elevated,
            'threshold': float(config.temperature_threshold), 'online': sum(d['status']=='online' for d in device_rows),
            'devices_total': len(device_rows), 'devices': device_rows, 'points': points,
            'time_zone':timezone.get_current_timezone_name(), 'updated_at': timezone.localtime(now).isoformat(), 'demo': settings.DEMO_MODE,
            'alert_count': appliance.alerts().filter(acknowledged=False).count(),
            'recent_html': render_to_string('portal/event_table.html', {'events': events[:6], 'building': config}, request=request)}

def format_stay(seconds):
    if seconds is None:return '—'
    seconds=round(seconds)
    hours,remainder=divmod(seconds,3600)
    minutes,seconds=divmod(remainder,60)
    return f'{hours}h {minutes}m' if hours else f'{minutes}m {seconds}s' if minutes else f'{seconds}s'

@portal_access
def dashboard(request):
    _, _, events = filtered_events(request)
    data = summary(request)
    return render(request, 'portal/dashboard.html', {'page': 'dashboard', 'title': 'Overview', 'data': data,
        'devices': appliance.cameras(), 'selected_camera':appliance.cameras().filter(pk=data['selected_device']).first(),
        'recent': events[:6], 'recent_alerts': appliance.alerts().filter(acknowledged=False)[:3]})

@portal_access
def dashboard_api(request):
    return JsonResponse(summary(request))

@portal_access
def devices(request):
    return redirect('device-detail',pk=PortalSettings.load().active_device_id)

@portal_access
def device_detail(request, pk):
    device = get_object_or_404(appliance.cameras(), pk=pk)
    return render(request, 'portal/device_detail.html', _device_detail_context(device))


def _device_detail_context(device, selection_form=None):
    from vision.service import camera_state
    from hardware.service import status as hardware_status
    return {'page': 'devices', 'title': device.name, 'device': device,
        'recent': device.detections.all()[:12], 'entries': device.detections.filter(direction='in').aggregate(n=Sum('count'))['n'] or 0,
        'selection_form': selection_form or CameraSelectionForm(initial={'camera_source':device.camera_source if device.source_type == 'usb' else '0'}),
        'tripwire_form': TripwireForm(instance=device), 'mode_form': CameraModeForm(instance=device), 'camera_state': camera_state(device), 'hardware_state': hardware_status(),
        'tripwire_data': {'ax':device.line_ax,'ay':device.line_ay,'bx':device.line_bx,'by':device.line_by,'entry_side':device.entry_side,'tolerance':device.line_tolerance}}


@portal_access
@require_POST
def camera_select(request, pk):
    device = get_object_or_404(appliance.cameras(), pk=pk)
    form = CameraSelectionForm(request.POST)
    if not form.is_valid():
        response = render(request, 'portal/device_detail.html', _device_detail_context(device, form))
        response['Cache-Control'] = 'private, no-store'
        return response
    from django.db.models import F
    from vision.service import stop_camera
    source = form.cleaned_data['camera_source']
    with transaction.atomic():
        # Serialize with camera configuration and snapshot persistence before
        # checking the source; saving the current choice never stops the feed.
        Device.objects.filter(pk=pk).update(config_version=F('config_version'))
        device.refresh_from_db()
        changed = device.source_type != 'usb' or device.camera_source != source
        if changed:
            stop_camera(pk)
            Device.objects.filter(pk=pk).update(camera_source=source, source_type='usb', temperature_enabled=False,
                config_version=F('config_version') + 1, last_crossing_direction='', last_crossing_at=None,
                line_tracking_status='reposition' if device.camera_mode == Device.Mode.TRIPWIRE else 'waiting',
                line_tracking_error='Camera changed. Position and save the counting line before counting.' if device.camera_mode == Device.Mode.TRIPWIRE else '')
            PortalSettings.objects.filter(pk=1).update(line_configured=False)
    if changed:
        messages.success(request, 'Camera selected. Start the new preview.' +
            (' Position and save its counting line to resume counting.' if device.camera_mode == Device.Mode.TRIPWIRE else ''))
    else:
        messages.success(request, 'This USB camera is already selected.')
    return redirect('device-detail', pk=pk)

@portal_access
def device_edit(request, pk=None):
    device = get_object_or_404(appliance.cameras(), pk=pk)
    original_source=device.camera_source
    form = ApplianceCameraForm(request.POST or None, instance=device)
    if request.method == 'POST' and form.is_valid():
        device = form.save(commit=False)
        if pk:
            from django.db.models import F
            device.config_version = F('config_version')+1
            from vision.service import stop_camera
            if original_source!=device.camera_source:
                stop_camera(pk)
                PortalSettings.objects.filter(pk=1).update(setup_stage=3,line_configured=False)
            device.save(update_fields=[*form.Meta.fields,'source_type','temperature_enabled','config_version'])
        else:
            device.save()
        messages.success(request, f'{device.name} has been saved.')
        return redirect('setup') if PortalSettings.load().setup_stage<4 else redirect('device-detail', pk=device.pk)
    return render(request, 'portal/form.html', {'page': 'devices', 'title': 'Edit camera' if pk else 'Add camera',
        'form': form, 'form_title': 'Camera configuration', 'form_description': 'Choose a connected USB webcam. Open camera details to position its counting line.', 'back_url': '/devices/'})

@portal_access
@require_POST
def camera_mode_save(request,pk):
    device = get_object_or_404(appliance.cameras(),pk=pk)
    form = CameraModeForm(request.POST,instance=device)
    if form.is_valid():
        from django.db.models import F
        camera = form.save(commit=False)
        camera.config_version = F('config_version')+1
        camera.last_crossing_direction=''
        camera.last_crossing_at=None
        if camera.camera_mode == Device.Mode.TRIPWIRE and not PortalSettings.load().line_configured:
            camera.line_tracking_status = 'reposition'
            camera.line_tracking_error = 'Position and save the counting line before counting.'
        elif camera.camera_mode == Device.Mode.SURVEILLANCE:
            camera.line_tracking_status = 'waiting'
            camera.line_tracking_error = ''
        camera.save(update_fields=[*form.Meta.fields,'config_version','last_crossing_direction','last_crossing_at',
                                   'line_tracking_status','line_tracking_error'])
        messages.success(request,f'{camera.get_camera_mode_display()} mode saved.')
    else:
        messages.error(request,'Camera mode was not saved. '+ ' '.join(str(message) for errors in form.errors.values() for message in errors))
    return redirect('setup-step',step='line') if PortalSettings.load().setup_stage<4 and not request.session.get('setup_deferred') else redirect('device-detail',pk=pk)

@portal_access
def captures(request):
    events = appliance.events().filter(has_snapshot=True).select_related('device')
    return render(request,'portal/captures.html',{'page':'captures','title':'Camera captures',
        'captures':Paginator(events,20).get_page(request.GET.get('page'))})

@portal_access
def capture_image(request,event_id):
    event = get_object_or_404(appliance.events(),event_id=event_id,has_snapshot=True)
    from vision.snapshots import snapshot_path
    try:
        response = FileResponse(snapshot_path(event.event_id).open('rb'),content_type='image/jpeg')
    except FileNotFoundError:
        raise Http404('Capture file is unavailable.')
    response['Cache-Control']='private, no-store'
    response['X-Content-Type-Options']='nosniff'
    return response

@portal_access
@require_POST
def tripwire_save(request,pk):
    device = get_object_or_404(appliance.cameras(),pk=pk)
    form = TripwireForm(request.POST,instance=device)
    if form.is_valid():
        from django.db.models import F
        camera = form.save(commit=False)
        camera.config_version = F('config_version')+1
        camera.line_tracking_status='waiting'
        camera.line_tracking_error=''
        camera.last_crossing_direction=''
        camera.last_crossing_at=None
        camera.save(update_fields=[*form.Meta.fields,'config_version','line_tracking_status','line_tracking_error','last_crossing_direction','last_crossing_at'])
        PortalSettings.objects.filter(pk=1).update(line_configured=True)
        messages.success(request,'Counting line saved. Existing visible tracks will establish a new baseline.')
        return redirect('setup-step',step='line') if PortalSettings.load().setup_stage<4 and not request.session.get('setup_deferred') else redirect('device-detail',pk=pk)
    messages.error(request,'Line was not saved. '+ ' '.join(str(message) for errors in form.errors.values() for message in errors))
    return redirect('setup-step',step='line') if PortalSettings.load().setup_stage<4 and not request.session.get('setup_deferred') else redirect('device-detail',pk=pk)

@portal_access
@require_POST
def camera_start(request,pk):
    device = get_object_or_404(appliance.cameras(),pk=pk)
    from vision.service import CameraError, start_camera
    try:
        pid = start_camera(device.pk)
        return JsonResponse({'started':True,'pid':pid})
    except CameraError as error:
        return JsonResponse({'error':str(error)},status=400)

@portal_access
@require_POST
def camera_stop(request,pk):
    get_object_or_404(appliance.cameras(),pk=pk)
    from vision.service import stop_camera
    stop_camera(pk)
    return JsonResponse({'stopped':True})

@portal_access
def camera_status(request,pk):
    device = get_object_or_404(appliance.cameras(),pk=pk)
    from vision.service import camera_state
    result = camera_state(device)
    result['entries'] = device.detections.filter(direction='in').aggregate(n=Sum('count'))['n'] or 0
    result['exits'] = device.detections.filter(direction='out').aggregate(n=Sum('count'))['n'] or 0
    response = JsonResponse(result)
    response['Cache-Control'] = 'no-store'
    return response

@portal_access
def camera_hardware_status(request, pk):
    get_object_or_404(appliance.cameras(), pk=pk)
    from hardware.service import status
    return JsonResponse(status(), headers={'Cache-Control':'private, no-store'})

@portal_access
@require_POST
def camera_rotate(request, pk):
    device = get_object_or_404(appliance.cameras(), pk=pk)
    from hardware.service import HardwareError, move
    try:
        return JsonResponse(move(device,request.POST.get('direction','')),headers={'Cache-Control':'private, no-store'})
    except HardwareError as error:
        return JsonResponse({'error':str(error)},status=409,headers={'Cache-Control':'private, no-store'})

@portal_access
@require_POST
def camera_capture(request, pk):
    device = get_object_or_404(appliance.cameras(), pk=pk)
    from portal.events import EventConflict
    from vision.preview import PreviewBuffer, preview_path
    from vision.snapshots import save_manual_snapshot
    if not device.worker_alive or device.worker_status != 'running':
        response = JsonResponse({'error': 'Start the camera before capturing a photo.'}, status=409)
    else:
        try:
            reader = PreviewBuffer(preview_path(settings.CAMERA_RUNTIME_DIR, pk, device.worker_token))
            try: frame = reader.read()
            finally: reader.close()
            if not frame:
                raise EventConflict('The live frame is not ready yet. Try again in a moment.')
            event = save_manual_snapshot(device, frame[1])
            response = JsonResponse({'saved': True, 'image_url': reverse('capture-image', args=[event.event_id]),
                'captures_url': reverse('captures')})
        except (FileNotFoundError, ValueError) as error:
            message = str(error) if isinstance(error, EventConflict) else 'The live frame is not ready yet. Try again in a moment.'
            response = JsonResponse({'error': message}, status=409)
        except OSError:
            response = JsonResponse({'error': 'Unable to save the photo. Check capture-folder permissions and disk space.'}, status=503)
    response['Cache-Control'] = 'private, no-store'
    return response

@portal_access
def camera_movement(request, pk):
    device = get_object_or_404(appliance.cameras(), pk=pk)
    from vision.movement import read_movement
    response = JsonResponse(read_movement(settings.CAMERA_RUNTIME_DIR, device))
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response

@portal_access
def camera_frame(request,pk):
    device = get_object_or_404(appliance.cameras(),pk=pk)
    from vision.service import frame_path
    if not device.worker_alive:
        return HttpResponse(status=204)
    from vision.preview import PreviewBuffer, preview_path
    try:
        reader = PreviewBuffer(preview_path(settings.CAMERA_RUNTIME_DIR, pk, device.worker_token))
        try: frame = reader.read()
        finally: reader.close()
        if frame: return HttpResponse(frame[1], content_type='image/jpeg', headers={'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})
    except (FileNotFoundError, ValueError):
        pass
    try:
        response = HttpResponse(frame_path(pk).read_bytes(),content_type='image/jpeg')
    except FileNotFoundError:
        return HttpResponse(status=204)
    response['Cache-Control'] = 'no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response

@portal_access
def camera_stream(request, pk):
    device = get_object_or_404(appliance.cameras(), pk=pk)
    if not device.worker_alive:
        return HttpResponse(status=204)
    from vision.preview import stream_frames
    response = StreamingHttpResponse(stream_frames(pk, device.worker_token, settings.CAMERA_RUNTIME_DIR, device.preview_fps), content_type='multipart/x-mixed-replace; boundary=frame')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    response['X-Accel-Buffering'] = 'no'
    return response

@portal_access
def activity(request):
    period, _, events = filtered_events(request)
    query = request.GET.get('q', '').strip()
    if query:
        events = events.filter(Q(device__name__icontains=query)|Q(device__location__icontains=query))
    direction = request.GET.get('direction', 'all')
    if direction in ['in', 'out', 'presence']:
        events = events.filter(direction=direction)
    paginator = Paginator(events, 20)
    return render(request, 'portal/activity.html', {'page': 'activity', 'title': 'Detection activity',
        'events': paginator.get_page(request.GET.get('page')), 'devices': appliance.cameras(), 'period': period,
        'query': query, 'direction': direction, 'selected_device': request.GET.get('device', '')})

@portal_access
def analytics(request):
    return render(request, 'portal/analytics.html', {'page': 'analytics', 'title': 'Traffic analytics', 'data': summary(request), 'devices': appliance.cameras()})

@portal_access
def alerts(request):
    show = request.GET.get('show', 'open')
    items = appliance.alerts().select_related('device')
    if show != 'all':
        items = items.filter(acknowledged=show=='resolved')
    return render(request, 'portal/alerts.html', {'page': 'alerts', 'title': 'Alerts & notifications', 'alerts': items, 'show': show})

@portal_access
@require_POST
def acknowledge(request, pk):
    alert = get_object_or_404(appliance.alerts(), pk=pk)
    alert.acknowledged = True
    alert.save(update_fields=['acknowledged'])
    messages.success(request, 'Alert marked as resolved.')
    return redirect('alerts')

@portal_access
@sensitive_post_parameters('old_password','new_password1','new_password2','wifi_ap_password')
def portal_settings(request):
    profile=PortalSettings.load()
    action=request.POST.get('action') if request.method=='POST' else None
    wifi_form=WifiSetupForm(request.POST if action=='wifi' else None,instance=profile)
    timezone_form=TimeZoneForm(request.POST if action=='timezone' else None,instance=profile)
    password_form=AdministratorPasswordForm(request.user,request.POST if action=='password' else None)
    forms={'wifi':wifi_form,'timezone':timezone_form,'password':password_form}
    status=200
    if request.method=='POST':
        if action not in forms:
            messages.error(request,'Choose the settings section you want to save.')
            status=400
        elif forms[action].is_valid():
            if action=='password':
                from django.contrib.auth import update_session_auth_hash
                user=password_form.save()
                update_session_auth_hash(request,user)
                PortalSettings.objects.filter(pk=1,setup_stage=0).update(setup_stage=1)
                messages.success(request,'Administrator password updated. You are still signed in.')
            elif action=='wifi':
                wifi_form.save()
                from django.conf import settings
                messages.success(request,'Wi-Fi saved. The Pi hotspot will update in 8 seconds; reconnect and open http://10.0.0.1/.' if settings.HOTSPOT_ENABLED else 'Wi-Fi AP configuration saved.')
            else:
                profile=timezone_form.save(commit=False)
                profile.save(update_fields=['time_zone'])
                messages.success(request,'Time zone updated.')
            return redirect('settings')
    response=render(request,'portal/settings.html',{'page':'settings','title':'Device settings',
        'wifi_form':wifi_form,'timezone_form':timezone_form,'password_form':password_form,
        'reset_form':FactoryResetForm(request.user)},status=status)
    response['Cache-Control']='private, no-store'
    return response

@portal_access
@require_POST
@sensitive_post_parameters('reset_password')
def reset_device(request):
    from .reset import ResetError, factory_reset
    from .reset_lock import ResetBusy
    form = FactoryResetForm(request.user, request.POST)
    status = 200
    if form.is_valid():
        try:
            factory_reset()
        except (ResetError, ResetBusy) as error:
            form.add_error(None, str(error))
            status = 503
        else:
            # All prior sessions were erased. Do not save a new authenticated
            # session or put old queued messages back into a fresh one.
            request.session.flush()
            list(messages.get_messages(request))
            response = redirect('/?reset=complete')
            response['Cache-Control'] = 'private, no-store'
            return response
    profile = PortalSettings.load()
    response = render(request, 'portal/settings.html', {'page':'settings', 'title':'Device settings',
        'wifi_form':WifiSetupForm(instance=profile), 'timezone_form':TimeZoneForm(instance=profile),
        'password_form':AdministratorPasswordForm(request.user), 'reset_form':form}, status=status)
    response['Cache-Control'] = 'private, no-store'
    return response

@portal_access
@require_GET
def hotspot_status(request):
    from networking.service import status
    response = JsonResponse(status())
    response['Cache-Control'] = 'private, no-store'
    return response

@portal_access
def export_report(request):
    period, _, events = filtered_events(request)
    query = request.GET.get('q', '').strip()
    if query:
        events = events.filter(Q(device__name__icontains=query)|Q(device__location__icontains=query))
    if request.GET.get('direction') in Detection.Direction.values:
        events = events.filter(direction=request.GET['direction'])
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="peoplesense-{period}-{timezone.localdate().isoformat()}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Event ID', f'Timestamp ({timezone.get_current_timezone_name()})', 'Camera', 'Location', 'Direction', 'People', 'Temperature (C)', 'Track ID'])
    def safe(value):
        value = str(value)
        return "'"+value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value
    for event in events.iterator():
        writer.writerow([str(event.event_id), timezone.localtime(event.timestamp).isoformat(), safe(event.device.name),
                         safe(event.device.location), event.get_direction_display(), event.count, event.temperature or '',safe(event.track_id)])
    return response

