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

    def prepare_managed_ignore_rewrite(self):
        self.prepare(interrupted=True)
        path = self.f.repo / '.gitignore'
        text, _ = self.module.replace_managed_block(
            path.read_text(), self.module.SYNCWHEEL_GITIGNORE_MARKER,
            self.module.SYNCWHEEL_GITIGNORE_END_MARKER,
            self.module.syncwheel_gitignore_patterns(
                self.module.syncwheel_worktree_root(self.expected)),
            self.module.all_syncwheel_managed_patterns(
                self.module.syncwheel_worktree_root(self.expected)),
        )
        path.write_text(text)
        return path

    def recovery_args(self, plan):
        return SimpleNamespace(repo=str(self.f.repo), manifest=None, personal=None,
                               from_backup=self.backup, apply=True, reason='reviewed recovery',
                               plan_digest=plan['planDigest'])

    def test_recovery_cas_preserves_managed_ignore_without_staging_it(self):
        ignore = self.prepare_managed_ignore_rewrite()
        before = ignore.read_bytes()
        head = self.f.git('rev-parse', 'HEAD')
        plan = self.plan()
        self.assertFalse(plan['commitManagedIgnore'])
        self.assertTrue(plan['preservedManagedIgnore'])
        self.apply(plan)
        self.assertEqual(self.f.git('rev-parse', 'HEAD^'), head)
        self.assertEqual(self.f.git('diff', '--name-only', 'HEAD^', 'HEAD'),
                         '.syncwheel/manifest.json')
        self.assertEqual(ignore.read_bytes(), before)
        self.assertEqual(self.f.git('diff', '--cached', '--name-only'), '')
        self.assertEqual(self.f.tracked_status(), 'M .gitignore')
        again = self.plan()
        self.assertTrue(again['alreadyRestored'])
        restored = self.f.git('rev-parse', 'HEAD')
        self.apply(again)
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), restored)

    def test_recovery_refuses_late_ignore_edit_before_cas(self):
        ignore = self.prepare_managed_ignore_rewrite()
        plan, _ = self.module.control_recovery_plan(self.f.repo, self.path, self.backup)
        head = self.f.git('rev-parse', 'HEAD')
        index = self.f.git('write-tree')
        def edit(stage):
            if stage == 'intent_saved':
                ignore.write_text(ignore.read_text() + '\nforeign-generated/\n')
        with self.module.manifest_write_transaction(self.f.repo, self.path):
            with mock.patch.object(self.module, 'control_manifest_io_checkpoint', side_effect=edit):
                with self.assertRaisesRegex(self.module.SyncwheelError, 'inputs changed before apply'):
                    self.module.command_manifest_recover_control(self.recovery_args(plan))
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.f.git('write-tree'), index)
        self.assertIn('foreign-generated/', ignore.read_text())
        self.assertEqual(self.f.read_manifest()['stacks'], [])
        self.assertEqual(self.module.pending_control_manifest_intents(
            self.module.load_control_manifest_events(self.f.repo, self.path)), [])

    def test_recovery_refuses_late_product_commit_before_save(self):
        self.prepare(interrupted=True)
        plan, _ = self.module.control_recovery_plan(self.f.repo, self.path, self.backup)
        observed = []
        def commit(stage):
            if stage == 'intent_saved':
                (self.f.repo / 'gamma.txt').write_text('concurrent product\n')
                self.f.git('add', 'gamma.txt')
                self.f.git('commit', '-qm', 'test: concurrent product writer')
                observed.append(self.f.git('rev-parse', 'HEAD'))
        with self.module.manifest_write_transaction(self.f.repo, self.path):
            with mock.patch.object(self.module, 'control_manifest_io_checkpoint', side_effect=commit):
                with self.assertRaisesRegex(self.module.SyncwheelError, 'inputs changed before apply'):
                    self.module.command_manifest_recover_control(self.recovery_args(plan))
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), observed[0])
        self.assertEqual((self.f.repo / 'gamma.txt').read_text(), 'concurrent product\n')
        self.assertEqual(self.f.read_manifest()['stacks'], [])
        self.assertEqual(self.f.tracked_status(), '')

    def test_recovery_parent_cas_refuses_commit_after_last_guard(self):
        self.prepare(interrupted=True)
        plan, _ = self.module.control_recovery_plan(self.f.repo, self.path, self.backup)
        real_git = self.module.git
        observed = []
        def concurrent_commit(repo, *args, **kwargs):
            if args[:2] == ('update-ref', 'refs/heads/main') and not observed:
                (self.f.repo / 'gamma.txt').write_text('late concurrent product\n')
                self.f.git('add', 'gamma.txt')
                self.f.git('commit', '-qm', 'test: advance at ref CAS')
                observed.append(self.f.git('rev-parse', 'HEAD'))
            return real_git(repo, *args, **kwargs)
        with self.module.manifest_write_transaction(self.f.repo, self.path):
            with mock.patch.object(self.module, 'git', side_effect=concurrent_commit):
                with self.assertRaisesRegex(self.module.SyncwheelError, 'ref changed before its CAS'):
                    self.module.command_manifest_recover_control(self.recovery_args(plan))
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), observed[0])
        self.assertEqual(self.f.read_manifest()['stacks'], [])
        self.assertEqual(self.f.tracked_status(), '')

    def test_recovery_never_captures_ignore_edit_after_last_guard(self):
        ignore = self.prepare_managed_ignore_rewrite()
        plan, _ = self.module.control_recovery_plan(self.f.repo, self.path, self.backup)
        real_git = self.module.git
        edited = []
        def concurrent_edit(repo, *args, **kwargs):
            if args[:2] == ('update-ref', 'refs/heads/main') and not edited:
                ignore.write_text(ignore.read_text() + '\nforeign-generated/\n')
                edited.append(True)
            return real_git(repo, *args, **kwargs)
        with self.module.manifest_write_transaction(self.f.repo, self.path):
            with mock.patch.object(self.module, 'git', side_effect=concurrent_edit):
                with self.assertRaisesRegex(self.module.SyncwheelError, 'changed after control publication'):
                    self.module.command_manifest_recover_control(self.recovery_args(plan))
        self.assertIn('foreign-generated/', ignore.read_text())
        self.assertNotIn('foreign-generated/', self.f.git('show', 'HEAD:.gitignore'))
        self.assertEqual(self.f.git('diff', '--cached', '--name-only'), '')
        self.assertEqual(self.f.git('diff', '--name-only', 'HEAD^', 'HEAD'),
                         '.syncwheel/manifest.json')

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

    def interrupt_recovery(self, stage):
        self.prepare(interrupted=True)
        plan, _ = self.module.control_recovery_plan(self.f.repo, self.path, self.backup)
        def interrupt(observed):
            if observed == stage:
                raise RuntimeError('simulated process interruption')
        with self.assertRaisesRegex(RuntimeError, 'process interruption'):
            with self.module.manifest_write_transaction(self.f.repo, self.path):
                with mock.patch.object(self.module, 'control_manifest_io_checkpoint', side_effect=interrupt):
                    self.module.command_manifest_recover_control(self.recovery_args(plan))
        pending = self.module.control_recovery_pending(self.f.repo, self.path)
        self.assertIsNotNone(pending)
        self.assertEqual(pending['recovery_policy'], 'explicit-plan-only')
        self.assertEqual(pending['expected_control_commit'], plan['expectedControlCommit'])
        return plan

    def assert_cli_quarantine_preserves_foreign_work(self, stage):
        self.interrupt_recovery(stage)
        ignore = self.f.repo / '.gitignore'
        ignore.write_text(ignore.read_text() + '\nforeign-generated/\n')
        before = self.module.control_recovery_observation(self.f.repo)
        ledger = self.module.load_control_manifest_events(self.f.repo, self.path)
        result = self.f.run_cli('stack', 'create', 'independent', '--branch',
                               'pr/independent', '--base', 'main', expected=2)
        # At ref_updated the preflight may refuse the temporary staged manifest
        # even earlier. Either path must preserve the complete observed state.
        self.assertTrue('quarantined' in result.stderr or 'unrelated staged paths' in result.stderr)
        self.assertEqual(self.module.control_recovery_observation(self.f.repo), before)
        self.assertEqual(self.module.load_control_manifest_events(self.f.repo, self.path), ledger)
        self.assertNotIn('foreign-generated/', self.f.git('show', 'HEAD:.gitignore'))
        self.assertFalse(any(s['id'] == 'independent' for s in self.f.read_manifest()['stacks']))

    def test_restart_after_ref_cas_never_captures_foreign_ignore_via_cli(self):
        self.assert_cli_quarantine_preserves_foreign_work('ref_updated')

    def test_restart_after_alignment_never_captures_foreign_ignore_via_cli(self):
        self.assert_cli_quarantine_preserves_foreign_work('checkout_aligned')

    def test_restart_consumer_refuses_without_writing_a_receipt(self):
        self.interrupt_recovery('checkout_aligned')
        before = self.module.control_recovery_observation(self.f.repo)
        ledger = self.module.load_control_manifest_events(self.f.repo, self.path)
        with self.module.manifest_write_transaction(self.f.repo, self.path):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'quarantined'):
                self.module.recover_incomplete_control_manifest_persistence(
                    self.f.repo, self.path, self.expected, allow_new_operation=True)
        self.assertEqual(self.module.control_recovery_observation(self.f.repo), before)
        self.assertEqual(self.module.load_control_manifest_events(self.f.repo, self.path), ledger)

    def test_interrupted_recovery_requires_and_accepts_a_fresh_explicit_plan(self):
        original = self.interrupt_recovery('checkout_aligned')
        refused = self.f.run_cli('stack', 'create', 'independent', '--branch',
                                'pr/independent', '--base', 'main', expected=2)
        self.assertIn('quarantined', refused.stderr)
        self.apply(original, expected=2)
        fresh = self.plan()
        self.assertIsNotNone(fresh['pendingIntent'])
        self.assertTrue(fresh['alreadyRestored'])
        head = self.f.git('rev-parse', 'HEAD')
        self.apply(fresh)
        self.assertEqual(self.f.git('rev-parse', 'HEAD'), head)
        self.assertIsNone(self.module.control_recovery_pending(self.f.repo, self.path))
        self.assertEqual(self.f.tracked_status(), '')
        self.assertFalse(any(s['id'] == 'independent' for s in self.f.read_manifest()['stacks']))

    def test_fresh_explicit_plan_refuses_post_crash_foreign_ignore(self):
        self.interrupt_recovery('checkout_aligned')
        ignore = self.f.repo / '.gitignore'
        ignore.write_text(ignore.read_text() + '\nforeign-generated/\n')
        before = self.module.control_recovery_observation(self.f.repo)
        result = self.f.run_cli('manifest', 'recover-control', '--from-backup', self.backup, expected=2)
        self.assertIn('unrelated ignore changes', result.stderr)
        self.assertEqual(self.module.control_recovery_observation(self.f.repo), before)

    def test_restart_never_adopts_a_foreign_product_commit(self):
        self.interrupt_recovery('checkout_aligned')
        (self.f.repo / 'delta.txt').write_text('foreign product\n')
        self.f.git('add', 'delta.txt')
        self.f.git('commit', '-qm', 'test: concurrent product after crash')
        before = self.module.control_recovery_observation(self.f.repo)
        result = self.f.run_cli('stack', 'create', 'independent', '--branch',
                               'pr/independent', '--base', 'main', expected=2)
        self.assertIn('quarantined', result.stderr)
        self.assertEqual(self.module.control_recovery_observation(self.f.repo), before)
        result = self.f.run_cli('manifest', 'recover-control', '--from-backup', self.backup, expected=2)
        self.assertIn('does not reproduce the current product tree', result.stderr)

    def test_guarded_object_is_incompatible_with_older_automatic_consumers(self):
        self.prepare(interrupted=True)
        parent = self.f.git('rev-parse', 'HEAD')
        guarded = self.module.materialize_control_manifest_commit(
            self.f.repo, self.expected, parent,
            self.module.CONTROL_MANIFEST_EXPLICIT_RECOVERY_IDENTITY_VERSION)
        ordinary = self.module.materialize_control_manifest_commit(self.f.repo, self.expected, parent)
        self.assertNotEqual(guarded, ordinary)
        self.assertEqual(self.f.git('rev-parse', guarded + '^{tree}'),
                         self.f.git('rev-parse', ordinary + '^{tree}'))

    def test_direct_restore_cannot_bypass_the_pending_explicit_plan_gate(self):
        plan = self.interrupt_recovery('checkout_aligned')
        before = self.module.control_recovery_observation(self.f.repo)
        with self.module.manifest_write_transaction(self.f.repo, self.path):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'quarantined'):
                self.module.restore_control_manifest_after_integration_rebuild(
                    self.f.repo, self.path, self.expected, plan['controlReplayTip'], 'recovery',
                    identity_version=plan['controlIdentity'])
        self.assertEqual(self.module.control_recovery_observation(self.f.repo), before)

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
