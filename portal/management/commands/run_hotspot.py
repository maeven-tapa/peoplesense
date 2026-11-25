from django.core.management.base import BaseCommand, CommandError
from networking.service import HotspotError


class Command(BaseCommand):
    help = 'Broadcast the Raspberry Pi Wi-Fi AP at 10.0.0.1 through NetworkManager.'

    def handle(self, *args, **options):
        from networking.worker import run_hotspot
        try:
            run_hotspot()
        except HotspotError as error:
            raise CommandError(str(error)) from error
