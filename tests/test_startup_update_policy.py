"""Startup update policy uses one real settings snapshot and honors off before IO."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


class StartupUpdatePolicyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='syncwheel-startup-policy-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = self.root / 'settings.json'
        self.state = self.root / 'state.json'
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith(('GIT_', 'SYNCWHEEL_', 'PYTHON', 'LD_'))}
        self.environment.update(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / 'config'),
            XDG_STATE_HOME=str(self.root / 'state'), XDG_CACHE_HOME=str(self.root / 'cache'),
            TMPDIR=str(self.root), GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null',
            GIT_CONFIG_NOSYSTEM='1', SYNCWHEEL_UPDATE_SETTINGS_PATH=str(self.settings),
            SYNCWHEEL_UPDATE_STATE_PATH=str(self.state))
        patch = mock.patch.dict(os.environ, self.environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        spec = importlib.util.spec_from_file_location('startup_policy_facade', SCRIPTS / 'syncwheel.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def write_settings(self, mode):
        self.settings.write_text(json.dumps({'update': {'mode': mode}, 'foreign': {'keep': True}}))

    def assert_off_has_no_refresh(self):
        before = self.settings.read_bytes()
        self.state.write_text('{"sentinel":"preserve"}\n')
        cache = self.state.read_bytes()
        with mock.patch.object(self.module, 'refresh_cached_self_update_status', return_value=({'update_available': False}, {'mode': 'off'}, {}, None)) as refresh, \
             mock.patch.object(self.module, 'collect_self_update_status') as collect, \
             mock.patch.object(self.module, 'save_update_state') as save, \
             mock.patch.object(self.module, 'perform_self_update') as update:
            self.module.maybe_handle_startup_update_policy(SimpleNamespace(command='plan'))
        refresh.assert_not_called()
        collect.assert_not_called()
        save.assert_not_called()
        update.assert_not_called()
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.state.read_bytes(), cache)

    def test_off_from_real_settings_skips_refresh_fetch_and_cache(self):
        self.write_settings('off')
        self.assert_off_has_no_refresh()

    def test_environment_off_overrides_real_notify_settings(self):
        self.write_settings('notify')
        with mock.patch.dict(os.environ, {'SYNCWHEEL_UPDATE_MODE': 'off'}):
            self.assert_off_has_no_refresh()

    def test_invalid_real_settings_do_not_start_update_io(self):
        for payload in ('{invalid', '{"update":[]}', '{"update":{"mode":"invalid"}}'):
            with self.subTest(payload=payload):
                self.settings.write_text(payload)
                self.assert_off_has_no_refresh()

    def test_concurrent_mode_writer_applies_at_next_startup_snapshot(self):
        self.write_settings('notify')
        original = self.module.load_update_settings
        first = None
        def read_then_run_writer():
            nonlocal first
            first = original()
            result = subprocess.run([sys.executable, str(SCRIPTS / 'syncwheel.py'), 'self', 'mode', 'off'],
                cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return first
        status = {'update_available': False, 'install_root': str(self.root), 'install_kind': 'script'}
        with mock.patch.object(self.module, 'load_update_settings', side_effect=read_then_run_writer) as load, \
             mock.patch.object(self.module, 'detect_syncwheel_install', return_value={'install_root': str(self.root), 'kind': 'script'}), \
             mock.patch.object(self.module, 'collect_self_update_status', return_value=status), \
             mock.patch.object(self.module, 'refresh_cached_self_update_status', wraps=self.module.refresh_cached_self_update_status) as refresh:
            self.module.maybe_handle_startup_update_policy(SimpleNamespace(command='plan'))
        load.assert_called_once_with()
        refresh.assert_called_once_with(force=False, settings=first)
        self.assertEqual(first['mode'], 'notify')
        self.assertEqual(json.loads(self.settings.read_text())['update']['mode'], 'off')
        self.assertTrue(json.loads(self.settings.read_text())['foreign']['keep'])
        self.assert_off_has_no_refresh()

    def test_notify_and_auto_from_real_settings_reuse_valid_cache(self):
        status = {'update_available': True, 'install_root': str(self.root), 'install_kind': 'script',
                  'current_version': '1.0.0', 'latest_version': '2.0.0', 'recommended_command': 'fixture self update'}
        for mode in ('notify', 'auto'):
            with self.subTest(mode=mode):
                self.write_settings(mode)
                self.state.write_text(json.dumps({'last_checked_epoch': int(time.time()), 'status': status}))
                before = self.state.read_bytes()
                output = io.StringIO()
                with mock.patch.object(self.module, 'detect_syncwheel_install', return_value={'install_root': str(self.root), 'kind': 'script'}), \
                     mock.patch.object(self.module, 'collect_self_update_status') as collect, \
                     mock.patch.object(self.module, 'save_update_state') as save, \
                     mock.patch.object(self.module, 'perform_self_update', return_value=({'current_version': '1.0.0'}, {'current_version': '2.0.0'}, [])) as update, \
                     contextlib.redirect_stderr(output):
                    self.module.maybe_handle_startup_update_policy(SimpleNamespace(command='plan'))
                collect.assert_not_called()
                save.assert_not_called()
                self.assertEqual(self.state.read_bytes(), before)
                if mode == 'notify':
                    update.assert_not_called()
                    self.assertIn('NOTICE: syncwheel update available', output.getvalue())
                else:
                    update.assert_called_once_with(fetch=True)
                    self.assertIn('syncwheel auto-updated', output.getvalue())

    def test_self_commands_skip_automatic_policy_even_with_invalid_settings(self):
        self.settings.write_text('{invalid')
        with mock.patch.object(self.module, 'load_update_settings') as load, \
             mock.patch.object(self.module, 'refresh_cached_self_update_status') as refresh:
            self.module.maybe_handle_startup_update_policy(SimpleNamespace(command='self'))
        load.assert_not_called()
        refresh.assert_not_called()


if __name__ == '__main__':
    unittest.main()
