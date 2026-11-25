import tempfile
import json
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import SkipTest
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.utils import timezone
from portal.models import Device, Detection, HardwareState
from vision.preview import PreviewBuffer
try:
    import cv2
    import numpy as np
    from vision.worker import run_camera
    from vision.test_worker_alignment import FakeCapture
except ModuleNotFoundError:
    raise SkipTest('Install requirements.txt to run worker mode tests.')

class WorkerModeTests(TestCase):
    def run_frames(self,mode,boxes):
        lease=uuid.uuid4()
        camera=Device.objects.create(name='Mode fixture',source_type='file',camera_source='fixture.avi',
            worker_token=lease,camera_mode=mode,follow_camera=True,capture_interval=30)
        frames=[np.zeros((360,640,3),dtype=np.uint8) for _ in boxes]
        clock=[timezone.now()]
        base=clock[0]
        detections=iter(enumerate(boxes))
        def detect(frame,timestamp):
            index,result=next(detections)
            clock[0]=base+timedelta(seconds=index*10)
            return result
        with tempfile.TemporaryDirectory() as directory,override_settings(CAMERA_RUNTIME_DIR=Path(directory)/'preview',SURVEILLANCE_CAPTURE_DIR=Path(directory)/'captures'), \
             patch('vision.worker.PersonDetector') as detector,patch('vision.worker.open_capture',return_value=FakeCapture(frames)), \
             patch('vision.worker.time.sleep'),patch('vision.worker.timezone.now',side_effect=lambda:clock[0]),patch('vision.worker.SceneAnchor') as anchor:
            detector.return_value.detect.side_effect=detect
            result=run_camera(camera.pk,lease)
            photos=[cv2.imdecode(np.frombuffer(path.read_bytes(),dtype=np.uint8),cv2.IMREAD_COLOR) for path in (Path(directory)/'captures').glob('*.jpg')]
        return camera,result,photos,anchor

    def test_surveillance_saves_real_jpegs_at_interval_and_skips_empty_scene(self):
        person=[(.2,.1,.4,.7,.9)]
        camera,result,photos,anchor=self.run_frames('surveillance',[person,person,person,[],[],person,person,person])
        self.assertEqual(result['status'],'finished',result)
        self.assertEqual(result['entries'],0);self.assertEqual(result['exits'],0)
        self.assertEqual(Detection.objects.filter(device=camera,direction='presence',has_snapshot=True).count(),2)
        self.assertEqual(len(photos),2)
        self.assertTrue(all(photo is not None and photo.shape==(360,640,3) for photo in photos))
        anchor.assert_not_called()

    def test_surveillance_with_no_people_saves_nothing(self):
        camera,result,photos,anchor=self.run_frames('surveillance',[[]]*8)
        self.assertEqual(result['status'],'finished',result)
        self.assertEqual(Detection.objects.count(),0)
        self.assertEqual(photos,[])
        anchor.assert_not_called()

    def test_default_tripwire_counts_both_directions_without_snapshots(self):
        lease=uuid.uuid4()
        camera=Device.objects.create(name='Tripwire fixture',source_type='file',camera_source='fixture.avi',
            worker_token=lease,follow_camera=False)
        ys=[.35]*4+[.4,.45,.5,.55,.6]+[.6]*4+[.55,.5,.45,.4,.35]+[.35]*3
        positions=iter(ys)
        frames=[np.zeros((360,640,3),dtype=np.uint8) for _ in ys]
        movement_payloads=[]
        original_publish=PreviewBuffer.publish
        def publish(buffer,payload,fps):
            if buffer.path.suffix=='.movement':movement_payloads.append(json.loads(payload))
            return original_publish(buffer,payload,fps)
        with tempfile.TemporaryDirectory() as directory,override_settings(CAMERA_RUNTIME_DIR=Path(directory),SURVEILLANCE_CAPTURE_DIR=Path(directory)/'captures'), \
             patch('vision.worker.PersonDetector') as detector,patch('vision.worker.open_capture',return_value=FakeCapture(frames)),patch('vision.worker.time.sleep'):
            def detect(frame,timestamp):
                y=next(positions)
                return [(.43,y-.25,.57,y,.9)]
            detector.return_value.detect.side_effect=detect
            with patch.object(PreviewBuffer,'publish',publish):result=run_camera(camera.pk,lease)
            self.assertFalse(list(Path(directory).glob('*.movement')))
        self.assertEqual(result['status'],'finished',result)
        self.assertEqual(result['entries'],1)
        self.assertEqual(result['exits'],1)
        self.assertEqual(Detection.objects.filter(has_snapshot=True).count(),0)
        camera.refresh_from_db();self.assertEqual(camera.last_crossing_direction,'out')
        paths=[payload['trails'] for payload in movement_payloads if payload['trails']]
        self.assertTrue(paths)
        self.assertTrue(all(len(trail['points'])<=64 for trails in paths for trail in trails))
        self.assertEqual(paths[-1][0]['points'][-1],[.5,.35])

    @override_settings(HARDWARE_ENABLED=True)
    def test_servo_movement_and_fixed_line_reposition_pause_crossings(self):
        for tracking_status in ['fixed','reposition']:
            lease=uuid.uuid4()
            camera=Device.objects.create(name='Servo fixture',source_type='file',camera_source='fixture.avi',
                worker_token=lease,follow_camera=False,line_tracking_status=tracking_status)
            HardwareState.objects.update_or_create(pk=1,defaults={'worker_token':uuid.uuid4(),'worker_ready':True,
                'worker_last_seen':timezone.now(),'revision':1 if tracking_status=='fixed' else 0,'applied_revision':0})
            positions=iter([.35]*4+[.4,.45,.5,.55,.6]+[.6]*4)
            frames=[np.zeros((360,640,3),dtype=np.uint8) for _ in range(13)]
            with tempfile.TemporaryDirectory() as directory,override_settings(CAMERA_RUNTIME_DIR=Path(directory)), \
                 patch('vision.worker.PersonDetector') as detector,patch('vision.worker.open_capture',return_value=FakeCapture(frames)),patch('vision.worker.time.sleep'):
                def detect(frame,timestamp):
                    y=next(positions);return [(.43,y-.25,.57,y,.9)]
                detector.return_value.detect.side_effect=detect
                result=run_camera(camera.pk,lease)
            self.assertEqual(result['status'],'finished',result)
            self.assertEqual(result['entries'],0)
            camera.refresh_from_db()
            self.assertEqual(camera.line_tracking_status,'moving' if tracking_status=='fixed' else 'reposition')
