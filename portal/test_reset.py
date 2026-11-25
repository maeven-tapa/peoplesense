import contextvars
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

from django.contrib.admin.models import LogEntry, CHANGE
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import Alert, AreaObservation, Detection, Device, HardwareState, HotspotState, PersonStay, PortalSettings
from .reset import ResetError, factory_reset, _storage_paths, _process_alive
from .reset_lock import ResetBusy, device_access
from .wifi_secrets import encrypt_password


class FactoryResetTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.captures = self.root / 'runtime' / 'captures'
        self.cameras = self.root / 'runtime' / 'cameras'
        self.key = self.root / 'runtime' / 'secrets' / 'wifi.key'
        override = override_settings(BASE_DIR=self.root, SURVEILLANCE_CAPTURE_DIR=self.captures,
            CAMERA_RUNTIME_DIR=self.cameras, AP_KEY_FILE=self.key, AP_SECRET_KEY=None,
            VISION_MODEL_DIR=self.root/'models', HOTSPOT_ENABLED=False)
        override.enable()
        self.addCleanup(override.disable)
        self.user = get_user_model().objects.create_user(username='owner', password='Existing-password-42!', is_staff=True)
        self.camera = Device.objects.create(name='Old camera', location='Old location', source_type='usb',
            camera_source='3', camera_mode='surveillance', worker_token=uuid.uuid4(), worker_status='running')
        self.old_token = self.camera.worker_token
        self.profile = PortalSettings.load()
        self.profile.active_device = self.camera
        self.profile.setup_stage = 4
        self.profile.line_configured = True
        self.profile.wifi_ap_name = 'Private AP'
        self.profile.ap_password_enabled = True
        self.profile.wifi_ap_password_encrypted = encrypt_password('Old-Wifi-password')
        self.profile.time_zone = 'America/New_York'
        self.profile.save()
        self.event = Detection.objects.create(device=self.camera, direction='presence', has_snapshot=True)
        Alert.objects.create(kind='device', title='Old alert', device=self.camera, detection=self.event)
        PersonStay.objects.create(device=self.camera, worker_token=self.old_token, track_id='old-track',
            started_at=timezone.now(), last_seen_at=timezone.now())
        AreaObservation.objects.create(device=self.camera, count=3)
        Group.objects.create(name='Old group')
        LogEntry.objects.create(user=self.user, content_type=ContentType.objects.get_for_model(Device),
            object_id=str(self.camera.pk), object_repr='Old camera', action_flag=CHANGE, change_message='Old settings')
        self.captures.mkdir(parents=True)
        self.cameras.mkdir(parents=True)
        (self.captures/f'{self.event.event_id}.jpg').write_bytes(b'private picture')
        (self.captures/'orphan.jpg').write_bytes(b'unrecorded private picture')
        (self.captures/'partial.tmp').write_bytes(b'partial picture')
        (self.cameras/'old.preview').write_bytes(b'private live frame')
        (self.cameras/'old.log').write_bytes(b'old camera log')
        self.root.joinpath('models').mkdir()
        self.root.joinpath('models', 'model.onnx').write_bytes(b'keep model')
        self.root.joinpath('.env.rpi').write_text('keep installation secrets')
        self.client.force_login(self.user)

    def submit(self, **changes):
        values = {'reset_password':'Existing-password-42!', 'reset_confirmation':'RESET'}
        values.update(changes)
        return self.client.post('/settings/factory-reset/', values)

    def assert_old_records(self):
        self.assertTrue(Device.objects.filter(pk=self.camera.pk).exists())
        self.assertTrue(Detection.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(get_user_model().objects.get(pk=self.user.pk).check_password('Existing-password-42!'))

    def test_complete_reset_erases_all_records_pictures_and_sessions_and_restores_defaults(self):
        another = Client()
        another.force_login(self.user)
        old_session = another.session.session_key
        response = self.submit()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/?reset=complete')
        self.assertIn('no-store', response['Cache-Control'])
        for model in [Detection, PersonStay, AreaObservation, Alert, LogEntry, Group, Session]:
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertFalse(Session.objects.filter(session_key=old_session).exists())
        self.assertEqual(Device.objects.count(), 1)
        camera = Device.objects.get()
        self.assertNotEqual(camera.pk, self.camera.pk)
        self.assertEqual((camera.camera_source, camera.camera_mode, camera.worker_token), ('0', 'tripwire', None))
        profile = PortalSettings.load()
        self.assertEqual((profile.setup_stage, profile.line_configured, profile.active_device_id), (0, False, camera.pk))
        self.assertEqual((profile.wifi_ap_name, profile.ap_password_enabled, profile.wifi_ap_password_encrypted), ('PeopleSense-AP', False, ''))
        self.assertEqual(profile.time_zone, 'Asia/Manila')
        self.assertEqual(get_user_model().objects.count(), 1)
        admin = get_user_model().objects.get(username='admin')
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.check_password('12345678'))
        self.assertFalse(self.captures.exists())
        self.assertFalse(self.cameras.exists())
        self.assertFalse(self.key.exists())
        self.assertEqual(self.root.joinpath('models', 'model.onnx').read_bytes(), b'keep model')
        self.assertTrue(self.root.joinpath('.env.rpi').exists())
        self.assertContains(self.client.get('/?reset=complete'), 'Device reset complete')
        self.assertEqual(another.get('/dashboard/').status_code, 302)
        self.assertTrue(self.client.login(username='admin', password='12345678'))
        self.assertRedirects(self.client.get('/dashboard/'), '/setup/', fetch_redirect_response=False)

    def test_requires_current_password_exact_confirmation_staff_login_post_and_csrf(self):
        for values in [{'reset_password':'wrong'}, {'reset_confirmation':'reset'}, {'reset_confirmation':''}]:
            response = self.submit(**values)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['reset_form'].errors)
            self.assertNotContains(response, 'value="Existing-password-42!"')
            self.assert_old_records()
            self.assertTrue((self.captures/'orphan.jpg').exists())
        self.assertEqual(self.client.get('/settings/factory-reset/').status_code, 405)
        self.assertEqual(Client().post('/settings/factory-reset/').status_code, 302)
        guest = get_user_model().objects.create_user(username='guest')
        client = Client()
        client.force_login(guest)
        self.assertEqual(client.post('/settings/factory-reset/').status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post('/settings/factory-reset/', {'reset_confirmation':'RESET'}).status_code, 403)
        self.assert_old_records()

    def test_settings_has_distinct_reset_card_with_reviewable_scope(self):
        response = self.client.get('/settings/')
        for text in ['Factory reset', 'all saved pictures', 'admin / 12345678', 'PeopleSense-AP',
                     'Type RESET to confirm', '/settings/factory-reset/', 'This cannot be undone']:
            self.assertContains(response, text)
        self.assertNotContains(response, 'Existing-password-42!')
        self.assertNotContains(response, 'Old-Wifi-password')

    def test_pi_reset_preserves_worker_ownership_but_queues_default_wifi_and_servo_targets(self):
        token = uuid.uuid4()
        HardwareState.objects.create(pk=1, pan_target=145, tilt_target=65, pan_angle=145, tilt_angle=65,
            worker_token=token, worker_ready=True, worker_last_seen=timezone.now(), revision=5, applied_revision=5,
            ambient_temperature=27, humidity=40, worker_error='Old hardware error')
        HotspotState.objects.create(pk=1, worker_token=token, worker_last_seen=timezone.now(), revision=7,
            applied_revision=7, applied_ssid='Private AP', applied_protected=True, status='active', error='Old network error')
        with override_settings(HOTSPOT_ENABLED=True):
            self.assertEqual(self.submit().status_code, 302)
        hardware = HardwareState.load()
        self.assertEqual((hardware.pan_target, hardware.tilt_target, hardware.revision, hardware.applied_revision), (90, 90, 6, 5))
        self.assertEqual(hardware.worker_token, token)
        self.assertIsNone(hardware.ambient_temperature)
        self.assertEqual(hardware.worker_error, '')
        hotspot = HotspotState.load()
        self.assertEqual((hotspot.revision, hotspot.applied_revision, hotspot.status, hotspot.applied_ssid), (8, 7, 'pending', ''))
        self.assertFalse(hotspot.applied_protected)
        self.assertIsNotNone(hotspot.changed_at)
        self.assertEqual(hotspot.worker_token, token)
        self.assertEqual(Device.objects.get().ip_address, '10.0.0.1')

    def test_stale_camera_cannot_recreate_captures_or_events_after_reset(self):
        from portal.events import EventConflict, record_event
        from vision.snapshots import save_snapshot, save_manual_snapshot
        factory_reset()
        with self.assertRaises(EventConflict):
            record_event(self.camera, event_id=uuid.uuid4(), direction='presence', lease=self.old_token)
        with self.assertRaises(EventConflict):
            save_snapshot(self.camera, b'old picture', 1, self.old_token)
        with self.assertRaises(EventConflict):
            save_manual_snapshot(self.camera, b'old picture')
        self.assertFalse(self.captures.exists())
        self.assertEqual(Detection.objects.count(), 0)

    def test_unsafe_configured_deletion_targets_are_rejected_before_stopping_camera(self):
        for path in [self.root, self.root/'runtime', self.root/'models', self.root.parent]:
            with override_settings(SURVEILLANCE_CAPTURE_DIR=path):
                with self.assertRaises(ResetError):
                    factory_reset()
            self.camera.refresh_from_db()
            self.assertEqual(self.camera.worker_token, self.old_token)
            self.assert_old_records()
        with override_settings(CAMERA_RUNTIME_DIR=self.captures):
            with self.assertRaises(ResetError):
                _storage_paths()

    def test_storage_symlinks_cannot_erase_another_folder(self):
        foreign = self.root/'foreign'
        foreign.mkdir()
        foreign.joinpath('keep.jpg').write_bytes(b'keep')
        link = self.root/'runtime'/'linked-captures'
        try:
            link.symlink_to(foreign, target_is_directory=True)
        except OSError:
            self.skipTest('Host does not allow creating symbolic links.')
        with override_settings(SURVEILLANCE_CAPTURE_DIR=link):
            with self.assertRaises(ResetError):
                factory_reset()
        self.assertEqual(foreign.joinpath('keep.jpg').read_bytes(), b'keep')

    @patch('portal.reset.shutil.rmtree', side_effect=PermissionError('private storage detail'))
    def test_file_cleanup_failure_is_reported_without_clearing_database_or_claiming_success(self, erase):
        response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertContains(response, 'Reset could not finish erasing device files', status_code=503)
        self.assertNotContains(response, 'private storage detail', status_code=503)
        self.assert_old_records()
        self.assertTrue(self.captures.joinpath('orphan.jpg').exists())
        self.camera.refresh_from_db()
        self.assertIsNone(self.camera.worker_token)

    def test_busy_camera_blocks_reset_and_preserves_records_and_files(self):
        self.camera.worker_pid = 999999
        self.camera.save(update_fields=['worker_pid'])
        with patch('portal.reset._process_alive', return_value=True), patch('portal.reset.CAMERA_STOP_TIMEOUT', 0):
            response = self.submit()
        self.assertContains(response, 'camera is still stopping', status_code=503)
        self.assert_old_records()
        self.assertTrue(self.key.exists())

    def test_revokes_all_camera_leases_before_waiting_for_process_exit(self):
        archived = Device.objects.create(name='Archived', location='Old', worker_token=uuid.uuid4(), worker_pid=123456)
        def exited(pid):
            self.assertEqual(pid, 123456)
            self.assertFalse(Device.objects.exclude(worker_token=None).exists())
            return False
        with patch('portal.reset._process_alive', side_effect=exited):
            factory_reset()
        self.assertFalse(Device.objects.filter(pk=archived.pk).exists())

    def test_process_exit_probe_checks_an_actual_child_without_terminating_it(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(_process_alive(child.pid))
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertFalse(_process_alive(child.pid))

    def test_kernel_gate_allows_readers_and_blocks_reset_until_they_finish(self):
        other = contextvars.Context()
        with device_access():
            def second_reader():
                with device_access():
                    pass
            other.run(second_reader)
            def reset_writer():
                with device_access(exclusive=True):
                    self.fail('Reset must not overlap another request.')
            with self.assertRaises(ResetBusy):
                other.run(reset_writer)
        other.run(lambda: self._exclusive_gate())

    def _exclusive_gate(self):
        with device_access(exclusive=True):
            pass

    def test_portal_requests_are_paused_during_reset_and_lock_releases_after_failure(self):
        ready, finish = threading.Event(), threading.Event()
        errors = []
        def hold():
            try:
                with device_access(exclusive=True):
                    ready.set()
                    finish.wait(5)
            except Exception as error:
                errors.append(error)
                ready.set()
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            self.assertEqual(errors, [])
            response = self.client.get('/settings/')
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response['Retry-After'], '3')
        finally:
            finish.set()
            thread.join(3)
        self.assertEqual(self.client.get('/settings/').status_code, 200)
        with self.assertRaises(RuntimeError):
            with device_access(exclusive=True):
                raise RuntimeError('test failure')
        self.assertEqual(self.client.get('/settings/').status_code, 200)
