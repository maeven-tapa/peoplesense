"""Low-frequency, unprivileged worker for the appliance's single hotspot."""
import json
import signal
import subprocess
import time
import uuid
from cryptography.fernet import InvalidToken
from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import F
from django.utils import timezone
from hardware.worker import is_pi4
from portal.models import Device, HotspotState, PortalSettings
from portal.wifi_secrets import device_ap_password
from .service import ADDRESS, APPLY_DELAY, HotspotError

HELPER = '/usr/local/libexec/peoplesense-hotspot'


def apply_hotspot(profile):
    try:
        request = {'ssid': profile.wifi_ap_name, 'protected': profile.ap_password_enabled,
                   'password': device_ap_password(profile)}
    except (InvalidToken, ValueError, OSError) as error:
        raise HotspotError('The saved AP password is unavailable. Enter it again in Wi-Fi settings.') from error
    try:
        process = subprocess.run(['/usr/bin/sudo', '-n', HELPER], input=json.dumps(request),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=80, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HotspotError('The hotspot adapter did not respond. Check its service and restricted sudo rule.') from error
    try:
        result = json.loads(process.stdout)
    except (ValueError, UnicodeError):
        raise HotspotError('Install the root-owned hotspot helper and restricted sudo rule from the Pi setup guide.')
    if not isinstance(result, dict):
        raise HotspotError('The hotspot adapter returned an invalid response.')
    if process.returncode or result.get('active') is not True or result.get('address') != ADDRESS:
        # Controlled helper diagnostics only; never include stdout/stderr/PSK.
        message = result.get('error', 'Hotspot apply failed. Check NetworkManager logs.')
        if not isinstance(message, str) or request['password'] and request['password'] in message:
            message = 'Hotspot apply failed. Check NetworkManager logs.'
        raise HotspotError(message[:240])


def ready_request(lease):
    # Settings and its mailbox revision are saved together. Read them in one
    # SQLite snapshot so a concurrent save cannot bypass the reconnect delay.
    with transaction.atomic():
        # Acquire SQLite's write lock before reading, matching the settings
        # save transaction and avoiding a read-to-write upgrade race.
        HotspotState.objects.filter(pk=1).update(revision=F('revision'))
        state = HotspotState.objects.get(pk=1)
        if state.worker_token != lease or (state.changed_at and
                (timezone.now() - state.changed_at).total_seconds() < APPLY_DELAY):
            return None
        profile = PortalSettings.load()
        HotspotState.objects.filter(pk=1, worker_token=lease, revision=state.revision).update(status='applying', error='')
        return state.revision, profile


def run_hotspot(*, max_cycles=None, adapter=apply_hotspot):
    if not settings.HOTSPOT_ENABLED:
        raise HotspotError('Set PEOPLESENSE_HOTSPOT=1 on the Raspberry Pi.')
    if not is_pi4():
        raise HotspotError('Hotspot provisioning requires a Raspberry Pi 4.')
    import fcntl
    directory = settings.BASE_DIR / 'runtime' / 'network'
    directory.mkdir(parents=True, exist_ok=True)
    lock = (directory / 'hotspot.lock').open('a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        lock.close()
        raise HotspotError('Another hotspot worker owns the adapter.') from error
    state = HotspotState.load()
    lease = uuid.uuid4()
    HotspotState.objects.filter(pk=1).update(worker_token=lease, worker_last_seen=timezone.now(), status='pending', error='')
    stopped = False
    previous_handlers = {}
    def stop(*_):
        nonlocal stopped
        stopped = True
    next_check = 0
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, stop)
        cycles = 0
        while not stopped:
            close_old_connections()
            state = HotspotState.objects.get(pk=1)
            if state.worker_token != lease:
                break
            HotspotState.objects.filter(pk=1, worker_token=lease).update(worker_last_seen=timezone.now())
            now = time.monotonic()
            waiting = state.changed_at and (timezone.now() - state.changed_at).total_seconds() < APPLY_DELAY
            changed = state.revision != state.applied_revision
            if not waiting and (now >= next_check or changed and state.status == 'pending'):
                ready = ready_request(lease)
                if ready:
                    revision, profile = ready
                    try:
                        adapter(profile)
                    except HotspotError as error:
                        HotspotState.objects.filter(pk=1, worker_token=lease, revision=revision).update(status='error', error=str(error), worker_last_seen=timezone.now())
                    else:
                        # A save during provisioning must stay pending for its own delay.
                        HotspotState.objects.filter(pk=1, worker_token=lease, revision=revision).update(status='active', error='',
                            applied_revision=revision, applied_ssid=profile.wifi_ap_name,
                            applied_protected=profile.ap_password_enabled, worker_last_seen=timezone.now())
                        if profile.active_device_id:
                            Device.objects.filter(pk=profile.active_device_id).update(ip_address=ADDRESS)
                    next_check = time.monotonic() + 30
            cycles += 1
            if max_cycles and cycles >= max_cycles:
                break
            time.sleep(2)
    finally:
        HotspotState.objects.filter(pk=1, worker_token=lease).update(worker_token=None, status='stopped')
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)
        lock.close()
