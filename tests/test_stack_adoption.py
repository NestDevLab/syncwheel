import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import test_syncwheel as fixtures


class StackAdoptionTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.SyncwheelFixtureTest()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.module = self.f.load_syncwheel_module()
        self.base, self.commits = self.f.prepare_replay_stack()
        self.f.run_cli('stack', 'rebuild', 'replay')
        self.old_tip = self.f.git('rev-parse', 'pr/replay')
        self.lane = json.loads(self.f.run_cli(
            'worktree', 'open', 'author', '--into', 'replay', '--base', self.old_tip, '--json',
        ).stdout)['lane']
        self.new_base = self.f.git('commit-tree', self.base + '^{tree}', '-p', self.base,
                                   '-m', 'Advance delivery base')
        self.f.git('-C', self.lane['path'], 'merge', '--no-ff', '-qm', 'Merge delivery base', self.new_base)
        self.tip = self.f.git('-C', self.lane['path'], 'rev-parse', 'HEAD')
        self.manifest_path = self.f.repo / '.syncwheel/manifest.json'

    def arguments(self):
        return ['stack', 'set', 'replay', '--base', self.new_base, '--adopt-tip', self.tip]

    def preview(self):
        return json.loads(self.f.run_cli(*self.arguments()).stdout)

    def apply(self, plan):
        return self.f.run_cli(*self.arguments(), '--plan-digest', plan['planDigest'], '--apply')

    def test_merge_graph_refusal_has_exact_history_adoption_route(self):
        before = self.manifest_path.read_bytes()
        result = self.f.run_cli('stack', 'add', 'replay', self.tip, expected=2)
        self.assertIn('has no unique parent contained in stack base', result.stderr)
        self.assertEqual(before, self.manifest_path.read_bytes())
        registry = self.module.governed_worktree_registry_path(self.f.repo)
        lanes_before = registry.read_bytes()
        plan = self.preview()
        self.assertEqual(before, self.manifest_path.read_bytes())
        self.assertEqual(self.f.git('rev-parse', 'pr/replay'), self.old_tip)
        self.assertIn(self.tip, plan['history'])
        self.assertIn(self.commits[0], plan['history'])
        self.apply(plan)
        stack = self.f.read_manifest()['stacks'][0]
        self.assertEqual(stack['adopted_history'], {'base': self.new_base, 'tip': self.tip})
        self.assertEqual(stack['commits'], [self.tip])
        self.assertEqual(self.f.git('rev-parse', 'pr/replay'), self.tip)
        self.assertEqual(registry.read_bytes(), lanes_before)
        self.assertTrue(Path(self.lane['path']).is_dir())
        validation = json.loads(self.f.run_cli('validate', '--json').stdout)
        self.assertEqual(validation['details']['stacks'][0]['undeclared_branch_commits'], [])

    def test_new_commit_after_adopted_tip_remains_undeclared(self):
        self.apply(self.preview())
        later = self.f.git('commit-tree', self.tip + '^{tree}', '-p', self.tip, '-m', 'Undeclared work')
        self.f.git('update-ref', 'refs/heads/pr/replay', later, self.tip)
        result = json.loads(self.f.run_cli('validate', '--json').stdout)
        self.assertIn(later, result['details']['stacks'][0]['undeclared_branch_commits'])

    def test_adoption_requires_exact_preview_and_preserves_local_history(self):
        before = self.manifest_path.read_bytes()
        self.f.run_cli(*self.arguments(), '--apply', '--plan-digest', 'stale', expected=2)
        self.assertEqual(self.manifest_path.read_bytes(), before)
        self.assertEqual(self.f.git('rev-parse', 'pr/replay'), self.old_tip)
        self.f.run_cli('stack', 'set', 'replay', '--base', self.base, '--adopt-tip', self.base, expected=2)
        self.assertEqual(self.manifest_path.read_bytes(), before)

    def test_checked_out_source_ref_is_not_moved(self):
        desk = self.f.tmp / 'checked-source'
        self.f.git('worktree', 'add', str(desk), 'pr/replay')
        result = self.f.run_cli(*self.arguments(), expected=2)
        self.assertIn('checked out in a worktree', result.stderr)
        self.assertEqual(self.f.git('-C', str(desk), 'rev-parse', 'HEAD'), self.old_tip)

    def test_crash_after_ref_move_resumes_without_rewriting_tip(self):
        plan = self.preview()
        args = self.module.build_parser().parse_args([
            *self.arguments(), '--repo', str(self.f.repo), '--plan-digest', plan['planDigest'], '--apply',
        ])
        import syncwheel_stack_adoption as adoption
        def crash(stage):
            if stage == 'ref-updated':
                raise RuntimeError('simulated process interruption')
        with self.module.manifest_write_transaction(self.f.repo, self.manifest_path), \
                mock.patch.object(adoption, 'adoption_checkpoint', side_effect=crash), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.module.command_stack_set(args)
        self.assertEqual(self.f.git('rev-parse', 'pr/replay'), self.tip)
        self.assertNotIn('adopted_history', self.f.read_manifest()['stacks'][0])
        blocked = self.f.run_cli('stack', 'set', 'replay', self.tip, expected=2)
        self.assertIn('interrupted stack adoption', blocked.stderr)
        self.apply(plan)
        self.assertEqual(self.f.read_manifest()['stacks'][0]['adopted_history']['tip'], self.tip)

    def test_coordination_snapshot_preserves_explicit_adoption(self):
        self.apply(self.preview())
        manifest, _ = self.module.load_manifest(self.f.repo)
        snapshot = self.module.coordination_manifest_snapshot(manifest, self.f.repo)
        import syncwheel_coordination as coordination
        self.assertEqual(coordination.coordination_snapshot_schema(snapshot), 5)
        state = {'schema_version': 5, 'coordination_id': 'adoption', 'manifest': snapshot,
                 'manifest_digest': self.module.manifest_digest(manifest), 'managed_refs': {}}
        self.module.validate_coordination_state(state)
        for old_schema in (2, 3, 4):
            with self.subTest(schema=old_schema), self.assertRaisesRegex(
                self.module.SyncwheelError, 'incompatible'
            ):
                self.module.validate_coordination_state({**state, 'schema_version': old_schema})
        restored = self.module.apply_coordination_snapshot(manifest, snapshot)
        self.assertEqual(restored['stacks'][0]['adopted_history'], manifest['stacks'][0]['adopted_history'])
        snapshot['stacks'][0].pop('adopted_history')
        restored = self.module.apply_coordination_snapshot(manifest, snapshot)
        self.assertNotIn('adopted_history', restored['stacks'][0])


if __name__ == '__main__':
    unittest.main()
