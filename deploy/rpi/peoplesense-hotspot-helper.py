#!/usr/bin/python3 -I
"""Root-owned, narrowly scoped NetworkManager adapter. No Django imports.

Install this file as /usr/local/libexec/peoplesense-hotspot (root:root 0755).
Only its fixed wlan0 profile can be changed. Credentials arrive on stdin,
never as command arguments, and are stored only in NM's root-only keyfile.
"""
import ipaddress
import json
import os
from pathlib import Path
import platform
import signal
import stat
import subprocess
import sys
import tempfile

INTERFACE = 'wlan0'
ADDRESS = '10.0.0.1/24'
PROFILE_UUID = 'c219e963-2bda-4e69-a191-643d60934007'
PROFILE_NAME = 'PeopleSense hotspot'
KEYFILE = Path('/etc/NetworkManager/system-connections/peoplesense-ap.nmconnection')
NMCLI = '/usr/bin/nmcli'


class HotspotError(ValueError):
    pass


def validate(request):
    if not isinstance(request, dict) or set(request) != {'ssid', 'protected', 'password'}:
        raise HotspotError('Invalid hotspot request.')
    ssid, protected, password = request['ssid'], request['protected'], request['password']
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode('utf-8')) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise HotspotError('AP name must contain 1–32 UTF-8 bytes without control characters.')
    if type(protected) is not bool or not isinstance(password, str):
        raise HotspotError('Invalid hotspot security settings.')
    if protected and (not 8 <= len(password) <= 63 or any(not 32 <= ord(c) <= 126 for c in password)):
        raise HotspotError('AP password must contain 8–63 printable ASCII characters.')
    if not protected and password:
        raise HotspotError('An open hotspot must not include a password.')
    return ssid, protected, password


def keyfile(request):
    ssid, protected, password = validate(request)
    # The keyfile accepts SSID bytes as a semicolon-delimited integer list.
    # This preserves UTF-8, backslashes and punctuation without INI injection.
    ssid_bytes = ''.join(f'{byte};' for byte in ssid.encode('utf-8'))
    result = (f'[connection]\nid={PROFILE_NAME}\nuuid={PROFILE_UUID}\ntype=wifi\n'
              f'interface-name={INTERFACE}\nautoconnect=true\nautoconnect-priority=999\n\n'
              f'[wifi]\nmode=ap\nband=bg\nchannel=6\nssid={ssid_bytes}\n'
              'hidden=false\nap-isolation=1\n')
    if protected:
        result += 'security=802-11-wireless-security\n'
        escaped = password.replace('\\', '\\\\').replace(' ', '\\s')
        result += ('\n[wifi-security]\nkey-mgmt=wpa-psk\nproto=rsn\npairwise=ccmp\ngroup=ccmp\n'
                   f'psk={escaped}\npsk-flags=0\n')
    result += (f'\n[ipv4]\nmethod=shared\naddress1={ADDRESS}\nnever-default=true\n'
               '\n[ipv6]\nmethod=disabled\n')
    return result.encode('utf-8')


