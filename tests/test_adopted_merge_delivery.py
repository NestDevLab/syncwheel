"""Adopted update-branch merge histories retain exact source ancestry."""

import contextlib
import io
import unittest

import test_github_pr_merge as fixtures


class AdoptedMergeDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GithubPrMergeTest()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.module = fixtures.SYNCWHEEL
        self.old_tip = self.f.head
        self.delivery_base = self.f.git(
            'commit-tree', self.f.base + '^{tree}', '-p', self.f.base,
            '-m', 'Advance delivery base',
        )
        self.tip = self.f.git(
            'commit-tree', self.old_tip + '^{tree}', '-p', self.old_tip,
            '-p', self.delivery_base, '-m', 'Merge delivery base into source',
        )
        self.f.git('update-ref', 'refs/heads/pr/feature', self.tip, self.old_tip)
        self.f.git('push', '-q', 'origin', 'pr/feature')
        self.f.git('branch', 'integration/adopted', self.old_tip)
        self.stack = self.f.manifest['stacks'][0]
        self.stack.update({
            'base': self.delivery_base, 'commits': [self.tip],
            'adopted_history': {'base': self.delivery_base, 'tip': self.tip},
            'integration_branch': 'integration/adopted',
        })
        self.stack.pop('integration_commits')
        self.f.manifest['integration'].update({
            'base': self.delivery_base, 'branch': 'integration/adopted',
        })
        self.engine = self.module._replay_engine(self.f.repo)

    def replay(self, mode='ephemeral', projection=False):
        target = self.module.replay_target(integration=self.f.manifest['integration'])
        if projection:
            return self.engine.materialize_integration_replay(self.f.manifest)
        plan = self.engine.replay_plan(self.f.manifest, target, mode)
        with contextlib.redirect_stdout(io.StringIO()):
            result = self.engine.execute_replay(plan, True)
        self.assertEqual(result['status'], 'applied')
        return self.f.git('rev-parse', 'integration/adopted')

    def test_preflight_accepts_adopted_full_history_after_integration(self):
        self.replay()
        blockers, warnings = self.f.preflight()
        self.assertEqual(blockers, [])
        self.assertEqual(warnings, [])
        self.assertEqual(self.stack['commits'], [self.tip])
        self.assertEqual(self.stack['adopted_history']['tip'], self.tip)
        self.assertEqual(
            self.module.declared_stack_history(self.f.repo, self.stack),
            [self.old_tip, self.tip],
        )

    def test_preflight_keeps_required_integration_gate(self):
        blockers, _ = self.f.preflight()
        self.assertEqual([b['code'] for b in blockers], ['integration_not_validated'])

    def test_preflight_rejects_undeclared_source_advance(self):
        later = self.f.git('commit-tree', self.tip + '^{tree}', '-p', self.tip, '-m', 'Later')
        self.f.git('update-ref', 'refs/heads/pr/feature', later, self.tip)
        blockers, _ = self.f.preflight()
        self.assertIn('stack_projection_mismatch', [b['code'] for b in blockers])

    def test_preflight_rejects_stale_adoption_recipe(self):
        self.stack['commits'] = [self.old_tip, self.tip]
        blockers, _ = self.f.preflight()
        self.assertIn('stack_projection_mismatch', [b['code'] for b in blockers])

    def test_preflight_rejects_tip_drift_even_when_ranges_are_empty(self):
        self.stack.update({
            'base': self.tip, 'commits': [],
            'adopted_history': {'base': self.tip, 'tip': self.tip},
        })
        self.f.git('update-ref', 'refs/heads/pr/feature', self.old_tip, self.tip)
        blockers, _ = self.f.preflight()
        self.assertIn('stack_projection_mismatch', [b['code'] for b in blockers])

    def test_replay_preserves_tip_and_primary_for_both_direct_parent_bases(self):
        for base in (self.delivery_base, self.old_tip):
            for mode in ('ephemeral', 'plumbing'):
                with self.subTest(base=base, mode=mode):
                    self.stack['base'] = base
                    self.stack['adopted_history']['base'] = base
                    self.f.manifest['integration']['base'] = base
                    head_before = self.f.git('rev-parse', 'HEAD')
                    index_before = (self.f.repo / '.git/index').read_bytes()
                    manifest_before = self.f.manifest_path.read_bytes()
                    self.assertEqual(self.replay(mode), self.tip)
                    self.assertEqual(self.f.git('rev-parse', 'HEAD'), head_before)
                    self.assertEqual((self.f.repo / '.git/index').read_bytes(), index_before)
                    self.assertEqual(self.f.manifest_path.read_bytes(), manifest_before)
                    self.assertEqual(self.f.git('rev-parse', 'pr/feature'), self.tip)

    def test_replay_composes_prior_stack_without_losing_adopted_tip(self):
        self.f.git('switch', '-qc', 'pr/other', self.f.base)
        (self.f.repo / 'other.txt').write_text('independent change\n')
        self.f.git('add', 'other.txt')
        self.f.git('commit', '-qm', 'Independent stack')
        other = self.f.git('rev-parse', 'HEAD')
        self.f.git('switch', '-q', 'main-integration')
        self.f.manifest['stacks'].insert(0, {
            'id': 'other', 'branch': 'pr/other', 'base': self.f.base, 'commits': [other],
        })
        self.f.manifest['integration']['stacks'].insert(0, 'other')
        self.f.manifest['integration']['base'] = self.f.base
        for mode in ('ephemeral', 'plumbing'):
            with self.subTest(mode=mode):
                result = self.replay(mode)
                self.assertTrue(self.module.branch_contains(self.f.repo, result, self.tip))
                self.assertTrue(self.module.branch_contains(self.f.repo, result, other))
                self.assertEqual(self.f.git('show', result + ':other.txt'), 'independent change')
                self.assertEqual(self.f.git('show', result + ':feature.txt'), 'feature')

    def test_projection_preserves_adopted_tip(self):
        self.assertEqual(self.replay(projection=True), self.tip)

    def test_replay_refuses_source_drift_before_execution(self):
        self.f.git('update-ref', 'refs/heads/pr/feature', self.old_tip, self.tip)
        with self.assertRaisesRegex(self.module.SyncwheelError, 'differs from its source'):
            self.engine.replay_plan(self.f.manifest, self.module.replay_target(
                integration=self.f.manifest['integration']), 'ephemeral')

    def test_dry_run_never_cherry_picks_adopted_tip(self):
        for mode in ('ephemeral', 'plumbing'):
            with self.subTest(mode=mode):
                plan = self.engine.replay_plan(self.f.manifest, self.module.replay_target(
                    integration=self.f.manifest['integration']), mode)
                output = io.StringIO()
                before = self.f.git('rev-parse', 'integration/adopted')
                with contextlib.redirect_stdout(output):
                    self.engine.execute_replay(plan, False)
                self.assertNotIn('cherry-pick', output.getvalue())
                self.assertEqual(self.f.git('rev-parse', 'integration/adopted'), before)


if __name__ == '__main__':
    unittest.main()
