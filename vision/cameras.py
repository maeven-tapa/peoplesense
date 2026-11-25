"""Discover camera devices without starting capture or interrupting a live feed."""
import logging
import platform
import re
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_STREAMING = 0x04000000
V4L2_CAP_DEVICE_CAPS = 0x80000000


def _linux_camera_choices(device_dir=Path('/dev'), sysfs=Path('/sys/class/video4linux')):
    import fcntl

    choices = []
    seen = set()
    nodes = [node for node in device_dir.glob('video*') if node.name[5:].isdigit()]
    for node in sorted(nodes, key=lambda path: int(path.name[5:])):
        try:
            # QUERYCAP reads metadata only; it never starts a video stream.
            capability = bytearray(104)  # struct v4l2_capability
            with node.open('rb', buffering=0) as handle:
                fcntl.ioctl(handle.fileno(), 0x80685600, capability, True)
            bus_info = bytes(capability[48:80]).split(b'\0', 1)[0]
            capabilities, device_caps = struct.unpack_from('=II', capability, 84)
            caps = device_caps if capabilities & V4L2_CAP_DEVICE_CAPS else capabilities
            # Identify the USB bus, including webcams using drivers other than UVC.
            # OpenCV's V4L2 worker needs a video capture node with streaming I/O.
            required = V4L2_CAP_VIDEO_CAPTURE | V4L2_CAP_STREAMING
            if not bus_info.startswith(b'usb-') or caps & required != required:
                continue
            interface = (sysfs / node.name / 'device').resolve()
            physical = bus_info
            for parent in (interface, *interface.parents):
                if (parent / 'idVendor').is_file():
                    physical = parent
                    break
            if physical in seen:
                continue
            seen.add(physical)
            name = capability[16:48].split(b'\0', 1)[0].decode('utf-8', errors='replace')
            index = node.name[5:]
            choices.append((index, f'USB camera {index} · {name or node.name}'))
        except OSError:
            logger.warning('Cannot query camera device %s; check video group permissions.', node)
    return choices


def connected_camera_choices():
    system = platform.system()
    if system == 'Linux':
        return _linux_camera_choices()
    try:
        from cv2_enumerate_cameras import enumerate_cameras
        # Match the capture worker's DirectShow indices, avoiding duplicate MSMF entries.
        cameras = enumerate_cameras(700 if system == 'Windows' else 1200)
        if system == 'Windows':
            # Friendly names can also belong to OBS, phone and other virtual cameras.
            cameras = [camera for camera in cameras if re.search(
                r'usb#vid_[0-9a-f]{4}&pid_[0-9a-f]{4}',
                getattr(camera, 'path', '') or '', re.IGNORECASE)]
        else:
            cameras = [camera for camera in cameras
                       if getattr(camera, 'vid', None) is not None and getattr(camera, 'pid', None) is not None]
        return [(str(camera.index), f'USB camera {camera.index} · {camera.name}')
                for camera in sorted(cameras, key=lambda camera: camera.index)]
    except (ImportError, OSError, RuntimeError, NotImplementedError):
        logger.exception('Camera discovery failed. Install the camera discovery dependencies.')
        return []


def configure_camera_field(field):
    choices = connected_camera_choices()
    field.choices = choices or [('', 'No connected USB cameras found')]
    field.help_text = ('Only connected USB cameras are listed. Reload after connecting a camera.' if choices else
                       'Connect a USB webcam and reload. On Linux, check access to /dev/video devices.')
