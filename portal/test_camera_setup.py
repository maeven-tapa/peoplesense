import io
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase


class CameraSetupChecksTests(SimpleTestCase):
    def run_check(self, cameras=(), inference_error=None, backend=True):
        output = io.StringIO()
        detector = Mock()
        detector.detect.side_effect = inference_error
        with patch('vision.detector.PersonDetector', return_value=detector) as factory, \
                patch('vision.cameras.connected_camera_choices', return_value=list(cameras)), \
                patch('cv2.videoio_registry.hasBackend', return_value=backend), \
                patch('portal.management.commands.check_camera_setup.sys.platform', 'linux'), \
                patch('cv2.VideoCapture') as capture:
            try:
                call_command('check_camera_setup', stdout=output)
            finally:
                capture.assert_not_called()
        return output.getvalue(), detector, factory

    def test_both_models_run_inference_and_usb_camera_names_are_reported(self):
        output, detector, factory = self.run_check([('12', 'USB camera 12 · Webcam')])
        self.assertIn('Available: USB camera 12', output)
        self.assertEqual([call.args[1] for call in factory.call_args_list], ['efficientdet_lite0', 'yolox_nano'])
        self.assertEqual(detector.detect.call_count, 2)
        self.assertEqual(detector.close.call_count, 2)
        self.assertEqual(detector.detect.call_args.args[0].shape, (480, 640, 3))

    def test_installation_can_finish_without_camera_connected(self):
        output, _, _ = self.run_check()
        self.assertIn('connect a USB webcam', output)
        self.assertIn('Camera setup checks passed', output)

    def test_missing_linux_capture_backend_fails(self):
        with self.assertRaisesRegex(CommandError, 'V4L2'):
            self.run_check(backend=False)

    def test_bad_model_fails_and_closes_detector(self):
        detector = Mock()
        detector.detect.side_effect = ValueError('broken model')
        with patch('vision.detector.PersonDetector', return_value=detector), \
                patch('cv2.videoio_registry.hasBackend', return_value=True), \
                self.assertRaisesRegex(CommandError, 'broken model'):
            call_command('check_camera_setup', stdout=io.StringIO())
        detector.close.assert_called_once()
