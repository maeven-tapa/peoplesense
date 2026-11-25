import uuid
from django.core.management.base import BaseCommand, CommandError
from vision.service import CameraError, claim_camera

class Command(BaseCommand):
    help = 'Run the lightweight OpenCV person detection worker for one camera.'
    def add_arguments(self,parser):
        parser.add_argument('--device',type=int,required=True)
        parser.add_argument('--lease',type=uuid.UUID)
        parser.add_argument('--max-frames',type=int)
    def handle(self,*args,**options):
        if options['max_frames'] is not None and options['max_frames']<1:
            raise CommandError('--max-frames must be positive.')
        try:
            lease = options['lease'] or claim_camera(options['device'])
            from vision.runtime import configure_native_threads
            configure_native_threads()
            from vision.worker import run_camera
            result = run_camera(options['device'],lease,options['max_frames'])
            self.stdout.write(str(result))
            if result['status']=='error':
                raise CommandError(result['error'])
        except (CameraError,ImportError) as error:
            if 'lease' in locals():
                from portal.models import Device
                Device.objects.filter(pk=options['device'],worker_token=lease).update(worker_token=None,worker_pid=0,worker_status='error',worker_error='Camera startup failed. Check the source and installed vision dependencies.')
            raise CommandError(str(error)) from error
