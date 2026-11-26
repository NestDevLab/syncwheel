"""Public activation and candidate/release evidence through real fixture Git state."""

import copy
import json
import os
import unittest
from unittest import mock

import test_worktree_policy as worktree_fixtures
import test_github_pr_merge as merge_fixtures
SYNCWHEEL = merge_fixtures.SYNCWHEEL
from syncwheel_errors import SyncwheelError
import syncwheel_execution_policy as execution
from syncwheel_policy import resolve_preferences


def receipt(head, kind, **overrides):
    value = {'schema_version': 1, 'head_sha': head, 'kind': kind,
             'source': 'internal', 'reference': 'fixture-evidence',
             'verified_by': 'operator', 'outcome': 'success'}
    if kind == 'tests':
        value['scope'] = 'targeted'
    elif kind == 'review':
        value.update(scope='sensitive', reviewer='reviewer', author='writer')
    else:
        value.update(installed=True, package_sha256='c' * 64)
    return dict(value, **overrides)


class ExecutionPolicyCliTest(unittest.TestCase):
    def setUp(self):
        self.fixture = worktree_fixtures.WorktreePolicyTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.cli = self.fixture.fixture.run_cli

    def command(self, scope, *args, **kwargs):
        return self.cli(scope, 'execution-policy', *args, '--json', **kwargs)

    def test_disabled_minimal_precedence_provenance_and_clear_preserve_other_data(self):
        self.fixture.profile.write_text(json.dumps({'personal': 'writer', 'hooks': {'mode': 'optional'}}))
        self.command('self', 'set', '--preset', 'disabled', '--apply')
        result = json.loads(self.command('repo', 'set', '--preset', 'minimal').stdout)
        self.assertFalse(result['applied'])
        self.assertNotIn('worktree_policy', json.loads(self.fixture.profile.read_text()))
        self.command('repo', 'set', '--preset', 'minimal', '--apply')
        result = json.loads(self.command('repo', 'status').stdout)
        self.assertEqual(result['effective']['tests'], 'targeted')
        self.assertEqual(result['provenance']['tests'], {'scope': 'repo', 'kind': 'preset', 'preset': 'minimal'})
        self.command('repo', 'set', '--tests', 'off', '--apply')
        result = json.loads(self.command('repo', 'status').stdout)
        self.assertEqual(result['provenance']['tests']['kind'], 'override')
        self.command('repo', 'clear', 'tests', '--apply')
        self.assertEqual(json.loads(self.command('repo', 'status').stdout)['effective']['tests'], 'targeted')
        self.command('repo', 'clear', '--apply')
        self.assertEqual(json.loads(self.fixture.profile.read_text()), {'personal': 'writer', 'hooks': {'mode': 'optional'}})
        self.assertEqual(json.loads(self.command('repo', 'status').stdout)['effective']['tests'], 'off')
        self.command('self', 'clear', 'preset', '--apply')
        self.assertEqual(json.loads(self.command('self', 'status').stdout)['effective']['tests'], 'legacy')

    def test_lane_generation_binding_is_preserved_for_presets(self):
        lane = self.fixture.open_lane()
        self.command('repo', 'set', '--preset', 'minimal', '--apply')
        result = self.cli('worktree', 'execution-policy', lane['id'], 'set', '--preset', 'disabled', '--apply', '--json')
        binding = json.loads(result.stdout)['lane_binding']
        self.assertEqual(binding['state'], 'bound')
        profile = json.loads(self.fixture.profile.read_text())
        profile['lane_worktree_policies'][lane['id']]['generation_token'] = 'old-generation'
        self.fixture.profile.write_text(json.dumps(profile))
        result = json.loads(self.cli('worktree', 'execution-policy', lane['id'], 'status', '--json').stdout)
        self.assertEqual(result['lane_binding']['state'], 'stale')
        self.assertEqual(result['effective']['tests'], 'targeted')
        self.cli('worktree', 'execution-policy', lane['id'], 'set', '--preset', 'disabled', '--apply', expected=2)
        self.cli('worktree', 'execution-policy', lane['id'], 'clear', '--apply', '--json')
        self.assertNotIn('lane_worktree_policies', json.loads(self.fixture.profile.read_text()))

    def test_record_preview_release_pending_then_actual_package_receipt(self):
        head = self.fixture.fixture.git('rev-parse', 'HEAD')
        self.command('repo', 'set', '--preset', 'minimal', '--apply')
        result = json.loads(self.command('repo', 'release-status', '--head', head, expected=1).stdout)
        self.assertEqual(result['status'], 'pending')
        path = self.fixture.fixture.tmp / 'smoke.json'
        path.write_text(json.dumps(receipt(head, 'installation_smoke')))
        self.command('repo', 'record', '--receipt', str(path))
        self.assertNotIn(execution.EVIDENCE_KEY, json.loads(self.fixture.profile.read_text()))
        self.command('repo', 'record', '--receipt', str(path), '--apply')
        result = json.loads(self.command('repo', 'release-status', '--head', head).stdout)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['evidence']['package_sha256'], 'c' * 64)
        self.command('repo', 'release-status', '--head', 'd' * 40, expected=1)

    def test_release_status_honors_the_current_lane_generation_preferences(self):
        lane = self.fixture.open_lane()
        head = self.fixture.fixture.git('rev-parse', 'HEAD')
        self.command('repo', 'set', '--preset', 'disabled', '--apply')
        self.cli('worktree', 'execution-policy', lane['id'], 'set', '--preset', 'minimal', '--apply', '--json')
        self.assertEqual(json.loads(self.command('repo', 'release-status', '--head', head).stdout)['status'], 'complete')
        result = self.command('repo', 'release-status', '--head', head, cwd=lane['path'], expected=1)
        self.assertEqual(json.loads(result.stdout)['status'], 'pending')

    def test_evidence_update_preserves_a_concurrent_configuration_writer(self):
        self.command('repo', 'set', '--preset', 'minimal', '--apply')
        module = self.fixture.module
        profile = module.load_repo_profile(self.fixture.fixture.repo)
        proposed = execution.record_receipts(profile, [receipt('a' * 40, 'tests')])
        concurrent = json.loads(self.fixture.profile.read_text())
        concurrent['personal'] = 'concurrent-writer'
        self.fixture.profile.write_text(json.dumps(concurrent))
        before = self.fixture.profile.read_bytes()
        with self.assertRaisesRegex(SyncwheelError, 'changed concurrently'):
            module.save_repo_profile(self.fixture.fixture.repo, proposed)
        self.assertEqual(self.fixture.profile.read_bytes(), before)

    def test_unknown_presets_and_malformed_receipts_preserve_profile_bytes(self):
        self.command('repo', 'set', '--preset', 'minimal', '--apply')
        before = self.fixture.profile.read_bytes()
        self.command('repo', 'set', '--preset', 'strict', '--apply', expected=2)
        path = self.fixture.fixture.tmp / 'bad.json'
        path.write_text(json.dumps(receipt('a' * 40, 'review', reviewer='WRITER')))
        self.command('repo', 'record', '--receipt', str(path), '--apply', expected=2)
        self.assertEqual(self.fixture.profile.read_bytes(), before)


