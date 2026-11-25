"""Exercise preferences through the public CLI and real governed lane records."""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import test_syncwheel as fixtures


class WorktreePolicyTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SyncwheelFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.settings = self.fixture.tmp / 'settings.json'
        self.environment = mock.patch.dict(os.environ, {
            'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.settings),
            'SYNCWHEEL_UPDATE_MODE': 'off',
            'SYNCWHEEL_LANE_OWNER': 'writer-a',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.module = self.fixture.load_syncwheel_module()
        self.profile = self.fixture.repo / '.syncwheel/profile.local.json'
        exclude = self.fixture.repo / '.git/info/exclude'
        with exclude.open('a') as handle:
            handle.write('\n.syncwheel/profile.local.json\n')
        self.fixture.git('config', 'user.name', 'Fixture')
        self.fixture.git('config', 'user.email', 'fixture@example.invalid')

    def command(self, scope, *args, **kwargs):
        return self.fixture.run_cli(scope, 'worktree-policy', *args, '--json', **kwargs)

    def set_policy(self, scope, *args):
        return self.command(scope, 'set', *args, '--apply')

    def open_lane(self):
        return json.loads(self.fixture.run_cli(
            'worktree', 'open', 'first', '--into', 'feature-a', '--json',
        ).stdout)['lane']

    def test_global_outside_repository_preview_apply_clear_and_preserve_other_keys(self):
        original = {'update': {'mode': 'off'}, 'installation': {'kind': 'script'}}
        self.settings.write_text(json.dumps(original))
        outside = self.fixture.tmp
        preview = self.command('self', 'set', '--expiry', 'off', cwd=outside)
        self.assertFalse(json.loads(preview.stdout)['applied'])
        self.assertEqual(json.loads(self.settings.read_text()), original)
        applied = self.command('self', 'set', '--expiry', 'off', '--apply', cwd=outside)
        self.assertTrue(json.loads(applied.stdout)['applied'])
        self.command('self', 'clear', '--apply', cwd=outside)
        self.assertEqual(json.loads(self.settings.read_text()), original)

    def test_precedence_is_per_field_and_clear_restores_inheritance(self):
        self.profile.write_text(json.dumps({'personal': 'alice', 'coordination': {'locks': {}}}))
        self.set_policy('self', '--expiry', 'off', '--writer-exclusivity', 'warn')
        self.set_policy('repo', '--expiry', 'enforced')
        result = json.loads(self.command('repo', 'status').stdout)
        self.assertEqual(result['effective'], {'expiry': 'enforced', 'writer_exclusivity': 'warn'})
        self.assertEqual(result['sources'], {'expiry': 'repo', 'writer_exclusivity': 'global'})
        self.command('repo', 'clear', 'expiry', '--apply')
        result = json.loads(self.command('repo', 'status').stdout)
        self.assertEqual(result['effective']['expiry'], 'off')
        self.assertEqual(json.loads(self.profile.read_text()),
                         {'personal': 'alice', 'coordination': {'locks': {}}})

    def test_default_legacy_and_invalid_values_fail_without_writes(self):
        result = json.loads(self.command('self', 'status', cwd=self.fixture.tmp).stdout)
        self.assertEqual(result['effective'], {'expiry': 'enforced', 'writer_exclusivity': 'legacy'})
        self.command('self', 'set', '--expiry', 'unknown', expected=2)
        self.command('self', 'set', expected=2)
        self.assertFalse(self.settings.exists())
        self.settings.write_text('{"worktree_policy":{"expiry":false}}')
        self.command('self', 'status', expected=2)

    def test_expiry_off_changes_existing_lane_observation_without_rewriting_record(self):
        lane = self.open_lane()
        registry, path = self.module.load_governed_worktree_registry(self.fixture.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)
        before = path.read_bytes()
        result = json.loads(self.fixture.run_cli('worktree', 'list', '--json').stdout)
        self.assertEqual(result['lanes'][0]['code'], 'expired')
        self.set_policy('self', '--expiry', 'off')
        result = json.loads(self.fixture.run_cli('worktree', 'list', '--json').stdout)
        self.assertIsNone(result['lanes'][0]['code'])
        self.assertEqual(path.read_bytes(), before)
        candidates = self.module.governed_worktree_cleanup_candidates(
            self.fixture.repo, self.fixture.read_manifest())
        self.assertEqual(candidates, [])
        self.set_policy('repo', '--expiry', 'enforced')
        result = json.loads(self.fixture.run_cli('worktree', 'list', '--json').stdout)
        self.assertEqual(result['lanes'][0]['code'], 'expired')
        self.assertTrue(Path(lane['path']).exists())

    def test_repo_override_is_shared_by_linked_worktrees(self):
        lane = self.open_lane()
        self.set_policy('self', '--writer-exclusivity', 'block')
        self.set_policy('repo', '--writer-exclusivity', 'off')
        result = self.command('repo', 'status', cwd=Path(lane['path']))
        report = json.loads(result.stdout)
        self.assertEqual(report['effective']['writer_exclusivity'], 'off')
        self.assertEqual(Path(report['path']), self.profile)

    def test_foreign_writer_modes_at_open_and_lane_commit_hook(self):
        lane = self.open_lane()
        other = {'SYNCWHEEL_LANE_OWNER': 'writer-b'}
        self.set_policy('self', '--writer-exclusivity', 'block')
        refused = self.fixture.run_cli('worktree', 'open', 'second', '--into', 'feature-a',
                                      extra_env=other, expected=2)
        self.assertIn('belongs to writer', refused.stderr)
        refused = self.fixture.run_cli('hooks', 'worktree-guard', '--event', 'pre-commit',
                                      cwd=Path(lane['path']), extra_env=other, expected=2)
        self.assertIn('belongs to writer', refused.stderr)
        self.set_policy('self', '--writer-exclusivity', 'warn')
        admitted = self.fixture.run_cli('worktree', 'open', 'second', '--into', 'feature-a',
                                       extra_env=other)
        self.assertIn('writer_exclusivity=warn', admitted.stderr)
        self.set_policy('repo', '--writer-exclusivity', 'off')
        admitted = self.fixture.run_cli('hooks', 'worktree-guard', '--event', 'pre-commit',
                                       cwd=Path(lane['path']), extra_env=other)
        self.assertNotIn('belongs to writer', admitted.stderr)

    def test_scoped_admission_matrix_preserves_physical_and_lifecycle_failures(self):
        policy_module = sys.modules['syncwheel_worktree_policy']
        lane = {'id': 'source', 'state': 'active', 'owner': 'writer-a'}
        for expiry in ('enforced', 'off'):
            for writer in ('legacy', 'block', 'warn', 'off'):
                policy = {'expiry': expiry, 'writer_exclusivity': writer}
                for failure in ('dirty', 'locked', 'invalid_lease', 'unavailable',
                                'registration_mismatch', 'ledger_pending'):
                    self.assertEqual(policy_module.scoped_recovery_blockers(
                        [lane], {'source': {'code': failure}}, policy, 'writer-b'), [lane])
                if writer == 'legacy':
                    self.assertEqual(policy_module.scoped_recovery_blockers(
                        [lane], {'source': {'code': None}}, policy, 'writer-b'), [lane])
                elif writer == 'block':
                    with self.assertRaisesRegex(self.module.SyncwheelError, 'belongs to writer'):
                        policy_module.scoped_recovery_blockers(
                            [lane], {'source': {'code': None}}, policy, 'writer-b')
                    self.assertEqual(policy_module.scoped_recovery_blockers(
                        [lane], {'source': {'code': None}}, policy, 'writer-a'), [])
                else:
                    stderr = io.StringIO()
                    with redirect_stderr(stderr):
                        result = policy_module.scoped_recovery_blockers(
                            [lane], {'source': {'code': None}}, policy, 'writer-b')
                    self.assertEqual(result, [])
                    self.assertEqual('WARNING' in stderr.getvalue(), writer == 'warn')

    def test_relaxed_policy_preserves_destructive_foreign_owner_hold(self):
        lane = self.open_lane()
        self.set_policy('self', '--expiry', 'off', '--writer-exclusivity', 'off')
        registry, _ = self.module.load_governed_worktree_registry(self.fixture.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)
        with mock.patch.dict(os.environ, {'SYNCWHEEL_LANE_OWNER': 'writer-b'}):
            hold = self.module._governed_worktrees(self.fixture.repo).automatic_cleanup_hold()
            self.assertIn('owner writer-a live', hold(registry['lanes'][0]))
        self.assertTrue(Path(lane['path']).exists())

    def test_scoped_command_requires_each_independent_relaxation(self):
        self.open_lane()
        registry, path = self.module.load_governed_worktree_registry(self.fixture.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)
        before = path.read_bytes()
        command = ('stack', 'resolve-integration', 'feature-a', '--empty')
        # Historical admission still blocks associated active lanes.
        result = self.fixture.run_cli(*command, expected=2)
        self.assertIn('recovery is required', result.stderr)
        self.set_policy('self', '--expiry', 'off')
        result = self.fixture.run_cli(*command, expected=2)
        self.assertIn('recovery is required', result.stderr)
        self.set_policy('repo', '--writer-exclusivity', 'off')
        self.fixture.run_cli(*command)
        self.assertEqual(path.read_bytes(), before)

    def test_relaxed_scoped_command_still_refuses_dirty_lane(self):
        lane = self.open_lane()
        (Path(lane['path']) / 'private-work.txt').write_text('keep this work\n')
        self.set_policy('self', '--expiry', 'off', '--writer-exclusivity', 'off')
        result = self.fixture.run_cli('stack', 'resolve-integration', 'feature-a',
                                      '--empty', expected=2)
        self.assertIn('[dirty]', result.stderr)
        self.assertEqual((Path(lane['path']) / 'private-work.txt').read_text(), 'keep this work\n')

    def test_profile_write_refuses_tracked_config_and_preserves_it(self):
        self.profile.write_text('{}\n')
        self.fixture.git('add', '-f', '.syncwheel/profile.local.json')
        self.command('repo', 'set', '--expiry', 'off', '--apply', expected=2)
        self.assertEqual(self.profile.read_text(), '{}\n')


if __name__ == '__main__':
    unittest.main()
