import json
import os
import shlex
import subprocess
import unittest
from unittest import mock

import test_stack_adoption_edges as fixtures


def files(root):
    return {
        str(path.relative_to(root)): (path.lstat().st_mode,
                                     os.readlink(path) if path.is_symlink() else path.read_bytes())
        for path in root.rglob('*') if path.is_file() or path.is_symlink()
    }


def contents(path):
    return path.read_bytes() if path.exists() else None


class AdoptedStackSyncTest(unittest.TestCase):
    def setUp(self):
        self.edge = fixtures.StackAdoptionEdgeTest()
        self.edge.setUp()
        self.addCleanup(self.edge.doCleanups)
        self.case = self.edge.case
        self.f = self.edge.f
        self.case.apply(self.case.preview())
        self.cache = self.f.tmp / 'update-cache.json'
        self.cache.write_text('{"last_checked_epoch": 0}\n')
        self.env = {'SYNCWHEEL_UPDATE_MODE': 'off', 'SYNCWHEEL_UPDATE_STATE_PATH': str(self.cache)}

    def state(self):
        result = self.edge.repository_state()
        result.update(
            index=(self.f.repo / '.git/index').read_bytes(),
            control=files(self.f.repo / '.syncwheel'),
            private_git=files(self.f.repo / '.git/syncwheel'),
            hooks=files(self.f.repo / '.git/hooks'),
            cache=self.cache.read_bytes(),
            ignore=contents(self.f.repo / '.gitignore'),
            exclude=contents(self.f.repo / '.git/info/exclude'),
        )
        return result

    def cli(self, *args, expected=0):
        return self.f.run_cli(*args, expected=expected, extra_env=self.env)

    def test_consistent_sync_preserves_dirty_primary_and_control_state(self):
        source = self.f.repo / 'pending.txt'
        source.write_text('staged contribution\n')
        self.f.git('add', source.name)
        source.write_text('staged contribution\nunstaged contribution\n')
        nonce = self.f.repo / '.git/syncwheel/ref-auth/expired'
        nonce.parent.mkdir(parents=True, exist_ok=True)
        nonce.write_text('{')
        os.utime(nonce, (1, 1))
        before = self.state()

        result = self.cli('stack', 'sync', 'replay')

        self.assertIn('stack is adopted; nothing to sync. Next: int rebuild / push', result.stdout)
        self.assertNotIn('stack set', result.stdout + result.stderr)
        self.assertEqual(self.state(), before)

    def test_empty_adoption_is_consistent(self):
        args = ['stack', 'set', 'replay', '--base', self.case.tip, '--adopt-tip', self.case.tip]
        plan = json.loads(self.cli(*args).stdout)
        self.cli(*args, '--apply', '--plan-digest', plan['planDigest'])
        self.assertEqual(self.f.read_manifest()['stacks'][0]['commits'], [])
        before = self.state()
        self.assertIn('nothing to sync', self.cli('stack', 'sync', 'replay').stdout)
        self.assertEqual(self.state(), before)

    def test_source_drift_never_reports_noop(self):
        ref = 'refs/heads/pr/replay'
        later = self.f.git('commit-tree', self.case.tip + '^{tree}', '-p', self.case.tip, '-m', 'Later work')
        for kind in ('advanced', 'rewound', 'missing', 'symbolic'):
            with self.subTest(kind=kind):
                self.f.git('update-ref', '--no-deref', '-d', ref)
                if kind == 'symbolic':
                    self.f.git('symbolic-ref', ref, 'refs/heads/main')
                elif kind != 'missing':
                    self.f.git('update-ref', ref, later if kind == 'advanced' else self.case.new_base)
                before = self.state()
                for command in ('sync', 'absorb'):
                    result = self.cli('stack', command, 'replay', expected=2)
                    self.assertIn('adopted history is stale', result.stderr)
                    self.assertIn('stack set replay --base BASE --adopt-tip TIP', result.stderr)
                    self.assertNotIn('nothing to sync', result.stdout)
                    self.assertEqual(self.state(), before)

    def test_printed_staged_remedy_preserves_unrelated_staged_work_on_refusal(self):
        self.cli('stack', 'create', 'patch', '--draft', '--base', self.case.base,
                 '--integration-branch', 'main')
        source = self.f.repo / 'pending.txt'
        source.write_text('staged contribution\n')
        self.f.git('add', source.name)
        source.write_text('staged contribution\nunstaged contribution\n')
        unrelated = self.f.repo / 'unrelated.txt'
        unrelated.write_text('another staged contribution\n')
        self.f.git('add', unrelated.name)
        before = self.state()

        result = self.cli('stack', 'absorb', 'replay', '--staged', '--', 'pending.txt', expected=2)

        self.assertIn('cannot absorb into its pinned history', result.stderr)
        self.assertNotIn('stack set', result.stderr)
        self.assertEqual(self.state(), before)
        remedy = shlex.split(result.stderr.partition('in: ')[2])
        self.assertEqual(remedy[:3], ['syncwheel', 'stack', 'absorb'])
        remedy[3] = 'patch'
        remedy[remedy.index('-m') + 1] = 'Preserve staged contribution'
        blocked = self.cli(*remedy[1:], expected=2)
        self.assertIn('primary checkout has unrelated staged paths: unrelated.txt', blocked.stderr)
        self.assertEqual(self.state(), before)

    def test_printed_staged_remedy_absorbs_selected_patch_when_guard_allows(self):
        self.cli('stack', 'create', 'patch', '--draft', '--base', self.case.base,
                 '--integration-branch', 'main')
        source = self.f.repo / 'pending.txt'
        source.write_text('staged contribution\n')
        self.f.git('add', source.name)
        before = self.state()
        result = self.cli('stack', 'absorb', 'replay', '--staged', '--', source.name, expected=2)
        self.assertEqual(self.state(), before)
        remedy = shlex.split(result.stderr.partition('in: ')[2])
        remedy[3] = 'patch'
        remedy[remedy.index('-m') + 1] = 'Preserve staged contribution'
        self.cli(*remedy[1:])
        draft = next(stack for stack in self.f.read_manifest()['stacks'] if stack['id'] == 'patch')
        self.assertEqual(self.f.git('show', draft['commits'][0] + ':pending.txt'), 'staged contribution')
        self.assertEqual(self.f.git('diff', '--cached'), '')
        self.assertFalse(source.exists())
        self.assertEqual(self.f.git('rev-parse', 'pr/replay'), self.case.tip)

    def test_printed_unstaged_remedy_keeps_other_paths_out_of_draft(self):
        self.cli('stack', 'create', 'patch', '--draft', '--base', self.case.base,
                 '--integration-branch', 'main')
        source = self.f.repo / 'alpha.txt'
        original = source.read_bytes()
        source.write_text('selected contribution\n')
        unrelated = self.f.repo / 'beta.txt'
        unrelated.write_text('other unstaged contribution\n')
        before = self.state()
        result = self.cli('stack', 'absorb', 'replay', '--', source.name, expected=2)
        self.assertEqual(self.state(), before)
        remedy = shlex.split(result.stderr.partition('in: ')[2])
        remedy[3] = 'patch'
        remedy[remedy.index('-m') + 1] = 'Preserve selected contribution'
        self.cli(*remedy[1:])
        draft = next(stack for stack in self.f.read_manifest()['stacks'] if stack['id'] == 'patch')
        self.assertEqual(self.f.git('show', draft['commits'][0] + ':alpha.txt'), 'selected contribution')
        self.assertEqual(self.f.git('diff-tree', '--no-commit-id', '--name-only', '-r', draft['commits'][0]),
                         'alpha.txt')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(unrelated.read_text(), 'other unstaged contribution\n')
        self.assertEqual(self.f.git('diff', '--cached'), '')
        self.assertEqual(self.f.git('rev-parse', 'pr/replay'), self.case.tip)

    def test_failed_side_parent_ancestry_query_does_not_prove_consistency(self):
        lifecycle = self.edge.module._stack_lifecycle(self.f.repo)
        stack = self.f.read_manifest()['stacks'][0]
        original = lifecycle.repository.git
        parents = self.f.git('rev-list', '--parents', '-n', '1', self.case.tip).split()[1:]
        side = next(parent for parent in parents if parent != self.case.new_base)

        def failed(root, *args, **kwargs):
            if args[:3] == ('merge-base', '--is-ancestor', side):
                return subprocess.CompletedProcess(args, 128, '', 'fixture object read failure')
            return original(root, *args, **kwargs)

        before = self.state()
        with mock.patch.object(lifecycle.repository, 'git', side_effect=failed):
            with self.assertRaisesRegex(self.edge.module.SyncwheelError, 'cannot verify adopted history'):
                lifecycle.consistent_stack_adoption(stack, '')
        self.assertEqual(self.state(), before)

    def test_pending_adoption_is_not_reported_as_synced(self):
        self.case.new_base = self.case.tip
        self.edge.interrupt(self.case.preview(), 'manifest-updated')
        before = self.state()
        result = self.cli('stack', 'sync', 'replay', expected=2)
        self.assertIn('interrupted stack adoption', result.stderr)
        self.assertEqual(self.state(), before)

    def test_control_and_promotion_intents_remain_blocking_without_recovery(self):
        for event, payload, expected in (
            ('control_manifest_persistence_intent', {'operation_id': 'fixture-control'}, 'control manifest recovery'),
            ('coordination_publish_intent', {'operation_token': 'fixture-promotion', 'scope': 'promote:replay'}, 'stack promote replay'),
        ):
            with self.subTest(event=event):
                self.edge.module.append_ledger_event(self.f.repo, event, payload, self.case.manifest_path)
                before = self.state()
                result = self.cli('stack', 'sync', 'replay', expected=2)
                self.assertIn(expected, result.stderr)
                self.assertEqual(self.state(), before)
                terminal = ('control_manifest_persistence_abandoned' if 'operation_id' in payload
                            else 'coordination_publish_abandoned')
                self.edge.module.append_ledger_event(self.f.repo, terminal, payload, self.case.manifest_path)


if __name__ == '__main__':
    unittest.main()
