"""Private JPEG captures, rate limited across worker restarts."""
import os
import uuid
from datetime import timedelta
from pathlib import Path
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from portal.events import EventConflict, record_event
from portal.models import Detection, Device

def snapshot_path(event_id):
    # Only UUID filenames are accepted; no path from a request or database is used.
    return Path(settings.SURVEILLANCE_CAPTURE_DIR) / f'{uuid.UUID(str(event_id))}.jpg'

def capture_due(device, last_capture, now):
    return last_capture is None or (now-last_capture).total_seconds() >= device.capture_interval

@transaction.atomic
def save_snapshot(device, jpeg, count, lease, now=None):
    now = now or timezone.now()
    # Acquire SQLite's write lock before validating the lease and latest capture.
    if not Device.objects.filter(pk=device.pk,worker_token=lease,config_version=device.config_version,
            camera_mode=Device.Mode.SURVEILLANCE).update(last_seen=now):
        raise EventConflict('Camera configuration changed before capture.')
    latest = Detection.objects.filter(device=device,has_snapshot=True,is_manual_capture=False).order_by('-timestamp').first()
    if latest and not capture_due(device,latest.timestamp,now):
        return None
    return _persist_snapshot(device, jpeg, count, now, lease=lease)


@transaction.atomic
def save_manual_snapshot(device, jpeg):
    now = timezone.now()
    if not device.worker_token:
        raise EventConflict('Start the camera before capturing a photo.')
    # The same write lock used by stop/configuration changes prevents saving a
    # frame from a revoked camera or assigning it to a different configuration.
    if not Device.objects.filter(pk=device.pk,worker_token=device.worker_token,
            config_version=device.config_version,camera_mode=device.camera_mode,
            worker_status='running',worker_last_seen__gt=now-timedelta(seconds=30)).update(last_seen=now):
        raise EventConflict('Camera changed or stopped. Start it and try again.')
    return _persist_snapshot(device, jpeg, device.visible_tracks, now, manual=True)


def _persist_snapshot(device, jpeg, count, now, *, lease=None, manual=False):
    event_id = uuid.uuid4()
    path = snapshot_path(event_id)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_bytes(jpeg)
        os.replace(temporary,path)
        if manual:
            event = Detection.objects.create(device=device,event_id=event_id,direction='presence',count=count,
                timestamp=now,has_snapshot=True,is_manual_capture=True)
        else:
            event,_ = record_event(device,event_id=event_id,direction='presence',count=count,timestamp=now,
                lease=lease,config_version=device.config_version,has_snapshot=True)
        return event
    except Exception:
        temporary.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise
