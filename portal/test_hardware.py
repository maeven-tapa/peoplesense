import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from portal.models import Device, HardwareState, PortalSettings
from hardware.service import HardwareError, move, status
from hardware.worker import PiIO, run_hardware


@override_settings(HARDWARE_ENABLED=True)
class HardwareTests(TestCase):
    def setUp(self):
        self.camera=Device.objects.create(name='Pi fixture',source_type='usb',camera_source='0',follow_camera=False)
        profile=PortalSettings.load();profile.active_device=self.camera;profile.setup_stage=4;profile.save()
        self.client.force_login(get_user_model().objects.create_user(username='pi-admin',is_staff=True))
        self.state=HardwareState.load()
        self.state.worker_token=uuid.uuid4();self.state.worker_ready=True;self.state.worker_last_seen=timezone.now()
        self.state.pan_angle=self.state.tilt_angle=90;self.state.save()
        self.url=reverse('camera-rotate',args=[self.camera.pk])

    def settle(self):
        self.state.refresh_from_db()
        self.state.applied_revision=self.state.revision;self.state.pan_angle=self.state.pan_target;self.state.tilt_angle=self.state.tilt_target
        self.state.moved_at=timezone.now()-timedelta(seconds=2);self.state.save()

    def test_direction_steps_bounds_home_and_single_pending_command(self):
        for direction,expected in [('left',(85,90)),('right',(90,90)),('up',(90,85)),('down',(90,90))]:
            result=move(self.camera,direction)
            self.assertEqual((result['pan_target'],result['tilt_target']),expected)
            self.assertTrue(result['moving'])
            with self.assertRaises(HardwareError):move(self.camera,direction)
            self.settle()
        self.state.pan_target=169;self.state.pan_angle=169;self.state.save()
        self.assertEqual(move(self.camera,'right')['pan_target'],170);self.settle()
        self.assertEqual(move(self.camera,'home')['pan_target'],90);self.settle()
        self.state.pan_target=11;self.state.save()
        self.assertEqual(move(self.camera,'left')['pan_target'],10)
        self.camera.refresh_from_db();self.assertEqual(self.camera.line_tracking_status,'reposition')

    def test_reversed_mount_and_follow_camera_do_not_require_fixed_line_save(self):
        self.camera.follow_camera=True;self.camera.save()
        with override_settings(SERVO_PAN_REVERSED=True,SERVO_TILT_REVERSED=True):
            self.assertEqual(move(self.camera,'left')['pan_target'],95);self.settle()
            self.assertEqual(move(self.camera,'up')['tilt_target'],95)
        self.camera.refresh_from_db();self.assertNotEqual(self.camera.line_tracking_status,'reposition')

    def test_disabled_stale_unready_invalid_commands_and_private_routes(self):
        self.assertEqual(self.client.get(self.url).status_code,405)
        self.assertEqual(Client().post(self.url,{'direction':'left'}).status_code,302)
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(get_user_model().objects.get(username='pi-admin'))
        self.assertEqual(csrf.post(self.url,{'direction':'left'}).status_code,403)
        old=Device.objects.create(name='Archive')
        self.assertEqual(self.client.post(reverse('camera-rotate',args=[old.pk]),{'direction':'left'}).status_code,404)
        self.assertEqual(self.client.post(self.url,{'direction':'shell-command'}).status_code,409)
        with override_settings(HARDWARE_ENABLED=False):
            self.assertEqual(self.client.post(self.url,{'direction':'left'}).status_code,409)
            self.assertFalse(status()['ready'])
        self.state.worker_last_seen=timezone.now()-timedelta(seconds=11);self.state.save()
        self.assertEqual(self.client.post(self.url,{'direction':'left'}).status_code,409)
        self.state.worker_last_seen=timezone.now();self.state.worker_ready=False;self.state.save()
        self.assertEqual(self.client.post(self.url,{'direction':'left'}).status_code,409)

    def test_ambient_values_expire_and_never_enter_people_temperature_records(self):
        self.state.ambient_temperature=27.3;self.state.humidity=61;self.state.sensor_seen_at=timezone.now();self.state.save()
        response=self.client.get(reverse('camera-hardware-status',args=[self.camera.pk]))
        self.assertEqual(response.json()['temperature'],27.3);self.assertEqual(response.json()['humidity'],61)
        self.assertIn('no-store',response['Cache-Control'])
        self.assertIsNone(self.client.get(reverse('dashboard-api')).json()['temperature'])
        self.state.sensor_seen_at=timezone.now()-timedelta(seconds=16);self.state.save()
        self.assertIsNone(status()['temperature']);self.assertIsNone(status()['humidity'])

    def test_gpio_pin_map_and_resources_are_initialized_only_by_worker(self):
        servo=MagicMock(side_effect=[MagicMock(),MagicMock()]);factory=MagicMock();dht=MagicMock()
        modules={'gpiozero':SimpleNamespace(AngularServo=servo),
            'gpiozero.pins':SimpleNamespace(), 'gpiozero.pins.pigpio':SimpleNamespace(PiGPIOFactory=factory),
            'board':SimpleNamespace(D4='BCM4'),'adafruit_dht':SimpleNamespace(DHT11=dht)}
        with patch.dict(sys.modules,modules):
            io=PiIO()
            self.assertEqual([call.args[0] for call in servo.call_args_list],[18,13])
            self.assertEqual(servo.call_args_list[0].kwargs['initial_angle'],None)
            dht.assert_called_once_with('BCM4',use_pulseio=False)
            io.rotate(95,85);self.assertEqual(io.pan.angle,95);self.assertEqual(io.tilt.angle,85)
            with self.assertRaises(HardwareError):io.rotate(181,90)
            io.dht.temperature=27;io.dht.humidity=62
            self.assertEqual(io.read_sensor(),(27.,62.))
            io.dht.temperature=70
            with self.assertRaises(RuntimeError):io.read_sensor()
            io.close();factory.return_value.close.assert_called_once();dht.return_value.exit.assert_called_once()

    def test_worker_applies_mailbox_reads_sensor_and_releases_ownership(self):
        io=MagicMock();io.read_sensor.return_value=(26.5,58)
        lock=SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=MagicMock())
        with tempfile.TemporaryDirectory() as directory,override_settings(BASE_DIR=Path(directory)), \
             patch('hardware.worker.is_pi4',return_value=True),patch.dict(sys.modules,{'fcntl':lock}), \
             patch('hardware.worker.time.sleep') as sleep:
            def queue(_):HardwareState.objects.filter(pk=1).update(pan_target=95,revision=1)
            sleep.side_effect=queue
            run_hardware(max_cycles=2,io_factory=lambda:io)
        self.assertEqual(io.rotate.call_args_list[0].args,(90,90))
        self.assertEqual(io.rotate.call_args_list[1].args,(95,90))
        io.read_sensor.assert_called_once();io.close.assert_called_once()
        self.state.refresh_from_db()
        self.assertEqual(self.state.pan_angle,95);self.assertEqual(self.state.ambient_temperature,26.5)
        self.assertFalse(self.state.worker_ready);self.assertIsNone(self.state.worker_token)

    def test_worker_rejects_non_pi_without_gpio_access(self):
        io=MagicMock()
        with patch('hardware.worker.is_pi4',return_value=False):
            with self.assertRaises(HardwareError):run_hardware(io_factory=io)
        io.assert_not_called()

    def test_rotation_stays_on_camera_page_information_moves_to_settings_and_preview_tools_share_heading(self):
        response=self.client.get(reverse('device-detail',args=[self.camera.pk]))
        html=response.content.decode()
        self.assertIn('Camera rotation', html)
        self.assertIn('Select camera', html)
        self.assertNotIn('Device information', html)
        self.assertContains(self.client.get('/settings/'), 'Device information')
        heading=html[html.index('card-heading preview-heading'):html.index('camera-preview large-preview')]
        self.assertIn('Capture',heading);self.assertIn('Draw movement line',heading)
