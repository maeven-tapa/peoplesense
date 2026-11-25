#!/usr/bin/env python3
"""Run the installed PeopleSense Pi appliance until Ctrl+C. No packages are installed."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP = Path('/opt/peoplesense')
SERVICES = ('peoplesense-pigpio', 'peoplesense-hardware',
            'peoplesense-web', 'peoplesense-hotspot')
SYSTEMCTL = '/usr/bin/systemctl'


def check_installation():
    marker = APP / '.peoplesense-installer.json'
    try:
        state = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise RuntimeError('Run python3 setup_peoplesense.py first, or use --repair to repair an installation.') from error
    if state.get('installer') != 'PeopleSense' or state.get('status') != 'complete':
        raise RuntimeError('Setup is incomplete. Re-run setup_peoplesense.py before starting PeopleSense.')
    for relative in ('manage.py', '.venv/bin/python', '.env.rpi'):
        if not (APP / relative).is_file():
            raise RuntimeError('Installation files are missing. Run python3 setup_peoplesense.py --repair.')


def systemctl(*arguments, check=True):
    result = subprocess.run([SYSTEMCTL, *arguments], check=False)
    if check and result.returncode:
        raise RuntimeError(f'Service command failed. See {APP / "logs"} or run setup_peoplesense.py --repair.')
    return result.returncode


@contextmanager
def launcher_lock():
    # A second terminal must not stop the first terminal's services on exit.
    import fcntl
    with open('/run/peoplesense-main.lock', 'a', encoding='utf-8') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('PeopleSense main.py is already running in another terminal.') from error
        yield


def wait_for_portal(seconds=60):
    deadline = time.monotonic() + seconds
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < deadline:
        request = urllib.request.Request('http://127.0.0.1/', headers={'Host': '10.0.0.1'})
        try:
            with opener.open(request, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError(f'The portal did not start. Check {APP / "logs/web.log"} and {APP / "logs/nginx-error.log"}.')


def run_app():
    with launcher_lock():
        check_installation()
        try:
            print('Starting PeopleSense. Keep this terminal open; Ctrl+C stops the app.', flush=True)
            print(f'Logs: {APP / "logs"}', flush=True)
            systemctl('start', 'NetworkManager', 'nginx', *SERVICES[:-1])
            wait_for_portal()
            print('Starting the Wi-Fi hotspot on wlan0. Use a local terminal or Ethernet.', flush=True)
            systemctl('start', SERVICES[-1])
            systemctl('is-active', '--quiet', *SERVICES)
            print('PeopleSense is running: http://10.0.0.1/\nSign in, then choose My camera → Start camera.', flush=True)
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print('\nStopping PeopleSense...', flush=True)
        finally:
            # Leave the OS network manager and shared web server running.
            systemctl('stop', *reversed(SERVICES))


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true', help='Show installed service status without starting the app.')
    args = parser.parse_args()
    if platform.system() != 'Linux':
        raise RuntimeError('Use main.py on the installed Linux Pi. On Windows, use start.ps1.')
    check_installation()
    if args.status:
        return systemctl('status', '--no-pager', *SERVICES, check=False)
    if os.geteuid() != 0:
        os.execv('/usr/bin/sudo', ['sudo', '/usr/bin/python3', str(Path(__file__).resolve()), *sys.argv[1:]])
    for name in ('SIGTERM', 'SIGHUP'):
        signal.signal(getattr(signal, name), interrupted)
    run_app()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as error:
        print(f'PeopleSense could not start: {error}', file=sys.stderr)
        sys.exit(1)
