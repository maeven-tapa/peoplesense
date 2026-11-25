import csv
import io
import json
import uuid
from unittest.mock import patch
from datetime import timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password, check_password
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from .models import Alert, Detection, Device, PortalSettings

class PortalTests(TestCase):
    def setUp(self):
        self.camera = Device.objects.create(name='Main entrance', location='Lobby', status='online', last_seen=timezone.now(), api_key_hash=make_password('test-camera-key'))
        self.config = PortalSettings.load()
        self.config.active_device=self.camera;self.config.setup_stage=4;self.config.save()
        self.client.force_login(get_user_model().objects.create_user(username='test-admin',password='test-password',is_staff=True))
        self.now = timezone.now()
        Detection.objects.create(device=self.camera, direction='in', count=10, temperature=36.5, timestamp=self.now-timedelta(hours=2))
        Detection.objects.create(device=self.camera, direction='out', count=4, timestamp=self.now-timedelta(hours=1))
        Detection.objects.create(device=self.camera, direction='presence', count=3, temperature=37.5, timestamp=self.now-timedelta(minutes=5))



    def test_counts_and_presence_semantics(self):
        data = self.client.get(reverse('dashboard-api')).json()
        self.assertEqual(data['occupancy'], 6)
        self.assertEqual(data['entries'], 10)
        self.assertEqual(data['exits'], 4)
        self.assertEqual(data['elevated'], 1)
        self.assertEqual(data['readings'], 2)
        self.assertEqual(data['temperature'], 37)
        self.assertEqual(data['points'][-1]['occupancy'], 6)

    def test_period_filters_do_not_reset_current_occupancy(self):
        Detection.objects.create(device=self.camera, direction='in', count=5, timestamp=self.now-timedelta(days=2))
        day = self.client.get(reverse('dashboard-api'), {'period':'24h'}).json()
        week = self.client.get(reverse('dashboard-api'), {'period':'7d'}).json()
        self.assertEqual(day['occupancy'], 11)
        self.assertEqual(week['occupancy'], 11)
        self.assertEqual(day['entries'], 10)
        self.assertEqual(week['entries'], 15)
        self.assertEqual(len(week['points']), 7)


    def test_future_event_not_counted(self):
        Detection.objects.create(device=self.camera, direction='in', count=500, timestamp=self.now+timedelta(days=1))
        self.assertEqual(self.client.get(reverse('dashboard-api')).json()['occupancy'], 6)








    def test_activity_search_direction_and_pagination(self):
        for i in range(25):
            Detection.objects.create(device=self.camera, direction='in')
        response = self.client.get(reverse('activity'), {'direction':'in','q':'Lobby','page':2})
        self.assertEqual(response.context['events'].number, 2)
        self.assertEqual(response.context['events'].paginator.count, 26)
        self.assertTrue(all(event.direction=='in' for event in response.context['events']))

    def test_csv_export_includes_data_and_prevents_formulas(self):
        self.camera.name = '=HYPERLINK("bad")'
        self.camera.save()
        response = self.client.get(reverse('export'))
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(len(rows), 4)
        self.assertTrue(rows[1][2].startswith("'="))
        self.assertEqual(response['Content-Type'], 'text/csv')









