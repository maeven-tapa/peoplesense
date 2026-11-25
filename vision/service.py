"""Camera lifecycle: one lease per camera, no camera access on page loads."""
import importlib.util
import os
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.db.models import Q, F
from django.utils import timezone
from portal.models import Device, PortalSettings, PersonStay
from .model_store import model_path, verify_model

class CameraError(ValueError):
    pass

def frame_path(pk):
    return Path(settings.CAMERA_RUNTIME_DIR) / f'camera-{pk}.jpg'

@transaction.atomic
def claim_camera(pk):
    # Camera selection must finish before a worker reads its source and pending
    # line state; otherwise a concurrent start can clear the reposition pause.
    Device.objects.filter(pk=pk).update(config_version=F('config_version'))
    active=PortalSettings.load().active_device_id
    if active is not None and active!=pk:
        raise CameraError('This portal supports only its configured USB camera.')
    device = Device.objects.get(pk=pk)
    if device.source_type=='external' or not device.camera_source:
        raise CameraError('Configure a USB, network, or video-file camera first.')
    runtime = None if device.detection_model=='yolox_nano' else ('ai_edge_litert' if settings.VISION_BACKEND == 'litert' else 'mediapipe')
    dependencies = ['cv2', 'numpy', 'scipy'] + ([runtime] if runtime else [])
    if not all(importlib.util.find_spec(name) for name in dependencies):
        raise CameraError('Install camera dependencies: python -m pip install -r requirements.txt')
    try:
        verify_model(settings.VISION_MODEL_DIR,device.detection_model)
    except (ValueError, FileNotFoundError, KeyError) as error:
        raise CameraError(str(error)) from error
    token = uuid.uuid4()
    stale = timezone.now()-timedelta(seconds=30)
    changed = Device.objects.filter(pk=pk).filter(Q(worker_token=None)|Q(worker_last_seen__lt=stale)|Q(worker_last_seen=None)).update(
        worker_token=token,worker_pid=0,worker_last_seen=timezone.now(),worker_status='starting',worker_error='',worker_fps=0,visible_tracks=0,
        line_tracking_status='reposition' if device.line_tracking_status == 'reposition' else 'waiting',
        line_tracking_error=device.line_tracking_error if device.line_tracking_status == 'reposition' else '',last_crossing_direction='',last_crossing_at=None)
    if not changed:
        raise CameraError('This camera worker is already running.')
    PersonStay.objects.filter(device_id=pk,ended=False).update(ended=True)
    frame_path(pk).unlink(missing_ok=True)
    for old in Path(settings.CAMERA_RUNTIME_DIR).glob(f'camera-{int(pk)}-*'):
        if old.suffix not in {'.preview', '.movement'}:
            continue
        try: old.unlink(missing_ok=True)
        except PermissionError: pass
    return token

def start_camera(pk):
    from portal.reset_lock import device_access
    with device_access():
        return _start_camera(pk)

def _start_camera(pk):
    token = claim_camera(pk)
    directory = Path(settings.CAMERA_RUNTIME_DIR)
    directory.mkdir(parents=True,exist_ok=True)
    try:
        log_directory = Path(settings.CAMERA_LOG_DIR)
        log_directory.mkdir(parents=True, exist_ok=True)
        with (log_directory/f'camera-{pk}.log').open('ab') as log:
            args = [sys.executable,'-u',str(settings.BASE_DIR/'manage.py'),'run_camera','--device',str(pk),'--lease',str(token)]
            process = subprocess.Popen(args,cwd=settings.BASE_DIR,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,start_new_session=os.name!='nt')
        return process.pid
    except Exception:
        Device.objects.filter(pk=pk,worker_token=token).update(worker_token=None,worker_status='error',worker_error='Unable to start the camera process.')
        raise CameraError('Unable to start the camera process.')

def stop_camera(pk):
    # Revoking the lease prevents further events, even while capture is blocked.
    Device.objects.filter(pk=pk).update(worker_token=None,worker_status='stopped',worker_fps=0,visible_tracks=0,status='offline')
    PersonStay.objects.filter(device_id=pk,ended=False).update(ended=True)
    frame_path(pk).unlink(missing_ok=True)

def camera_state(device):
    alive = device.worker_alive
    preview = None
    if alive:
        from .preview import PreviewBuffer, preview_path
        try:
            reader = PreviewBuffer(preview_path(settings.CAMERA_RUNTIME_DIR, device.pk, device.worker_token))
            try: preview = reader.read()
            finally: reader.close()
        except (FileNotFoundError, ValueError): pass
    return {'status': device.worker_status if alive or device.worker_status in ['error','finished','stopped'] else 'stopped',
            'alive': alive, 'fps': round(device.worker_fps,1) if alive else 0,
            'visible_tracks': device.visible_tracks if alive else 0, 'error': device.worker_error,
            'has_frame': alive and (bool(preview) or frame_path(device.pk).is_file()),
            'preview_fps': round(preview[2],1) if preview else 0, 'preview_target_fps': device.preview_fps,
            'camera_mode': device.camera_mode, 'capture_interval': device.capture_interval,
            'last_crossing': {'direction':device.last_crossing_direction,'at':device.last_crossing_at.isoformat()} if alive and device.camera_mode==Device.Mode.TRIPWIRE and device.last_crossing_at and 0 <= (timezone.now()-device.last_crossing_at).total_seconds() < 3 else None,
            'model_installed': model_path(settings.VISION_MODEL_DIR,device.detection_model).exists(),
            'line': {'ax':device.line_ax,'ay':device.line_ay,'bx':device.line_bx,'by':device.line_by},
            'follow_camera':device.follow_camera,'line_tracking_status':device.line_tracking_status,
            'line_tracking_error':device.line_tracking_error, 'config_version':device.config_version}
