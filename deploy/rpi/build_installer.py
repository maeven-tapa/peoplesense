"""Build a self-contained installer from a clean allowlist of application files."""
import base64
import hashlib
import io
from pathlib import Path
import textwrap
import zipfile

ROOT = Path(__file__).resolve().parents[2]
APP_DIRS = ('hardware', 'networking', 'peoplesense', 'portal', 'static', 'templates', 'vision')
ROOT_FILES = ('main.py', 'manage.py', 'requirements.txt')
DEPLOY_FILES = ('README.md', '.env.example', 'nginx.conf',
                'peoplesense-pigpio.service', 'peoplesense-hardware.service',
                'peoplesense-web.service', 'peoplesense-hotspot.service',
                'peoplesense-hotspot-helper.py', 'peoplesense-hotspot.sudoers', 'install_pigpio.py')


def build(output=None):
    files = [ROOT / name for name in ROOT_FILES]
    files.extend(ROOT / 'deploy/rpi' / name for name in DEPLOY_FILES)
    for directory in APP_DIRS:
        files.extend(path for path in (ROOT / directory).rglob('*') if path.is_file()
                     and not path.is_symlink() and '__pycache__' not in path.parts
                     and path.suffix.lower() not in ('.pyc', '.pyo', '.log', '.sqlite3'))
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(files):
            if path.is_symlink():
                raise ValueError(f'Refusing symbolic-link source: {path}')
            name = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            if path.suffix.lower() in ('.py', '.txt', '.md', '.html', '.css', '.js', '.service', '.sudoers', '.conf') or path.name == '.env.example':
                content = content.replace(b'\r\n', b'\n')
            entry = zipfile.ZipInfo(name, date_time=(2026, 10, 7, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, content)
    payload = archive_bytes.getvalue()
    encoded = base64.b85encode(payload).decode('ascii')
    literal = '(\n' + '\n'.join('    ' + repr(chunk) for chunk in textwrap.wrap(encoded, 100, break_on_hyphens=False)) + '\n)'
    template = Path(__file__).with_name('installer_template.py').read_text(encoding='utf-8')
    script = template.replace("'__PAYLOAD_SHA256__'", repr(hashlib.sha256(payload).hexdigest()))
    script = script.replace('__PAYLOAD_LITERAL__', literal)
    compile(script, 'setup_peoplesense.py', 'exec')
    output = Path(output) if output else ROOT / 'setup_peoplesense.py'
    output.write_text(script, encoding='utf-8', newline='\n')
    print(f'Built {output}: {len(files)} application files, {len(payload):,} bundled bytes.')
    return output


if __name__ == '__main__':
    build()
