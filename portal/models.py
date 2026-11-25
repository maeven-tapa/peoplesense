import uuid
from django.conf import settings
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone

class Device(models.Model):
    class Mode(models.TextChoices):
        TRIPWIRE = 'tripwire', 'Tripwire'
        SURVEILLANCE = 'surveillance', 'Surveillance'
    class Status(models.TextChoices):
        ONLINE = 'online', 'Online'
        WARNING = 'warning', 'Attention'
        OFFLINE = 'offline', 'Offline'
    name = models.CharField(max_length=80)
    location = models.CharField(max_length=80)
    ip_address = models.GenericIPAddressField(default='192.168.1.100')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OFFLINE)
    firmware = models.CharField(max_length=30, default='v2.4.1')
    temperature_enabled = models.BooleanField(default=True)
    api_key_hash = models.CharField(max_length=128, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    source_type = models.CharField(max_length=10, choices=[('external', 'External IoT events'), ('usb', 'USB webcam'), ('network', 'RTSP / HTTP camera'), ('file', 'Video file')], default='external')
    camera_source = models.CharField(max_length=1024, blank=True, help_text='USB index (e.g. 0), an RTSP/HTTP URL, or a full path to a video file on this computer.')
    detection_model = models.CharField(max_length=30, choices=[('efficientdet_lite0', 'EfficientDet-Lite0 INT8'), ('yolox_nano', 'YOLOX-Nano · low resource')], default='efficientdet_lite0')
    camera_mode = models.CharField(max_length=12, choices=Mode.choices, default=Mode.TRIPWIRE)
    capture_interval = models.PositiveIntegerField(default=30, validators=[MinValueValidator(1), MaxValueValidator(86400)])
    last_crossing_direction = models.CharField(max_length=3, blank=True, editable=False)
    last_crossing_at = models.DateTimeField(null=True, blank=True, editable=False)
    follow_camera = models.BooleanField(default=True)
    line_tracking_status = models.CharField(max_length=12, default='waiting', editable=False)
    line_tracking_error = models.CharField(max_length=240, blank=True, editable=False)
    line_ax = models.FloatField(default=0.1, validators=[MinValueValidator(0), MaxValueValidator(1)])
    line_ay = models.FloatField(default=0.5, validators=[MinValueValidator(0), MaxValueValidator(1)])
    line_bx = models.FloatField(default=0.9, validators=[MinValueValidator(0), MaxValueValidator(1)])
    line_by = models.FloatField(default=0.5, validators=[MinValueValidator(0), MaxValueValidator(1)])
    entry_side = models.CharField(max_length=8, choices=[('positive', 'Toward the IN side'), ('negative', 'Reverse direction')], default='positive')
    confidence_threshold = models.FloatField(default=0.45, validators=[MinValueValidator(0.1), MaxValueValidator(0.95)])
    line_tolerance = models.FloatField(default=0.025, validators=[MinValueValidator(0.005), MaxValueValidator(0.15)])
    process_fps = models.PositiveSmallIntegerField(default=8, validators=[MinValueValidator(1), MaxValueValidator(30)])
    preview_fps = models.PositiveSmallIntegerField(default=30, choices=[(30, '30 FPS - lower CPU use'), (60, '60 FPS - supported cameras')])
    config_version = models.PositiveIntegerField(default=1)
    worker_token = models.UUIDField(null=True, blank=True, editable=False)
    worker_pid = models.PositiveIntegerField(default=0, editable=False)
    worker_last_seen = models.DateTimeField(null=True, blank=True, editable=False)
    worker_status = models.CharField(max_length=12, default='stopped', editable=False)
    worker_error = models.CharField(max_length=240, blank=True, editable=False)
    worker_fps = models.FloatField(default=0, editable=False)
    visible_tracks = models.PositiveIntegerField(default=0, editable=False)
    class Meta:
        ordering = ['id']
    def __str__(self):
        return self.name
    @property
    def effective_status(self):
        if self.source_type != 'external':
            return self.Status.ONLINE if self.worker_alive and self.worker_status == 'running' else self.Status.OFFLINE
        if settings.DEMO_MODE:
            return self.status
        if not self.last_seen or (timezone.now() - self.last_seen).total_seconds() > 300:
            return self.Status.OFFLINE
        return self.status
    @property
    def status_label(self):
        return dict(self.Status.choices)[self.effective_status]
    @property
    def worker_alive(self):
        return bool(self.worker_token and self.worker_last_seen and (timezone.now()-self.worker_last_seen).total_seconds() < 30)
    @property
    def safe_source(self):
        from urllib.parse import urlsplit, urlunsplit
        if self.source_type == 'network':
            try:
                url = urlsplit(self.camera_source)
                return urlunsplit((url.scheme, url.netloc.rsplit('@', 1)[-1], url.path, '', ''))
            except ValueError:
                return 'Configured network stream'
        if self.source_type == 'file':
            from pathlib import Path
            return Path(self.camera_source).name
        return self.camera_source or 'Not configured'

class Detection(models.Model):
    class Direction(models.TextChoices):
        IN = 'in', 'Entry'
        OUT = 'out', 'Exit'
        PRESENCE = 'presence', 'Presence'
    event_id = models.UUIDField(default=uuid.uuid4, unique=True)
    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name='detections')
    direction = models.CharField(max_length=8, choices=Direction.choices)
    count = models.PositiveIntegerField(default=1)
    temperature = models.DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    track_id = models.CharField(max_length=80, blank=True)
    has_snapshot = models.BooleanField(default=False)
    is_manual_capture = models.BooleanField(default=False, editable=False)
    class Meta:
        ordering = ['-timestamp', '-id']
        indexes = [models.Index(fields=['device', 'timestamp'])]
    @property
    def short_track_id(self):
        return self.track_id.rsplit(':',1)[-1]

