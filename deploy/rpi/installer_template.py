#!/usr/bin/env python3
"""Install or repair PeopleSense. Run on Pi 4: python3 ~/setup_peoplesense.py.

Target: Raspberry Pi OS Trixie 64-bit / Python 3.13 (also Bookworm / Python 3.11).
Requires internet for apt, pip and model downloads. The hotspot replaces wlan0's
client connection. Use the local Pi terminal or Ethernet for installation.
GPIO workers start with the configured mount targets, initially 90 degrees.
"""
import argparse
import base64
import contextlib
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

PAYLOAD_SHA256 = '__PAYLOAD_SHA256__'
PAYLOAD = __PAYLOAD_LITERAL__
APP = Path('/opt/peoplesense')
MARKER = APP / '.peoplesense-installer.json'
BACKUP_DIR = Path('/opt/peoplesense-backups')
LOG = APP / 'logs/setup.log'
STARTUP_LOG = Path(__file__).resolve().with_name('setup-startup.log')
SERVICE_NAMES = ('peoplesense-hotspot', 'peoplesense-hardware',
                 'peoplesense-web', 'peoplesense-pigpio')
LOG_STREAM = None
ACTIVE_LOG = None


def open_install_log(path=None):
    global LOG_STREAM, ACTIVE_LOG
    target = LOG if path is None else Path(path)
    regular_target(target, APP if target == LOG else target.parent)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    if hasattr(os, 'fchmod'):
        os.fchmod(descriptor, 0o600)
    stream = os.fdopen(descriptor, 'a', buffering=1, encoding='utf-8')
    if LOG_STREAM is not None:
        LOG_STREAM.close()
    LOG_STREAM = stream
    ACTIVE_LOG = target


def emit(message):
    if LOG_STREAM is not None:
        LOG_STREAM.write(str(message) + '\n')
        LOG_STREAM.flush()
    try:
        print(message, flush=True)
    except OSError:
        # Keep installation alive if wlan0 activation disconnects SSH.
        pass


def report_failure(error):
    location = f'Log: {ACTIVE_LOG}' if LOG_STREAM is not None else 'See the terminal output above.'
    emit(f'INSTALLATION STOPPED: {error}\nRe-run the same file after fixing the error. '
         'Existing database, pictures and secrets are preserved.\n' + location)