class CameraAndRegressionTests(TestCase):
    def setUp(self):
        discovery = patch('vision.cameras.connected_camera_choices', return_value=[('0', 'Webcam')])
        discovery.start()
        self.addCleanup(discovery.stop)
        import tempfile
        from pathlib import Path
        self.camera_temp=tempfile.TemporaryDirectory(prefix='peoplesense-camera-tests-')
        self.addCleanup(self.camera_temp.cleanup)
        self.runtime_override=override_settings(CAMERA_RUNTIME_DIR=Path(self.camera_temp.name),
            CAMERA_LOG_DIR=Path(self.camera_temp.name)/'logs')
        self.runtime_override.enable()
        self.addCleanup(self.runtime_override.disable)
        self.device=Device.objects.create(name='USB entrance',location='Lobby',source_type='usb',camera_source='0',temperature_enabled=False,api_key_hash=make_password('test-key'))
        profile=PortalSettings.load();profile.active_device=self.device;profile.setup_stage=4;profile.save()
        self.client.force_login(get_user_model().objects.create_user(username='test-admin',password='test-password',is_staff=True))
    def test_invalid_device_filters_do_not_crash_or_return_all_data(self):
        for value in ['²','9999999999999999999999999999999','-1','abc']:
            with self.subTest(value=value):
                response=self.client.get(reverse('dashboard-api'),{'device':value})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['devices_total'],0)
                self.assertEqual(self.client.get(reverse('activity'),{'device':value}).status_code,200)
    def test_occupancy_chart_and_metric_use_same_negative_net_semantics(self):
        Detection.objects.create(device=self.device,direction='out',count=10,timestamp=timezone.now()-timedelta(days=2))
        Detection.objects.create(device=self.device,direction='in',count=5)
        data=self.client.get(reverse('dashboard-api')).json()
        self.assertEqual(data['occupancy'],0)
        self.assertEqual(data['points'][-1]['occupancy'],0)
    def test_paused_or_reconfigured_camera_cannot_record_stale_crossings(self):
        from .events import record_event,EventConflict
        self.device.worker_token=uuid.uuid4();self.device.save()
        token=self.device.worker_token
        Device.objects.filter(pk=self.device.pk).update(config_version=2)
        with self.assertRaises(EventConflict):record_event(self.device,event_id=uuid.uuid4(),direction='in',lease=token,config_version=1)
        Device.objects.filter(pk=self.device.pk).update(camera_mode='surveillance')
        with self.assertRaises(EventConflict):record_event(self.device,event_id=uuid.uuid4(),direction='in',lease=token,config_version=2)
        self.assertEqual(Detection.objects.count(),0)
    def test_filtered_csv_matches_event_filter(self):
        Detection.objects.create(device=self.device,direction='in')
        Detection.objects.create(device=self.device,direction='out')
        response=self.client.get(reverse('export'),{'direction':'out','q':'Lobby'})
        rows=list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1][4],'Exit')
    def test_tripwire_configuration_validates_and_preserves_worker_lease(self):
        token=uuid.uuid4();self.device.worker_token=token;self.device.save()
        data={'line_ax':.1,'line_ay':.5,'line_bx':.9,'line_by':.5,'entry_side':'negative','line_tolerance':.03,'follow_camera':'on'}
        self.assertEqual(self.client.post(reverse('tripwire-save',args=[self.device.pk]),data).status_code,302)
        self.device.refresh_from_db()
        self.assertEqual(self.device.worker_token,token)
        self.assertEqual(self.device.config_version,2)
        self.assertEqual(self.device.camera_mode,'tripwire')
        self.assertTrue(self.device.follow_camera)
        self.assertEqual(self.client.get(reverse('camera-status',args=[self.device.pk])).json()['line']['ay'],.5)
        self.assertEqual(self.device.entry_side,'negative')
        data['line_ax']='nan'
        self.client.post(reverse('tripwire-save',args=[self.device.pk]),data)
        self.device.refresh_from_db()
        self.assertEqual(self.device.line_ax,.1)
    def test_camera_settings_edit_does_not_clear_running_worker(self):
        self.device.worker_token=uuid.uuid4();self.device.worker_pid=123;self.device.worker_status='running';self.device.save()
        token=self.device.worker_token
        self.client.post(reverse('device-edit',args=[self.device.pk]),{'name':'Renamed','location':'Lobby','ip_address':'127.0.0.1','source_type':'usb','camera_source':'0','detection_model':'efficientdet_lite0','confidence_threshold':.45,'process_fps':8})
        self.device.refresh_from_db()
        self.assertEqual(self.device.worker_token,token)
        self.assertEqual(self.device.worker_pid,123)
        self.assertEqual(self.device.name,'Renamed')
    def test_start_only_allows_one_worker_and_stop_revokes_lease(self):
        from unittest.mock import patch,Mock
        from vision.service import start_camera,stop_camera,CameraError
        with patch('vision.service.verify_model'),patch('vision.service.importlib.util.find_spec',return_value=True),patch('vision.service.subprocess.Popen',return_value=Mock(pid=123)):
            self.assertEqual(start_camera(self.device.pk),123)
            with self.assertRaises(CameraError):start_camera(self.device.pk)
        from django.conf import settings
        self.assertTrue((settings.CAMERA_LOG_DIR/f'camera-{self.device.pk}.log').is_file())
        self.assertFalse((settings.CAMERA_RUNTIME_DIR/f'camera-{self.device.pk}.log').exists())
        self.device.refresh_from_db();old=self.device.worker_token
        stop_camera(self.device.pk)
        self.device.refresh_from_db();self.assertIsNone(self.device.worker_token)
        from .events import record_event,EventConflict
        with self.assertRaises(EventConflict):record_event(self.device,event_id=uuid.uuid4(),direction='in',lease=old)
        self.assertEqual(Detection.objects.count(),0)
    def test_old_worker_cannot_reclaim_started_lease(self):
        from unittest.mock import patch
        from vision.service import claim_camera
        with patch('vision.service.verify_model'),patch('vision.service.importlib.util.find_spec',return_value=True):
            lease=claim_camera(self.device.pk)
        self.device.refresh_from_db();self.device.worker_pid=123;self.device.save()
        import importlib.util
        if importlib.util.find_spec('cv2'):
            from vision.worker import run_camera
            from vision.service import CameraError
            with self.assertRaises(CameraError):run_camera(self.device.pk,lease,max_frames=1)
    def test_cv_detail_preview_renders_and_frame_route_is_private(self):
        response=self.client.get(reverse('device-detail',args=[self.device.pk]))
        self.assertContains(response,'tripwire-canvas')
        self.assertContains(response,'Start camera')
        self.assertContains(response,'Draw new line')
        self.assertContains(response,'Follow camera movement')
        self.assertEqual(self.client.get(reverse('camera-frame',args=[self.device.pk])).status_code,204)
        with override_settings(DEMO_MODE=False):
            self.assertEqual(Client().get(reverse('camera-frame',args=[self.device.pk])).status_code,302)
            self.assertEqual(Client().post(reverse('camera-start',args=[self.device.pk])).status_code,302)
