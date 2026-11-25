import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Device, Detection, PersonStay, PortalSettings


class CameraSelectionTests(TestCase):
    def setUp(self):
        discovery = patch('vision.cameras.connected_camera_choices', return_value=[
            ('0', 'USB camera 0 · Webcam'), ('1', 'USB camera 1 · Second webcam'), ('2', 'USB camera 2 · Third webcam')])
        discovery.start()
        self.addCleanup(discovery.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        override = override_settings(CAMERA_RUNTIME_DIR=self.folder/'cameras', SURVEILLANCE_CAPTURE_DIR=self.folder/'captures')
        override.enable()
        self.addCleanup(override.disable)
        self.user = get_user_model().objects.create_user(username='camera-admin', is_staff=True)
        self.client.force_login(self.user)
        self.lease = uuid.uuid4()
        self.camera = Device.objects.create(name='Doorway camera', location='Lobby', source_type='usb', camera_source='0',
            worker_token=self.lease, worker_status='running', worker_last_seen=timezone.now(), detection_model='yolox_nano')
        self.profile = PortalSettings.load()
        self.profile.active_device = self.camera
        self.profile.setup_stage = 4
        self.profile.line_configured = True
        self.profile.save()
        self.url = reverse('camera-select', args=[self.camera.pk])
        self.page = reverse('device-detail', args=[self.camera.pk])
        Detection.objects.create(device=self.camera, direction='in', count=4)
        PersonStay.objects.create(device=self.camera, track_id='old', worker_token=self.lease,
            started_at=timezone.now(), last_seen_at=timezone.now())

    def test_selector_on_camera_page_and_device_information_on_settings_only(self):
        response = self.client.get(self.page)
        for text in ['Select camera', 'Use this camera', 'USB camera 0', 'Camera rotation', 'Live camera preview']:
            self.assertContains(response, text)
        self.assertNotContains(response, 'USB camera 5')
        self.assertNotContains(response, 'Device information')
        self.assertEqual(response.context['selection_form'].initial['camera_source'], '0')
        archived = Device.objects.create(name='Archived camera', location='Archived')
        Detection.objects.create(device=archived, direction='in', count=100)
        response = self.client.get('/settings/')
        for text in ['Device information', 'Doorway camera', 'Lobby', 'Total entries recorded', 'IP address']:
            self.assertContains(response, text)
        self.assertEqual(response.context['device_entries'], 4)
        self.assertNotContains(response, 'Archived camera')

    def test_new_usb_source_stops_feed_preserves_history_and_keeps_my_camera_accessible(self):
        original_version = self.camera.config_version
        original_mode = self.camera.camera_mode
        picture = self.folder/'captures'/'keep.jpg'
        picture.parent.mkdir()
        picture.write_bytes(b'saved picture')
        response = self.client.post(self.url, {'camera_source':'2', 'detection_model':'efficientdet_lite0', 'name':'Injected name'})
        self.assertRedirects(response, self.page)
        self.camera.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual((self.camera.camera_source, self.camera.source_type), ('2', 'usb'))
        self.assertIsNone(self.camera.worker_token)
        self.assertEqual(self.camera.worker_status, 'stopped')
        self.assertEqual(self.camera.config_version, original_version+1)
        self.assertEqual(self.camera.line_tracking_status, 'reposition')
        self.assertEqual(self.camera.camera_mode, original_mode)
        self.assertEqual(self.camera.detection_model, 'yolox_nano')
        self.assertEqual(self.camera.name, 'Doorway camera')
        self.assertFalse(self.profile.line_configured)
        self.assertEqual(self.profile.setup_stage, 4)
        self.assertEqual(Detection.objects.filter(device=self.camera).count(), 1)
        self.assertTrue(PersonStay.objects.get().ended)
        self.assertEqual(picture.read_bytes(), b'saved picture')
        self.assertContains(self.client.get(self.page), 'value="2" selected')

    @patch('vision.service.stop_camera')
    def test_selecting_current_source_does_not_interrupt_feed_or_invalidate_line(self, stop):
        self.assertRedirects(self.client.post(self.url, {'camera_source':'0'}), self.page)
        stop.assert_not_called()
        self.camera.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(self.camera.worker_token, self.lease)
        self.assertEqual(self.camera.config_version, 1)
        self.assertTrue(self.profile.line_configured)
        self.assertFalse(PersonStay.objects.get().ended)

    def test_invalid_sources_are_rejected_without_partial_updates(self):
        for source in ['', '-1', '3', '6', '999', 'rtsp://example/camera', '../video.mp4', 'NaN']:
            response = self.client.post(self.url, {'camera_source':source})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['selection_form'].errors)
            self.camera.refresh_from_db()
            self.assertEqual(self.camera.camera_source, '0')
            self.assertEqual(self.camera.worker_token, self.lease)
            self.assertEqual(self.camera.config_version, 1)

    def test_single_connected_camera_has_one_choice_even_with_live_worker(self):
        with patch('vision.cameras.connected_camera_choices', return_value=[('0', 'USB camera 0 · Webcam')]):
            response = self.client.get(self.page)
        self.assertEqual(list(response.context['selection_form'].fields['camera_source'].choices),
                         [('0', 'USB camera 0 · Webcam')])
        self.assertNotContains(response, 'USB camera 1')

    def test_disconnected_camera_is_not_offered_or_accepted(self):
        with patch('vision.cameras.connected_camera_choices', return_value=[]):
            response = self.client.get(self.page)
            self.assertContains(response, 'No connected USB cameras found')
            response = self.client.post(self.url, {'camera_source': '0'})
        self.assertTrue(response.context['selection_form'].errors)
        self.camera.refresh_from_db()
        self.assertEqual(self.camera.worker_token, self.lease)

    def test_connected_linux_index_above_five_is_accepted_by_both_forms(self):
        from .forms import ApplianceCameraForm, CameraSelectionForm
        with patch('vision.cameras.connected_camera_choices', return_value=[('12', 'USB camera 12 · Webcam')]):
            form = CameraSelectionForm({'camera_source': '12'})
            self.assertTrue(form.is_valid(), form.errors)
            settings_form = ApplianceCameraForm({'name': 'Camera', 'location': 'Door', 'camera_source': '12',
                'detection_model': 'yolox_nano', 'confidence_threshold': .45, 'process_fps': 4})
            self.assertTrue(settings_form.is_valid(), settings_form.errors)

    def test_selection_is_admin_only_post_only_csrf_protected_and_active_camera_only(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(Client().post(self.url, {'camera_source':'1'}).status_code, 302)
        guest = get_user_model().objects.create_user(username='guest')
        client = Client()
        client.force_login(guest)
        self.assertEqual(client.post(self.url, {'camera_source':'1'}).status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, {'camera_source':'1'}).status_code, 403)
        archived = Device.objects.create(name='Archived camera')
        self.assertEqual(self.client.post(reverse('camera-select', args=[archived.pk]), {'camera_source':'1'}).status_code, 404)

    def test_surveillance_selection_preserves_mode_capture_interval_and_requires_no_tripwire(self):
        Device.objects.filter(pk=self.camera.pk).update(camera_mode='surveillance', capture_interval=60)
        self.assertRedirects(self.client.post(self.url, {'camera_source':'1'}), self.page)
        self.camera.refresh_from_db()
        self.assertEqual((self.camera.camera_mode, self.camera.capture_interval), ('surveillance', 60))
        self.assertEqual((self.camera.line_tracking_status, self.camera.line_tracking_error), ('waiting', ''))
        response = self.client.get(self.page)
        self.assertNotContains(response, 'Reposition and save the counting line before counting resumes.')

    def test_pending_tripwire_position_survives_worker_claim_and_save_reenables_counting(self):
        from vision.service import claim_camera
        self.client.post(self.url, {'camera_source':'1'})
        with patch('vision.service.verify_model'), patch('vision.service.importlib.util.find_spec', return_value=True):
            lease = claim_camera(self.camera.pk)
        self.camera.refresh_from_db()
        self.assertEqual(self.camera.worker_token, lease)
        self.assertEqual(self.camera.line_tracking_status, 'reposition')
        self.assertIn('Position and save', self.camera.line_tracking_error)
        self.client.post(reverse('tripwire-save',args=[self.camera.pk]), {'line_ax':.1,'line_ay':.6,'line_bx':.9,'line_by':.6,
            'entry_side':'positive','line_tolerance':.025,'follow_camera':'on'})
        self.camera.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual((self.camera.line_tracking_status, self.camera.line_tracking_error), ('waiting', ''))
        self.assertTrue(self.profile.line_configured)

    def test_camera_changed_in_surveillance_requires_new_line_when_switching_to_tripwire(self):
        Device.objects.filter(pk=self.camera.pk).update(camera_mode='surveillance')
        self.client.post(self.url, {'camera_source':'1'})
        self.client.post(reverse('camera-mode-save', args=[self.camera.pk]), {'camera_mode':'tripwire', 'capture_interval':30})
        self.camera.refresh_from_db()
        self.assertEqual(self.camera.line_tracking_status, 'reposition')
        self.assertIn('save the counting line', self.camera.line_tracking_error)

    def test_device_information_handles_unconfigured_appliance(self):
        self.profile.active_device = None
        self.profile.save()
        response = self.client.get('/settings/')
        self.assertContains(response, 'Device information')
        self.assertContains(response, 'Select a USB camera in Quick setup')
