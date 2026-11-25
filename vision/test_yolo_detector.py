import io
import math
import tempfile
from pathlib import Path
from unittest import SkipTest
from unittest.mock import MagicMock, patch
import numpy as np
from django.conf import settings
from django.test import SimpleTestCase, override_settings
from .detector import PersonDetector
from .model_store import MODELS, download_model
from .yolo_detector import YoloNanoDetector


class YoloNanoTests(SimpleTestCase):
    def make_detector(self):
        net = MagicMock()
        with patch('vision.yolo_detector.verify_model', return_value=Path('model.onnx')), \
             patch('vision.yolo_detector.cv2.dnn.readNetFromONNX', return_value=net):
            return YoloNanoDetector('models'), net

    def test_letterbox_person_decode_and_duplicate_suppression(self):
        detector, net = self.make_detector()
        rows = np.zeros((1, 3549, 85), dtype=np.float32)
        rows[0, 0, :6] = [20, 10, math.log(8), math.log(4), .9, .9]
        # Adjacent grid cell, same box: must be suppressed by NMS.
        rows[0, 1, :6] = [19, 10, math.log(8), math.log(4), .8, .9]
        # A strong non-person class is not counted.
        rows[0, 2, 4] = .99
        rows[0, 2, 6] = .99
        net.forward.return_value = rows
        frame = np.zeros((416, 832, 3), dtype=np.uint8)
        frame[:] = [12, 34, 56]
        try:
            boxes = detector.detect(frame, 0)
            self.assertEqual(len(boxes), 1)
            np.testing.assert_allclose(boxes[0], [128/416, 64/208, 192/416, 96/208, .81], atol=1e-6)
            tensor = net.setInput.call_args.args[0]
            self.assertEqual(tensor.shape, (1, 3, 416, 416))
            np.testing.assert_array_equal(tensor[0, :, 0, 0], [12, 34, 56])
            np.testing.assert_array_equal(tensor[0, :, 415, 0], [114, 114, 114])
        finally:
            detector.close()
        self.assertIsNone(detector.net)

    def test_invalid_boxes_and_incorrect_export_are_rejected(self):
        detector, net = self.make_detector()
        rows = np.zeros((1, 3549, 85), dtype=np.float32)
        rows[0, 0, :6] = [float('nan'), 2, 1, 1, .9, .9]
        rows[0, 1, :6] = [1, 2, 1, 1, float('inf'), .9]
        net.forward.return_value = rows
        self.assertEqual(detector.detect(np.zeros((416, 416, 3), np.uint8), 0), [])
        net.forward.return_value = np.zeros((1, 84, 8400), np.float32)
        with self.assertRaisesMessage(ValueError, 'Unexpected YOLOX-Nano output'):
            detector.detect(np.zeros((416, 416, 3), np.uint8), 1)
        detector.close()

    @override_settings(VISION_BACKEND='litert')
    def test_factory_uses_opencv_instead_of_litert(self):
        sentinel = object()
        with patch('vision.yolo_detector.YoloNanoDetector', return_value=sentinel) as constructor:
            self.assertIs(PersonDetector('models', 'yolox_nano', .6), sentinel)
        constructor.assert_called_once_with('models', 'yolox_nano', .6)

    def test_removed_model_and_corrupt_download(self):
        self.assertNotIn('ssd_mobilenet_v2', MODELS)
        with tempfile.TemporaryDirectory() as folder:
            with patch('vision.model_store.urllib.request.urlopen', return_value=io.BytesIO(b'not an ONNX model')):
                with self.assertRaisesMessage(ValueError, 'Model hash'):
                    download_model(folder, 'yolox_nano')
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_real_model_empty_scene_and_person_fixture(self):
        import cv2
        from .model_store import model_path
        if not model_path(settings.VISION_MODEL_DIR, 'yolox_nano').exists():
            raise SkipTest('Download YOLOX-Nano for inference checks.')
        detector = YoloNanoDetector(settings.VISION_MODEL_DIR, confidence=.3)
        try:
            self.assertEqual(detector.detect(np.zeros((480, 640, 3), np.uint8), 0), [])
            fixture = Path(settings.BASE_DIR) / 'runtime' / 'verification' / 'pose.jpg'
            if fixture.exists():
                boxes = detector.detect(cv2.imread(str(fixture)), 1)
                self.assertGreater(len(boxes), 0)
                self.assertTrue(all(0 <= value <= 1 for box in boxes for value in box))
        finally:
            detector.close()
