import importlib.util
import json
import signal
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from portal.forms import WifiSetupForm
from portal.models import Device, HotspotState, PortalSettings
from .service import HotspotError, request_apply, status
from .worker import apply_hotspot, run_hotspot, ready_request

spec = importlib.util.spec_from_file_location('hotspot_helper', settings.BASE_DIR / 'deploy/rpi/peoplesense-hotspot-helper.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class HelperTests(SimpleTestCase):
    def request(self, **changes):
        return {'ssid': 'PeopleSense-AP', 'protected': False, 'password': '', **changes}

    def test_ssid_and_password_are_safely_encoded_in_fixed_profile(self):
        request = self.request(ssid='Door;[wifi]\\🙂', protected=True, password=' pass\\word=42; ')
        data = helper.keyfile(request).decode()
        self.assertIn('address1=10.0.0.1/24', data)
        self.assertIn('interface-name=wlan0', data)
        self.assertIn('method=shared', data)
        self.assertIn('never-default=true', data)
        self.assertIn('key-mgmt=wpa-psk', data)
        self.assertIn('proto=rsn', data)
        encoded = data.split('ssid=')[1].splitlines()[0]
        self.assertEqual(bytes(int(x) for x in encoded.rstrip(';').split(';')).decode(), request['ssid'])
        self.assertIn(r'psk=\spass\\word=42;\s', data)
        self.assertNotIn('wifi-security', helper.keyfile(self.request()).decode())

    def test_helper_rejects_injection_extra_fields_and_bad_credentials(self):
        invalid = [self.request(ssid='x\n[ipv4]'), self.request(ssid='x'*33),
                   self.request(protected='true'), self.request(password='unexpected'),
                   self.request(protected=True, password='short'),
                   self.request(protected=True, password='abcdefgh\n'),
                   self.request(interface='eth0'), self.request(address='10.2.0.1')]
        for value in invalid:
            with self.subTest(value={k:v for k,v in value.items() if k!='password'}):
                with self.assertRaises(helper.HotspotError):
                    helper.keyfile(value)

    def test_nmcli_never_uses_shell_and_redacts_backend_failure(self):
        failure = SimpleNamespace(returncode=1, stdout='', stderr='secret-password-in-backend')
        with patch.object(helper.subprocess, 'run', return_value=failure) as run:
            with self.assertRaises(helper.HotspotError) as result:
                helper.nmcli('connection', 'load', str(helper.KEYFILE))
        self.assertNotIn('secret-password', str(result.exception))
        self.assertEqual(run.call_args.args[0][0], '/usr/bin/nmcli')
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_overlap_guard_leaves_other_interfaces_unchanged(self):
        def nm(*args, **kwargs):
            if 'GENERAL.NM-MANAGED' in args or 'WIFI-PROPERTIES.AP' in args:
                return 'yes'
            if 'DEVICE' in args:
                return 'wlan0\neth0\nlo'
            return '10.0.0.5/24'
        with patch.object(helper.platform, 'system', return_value='Linux'), \
             patch.object(helper.Path, 'exists', return_value=True), \
             patch.object(helper.Path, 'read_text', return_value='Raspberry Pi 4 Model B'), \
             patch.object(helper, 'nmcli', side_effect=nm) as commands:
            with self.assertRaisesMessage(helper.HotspotError, 'Another interface uses 10.0.0.0/24'):
                helper.preflight()
        self.assertFalse(any('up' in call.args or 'modify' in call.args for call in commands.call_args_list))

    def test_idempotent_profile_does_not_restart_the_ap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'profile.nmconnection'
            path.write_bytes(helper.keyfile(self.request()))
            with patch.object(helper, 'KEYFILE', path), patch.object(helper, 'preflight'), \
                 patch.object(helper, 'active_state', return_value=(helper.PROFILE_UUID, True)), \
                 patch.object(helper, 'nmcli') as commands:
                self.assertEqual(helper.apply(self.request()), {'active': True, 'address': '10.0.0.1'})
            commands.assert_not_called()

    def test_failed_update_restores_previous_profile_and_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'profile.nmconnection'
            previous = helper.keyfile(self.request(ssid='Previous AP', protected=True, password='Previous-42!'))
            path.write_bytes(previous)
            calls = []
            def nm(*args, **kwargs):
                calls.append(args)
                if args[:3] == ('connection', 'up', 'uuid') and len([x for x in calls if x[:2]==('connection','up')]) == 1:
                    raise helper.HotspotError('Activation failed.')
                return ''
            with patch.object(helper, 'KEYFILE', path), patch.object(helper, 'preflight'), \
                 patch.object(helper, 'active_state', return_value=(helper.PROFILE_UUID, True)), \
                 patch.object(helper, 'atomic_write', side_effect=lambda p,b: p.write_bytes(b)), \
                 patch.object(helper, 'nmcli', side_effect=nm):
                with self.assertRaisesMessage(helper.HotspotError, 'Activation failed'):
                    helper.apply(self.request(ssid='New AP'))
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(calls[-1][:4], ('connection', 'up', 'uuid', helper.PROFILE_UUID))

    def test_success_verifies_the_actual_uuid_and_address(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'profile.nmconnection'
            with patch.object(helper, 'KEYFILE', path), patch.object(helper, 'preflight'), \
                 patch.object(helper, 'active_state', side_effect=[('old-station-uuid',False),(helper.PROFILE_UUID,True)]), \
                 patch.object(helper, 'atomic_write', side_effect=lambda p,b: p.write_bytes(b)), \
                 patch.object(helper, 'nmcli') as commands:
                self.assertTrue(helper.apply(self.request())['active'])
                self.assertIn('10.0.0.1/24', path.read_text())
                self.assertTrue(any(call.args[:4]==('connection','up','uuid',helper.PROFILE_UUID) for call in commands.call_args_list))
                self.assertFalse(any('down' in call.args or 'delete' in call.args for call in commands.call_args_list))


@override_settings(HOTSPOT_ENABLED=True)
class HotspotTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        override = override_settings(AP_KEY_FILE=self.directory/'wifi.key', AP_SECRET_KEY=None)
        override.enable(); self.addCleanup(override.disable)
        self.user = get_user_model().objects.create_user(username='ap-admin', password='Portal-password-42!', is_staff=True)
        self.camera = Device.objects.create(name='USB', source_type='usb', camera_source='0')
        self.profile = PortalSettings.load(); self.profile.active_device=self.camera; self.profile.setup_stage=4; self.profile.save()
        self.client.force_login(self.user)

    def test_wifi_save_queues_delayed_apply_and_keeps_secrets_private(self):
        response = self.client.post('/settings/', {'action':'wifi','wifi_ap_name':'Entrance AP',
            'ap_password_enabled':'on','wifi_ap_password':'AP-Secret-42!'})
        self.assertRedirects(response, '/settings/')
        state = HotspotState.load()
        self.assertEqual(state.revision,1)
        self.assertEqual(state.status,'pending')
        self.assertIsNotNone(state.changed_at)
        response = self.client.get('/settings/hotspot/status/')
        self.assertEqual(response.json()['url'],'http://10.0.0.1/')
        self.assertEqual(response.json()['ssid'],'Entrance AP')
        self.assertNotIn('AP-Secret-42!', response.content.decode())
        self.profile.refresh_from_db()
        self.assertNotIn(self.profile.wifi_ap_password_encrypted, response.content.decode())
        self.assertIn('no-store', response['Cache-Control'])

    def test_quick_setup_uses_the_same_mailbox(self):
        self.profile.setup_stage=1; self.profile.save()
        response=self.client.post('/setup/network/',{'wifi_ap_name':'Setup AP'})
        self.assertEqual(response.status_code,302)
        self.assertEqual(HotspotState.load().revision,1)

    def test_auth_methods_and_hotspot_http_host(self):
        self.client.logout()
        self.assertEqual(self.client.get('/settings/hotspot/status/').status_code,302)
        self.assertEqual(self.client.get('/', HTTP_HOST='10.0.0.1').status_code,200)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/settings/hotspot/status/').status_code,405)

    def test_active_status_requires_a_fresh_worker_and_applied_revision(self):
        state=HotspotState.load(); state.worker_token=uuid.uuid4(); state.worker_last_seen=timezone.now()
        state.status='active'; state.applied_revision=state.revision; state.save()
        self.assertTrue(status()['active'])
        request_apply()
        self.assertFalse(status()['active'])
        HotspotState.objects.filter(pk=1).update(status='active',applied_revision=1,worker_last_seen=timezone.now()-timedelta(seconds=16))
        self.assertFalse(status()['active'])
        with override_settings(HOTSPOT_ENABLED=False):
            self.assertEqual(status()['label'],'Pi setup required')

    def test_adapter_passes_password_on_stdin_only_and_redacts_it(self):
        form=WifiSetupForm({'wifi_ap_name':'Door','ap_password_enabled':'on','wifi_ap_password':'Private-42!'},instance=self.profile)
        self.assertTrue(form.is_valid(),form.errors); profile=form.save()
        result=SimpleNamespace(returncode=0,stdout=json.dumps({'active':True,'address':'10.0.0.1'}),stderr='')
        with patch('networking.worker.subprocess.run',return_value=result) as run:
            apply_hotspot(profile)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/sudo','-n','/usr/local/libexec/peoplesense-hotspot'])
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['password'],'Private-42!')
        result=SimpleNamespace(returncode=1,stdout=json.dumps({'error':'Private-42!'}),stderr='')
        with patch('networking.worker.subprocess.run',return_value=result):
            with self.assertRaises(HotspotError) as error:
                apply_hotspot(profile)
            self.assertNotIn('Private-42!',str(error.exception))

    def test_windows_does_not_execute_adapter(self):
        with patch('networking.worker.is_pi4',return_value=False), patch('networking.worker.subprocess.run') as run:
            with self.assertRaisesMessage(HotspotError,'requires a Raspberry Pi 4'):
                run_hotspot(max_cycles=1)
        run.assert_not_called()

    def run_worker(self, cycles, adapter):
        with override_settings(BASE_DIR=self.directory), patch('networking.worker.is_pi4',return_value=True), \
             patch.dict('sys.modules',{'fcntl':MagicMock()}), patch('networking.worker.time.sleep'), \
             patch('networking.worker.signal.signal',return_value=signal.SIG_DFL):
            run_hotspot(max_cycles=cycles, adapter=adapter)

    def test_worker_waits_for_response_delay_and_applies_on_boot(self):
        request_apply()
        adapter=MagicMock()
        self.run_worker(1,adapter)
        adapter.assert_not_called()
        HotspotState.objects.filter(pk=1).update(changed_at=timezone.now()-timedelta(seconds=10))
        self.run_worker(2,adapter)
        adapter.assert_called_once()
        state=HotspotState.load()
        self.assertEqual(state.applied_revision,1)
        self.assertEqual(state.applied_ssid,'PeopleSense-AP')
        self.assertIsNone(state.worker_token)
        self.camera.refresh_from_db(); self.assertEqual(self.camera.ip_address,'10.0.0.1')

    def test_save_during_provisioning_cannot_mark_new_config_as_applied(self):
        def adapter(profile):
            profile.wifi_ap_name='Second AP'; profile.save(update_fields=['wifi_ap_name'])
            request_apply()
        self.run_worker(1,adapter)
        state=HotspotState.load()
        self.assertEqual(state.revision,1)
        self.assertEqual(state.applied_revision,0)

    def test_fresh_save_is_rechecked_before_adapter_execution(self):
        state=HotspotState.load(); state.worker_token=uuid.uuid4(); state.save()
        request_apply()
        self.assertIsNone(ready_request(state.worker_token))
        HotspotState.objects.filter(pk=1).update(changed_at=timezone.now()-timedelta(seconds=10))
        ready=ready_request(state.worker_token)
        self.assertEqual(ready[0],1)
        self.assertEqual(ready[1].wifi_ap_name,'PeopleSense-AP')
        self.assertIsNone(ready_request(uuid.uuid4()))

    def test_failure_is_recorded_and_retried_with_backoff(self):
        adapter=MagicMock(side_effect=HotspotError('No AP radio.'))
        self.run_worker(3,adapter)
        adapter.assert_called_once()
        state=HotspotState.load()
        self.assertEqual(state.error,'No AP radio.')
        self.assertIsNone(state.worker_token)
