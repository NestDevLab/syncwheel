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
        # Authority is published on the actual target, not invented in the
        # selected working manifest. Incorporate that target without rewriting.
        self.fixture.publish_delivery_policy()
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

    def plan(self, *, floor=False, observation_error=None):
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
        with mock.patch.object(SW, 'github_adapter_request', return_value=self.observation,
                               side_effect=observation_error), \
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

    def test_unrelated_and_detached_primaries_block_with_or_without_projection(self):
        f = self.fixture
        for branch in ('unrelated', 'DETACHED'):
            for projection_present in (False, True):
                with self.subTest(branch=branch, projection_present=projection_present):
                    f.git('switch', '-q', 'main')
                    if SW.ref_tip(f.repo, 'refs/heads/unrelated') is None:
                        f.git('branch', 'unrelated', 'main')
                    if projection_present:
                        f.git('branch', 'main-integration', f.head)
                    if branch == 'DETACHED':
                        f.git('switch', '--detach', '-q', f.base)
                    else:
                        f.git('switch', '-q', branch)
                    plan = self.plan()
                    if projection_present:
                        f.git('branch', '-D', 'main-integration')
                    self.assertIn('primary_projection_invalid', self.codes(plan))
                    self.assertIn('primary_projection_not_validated', {w['code'] for w in plan['warnings']})

    def test_target_with_existing_projection_is_not_an_absence_exception(self):
        self.fixture.git('branch', 'main-integration', self.fixture.head)
        self.assertIn('primary_projection_invalid', self.codes(self.plan()))

    def test_remote_projection_cannot_be_hidden_by_removing_local_ref(self):
        self.fixture.git('push', '-q', 'origin', 'pr/feature:main-integration')
        self.assertIn('primary_projection_invalid', self.codes(self.plan()))

    def test_working_primary_cannot_elevate_published_authority(self):
        f = self.fixture
        f.manifest['authority']['allow'].append('runtime_change')
        plan = self.plan()
        self.assertIn('primary_projection_invalid', self.codes(plan))
        self.assertFalse(plan['primaryProjectionEvidence']['authorized'])
        self.assertIn('working authority exceeds published target policy', plan['primaryProjectionEvidence']['reasons'])

    def test_stale_forge_base_cannot_authorize_current_git_target(self):
        self.observation['pr']['baseRefOid'] = self.fixture.git('rev-parse', 'main^')
        plan = self.plan()
        self.assertIn('primary_projection_invalid', self.codes(plan))
        self.assertIn('delivery target is not bound to the current forge base',
                      plan['primaryProjectionEvidence']['reasons'])

    def test_published_human_policy_cannot_be_elevated_by_primary(self):
        f = self.fixture
        canonical = json.loads(f.git('show', 'main:.syncwheel/manifest.json'))
        canonical['authority'] = {'mode': 'human-gated', 'allow': [], 'deny': []}
        f.manifest_path.write_text(json.dumps(canonical) + '\n')
        f.git('add', '.syncwheel/manifest.json')
        f.git('commit', '-qm', 'Require human authority')
        f.git('push', '-q', 'origin', 'main')
        self.observation['pr']['baseRefOid'] = f.git('rev-parse', 'HEAD')
        plan = self.plan()
        self.assertIn('primary_projection_invalid', self.codes(plan))
        self.assertIn('published target does not authorize source changes',
                      plan['primaryProjectionEvidence']['reasons'])

    def test_selected_integration_cannot_invent_compliance(self):
        self.fixture.git('switch', '-qc', 'unrelated')
        self.fixture.manifest['integration']['branch'] = 'unrelated'
        self.assertIn('primary_projection_invalid', self.codes(self.plan()))

    def test_preview_preserves_primary_head_index_and_dirty_content(self):
        f = self.fixture
        (f.repo / 'base.txt').write_text('unstaged primary work\n')
        (f.repo / 'staged.txt').write_text('staged primary work\n')
        (f.repo / 'untracked.txt').write_text('untracked primary work\n')
        f.git('add', 'staged.txt')
        self.plan()
        before = (f.git('rev-parse', 'HEAD'), (f.repo / '.git/index').read_bytes(),
                  f.git('diff', '--binary'), (f.repo / 'untracked.txt').read_bytes())
        plan = self.plan()
        self.assertEqual('ready', plan['status'], plan['blockers'])
        after = (f.git('rev-parse', 'HEAD'), (f.repo / '.git/index').read_bytes(),
                 f.git('diff', '--binary'), (f.repo / 'untracked.txt').read_bytes())
        self.assertEqual(before, after)

    def assert_observation_failure_is_read_only(self, code, *, error=None):
        f = self.fixture
        # Materialize the selected private configuration before measuring.
        self.plan()
        before = (f.git('for-each-ref', '--format=%(refname) %(objectname)'),
                  f.git('rev-parse', 'HEAD'), (f.repo / '.git/index').read_bytes(),
                  f.manifest_path.read_bytes(), f.git('status', '--porcelain'))
        plan = self.plan(observation_error=error)
        self.assertEqual('blocked', plan['status'])
        self.assertIn(code, self.codes(plan))
        self.assertFalse(plan['primaryProjectionEvidence']['sourceVerified'])
        self.assertFalse(plan['primaryProjectionEvidence']['authorized'])
        self.assertIsNone(plan['primaryProjectionEvidence']['fetchedSha'])
        self.assertEqual(before, (f.git('for-each-ref', '--format=%(refname) %(objectname)'),
                                f.git('rev-parse', 'HEAD'), (f.repo / '.git/index').read_bytes(),
                                f.manifest_path.read_bytes(), f.git('status', '--porcelain')))
        self.assertEqual([], list((f.repo / '.syncwheel/ledger').rglob('*')))
        return plan

    def test_missing_pr_returns_blocked_plan_without_effects(self):
        self.observation.update(pr=None, selection={'count': 0})
        self.assert_observation_failure_is_read_only('pull_request_not_found')

    def test_ambiguous_pr_returns_blocked_plan_without_effects(self):
        self.observation.update(pr=None, selection={'count': 2})
        self.assert_observation_failure_is_read_only('pull_request_ambiguous')

    def test_observation_error_returns_blocked_plan_without_effects(self):
        self.assert_observation_failure_is_read_only(
            'github_observation_failed', error=SW.SyncwheelError('fixture observation failed'))

    def test_recovery_discovery_error_preserves_the_blocked_observation_plan(self):
        with mock.patch.object(SW._actor_recovery, '_live_policy',
                               side_effect=SW.SyncwheelError('fixture recovery authority unavailable')):
            plan = self.assert_observation_failure_is_read_only(
                'github_observation_failed', error=SW.SyncwheelError('fixture observation failed'))
        self.assertEqual(plan['actorRecovery'], {
            'status': 'blocked', 'detail': 'fixture recovery authority unavailable', 'policy': None,
        })
        self.assertIn('attributed_delivery_recovery', self.codes(plan))

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