class ExecutionEvidenceTest(unittest.TestCase):
    def test_receipt_collision_and_invalid_identity_do_not_change_inputs(self):
        original = {'personal': 'writer'}
        first = receipt('a' * 40, 'tests')
        profile = execution.record_receipts(original, [first])
        self.assertEqual(original, {'personal': 'writer'})
        self.assertEqual(execution.record_receipts(profile, [first]), profile)
        before = copy.deepcopy(profile)
        with self.assertRaisesRegex(SyncwheelError, 'collision'):
            execution.record_receipts(profile, [dict(first, reference='different')])
        self.assertEqual(profile, before)
        for bad in (dict(first, outcome='failure'), dict(first, head_sha='main'),
                    receipt('a' * 40, 'review', reviewer='WRITER'),
                    receipt('a' * 40, 'installation_smoke', installed=False)):
            with self.subTest(receipt=bad), self.assertRaises(SyncwheelError):
                execution.normalize_receipt(bad)

    def test_ci_evidence_requires_live_success_on_same_head_not_skipped(self):
        head = 'a' * 40
        profile = execution.record_receipts({}, [receipt(head, 'tests', source='ci', reference='existing-job')])
        report = resolve_preferences({'worktree_policy': {'preset': 'minimal'}})
        # Documentation-only candidate needs targeted tests, but no sensitive review.
        for result in ('SKIPPED', 'FAILURE', 'SUCCESS'):
            blockers, _ = execution.candidate_gates(report, profile, head, sensitive=False,
                checks=[{'name': 'existing-job', 'headSha': head, 'conclusion': result}],
                successful_check=lambda item, sha: item['headSha'] == sha and item['conclusion'] == 'SUCCESS')
            self.assertEqual(bool(blockers), result != 'SUCCESS')
        blockers, _ = execution.candidate_gates(report, profile, 'b' * 40, sensitive=False)
        self.assertTrue(blockers)

    def test_sensitive_review_requires_independent_receipt_for_this_sha(self):
        head = 'a' * 40
        profile = execution.record_receipts({}, [receipt(head, 'tests')])
        report = resolve_preferences({'worktree_policy': {'preset': 'minimal'}})
        blocked, _ = execution.candidate_gates(report, profile, head, sensitive=True)
        self.assertEqual([item['code'] for item in blocked], ['independent_review_evidence_missing'])
        profile = execution.record_receipts(profile, [receipt(head, 'review')])
        blocked, consumed = execution.candidate_gates(report, profile, head, sensitive=True)
        self.assertEqual(blocked, [])
        self.assertEqual(consumed['review']['reviewer'], 'reviewer')
        self.assertFalse(execution.sensitive_paths(['docs/change.md']))
        self.assertTrue(execution.sensitive_paths(['docs/generate.py']))

    def test_merge_floor_keeps_original_provenance_and_disabled_does_not_weaken_it(self):
        report = resolve_preferences({'worktree_policy': {'preset': 'disabled'}})
        result = execution.apply_merge_floor(report, {'checks': 'all', 'integration_validation': 'required'})
        self.assertEqual(result['effective']['checks'], 'all')
        self.assertEqual(result['provenance']['checks']['requested_provenance']['preset'], 'disabled')
        self.assertEqual(report['effective']['checks'], 'none')


class ExecutionMergeGatesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = merge_fixtures.GithubPrMergeTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.settings = self.fixture.tmp / 'settings.json'
        self.env = mock.patch.dict(os.environ, {'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.settings)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def plan(self, preset, *, mandatory=False, tests=True, review=True, ci=None,
             decision='APPROVED', permissions=None, historical_authors=False, observed_actor='Yehonal'):
        fixture = self.fixture
        fixture.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        fixture.manifest_path.write_text(json.dumps(fixture.manifest))
        policy = fixture.policy()
        policy['checks'] = 'none'
        if historical_authors:
            policy['pr_authors'] = ['previous-writer']
            policy['commit_authors'] = ['previous-writer']
            policy['merge_actors'] = ['previous-writer']
        path = fixture.repo / '.syncwheel/profile.local.json'
        profile = json.loads(path.read_text())
        profile['github_pr_merge'] = policy
        profile['worktree_policy'] = {'preset': preset}
        receipts = []
        if tests:
            receipts.append(receipt(fixture.head, 'tests'))
        if review:
            receipts.append(receipt(fixture.head, 'review'))
        profile = execution.record_receipts(profile, receipts)
        path.write_text(json.dumps(profile))
        observation = fixture.observation(decision=decision, checks=ci or [])
        if permissions is not None:
            observation['repositoryInfo']['permissions'] = permissions
        observation['identity']['login'] = observed_actor
        observation['pr']['mergeStateStatus'] = 'CLEAN'
        if mandatory:
            observation['rules']['branchProtection']['required_status_checks'] = {'contexts': ['mandatory-ci']}
        with mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=observation), \
             mock.patch.object(SYNCWHEEL, 'governed_worktree_diagnostics', return_value={'lanes': []}):
            return SYNCWHEEL.build_github_pr_merge_plan(fixture.repo, fixture.manifest,
                fixture.manifest_path, 'feature', fixture.args())

    def test_disabled_skips_optional_checks_but_missing_forge_check_still_blocks(self):
        ready = self.plan('disabled', tests=False, review=False)
        self.assertEqual(ready['status'], 'ready', ready['blockers'])
        blocked = self.plan('disabled', mandatory=True, tests=False, review=False)
        self.assertIn('required_check_missing', [item['code'] for item in blocked['blockers']])

    def test_minimal_internal_review_and_targeted_tests_do_not_substitute_mandatory_forge(self):
        blocked = self.plan('minimal', mandatory=True)
        self.assertIn('required_check_missing', [item['code'] for item in blocked['blockers']])
        ready = self.plan('minimal', mandatory=True, ci=[{'name': 'mandatory-ci', 'headSha': self.fixture.head, 'conclusion': 'SUCCESS'}])
        self.assertEqual(ready['status'], 'ready', ready['blockers'])
        self.assertIn('tests', ready['candidateEvidence'])
        # Fixture feature.txt is documentation-like; executable changes below
        # exercise sensitive review independently of GitHub's approval state.
        self.assertEqual(ready['releaseEvidence']['status'], 'pending')

    def test_operational_presets_never_inherit_legacy_forge_review_bypass(self):
        for preset in ('disabled', 'minimal'):
            with self.subTest(preset=preset):
                blocked = self.plan(preset, decision='REVIEW_REQUIRED')
                self.assertIn('required_reviews_not_authorized', [item['code'] for item in blocked['blockers']])
                self.assertEqual(blocked['path'], 'normal')
                self.assertNotIn('--admin', blocked['command'] or [])
                self.assertEqual(blocked['policy']['allowed_bypasses'], [])

    def test_authorized_writer_does_not_wait_for_historical_author_but_needs_real_permission(self):
        for preset in ('disabled', 'minimal'):
            with self.subTest(preset=preset):
                ready = self.plan(preset, historical_authors=True, permissions={'push': True, 'admin': False})
                self.assertEqual(ready['status'], 'ready', ready['blockers'])
                self.assertNotIn('--admin', ready['command'])
                blocked = self.plan(preset, historical_authors=True, permissions={'pull': True})
                self.assertIn('merge_actor_permission_missing', [item['code'] for item in blocked['blockers']])
                unknown = self.plan(preset, historical_authors=True, permissions={'push': True}, observed_actor=None)
                self.assertIn('merge_actor_unknown', [item['code'] for item in unknown['blockers']])

    def test_minimal_missing_targeted_test_evidence_blocks_without_waiting_for_smoke(self):
        blocked = self.plan('minimal', tests=False)
        codes = [item['code'] for item in blocked['blockers']]
        self.assertIn('targeted_test_evidence_missing', codes)
        self.assertNotIn('installation_smoke', codes)
