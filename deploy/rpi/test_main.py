"""Launcher tests never start services, install packages, or access GPIO/Wi-Fi."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, call

spec = importlib.util.spec_from_file_location('peoplesense_launcher', Path(__file__).resolve().parents[2] / 'main.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.app = Path(folder.name)
        app_patch = patch.object(launcher, 'APP', self.app)
        app_patch.start()
        self.addCleanup(app_patch.stop)
        output = patch('builtins.print')
        output.start()
        self.addCleanup(output.stop)

    def ready(self):
        (self.app / '.peoplesense-installer.json').write_text(json.dumps(
            {'installer': 'PeopleSense', 'status': 'complete'}))
        for relative in ('manage.py', '.env.rpi', '.venv/bin/python'):
            target = self.app / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()

    def test_uninstalled_app_has_setup_instructions_and_never_starts_services(self):
        with patch.object(launcher, 'systemctl') as services:
            with self.assertRaisesRegex(RuntimeError, 'setup_peoplesense.py'):
                launcher.check_installation()
        services.assert_not_called()

    def test_missing_runtime_files_have_repair_instructions(self):
        self.ready()
        (self.app / '.venv/bin/python').unlink()
        with self.assertRaisesRegex(RuntimeError, '--repair'):
            launcher.check_installation()

    def test_ctrl_c_stops_the_app_and_leaves_shared_os_services_running(self):
        self.ready()
        with patch.object(launcher, 'launcher_lock', return_value=nullcontext()), \
                patch.object(launcher, 'wait_for_portal') as ready, \
                patch.object(launcher.time, 'sleep', side_effect=KeyboardInterrupt), \
                patch.object(launcher, 'systemctl') as services:
            launcher.run_app()
        ready.assert_called_once()
        self.assertEqual(services.call_args_list, [
            call('start', 'NetworkManager', 'nginx', *launcher.SERVICES[:-1]),
            call('start', 'peoplesense-hotspot'),
            call('is-active', '--quiet', *launcher.SERVICES),
            call('stop', *reversed(launcher.SERVICES)),
        ])

    def test_failed_portal_start_cleans_up_without_starting_hotspot(self):
        self.ready()
        with patch.object(launcher, 'launcher_lock', return_value=nullcontext()), \
                patch.object(launcher, 'wait_for_portal', side_effect=RuntimeError('portal unavailable')), \
                patch.object(launcher, 'systemctl') as services:
            with self.assertRaisesRegex(RuntimeError, 'portal unavailable'):
                launcher.run_app()
        self.assertNotIn(call('start', 'peoplesense-hotspot'), services.call_args_list)
        self.assertEqual(services.call_args_list[-1], call('stop', *reversed(launcher.SERVICES)))

    def test_second_launcher_does_not_stop_the_running_app(self):
        self.ready()
        with patch.object(launcher, 'launcher_lock', side_effect=RuntimeError('already running')), \
                patch.object(launcher, 'systemctl') as services:
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                launcher.run_app()
        services.assert_not_called()

    def test_status_does_not_start_the_app_or_request_sudo(self):
        self.ready()
        with patch.object(launcher.platform, 'system', return_value='Linux'), \
                patch.object(launcher.sys, 'argv', ['main.py', '--status']), \
                patch.object(launcher.os, 'execv') as sudo, \
                patch.object(launcher, 'systemctl', return_value=3) as services:
            self.assertEqual(launcher.main(), 3)
        services.assert_called_once_with('status', '--no-pager', *launcher.SERVICES, check=False)
        sudo.assert_not_called()


if __name__ == '__main__':
    unittest.main()
