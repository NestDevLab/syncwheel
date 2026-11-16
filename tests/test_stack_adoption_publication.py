import contextlib
import io
import json
import shlex
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_stack_adoption as adoption


class StackAdoptionPublicationTest(unittest.TestCase):
    source_ref = 'refs/heads/pr/replay'

    def setUp(self):
        self.case = adoption.StackAdoptionTest()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.fixture = self.case.f
        self.case.apply(self.case.preview())
        self.registry_path = self.case.module.governed_worktree_registry_path(self.fixture.repo)

        self.origin = self.fixture.tmp / 'origin.git'
        self.fixture.git('init', '--bare', '-q', str(self.origin))
        self.fixture.git('remote', 'add', 'origin', str(self.origin))
        self.remote_git('fetch', '-q', str(self.fixture.repo),
                        f'{self.case.old_tip}:{self.source_ref}')
        self.assertEqual(self.remote_tip(), self.case.old_tip)

    def remove_fixture_lane(self):
        lane = self.case.lane
        self.fixture.git('worktree', 'remove', lane['path'])
        self.fixture.git('branch', '-D', lane['branch'])
        registry, _ = self.case.module.load_governed_worktree_registry(
            self.fixture.repo,
        )
        self.assertEqual(registry['lanes'], [lane])
        registry['lanes'][0]['state'] = 'reaped'
        self.case.module.save_governed_worktree_registry(self.fixture.repo, registry)
        self.assertFalse(Path(lane['path']).exists())

    def remote_git(self, *args):
        return self.fixture.git('--git-dir=' + str(self.origin), *args)

    def remote_tip(self):
        return self.remote_git('rev-parse', self.source_ref)

    def snapshot(self):
        return (
            self.fixture.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads'),
            self.fixture.git('worktree', 'list', '--porcelain'),
            self.case.manifest_path.read_bytes(),
            self.registry_path.read_bytes(),
        )

    def push(self, *extra, expected=0):
        return self.fixture.run_cli(
            'stack', 'push', 'replay', '--remote', 'origin', *extra, expected=expected,
        )

    def assert_exact_push_command(self, output):
        self.assertEqual(
            shlex.split(output.strip().splitlines()[-1]),
            ['git', 'push', 'origin', f'{self.case.tip}:{self.source_ref}'],
        )

    def test_push_publishes_exact_adopted_tip_and_preserves_original_remote_history(self):
        self.remove_fixture_lane()
        before = self.snapshot()

        self.assert_exact_push_command(self.push('--dry-run').stdout)
        self.assertEqual(self.remote_tip(), self.case.old_tip)
        result = self.push()

        self.assert_exact_push_command(result.stdout)
        self.assertEqual(self.remote_tip(), self.case.tip)
        self.remote_git('merge-base', '--is-ancestor', self.case.old_tip, self.case.tip)
        self.remote_git('merge-base', '--is-ancestor', self.case.new_base, self.case.tip)
        self.assertEqual(self.snapshot(), before)

    def test_push_refuses_a_source_ref_advanced_beyond_the_adopted_tip(self):
        self.remove_fixture_lane()
        later_tip = self.fixture.git('commit-tree', self.case.tip + '^{tree}', '-p',
                                     self.case.tip, '-m', 'Undeclared continuation')
        self.fixture.git('update-ref', self.source_ref, later_tip, self.case.tip)
        before = self.snapshot()

        for options in [(), ('--dry-run',)]:
            with self.subTest(options=options):
                result = self.push(*options, expected=2)
                self.assertIn('adopted stack source changed', result.stderr)
                self.assertEqual(self.remote_tip(), self.case.old_tip)
                self.assertEqual(self.snapshot(), before)

    def test_push_does_not_overwrite_a_divergent_remote_tip(self):
        self.remove_fixture_lane()
        independent = self.fixture.git('commit-tree', self.case.old_tip + '^{tree}', '-p',
                                       self.case.old_tip, '-m', 'Independent remote change')
        self.remote_git('fetch', '-q', str(self.fixture.repo), f'{independent}:{self.source_ref}')
        before = self.snapshot()

        result = self.push(expected=2)

        self.assertIn('non-fast-forward', result.stderr)
        self.assertEqual(self.remote_tip(), independent)
        self.assertEqual(self.snapshot(), before)

    def test_active_original_lane_can_publish_after_adopting_a_new_base(self):
        lane = self.case.lane
        lane_range = self.fixture.git('rev-list', f"{lane['base']}..{lane['branch']}").splitlines()
        self.assertIn(self.case.new_base, lane_range)
        self.assertEqual(lane['state'], 'active')
        before = self.snapshot()

        result = self.push()

        self.assert_exact_push_command(result.stdout)
        self.assertEqual(self.remote_tip(), self.case.tip)
        self.remote_git('merge-base', '--is-ancestor', self.case.old_tip, self.case.tip)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fixture.git('-C', lane['path'], 'rev-parse', 'HEAD'), self.case.tip)
        self.assertEqual(self.fixture.git('-C', lane['path'], 'status', '--porcelain'), '')

    def test_new_lane_can_open_at_exact_adopted_tip(self):
        output = json.loads(self.fixture.run_cli(
            'worktree', 'open', 'successor', '--into', 'replay', '--base', self.case.tip, '--json',
        ).stdout)

        lane = output['lane']
        self.assertEqual(lane['base'], self.case.tip)
        self.assertEqual(lane['target'], 'replay')
        self.assertEqual(self.fixture.git('-C', lane['path'], 'rev-parse', 'HEAD'), self.case.tip)
        self.assertEqual(self.fixture.git('rev-parse', self.source_ref), self.case.tip)
        self.assertEqual(json.loads(self.registry_path.read_text())['lanes'], [self.case.lane, lane])

    def test_force_lease_and_ledger_use_the_adopted_tip_after_a_source_ref_race(self):
        self.remove_fixture_lane()
        module = self.case.module
        later_tip = self.fixture.git('commit-tree', self.case.tip + '^{tree}', '-p',
                                     self.case.tip, '-m', 'Concurrent source update')
        forced_push_requested = module.forced_push_requested
        exact_lease = f'--force-with-lease={self.source_ref}:{self.case.old_tip}'

        def advance_source(push_args):
            self.fixture.git('update-ref', self.source_ref, later_tip, self.case.tip)
            return forced_push_requested(push_args)

        argv = [
            'syncwheel', 'stack', 'push', 'replay', '--repo', str(self.fixture.repo),
            '--remote', 'origin', '--force-with-lease',
        ]
        with (
            mock.patch.object(sys, 'argv', argv),
            mock.patch.object(module, 'forced_push_requested', side_effect=advance_source),
            mock.patch.object(module, 'legacy_stack_push_lease', return_value=[exact_lease]) as lease,
            mock.patch.object(module, 'run_authorized_push') as authorized_push,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(module.main(), 0)

        lease.assert_called_once()
        self.assertEqual(lease.call_args.args[3], self.case.tip)
        authorized_push.assert_called_once()
        self.assertEqual(authorized_push.call_args.args[:4], (
            self.fixture.repo,
            ['git', 'push', exact_lease, 'origin', f'{self.case.tip}:{self.source_ref}'],
            'origin',
            [self.source_ref],
        ))
        events = module.load_ledger_events(self.fixture.repo, self.case.manifest_path)
        self.assertEqual(events[-1]['type'], 'stack_pushed')
        self.assertEqual(events[-1]['payload']['tip'], self.case.tip)
        self.assertEqual(self.fixture.git('rev-parse', self.source_ref), later_tip)
        self.assertEqual(self.remote_tip(), self.case.old_tip)
