import contextlib
import copy
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / 'scripts'
if str(SCRIPTS) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('syncwheel_github_merge_test', SCRIPTS / 'syncwheel.py')
SYNCWHEEL = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SYNCWHEEL)
import syncwheel_github as ADAPTER


class GithubPrMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='syncwheel-github-pr-merge-'))
        self.remote = self.tmp / 'origin.git'
        self.repo = self.tmp / 'repo'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        subprocess.run(['git', 'init', '--quiet', '-b', 'main', str(self.repo)], check=True)
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('remote', 'add', 'origin', str(self.remote))
        (self.repo / '.gitignore').write_text('.syncwheel/profile.local.json\n.syncwheel/ledger/\n')
        (self.repo / 'base.txt').write_text('base\n')
        self.git('add', '.gitignore', 'base.txt')
        self.git('commit', '-qm', 'base')
        self.git('push', '-qu', 'origin', 'main')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-qc', 'pr/feature', 'main')
        (self.repo / 'feature.txt').write_text('feature\n')
        self.git('add', 'feature.txt')
        self.git('commit', '-qm', 'feature')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('push', '-qu', 'origin', 'pr/feature')
        self.git('switch', '-qc', 'main-integration', 'main')
        self.git('cherry-pick', self.head)
        self.integration = self.git('rev-parse', 'HEAD')
        self.manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        self.manifest_path.parent.mkdir(exist_ok=True)
        self.manifest = {
            'version': 1,
            'repository_mode': 'delivery',
            'defaults': {
                'canonical_remote': 'origin', 'publication_remote': 'origin',
                'base_branch': 'main', 'base_ref': 'origin/main',
                'integration_membership': 'legacy',
            },
            'integration': {
                'branch': 'main-integration', 'base': 'origin/main',
                'strategy': 'cherry-pick', 'stacks': ['feature'],
            },
            'stacks': [{
                'id': 'feature', 'branch': 'pr/feature', 'base': 'origin/main',
                'target_remote': 'origin', 'target_branch': 'main',
                'integration_branch': 'main-integration', 'commits': [self.head],
                'integration_commits': [self.integration], 'state': 'published',
            }],
        }
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        profile_path = self.repo / '.syncwheel' / 'profile.local.json'
        profile_path.write_text(json.dumps({'hooks': {'mode': 'required'}}, indent=2) + '\n')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def git(self, *args):
        result = subprocess.run(['git', *args], cwd=self.repo, text=True, capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout.strip()

    def policy(self):
        return {
            'enabled': True,
            'repository': 'NestDevLab/agent-core-toolkit-public',
            'base_branches': ['main'],
            'merge_method': 'squash',
            'allowed_bypasses': ['required_reviews'],
            'merge_actors': ['Yehonal'],
            'pr_authors': ['Yehonal'],
            'commit_authors': ['Yehonal'],
            'head_repositories': ['NestDevLab/agent-core-toolkit-public'],
            'checks': 'all',
        }

    def observation(self, *, state='OPEN', decision='REVIEW_REQUIRED', checks=None, head=None):
        return {
            'schemaVersion': 1,
            'repository': 'NestDevLab/agent-core-toolkit-public',
            'identity': {'login': 'Yehonal'},
            'repositoryInfo': {
                'permissions': {'admin': True},
                'allowMergeMethods': {'squash': True, 'merge': True, 'rebase': True},
            },
            'pr': {
                'number': 25, 'url': 'https://github.com/NestDevLab/agent-core-toolkit-public/pull/25',
                'state': state, 'isDraft': False, 'headRefName': 'pr/feature',
                'headRefOid': head or self.head, 'baseRefName': 'main', 'baseRefOid': self.base,
                'headRepository': 'NestDevLab/agent-core-toolkit-public', 'author': 'Yehonal',
                'commitAuthors': [{'sha': self.head, 'login': 'Yehonal'}],
                'review': {
                    'decision': decision, 'changesRequested': False,
                    'threads': [], 'unresolvedThreads': [],
                },
                'mergeable': 'MERGEABLE', 'mergeStateStatus': 'BLOCKED',
                'checks': checks if checks is not None else [
                    {'name': 'ci', 'status': 'SUCCESS', 'conclusion': 'SUCCESS'},
                ],
                'mergeCommit': None,
            },
            'rules': {
                'branchProtection': {
                    'required_pull_request_reviews': {'required_approving_review_count': 1},
                },
                'rulesets': [],
            },
        }

    def args(self, *, apply=False, operation_id='merge-1', plan_digest=None):
        return SimpleNamespace(
            repo=str(self.repo), manifest=None, personal=None, stack='feature',
            apply=apply, operation_id=operation_id, plan_digest=plan_digest,
            json=True,
        )

    def test_policy_normalization_requires_a_provenance_filter(self):
        invalid = self.policy()
        invalid.pop('pr_authors')
        invalid.pop('commit_authors')
        invalid.pop('head_repositories')
        with self.assertRaisesRegex(SYNCWHEEL.SyncwheelError, 'at least one'):
            SYNCWHEEL.normalize_github_pr_merge_policy(invalid)

    def test_policy_normalization_accepts_explicit_no_checks(self):
        policy = self.policy()
        policy['checks'] = 'none'
        self.assertEqual(
            SYNCWHEEL.normalize_github_pr_merge_policy(policy)['checks'], 'none'
        )

    def test_policy_dry_run_preserves_existing_profile_and_does_not_write(self):
        before = (self.repo / '.syncwheel' / 'profile.local.json').read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            SYNCWHEEL.github_pr_merge_policy_edit(
                self.repo, self.policy(), apply=False, json_mode=True,
            )
        self.assertEqual((self.repo / '.syncwheel' / 'profile.local.json').read_bytes(), before)
        report = json.loads(output.getvalue())
        self.assertEqual(report['preservedKeys'], ['hooks'])
        self.assertTrue(report['dryRun'])

    def test_preview_selects_admin_review_bypass_and_is_digest_stable(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = self.policy()
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=self.observation()):
            first = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
            second = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
        self.assertEqual(first['status'], 'ready')
        self.assertEqual(first['path'], 'admin-review-bypass')
        self.assertIn('--admin', first['command'])
        self.assertIn('--match-head-commit', first['command'])
        self.assertEqual(first['planDigest'], second['planDigest'])

    def test_historical_pr_base_is_allowed_when_head_contains_live_base(self):
        other = self.tmp / 'other'
        subprocess.run(['git', 'clone', '-q', '-b', 'main', str(self.remote), str(other)], check=True)
        subprocess.run(['git', 'config', 'user.name', 'Fixture'], cwd=other, check=True)
        subprocess.run(['git', 'config', 'user.email', 'fixture@example.invalid'], cwd=other, check=True)
        (other / 'later.txt').write_text('later\n')
        subprocess.run(['git', 'add', 'later.txt'], cwd=other, check=True)
        subprocess.run(['git', 'commit', '-qm', 'advance main'], cwd=other, check=True)
        subprocess.run(['git', 'push', '-q', 'origin', 'main'], cwd=other, check=True)
        live_base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=other, text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual(self.git('rev-parse', 'origin/main'), self.base)
        self.git('fetch', '-q', 'origin', 'refs/heads/main')
        self.git('switch', 'pr/feature')
        self.git('merge', '--no-edit', 'FETCH_HEAD')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'origin', 'pr/feature')
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = self.policy()
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_stack_git_preflight', return_value=(self.head, [self.head])), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=self.observation()):
            plan = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['pullRequest']['baseSha'], self.base)
        self.assertEqual(plan['baseSha'], live_base)

    def test_base_change_between_observation_and_fetch_blocks_merge(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = self.policy()
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        remote_ref_tips = SYNCWHEEL.remote_ref_tips
        def changed_base(repo, remote, refs):
            if refs == ['refs/heads/main']:
                return {'refs/heads/main': '0' * 40}
            return remote_ref_tips(repo, remote, refs)
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=self.observation()), \
             mock.patch.object(SYNCWHEEL, 'remote_ref_tips', side_effect=changed_base):
            plan = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
        self.assertEqual(plan['status'], 'blocked')
        self.assertTrue(any(item['code'] == 'base_fetch_failed' for item in plan['blockers']))

    def test_advanced_live_base_not_in_head_blocks_merge(self):
        self.git('switch', 'main')
        (self.repo / 'later.txt').write_text('later\n')
        self.git('add', 'later.txt')
        self.git('commit', '-qm', 'advance main')
        self.git('push', '-q', 'origin', 'main')
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = self.policy()
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=self.observation()):
            plan = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
        self.assertEqual(plan['status'], 'blocked')
        self.assertTrue(any(item['code'] == 'base_not_in_head' for item in plan['blockers']))

    def test_failed_check_blocks_and_never_selects_bypass(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = self.policy()
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        failed = [{'name': 'ci', 'status': 'FAILURE', 'conclusion': 'FAILURE'}]
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=self.observation(checks=failed)):
            plan = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
        self.assertEqual(plan['status'], 'blocked')
        self.assertTrue(any(item['code'] == 'check_failed_or_pending' for item in plan['blockers']))

    def test_explicit_no_checks_allows_a_pr_without_ci_and_warns(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        policy = self.policy()
        policy['checks'] = 'none'
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = policy
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        observed = self.observation(decision='APPROVED', checks=[])
        observed['pr']['mergeStateStatus'] = 'CLEAN'
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=observed):
            plan = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
        self.assertEqual(plan['status'], 'ready')
        self.assertFalse(any(item['code'] == 'checks_missing' for item in plan['blockers']))
        self.assertTrue(any(item['code'] == 'checks_not_required' for item in plan['warnings']))

    def test_required_check_context_must_be_present(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = self.policy()
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        observed = self.observation(checks=[{'name': 'other-ci', 'status': 'SUCCESS'}])
        observed['rules']['branchProtection']['required_status_checks'] = {'contexts': ['required-ci']}
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=observed):
            plan = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
        self.assertTrue(any(item['code'] == 'required_check_missing' for item in plan['blockers']))

    def test_explicit_no_checks_still_blocks_failed_or_pending_required_checks(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        policy = self.policy()
        policy['checks'] = 'none'
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = policy
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        for status in ('FAILURE', 'PENDING'):
            with self.subTest(status=status):
                observed = self.observation(
                    decision='APPROVED',
                    checks=[{'name': 'required-ci', 'status': status}],
                )
                observed['pr']['mergeStateStatus'] = 'CLEAN'
                observed['rules']['branchProtection']['required_status_checks'] = {
                    'contexts': ['required-ci']
                }
                with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
                     mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=observed):
                    plan = SYNCWHEEL.build_github_pr_merge_plan(
                        self.repo, self.manifest, self.manifest_path, 'feature', self.args()
                    )
                self.assertEqual(plan['status'], 'blocked')
                self.assertTrue(any(
                    item['code'] == 'required_check_failed_or_pending'
                    for item in plan['blockers']
                ))

    def private_free_observation(self):
        observed = self.observation(decision='')
        observed['repositoryInfo']['isPrivate'] = True
        observed['pr']['mergeStateStatus'] = 'CLEAN'
        detail = 'gh: Upgrade to GitHub Pro or make this repository public to enable this feature. (HTTP 403)'
        observed['rules'] = {
            'branchProtection': None,
            'branchProtectionStatus': 403,
            'branchProtectionError': detail,
            'rulesets': None,
            'rulesetsStatus': 403,
            'rulesetsError': detail,
        }
        return observed

    def empty_rules_proof(self):
        return {'data': {'repository': {
            'nameWithOwner': 'Example/project', 'viewerPermission': 'ADMIN', 'isPrivate': True,
            'branchProtectionRules': {'totalCount': 0, 'pageInfo': {'hasNextPage': False}, 'nodes': []},
            'rulesets': {'totalCount': 0, 'pageInfo': {'hasNextPage': False}, 'nodes': []},
        }}}

    def observe_empty_rules(self, graph=None, *, protection=None, rulesets=None, repo_info=None, repository="Example/project"):
        limited = {'status': 403, 'value': None, 'detail': 'gh: ' + ADAPTER.PLAN_LIMIT_MESSAGE + ' (HTTP 403)'}
        if graph is None:
            graph = self.empty_rules_proof()
            graph['data']['repository']['nameWithOwner'] = repository
        with mock.patch.object(ADAPTER, 'run_json', return_value={
            'ok': True, 'value': graph,
        }) as query:
            result = ADAPTER._observe_rules(
                repository, repo_info if repo_info is not None else {
                    'private': True, 'full_name': repository, 'permissions': {'admin': True},
                }, protection if protection is not None else limited,
                rulesets if rulesets is not None else limited, self.repo,
            )
        return result, query

    def test_graphql_empty_rules_preserves_rest_provenance_and_uses_variables(self):
        rules, query = self.observe_empty_rules()
        self.assertEqual(rules['observationSource'], 'graphql-complete-empty')
        self.assertEqual(rules['branchProtection'], {})
        self.assertEqual(rules['rulesets'], [])
        for field in ('branchProtection', 'rulesets'):
            self.assertEqual(rules[field + 'Status'], 200)
            self.assertIsNone(rules[field + 'Error'])
            self.assertEqual(rules['restObservation'][field]['status'], 403)
            self.assertIn(ADAPTER.PLAN_LIMIT_MESSAGE, rules['restObservation'][field]['detail'])
        argv = query.call_args.args[0]
        self.assertIn('query=' + ADAPTER.EMPTY_RULES_QUERY, argv)
        self.assertIn('owner=Example', argv)
        self.assertIn('name=project', argv)
        self.assertNotIn('Example', ADAPTER.EMPTY_RULES_QUERY)
        self.assertEqual(rules['emptyRulesEvidence'], self.empty_rules_proof()['data']['repository'])

    def test_graphql_empty_rules_rejects_partial_errors_identity_and_nonempty_connections(self):
        invalid = [None, [], {}, {'errors': [], **self.empty_rules_proof()},
                   {'errors': [{'message': 'denied'}], **self.empty_rules_proof()},
                   {'data': None}, {'data': {'repository': None}}]
        for field, values in {
            'nameWithOwner': [None, 'Other/project', 42],
            'viewerPermission': [None, 'WRITE', 'MAINTAIN'],
            'isPrivate': [None, False, 1],
        }.items():
            for value in values:
                graph = self.empty_rules_proof()
                graph['data']['repository'][field] = value
                invalid.append(graph)
        for connection in ('branchProtectionRules', 'rulesets'):
            for value in (None, [], {},
                          {'totalCount': 1, 'pageInfo': {'hasNextPage': False}, 'nodes': []},
                          {'totalCount': False, 'pageInfo': {'hasNextPage': False}, 'nodes': []},
                          {'totalCount': '0', 'pageInfo': {'hasNextPage': False}, 'nodes': []},
                          {'totalCount': 0.0, 'pageInfo': {'hasNextPage': False}, 'nodes': []},
                          {'totalCount': 0, 'pageInfo': {'hasNextPage': True}, 'nodes': []},
                          {'totalCount': 0, 'pageInfo': {'hasNextPage': 0}, 'nodes': []},
                          {'totalCount': 0, 'pageInfo': {}, 'nodes': []},
                          {'totalCount': 0, 'pageInfo': None, 'nodes': []},
                          {'totalCount': 0, 'pageInfo': {'hasNextPage': False}, 'nodes': None},
                          {'totalCount': 0, 'pageInfo': {'hasNextPage': False}, 'nodes': [{}]}):
                graph = self.empty_rules_proof()
                graph['data']['repository'][connection] = value
                invalid.append(graph)
        for graph in invalid:
            with self.subTest(graph=graph):
                # None must be passed directly rather than the success helper default.
                with mock.patch.object(ADAPTER, 'run_json', return_value={'value': graph}):
                    limited = {'status': 403, 'value': None, 'detail': ADAPTER.PLAN_LIMIT_MESSAGE}
                    rules = ADAPTER._observe_rules('Example/project', {
                        'private': True, 'full_name': 'Example/project', 'permissions': {'admin': True},
                    }, limited, limited, self.repo)
                self.assertEqual(rules['observationSource'], 'rest')
                self.assertEqual(rules['rulesetsStatus'], 403)
                self.assertNotIn('emptyRulesEvidence', rules)

    def test_observe_integrates_empty_rules_proof_after_rest_failures(self):
        graph = self.empty_rules_proof()
        repo = {'private': True, 'full_name': 'Example/project', 'permissions': {'admin': True}}
        limited = {'status': 403, 'value': None, 'detail': ADAPTER.PLAN_LIMIT_MESSAGE}
        replies = [{'value': value} for value in (repo, {'login': 'fixture'}, {}, {}, graph)]
        with mock.patch.object(ADAPTER, 'run_json', side_effect=replies) as query, \
             mock.patch.object(ADAPTER, '_optional_api', return_value=limited) as rest:
            observed = ADAPTER.observe({
                'repository': 'Example/project', 'baseBranch': 'main', 'pullRequestNumber': 25,
            }, self.repo)
        self.assertEqual(rest.call_count, 2)
        self.assertIn('query=' + ADAPTER.EMPTY_RULES_QUERY, query.call_args.args[0])
        self.assertEqual(observed['rules']['observationSource'], 'graphql-complete-empty')
        self.assertEqual(observed['rules']['restObservation']['rulesets'], limited)

    def test_graphql_empty_rules_requires_compatible_rest_and_private_admin_identity(self):
        incompatible = [
            {'status': 403, 'value': None, 'detail': 'Resource not accessible by integration'},
            {'status': 403, 'value': None, 'detail': ADAPTER.PLAN_LIMIT_MESSAGE + ' denied'},
            {'status': 500, 'value': None, 'detail': ADAPTER.PLAN_LIMIT_MESSAGE},
            {'status': -1, 'value': None, 'detail': 'timeout'},
            {'status': 200, 'value': None, 'detail': 'invalid JSON'},
            {'status': 200, 'value': {'required_status_checks': {}}, 'detail': None},
            {'status': 200, 'value': [{'type': 'pull_request'}], 'detail': None},
            {'status': 404, 'value': {}, 'detail': None},
        ]
        for endpoint in ('protection', 'rulesets'):
            for result in incompatible:
                with self.subTest(endpoint=endpoint, result=result):
                    rules, query = self.observe_empty_rules(**{endpoint: result})
                    query.assert_not_called()
                    self.assertEqual(rules['observationSource'], 'rest')
        for field, values in {
            'private': [False, None, 1], 'full_name': [None, 'Other/project'],
            'permissions': [None, {}, {'admin': False}, {'admin': 1}],
        }.items():
            for value in values:
                info = {'private': True, 'full_name': 'Example/project', 'permissions': {'admin': True}}
                info[field] = value
                rules, query = self.observe_empty_rules(repo_info=info)
                query.assert_not_called()
                self.assertEqual(rules['observationSource'], 'rest')
        # Both ordinary REST observations remain REST-only, without querying GraphQL.
        rules, query = self.observe_empty_rules(
            protection={'status': 404, 'value': None, 'detail': None},
            rulesets={'status': 200, 'value': [], 'detail': None},
        )
        query.assert_not_called()
        self.assertEqual(rules['observationSource'], 'rest')

    def test_graphql_empty_rules_accepts_compatible_mixed_rest_observations(self):
        for endpoint, value in (('protection', {}), ('rulesets', [])):
            for status in (200, 404):
                with self.subTest(endpoint=endpoint, status=status):
                    rules, query = self.observe_empty_rules(**{endpoint: {
                        'status': status, 'value': value if status == 200 else None, 'detail': None,
                    }})
                    query.assert_called_once()
                    self.assertEqual(rules['observationSource'], 'graphql-complete-empty')

    def test_graphql_empty_rules_command_failure_retains_rest_failure(self):
        limited = {'status': 403, 'value': None, 'detail': ADAPTER.PLAN_LIMIT_MESSAGE}
        with mock.patch.object(ADAPTER, 'run_json', side_effect=ADAPTER.AdapterError('query failed')):
            rules = ADAPTER._observe_rules('Example/project', {
                'private': True, 'full_name': 'Example/project', 'permissions': {'admin': True},
            }, limited, limited, self.repo)
        self.assertEqual(rules['branchProtectionStatus'], 403)
        self.assertEqual(rules['emptyRulesEvidenceError'], 'query failed')

    def test_graphql_empty_rules_proof_allows_normal_plan_without_bypasses(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        policy = self.policy()
        policy['allowed_bypasses'] = ['required_reviews']
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = policy
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        observed = self.private_free_observation()
        observed['rules'], _ = self.observe_empty_rules(repository=observed['repository'])
        for valid in (True, False):
            candidate = copy.deepcopy(observed)
            if not valid:
                candidate['rules'] = self.private_free_observation()['rules']
            with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
                 mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=candidate):
                plan = SYNCWHEEL.build_github_pr_merge_plan(
                    self.repo, self.manifest, self.manifest_path, 'feature', self.args()
                )
            self.assertIsNone(plan['rulesFallback'])
            self.assertEqual(plan['status'], 'ready' if valid else 'blocked')
            if valid:
                self.assertEqual(plan['path'], 'normal')
                self.assertNotIn('--admin', plan['command'])
                self.assertIn('--match-head-commit', plan['command'])
                self.assertIn(self.head, plan['command'])
                self.assertFalse(any(item['code'] == 'private_free_rules_unavailable' for item in plan['warnings']))
            else:
                self.assertTrue(any(item['code'] == 'rules_unavailable' for item in plan['blockers']))

        # Alternative rule evidence does not relax CI, review, or provenance gates.
        failed_check = copy.deepcopy(observed)
        failed_check['pr']['checks'][0]['status'] = 'FAILURE'
        failed_check['pr']['checks'][0]['conclusion'] = 'FAILURE'
        unresolved_review = copy.deepcopy(observed)
        unresolved_review['pr']['review']['unresolvedThreads'] = [{'isResolved': False}]
        foreign_author = copy.deepcopy(observed)
        foreign_author['pr']['author'] = 'other-fixture'
        for candidate in (failed_check, unresolved_review, foreign_author):
            with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
                 mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=candidate):
                plan = SYNCWHEEL.build_github_pr_merge_plan(
                    self.repo, self.manifest, self.manifest_path, 'feature', self.args()
                )
            self.assertEqual(plan['status'], 'blocked')
            self.assertIsNone(plan['rulesFallback'])

    def test_private_free_rules_fallback_requires_explicit_policy(self):
        observed = self.private_free_observation()
        blockers = []
        fallback = SYNCWHEEL.github_rules_blockers(
            observed['rules'],
            blockers,
            repository_info=observed['repositoryInfo'],
            allowed_bypasses=['required_reviews'],
        )
        self.assertIsNone(fallback)
        self.assertEqual([item['code'] for item in blockers], ['rules_unavailable', 'rules_unavailable'])

    def test_private_free_rules_fallback_allows_a_green_merge_plan(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        policy = self.policy()
        policy['allowed_bypasses'].append('private_free_rules')
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = policy
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        observed = self.private_free_observation()
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=observed):
            plan = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['rulesFallback'], 'private_free_rules')
        self.assertTrue(any(item['code'] == 'private_free_rules_unavailable' for item in plan['warnings']))
        self.assertFalse(any(item['code'] == 'rules_unavailable' for item in plan['blockers']))

    def test_private_free_rules_fallback_rejects_public_or_unrecognized_403(self):
        observed = self.private_free_observation()
        observed['repositoryInfo']['isPrivate'] = False
        self.assertFalse(SYNCWHEEL.github_private_free_rules_unavailable(
            observed['repositoryInfo'], observed['rules'],
        ))
        observed['repositoryInfo']['isPrivate'] = True
        observed['rules']['rulesetsError'] = 'gh: Resource not accessible by integration (HTTP 403)'
        self.assertFalse(SYNCWHEEL.github_private_free_rules_unavailable(
            observed['repositoryInfo'], observed['rules'],
        ))

    def test_merge_receipt_reconciles_success_without_second_merge(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = self.policy()
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        preview_args = self.args()
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=self.observation()):
            preview = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', preview_args
            )
        merged = self.observation(state='MERGED', decision='APPROVED')
        merged['pr']['mergeCommit'] = 'f' * 40
        adapter_results = [self.observation(), {'ok': False, 'returncode': 1, 'stderr': 'already merged'}, merged]
        output = io.StringIO()
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', side_effect=adapter_results), \
             mock.patch.object(SYNCWHEEL, 'github_pr_merge_postverify', return_value={'status': 'verified'}), \
             contextlib.redirect_stdout(output):
            result = SYNCWHEEL.command_stack_merge_pr(
                self.args(apply=True, plan_digest=preview['planDigest'])
            )
        self.assertEqual(result, 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt['status'], 'succeeded-equivalent')
        events = SYNCWHEEL.load_ledger_events(self.repo, self.manifest_path)
        self.assertTrue(any(event['type'] == 'github_pr_merge_receipt' for event in events))

    def test_rejected_merge_is_recorded_as_failed_after_reobserve(self):
        self.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        profile = json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())
        profile['github_pr_merge'] = self.policy()
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps(profile) + '\n')
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', return_value=self.observation()):
            preview = SYNCWHEEL.build_github_pr_merge_plan(
                self.repo, self.manifest, self.manifest_path, 'feature', self.args()
            )
        open_after_rejection = self.observation()
        adapter_results = [
            self.observation(),
            {'ok': False, 'returncode': 1, 'argv': ['pr', 'merge'], 'stderr': 'protected branch'},
            open_after_rejection,
        ]
        output = io.StringIO()
        with mock.patch.object(SYNCWHEEL, 'validate_manifest', return_value={'errors': []}), \
             mock.patch.object(SYNCWHEEL, 'github_adapter_request', side_effect=adapter_results), \
             contextlib.redirect_stdout(output):
            result = SYNCWHEEL.command_stack_merge_pr(
                self.args(apply=True, plan_digest=preview['planDigest'])
            )
        self.assertEqual(result, 2)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt['status'], 'failed')
        self.assertEqual(receipt['adapterResult']['returncode'], 1)


