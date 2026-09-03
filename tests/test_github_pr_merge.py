import contextlib
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
