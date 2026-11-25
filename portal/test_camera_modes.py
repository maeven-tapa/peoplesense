import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from portal.events import EventConflict, record_event
from portal.forms import CameraModeForm
from portal.models import Device, Detection, PortalSettings
from vision.snapshots import save_snapshot, snapshot_path

class CameraModeTests(TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override=override_settings(SURVEILLANCE_CAPTURE_DIR=Path(self.directory.name))
        override.enable();self.addCleanup(override.disable)
        self.lease=uuid.uuid4()
        self.device=Device.objects.create(name='USB doorway',source_type='usb',camera_source='0',worker_token=self.lease,worker_last_seen=timezone.now(),worker_status='running')
        profile=PortalSettings.load();profile.active_device=self.device;profile.setup_stage=4;profile.save()
        self.client.force_login(get_user_model().objects.create_user(username='admin',is_staff=True))
        self.now=timezone.now()

    def surveillance(self,interval=30):
        self.device.camera_mode='surveillance';self.device.capture_interval=interval;self.device.save()

    def test_only_two_modes_and_tripwire_is_default(self):
        self.assertEqual(self.device.camera_mode,'tripwire')
        self.assertEqual(list(CameraModeForm().fields['camera_mode'].choices),[('tripwire','Tripwire'),('surveillance','Surveillance')])
        response=self.client.get(reverse('device-detail',args=[self.device.pk]))
        self.assertContains(response,'Save camera mode')
        self.assertNotContains(response,'Enable tripwire counting')

    def test_save_modes_validates_interval_and_preserves_lease(self):
        url=reverse('camera-mode-save',args=[self.device.pk])
        self.assertEqual(self.client.get(url).status_code,405)
        for mode,interval in [('surveillance',60),('tripwire',30)]:
            self.client.post(url,{'camera_mode':mode,'capture_interval':interval})
            self.device.refresh_from_db()
            self.assertEqual(self.device.camera_mode,mode)
            self.assertEqual(self.device.capture_interval,interval)
            self.assertEqual(self.device.worker_token,self.lease)
        version=self.device.config_version
        for interval in ['nan','0','-1','86401','30.5']:
            self.client.post(url,{'camera_mode':'surveillance','capture_interval':interval})
        self.client.post(url,{'camera_mode':'preview','capture_interval':30})
        self.device.refresh_from_db()
        self.assertEqual(self.device.config_version,version)
        self.assertEqual(self.device.camera_mode,'tripwire')

    def test_capture_interval_survives_restart_and_can_be_changed(self):
        self.surveillance()
        first=save_snapshot(self.device,b'jpeg',2,self.lease,self.now)
        self.assertTrue(snapshot_path(first.event_id).is_file())
        self.assertIsNone(save_snapshot(self.device,b'jpeg',2,self.lease,self.now+timedelta(seconds=29)))
        # A newly loaded worker must obey the last saved timestamp.
        fresh=Device.objects.get(pk=self.device.pk)
        self.assertIsNone(save_snapshot(fresh,b'jpeg',2,self.lease,self.now+timedelta(seconds=29.9)))
        second=save_snapshot(fresh,b'jpeg',1,self.lease,self.now+timedelta(seconds=30))
        self.assertIsNotNone(second)
        self.surveillance(60)
        self.assertIsNone(save_snapshot(self.device,b'jpeg',1,self.lease,self.now+timedelta(seconds=60)))
        self.assertIsNotNone(save_snapshot(self.device,b'jpeg',1,self.lease,self.now+timedelta(seconds=90)))
        self.assertEqual(len(list(Path(self.directory.name).glob('*.jpg'))),3)
        self.assertEqual(Detection.objects.filter(direction='presence').count(),3)
        self.assertEqual(self.client.get(reverse('dashboard-api')).json()['occupancy'],0)
        self.assertEqual(self.client.get(reverse('dashboard-api')).json()['entries'],0)

    def test_stale_mode_or_lease_cannot_save_snapshot_or_crossing(self):
        with self.assertRaises(EventConflict):save_snapshot(self.device,b'jpeg',1,self.lease,self.now)
        self.surveillance()
        with self.assertRaises(EventConflict):record_event(self.device,event_id=uuid.uuid4(),direction='in',lease=self.lease,config_version=1)
        Device.objects.filter(pk=self.device.pk).update(config_version=2)
        with self.assertRaises(EventConflict):save_snapshot(self.device,b'jpeg',1,self.lease,self.now)
        self.device.refresh_from_db()
        Device.objects.filter(pk=self.device.pk).update(worker_token=None)
        with self.assertRaises(EventConflict):save_snapshot(self.device,b'jpeg',1,self.lease,self.now)
        self.assertFalse(list(Path(self.directory.name).iterdir()))
        self.assertEqual(Detection.objects.count(),0)

    def test_capture_routes_are_private_and_active_camera_only(self):
        self.surveillance();event=save_snapshot(self.device,b'jpeg',1,self.lease,self.now)
        url=reverse('capture-image',args=[event.event_id])
        response=self.client.get(url)
        self.assertEqual(b''.join(response.streaming_content),b'jpeg')
        self.assertEqual(response['Cache-Control'],'private, no-store')
        self.assertContains(self.client.get(reverse('captures')),url)
        self.assertEqual(Client().get(url).status_code,302)
        nonstaff=Client();nonstaff.force_login(get_user_model().objects.create_user(username='guest'))
        self.assertEqual(nonstaff.get(url).status_code,403)
        archived=Device.objects.create(name='Archived')
        old=Detection.objects.create(device=archived,direction='presence',has_snapshot=True)
        self.assertEqual(self.client.get(reverse('capture-image',args=[old.event_id])).status_code,404)
        snapshot_path(event.event_id).unlink()
        self.assertEqual(self.client.get(url).status_code,404)

    def test_direction_feedback_is_recent_and_cleared_when_mode_changes(self):
        url=reverse('camera-status',args=[self.device.pk])
        for direction in ['in','out']:
            event,_=record_event(self.device,event_id=uuid.uuid4(),direction=direction,lease=self.lease,config_version=1)
            self.assertEqual(self.client.get(url).json()['last_crossing']['direction'],direction)
        Device.objects.filter(pk=self.device.pk).update(last_crossing_at=self.now-timedelta(seconds=4))
        self.assertIsNone(self.client.get(url).json()['last_crossing'])
        self.client.post(reverse('camera-mode-save',args=[self.device.pk]),{'camera_mode':'surveillance','capture_interval':30})
        self.assertIsNone(self.client.get(url).json()['last_crossing'])

    def test_surveillance_can_finish_setup_without_counting_line(self):
        profile=PortalSettings.load();profile.setup_stage=3;profile.line_configured=False;profile.save()
        self.client.post(reverse('camera-mode-save',args=[self.device.pk]),{'camera_mode':'surveillance','capture_interval':60})
        response=self.client.post('/setup/line/',{'line_confirmed':'on'})
        self.assertEqual(response.status_code,302)
        profile.refresh_from_db();self.assertEqual(profile.setup_stage,4)

    def test_skipped_setup_does_not_interrupt_saving_camera_controls(self):
        profile=PortalSettings.load();profile.setup_stage=0;profile.save()
        session=self.client.session;session['setup_deferred']=True;session.save()
        url=reverse('device-detail',args=[self.device.pk])
        mode=self.client.post(reverse('camera-mode-save',args=[self.device.pk]),{'camera_mode':'tripwire','capture_interval':30})
        self.assertRedirects(mode,url)
        line=self.client.post(reverse('tripwire-save',args=[self.device.pk]),{'line_ax':.1,'line_ay':.5,'line_bx':.9,'line_by':.5,'line_tolerance':.025,'entry_side':'positive'})
        self.assertRedirects(line,url)

    def test_snapshot_write_failure_does_not_leave_event(self):
        from unittest.mock import patch
        self.surveillance()
        with patch('vision.snapshots.os.replace',side_effect=OSError('Full disk')):
            with self.assertRaises(OSError):save_snapshot(self.device,b'jpeg',1,self.lease,self.now)
        self.assertEqual(Detection.objects.count(),0)
        self.assertFalse(list(Path(self.directory.name).iterdir()))