def run(arguments, *, cwd=None, env=None, input_text=None, check=True):
    arguments = [str(value) for value in arguments]
    emit('$ ' + shlex.join(arguments))
    if input_text is not None:
        result = subprocess.run(arguments, cwd=cwd, env=env, input=input_text,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        emit(result.stdout.rstrip())
    else:
        process = subprocess.Popen(arguments, cwd=cwd, env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for line in process.stdout:
            emit(line.rstrip())
        process.stdout.close()
        result = subprocess.CompletedProcess(arguments, process.wait())
    if check and result.returncode:
        raise RuntimeError(f'Command failed (exit {result.returncode}): {shlex.join(arguments)}')
    return result.returncode


def capture(arguments):
    result = subprocess.run(arguments, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False)
    return result.stdout.strip() if result.returncode == 0 else ''


def download_step(arguments, **kwargs):
    """Retry network-dependent commands; later steps run only after success."""
    for attempt in range(1, 4):
        try:
            return run(arguments, **kwargs)
        except RuntimeError:
            if attempt == 3:
                raise
            emit(f'Download/install step failed; retrying ({attempt}/3).')
            time.sleep(2 * attempt)


def decode_payload():
    raw = base64.b85decode(PAYLOAD)
    if hashlib.sha256(raw).hexdigest() != PAYLOAD_SHA256:
        raise RuntimeError('Embedded application checksum failed. Transfer the installer again.')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Embedded application archive is damaged.')
        for entry in archive.infolist():
            safe_archive_path(entry.filename)
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise RuntimeError('Symbolic links are not allowed in the application bundle.')
    return raw


def safe_archive_path(name):
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or any(
            value in ('..', '') or ':' in value or '\\' in value for value in relative.parts):
        raise RuntimeError(f'Unsafe application bundle path: {name}')
    return relative


def preflight():
    if platform.system() != 'Linux' or platform.machine().lower() not in ('aarch64', 'arm64'):
        raise RuntimeError('Run this file on Raspberry Pi OS 64-bit on the Pi, not Windows.')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text(encoding='utf-8').splitlines()
                   if '=' in line and not line.startswith('#'))
    codename = release.get('VERSION_CODENAME', '').strip('"')
    versions = {'bookworm': (3, 11), 'trixie': (3, 13)}
    if codename not in versions:
        raise RuntimeError('Use Raspberry Pi OS Trixie 64-bit or Bookworm 64-bit.')
    if sys.version_info[:2] != versions[codename]:
        raise RuntimeError(f'Use the default OS Python {".".join(map(str, versions[codename]))} for {codename}.')
    model = Path('/proc/device-tree/model').read_bytes().rstrip(b'\x00').decode()
    if not model.startswith('Raspberry Pi 4 '):
        raise RuntimeError(f'This GPIO configuration targets Raspberry Pi 4; found: {model}')
    if not Path('/sys/class/net/wlan0').exists():
        raise RuntimeError('wlan0 was not found. This installer uses the Pi built-in Wi-Fi.')
    for program in ('apt-get', 'systemctl', 'raspi-config', 'runuser'):
        if not shutil.which(program, path='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'):
            raise RuntimeError(f'Required Raspberry Pi OS utility is missing: {program}')
    emit(f'Platform checked: Raspberry Pi 4 / {codename} / 64-bit / Python {sys.version_info.major}.{sys.version_info.minor}.')


def country_code(code, country_file=Path('/usr/share/zoneinfo/iso3166.tab')):
    code = code.strip().upper()
    valid = {line.split()[0] for line in country_file.read_text(encoding='utf-8').splitlines()
             if line and not line.startswith('#')}
    if not re.fullmatch('[A-Z]{2}', code) or code not in valid:
        raise RuntimeError('Enter a valid two-letter Wi-Fi country code, for example PH or US.')
    return code


def configure_country(requested):
    existing = capture(['raspi-config', 'nonint', 'get_wifi_country'])
    # A fresh OS can report the world regulatory domain (00), not a country.
    if existing:
        try:
            existing = country_code(existing)
        except RuntimeError:
            existing = ''
    if requested:
        selected = country_code(requested)
    elif existing:
        selected = existing
    elif sys.stdin.isatty():
        selected = country_code(input('Wi-Fi country where the Pi is used (e.g. PH): '))
    else:
        raise RuntimeError('Wi-Fi country is unset. Run again with --wifi-country YOUR_CODE.')
    if existing != selected:
        run(['raspi-config', 'nonint', 'do_wifi_country', selected])
    emit(f'Wi-Fi country: {selected}')


def regular_target(target, root):
    target.relative_to(root)
    if root.is_symlink():
        raise RuntimeError(f'Refusing symbolic-link installation directory: {root}')
    for part in (target, *target.parents):
        if part.is_symlink():
            raise RuntimeError(f'Refusing symbolic-link installation path: {part}')
        if part == root:
            break
    if target.exists() and not target.is_file():
        raise RuntimeError(f'Refusing to overwrite a non-file: {target}')


def write_state(status, **extra):
    regular_target(MARKER, APP)
    data = json.loads(MARKER.read_text(encoding='utf-8')) if MARKER.exists() else {}
    data.update(installer='PeopleSense', payload_sha256=PAYLOAD_SHA256, status=status, **extra)
    descriptor, name = tempfile.mkstemp(prefix='.peoplesense-state-', suffix='.tmp', dir=APP)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(data, indent=2) + '\n')
        temporary.chmod(0o644)
        os.replace(temporary, MARKER)
    finally:
        temporary.unlink(missing_ok=True)


