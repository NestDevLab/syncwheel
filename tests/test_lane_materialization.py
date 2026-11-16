import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import test_syncwheel as fixtures


class LaneMaterializationTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SyncwheelFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.module = self.fixture.load_syncwheel_module()

    def create_empty_stack(self):
        self.fixture.run_cli('stack', 'create', 'new-stack', '--base', 'main')

    def open_lane(self, base='main', stack='new-stack', *, expected=0):
        return self.fixture.run_cli(
            'worktree', 'open', 'new-lane', '--into', stack, '--base', base, '--json',
            expected=expected,
        )

    def local_state(self):
        registry = self.module.governed_worktree_registry_path(self.fixture.repo)
        return (
            self.fixture.git('show-ref'),
            self.fixture.git('worktree', 'list', '--porcelain'),
            registry.read_bytes() if registry.exists() else None,
        )

    def open_args(self):
        return SimpleNamespace(
            repo=str(self.fixture.repo), manifest=None, personal=None, lane='new-lane',
            into='new-stack', base='main', full=False, json=True,
        )

    def test_native_open_materializes_empty_stack_at_exact_base(self):
        self.create_empty_stack()
        base = self.fixture.git('rev-parse', 'main')
        self.assertIsNone(self.module.ref_tip(self.fixture.repo, 'refs/heads/pr/new-stack'))

        output = json.loads(self.open_lane().stdout)

        self.assertEqual(output['stack_materialization'], {'branch': 'pr/new-stack', 'tip': base})
        self.assertEqual(self.fixture.git('rev-parse', 'refs/heads/pr/new-stack'), base)
        self.assertEqual(self.fixture.git('-C', output['lane']['path'], 'rev-parse', 'HEAD'), base)
        self.assertEqual(json.loads(Path(output['registry_path']).read_text())['lanes'], [output['lane']])

    def test_missing_stack_with_different_commit_at_same_tree_refuses(self):
        self.create_empty_stack()
        tree = self.fixture.git('rev-parse', 'main^{tree}')
        different = self.fixture.git('commit-tree', tree, '-p', 'main', '-m', 'Same tree')
        before = self.local_state()

        result = self.open_lane(different, expected=2)

        self.assertIn('current exact projection', result.stderr)
        self.assertEqual(self.local_state(), before)

    def test_missing_nonempty_stack_refuses_even_at_exact_projection(self):
        self.fixture.prepare_replay_stack()
        before = self.local_state()

        result = self.open_lane(stack='replay', expected=2)

        self.assertIn('current exact projection', result.stderr)
        self.assertEqual(self.local_state(), before)

    def test_existing_stack_branch_is_never_realigned(self):
        self.create_empty_stack()
        self.fixture.git('branch', 'pr/new-stack', 'main~1')
        before = self.local_state()

        result = self.open_lane(expected=2)

        self.assertIn('current exact projection', result.stderr)
        self.assertEqual(self.local_state(), before)

    def test_dangling_symbolic_stack_branch_is_preserved(self):
        self.create_empty_stack()
        stack_ref = 'refs/heads/pr/new-stack'
        target = 'refs/heads/missing'
        self.fixture.git('symbolic-ref', stack_ref, target)

        result = self.open_lane(expected=2)

        self.assertIn('current exact projection', result.stderr)
        self.assertEqual(self.fixture.git('symbolic-ref', stack_ref), target)
        self.assertEqual(self.module.load_governed_worktree_registry(self.fixture.repo)[0]['lanes'], [])

    def test_worktree_add_failure_leaves_stack_branch_missing(self):
        self.create_empty_stack()
        self.fixture.git('branch', 'syncwheel/lane/new-lane', 'main~1')
        before = self.local_state()

        self.open_lane(expected=2)

        self.assertEqual(self.local_state(), before)

    def test_stack_branch_created_concurrently_is_preserved(self):
        self.create_empty_stack()
        original_git = self.module.git
        competing_tip = self.fixture.git('rev-parse', 'main~1')

        def create_competing_branch(repo_root, *args, **kwargs):
            if args[:3] == ('update-ref', '--no-deref', 'refs/heads/pr/new-stack'):
                self.fixture.git('update-ref', 'refs/heads/pr/new-stack', competing_tip)
            return original_git(repo_root, *args, **kwargs)

        with mock.patch.object(self.module, 'git', side_effect=create_competing_branch):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'cannot materialize'):
                self.module.command_worktree_open(self.open_args())

        self.assertEqual(self.fixture.git('rev-parse', 'refs/heads/pr/new-stack'), competing_tip)
        self.assertIsNone(self.module.ref_tip(self.fixture.repo, 'refs/heads/syncwheel/lane/new-lane'))
        self.assertEqual(self.module.load_governed_worktree_registry(self.fixture.repo)[0]['lanes'], [])

    def test_registry_failure_before_replace_rolls_back_both_created_refs(self):
        self.create_empty_stack()
        before = self.local_state()

        with mock.patch.object(self.module, 'governed_worktree_registry_io_checkpoint',
                               side_effect=OSError('simulated registry write failure')):
            with self.assertRaisesRegex(OSError, 'registry write failure'):
                self.module.command_worktree_open(self.open_args())

        self.assertEqual(self.local_state(), before)

    def test_registry_failure_preserves_stack_branch_advanced_concurrently(self):
        self.create_empty_stack()
        stack_ref = 'refs/heads/pr/new-stack'
        competing_tip = self.fixture.git('rev-parse', 'main~1')

        def advance_stack(_stage):
            self.fixture.git('update-ref', stack_ref, competing_tip)
            raise OSError('simulated registry write failure')

        with mock.patch.object(self.module, 'governed_worktree_registry_io_checkpoint',
                               side_effect=advance_stack):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'could not roll back'):
                self.module.command_worktree_open(self.open_args())

        self.assertEqual(self.fixture.git('rev-parse', stack_ref), competing_tip)
        self.assertIsNone(self.module.ref_tip(self.fixture.repo, 'refs/heads/syncwheel/lane/new-lane'))
        self.assertEqual(self.module.load_governed_worktree_registry(self.fixture.repo)[0]['lanes'], [])

    def test_registry_failure_after_replace_keeps_registered_lane_and_stack_refs(self):
        self.create_empty_stack()

        def fail_after_replace(stage):
            if stage == 'registry_directory_fsynced':
                raise OSError('simulated registry write interruption')

        with mock.patch.object(self.module, 'governed_worktree_registry_io_checkpoint',
                               side_effect=fail_after_replace):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'lane is visible'):
                self.module.command_worktree_open(self.open_args())

        lanes = self.module.load_governed_worktree_registry(self.fixture.repo)[0]['lanes']
        self.assertEqual(len(lanes), 1)
        self.assertEqual(self.fixture.git('rev-parse', 'refs/heads/pr/new-stack'), lanes[0]['base'])
        self.assertEqual(self.fixture.git('-C', lanes[0]['path'], 'rev-parse', 'HEAD'), lanes[0]['base'])
