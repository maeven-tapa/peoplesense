"""Cross-process reader/writer gate for appliance reset, including Windows."""
import contextlib
import contextvars
import os
import time
from pathlib import Path

from django.conf import settings


class ResetBusy(ValueError):
    pass


_owned = contextvars.ContextVar('appliance_access_lock', default=None)


def _windows_lock(file, exclusive):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                    ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.POINTER(Overlapped)]
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(file.fileno()))
    overlap = Overlapped()
    if not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0, ctypes.byref(overlap)):
        error = ctypes.get_last_error()
        if error in {32, 33, 997}:
            raise BlockingIOError('The appliance is busy.')
        raise ctypes.WinError(error)
    return lambda: kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlap))


@contextlib.contextmanager
def device_access(*, exclusive=False, timeout=0):
    owned = _owned.get()
    if owned is not None:
        if exclusive and not owned:
            raise ResetBusy('Retry the reset after the current request completes.')
        yield
        return
    directory = Path(settings.BASE_DIR) / 'runtime'
    if directory.is_symlink():
        raise ResetBusy('The device runtime folder must not be a symbolic link.')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'reset.guard'
    if path.is_symlink():
        raise ResetBusy('The device reset lock must not be a symbolic link.')
    # The lock file contains no data. Kernel locks disappear if a process crashes.
    with path.open('a+b') as file:
        if not path.stat().st_size:
            file.write(b'\0')
            file.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    unlock = _windows_lock(file, exclusive)
                else:
                    import fcntl
                    fcntl.flock(file, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    unlock = lambda: fcntl.flock(file, fcntl.LOCK_UN)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ResetBusy('Device reset is in progress, or another request is still finishing. Try again shortly.')
                time.sleep(.05)
        token = _owned.set(exclusive)
        try:
            yield
        finally:
            _owned.reset(token)
            unlock()


class ResetGuardMiddleware:
    """Drain requests before reset and prevent old requests recreating wiped data."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.http import HttpResponse
        resetting = request.method == 'POST' and request.path == '/settings/factory-reset/'
        try:
            with device_access(exclusive=resetting, timeout=2 if resetting else 0):
                return self.get_response(request)
        except (ResetBusy, OSError):
            return HttpResponse('The device is busy. Wait a moment, then reload the portal.', status=503,
                                headers={'Retry-After': '3', 'Cache-Control': 'no-store'})