def backup_installation():
    """Keep source, settings, secrets and data before accepting another bundle."""
    if BACKUP_DIR.is_symlink() or BACKUP_DIR.exists() and not BACKUP_DIR.is_dir():
        raise RuntimeError('The upgrade backup path must be a normal directory.')
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    BACKUP_DIR.chmod(0o700)
    files = []
    for path in APP.rglob('*'):
        relative = path.relative_to(APP)
        if relative.parts[0] in {'.venv', 'staticfiles'} or '__pycache__' in relative.parts:
            continue
        if path.is_symlink():
            raise RuntimeError(f'Refusing a symbolic link in the upgrade backup: {path}')
        if path.is_file() and relative.as_posix() not in {'db.sqlite3-wal', 'db.sqlite3-shm', 'db.sqlite3-journal'}:
            files.append(path)
    required = sum(path.stat().st_size for path in files) + 32 * 1024 * 1024
    if shutil.disk_usage(BACKUP_DIR).free < required:
        raise RuntimeError('Not enough free space for the upgrade backup. Free space and retry; nothing was replaced.')
    descriptor, name = tempfile.mkstemp(
        prefix='before-update-' + time.strftime('%Y%m%d-%H%M%S-'), suffix='.tar.gz', dir=BACKUP_DIR)
    os.close(descriptor)
    target = Path(name)
    target.chmod(0o600)
    try:
        with tempfile.TemporaryDirectory(prefix='peoplesense-db-') as temporary:
            database = APP / 'db.sqlite3'
            snapshot = Path(temporary) / 'db.sqlite3'
            if database in files:
                # The backup API also includes committed data still in a WAL file.
                with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as source, \
                        contextlib.closing(sqlite3.connect(snapshot)) as destination:
                    source.backup(destination)
            with tarfile.open(target, 'w:gz') as archive:
                for path in sorted(files):
                    relative = path.relative_to(APP).as_posix()
                    if path == database:
                        entry = archive.gettarinfo(str(path), arcname=relative)
                        entry.size = snapshot.stat().st_size
                        with snapshot.open('rb') as handle:
                            archive.addfile(entry, handle)
                    else:
                        archive.add(path, arcname=relative, recursive=False)
        emit(f'Upgrade backup saved: {target}')
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def stop_installed_services():
    active = []
    try:
        for name in SERVICE_NAMES:
            if capture(['systemctl', 'show', '-p', 'LoadState', '--value', name]) == 'loaded':
                if capture(['systemctl', 'is-active', name]) == 'active':
                    active.append(name)
                run(['systemctl', 'stop', name])
        return active
    except Exception:
        for name in reversed(active):
            run(['systemctl', 'start', name], check=False)
        raise


def prepare_directory(*, update=False, repair=False):
    if APP.is_symlink() or APP.exists() and not APP.is_dir():
        raise RuntimeError('Installation path must be a normal directory: /opt/peoplesense.')
    if MARKER.is_symlink():
        raise RuntimeError('Installer marker must not be a symbolic link.')
    if MARKER.exists():
        state = json.loads(MARKER.read_text(encoding='utf-8'))
        if (state.get('installer') != 'PeopleSense' or not isinstance(state.get('payload_sha256'), str)
                or not re.fullmatch('[0-9a-f]{64}', state['payload_sha256'])):
            raise RuntimeError('This directory is not a recognized PeopleSense installation. Nothing was replaced.')
        different_bundle = state['payload_sha256'] != PAYLOAD_SHA256
        if different_bundle:
            emit('A different PeopleSense bundle is installed. Backing it up before updating automatically.')
            active = stop_installed_services()
            try:
                backup = backup_installation()
                write_state('installing', previous_payload_sha256=state['payload_sha256'],
                            upgrade_backup=str(backup),
                            defaults_initialized=bool(state.get('defaults_initialized') or (APP / 'db.sqlite3').exists()))
            except BaseException:
                for name in reversed(active):
                    run(['systemctl', 'start', name], check=False)
                raise
            emit('Updating the existing installation; administrator credentials, camera settings and history are preserved.')
            return True
        if state.get('status') == 'complete' and not repair:
            emit('PeopleSense is installed. Start it with: python3 /opt/peoplesense/main.py\n'
                 'To repair it, run: python3 ~/setup_peoplesense.py --repair')
            return False
        stop_installed_services()
        if repair:
            write_state('installing', defaults_initialized=bool(
                state.get('defaults_initialized') or (APP / 'db.sqlite3').exists()))
    elif APP.exists() and any(APP.iterdir()):
        raise RuntimeError('/opt/peoplesense already contains files. This installer will not replace an existing device.')
    APP.mkdir(parents=True, exist_ok=True, mode=0o755)
    APP.chmod(0o755)
    write_state('installing')
    return True


@contextlib.contextmanager
def installation_lock():
    import fcntl
    with open('/run/peoplesense-main.lock', 'a', encoding='utf-8') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Stop main.py with Ctrl+C before running setup or repair. Another setup may also be running.') from error
        yield


def install_bundle(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            relative = safe_archive_path(entry.filename)
            target = APP.joinpath(*relative.parts)
            regular_target(target, APP)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))
            target.chmod(0o644)
            shutil.chown(target, user='peoplesense', group='peoplesense')
            for directory in target.parents:
                shutil.chown(directory, user='peoplesense', group='peoplesense')
                directory.chmod(0o755)
                if directory == APP:
                    break