def nmcli(*arguments, timeout=10):
    try:
        result = subprocess.run([NMCLI, '--wait', str(timeout), *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout + 2, check=False,
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HotspotError('NetworkManager is unavailable or did not respond.') from error
    if result.returncode:
        # Never echo backend errors or commands that could expose credentials.
        raise HotspotError('NetworkManager could not apply the hotspot. Check wlan0, Wi-Fi country and NetworkManager logs.')
    return result.stdout.strip()


def atomic_write(path, content):
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat()
    if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise HotspotError('The NetworkManager profile directory must be owned and writable only by root.')
    if path.is_symlink():
        raise HotspotError('Refusing a symbolic link at the hotspot profile path.')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.peoplesense-', delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            file.write(content); file.flush(); os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def active_state():
    values = nmcli('-g', 'GENERAL.CON-UUID,IP4.ADDRESS', 'device', 'show', INTERFACE).splitlines()
    uuid = values[0] if values else '--'
    active = uuid == PROFILE_UUID and ADDRESS in values[1:]
    return uuid, active


def preflight():
    model = Path('/proc/device-tree/model')
    if platform.system() != 'Linux' or not model.exists() or not model.read_text().startswith('Raspberry Pi 4'):
        raise HotspotError('Hotspot provisioning requires a Raspberry Pi 4.')
    if nmcli('-g', 'GENERAL.NM-MANAGED', 'device', 'show', INTERFACE) != 'yes':
        raise HotspotError('NetworkManager must manage wlan0.')
    if nmcli('-g', 'WIFI-PROPERTIES.AP', 'device', 'show', INTERFACE) != 'yes':
        raise HotspotError('wlan0 does not support access point mode.')
    # Do not create an ambiguous subnet on a connected Ethernet/other interface.
    interfaces = nmcli('-g', 'DEVICE', 'device', 'status').splitlines()
    hotspot_network = ipaddress.ip_network(ADDRESS, strict=False)
    for interface in interfaces:
        if interface in {INTERFACE, 'lo'}:
            continue
        for address in nmcli('-g', 'IP4.ADDRESS', 'device', 'show', interface).splitlines():
            if address and address != '--':
                try:
                    overlaps = ipaddress.ip_interface(address).network.overlaps(hotspot_network)
                except ValueError:
                    continue
                if overlaps:
                    raise HotspotError('Another interface uses 10.0.0.0/24. Change that network before enabling this hotspot.')


def apply(request):
    content = keyfile(request)
    preflight()
    if KEYFILE.is_symlink():
        raise HotspotError('Refusing a symbolic link at the hotspot profile path.')
    previous = KEYFILE.read_bytes() if KEYFILE.exists() else None
    if previous is not None:
        # Never overwrite a foreign profile placed at our fixed file path.
        import configparser
        parsed = configparser.ConfigParser(interpolation=None)
        try:
            parsed.read_string(previous.decode('utf-8'))
            owned = parsed.get('connection', 'uuid') == PROFILE_UUID
        except (UnicodeError, configparser.Error):
            owned = False
        if not owned:
            raise HotspotError('The hotspot keyfile belongs to another connection.')
    previous_uuid, active = active_state()
    if previous == content and active:
        return {'active': True, 'address': '10.0.0.1'}
    try:
        atomic_write(KEYFILE, content)
        nmcli('connection', 'load', str(KEYFILE))
        nmcli('radio', 'wifi', 'on')
        nmcli('connection', 'up', 'uuid', PROFILE_UUID, 'ifname', INTERFACE, timeout=25)
        if not active_state()[1]:
            raise HotspotError('The hotspot did not acquire 10.0.0.1. Check NetworkManager logs.')
    except HotspotError:
        # Restore the previous managed profile/connection after an apply failure.
        try:
            if previous is not None:
                atomic_write(KEYFILE, previous)
                nmcli('connection', 'load', str(KEYFILE))
            else:
                KEYFILE.unlink(missing_ok=True)
                nmcli('connection', 'delete', 'uuid', PROFILE_UUID)
            if previous_uuid not in {'', '--'}:
                nmcli('connection', 'up', 'uuid', previous_uuid, 'ifname', INTERFACE, timeout=15)
        except HotspotError:
            pass
        raise
    return {'active': True, 'address': '10.0.0.1'}


def main():
    try:
        if len(sys.argv) != 1 or os.geteuid() != 0:
            raise HotspotError('Run the installed hotspot helper through the restricted sudo rule.')
        import fcntl
        def deadline(*_):
            raise HotspotError('Hotspot update timed out. Check NetworkManager and Wi-Fi country.')
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(65)
        # One adapter owner, even if a second worker starts before its DB lease.
        with open('/run/peoplesense-hotspot.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise HotspotError('Another hotspot update is in progress.') from error
            data = sys.stdin.buffer.read(4097)
            if len(data) > 4096:
                raise HotspotError('Hotspot request is too large.')
            try:
                request = json.loads(data)
            except (ValueError, UnicodeError) as error:
                raise HotspotError('Invalid hotspot request.') from error
            print(json.dumps(apply(request)))
    except Exception as error:
        message = str(error) if isinstance(error, HotspotError) else 'Hotspot helper failed. Check the installed adapter and NetworkManager.'
        print(json.dumps({'active': False, 'error': message}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
