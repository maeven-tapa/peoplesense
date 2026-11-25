from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from portal.models import Device, PortalSettings


class Command(BaseCommand):
    help='Initialize the single-camera portal and first-use administrator without resetting an existing password.'
    @transaction.atomic
    def handle(self,*args,**options):
        User=get_user_model()
        admin,created=User.objects.get_or_create(username='admin',defaults={'is_staff':True})
        if created:
            admin.set_password('12345678')
            admin.save(update_fields=['password'])
        profile=PortalSettings.load()
        if not profile.active_device_id:
            device=Device.objects.filter(source_type='usb').order_by('-worker_last_seen','id').first()
            if device is None:
                from vision.cameras import connected_camera_choices
                cameras = connected_camera_choices()
                source = cameras[0][0] if cameras else '0'
                device=Device.objects.create(name='Entrance camera',location='Main entrance',source_type='usb',camera_source=source,ip_address='127.0.0.1',temperature_enabled=False)
            profile.active_device=device
            profile.save(update_fields=['active_device'])
        # Revoke other local workers without deleting archived cameras/events.
        Device.objects.exclude(pk=profile.active_device_id).update(worker_token=None,worker_status='stopped',worker_fps=0,visible_tracks=0)
        if profile.setup_stage<4:
            Device.objects.filter(pk=profile.active_device_id).update(worker_token=None,worker_status='stopped',worker_fps=0,visible_tracks=0)
        self.stdout.write('Single-camera portal initialized. Existing passwords and historical records were preserved.')
