"""Synthetic divergent histories delivered without moving the preserved lane."""
import contextlib
import io
import json
import os
import shlex
import sys
from unittest import mock
import unittest

import test_syncwheel as fixtures


class PreservedCandidateTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SyncwheelFixtureTest()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.path, self.preserved, self.base, self.origin = self.f.captured_push_copy()
        self.module = self.f.load_syncwheel_module()
        # The published line has a different history but the preserved tree is
        # carried exactly by the adopted candidate. No production refs are used.
        self.remote = self.f.git('commit-tree', self.base + '^{tree}', '-p', self.base,
                                 '-m', 'Published source history')
        self.candidate = self.f.git('commit-tree', self.preserved + '^{tree}',
                                    '-p', self.base, '-p', self.remote,
                                    '-m', 'Equivalent candidate')
        self.f.git('--git-dir=' + str(self.origin), 'fetch', str(self.f.repo), self.remote)
        self.f.git('--git-dir=' + str(self.origin), 'update-ref',
                   'refs/heads/pr/feature-a', self.remote)
        self.f.git('update-ref', 'refs/heads/pr/feature-a', self.remote)
        self.f.git('fetch', 'origin')
        # Seed the pre-adoption stack source; perform the real supported adoption.
        args = ('stack', 'set', 'feature-a', '--base', self.base,
                '--adopt-tip', self.candidate)
        plan = json.loads(self.f.run_cli(*args).stdout)
        self.f.run_cli(*args, '--plan-digest', plan['planDigest'], '--apply')
        # Persisted hooks need an executable outside every checkout. Keep the
        # test independent of a globally installed CLI and exercise this source.
        bin_dir = self.f.tmp / 'bin'
        bin_dir.mkdir()
        executable = bin_dir / 'syncwheel'
        executable.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' '
                              + shlex.quote(str(fixtures.CLI)) + ' "$@"\n')
        executable.chmod(0o755)
        environment = mock.patch.dict(os.environ, {
            'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', ''),
            'SYNCWHEEL_UPDATE_MODE': 'off',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.args = ('stack', 'push', 'feature-a', '--remote', 'origin',
                     '--retain-captured-worktrees', 'Keep reviewed source',
                     '--allow-equivalent-candidate')

    def remote_head(self):
        return self.f.git('--git-dir=' + str(self.origin), 'rev-parse', 'pr/feature-a')

    def test_delivery_through_guard_hook_preserves_lane_and_records_both_refs(self):
        registry = self.module.governed_worktree_registry_path(self.f.repo)
        before = registry.read_bytes()
        ignored = (self.path / 'node_modules/cached.bin').read_bytes()
        self.f.run_cli('hooks', 'install', '--apply')
        # Old retention path still refuses history differences.
        self.assertIn('match exactly', self.f.run_cli(*self.args[:-1], expected=2).stderr)
        self.f.run_cli(*self.args, '--dry-run')
        self.assertEqual(self.remote_head(), self.remote)
        result = self.f.run_cli(*self.args)
        self.assertIn(self.preserved, result.stdout)
        self.assertEqual(self.remote_head(), self.candidate)
        self.assertEqual(self.f.git('-C', str(self.path), 'rev-parse', 'HEAD'), self.preserved)
        self.assertEqual(self.f.git('rev-parse', 'syncwheel/lane/kept-validation'), self.preserved)
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual((self.path / 'node_modules/cached.bin').read_bytes(), ignored)
        events = self.module.load_ledger_events(self.f.repo, self.f.repo / '.syncwheel/manifest.json')
        prepared = next(e for e in events if e['type'] == 'preserved_candidate_delivery_prepared')
        proof = prepared['payload']['proofs'][0]
        self.assertEqual(proof['preserved_tip'], self.preserved)
        self.assertEqual(proof['candidate_tip'], self.candidate)
        self.assertEqual(proof['remote_tip'], self.remote)
        self.assertEqual(proof['equivalence'], 'exact-tree')
        pushed = [e for e in events if e['type'] == 'stack_pushed'][-1]
        self.assertEqual(pushed['payload']['preserved_candidate_delivery'], [proof])

    def test_refuses_non_fast_forward_even_with_an_identical_tree(self):
        foreign = self.f.git('commit-tree', self.preserved + '^{tree}',
                             '-p', self.remote, '-m', 'Concurrent remote change')
        self.f.git('--git-dir=' + str(self.origin), 'fetch', str(self.f.repo), foreign)
        self.f.git('--git-dir=' + str(self.origin), 'update-ref', 'refs/heads/pr/feature-a', foreign)
        result = self.f.run_cli(*self.args, expected=2)
        self.assertIn('fast-forward', result.stderr)
        self.assertEqual(self.remote_head(), foreign)

    def test_refuses_missing_remote_head(self):
        self.f.git('--git-dir=' + str(self.origin), 'update-ref', '-d', 'refs/heads/pr/feature-a')
        self.assertIn('existing remote head', self.f.run_cli(*self.args, expected=2).stderr)

    def test_refuses_tree_difference_dirty_lane_and_missing_lock(self):
        self.f.git('worktree', 'unlock', str(self.path))
        self.assertIn('retention lock', self.f.run_cli(*self.args, expected=2).stderr)
        self.f.git('worktree', 'lock', '--reason', 'Retain source', str(self.path))
        (self.path / 'untracked.txt').write_text('unreviewed\n')
        self.assertIn('source changes remain', self.f.run_cli(*self.args, expected=2).stderr)
        self.f.git('-C', str(self.path), 'add', 'untracked.txt')
        self.f.git('-C', str(self.path), 'commit', '-qm', 'Additional lane content')
        self.assertIn('tree differs', self.f.run_cli(*self.args, expected=2).stderr)
        self.assertEqual(self.remote_head(), self.remote)

    def test_requires_explicit_retention_adoption_and_no_force(self):
        self.assertIn('requires --retain', self.f.run_cli(
            'stack', 'push', 'feature-a', '--allow-equivalent-candidate', expected=2).stderr)
        self.assertIn('refuses force', self.f.run_cli(*self.args, '--force-with-lease', expected=2).stderr)
        manifest = self.f.read_manifest()
        stack = next(s for s in manifest['stacks'] if s['id'] == 'feature-a')
        stack.pop('adopted_history')
        (self.f.repo / '.syncwheel/manifest.json').write_text(json.dumps(manifest) + '\n')
        self.assertIn('unchanged adopted', self.f.run_cli(*self.args, expected=2).stderr)

    def test_refuses_passthrough_options_including_force_and_ref_selection(self):
        for option in ('--force', '-f', '--force-with-lease',
                       '--force-with-lease=refs/heads/pr/feature-a:' + self.remote,
                       '--no-force-with-lease', '--mirror', '--all', '--delete', '--prune'):
            with self.subTest(option=option):
                result = self.f.run_cli(*self.args, '--', option, expected=2)
                self.assertIn('refuses force and passthrough', result.stderr)
                self.assertEqual(self.remote_head(), self.remote)

    def assert_remote_race_refused(self, replacement):
        original = self.module.run_authorized_push
        ref = 'refs/heads/pr/feature-a'

        def race(*args, **kwargs):
            command = args[1]
            self.assertIn('--force-with-lease=' + ref + ':' + self.remote, command)
            if replacement:
                self.f.git('--git-dir=' + str(self.origin), 'fetch', str(self.f.repo), replacement)
                self.f.git('--git-dir=' + str(self.origin), 'update-ref', ref, replacement)
            else:
                self.f.git('--git-dir=' + str(self.origin), 'update-ref', '-d', ref)
            return original(*args, **kwargs)

        argv = ['syncwheel', *self.args, '--repo', str(self.f.repo)]
        with mock.patch.object(sys, 'argv', argv), \
                mock.patch.object(self.module, 'run_authorized_push', side_effect=race), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.module.main(), 2)
        observed = self.module.remote_ref_tips(self.f.repo, 'origin', [ref])[ref]
        self.assertEqual(observed, replacement)
        events = self.module.load_ledger_events(self.f.repo, self.f.repo / '.syncwheel/manifest.json')
        self.assertTrue(any(e['type'] == 'preserved_candidate_delivery_prepared' for e in events))
        self.assertFalse(any(e['type'] == 'stack_pushed' for e in events))
        self.assertEqual(self.f.git('rev-parse', 'syncwheel/lane/kept-validation'), self.preserved)

    def test_exact_lease_refuses_remote_deletion_after_qualification(self):
        self.assert_remote_race_refused(None)

    def test_exact_lease_refuses_remote_advance_after_qualification(self):
        foreign = self.f.git('commit-tree', self.preserved + '^{tree}',
                             '-p', self.remote, '-m', 'Concurrent publication')
        self.assert_remote_race_refused(foreign)

    def test_rechecks_remote_and_lane_after_preflight(self):
        args = self.module.build_parser().parse_args([*self.args, '--repo', str(self.f.repo)])
        self.module.governed_worktree_preflight(args)
        manifest = self.f.read_manifest()
        self.module.verify_retained_captured_worktrees(self.f.repo, manifest, args)
        # Advance remote to another ancestor of the candidate: still FF, but
        # the frozen observation changed and publication must be requalified.
        self.f.git('--git-dir=' + str(self.origin), 'update-ref', 'refs/heads/pr/feature-a', self.base)
        with self.assertRaisesRegex(self.module.SyncwheelError, 'changed after preflight'):
            self.module.verify_retained_captured_worktrees(self.f.repo, manifest, args)
        self.f.git('--git-dir=' + str(self.origin), 'update-ref', 'refs/heads/pr/feature-a', self.remote)
        self.f.git('-C', str(self.path), 'config', 'status.showUntrackedFiles', 'no')
        (self.path / 'hidden.txt').write_text('unreviewed\n')
        with self.assertRaisesRegex(self.module.SyncwheelError, 'source changes remain'):
            self.module.verify_retained_captured_worktrees(self.f.repo, manifest, args)


