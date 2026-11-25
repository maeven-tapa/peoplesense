"""Safe installer tests: no apt, GPIO, services or networking are changed."""
import base64
import contextlib
import configparser
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
daemon_spec = importlib.util.spec_from_file_location('pi_daemon_under_test', ROOT / 'deploy/rpi/install_pigpio.py')
daemon = importlib.util.module_from_spec(daemon_spec)
daemon_spec.loader.exec_module(daemon)
spec = importlib.util.spec_from_file_location('pi_installer_under_test', ROOT / 'setup_peoplesense.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name) / 'appliance'
        self.app.mkdir()
        for attribute, value in (('APP', self.app), ('MARKER', self.app / '.peoplesense-installer.json'),
                                 ('BACKUP_DIR', Path(self.temp.name) / 'backups'),
                                 ('LOG', self.app / 'logs/setup.log'),
                                 ('STARTUP_LOG', Path(self.temp.name) / 'setup-startup.log')):
            p = patch.object(installer, attribute, value)
            p.start()
            self.addCleanup(p.stop)
        self.messages = patch.object(installer, 'emit')
        self.messages.start()
        self.addCleanup(self.messages.stop)
        delays = patch.object(installer.time, 'sleep')
        delays.start()
        self.addCleanup(delays.stop)
        self.addCleanup(self.close_log)

    def close_log(self):
        if installer.LOG_STREAM is not None:
            installer.LOG_STREAM.close()
            installer.LOG_STREAM = None
        installer.ACTIVE_LOG = None

    def test_bundle_contains_app_and_services_without_private_runtime(self):
        raw = installer.decode_payload()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
            self.assertIn('manage.py', names)
            self.assertIn('main.py', names)
            self.assertIn('requirements.txt', names)
            self.assertFalse(any(name.startswith('requirements-') for name in names))
            self.assertIn('templates/portal/login.html', names)
            self.assertIn('static/portal/peoplesense-wordmark.png', names)
            self.assertIn('deploy/rpi/.env.example', names)
            self.assertIn('deploy/rpi/peoplesense-hotspot-helper.py', names)
            for service in installer.SERVICE_NAMES:
                self.assertIn(f'deploy/rpi/{service}.service', names)
            self.assertFalse(any(name.startswith(('runtime/', 'logs/', '.venv/', 'artifacts/', 'models/'))
                                 or 'db.sqlite3' in name or '__pycache__' in name for name in names))
            for name in names:
                if name.endswith('.py'):
                    compile(archive.read(name), name, 'exec')

    def test_checksum_tampering_rejected(self):
        raw = bytearray(installer.decode_payload())
        raw[-1] ^= 1
        with patch.object(installer, 'PAYLOAD', base64.b85encode(raw).decode()):
            with self.assertRaisesRegex(RuntimeError, 'checksum failed'):
                installer.decode_payload()

    def test_daemon_checksum_mismatch_never_extracts_source(self):
        with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
            daemon.extract_source(b'invalid download', self.app)
        self.assertEqual(list(self.app.iterdir()), [])

    def test_daemon_source_traversal_and_symlink_rejected(self):
        for name in ('pigpio-79/../../secret', 'pigpio-79/linked', '/etc/passwd', 'C:/secret'):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                entry = zipfile.ZipInfo(name)
                if name.endswith('linked'):
                    entry.external_attr = 0o120777 << 16
                archive.writestr(entry, b'data')
            raw = stream.getvalue()
            with patch.object(daemon, 'SOURCE_SHA256', hashlib.sha256(raw).hexdigest()):
                with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                    daemon.extract_source(raw, self.app)

    def test_traversal_absolute_and_windows_paths_rejected(self):
        for name in ('../secret', 'portal/../../secret', '/etc/passwd', 'C:/secret', 'portal\\secret'):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                installer.safe_archive_path(name)
        self.assertEqual(installer.safe_archive_path('portal/models.py').as_posix(), 'portal/models.py')

    def test_symlink_archive_entry_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            entry = zipfile.ZipInfo('portal/linked.py')
            entry.external_attr = 0o120777 << 16
            archive.writestr(entry, '/etc/passwd')
        raw = stream.getvalue()
        with patch.object(installer, 'PAYLOAD', base64.b85encode(raw).decode()), \
             patch.object(installer, 'PAYLOAD_SHA256', hashlib.sha256(raw).hexdigest()):
            with self.assertRaisesRegex(RuntimeError, 'Symbolic links'):
                installer.decode_payload()

    def test_country_validation_uses_iso_list(self):
        country_file = self.app / 'countries.tab'
        country_file.write_text('# Sample\nPH\tPhilippines\nUS\tUnited States\n')
        self.assertEqual(installer.country_code(' ph ', country_file), 'PH')
        for bad in ('ZZ', 'PH; reboot', '00'):
            with self.assertRaises(RuntimeError):
                installer.country_code(bad, country_file)

    def test_fresh_os_world_domain_prompts_for_country(self):
        def validate(value):
            if value == '00':
                raise RuntimeError('not a country')
            return 'PH'

        with patch.object(installer, 'capture', return_value='00'), \
                patch.object(installer, 'country_code', side_effect=validate), \
                patch.object(installer.sys.stdin, 'isatty', return_value=True), \
                patch('builtins.input', return_value='PH') as prompt, \
                patch.object(installer, 'run') as run:
            installer.configure_country(None)
        prompt.assert_called_once()
        run.assert_called_once_with(['raspi-config', 'nonint', 'do_wifi_country', 'PH'])

    def test_unattended_fresh_os_requires_explicit_country(self):
        with patch.object(installer, 'capture', return_value='00'), \
                patch.object(installer, 'country_code', side_effect=RuntimeError('not a country')), \
                patch.object(installer.sys.stdin, 'isatty', return_value=False), \
                patch.object(installer, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, '--wifi-country'):
                installer.configure_country(None)
        run.assert_not_called()

    def test_secret_and_hosts_are_generated_without_ipv6_or_duplicates(self):
        template = 'DJANGO_SECRET_KEY=replace-with-a-random-secret\nDJANGO_ALLOWED_HOSTS=localhost\n'
        result = installer.environment_text(template, 'camera-pi', ['192.168.1.20', '::1', '192.168.1.20'])
        lines = dict(line.split('=', 1) for line in result.splitlines())
        self.assertNotIn('replace-with', lines['DJANGO_SECRET_KEY'])
        self.assertGreaterEqual(len(lines['DJANGO_SECRET_KEY']), 48)
        hosts = lines['DJANGO_ALLOWED_HOSTS'].split(',')
        self.assertIn('10.0.0.1', hosts)
        self.assertIn('camera-pi.local', hosts)
        self.assertIn('192.168.1.20', hosts)
        self.assertNotIn('::1', hosts)
        self.assertEqual(len(hosts), len(set(hosts)))

    def test_existing_deployment_secret_is_preserved(self):
        target = self.app / '.env.rpi'
        original = 'DJANGO_SECRET_KEY=existing-private-secret\nDJANGO_ALLOWED_HOSTS=10.0.0.1\n'
        target.write_text(original)
        with patch.object(installer.shutil, 'chown'):
            env = installer.configure_environment()
        self.assertEqual(target.read_text(), original)
        self.assertEqual(env['DJANGO_SECRET_KEY'], 'existing-private-secret')

    def test_unmanaged_existing_install_refused_without_data_loss(self):
        database = self.app / 'db.sqlite3'
        database.write_bytes(b'existing device records')
        with self.assertRaisesRegex(RuntimeError, 'will not replace'):
            installer.prepare_directory()
        self.assertEqual(database.read_bytes(), b'existing device records')
        self.assertFalse(installer.MARKER.exists())

    def test_resume_preserves_data_and_stops_owned_services(self):
        installer.write_state('incomplete', defaults_initialized=True)
        database = self.app / 'db.sqlite3'
        database.write_bytes(b'existing device records')
        with patch.object(installer, 'capture', return_value='loaded'), patch.object(installer, 'run') as run:
            self.assertTrue(installer.prepare_directory())
        self.assertEqual(run.call_count, 4)
        self.assertEqual(database.read_bytes(), b'existing device records')
        self.assertTrue(json.loads(installer.MARKER.read_text())['defaults_initialized'])

    def test_complete_install_is_not_reinstalled(self):
        installer.write_state('complete')
        with patch.object(installer, 'run') as run:
            self.assertFalse(installer.prepare_directory())
        run.assert_not_called()

    def test_repair_reinstalls_without_resetting_existing_data(self):
        installer.write_state('complete')
        database = self.app / 'db.sqlite3'
        database.write_bytes(b'existing accounts and camera settings')
        environment = self.app / '.env.rpi'
        environment.write_text('DJANGO_SECRET_KEY=keep-this-secret\n')
        with patch.object(installer, 'stop_installed_services') as stop:
            self.assertTrue(installer.prepare_directory(repair=True))
        stop.assert_called_once()
        self.assertEqual(database.read_bytes(), b'existing accounts and camera settings')
        self.assertEqual(environment.read_text(), 'DJANGO_SECRET_KEY=keep-this-secret\n')
        state = json.loads(installer.MARKER.read_text())
        self.assertEqual(state['status'], 'installing')
        self.assertTrue(state['defaults_initialized'])

    def test_failed_repair_stops_services_and_marks_installation_incomplete(self):
        with patch.object(installer.sys, 'argv', ['setup_peoplesense.py', '--repair']), \
                patch.object(installer, 'decode_payload', return_value=b'fixture'), \
                patch.object(installer, 'preflight'), \
                patch.object(installer.os, 'geteuid', return_value=0, create=True), \
                patch.object(installer.os, 'O_NOFOLLOW', 0, create=True), \
                patch.object(installer.os, 'fchmod', create=True), \
                patch.object(installer, 'LOG', self.app / 'install.log'), \
                patch.object(installer, 'installation_lock', return_value=contextlib.nullcontext()), \
                patch.object(installer, 'prepare_directory', return_value=True) as prepare, \
                patch.object(installer, 'install', side_effect=RuntimeError('model failed')), \
                patch.object(installer, 'capture', return_value='loaded'), \
                patch.object(installer, 'run') as run:
            try:
                with self.assertRaisesRegex(RuntimeError, 'model failed'):
                    installer.main()
            finally:
                if installer.LOG_STREAM is not None:
                    installer.LOG_STREAM.close()
                    installer.LOG_STREAM = None
        prepare.assert_called_once_with(update=False, repair=True)
        self.assertEqual(json.loads(installer.MARKER.read_text())['status'], 'incomplete')
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [['systemctl', 'stop', name] for name in installer.SERVICE_NAMES])

    def old_bundle(self, **extra):
        installer.write_state('complete', **extra)
        state = json.loads(installer.MARKER.read_text())
        state['payload_sha256'] = '0' * 64
        installer.MARKER.write_text(json.dumps(state))
        return installer.MARKER.read_bytes()

    def test_different_bundle_updates_automatically_including_repair(self):
        for repair in (False, True):
            with self.subTest(repair=repair):
                original = self.old_bundle()
                with patch.object(installer, 'capture', return_value=''), patch.object(installer, 'run'):
                    self.assertTrue(installer.prepare_directory(repair=repair))
                state = json.loads(installer.MARKER.read_text())
                self.assertEqual(state['payload_sha256'], installer.PAYLOAD_SHA256)
                self.assertEqual(state['status'], 'installing')
                with tarfile.open(state['upgrade_backup']) as archive:
                    self.assertEqual(archive.extractfile('.peoplesense-installer.json').read(), original)

    def test_update_backs_up_data_and_preserves_existing_camera_defaults(self):
        original_marker = self.old_bundle()
        database = self.app / 'db.sqlite3'
        connection = sqlite3.connect(database)
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE settings (password TEXT, camera TEXT)')
            connection.execute("INSERT INTO settings VALUES ('existing-password', '12')")
            connection.commit()
            environment = self.app / '.env.rpi'
            environment.write_text('DJANGO_SECRET_KEY=keep-existing-secret\n')
            picture = self.app / 'runtime/captures/keep.jpg'
            picture.parent.mkdir(parents=True)
            picture.write_bytes(b'existing photo')
            key = self.app / 'runtime/secrets/wifi.key'
            key.parent.mkdir()
            key.write_bytes(b'existing encryption key')
            (self.app / '.venv').mkdir()
            (self.app / '.venv/large-package').write_bytes(b'not backed up')
            (self.app / 'manage.py').write_text('old application')
            with patch.object(installer, 'capture', return_value='loaded'), patch.object(installer, 'run'):
                self.assertTrue(installer.prepare_directory())
            state = json.loads(installer.MARKER.read_text())
            self.assertEqual(state['payload_sha256'], installer.PAYLOAD_SHA256)
            self.assertEqual(state['previous_payload_sha256'], '0' * 64)
            self.assertTrue(state['defaults_initialized'])
            self.assertEqual(state['status'], 'installing')
            self.assertEqual(environment.read_text(), 'DJANGO_SECRET_KEY=keep-existing-secret\n')
            self.assertEqual(picture.read_bytes(), b'existing photo')
            self.assertEqual(key.read_bytes(), b'existing encryption key')
            with tarfile.open(state['upgrade_backup']) as archive:
                self.assertEqual(archive.extractfile('.peoplesense-installer.json').read(), original_marker)
                self.assertEqual(archive.extractfile('runtime/captures/keep.jpg').read(), b'existing photo')
                self.assertEqual(archive.extractfile('runtime/secrets/wifi.key').read(), b'existing encryption key')
                self.assertEqual(archive.extractfile('manage.py').read(), b'old application')
                self.assertFalse(any(name.startswith('.venv') or name.endswith(('-wal', '-shm'))
                                     for name in archive.getnames()))
                restored = self.app / 'verified-backup.sqlite3'
                restored.write_bytes(archive.extractfile('db.sqlite3').read())
            with contextlib.closing(sqlite3.connect(restored)) as check:
                self.assertEqual(check.execute('SELECT * FROM settings').fetchall(), [('existing-password', '12')])
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, 'w') as archive:
                archive.writestr('manage.py', b'new application')
            with patch.object(installer.shutil, 'chown'):
                installer.install_bundle(payload.getvalue())
            self.assertEqual((self.app / 'manage.py').read_text(), 'new application')
            self.assertEqual(environment.read_text(), 'DJANGO_SECRET_KEY=keep-existing-secret\n')
            self.assertEqual(picture.read_bytes(), b'existing photo')
            self.assertEqual(connection.execute('SELECT * FROM settings').fetchall(), [('existing-password', '12')])
        finally:
            connection.close()

    def test_backup_failure_restores_services_and_leaves_old_marker_and_sources(self):
        original = self.old_bundle()
        source = self.app / 'manage.py'
        source.write_text('old source')

        def service_status(arguments):
            return 'active' if arguments[1] == 'is-active' else 'loaded'

        with patch.object(installer, 'capture', side_effect=service_status), \
                patch.object(installer, 'run') as run, \
                patch.object(installer, 'backup_installation', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                installer.prepare_directory()
        self.assertEqual(installer.MARKER.read_bytes(), original)
        self.assertEqual(source.read_text(), 'old source')
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(calls[:4], [['systemctl', 'stop', name] for name in installer.SERVICE_NAMES])
        self.assertEqual(calls[4:], [['systemctl', 'start', name] for name in reversed(installer.SERVICE_NAMES)])

    def test_update_resume_keeps_original_backup_and_does_not_reapply_defaults(self):
        installer.write_state('incomplete', upgrade_backup='original-backup.tar.gz',
                              previous_payload_sha256='0' * 64, defaults_initialized=True)
        with patch.object(installer, 'capture', return_value='loaded'), patch.object(installer, 'run'), \
                patch.object(installer, 'backup_installation') as backup:
            self.assertTrue(installer.prepare_directory(update=True))
        backup.assert_not_called()
        state = json.loads(installer.MARKER.read_text())
        self.assertEqual(state['upgrade_backup'], 'original-backup.tar.gz')
        self.assertTrue(state['defaults_initialized'])

    def test_update_flag_does_not_override_unmanaged_directory(self):
        source = self.app / 'manage.py'
        source.write_text('unmanaged source')
        with self.assertRaisesRegex(RuntimeError, 'will not replace'):
            installer.prepare_directory(update=True)
        self.assertEqual(source.read_text(), 'unmanaged source')

    def test_automatic_update_does_not_override_unmanaged_directory(self):
        source = self.app / 'manage.py'
        source.write_text('unmanaged source')
        with self.assertRaisesRegex(RuntimeError, 'will not replace'):
            installer.prepare_directory()
        self.assertEqual(source.read_text(), 'unmanaged source')

    def test_automatic_update_rejects_unrecognized_installer_marker(self):
        installer.MARKER.write_text(json.dumps({'installer': 'Other', 'payload_sha256': '0' * 64}))
        with self.assertRaisesRegex(RuntimeError, 'not a recognized'):
            installer.prepare_directory()

    def test_update_flag_rejects_unrecognized_installer_marker(self):
        installer.MARKER.write_text(json.dumps({'installer': 'Other', 'payload_sha256': '0' * 64}))
        with self.assertRaisesRegex(RuntimeError, 'not a recognized'):
            installer.prepare_directory(update=True)

    def test_low_disk_space_prevents_backup_and_preserves_marker(self):
        original = self.old_bundle()
        usage = type('Usage', (), {'free': 0})()
        with patch.object(installer, 'capture', return_value=''), patch.object(installer, 'run'), \
                patch.object(installer.shutil, 'disk_usage', return_value=usage):
            with self.assertRaisesRegex(RuntimeError, 'Not enough free space'):
                installer.prepare_directory(update=True)
        self.assertEqual(installer.MARKER.read_bytes(), original)
        self.assertEqual(list(installer.BACKUP_DIR.iterdir()), [])

    def test_failed_atomic_marker_write_preserves_original_state(self):
        original = self.old_bundle()
        with patch.object(installer.os, 'replace', side_effect=OSError('fixture rename failure')):
            with self.assertRaisesRegex(OSError, 'rename failure'):
                installer.write_state('installing')
        self.assertEqual(installer.MARKER.read_bytes(), original)
        self.assertEqual(list(self.app.glob('.peoplesense-state-*.tmp')), [])

    def test_completed_update_reports_preserved_credentials_instead_of_factory_defaults(self):
        installer.write_state('installing', upgrade_backup='original-backup.tar.gz', defaults_initialized=True)
        with patch.object(installer, 'configure_country'), patch.object(installer, 'install_bundle'), \
                patch.object(installer, 'configure_environment', return_value={}), \
                patch.object(installer, 'install_services'), patch.object(installer, 'wait_for_http'), \
                patch.object(installer, 'capture', return_value='10.0.0.1/24'), \
                patch.object(installer, 'run') as run:
            installer.install(b'fixture', 'PH')
        messages = '\n'.join(str(call.args[0]) for call in installer.emit.call_args_list)
        self.assertIn('UPDATE COMPLETE', messages)
        self.assertIn('existing password', messages)
        self.assertNotIn('admin / 12345678', messages)
        self.assertFalse(any(call.kwargs.get('input_text') for call in run.call_args_list))

    def test_install_activates_hotspot_last_and_preserves_defaults_on_resume(self):
        installer.write_state('installing')
        with patch.object(installer, 'configure_country'), patch.object(installer, 'install_bundle'), \
             patch.object(installer, 'configure_environment', return_value={}), \
             patch.object(installer, 'install_services') as services, \
             patch.object(installer, 'wait_for_http'), \
             patch.object(installer, 'capture', return_value='10.0.0.1/24'), \
             patch.object(installer, 'run') as run:
            installer.install(b'fixture', 'PH')
            calls = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any('collectstatic' in call for call in calls))
            self.assertIn(['systemctl', 'start', 'peoplesense-hotspot'], calls)
            self.assertEqual(calls[-4:], [['systemctl', 'stop', name] for name in installer.SERVICE_NAMES])
            services.assert_called_once()
            self.assertEqual(json.loads(installer.MARKER.read_text())['status'], 'complete')
            self.assertEqual(json.loads(installer.MARKER.read_text())['launch_mode'], 'manual')
            self.assertTrue(any(call.kwargs.get('input_text') and 'yolox_nano' in call.kwargs['input_text']
                                for call in run.call_args_list))
            run.reset_mock()
            installer.install(b'fixture', 'PH')
            self.assertFalse(any(call.kwargs.get('input_text') for call in run.call_args_list))

    def test_dependency_failure_never_starts_gpio_or_hotspot(self):
        installer.write_state('installing')
        def fail_pip(arguments, **kwargs):
            if 'pip' in arguments:
                raise RuntimeError('fixture dependency failure')
        with patch.object(installer, 'configure_country'), patch.object(installer, 'install_bundle'), \
             patch.object(installer, 'configure_environment', return_value={}), \
             patch.object(installer, 'capture', return_value='1000'), \
             patch.object(installer, 'install_services') as services, \
             patch.object(installer, 'run', side_effect=fail_pip) as run:
            with self.assertRaisesRegex(RuntimeError, 'dependency failure'):
                installer.install(b'fixture', 'PH')
        services.assert_not_called()
        self.assertFalse(any('peoplesense-hotspot' in call.args[0] for call in run.call_args_list))

    def test_windows_rejected_before_any_installation_command(self):
        with patch.object(installer.platform, 'system', return_value='Windows'), \
             patch.object(installer, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'not Windows'):
                installer.preflight()
        run.assert_not_called()

    def test_supported_os_and_python_pairs(self):
        for codename, version in [('trixie', (3, 13)), ('bookworm', (3, 11))]:
            info = type('Version', (tuple,), {'major': version[0], 'minor': version[1]})(version)
            with self.subTest(codename=codename), \
                    patch.object(installer.platform, 'system', return_value='Linux'), \
                    patch.object(installer.platform, 'machine', return_value='aarch64'), \
                    patch.object(installer.Path, 'read_text', return_value=f'VERSION_CODENAME={codename}\n'), \
                    patch.object(installer.Path, 'read_bytes', return_value=b'Raspberry Pi 4 Model B\0'), \
                    patch.object(installer.Path, 'exists', return_value=True), \
                    patch.object(installer.shutil, 'which', return_value='/usr/bin/tool'), \
                    patch.object(installer.sys, 'version_info', info):
                installer.preflight()

    def test_latest_os_dependencies_and_import_checks_precede_service_activation(self):
        installer.write_state('installing')
        with patch.object(installer, 'configure_country'), patch.object(installer, 'install_bundle'), \
                patch.object(installer, 'configure_environment', return_value={}), \
                patch.object(installer, 'install_services'), patch.object(installer, 'wait_for_http'), \
                patch.object(installer, 'capture', return_value='10.0.0.1/24'), \
                patch.object(installer, 'run') as run:
            installer.install(b'fixture', 'PH')
        calls = [list(map(str, call.args[0])) for call in run.call_args_list]
        apt = next(call for call in calls if call[:2] == ['apt-get', 'install'])
        self.assertIn('python3-rpi.gpio', apt)
        self.assertNotIn('pigpio', apt)
        self.assertNotIn('libgpiod2', apt)
        self.assertTrue(any('--system-site-packages' in call for call in calls))
        self.assertTrue(any('install_pigpio.py' in ' '.join(call) for call in calls))
        # Raspberry Pi OS exposes unrelated metadata (e.g. types-flask and
        # apt-listchanges) through system-site-packages. It must not block setup.
        self.assertFalse(any(call[-3:] == ['-m', 'pip', 'check'] for call in calls))
        imports = next(i for i, call in enumerate(calls) if 'import django' in ' '.join(call))
        hotspot = calls.index(['systemctl', 'start', 'peoplesense-hotspot'])
        self.assertLess(imports, hotspot)

    def test_download_step_retries_and_stops_after_three_failures(self):
        with patch.object(installer, 'run', side_effect=[RuntimeError('offline'), RuntimeError('timeout'), 0]) as run:
            self.assertEqual(installer.download_step(['download'], cwd=self.app), 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.kwargs['cwd'], self.app)
        with patch.object(installer, 'run', side_effect=RuntimeError('offline')) as run:
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                installer.download_step(['download'])
        self.assertEqual(run.call_count, 3)

    def test_unattended_downloads_permissions_and_model_checks_before_hotspot(self):
        installer.write_state('installing')
        with patch.object(installer, 'configure_country'), patch.object(installer, 'install_bundle'), \
                patch.object(installer, 'configure_environment', return_value={}), \
                patch.object(installer, 'install_services'), patch.object(installer, 'wait_for_http'), \
                patch.object(installer, 'capture', return_value='10.0.0.1/24'), \
                patch.object(installer, 'run') as run:
            installer.install(b'fixture', 'PH')
        calls = [list(map(str, call.args[0])) for call in run.call_args_list]
        apt_call = next(call for call in run.call_args_list if call.args[0][:2] == ['apt-get', 'install'])
        self.assertEqual(apt_call.kwargs['env']['DEBIAN_FRONTEND'], 'noninteractive')
        self.assertIn('v4l-utils', apt_call.args[0])
        self.assertIn(['usermod', '-a', '-G', 'video,gpio', 'peoplesense'], calls)
        packages = next(call for call in calls if 'requirements.txt' in call)
        self.assertIn('--no-input', packages)
        self.assertIn('--only-binary=numpy,scipy,opencv-python-headless,ai-edge-litert,cryptography', packages)
        model_download = next(i for i, call in enumerate(calls) if 'download_models' in call)
        model_check = next(i for i, call in enumerate(calls) if 'check_camera_setup' in call)
        hotspot = calls.index(['systemctl', 'start', 'peoplesense-hotspot'])
        self.assertLess(model_download, model_check)
        self.assertLess(model_check, hotspot)

    def test_installed_app_services_wait_for_main_instead_of_boot(self):
        with patch.object(installer, 'run') as run, patch.object(installer, 'capture', return_value=''), \
                patch.object(installer, 'prepare_service_logs') as logs, \
                patch.object(installer, 'wait_for_http'), \
                patch.object(installer.Path, 'is_symlink', return_value=False), \
                patch.object(installer.Path, 'exists', return_value=False), \
                patch.object(installer.Path, 'symlink_to'):
            installer.install_services()
        logs.assert_called_once()
        calls = [call.args[0] for call in run.call_args_list]
        self.assertIn(['systemctl', 'disable', *installer.SERVICE_NAMES], calls)
        enabled = [call for call in calls if call[:2] == ['systemctl', 'enable']]
        self.assertEqual(enabled, [['systemctl', 'enable', '--now', 'NetworkManager', 'nginx']])

    def test_fresh_setup_creates_its_log_after_claiming_the_application_folder(self):
        self.app.rmdir()
        self.assertTrue(installer.prepare_directory())
        with patch.object(installer.os, 'O_NOFOLLOW', 0, create=True), \
                patch.object(installer.os, 'fchmod', create=True):
            installer.open_install_log()
            try:
                installer.LOG_STREAM.write('first install\n')
            finally:
                installer.LOG_STREAM.close()
                installer.LOG_STREAM = None
            installer.open_install_log()
            try:
                installer.LOG_STREAM.write('resumed install\n')
            finally:
                installer.LOG_STREAM.close()
                installer.LOG_STREAM = None
        self.assertEqual(installer.LOG.read_text(), 'first install\nresumed install\n')
        self.assertTrue(installer.MARKER.exists())

    def test_startup_failure_is_logged_even_without_an_application_folder(self):
        self.app.rmdir()
        self.messages.stop()
        with patch('builtins.print'), \
                patch.object(installer.sys, 'argv', ['setup_peoplesense.py', '--check']), \
                patch.object(installer, 'decode_payload', side_effect=RuntimeError('damaged bundle')):
            with self.assertRaisesRegex(RuntimeError, 'damaged bundle') as failure:
                installer.main()
            installer.report_failure(failure.exception)
        self.assertFalse(self.app.exists())
        text = installer.STARTUP_LOG.read_text()
        self.assertIn('damaged bundle', text)
        self.assertIn(str(installer.STARTUP_LOG), text)

    def test_preparation_failure_is_logged_before_the_application_log_exists(self):
        self.messages.stop()
        with patch('builtins.print'), \
                patch.object(installer.sys, 'argv', ['setup_peoplesense.py']), \
                patch.object(installer.os, 'geteuid', return_value=0, create=True), \
                patch.object(installer, 'decode_payload', return_value=b'fixture'), \
                patch.object(installer, 'preflight'), \
                patch.object(installer, 'installation_lock', return_value=contextlib.nullcontext()), \
                patch.object(installer, 'prepare_directory', side_effect=RuntimeError('backup disk full')), \
                patch.object(installer, 'install') as install:
            with self.assertRaisesRegex(RuntimeError, 'backup disk full') as failure:
                installer.main()
            installer.report_failure(failure.exception)
        install.assert_not_called()
        self.assertFalse(installer.LOG.exists())
        self.assertIn('backup disk full', installer.STARTUP_LOG.read_text())

    def test_service_logs_are_created_without_truncating_existing_diagnostics(self):
        directory = self.app / 'logs'
        directory.mkdir()
        (directory / 'web.log').write_text('previous web error\n')
        with patch.object(installer.shutil, 'chown') as owner, \
                patch.object(installer.os, 'O_NOFOLLOW', 0, create=True):
            installer.prepare_service_logs()
        self.assertEqual((directory / 'web.log').read_text(), 'previous web error\n')
        self.assertEqual({p.name for p in directory.iterdir()}, {
            'web.log', 'hardware.log', 'hotspot.log', 'pigpio.log', 'nginx-access.log', 'nginx-error.log'})
        self.assertEqual(owner.call_count, 7)

    def test_service_outputs_append_to_logs_in_the_application_folder(self):
        for service, filename in [('web', 'web.log'), ('hardware', 'hardware.log'),
                                  ('hotspot', 'hotspot.log'), ('pigpio', 'pigpio.log')]:
            config = configparser.ConfigParser(interpolation=None)
            config.read(ROOT / f'deploy/rpi/peoplesense-{service}.service')
            self.assertEqual(config['Service']['StandardOutput'], f'append:/opt/peoplesense/logs/{filename}')
            self.assertEqual(config['Service']['StandardError'], 'inherit')
        self.assertEqual(config['Service']['Type'], 'simple')
        self.assertIn('pigpiod -g -l', config['Service']['ExecStart'])
        nginx = (ROOT / 'deploy/rpi/nginx.conf').read_text()
        self.assertIn('access_log /opt/peoplesense/logs/nginx-access.log;', nginx)
        self.assertIn('error_log /opt/peoplesense/logs/nginx-error.log;', nginx)

    def test_failed_model_inference_keeps_hotspot_disabled(self):
        installer.write_state('installing')

        def fail_model_check(arguments, **kwargs):
            if 'check_camera_setup' in arguments:
                raise RuntimeError('fixture inference failure')

        with patch.object(installer, 'configure_country'), patch.object(installer, 'install_bundle'), \
                patch.object(installer, 'configure_environment', return_value={}), \
                patch.object(installer, 'capture', return_value='1000'), \
                patch.object(installer, 'install_services') as services, \
                patch.object(installer, 'run', side_effect=fail_model_check) as run:
            with self.assertRaisesRegex(RuntimeError, 'inference failure'):
                installer.install(b'fixture', 'PH')
        services.assert_not_called()
        self.assertFalse(any('peoplesense-hotspot' in call.args[0] for call in run.call_args_list))


if __name__ == '__main__':
    unittest.main()
