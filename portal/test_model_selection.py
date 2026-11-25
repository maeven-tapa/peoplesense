from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
from django.apps import apps
from django.db import connection
from django.test import TestCase, override_settings
from .forms import ApplianceCameraForm
from .models import Device, PortalSettings
from vision.service import claim_camera


class ModelSelectionTests(TestCase):
    def setUp(self):
        discovery = patch('vision.cameras.connected_camera_choices', return_value=[('0', 'Webcam')])
        discovery.start()
        self.addCleanup(discovery.stop)

    def test_yolo_selection_and_removed_ssd_validation(self):
        data = {'name': 'Door camera', 'location': 'Door', 'camera_source': '0',
                'detection_model': 'yolox_nano', 'confidence_threshold': .45, 'process_fps': 8}
        form = ApplianceCameraForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().detection_model, 'yolox_nano')
        data['detection_model'] = 'ssd_mobilenet_v2'
        self.assertFalse(ApplianceCameraForm(data).is_valid())

    def test_legacy_ssd_migrates_without_changing_efficientdet(self):
        old = Device.objects.create(name='Old SSD', detection_model='ssd_mobilenet_v2', config_version=4)
        default = Device.objects.create(name='Current', detection_model='efficientdet_lite0', config_version=7)
        migration = import_module('portal.migrations.0012_replace_ssd_with_yolox_nano')
        migration.replace_ssd(apps, SimpleNamespace(connection=connection))
        old.refresh_from_db(); default.refresh_from_db()
        self.assertEqual((old.detection_model, old.config_version), ('yolox_nano', 5))
        self.assertEqual((default.detection_model, default.config_version), ('efficientdet_lite0', 7))

    def test_yolo_start_does_not_require_tflite_runtime(self):
        camera = Device.objects.create(name='Nano', source_type='usb', camera_source='0', detection_model='yolox_nano')
        profile = PortalSettings.load(); profile.active_device = camera; profile.save()
        def available(name):
            return object() if name in ['cv2', 'numpy', 'scipy'] else None
        with tempfile.TemporaryDirectory() as folder, override_settings(CAMERA_RUNTIME_DIR=folder), \
             patch('vision.service.importlib.util.find_spec', side_effect=available), \
             patch('vision.service.verify_model'), patch('vision.service.frame_path'):
            self.assertIsNotNone(claim_camera(camera.pk))
