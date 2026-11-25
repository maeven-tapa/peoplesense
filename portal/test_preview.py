import tempfile
import json
import time
import uuid
from pathlib import Path
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from portal.models import Device, PortalSettings
from portal.forms import CameraModeForm
from vision.preview import PreviewBuffer, preview_path
from vision.movement import movement_path


class PreviewAccessTests(TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.directory=Path(folder.name)
        override=override_settings(CAMERA_RUNTIME_DIR=self.directory);override.enable();self.addCleanup(override.disable)
        self.lease=uuid.uuid4()
        self.camera=Device.objects.create(name='Preview',source_type='usb',camera_source='0',worker_token=self.lease,worker_last_seen=timezone.now())
        profile=PortalSettings.load();profile.active_device=self.camera;profile.setup_stage=4;profile.save()
        self.client.force_login(get_user_model().objects.create_user(username='preview-admin',is_staff=True))
        self.url=reverse('camera-stream',args=[self.camera.pk])

    def test_preview_choices_and_older_mode_posts_preserve_target(self):
        for target in [30,60]:
            form=CameraModeForm({'camera_mode':'tripwire','capture_interval':30,'preview_fps':target},instance=self.camera)
            self.assertTrue(form.is_valid(),form.errors);form.save()
        form=CameraModeForm({'camera_mode':'surveillance','capture_interval':30},instance=self.camera)
        self.assertTrue(form.is_valid());self.assertEqual(form.cleaned_data['preview_fps'],60)
        for target in [0,29,61,1000]:
            form=CameraModeForm({'camera_mode':'tripwire','capture_interval':30,'preview_fps':target},instance=self.camera)
            self.assertFalse(form.is_valid())

    def test_private_stream_returns_latest_and_ends_on_revoked_lease(self):
        writer=PreviewBuffer(preview_path(self.directory,self.camera.pk,self.lease),create=True)
        try:
            writer.publish(b'first',30);writer.publish(b'latest',30)
            response=self.client.get(self.url)
            self.assertTrue(response.streaming)
            self.assertIn('no-store',response['Cache-Control'])
            chunks=iter(response.streaming_content)
            self.assertIn(b'latest',next(chunks))
            response.close()
            Device.objects.filter(pk=self.camera.pk).update(worker_token=uuid.uuid4())
            from vision.preview import stream_frames
            self.assertEqual(list(stream_frames(self.camera.pk,self.lease,self.directory,30)),[])
            self.assertEqual(Client().get(self.url).status_code,302)
            archived=Device.objects.create(name='Archived')
            self.assertEqual(self.client.get(reverse('camera-stream',args=[archived.pk])).status_code,404)
        finally:writer.close()

    def test_stopped_camera_and_single_frame_compatibility(self):
        writer=PreviewBuffer(preview_path(self.directory,self.camera.pk,self.lease),create=True)
        try:
            writer.publish(b'jpeg',60)
            frame=self.client.get(reverse('camera-frame',args=[self.camera.pk]))
            self.assertEqual(frame.content,b'jpeg')
            status=self.client.get(reverse('camera-status',args=[self.camera.pk])).json()
            self.assertEqual(status['preview_fps'],60)
            Device.objects.filter(pk=self.camera.pk).update(worker_token=None)
            self.assertEqual(self.client.get(self.url).status_code,204)
        finally:writer.close()

    def test_movement_is_private_current_tripwire_data_only(self):
        url=reverse('camera-movement',args=[self.camera.pk])
        payload={'camera_mode':'tripwire','config_version':self.camera.config_version,'generation':2,
            'published_at':time.monotonic(),'width':640,'height':480,
            'trails':[{'id':1,'points':[[.2,.3],[.4,.5]]}]}
        writer=PreviewBuffer(movement_path(self.directory,self.camera.pk,self.lease),create=True)
        try:
            def publish():writer.publish(json.dumps(payload).encode(),0)
            publish()
            response=self.client.get(url)
            self.assertEqual(response.json()['trails'],payload['trails'])
            self.assertIn('private',response['Cache-Control']);self.assertIn('no-store',response['Cache-Control'])
            self.assertEqual(Client().get(url).status_code,302)
            archived=Device.objects.create(name='Archived')
            self.assertEqual(self.client.get(reverse('camera-movement',args=[archived.pk])).status_code,404)
            payload['published_at']=time.monotonic()-3;publish()
            self.assertEqual(self.client.get(url).json()['trails'],[])
            payload['published_at']=time.monotonic();payload['config_version']+=1;publish()
            self.assertEqual(self.client.get(url).json()['trails'],[])
            payload['config_version']=self.camera.config_version;publish()
            Device.objects.filter(pk=self.camera.pk).update(camera_mode='surveillance')
            self.assertEqual(self.client.get(url).json()['trails'],[])
            Device.objects.filter(pk=self.camera.pk).update(camera_mode='tripwire',worker_token=uuid.uuid4())
            self.assertEqual(self.client.get(url).json()['trails'],[])
            Device.objects.filter(pk=self.camera.pk).update(worker_token=None)
            self.assertEqual(self.client.get(url).json()['trails'],[])
        finally:writer.close()

    def test_debug_toggle_is_off_in_preview_cards_and_hidden_for_surveillance(self):
        detail=reverse('device-detail',args=[self.camera.pk])
        for url in [detail,reverse('dashboard')]:
            response=self.client.get(url)
            self.assertContains(response,'Draw movement line')
            self.assertContains(response,'aria-pressed="false" data-movement-url=')
        Device.objects.filter(pk=self.camera.pk).update(camera_mode='surveillance')
        self.assertContains(self.client.get(detail),'data-movement-debug hidden')
