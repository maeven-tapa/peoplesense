import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from portal.events import EventConflict
from portal.models import Device, Detection, PortalSettings
from vision.preview import PreviewBuffer, preview_path
from vision.snapshots import save_manual_snapshot, save_snapshot, snapshot_path


class ManualCaptureTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        override = override_settings(CAMERA_RUNTIME_DIR=self.directory/'preview', SURVEILLANCE_CAPTURE_DIR=self.directory/'captures')
        override.enable();self.addCleanup(override.disable)
        self.lease = uuid.uuid4()
        self.camera = Device.objects.create(name='Capture fixture', source_type='usb', camera_source='0',
            worker_token=self.lease, worker_last_seen=timezone.now(), worker_status='running', visible_tracks=2)
        profile = PortalSettings.load();profile.active_device=self.camera;profile.setup_stage=4;profile.save()
        self.user = get_user_model().objects.create_user(username='capture-admin',is_staff=True)
        self.client.force_login(self.user)
        self.url = reverse('camera-capture',args=[self.camera.pk])

    def writer(self):
        buffer = PreviewBuffer(preview_path(self.directory/'preview',self.camera.pk,self.lease),create=True)
        self.addCleanup(buffer.close)
        return buffer

    def test_saves_latest_frame_in_both_modes_without_crossing_events(self):
        buffer = self.writer()
        for mode, people in [('tripwire',2),('surveillance',0)]:
            Device.objects.filter(pk=self.camera.pk).update(camera_mode=mode,visible_tracks=people)
            buffer.publish(b'old-frame',30);buffer.publish(b'latest-frame',30)
            response = self.client.post(self.url)
            self.assertEqual(response.status_code,200,response.content)
            self.assertIn('no-store',response['Cache-Control'])
            event = Detection.objects.latest('id')
            self.assertTrue(event.is_manual_capture);self.assertEqual(event.count,people)
            self.assertEqual(snapshot_path(event.event_id).read_bytes(),b'latest-frame')
            image = self.client.get(response.json()['image_url'])
            self.assertEqual(b''.join(image.streaming_content),b'latest-frame');image.close()
            self.assertContains(self.client.get(reverse('captures')),'Manual capture')
        self.assertEqual(Detection.objects.exclude(direction='presence').count(),0)
        self.assertEqual(self.client.get(reverse('dashboard-api')).json()['entries'],0)

    def test_private_post_only_csrf_and_active_camera_only(self):
        self.assertEqual(self.client.get(self.url).status_code,405)
        self.assertEqual(Client().post(self.url).status_code,302)
        guest = Client();guest.force_login(get_user_model().objects.create_user(username='guest'))
        self.assertEqual(guest.post(self.url).status_code,403)
        csrf = Client(enforce_csrf_checks=True);csrf.force_login(self.user)
        self.assertEqual(csrf.post(self.url).status_code,403)
        archived = Device.objects.create(name='Archived')
        self.assertEqual(self.client.post(reverse('camera-capture',args=[archived.pk])).status_code,404)

    def test_stopped_starting_missing_empty_and_revoked_preview_cannot_capture(self):
        self.assertEqual(self.client.post(self.url).status_code,409)
        buffer = self.writer()
        self.assertEqual(self.client.post(self.url).status_code,409)
        buffer.publish(b'jpeg',30)
        for updates in [{'worker_status':'starting'},{'worker_status':'running','worker_token':None},
                        {'worker_token':self.lease,'worker_last_seen':timezone.now()-timedelta(seconds=31)},
                        {'worker_token':uuid.uuid4(),'worker_last_seen':timezone.now()}]:
            Device.objects.filter(pk=self.camera.pk).update(**updates)
            self.assertEqual(self.client.post(self.url).status_code,409)
        self.assertFalse(Detection.objects.exists())

    def test_race_with_stop_or_configuration_change_is_rejected(self):
        for update in [{'worker_token':None},{'config_version':2}]:
            Device.objects.filter(pk=self.camera.pk).update(**update)
            with self.assertRaises(EventConflict):save_manual_snapshot(self.camera,b'jpeg')
            Device.objects.filter(pk=self.camera.pk).update(worker_token=self.lease,config_version=1)
        self.assertFalse(Detection.objects.exists())
        self.assertFalse((self.directory/'captures').exists())

    def test_manual_capture_does_not_delay_automatic_surveillance(self):
        self.camera.camera_mode='surveillance';self.camera.save()
        now = timezone.now()
        first = save_snapshot(self.camera,b'automatic',1,self.lease,now)
        with patch('vision.snapshots.timezone.now',return_value=now+timedelta(seconds=29)):
            manual = save_manual_snapshot(self.camera,b'manual')
        self.assertTrue(manual.is_manual_capture)
        automatic = save_snapshot(self.camera,b'next',1,self.lease,now+timedelta(seconds=30))
        self.assertIsNotNone(automatic);self.assertFalse(automatic.is_manual_capture)
        self.assertNotEqual(first.event_id,automatic.event_id)

    def test_failed_write_reports_error_without_orphan_event(self):
        self.writer().publish(b'jpeg',30)
        with patch('vision.snapshots.os.replace',side_effect=OSError('Full disk')):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code,503)
        self.assertFalse(Detection.objects.exists())
        self.assertFalse(list((self.directory/'captures').iterdir()))

    def test_shared_topbar_and_preview_capture_buttons(self):
        for url in [reverse('device-detail',args=[self.camera.pk]),reverse('dashboard')]:
            response = self.client.get(url)
            self.assertContains(response,'data-preview-capture')
            self.assertContains(response,'portal-background-art')
            self.assertNotContains(response,'notification-button')
            self.assertNotContains(response,'top-avatar')
