"""Factory reset of appliance-owned records and files; never formats the host."""
import os
import shutil
import time
from pathlib import Path

from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from .models import Alert, AreaObservation, Detection, Device, HardwareState, HotspotState, PersonStay, PortalSettings
from .reset_lock import device_access


class ResetError(ValueError):
    pass

CAMERA_STOP_TIMEOUT = 15


def _owned_path(path, *, directory=False):
    """Refuse broad, overlapping, or redirected deletion targets."""
    runtime = Path(settings.BASE_DIR).resolve() / 'runtime'
    candidate = Path(path).absolute()
    if runtime.is_symlink() or candidate.is_symlink():
        raise ResetError('Reset requires ordinary folders inside the device runtime folder. Check the configured storage paths.')
    resolved = candidate.resolve()
    if resolved == runtime or not resolved.is_relative_to(runtime):
        raise ResetError('Reset requires capture, camera, and AP key storage inside the device runtime folder.')
    current = candidate
    while current != runtime and current != current.parent:
        if current.is_symlink():
            raise ResetError('Reset cannot erase storage through a symbolic link.')
        current = current.parent
    if candidate.exists() and (candidate.is_dir() != directory):
        raise ResetError('Device storage has an unexpected file type. Correct it before resetting.')
    return resolved


def _storage_paths():
    captures = _owned_path(settings.SURVEILLANCE_CAPTURE_DIR, directory=True)
    cameras = _owned_path(settings.CAMERA_RUNTIME_DIR, directory=True)
    key = _owned_path(settings.AP_KEY_FILE)
    runtime = Path(settings.BASE_DIR).resolve() / 'runtime'
    protected = [Path(settings.VISION_MODEL_DIR).resolve(), Path(settings.DATABASES['default']['NAME']).resolve(),
                 runtime/'network', runtime/'hardware', runtime/'reset.guard']
    for folder in (captures, cameras):
        if any(target == folder or target.is_relative_to(folder) or folder.is_relative_to(target) for target in protected):
            raise ResetError('Reset storage overlaps the application database or detection models.')
    if captures == cameras or captures.is_relative_to(cameras) or cameras.is_relative_to(captures):
        raise ResetError('Camera and capture storage must use separate folders.')
    if key == cameras or key == captures or key in protected:
        raise ResetError('AP key storage overlaps other device files.')
    return captures, cameras, key


def _process_alive(pid):
    if not pid:
        return False
    if pid == os.getpid():
        raise ResetError('The camera worker cannot run inside the web server during reset.')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)  # Synchronize only; never terminate a PID.
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            raise ResetError('Unable to confirm that the camera process stopped. Restart the camera service and retry.')
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        # Reap this web process's exited child before probing the PID. Otherwise
        # an unreaped Linux zombie still passes kill(pid, 0) forever.
        if os.waitpid(pid, os.WNOHANG)[0]:
            return False
    except ChildProcessError:
        pass
    try:
        # A worker launched by a previous web process may be awaiting its own
        # parent's reap; it no longer owns files or the camera when zombie.
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists() and stat.read_text().rsplit(')', 1)[-1].split()[0] == 'Z':
            return False
    except (OSError, IndexError):
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stop_cameras():
    with transaction.atomic():
        # The same write lock used by capture persistence settles in-flight
        # pictures before revocation. A starting worker cannot claim a PID later.
        Device.objects.all().update(worker_token=None, worker_status='stopped', visible_tracks=0, worker_fps=0, status='offline')
        pids = set(Device.objects.exclude(worker_pid=0).values_list('worker_pid', flat=True))
    deadline = time.monotonic() + CAMERA_STOP_TIMEOUT
    while pids:
        pids = {pid for pid in pids if _process_alive(pid)}
        if not pids:
            break
        if time.monotonic() >= deadline:
            raise ResetError('The camera is still stopping. No reset was completed. Wait a moment and try again.')
        time.sleep(.1)


def _erase_files(captures, cameras, key):
    for folder in (captures, cameras):
        # Resolve and validate the final absolute target immediately before any
        # recursive deletion. Models, source files, and host folders are excluded.
        folder = _owned_path(folder, directory=True)
        if folder.exists():
            shutil.rmtree(folder)
    key = _owned_path(key)
    key.unlink(missing_ok=True)


def _reset_workers():
    hardware = HardwareState.load()
    defaults = HardwareState()
    keep = {'id', 'worker_token', 'worker_last_seen', 'worker_ready', 'revision', 'applied_revision'}
    values = {field.name: getattr(defaults, field.name) for field in HardwareState._meta.fields if field.name not in keep}
    HardwareState.objects.filter(pk=hardware.pk).update(**values, revision=F('revision') + 1)
    hotspot = HotspotState.load()
    HotspotState.objects.filter(pk=hotspot.pk).update(revision=F('revision') + 1,
        changed_at=timezone.now(), status='pending' if settings.HOTSPOT_ENABLED else 'stopped',
        error='', applied_ssid='', applied_protected=False)


def factory_reset():
    with device_access(exclusive=True, timeout=2):
        captures, cameras, key = _storage_paths()  # Preflight before stopping anything.
        _stop_cameras()
        try:
            with transaction.atomic():
                if connection.vendor == 'sqlite':
                    with connection.cursor() as cursor:
                        cursor.execute('PRAGMA secure_delete = ON')
                Alert.objects.all().delete()
                Detection.objects.all().delete()
                PersonStay.objects.all().delete()
                AreaObservation.objects.all().delete()
                PortalSettings.objects.all().delete()
                Device.objects.all().delete()
                LogEntry.objects.all().delete()
                Session.objects.all().delete()
                get_user_model().objects.all().delete()
                Group.objects.all().delete()
                camera = Device.objects.create(name='Entrance camera', location='Main entrance', source_type='usb',
                    camera_source='0', ip_address='10.0.0.1' if settings.HOTSPOT_ENABLED else '127.0.0.1', temperature_enabled=False)
                PortalSettings.objects.create(active_device=camera)
                get_user_model().objects.create_user(username='admin', password='12345678', is_staff=True)
                _reset_workers()
                _erase_files(captures, cameras, key)
        except OSError as error:
            # Filesystems cannot roll back a deletion. Retain database records
            # and report failure; never claim success with pictures left behind.
            raise ResetError('Reset could not finish erasing device files. Some files may already be removed. Check storage permissions and retry; the camera is stopped.') from error