class CoordinatedPreservedCandidateTest(unittest.TestCase):
    def test_atomic_publication_uses_candidate_and_preserves_active_lane(self):
        import test_coordination as coordination
        c = coordination.ActiveActiveCoordinationTest()
        c.setUp()
        self.addCleanup(c.tearDown)
        origin = c.create_remote()
        repo = c.clone(origin, 'preserved-candidate')
        c.init_coordinated(repo)
        c.run_cli(repo, 'int', 'push')
        base = c.git(repo, 'rev-parse', 'origin/main').stdout.strip()
        c.run_cli(repo, 'stack', 'create', 'candidate', '--base', base)
        c.run_cli(repo, 'stack', 'rebuild', 'candidate')
        c.run_cli(repo, 'stack', 'push', 'candidate')
        lane = json.loads(c.run_cli(repo, 'worktree', 'open', 'preserved',
                                   '--into', 'candidate', '--base', base, '--json').stdout)['lane']
        from pathlib import Path
        path = Path(lane['path'])
        (path / 'feature.txt').write_text('preserved content\n')
        c.git(path, 'add', 'feature.txt')
        c.git(path, 'commit', '-qm', 'Preserved source')
        tip = c.git(path, 'rev-parse', 'HEAD').stdout.strip()
        c.git(repo, 'worktree', 'lock', '--reason', 'Retain source', str(path))
        candidate = c.git(repo, 'commit-tree', tip + '^{tree}', '-p', base,
                          '-m', 'Equivalent delivery history').stdout.strip()
        args = ('stack', 'set', 'candidate', '--base', base, '--adopt-tip', candidate)
        plan = json.loads(c.run_cli(repo, *args).stdout)
        c.run_cli(repo, *args, '--plan-digest', plan['planDigest'], '--apply')
        module = c.load_module()
        registry = module.governed_worktree_registry_path(repo)
        before = registry.read_bytes()
        c.run_cli(repo, 'stack', 'push', 'candidate',
                  '--retain-captured-worktrees', 'Keep reviewed source', '--allow-equivalent-candidate')
        _, state = c.remote_state(origin)
        self.assertEqual(state['managed_refs']['refs/heads/pr/candidate'], candidate)
        self.assertEqual(c.git(origin, 'rev-parse', 'refs/heads/pr/candidate').stdout.strip(), candidate)
        self.assertEqual(c.git(path, 'rev-parse', 'HEAD').stdout.strip(), tip)
        self.assertEqual(registry.read_bytes(), before)