class GithubAdapterTest(unittest.TestCase):
    def test_commit_authors_reads_gh_cli_authors_list(self):
        observed = {
            'commits': [{
                'oid': 'a' * 40,
                'authors': [{'login': 'Yehonal', 'name': 'Yehonal'}],
            }],
        }
        self.assertEqual(
            ADAPTER._commit_authors(observed),
            [{'sha': 'a' * 40, 'login': 'Yehonal', 'name': 'Yehonal'}],
        )

    def test_commit_authors_preserves_unresolved_author_as_blocking_record(self):
        observed = {'commits': [{'oid': 'b' * 40, 'authors': []}]}
        self.assertEqual(
            ADAPTER._commit_authors(observed),
            [{'sha': 'b' * 40, 'login': None, 'name': None}],
        )

    def test_core_preserves_nonzero_merge_adapter_result_for_reconciliation(self):
        response = {
            'schemaVersion': 1,
            'ok': False,
            'returncode': 1,
            'argv': ['pr', 'merge', '25'],
            'stderr': 'protected branch',
        }
        completed = SimpleNamespace(returncode=2, stdout=json.dumps(response), stderr='')
        with mock.patch.object(SYNCWHEEL.shutil, 'which', return_value='/usr/local/bin/syncwheel-github'), \
             mock.patch.object(SYNCWHEEL.subprocess, 'run', return_value=completed):
            result = SYNCWHEEL.github_adapter_request(
                Path('/tmp'), {'operation': 'merge', 'pullRequestNumber': 25}
            )
        self.assertEqual(result['returncode'], 1)

    def test_merge_argv_is_fixed_and_contains_head_pin_without_delete_branch(self):
        completed = SimpleNamespace(returncode=0, stdout='merged', stderr='')
        with mock.patch.object(ADAPTER.subprocess, 'run', return_value=completed) as run:
            result = ADAPTER.merge({
                'repository': 'NestDevLab/example', 'pullRequestNumber': 25,
                'method': 'squash', 'admin': True, 'headSha': 'a' * 40,
            }, Path('/tmp'))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:4], ['gh', 'pr', 'merge', '25'])
        self.assertIn('--admin', argv)
        self.assertIn('--match-head-commit', argv)
        self.assertNotIn('--delete-branch', argv)
        self.assertTrue(result['ok'])


if __name__ == '__main__':
    unittest.main()
