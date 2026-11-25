#!/usr/bin/env python3
"""Install the pinned Pi 4 PWM daemon (not packaged in Raspberry Pi OS Trixie)."""
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tempfile
import urllib.request
import zipfile

SOURCE_URL = 'https://codeload.github.com/joan2937/pigpio/zip/refs/tags/v79'
SOURCE_SHA256 = 'd7dfe73a7a5d9d170ccec12aa5968c524b0b279005a3fcf95ce30588aefceb1b'


def extract_source(raw, destination):
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise RuntimeError('pigpio source checksum mismatch.')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            relative = PurePosixPath(entry.filename)
            if (relative.is_absolute() or not relative.parts or relative.parts[0] != 'pigpio-79'
                    or any(part == '..' or ':' in part or '\\' in part for part in relative.parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise RuntimeError('Unsafe pigpio source path.')
            target = Path(destination).joinpath(*relative.parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))
    return Path(destination) / 'pigpio-79'


def install():
    if not hasattr(os, 'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Run with sudo on the Raspberry Pi 4.')
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as response:
        raw = response.read()
    with tempfile.TemporaryDirectory(prefix='peoplesense-pigpio-') as directory:
        source = extract_source(raw, directory)
        # Link the daemon directly, avoiding global Python installs and shared-library paths.
        subprocess.run(['gcc', '-O3', '-pthread', '-o', 'pigpiod', 'pigpiod.c', 'pigpio.c',
                        'command.c', '-lrt'], cwd=source, check=True)
        subprocess.run(['install', '-o', 'root', '-g', 'root', '-m', '755',
                        str(source / 'pigpiod'), '/usr/local/bin/pigpiod'], check=True)
    print('Installed verified pigpio v79 daemon to /usr/local/bin/pigpiod.', flush=True)


if __name__ == '__main__':
    install()
