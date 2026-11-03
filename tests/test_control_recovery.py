import json
import unittest
from types import SimpleNamespace
from unittest import mock

import test_syncwheel as fixtures


class ControlReplayTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SyncwheelFixtureTest('runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.module = self.f.load_syncwheel_module()
        self.path = self.f.repo / '.syncwheel/manifest.json'

    def prepare(self, interrupted=False):
        data = self.f.read_manifest()
        data.update(syncwheel_tracking='git-tracked', syncwheel_worktree_root='var/syncwheel')
        data['integration'] = {'branch': 'main', 'base': self.f.git('rev-parse', 'HEAD'),
                               'strategy': 'cherry-pick', 'stacks': []}
        data['stacks'] = []
        self.path.write_text(json.dumps(data) + '\n')
        self.module.ensure_syncwheel_metadata_excluded(self.f.repo, 'git-tracked', 'var/syncwheel')
        ignore = self.f.repo / '.gitignore'
        if interrupted:
            ignore.write_text(ignore.read_text() + '\n# Append-only transaction metadata\n.syncwheel/manifest-ledger/\n')
        self.f.git('add', '-f', '.syncwheel/manifest.json', '.gitignore')
        self.f.git('commit', '-qm', 'test: track replay base control')
        base = self.f.git('rev-parse', 'HEAD')
        self.f.git('switch', '-qc', 'pr/change')
        (self.f.repo / 'gamma.txt').write_text('preserve source product\n')
        self.f.git('add', 'gamma.txt')
        self.f.git('commit', '-qm', 'test: product change')
        self.source = self.f.git('rev-parse', 'HEAD')
        self.f.git('switch', '-q', 'main')
        data['integration'].update(base=base, stacks=['change'])
        data['stacks'] = [{'id': 'change', 'branch': 'pr/change', 'base': base,
                           'target_remote': 'origin', 'target_branch': 'main',
                           'integration_branch': 'main', 'commits': [self.source]}]
        self.path.write_text(json.dumps(data) + '\n')
        self.f.git('add', '-f', '.syncwheel/manifest.json')
        self.f.git('commit', '-qm', 'test: save declared control')
        self.expected, _ = self.module.load_manifest(self.f.repo, self.path)
        self.backup = 'backup/main-before-syncwheel-20261104T000000Z'
        self.f.git('branch', self.backup)
        if interrupted:
            # Test-only simulation of the pre-fix interrupted native replay.
            self.f.git('reset', '--hard', self.source)

    def plan(self):
        return json.loads(self.f.run_cli(
            'manifest', 'recover-control', '--from-backup', self.backup,
        ).stdout)

    def apply(self, plan, expected=0):
        return self.f.run_cli(
            'manifest', 'recover-control', '--from-backup', self.backup,
            '--plan-digest', plan['planDigest'], '--reason', 'recover reviewed replay',
            '--apply', expected=expected,
        )

    def test_ephemeral_rebuild_restores_normalized_tracked_control(self):
        self.prepare()
        for _ in range(2):
            self.f.run_cli('int', 'rebuild', '--replay-mode', 'ephemeral',
                           '--reason', 'verified ephemeral replay')
            restored, _ = self.module.load_manifest(self.f.repo, self.path)
            self.assertEqual(restored, self.expected)
            self.assertEqual(self.f.git('rev-parse', 'pr/change'), self.source)
            self.assertEqual((self.f.repo / 'gamma.txt').read_text(), 'preserve source product\n')
            self.assertEqual(self.f.tracked_status(), '')

    def test_unknown_manifest_change_is_not_acknowledged_as_replay(self):
        self.prepare()
        with self.module.manifest_write_transaction(self.f.repo, self.path, commit_tracked_changes=False):
            self.f.git('reset', '--hard', self.source)
            edited = self.f.read_manifest()
            edited['defaults']['base_branch'] = 'unreviewed-change'
            self.path.write_text(json.dumps(edited) + '\n')
            before = self.path.read_bytes()
            with self.assertRaisesRegex(self.module.SyncwheelError, 'manifest changed unexpectedly'):
                self.module.restore_control_manifest_after_integration_rebuild(
                    self.f.repo, self.path, self.expected, self.source, 'ephemeral',
                )
            self.assertEqual(self.path.read_bytes(), before)

    def test_recovery_is_plan_first_preserves_product_and_commits_only_control(self):
        self.prepare(interrupted=True)
        head = self.f.git('rev-parse', 'HEAD')
        plan = self.plan()
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.f.read_manifest()['stacks'], [])
        self.apply(plan)
        restored, _ = self.module.load_manifest(self.f.repo, self.path)
        self.assertEqual(restored, self.expected)
        self.assertEqual(self.f.git('rev-parse', 'HEAD^'), head)
        self.assertEqual(self.f.git('rev-parse', 'pr/change'), self.source)
        paths = self.f.git('diff', '--name-only', 'HEAD^', 'HEAD').splitlines()
        self.assertIn('.syncwheel/manifest.json', paths)
        self.assertLessEqual(set(paths), {'.syncwheel/manifest.json', '.gitignore'})
        self.assertEqual(self.f.tracked_status(), '')
        again = self.plan()
        self.assertTrue(again['alreadyRestored'])
        restored_head = self.f.git('rev-parse', 'HEAD')
        self.apply(again)
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), restored_head)

    def test_recovery_rejects_stale_plan(self):
        self.prepare(interrupted=True)
        plan = self.plan()
        self.f.git('commit', '--allow-empty', '-qm', 'test: concurrent ref advance')
        result = self.apply(plan, expected=2)
        self.assertIn('exact current --plan-digest', result.stderr)
        self.assertEqual(self.f.read_manifest()['stacks'], [])

    def test_recovery_rejects_changed_product_tree(self):
        self.prepare(interrupted=True)
        (self.f.repo / 'gamma.txt').write_text('independent product edit\n')
        self.f.git('add', 'gamma.txt')
        self.f.git('commit', '-qm', 'test: other product change')
        result = self.f.run_cli('manifest', 'recover-control', '--from-backup', self.backup, expected=2)
        self.assertIn('does not reproduce the current product tree', result.stderr)
        self.assertEqual((self.f.repo / 'gamma.txt').read_text(), 'independent product edit\n')

    def test_recovery_rejects_edited_manifest_and_ignore(self):
        self.prepare(interrupted=True)
        original = self.path.read_bytes()
        self.path.write_bytes(original + b'\n')
        result = self.f.run_cli('manifest', 'recover-control', '--from-backup', self.backup, expected=2)
        self.assertIn('edited replay manifest', result.stderr)
        self.path.write_bytes(original)
        ignore = self.f.repo / '.gitignore'
        ignore.write_text(ignore.read_text() + '\n!independent-file\n')
        result = self.f.run_cli('manifest', 'recover-control', '--from-backup', self.backup, expected=2)
        self.assertIn('unrelated ignore changes', result.stderr)

    def test_recovery_requires_native_backup_ref(self):
        self.prepare(interrupted=True)
        result = self.f.run_cli('manifest', 'recover-control', '--from-backup', 'pr/change', expected=2)
        self.assertIn('explicit native integration backup ref', result.stderr)

    def test_recovery_remedy_still_refuses_uncommitted_product_work(self):
        self.prepare(interrupted=True)
        path = self.f.repo / 'gamma.txt'
        path.write_text('preserve unrelated uncommitted work\n')
        head = self.f.git('rev-parse', 'HEAD')
        result = self.f.run_cli('manifest', 'recover-control', '--from-backup', self.backup, expected=2)
        self.assertIn('unrelated checkout changes', result.stderr)
        self.assertEqual(path.read_text(), 'preserve unrelated uncommitted work\n')
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), head)

    def test_recovery_preview_is_read_only_and_apply_requires_a_plan(self):
        parser = self.module.build_parser()
        argv = ['manifest', 'recover-control', '--from-backup', 'backup/main-before-syncwheel-test']
        preview, apply = parser.parse_args(argv), parser.parse_args([*argv, '--apply'])
        self.assertFalse(self.module.syncwheel_mutation_requested(preview))
        self.assertFalse(self.module.manifest_mutation_requested(preview))
        self.assertTrue(self.module.syncwheel_mutation_requested(apply))
        self.assertTrue(self.module.manifest_mutation_requested(apply))
        self.prepare(interrupted=True)
        head = self.f.git('rev-parse', 'HEAD')
        result = self.f.run_cli(
            'manifest', 'recover-control', '--from-backup', self.backup,
            '--apply', '--reason', 'reviewed recovery', expected=2,
        )
        self.assertIn('exact current --plan-digest', result.stderr)
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), head)

    def test_recovery_rechecks_concurrent_edit_at_apply_boundary(self):
        self.prepare(interrupted=True)
        plan, _ = self.module.control_recovery_plan(self.f.repo, self.path, self.backup)
        args = SimpleNamespace(repo=str(self.f.repo), manifest=None, personal=None,
                               from_backup=self.backup, apply=True, reason='reviewed recovery',
                               plan_digest=plan['planDigest'])
        def edit():
            self.path.write_bytes(self.path.read_bytes() + b'\n')
        with self.module.manifest_write_transaction(self.f.repo, self.path, commit_tracked_changes=False):
            with mock.patch.object(self.module, 'control_recovery_checkpoint', side_effect=edit):
                with self.assertRaisesRegex(self.module.SyncwheelError, 'inputs changed before apply'):
                    self.module.command_manifest_recover_control(args)
        self.assertEqual(self.f.read_manifest()['stacks'], [])

    def test_recovery_rechecks_remote_tracking_inputs_at_apply_boundary(self):
        self.prepare(interrupted=True)
        plan, _ = self.module.control_recovery_plan(self.f.repo, self.path, self.backup)
        args = SimpleNamespace(repo=str(self.f.repo), manifest=None, personal=None,
                               from_backup=self.backup, apply=True, reason='reviewed recovery',
                               plan_digest=plan['planDigest'])
        def move_base():
            self.f.git('update-ref', 'refs/remotes/origin/main', self.source)
        with self.module.manifest_write_transaction(self.f.repo, self.path, commit_tracked_changes=False):
            with mock.patch.object(self.module, 'control_recovery_checkpoint', side_effect=move_base):
                with self.assertRaisesRegex(self.module.SyncwheelError, 'inputs changed before apply'):
                    self.module.command_manifest_recover_control(args)
        self.assertEqual(self.f.read_manifest()['stacks'], [])

    def prepare_state_remote(self):
        self.f.git('switch', '-qc', 'syncwheel/state/default')
        state_path = self.f.repo / '.syncwheel/coordination-state.json'
        state_path.write_text(json.dumps({
            'manifest': {'version': 1}, 'coordination_id': 'default', 'marker': 'observed',
        }) + '\n')
        self.f.git('add', '-f', '.syncwheel/coordination-state.json')
        self.f.git('commit', '-qm', 'test: observed coordination object')
        observed = self.f.git('rev-parse', 'HEAD')
        self.f.git('switch', '-q', 'main')
        origin = self.f.tmp / 'origin.git'
        self.f.git('clone', '--bare', '-q', str(self.f.repo), str(origin))
        self.f.git('remote', 'add', 'origin', str(origin))
        config = {'remote': 'origin', 'id': 'default',
                  'state_branch': 'syncwheel/state/default', 'claims': 'advisory'}
        return observed, origin, config

    def read_state_json(self, repo, commit, *_args):
        # Real Git object reads isolate provenance from the unchanged schema parser.
        return json.loads(self.f.git('show', f'{commit}:.syncwheel/coordination-state.json'))

    def test_state_read_survives_an_unrelated_fetch_overwriting_fetch_head(self):
        observed, _origin, config = self.prepare_state_remote()
        real_git = self.module.git
        def interfering_fetch(repo, *args, **kwargs):
            result = real_git(repo, *args, **kwargs)
            if args and args[0] == 'fetch':
                self.f.git('fetch', '--quiet', 'origin', 'main')
            return result
        with mock.patch.object(self.module, 'git', side_effect=interfering_fetch), \
             mock.patch.object(self.module, 'coordination_state_from_commit', side_effect=self.read_state_json):
            result = self.module.read_remote_coordination_state(
                self.f.repo, config, local_manifest_version=1,
            )
        self.assertNotEqual(self.f.git('rev-parse', 'FETCH_HEAD'), observed)
        self.assertEqual(result['tip'], observed)
        self.assertEqual(result['state']['marker'], 'observed')

    def test_state_scan_keeps_each_observed_tip_when_fetch_head_is_overwritten(self):
        observed, _origin, config = self.prepare_state_remote()
        real_git = self.module.git
        def interfering_fetch(repo, *args, **kwargs):
            result = real_git(repo, *args, **kwargs)
            if args and args[0] == 'fetch':
                self.f.git('fetch', '--quiet', 'origin', 'main')
            return result
        with mock.patch.object(self.module, 'git', side_effect=interfering_fetch), \
             mock.patch.object(self.module, 'coordination_state_from_commit', side_effect=self.read_state_json):
            results = self.module.read_remote_coordination_states(self.f.repo, config)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tip'], observed)
        self.assertEqual(results[0]['state']['marker'], 'observed')

    def test_state_read_keeps_tip_and_payload_paired_when_remote_advances(self):
        observed, origin, config = self.prepare_state_remote()
        self.f.git('switch', '-q', 'syncwheel/state/default')
        path = self.f.repo / '.syncwheel/coordination-state.json'
        data = json.loads(path.read_text())
        data['marker'] = 'advanced'
        path.write_text(json.dumps(data) + '\n')
        self.f.git('add', '-f', '.syncwheel/coordination-state.json')
        self.f.git('commit', '-qm', 'test: later coordination object')
        advanced = self.f.git('rev-parse', 'HEAD')
        self.f.git('switch', '-q', 'main')
        self.f.git('push', '--quiet', 'origin', f'{advanced}:refs/heads/later-object')
        real_observe = self.module.remote_ref_tips
        def advance_after_observation(repo, remote, refs):
            result = real_observe(repo, remote, refs)
            self.f.git('--git-dir=' + str(origin), 'update-ref',
                       'refs/heads/syncwheel/state/default', advanced, observed)
            return result
        with mock.patch.object(self.module, 'remote_ref_tips', side_effect=advance_after_observation), \
             mock.patch.object(self.module, 'coordination_state_from_commit', side_effect=self.read_state_json):
            result = self.module.read_remote_coordination_state(
                self.f.repo, config, local_manifest_version=1,
            )
        self.assertEqual(self.f.git('rev-parse', 'FETCH_HEAD'), advanced)
        self.assertEqual(result['tip'], observed)
        self.assertEqual(result['state']['marker'], 'observed')
