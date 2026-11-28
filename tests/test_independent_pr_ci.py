"""Independent PR CI is sufficient only when integration validation is advisory."""

import contextlib
import copy
import io
import os
import sys
import json
import unittest
from unittest import mock

import test_github_pr_merge as fixtures
from syncwheel_execution_policy import apply_required_floor

SW = fixtures.SYNCWHEEL


class IndependentPrCiTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.GithubPrMergeTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.git('switch', '-q', 'main')
        self.fixture.git('branch', '-D', 'main-integration')
        self.fixture.manifest['defaults']['integration_membership'] = 'required'
        self.fixture.manifest['authority'] = {
            'mode': 'ai-managed', 'allow': ['source_change'], 'deny': [],
        }
        self.policy = self.fixture.policy()
        self.policy.update(integration='advisory', allowed_bypasses=[])
        self.observation = self.fixture.observation(decision='APPROVED', checks=[{
            'name': 'required-ci', 'status': 'COMPLETED', 'conclusion': 'SUCCESS',
            'headSha': self.fixture.head,
        }])
        self.observation['pr']['mergeStateStatus'] = 'CLEAN'
        self.observation['rules']['branchProtection'] = {
            'required_status_checks': {'contexts': ['required-ci']},
        }

    def plan(self, *, floor=False):
        f = self.fixture
        f.manifest_path.write_text(json.dumps(f.manifest) + '\n')
        path = f.repo / '.syncwheel/profile.local.json'
        profile = json.loads(path.read_text())
        profile['github_pr_merge'] = self.policy
        path.write_text(json.dumps(profile) + '\n')
        report = SW.execution_policy_report(f.repo)
        if floor:
            report = apply_required_floor(report, {'integration_validation': 'required'},
                                          authority='verified required integration contract')
        with mock.patch.object(SW, 'github_adapter_request', return_value=self.observation), \
             mock.patch.object(SW, 'execution_policy_report', return_value=report):
            return SW.build_github_pr_merge_plan(f.repo, f.manifest, f.manifest_path,
                                                'feature', f.args())

    def codes(self, plan):
        return {item['code'] for item in plan['blockers']}

    def test_advisory_required_membership_uses_exact_head_ci_without_creating_integration(self):
        f = self.fixture
        before = f.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads')
        plan = self.plan()
        self.assertEqual(plan['status'], 'ready', plan['blockers'])
        self.assertEqual(plan['path'], 'normal')
        self.assertNotIn('--admin', plan['command'])
        self.assertIn('integration_not_validated', {item['code'] for item in plan['warnings']})
        self.assertIn('primary_projection_not_validated', {item['code'] for item in plan['warnings']})
        self.assertEqual(f.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads'), before)
        self.assertIsNone(SW.ref_tip(f.repo, 'refs/heads/main-integration'))

    def test_global_validation_still_requires_the_primary_integration_layout(self):
        self.plan()
        result = SW.validate_manifest(self.fixture.repo, self.fixture.manifest,
                                      self.fixture.manifest_path)
        self.assertTrue(any('primary worktree branch mismatch' in item for item in result['errors']))

    def test_normal_cli_preview_uses_required_ci_without_creating_a_local_projection(self):
        self.plan()
        f = self.fixture
        before = f.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads')
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', [str(SW.__file__), 'stack', 'merge-pr',
                                           'feature', '--repo', str(f.repo), '--json']), \
             mock.patch.dict(os.environ, {'SYNCWHEEL_UPDATE_MODE': 'off',
                                           'SYNCWHEEL_REPO_REGISTRY': str(f.tmp / 'registry.json'),
                                           'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(f.tmp / 'settings.json')}), \
             mock.patch.object(SW, 'github_adapter_request', return_value=self.observation), \
             mock.patch.object(SW, 'managed_hook_syncwheel_command',
                               return_value=[sys.executable, str(SW.__file__)]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = SW.main()
        self.assertEqual(code, 0, errors.getvalue())
        plan = json.loads(output.getvalue())
        self.assertEqual(plan['status'], 'ready', plan['blockers'])
        self.assertEqual(f.git('for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads'), before)

    def test_required_integration_still_requires_a_real_branch(self):
        self.policy['integration'] = 'required'
        self.assertIn('integration_missing', self.codes(self.plan()))

    def test_verified_integration_floor_cannot_be_lowered_by_advisory(self):
        self.assertIn('integration_missing', self.codes(self.plan(floor=True)))

    def test_required_membership_is_independent_of_optional_validation(self):
        self.fixture.manifest['integration']['stacks'] = []
        self.assertIn('integration_membership', self.codes(self.plan()))

    def test_required_ci_cannot_be_replaced_by_another_revision(self):
        self.observation['pr']['checks'][0]['headSha'] = self.fixture.base
        self.assertIn('required_check_failed_or_pending', self.codes(self.plan()))

    def test_required_ci_must_exist(self):
        self.observation['pr']['checks'] = []
        self.assertIn('required_check_missing', self.codes(self.plan()))

    def test_failed_or_pending_required_ci_still_blocks(self):
        for conclusion in ('FAILURE', None):
            with self.subTest(conclusion=conclusion):
                self.observation['pr']['checks'][0].update(
                    conclusion=conclusion, status='COMPLETED' if conclusion else 'IN_PROGRESS')
                self.assertIn('required_check_failed_or_pending', self.codes(self.plan()))

    def test_wrong_pr_head_still_blocks(self):
        self.observation['pr']['headRefOid'] = self.fixture.base
        self.assertIn('head_sha_mismatch', self.codes(self.plan()))

    def test_draft_source_cannot_be_published(self):
        self.fixture.manifest['stacks'][0]['state'] = 'draft'
        self.assertIn('stack_not_published', self.codes(self.plan()))

    def test_wrong_delivery_target_still_blocks(self):
        self.observation['pr']['baseRefName'] = 'other-target'
        self.assertIn('base_branch_mismatch', self.codes(self.plan()))

    def test_unabsorbed_real_dependency_still_blocks(self):
        f = self.fixture
        f.git('switch', '-qc', 'pr/dependency', 'main')
        (f.repo / 'dependency.txt').write_text('real prerequisite\n')
        f.git('add', 'dependency.txt')
        f.git('commit', '-qm', 'Prerequisite')
        tip = f.git('rev-parse', 'HEAD')
        f.git('push', '-q', 'origin', 'pr/dependency')
        f.git('switch', '-q', 'main')
        dependency = copy.deepcopy(f.manifest['stacks'][0])
        dependency.update(id='dependency', branch='pr/dependency', commits=[tip],
                          integration_commits=[tip])
        f.manifest['stacks'].append(dependency)
        f.manifest['integration']['stacks'].append('dependency')
        f.manifest['stacks'][0]['depends_on'] = ['dependency']
        f.manifest['version'] = 3
        f.manifest['coordination'] = SW.disabled_coordination_config(f.manifest_path, 'origin')
        f.manifest = SW.normalize_manifest_data(f.manifest, f.manifest_path)
        self.assertIn('dependency_not_absorbed', self.codes(self.plan()))

    def test_remote_source_drift_still_blocks(self):
        f = self.fixture
        f.git('--git-dir=' + str(f.remote), 'update-ref', 'refs/heads/pr/feature', f.base, f.head)
        self.assertIn('source_remote_drift', self.codes(self.plan()))

    def test_human_gated_authority_is_not_overridden_by_optional_integration(self):
        self.fixture.manifest['authority'] = {'mode': 'human-gated', 'allow': [], 'deny': []}
        with self.assertRaisesRegex(SW.SyncwheelError, 'authority.mode=ai-managed'):
            self.plan()
