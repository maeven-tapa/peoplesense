from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from portal.models import Alert, Device

class Command(BaseCommand):
    help = 'Create an alert for cameras offline for more than five minutes. Run periodically in live mode.'
    def handle(self, *args, **options):
        if settings.DEMO_MODE:
            self.stdout.write('Demo mode: camera health check skipped.')
            return
        created = 0
        cutoff = timezone.now()-timedelta(minutes=5)
        for device in Device.objects.all():
            if device.source_type!='external' and device.worker_status in ['stopped','finished']:
                continue  # An intentionally stopped camera is not a lost heartbeat.
            if device.effective_status != 'offline' or (device.last_seen is None and device.created_at > cutoff):
                continue
            if not Alert.objects.filter(device=device,kind='device',acknowledged=False).exists():
                Alert.objects.create(kind='device',device=device,title=f'{device.name} is offline',
                    description='No heartbeat or detection has been received for more than five minutes. Check camera power and connectivity.')
                created += 1
        self.stdout.write(f'Created {created} offline camera alerts.')
