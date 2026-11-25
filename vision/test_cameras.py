import struct
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from .cameras import _linux_camera_choices, connected_camera_choices


class CameraDiscoveryTests(TestCase):
    def test_linux_filters_metadata_and_pi_codecs_sorts_indices_and_keeps_busy_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('video0', 'video1', 'video2', 'video3', 'video12', 'video-not-a-device'):
                (root / name).touch()

            def query(fd, request, data, mutate):
                import os
                # fcntl sees a real open descriptor but only queries metadata.
                index = next(i for i in (0, 1, 2, 3, 12)
                             if os.stat(root / f'video{i}').st_ino == os.fstat(fd).st_ino)
                driver = b'bcm2835-codec' if index == 2 else b'uvcvideo'
                data[:len(driver)] = driver
                data[16:22] = b'Webcam'
                bus = b'platform:codec' if index == 2 else b'usb-1-1' if index in (0, 1, 3) else b'usb-1-2'
                data[48:48+len(bus)] = bus
                caps = 0x00800000 if index == 1 else 0x04000001
                struct.pack_into('=II', data, 84, 0x84000001, caps)

            def physical(path, *args, **kwargs):
                # Two usable interfaces on the same webcam must make one choice.
                return root / ('physical0' if path.parent.name in ('video0', 'video3') else path.parent.name)

            with patch.dict(sys.modules, {'fcntl': SimpleNamespace(ioctl=Mock(side_effect=query))}), \
                    patch.object(Path, 'resolve', physical):
                choices = _linux_camera_choices(root, root / 'sysfs')
            self.assertEqual([choice[0] for choice in choices], ['0', '12'])

    def test_linux_query_failure_does_not_invent_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'video0').touch()
            with patch.dict(sys.modules, {'fcntl': SimpleNamespace(ioctl=Mock(side_effect=PermissionError))}):
                self.assertEqual(_linux_camera_choices(root, root / 'sysfs'), [])

    def test_linux_deduplicates_usb_interfaces_using_physical_sysfs_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in (0, 2):
                (root / f'video{index}').touch()
            usb = root / 'usb-device'
            usb.mkdir()
            (usb / 'idVendor').write_text('046d')

            def query(fd, request, data, mutate):
                # Some drivers report different bus strings for each interface.
                bus = f'usb-1-1:{query.calls}'.encode()
                query.calls += 1
                data[48:48+len(bus)] = bus
                struct.pack_into('=II', data, 84, 0x84000001, 0x04000001)

            query.calls = 0

            def physical(path, *args, **kwargs):
                return usb / path.parent.name

            with patch.dict(sys.modules, {'fcntl': SimpleNamespace(ioctl=Mock(side_effect=query))}), \
                    patch.object(Path, 'resolve', physical):
                self.assertEqual(_linux_camera_choices(root, root/'sysfs'), [('0', 'USB camera 0 · video0')])

    def test_windows_enumerates_directshow_without_opening_capture(self):
        enumerate_cameras = Mock(return_value=[SimpleNamespace(index=0, name='Connected webcam',
            path=r'\\?\usb#vid_046d&pid_082d&mi_00#camera')])
        with patch('vision.cameras.platform.system', return_value='Windows'), \
                patch.dict(sys.modules, {'cv2_enumerate_cameras': SimpleNamespace(enumerate_cameras=enumerate_cameras)}):
            self.assertEqual(connected_camera_choices(), [('0', 'USB camera 0 · Connected webcam')])
        enumerate_cameras.assert_called_once_with(700)

    def test_windows_filters_virtual_and_non_usb_cameras_and_preserves_capture_indices(self):
        cameras = [
            SimpleNamespace(index=0, name='OBS Virtual Camera', path=r'@device:sw:obs'),
            SimpleNamespace(index=1, name='USB camera', path=r'\\?\root#camera'),
            SimpleNamespace(index=12, name='Second webcam', path=r'\\?\USB#VID_046D&PID_082D#second'),
            SimpleNamespace(index=3, name='First webcam', path=r'\\?\usb#vid_046d&pid_082d#first'),
            SimpleNamespace(index=4, name='Phone camera', path=None),
            SimpleNamespace(index=5, name='Unknown camera'),
        ]
        enumerate_cameras = Mock(return_value=cameras)
        with patch('vision.cameras.platform.system', return_value='Windows'), \
                patch.dict(sys.modules, {'cv2_enumerate_cameras': SimpleNamespace(enumerate_cameras=enumerate_cameras)}):
            self.assertEqual(connected_camera_choices(), [
                ('3', 'USB camera 3 · First webcam'), ('12', 'USB camera 12 · Second webcam')])

    def test_linux_uses_usb_bus_and_node_capabilities_instead_of_driver_name(self):
        # A physical USB camera may use gspca; CSI, PCI and loopback devices must
        # stay hidden even if they claim the same driver or capture capabilities.
        cases = [
            (b'gspca_main', b'usb-1-1', 0x84000001, 0x04000001, True),
            (b'uvcvideo', b'platform:camera', 0x84000001, 0x04000001, False),
            (b'v4l2loopback', b'platform:loopback', 0x84000001, 0x04000001, False),
            (b'pci-camera', b'PCI:0000:01:00.0', 0x84000001, 0x04000001, False),
            (b'uvcvideo', b'usb-1-1', 0x84800001, 0x04800000, False),
            (b'uvcvideo', b'usb-1-1', 0x84000001, 0x04000002, False),
            (b'uvcvideo', b'usb-1-1', 0x80000001, 0x00000001, False),
            # Older drivers expose capabilities without DEVICE_CAPS.
            (b'uvcvideo', b'usb-1-1', 0x04000001, 0, True),
            (b'uvcvideo', b'', 0x84000001, 0x04000001, False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'video12').touch()
            for driver, bus, caps, device_caps, expected in cases:
                with self.subTest(driver=driver, bus=bus, caps=caps, device_caps=device_caps):
                    def query(fd, request, data, mutate):
                        self.assertEqual(request, 0x80685600)
                        data[:len(driver)] = driver
                        data[16:22] = b'Webcam'
                        data[48:48+len(bus)] = bus
                        struct.pack_into('=II', data, 84, caps, device_caps)

                    with patch('vision.cameras.platform.system', return_value='Linux'), \
                            patch('vision.cameras._linux_camera_choices', wraps=lambda: _linux_camera_choices(root, root/'sysfs')), \
                            patch.dict(sys.modules, {'fcntl': SimpleNamespace(ioctl=Mock(side_effect=query)),
                                                     'cv2_enumerate_cameras': None}):
                        choices = connected_camera_choices()
                    self.assertEqual(choices, [('12', 'USB camera 12 · Webcam')] if expected else [])
