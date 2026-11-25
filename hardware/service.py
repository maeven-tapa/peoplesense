"""Django-side command mailbox. Web requests never initialize GPIO."""
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from portal.models import HardwareState, Device


class HardwareError(ValueError):
    pass


def status():
    state = HardwareState.load()
    ready = settings.HARDWARE_ENABLED and state.alive
    fresh = ready and state.sensor_seen_at and 0 <= (timezone.now()-state.sensor_seen_at).total_seconds() < 15
    return {'enabled': settings.HARDWARE_ENABLED, 'ready': ready, 'moving': state.moving if ready else False,
        'pan': state.pan_angle if ready else None, 'tilt': state.tilt_angle if ready else None,
        'pan_target': state.pan_target, 'tilt_target': state.tilt_target,
        'error': state.worker_error if settings.HARDWARE_ENABLED else '',
        'message': 'Connected to Raspberry Pi GPIO.' if ready else
                   'Start the Raspberry Pi hardware service.' if settings.HARDWARE_ENABLED else
                   'GPIO controls become available on your Raspberry Pi 4.',
        'temperature': state.ambient_temperature if fresh else None, 'humidity': state.humidity if fresh else None,
        'sensor_error': state.sensor_error if ready else '',
        'sensor_at': state.sensor_seen_at.isoformat() if fresh else None}


@transaction.atomic
def move(device, direction):
    if direction not in {'left','right','up','down','home'}:
        raise HardwareError('Choose left, right, up, down, or center.')
    if not settings.HARDWARE_ENABLED:
        raise HardwareError('Camera rotation requires the Raspberry Pi GPIO service.')
    state = HardwareState.load()
    # Acquire the write lock before reading targets so concurrent tabs cannot
    # overwrite each other's increments or queue commands for a stopped worker.
    HardwareState.objects.filter(pk=1).update(revision=F('revision'))
    state.refresh_from_db()
    if not state.alive:
        raise HardwareError('The Raspberry Pi hardware service is unavailable.')
    if state.moving:
        raise HardwareError('Wait for the camera to finish moving.')
    low, high = settings.SERVO_MIN_ANGLE, settings.SERVO_MAX_ANGLE
    if not 0 <= low < 90 < high <= 180:
        raise HardwareError('Servo angle limits are invalid.')
    pan, tilt = state.pan_target, state.tilt_target
    if direction == 'home':pan=tilt=90
    elif direction in {'left','right'}:pan += (-5 if direction=='left' else 5)*(-1 if settings.SERVO_PAN_REVERSED else 1)
    else:tilt += (-5 if direction=='up' else 5)*(-1 if settings.SERVO_TILT_REVERSED else 1)
    pan, tilt = max(low,min(high,pan)), max(low,min(high,tilt))
    if (pan,tilt) == (state.pan_target,state.tilt_target):
        return status()
    state.pan_target, state.tilt_target = pan, tilt
    state.revision += 1
    state.save(update_fields=['pan_target','tilt_target','revision'])
    if device.camera_mode == Device.Mode.TRIPWIRE and not device.follow_camera:
        Device.objects.filter(pk=device.pk).update(line_tracking_status='reposition',
            line_tracking_error='Camera rotated. Reposition the counting line and save to resume counting.')
    return status()
