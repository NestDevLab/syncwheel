"""Deterministic process races between policy and existing configuration writers."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_syncwheel as fixtures


PAUSED_WRITER = r'''
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location('paused_configuration_writer', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
hook, command = sys.argv[2], json.loads(sys.argv[3])
original = getattr(module, hook)
paused = False
def read_then_pause(*args, **kwargs):
    global paused
    value = original(*args, **kwargs)
    if not paused:
        paused = True
        print('snapshot-ready', flush=True)
        if sys.stdin.readline().strip() != 'continue':
            raise RuntimeError('missing test barrier release')
    return value
setattr(module, hook, read_then_pause)
sys.argv = ['syncwheel', *command]
raise SystemExit(module.main())
'''


class PrivateJsonConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SyncwheelFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.settings = self.fixture.tmp / 'settings.json'
        self.profile = self.fixture.repo / '.syncwheel/profile.local.json'
        with (self.fixture.repo / '.git/info/exclude').open('a') as handle:
            handle.write('\n.syncwheel/profile.local.json\n')
        self.environment = mock.patch.dict(os.environ, {
            'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.settings),
            'SYNCWHEEL_UPDATE_MODE': 'off',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def race(self, path, hook, first, second, *, original, expected):
        path.write_text(json.dumps(original) + '\n')
        process = subprocess.Popen(
            [sys.executable, '-c', PAUSED_WRITER, str(fixtures.CLI), hook, json.dumps(first)],
            cwd=self.fixture.repo, env=dict(os.environ), text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), 'snapshot-ready')
            self.fixture.run_cli(*second)
            winner = path.read_bytes()
            stdout, stderr = process.communicate('continue\n', timeout=20)
            self.assertEqual(process.returncode, 2, stdout + stderr)
            self.assertIn('changed concurrently', stderr)
            self.assertEqual(path.read_bytes(), winner)
            self.assertEqual(json.loads(winner), expected)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_concurrent_global_policy_fields_do_not_overwrite_each_other(self):
        self.race(self.settings, 'load_private_configuration',
                  ['self', 'worktree-policy', 'set', '--expiry', 'off', '--apply'],
                  ['self', 'worktree-policy', 'set', '--writer-exclusivity', 'block', '--apply'],
                  original={'foreign': {'preserve': True}},
                  expected={'foreign': {'preserve': True},
                            'worktree_policy': {'writer_exclusivity': 'block'}})

    def test_global_policy_and_existing_update_writer_cooperate_in_both_orders(self):
        self.race(self.settings, 'load_private_configuration',
                  ['self', 'worktree-policy', 'set', '--expiry', 'off', '--apply'],
                  ['self', 'mode', 'notify'], original={'foreign': 7},
                  expected={'foreign': 7, 'update': {'mode': 'notify', 'check_interval_seconds': 21600}})
        self.race(self.settings, 'load_private_configuration',
                  ['self', 'mode', 'notify'],
                  ['self', 'worktree-policy', 'set', '--expiry', 'off', '--apply'],
                  original={'foreign': 7}, expected={'foreign': 7, 'worktree_policy': {'expiry': 'off'}})

    def test_concurrent_repository_policy_fields_preserve_other_keys(self):
        self.race(self.profile, 'load_repo_profile',
                  ['repo', 'worktree-policy', 'set', '--expiry', 'off', '--apply'],
                  ['repo', 'worktree-policy', 'set', '--writer-exclusivity', 'block', '--apply'],
                  original={'foreign': {'preserve': True}},
                  expected={'foreign': {'preserve': True},
                            'worktree_policy': {'writer_exclusivity': 'block'}})

    def test_repository_policy_and_existing_writer_preserve_winning_document(self):
        self.race(self.profile, 'load_repo_profile',
                  ['repo', 'worktree-policy', 'set', '--expiry', 'off', '--apply'],
                  ['use', 'alice'], original={'foreign': 7},
                  expected={'foreign': 7, 'personal': 'alice'})
        self.race(self.profile, 'load_repo_profile',
                  ['use', 'alice'],
                  ['repo', 'worktree-policy', 'set', '--expiry', 'off', '--apply'],
                  original={'foreign': 7}, expected={'foreign': 7, 'worktree_policy': {'expiry': 'off'}})

    def test_shared_selection_preserves_unrelated_profile_preferences(self):
        self.profile.write_text(json.dumps({'personal': 'alice', 'worktree_policy': {'expiry': 'off'},
                                            'foreign': {'keep': True}}))
        self.fixture.run_cli('use', '--shared')
        self.assertEqual(json.loads(self.profile.read_text()),
                         {'worktree_policy': {'expiry': 'off'}, 'foreign': {'keep': True}})


if __name__ == '__main__':
    unittest.main()
