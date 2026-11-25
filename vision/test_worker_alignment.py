import tempfile
import uuid
from pathlib import Path
from unittest import SkipTest
from unittest.mock import patch
from django.test import TestCase, override_settings
try:
    import cv2
    import numpy as np
    from .worker import run_camera
    from .test_scene import doorway
except ModuleNotFoundError:
    raise SkipTest('Install requirements.txt to run worker alignment tests.')
from portal.models import Device, Detection


class FakeCapture:
    def __init__(self,frames):
        self.frames=iter(frames)
    def get(self,_):return 8
    def read(self):
        frame=next(self.frames,None)
        return frame is not None,frame
    def release(self):pass


class WorkerAlignmentTests(TestCase):
    def run_frames(self,frames,detect,**options):
        lease=uuid.uuid4()
        camera=Device.objects.create(name='Moving USB fixture',source_type='file',camera_source='fixture.avi',
            worker_token=lease,line_ax=.2,line_bx=.8,**options)
        with tempfile.TemporaryDirectory() as directory,override_settings(CAMERA_RUNTIME_DIR=Path(directory)), \
             patch('vision.worker.PersonDetector') as detector,patch('vision.worker.open_capture',return_value=FakeCapture(frames)), \
             patch('vision.worker.time.sleep'):
            detector.return_value.detect.side_effect=detect
            result=run_camera(camera.pk,lease)
        camera.refresh_from_db()
        return camera,result

    def test_camera_pan_moves_line_without_counting_stationary_person(self):
        scene=doorway()
        offsets=[0]*10+list(range(4,45,4))+[44]*10
        frames=[cv2.warpAffine(scene,np.float32([[1,0,0],[0,1,offset]]),(640,360)) for offset in offsets]
        index=iter(offsets)
        def detect(frame,timestamp):
            y=.44+next(index)/360
            return [(.43,y-.25,.57,y,.9)]
        camera,result=self.run_frames(frames,detect)
        self.assertEqual(result['status'],'finished')
        self.assertEqual(result['entries'],0)
        self.assertEqual(result['exits'],0)
        self.assertEqual(Detection.objects.count(),0)
        self.assertAlmostEqual(camera.line_ay,.5+44/360,delta=.005)
        self.assertEqual(camera.line_tracking_status,'locked')

    def test_lost_alignment_suppresses_person_crossing(self):
        scene=doorway()
        frames=[scene]*10+[np.zeros_like(scene)]*15
        ys=iter([.35]*10+[.35,.4,.45,.5,.55,.6,.65]+[.65]*8)
        def detect(frame,timestamp):
            y=next(ys)
            return [(.43,y-.25,.57,y,.9)]
        camera,result=self.run_frames(frames,detect)
        self.assertEqual(result['status'],'finished')
        self.assertEqual(camera.line_tracking_status,'lost')
        self.assertEqual(Detection.objects.count(),0)

    def test_manual_save_during_inference_wins_over_automatic_coordinates(self):
        def detect(frame,timestamp):
            Device.objects.filter(name='Moving USB fixture').update(line_ay=.7,line_by=.7,config_version=2)
            return []
        camera,result=self.run_frames([doorway()],detect)
        self.assertEqual(result['status'],'finished')
        self.assertEqual(camera.line_ay,.7)
        self.assertEqual(camera.line_by,.7)
        self.assertEqual(camera.config_version,2)

    @override_settings(HARDWARE_ENABLED=False)
    def test_new_camera_tripwire_stays_paused_until_repositioned_even_without_pi_hardware(self):
        ys = [.35]*4 + [.4,.45,.5,.55,.6] + [.6]*4 + [.55,.5,.45,.4,.35] + [.35]*3
        positions = iter(ys)
        def detect(frame, timestamp):
            y = next(positions)
            return [(.43,y-.25,.57,y,.9)]
        camera, result = self.run_frames([doorway()]*len(ys), detect, follow_camera=False,
            line_tracking_status='reposition', line_tracking_error='Position and save the new camera line.')
        self.assertEqual(result['status'], 'finished')
        self.assertEqual((result['entries'], result['exits']), (0, 0))
        self.assertEqual(camera.line_tracking_status, 'reposition')
        self.assertEqual(Detection.objects.count(), 0)
