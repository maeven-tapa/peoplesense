"""Validate installed camera dependencies as the actual appliance service user."""
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Test both installed detection models and discover USB cameras without starting capture.'

    def handle(self, *args, **options):
        try:
            from vision.runtime import configure_native_threads
            configure_native_threads()
            import cv2
            import numpy as np
            from vision.cameras import connected_camera_choices
            from vision.detector import PersonDetector
            from vision.model_store import MODELS

            cv2.setNumThreads(1)
            if sys.platform.startswith('linux') and not cv2.videoio_registry.hasBackend(cv2.CAP_V4L2):
                raise CommandError('OpenCV is missing its Linux V4L2 camera backend.')

            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            for name in MODELS:
                detector = PersonDetector(settings.VISION_MODEL_DIR, name)
                try:
                    detector.detect(frame, 0)
                finally:
                    detector.close()
                self.stdout.write(self.style.SUCCESS(f'{name}: model loaded and inference passed.'))

            cameras = connected_camera_choices()
            if cameras:
                for _, label in cameras:
                    self.stdout.write(self.style.SUCCESS(f'Available: {label}'))
            else:
                self.stdout.write(self.style.WARNING(
                    'No accessible USB webcam found. Setup can finish now; connect a USB webcam '
                    'and reload camera selection. On Linux the service user needs video-group access.'))
            self.stdout.write('Camera setup checks passed. No camera stream or GPIO was started.')
        except CommandError:
            raise
        except Exception as error:
            raise CommandError(f'Camera setup check failed: {error}') from error