def environment_text(template, hostname, addresses):
    hosts = ['localhost', '127.0.0.1', '10.0.0.1', 'peoplesense.local', hostname, hostname + '.local']
    for value in addresses:
        address = ipaddress.ip_address(value)
        if address.version == 4:
            hosts.append(str(address))
    lines = template.replace('replace-with-a-random-secret', secrets.token_urlsafe(48)).splitlines()
    return '\n'.join('DJANGO_ALLOWED_HOSTS=' + ','.join(dict.fromkeys(hosts))
                     if line.startswith('DJANGO_ALLOWED_HOSTS=') else line for line in lines) + '\n'


def configure_environment():
    target = APP / '.env.rpi'
    regular_target(target, APP)
    if not target.exists():
        template = (APP / 'deploy/rpi/.env.example').read_text(encoding='utf-8')
        target.write_text(environment_text(template, socket.gethostname(), capture(['hostname', '-I']).split()), encoding='utf-8')
    target.chmod(0o600)
    shutil.chown(target, user='peoplesense', group='peoplesense')
    env = {**os.environ, 'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}
    for line in target.read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            if not key.startswith(('DJANGO_', 'PEOPLESENSE_')):
                raise RuntimeError(f'Unexpected deployment environment key: {key}')
            env[key] = value
    if not env.get('DJANGO_SECRET_KEY') or env['DJANGO_SECRET_KEY'] == 'replace-with-a-random-secret':
        raise RuntimeError('The deployment environment needs a generated Django secret.')
    emit('Deployment environment ready; existing secrets are preserved.')
    return env


def prepare_service_logs():
    directory = APP / 'logs'
    regular_target(directory / 'web.log', APP)
    directory.mkdir(exist_ok=True, mode=0o750)
    directory.chmod(0o750)
    shutil.chown(directory, user='peoplesense', group='peoplesense')
    for name in ('web.log', 'hardware.log', 'hotspot.log', 'pigpio.log',
                 'nginx-access.log', 'nginx-error.log'):
        target = directory / name
        regular_target(target, APP)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o640)
        os.close(descriptor)
        target.chmod(0o640)
        shutil.chown(target, user='peoplesense', group='peoplesense')


def install_services():
    prepare_service_logs()
    source = APP / 'deploy/rpi'
    run(['install', '-d', '-o', 'root', '-g', 'root', '-m', '755', '/usr/local/libexec'])
    run(['install', '-o', 'root', '-g', 'root', '-m', '755',
         source / 'peoplesense-hotspot-helper.py', '/usr/local/libexec/peoplesense-hotspot'])
    run(['install', '-o', 'root', '-g', 'root', '-m', '440',
         source / 'peoplesense-hotspot.sudoers', '/etc/sudoers.d/peoplesense-hotspot'])
    run(['visudo', '-cf', '/etc/sudoers.d/peoplesense-hotspot'])
    for name in SERVICE_NAMES:
        run(['install', '-o', 'root', '-g', 'root', '-m', '644',
             source / (name + '.service'), '/etc/systemd/system/' + name + '.service'])
    run(['install', '-o', 'root', '-g', 'root', '-m', '644',
         source / 'nginx.conf', '/etc/nginx/sites-available/peoplesense'])
    link = Path('/etc/nginx/sites-enabled/peoplesense')
    if link.is_symlink() and os.readlink(link) != '/etc/nginx/sites-available/peoplesense':
        raise RuntimeError('The PeopleSense Nginx link points to an unexpected site.')
    if not link.is_symlink():
        if link.exists():
            raise RuntimeError('The PeopleSense Nginx site path is already a regular file.')
        link.symlink_to('/etc/nginx/sites-available/peoplesense')
    default = Path('/etc/nginx/sites-enabled/default')
    if default.is_symlink():
        default.unlink()
    if capture(['systemctl', 'show', '-p', 'LoadState', '--value', 'pigpiod']) == 'loaded':
        run(['systemctl', 'disable', '--now', 'pigpiod'])
    run(['systemctl', 'daemon-reload'])
    run(['nginx', '-t'])
    emit('Starting GPIO services; an attached mount initially centers at 90 degrees.')
    run(['systemctl', 'disable', *SERVICE_NAMES])
    run(['systemctl', 'enable', '--now', 'NetworkManager', 'nginx'])
    run(['systemctl', 'start', 'peoplesense-pigpio', 'peoplesense-hardware', 'peoplesense-web'])
    run(['systemctl', 'reload', 'nginx'])
    for name in ('peoplesense-pigpio', 'peoplesense-hardware', 'peoplesense-web', 'nginx'):
        run(['systemctl', 'is-active', '--quiet', name])
    wait_for_http('http://127.0.0.1/', host='10.0.0.1', seconds=45)


