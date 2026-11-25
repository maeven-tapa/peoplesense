"""Transactional event recording for the local camera worker."""
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from .models import Alert, Detection, Device, PortalSettings

class EventConflict(ValueError):
    pass

@transaction.atomic
def record_event(device, *, event_id, direction, count=1, temperature=None, timestamp=None, track_id='', lease=None, config_version=None, has_snapshot=False):
    # Obtain the write lock before checking uniqueness on SQLite. Postgres locks
    # this camera row too. Worker events share this write path.
    lock = Device.objects.filter(pk=device.pk)
    if lease is not None:
        lock = lock.filter(worker_token=lease)
        if config_version is not None:
            lock = lock.filter(config_version=config_version,camera_mode=Device.Mode.SURVEILLANCE if direction=='presence' else Device.Mode.TRIPWIRE)
    if not lock.update(last_seen=timezone.now(),status=Device.Status.ONLINE):
        raise EventConflict('Camera worker lease has ended.')
    existing = Detection.objects.filter(event_id=event_id).first()
    if existing:
        if (existing.device_id != device.pk or existing.direction!=direction or existing.count!=count
                or existing.temperature!=temperature or existing.track_id!=track_id or existing.has_snapshot!=has_snapshot
                or (timestamp is not None and existing.timestamp!=timestamp)):
            raise EventConflict('Event ID already exists with different event data.')
        return existing,False
    event = Detection.objects.create(device=device,event_id=event_id,direction=direction,count=count,
        temperature=temperature,timestamp=timestamp or timezone.now(),track_id=track_id,has_snapshot=has_snapshot)
    if direction in ['in','out']:
        Device.objects.filter(pk=device.pk).update(last_crossing_direction=direction,last_crossing_at=event.timestamp)
    config = PortalSettings.load()
    if temperature is not None and temperature>=config.temperature_threshold:
        Alert.objects.create(kind='temperature',title='Elevated temperature detected',
            description=f'{temperature}°C reading at {device.location}. Configured threshold: {config.temperature_threshold}°C.',device=device,detection=event)
    if direction=='in':
        occupancy_events=Detection.objects.filter(timestamp__lte=timezone.now())
        if config.active_device_id:
            occupancy_events=occupancy_events.filter(device_id=config.active_device_id)
        totals = {r['direction']:r['n'] or 0 for r in occupancy_events.values('direction').annotate(n=Sum('count'))}
        occupancy = max(0,totals.get('in',0)-totals.get('out',0))
        if occupancy>config.capacity and not Alert.objects.filter(kind='capacity',device=device,acknowledged=False).exists():
            Alert.objects.create(kind='capacity',device=device,title='Building capacity exceeded',description=f'{occupancy} people detected. Building capacity is {config.capacity}.')
    return event,True
