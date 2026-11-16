import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_syncwheel as fixtures


class StackPublicationLaneTest(unittest.TestCase):
    owner = 'fixture-author'
    other_owner = 'fixture-successor'
    stack_ref = 'refs/heads/pr/publication'

    def setUp(self):
        self.fixture = fixtures.SyncwheelFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.module = self.fixture.load_syncwheel_module()
        self.manifest_path = self.fixture.repo / '.syncwheel/manifest.json'
        self.fixture.run_cli('stack', 'create', 'publication', '--base', 'main')
        self.lane = self.open_lane('source', 'publication', self.owner)
        self.path = Path(self.lane['path'])
        self.source = self.path / 'source.txt'
        self.source.write_text('declared source\n')
        self.git_in(self.path, 'add', 'source.txt')
        self.git_in(self.path, 'commit', '-qm', 'feat: declare source')
        self.tip = self.git_in(self.path, 'rev-parse', 'HEAD')

        # Put the lane commit in the shared recipe without native capture so the
        # original registration remains active.
        manifest = self.fixture.read_manifest()
        stack = next(stack for stack in manifest['stacks'] if stack['id'] == 'publication')
        stack['commits'] = [self.tip]
        self.manifest_path.write_text(json.dumps(manifest) + '\n')
        self.fixture.git('update-ref', self.stack_ref, self.tip)

        self.origin = self.fixture.tmp / 'origin.git'
        self.fixture.git('init', '--bare', '-q', str(self.origin))
        self.fixture.git('remote', 'add', 'origin', str(self.origin))
        self.fixture.git('--git-dir=' + str(self.origin), 'fetch', '-q',
                         str(self.fixture.repo), 'main:' + self.stack_ref)
        self.remote_base = self.origin_tip()

    def git_in(self, path, *args):
        return self.fixture.git('-C', str(path), *args)

    def open_lane(self, name, stack, owner):
        output = self.fixture.run_cli(
            'worktree', 'open', name, '--into', stack, '--json',
            extra_env={'SYNCWHEEL_LANE_OWNER': owner},
        )
        return json.loads(output.stdout)['lane']

    def origin_tip(self):
        return self.fixture.git('--git-dir=' + str(self.origin), 'rev-parse', self.stack_ref)

    def registry_path(self):
        return self.module.governed_worktree_registry_path(self.fixture.repo)

    def snapshot(self):
        return (
            self.fixture.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads'),
            self.fixture.git('worktree', 'list', '--porcelain'),
            self.registry_path().read_bytes(),
            self.manifest_path.read_bytes(),
            self.git_in(self.path, 'status', '--porcelain', '--untracked-files=all'),
            self.source.read_bytes(),
        )

    def mutate_lane_record(self, **fields):
        registry, _ = self.module.load_governed_worktree_registry(self.fixture.repo)
        record = next(lane for lane in registry['lanes'] if lane['id'] == self.lane['id'])
        record.update(fields)
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)

    def push_args(self, *extra):
        return ('stack', 'push', 'publication', '--remote', 'origin',
                '--manifest', str(self.manifest_path), *extra)

    def push(self, *extra, owner=None, cwd=None, expected=0):
        return self.fixture.run_cli(
            *self.push_args(*extra), cwd=cwd, expected=expected,
            extra_env={'SYNCWHEEL_LANE_OWNER': owner or self.owner},
        )

    def assert_refused_unchanged(self, detail, *extra):
        before = self.snapshot()
        result = self.push(*extra, expected=2)
        self.assertIn(detail, result.stderr)
        self.assertEqual(self.origin_tip(), self.remote_base)
        self.assertEqual(self.snapshot(), before)

    def test_owner_can_push_from_active_source_without_capture(self):
        before = self.snapshot()

        self.push(cwd=self.path)

        self.assertEqual(self.origin_tip(), self.tip)
        self.assertEqual(self.snapshot(), before)

    def test_successor_can_push_exact_tip_while_unrelated_foreign_lane_is_dirty(self):
        unrelated = self.open_lane('unrelated', 'feature-a', 'fixture-unrelated-author')
        foreign_path = Path(unrelated['path'])
        foreign_source = foreign_path / 'scratch.txt'
        foreign_source.write_bytes(b'unrelated work\x00')
        before = self.snapshot()
        foreign_status = self.git_in(foreign_path, 'status', '--porcelain')

        self.push(owner=self.other_owner)

        self.assertEqual(self.origin_tip(), self.tip)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(foreign_source.read_bytes(), b'unrelated work\x00')
        self.assertEqual(self.git_in(foreign_path, 'status', '--porcelain'), foreign_status)

    def test_explicit_retain_accepts_active_source(self):
        before = self.snapshot()

        result = self.push('--retain-captured-worktrees', 'Preserve the validation checkout')

        self.assertIn('Preserve the validation checkout', result.stdout)
        self.assertEqual(self.origin_tip(), self.tip)
        self.assertEqual(self.snapshot(), before)

    def test_untracked_source_refuses(self):
        untracked = self.path / 'untracked.txt'
        untracked.write_bytes(b'work in progress\x00')

        self.assert_refused_unchanged('tracked or untracked source changes remain')

        self.assertEqual(untracked.read_bytes(), b'work in progress\x00')

    def test_tracked_source_change_refuses_explicit_retain(self):
        self.source.write_text('unpublished work\n')

        self.assert_refused_unchanged(
            'tracked or untracked source changes remain',
            '--retain-captured-worktrees', 'Retain validation',
        )

    def test_exact_publication_tip_with_an_undeclared_commit_refuses(self):
        self.source.write_text('extra source\n')
        self.git_in(self.path, 'commit', '-qam', 'feat: extra source')
        self.fixture.git('update-ref', self.stack_ref, self.git_in(self.path, 'rev-parse', 'HEAD'))

        self.assert_refused_unchanged('not fully owned by the selected stack')

    def test_same_tree_different_lane_tip_refuses(self):
        self.git_in(self.path, 'commit', '--allow-empty', '-qm', 'Same tree, new tip')

        self.assert_refused_unchanged('publication HEAD must match exactly')

    def test_wrong_git_registration_refuses(self):
        wrong_path = self.path.parent / 'wrong-checkout'
        self.fixture.git('worktree', 'add', '--detach', str(wrong_path), self.tip)
        self.mutate_lane_record(path=str(wrong_path))

        self.assert_refused_unchanged('Git worktree registration does not match')

    def test_pending_cleanup_refuses(self):
        self.mutate_lane_record(pending_reason='branch_advanced')

        self.assert_refused_unchanged('interrupted cleanup must be recovered first')

    def test_captured_lane_requires_explicit_retain(self):
        retain = ('--retain-captured-worktrees', 'Keep captured source')
        self.mutate_lane_record(state='captured_pending_cleanup')
        self.assert_refused_unchanged('native capture evidence is missing', *retain)

        self.mutate_lane_record(captured_at='2020-01-01T00:00:00+00:00')
        self.assert_refused_unchanged('retention lock is required', *retain)
        self.fixture.git('worktree', 'lock', '--reason', 'Retain the captured source', str(self.path))
        self.assert_refused_unchanged('an active lane')

        before = self.snapshot()
        self.push(*retain)
        self.assertEqual(self.origin_tip(), self.tip)
        self.assertEqual(self.snapshot(), before)

    def test_lane_registry_change_at_final_push_check_refuses(self):
        before = self.snapshot()
        verify = self.module.verify_retained_captured_worktrees
        calls = 0

        def change_before_final_check(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.mutate_lane_record(owner=self.other_owner)
            return verify(*args, **kwargs)

        stderr = io.StringIO()
        argv = ['syncwheel', *self.push_args('--repo', str(self.fixture.repo))]
        with (
            mock.patch.object(sys, 'argv', argv),
            mock.patch.object(self.module, 'verify_retained_captured_worktrees',
                              side_effect=change_before_final_check),
            contextlib.redirect_stderr(stderr),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(self.module.main(), 2)

        self.assertEqual(calls, 2)
        self.assertIn('changed after preflight', stderr.getvalue())
        self.assertEqual(self.origin_tip(), self.remote_base)
        after = self.snapshot()
        self.assertEqual(after[:2] + after[3:], before[:2] + before[3:])
        registry = json.loads(after[2])
        self.assertEqual(registry['lanes'], [{**self.lane, 'owner': self.other_owner}])
