import uuid
from datetime import timedelta
from types import SimpleNamespace
from django.test import TestCase
from django.utils import timezone
from portal.models import Device, PersonStay, AreaObservation
from portal.events import EventConflict
from vision.presence import PresenceRecorder

class PresenceRecorderTests(TestCase):
    def setUp(self):
        self.lease=uuid.uuid4();self.now=timezone.now()
        self.camera=Device.objects.create(name='Area camera',camera_mode='surveillance',worker_token=self.lease)
        self.recorder=PresenceRecorder(self.camera,self.lease,0)
    def observe(self,second,visible=True,hits=3,active=True):
        self.recorder.observe([SimpleNamespace(id=1,hits=hits)] if visible else [],{1} if active else set(),second,self.now+timedelta(seconds=second))
    def test_duration_is_independent_of_snapshot_interval_and_missing_frame_grace(self):
        self.observe(0,hits=1);self.observe(.2,hits=2);self.observe(1)
        self.observe(20);self.observe(21,visible=False);self.observe(24,visible=False,active=False)
        stay=PersonStay.objects.get()
        self.assertEqual(stay.duration_seconds,20)
        self.assertEqual(stay.last_seen_at,self.now+timedelta(seconds=20))
        self.assertTrue(stay.ended)
        self.assertEqual(list(AreaObservation.objects.values_list('count',flat=True)),[1,1,0])
    def test_brief_unconfirmed_detection_does_not_create_dwell_visit(self):
        self.observe(0,hits=1);self.observe(1,visible=False,active=False)
        self.assertEqual(PersonStay.objects.count(),0)
    def test_new_worker_track_id_does_not_merge_another_visit(self):
        self.observe(0);self.observe(5);self.recorder.finish()
        next_lease=uuid.uuid4();self.camera.worker_token=next_lease;self.camera.save()
        recorder=PresenceRecorder(self.camera,next_lease,0)
        recorder.observe([SimpleNamespace(id=1,hits=3)],{1},0,self.now+timedelta(seconds=10))
        self.assertEqual(PersonStay.objects.count(),2)
        self.assertEqual(PersonStay.objects.filter(ended=True).count(),1)
    def test_stale_mode_or_lease_cannot_write_history(self):
        Device.objects.filter(pk=self.camera.pk).update(config_version=2)
        with self.assertRaises(EventConflict):self.observe(0)
        self.assertEqual(PersonStay.objects.count(),0);self.assertEqual(AreaObservation.objects.count(),0)
        self.camera.refresh_from_db();Device.objects.filter(pk=self.camera.pk).update(worker_token=None)
        recorder=PresenceRecorder(self.camera,self.lease,0)
        with self.assertRaises(EventConflict):recorder.observe([],set(),0,self.now)
