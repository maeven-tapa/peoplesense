from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from vision.model_store import MODELS, download_model, verify_model

class Command(BaseCommand):
    help = 'Download checksum-pinned EfficientDet Lite and YOLOX-Nano person detection models.'
    def add_arguments(self, parser):
        parser.add_argument('--model', choices=[*MODELS, 'all'], default='all')
    def handle(self, *args, **options):
        names = MODELS if options['model'] == 'all' else [options['model']]
        for name in names:
            try:
                try:
                    path = verify_model(settings.VISION_MODEL_DIR, name)
                    self.stdout.write(f'{name}: verified {path.name}')
                    continue
                except (FileNotFoundError, ValueError):
                    pass
                result = download_model(settings.VISION_MODEL_DIR, name)
                self.stdout.write(self.style.SUCCESS(f'{result["name"]}: {result["bytes"]/1024/1024:.2f} MiB, SHA256 {result["sha256"]}'))
            except Exception as error:
                raise CommandError(f'Could not download {name}: {error}') from error
