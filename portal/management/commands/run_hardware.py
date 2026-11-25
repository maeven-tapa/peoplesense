from django.core.management.base import BaseCommand, CommandError
from hardware.worker import run_hardware
from hardware.service import HardwareError


class Command(BaseCommand):
    help = 'Run the Raspberry Pi 4 pan/tilt servos and DHT11 worker.'

    def handle(self, *args, **options):
        try:run_hardware()
        except HardwareError as error:raise CommandError(str(error)) from error