def wait_for_http(url, *, host=None, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, headers={'Host': host} if host else {})
            # Local portal checks should not use a system HTTP proxy.
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise RuntimeError(f'Portal did not respond at {url}. Check the web, Nginx and hotspot logs.')


def install(raw, country):
    configure_country(country)
    apt_env = {**os.environ, 'DEBIAN_FRONTEND': 'noninteractive',
               'APT_LISTCHANGES_FRONTEND': 'none', 'NEEDRESTART_MODE': 'l'}
    apt_options = ['-o', 'Acquire::Retries=3', '-o', 'DPkg::Lock::Timeout=120']
    emit('1/6 Installing Linux packages and USB camera utilities.')
    download_step(['apt-get', 'update', *apt_options], env=apt_env)
    # DHT11 uses RPi.GPIO bit-banging, not libgpiod's obsolete v1 PulseIn ABI.
    download_step(['apt-get', 'install', '-y', *apt_options,
         '-o', 'Dpkg::Options::=--force-confold', 'python3-venv', 'python3-dev', 'python3-rpi.gpio',
         'build-essential', 'ca-certificates', 'libopenblas0', 'nginx',
         'network-manager', 'dnsmasq-base', 'sudo', 'v4l-utils'], env=apt_env)
    if not capture(['id', '-u', 'peoplesense']):
        run(['useradd', '--system', '--create-home', '--groups', 'video,gpio', 'peoplesense'])
    run(['usermod', '-a', '-G', 'video,gpio', 'peoplesense'])
    emit('2/6 Installing the application and verified Pi GPIO driver.')
    install_bundle(raw)
    download_step(['/usr/bin/python3', APP / 'deploy/rpi/install_pigpio.py'])
    env = configure_environment()
    venv = APP / '.venv'
    if venv.is_symlink():
        raise RuntimeError('Refusing a symbolic-link virtual environment.')
    # Expose the OS-built RPi.GPIO extension for this release's default Python.
    run(['runuser', '-u', 'peoplesense', '--', '/usr/bin/python3', '-m', 'venv', '--system-site-packages', venv])
    python = venv / 'bin/python'
    prefix = ['runuser', '-u', 'peoplesense', '--', python]
    emit('3/6 Downloading Python packages into the Pi virtual environment.')
    pip_options = ['--disable-pip-version-check', '--no-input', '--retries', '5', '--timeout', '60']
    download_step([*prefix, '-m', 'pip', 'install', *pip_options, '--upgrade', 'pip'], cwd=APP)
    download_step([*prefix, '-m', 'pip', 'install', *pip_options, '--prefer-binary',
         '--only-binary=numpy,scipy,opencv-python-headless,ai-edge-litert,cryptography',
         '-r', 'requirements.txt'], cwd=APP)
    # System-site-packages exposes the OS GPIO driver, plus unrelated Debian
    # package metadata. Validate PeopleSense's imports and models instead of
    # running a global pip check against the entire OS Python installation.
    run([*prefix, '-c', 'import django, cv2, numpy, scipy, flatbuffers, ai_edge_litert, '
         'gpiozero, pigpio, adafruit_dht, RPi.GPIO, gunicorn'], cwd=APP)
    emit('4/6 Preparing the database and detecting connected USB cameras.')
    for command in ('migrate', 'bootstrap_appliance'):
        run([*prefix, 'manage.py', command], cwd=APP, env=env)
    if not json.loads(MARKER.read_text(encoding='utf-8')).get('defaults_initialized'):
        run([*prefix, 'manage.py', 'shell'], cwd=APP, env=env, input_text=
            "from portal.models import Device, PortalSettings\n"
            "Device.objects.filter(pk=PortalSettings.load().active_device_id).update("
            "detection_model='yolox_nano', process_fps=4, preview_fps=30)\n")
        write_state('installing', defaults_initialized=True)
    emit('5/6 Downloading and testing both person detection models.')
    download_step([*prefix, 'manage.py', 'download_models'], cwd=APP, env=env)
    run([*prefix, 'manage.py', 'check_camera_setup'], cwd=APP, env=env)
    run([*prefix, 'manage.py', 'collectstatic', '--noinput'], cwd=APP, env=env)
    run([*prefix, 'manage.py', 'check'], cwd=APP, env=env)
    emit('6/6 Checking application services and the portal before Wi-Fi changes.')
    install_services()
    emit('Enabling PeopleSense-AP on wlan0 now. A Wi-Fi SSH connection may disconnect.')
    run(['systemctl', 'start', 'peoplesense-hotspot'])
    wait_for_http('http://10.0.0.1/')
    addresses = capture(['nmcli', '-g', 'IP4.ADDRESS', 'device', 'show', 'wlan0']).splitlines()
    if '10.0.0.1/24' not in addresses:
        raise RuntimeError('The portal responds, but wlan0 did not receive 10.0.0.1/24.')
    run(['systemctl', 'is-active', '--quiet', 'peoplesense-hotspot'])
    # Installation validates the services, then leaves everyday startup to main.py.
    for name in SERVICE_NAMES:
        run(['systemctl', 'stop', name])
    state = json.loads(MARKER.read_text(encoding='utf-8'))
    write_state('complete', launch_mode='manual')
    if state.get('upgrade_backup'):
        emit('\nUPDATE COMPLETE\nStart: python3 /opt/peoplesense/main.py\nPortal: http://10.0.0.1/\n'
             'Your existing Wi-Fi name, administrator password, camera settings and history were preserved.\n'
             'Sign in with your existing password and press Start camera when ready.\n'
             f'Backup: {state["upgrade_backup"]}\nInstall log: {LOG}')
        return
    emit('\nSETUP COMPLETE\nStart: python3 /opt/peoplesense/main.py\n'
         'Keep main.py running; Ctrl+C stops the app.\nWi-Fi: PeopleSense-AP\nPortal: http://10.0.0.1/\n'
         'Initial login: admin / 12345678\n'
         'Complete Quick setup, change the password and press Start camera.\n'
         'Selected model: YOLOX-Nano. Preview target: 30 FPS. AI target: 4 FPS.\n'
         'Pin signals: pan GPIO18/pin12; tilt GPIO13/pin33; DHT11 GPIO4/pin7.\n'
         f'Install log: {LOG}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wifi-country', help='Two-letter country where the Pi is used, e.g. PH.')
    parser.add_argument('--check', action='store_true', help='Check bundle/platform only; do not install.')
    parser.add_argument('--update', action='store_true',
                        help='Compatibility option; different bundles are backed up and updated automatically.')
    parser.add_argument('--repair', action='store_true',
                        help='Reinstall this bundle and dependencies, preserving existing data and settings.')
    args = parser.parse_args()
    if not args.check and platform.system() == 'Linux' and os.geteuid() != 0:
        os.execv('/usr/bin/sudo', ['sudo', '/usr/bin/python3', str(Path(__file__).resolve()), *sys.argv[1:]])
    try:
        open_install_log(STARTUP_LOG)
        emit(f'PeopleSense setup: {Path(__file__).resolve()}\nStartup log: {STARTUP_LOG}')
    except OSError as error:
        # A read-only USB drive must not prevent installation. Terminal output
        # remains available, and normal logging starts once APP is prepared.
        emit(f'Cannot write startup log: {error}. Continuing with terminal output.')
    raw = decode_payload()
    preflight()
    if args.check:
        emit('Embedded application checksum and platform checks passed. Nothing was installed.')
        return
    if hasattr(signal, 'SIGHUP'):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    with installation_lock():
        if not prepare_directory(update=args.update, repair=args.repair):
            return
        try:
            # Create the marker first: logging must not make a fresh install
            # look like an unmanaged, nonempty application directory.
            open_install_log()
            emit(f'\nPeopleSense Raspberry Pi setup starting. Log: {LOG}')
            install(raw, args.wifi_country)
        except BaseException:
            write_state('incomplete')
            for name in SERVICE_NAMES:
                if capture(['systemctl', 'show', '-p', 'LoadState', '--value', name]) == 'loaded':
                    run(['systemctl', 'stop', name], check=False)
            raise


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        emit('Installation interrupted. Re-run the same file to resume.')
        sys.exit(130)
    except Exception as error:
        report_failure(error)
        sys.exit(1)