class PersonStay(models.Model):
    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name='person_stays')
    worker_token = models.UUIDField()
    track_id = models.CharField(max_length=80)
    started_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(db_index=True)
    duration_seconds = models.FloatField(default=0, validators=[MinValueValidator(0)])
    ended = models.BooleanField(default=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['device','track_id'],name='unique_camera_stay')]


class HardwareState(models.Model):
    """One appliance's bounded command mailbox and latest ambient reading."""
    pan_target = models.PositiveSmallIntegerField(default=90)
    tilt_target = models.PositiveSmallIntegerField(default=90)
    pan_angle = models.PositiveSmallIntegerField(null=True, blank=True)
    tilt_angle = models.PositiveSmallIntegerField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=0)
    applied_revision = models.PositiveIntegerField(default=0)
    moved_at = models.DateTimeField(null=True, blank=True)
    worker_token = models.UUIDField(null=True, blank=True)
    worker_last_seen = models.DateTimeField(null=True, blank=True)
    worker_ready = models.BooleanField(default=False)
    worker_error = models.CharField(max_length=240, blank=True)
    ambient_temperature = models.FloatField(null=True, blank=True)
    humidity = models.FloatField(null=True, blank=True)
    sensor_seen_at = models.DateTimeField(null=True, blank=True)
    sensor_error = models.CharField(max_length=240, blank=True)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def alive(self):
        return bool(self.worker_token and self.worker_ready and self.worker_last_seen
                    and 0 <= (timezone.now()-self.worker_last_seen).total_seconds() < 10)

    @property
    def moving(self):
        return self.alive and (self.revision != self.applied_revision or bool(self.moved_at
            and 0 <= (timezone.now()-self.moved_at).total_seconds() < 1))

class HotspotState(models.Model):
    """One Wi-Fi adapter's apply mailbox; never contains AP credentials."""
    revision = models.PositiveIntegerField(default=0)
    applied_revision = models.PositiveIntegerField(default=0)
    changed_at = models.DateTimeField(null=True, blank=True)
    worker_token = models.UUIDField(null=True, blank=True)
    worker_last_seen = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, default='stopped')
    error = models.CharField(max_length=240, blank=True)
    applied_ssid = models.CharField(max_length=32, blank=True)
    applied_protected = models.BooleanField(default=False)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def alive(self):
        grace = 90 if self.status in {'pending', 'applying'} else 15
        return bool(self.worker_token and self.worker_last_seen
                    and 0 <= (timezone.now() - self.worker_last_seen).total_seconds() < grace)


class AreaObservation(models.Model):
    device = models.ForeignKey(Device, on_delete=models.PROTECT, related_name='area_observations')
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)
    count = models.PositiveIntegerField(default=0)
    class Meta:
        indexes = [models.Index(fields=['device','timestamp'])]

class PortalSettings(models.Model):
    active_device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    wifi_ap_name = models.CharField(max_length=32, default='PeopleSense-AP')
    ap_password_enabled = models.BooleanField(default=False)
    wifi_ap_password_encrypted = models.TextField(blank=True, editable=False)
    time_zone = models.CharField(max_length=64, default='Asia/Manila')
    setup_stage = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(4)])
    line_configured = models.BooleanField(default=False)
    building_name = models.CharField(max_length=80, default='North Building')
    capacity = models.PositiveIntegerField(default=500, validators=[MinValueValidator(1), MaxValueValidator(100000)])
    temperature_threshold = models.DecimalField(max_digits=4, decimal_places=1, default=37.5, validators=[MinValueValidator(35), MaxValueValidator(42)])
    class Meta:
        verbose_name_plural = 'Portal settings'
    def save(self, *args, **kwargs):
        self.pk = 1
        return super().save(*args, **kwargs)
    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

class Alert(models.Model):
    class Kind(models.TextChoices):
        TEMPERATURE = 'temperature', 'Temperature'
        DEVICE = 'device', 'Device'
        CAPACITY = 'capacity', 'Occupancy'
    kind = models.CharField(max_length=15, choices=Kind.choices)
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    device = models.ForeignKey(Device, null=True, blank=True, on_delete=models.SET_NULL)
    detection = models.OneToOneField(Detection, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(default=timezone.now)
    acknowledged = models.BooleanField(default=False)
    class Meta:
        ordering = ['-created_at']
