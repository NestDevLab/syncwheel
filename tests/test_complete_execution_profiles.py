"""Operational presets and custom inheritance at the actual CLI/merge boundary."""

import json
from pathlib import Path
import unittest

import test_execution_policy as fixtures
from syncwheel_policy import PRESETS, resolve_preferences
from syncwheel_worktree_policy import admit_writers


class CompleteProfileCliTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionPolicyCliTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.command = self.fixture.command
        self.cli = self.fixture.cli

    def test_all_presets_activate_outside_repo_and_repo_lane_inheritance_clear(self):
        lane = self.fixture.fixture.open_lane()
        for preset in PRESETS:
            with self.subTest(preset=preset):
                preview = json.loads(self.command('self', 'set', '--preset', preset).stdout)
                self.assertFalse(preview['applied'])
                self.command('self', 'set', '--preset', preset, '--apply', cwd=self.fixture.fixture.fixture.tmp)
                global_status = json.loads(self.command('self', 'status', cwd=self.fixture.fixture.fixture.tmp).stdout)
                self.assertEqual(global_status['effective'], PRESETS[preset])
                self.command('repo', 'set', '--preset', preset, '--apply')
                status = json.loads(self.command('repo', 'status').stdout)
                self.assertEqual(status['effective'], PRESETS[preset])
                self.assertEqual(status['provenance']['tests']['preset'], preset)
                result = self.cli('worktree', 'execution-policy', lane['id'], 'set', '--preset', preset,
                                  '--apply', '--json')
                self.assertEqual(json.loads(result.stdout)['lane_binding']['state'], 'bound')
                self.cli('worktree', 'execution-policy', lane['id'], 'clear', '--apply', '--json')
                self.command('repo', 'clear', '--apply')

    def test_custom_is_existing_per_field_overrides_without_a_synthetic_preset(self):
        self.command('self', 'set', '--preset', 'strict', '--apply')
        self.command('repo', 'set', '--checks', 'required', '--tests', 'targeted', '--apply')
        lane = self.fixture.fixture.open_lane()
        result = self.cli('worktree', 'execution-policy', lane['id'], 'set', '--independent-review',
                          'sensitive', '--apply', '--json')
        status = json.loads(result.stdout)
        self.assertEqual(status['effective']['checks'], 'required')
        self.assertEqual(status['sources']['checks'], 'repo')
        self.assertEqual(status['sources']['installation_smoke'], 'global')
        self.assertEqual(status['provenance']['independent_review'], {'scope': 'lane', 'kind': 'override'})
        cleared = json.loads(self.command('repo', 'clear', 'checks', '--apply').stdout)
        self.assertEqual(cleared['effective']['checks'], 'all')
        self.command('repo', 'set', '--preset', 'custom', expected=2)

    def test_every_preset_admits_absent_predecessor_without_a_time_or_owner_gate(self):
        old = {'id': 'previous-session', 'owner': 'unavailable-writer', 'state': 'active',
               'expires_at': '2000-01-01T00:00:00Z'}
        for preset in PRESETS:
            with self.subTest(preset=preset):
                policy = resolve_preferences({'worktree_policy': {'preset': preset}})['effective']
                self.assertEqual(policy['expiry'], 'off')
                admit_writers([old], 'authorized-writer', policy)

    def test_strict_expired_foreign_session_proceeds_but_real_lock_and_dirty_preserve_data(self):
        fixture = self.fixture.fixture
        lane = fixture.open_lane()
        registry, registry_path = fixture.module.load_governed_worktree_registry(fixture.fixture.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        fixture.module.save_governed_worktree_registry(fixture.fixture.repo, registry)
        self.command('repo', 'set', '--preset', 'strict', '--apply')
        before = registry_path.read_bytes()
        observed = json.loads(self.cli('worktree', 'list', '--json').stdout)
        self.assertIsNone(observed['lanes'][0]['code'])
        command = ('stack', 'resolve-integration', 'feature-a', '--empty')
        self.cli(*command, extra_env={'SYNCWHEEL_LANE_OWNER': 'new-writer'})
        self.assertEqual(registry_path.read_bytes(), before)
        fixture.fixture.git('worktree', 'lock', '--reason', 'fixture collision', lane['path'])
        refused = self.cli(*command, expected=2)
        self.assertIn('[locked]', refused.stderr)
        self.assertEqual(registry_path.read_bytes(), before)
        fixture.fixture.git('worktree', 'unlock', lane['path'])
        private = Path(lane['path']) / 'private.txt'
        private.write_text('retain foreign work\n')
        refused = self.cli(*command, expected=2)
        self.assertIn('[dirty]', refused.stderr)
        self.assertEqual(private.read_text(), 'retain foreign work\n')
        self.assertEqual(registry_path.read_bytes(), before)


class CompleteProfileMergeTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionMergeGatesTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def plan(self, preset, **kwargs):
        head = self.fixture.fixture.head
        return self.fixture.plan(preset, ci=[{'name': 'candidate-ci', 'headSha': head,
                                             'conclusion': 'SUCCESS'}],
                                 test_scope='extended',
                                 review_scope='all', **kwargs)

    def test_standard_and_strict_consume_existing_evidence_without_merge_smoke_wait(self):
        for preset in ('standard', 'strict'):
            with self.subTest(preset=preset):
                result = self.plan(preset, historical_authors=True,
                                   permissions={'push': True, 'admin': False})
                self.assertEqual(result['status'], 'ready', result['blockers'])
                self.assertEqual(set(result['candidateEvidence']), {'tests', 'review'})
                self.assertEqual(result['releaseEvidence']['status'], 'pending')
                self.assertNotIn('--admin', result['command'])

    def test_review_and_extended_tests_are_real_candidate_gates(self):
        standard = self.fixture.plan('standard', review_scope='sensitive')
        self.assertIn('independent_review_evidence_missing', [x['code'] for x in standard['blockers']])

    def test_strict_requires_extended_test_evidence(self):
        strict = self.fixture.plan('strict', review_scope='all')
        self.assertIn('extended_test_evidence_missing', [x['code'] for x in strict['blockers']])

    def test_strict_integration_is_explicit_while_standard_is_advisory(self):
        self.fixture.fixture.stale_integration()
        standard = self.plan('standard')
        self.assertEqual(standard['status'], 'ready', standard['blockers'])
        strict = self.plan('strict')
        self.assertIn('integration_not_validated', [x['code'] for x in strict['blockers']])

    def test_all_ci_is_exact_candidate_not_unrelated_history_and_unknown_is_blocking(self):
        head = self.fixture.fixture.head
        for preset in ('standard', 'strict'):
            with self.subTest(preset=preset):
                result = self.fixture.plan(preset, review_scope='all', test_scope='extended', ci=[
                    {'name': 'candidate-ci', 'headSha': head, 'conclusion': 'SUCCESS'},
                    {'name': 'foreign-stack-ci', 'headSha': self.fixture.fixture.base, 'conclusion': 'FAILURE'},
                ])
                self.assertEqual(result['status'], 'ready', result['blockers'])
                self.assertEqual(result['outOfScopeChecks'][0]['name'], 'foreign-stack-ci')
                unknown = self.fixture.plan(preset, review_scope='all', test_scope='extended', ci=[
                    {'name': 'unknown-ci', 'conclusion': 'SUCCESS'},
                ])
                self.assertIn('check_failed_or_pending', [x['code'] for x in unknown['blockers']])

    def test_every_operational_preset_requires_exact_revision_for_a_forge_check(self):
        head = self.fixture.fixture.head
        for preset in PRESETS:
            with self.subTest(preset=preset):
                blocked = self.fixture.plan(preset, mandatory=True, test_scope='extended',
                    review_scope='all', ci=[
                        {'name': 'candidate-ci', 'headSha': head, 'conclusion': 'SUCCESS'},
                        {'name': 'mandatory-ci', 'conclusion': 'SUCCESS'},
                    ])
                self.assertIn('required_check_failed_or_pending', [x['code'] for x in blocked['blockers']])

    def test_new_candidate_sha_cannot_reuse_previous_test_or_review_receipts(self):
        old = self.fixture.fixture.head
        report = resolve_preferences({'worktree_policy': {'preset': 'strict'}})
        profile = fixtures.execution.record_receipts({}, [
            fixtures.receipt(old, 'tests', scope='extended'),
            fixtures.receipt(old, 'review', scope='all'),
        ])
        blocked, consumed = fixtures.execution.candidate_gates(report, profile, 'f' * 40)
        self.assertEqual(consumed, {})
        self.assertEqual({x['code'] for x in blocked}, {
            'extended_test_evidence_missing', 'independent_review_evidence_missing'})
        self.assertIn(old, profile['execution_evidence'])

    def test_new_live_base_blocks_old_head_even_with_valid_old_candidate_evidence(self):
        fixture = self.fixture.fixture
        fixture.git('switch', '-qc', 'advanced-base', fixture.base)
        (fixture.repo / 'new-base.txt').write_text('new base\n')
        fixture.git('add', 'new-base.txt')
        fixture.git('commit', '-qm', 'advance base')
        fixture.git('push', '-q', 'origin', 'HEAD:main')
        blocked = self.plan('strict')
        self.assertIn('base_not_in_head', [x['code'] for x in blocked['blockers']])
        self.assertEqual(fixture.git('rev-parse', 'pr/feature'), fixture.head)

    def test_strict_cannot_replace_real_permissions_or_required_forge(self):
        missing_check = self.plan('strict', mandatory=True)
        self.assertIn('required_check_missing', [x['code'] for x in missing_check['blockers']])
        missing_review = self.plan('strict', decision='REVIEW_REQUIRED')
        self.assertIn('required_reviews_not_authorized', [x['code'] for x in missing_review['blockers']])
        no_permission = self.plan('strict', permissions={'pull': True})
        self.assertIn('merge_actor_permission_missing', [x['code'] for x in no_permission['blockers']])



if __name__ == '__main__':
    unittest.main()
