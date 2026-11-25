import random
from datetime import timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from portal.models import Alert, Detection, Device, PortalSettings

class Command(BaseCommand):
    help = 'Create a deterministic demo dataset. Does not overwrite existing cameras or events.'
    @transaction.atomic
    def handle(self, *args, **options):
        if Device.objects.exists() or Detection.objects.exists():
            self.stdout.write('Existing data found; demo seed skipped.')
            return
        config = PortalSettings.load()
        cameras = [
            ('Main entrance', 'Ground floor · Lobby', '192.168.1.101', 'online'),
            ('East entrance', 'Ground floor · East wing', '192.168.1.102', 'online'),
            ('Lobby corridor', 'Ground floor · Reception', '192.168.1.103', 'online'),
            ('West entrance', 'Ground floor · West wing', '192.168.1.104', 'online'),
            ('Staff entrance', 'Ground floor · Service area', '192.168.1.105', 'warning'),
            ('Parking access', 'Basement · Parking level', '192.168.1.106', 'offline'),
        ]
        now = timezone.now()
        devices = [Device.objects.create(name=n, location=l, ip_address=ip, status=s, last_seen=now if s!='offline' else now-timedelta(hours=2)) for n,l,ip,s in cameras]
        rng = random.Random(42)
        events = []
        # Balanced earlier days; the last 24 hours end with 188 occupants.
        for day in range(7):
            start = now-timedelta(days=7-day)
            total_entries = 1248 if day == 6 else rng.randint(900, 1250)
            remaining_in = total_entries
            remaining_out = total_entries-(188 if day==6 else 0)
            weights = [2,1,1,1,1,2,4,7,10,9,7,6,7,8,7,7,6,5,4,3,2,1,1,1]
            in_counts = [round(total_entries*w/sum(weights)) for w in weights]
            in_counts[-1] += total_entries-sum(in_counts)
            running = 0
            for hour, enters in enumerate(in_counts):
                exits = min(running+enters, round(remaining_out/(24-hour)))
                if hour==23:
                    exits = remaining_out
                remaining_out -= exits
                running += enters-exits
                timestamp = start+timedelta(hours=hour, minutes=15)
                for direction, count in [('in', enters), ('out', exits)]:
                    if count:
                        for index, weight in enumerate([0.42, 0.23, 0.14, 0.13, 0.08]):
                            portion = round(count*weight) if index<4 else count-sum(round(count*w) for w in [0.42,0.23,0.14,0.13])
                            if portion>0:
                                temp = Decimal(str(round(rng.uniform(36.1, 36.9), 1))) if direction=='in' else None
                                events.append(Detection(device=devices[index], direction=direction, count=portion, temperature=temp, timestamp=timestamp+timedelta(minutes=index*2)))
        Detection.objects.bulk_create(events)
        # Recent single-person detections show the camera event format without changing occupancy.
        for i, direction in enumerate(['in', 'out', 'in', 'out', 'in', 'out']):
            event = Detection.objects.create(device=devices[i%4], direction=direction, count=1,
                temperature=Decimal('37.8') if i==4 else Decimal('36.5') if direction=='in' else None,
                timestamp=now-timedelta(minutes=6-i))
            if i==4:
                Alert.objects.create(kind='temperature', title='Elevated temperature detected', description='A 37.8°C reading was recorded at the main entrance. Review the sensor reading.', device=devices[0], detection=event, created_at=event.timestamp)
        Alert.objects.create(kind='device', title='Parking access is offline', description='No heartbeat received from this camera. Check its power and network connection.', device=devices[5], created_at=now-timedelta(minutes=42))
        Alert.objects.create(kind='device', title='Staff entrance needs attention', description='Intermittent connection reported. Inspect network signal strength.', device=devices[4], created_at=now-timedelta(minutes=18))
        self.stdout.write(self.style.SUCCESS(f'Created {len(devices)} cameras and {len(events)+6} demo detections.'))
