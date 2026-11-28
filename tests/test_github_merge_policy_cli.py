import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CLI = Path(__file__).resolve().parents[1] / 'scripts' / 'syncwheel.py'


class MergePolicyCliTest(unittest.TestCase):
    def test_bypasses_are_empty_by_default_and_explicit_when_requested(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / '.gitignore').write_text('.syncwheel/profile.local.json\n')
            command = [sys.executable, str(CLI), 'repo', 'pr-merge-policy', 'set', 'github',
                       '--repository', 'example/project', '--base', 'main', '--method', 'squash',
                       '--merge-actor', 'reviewer', '--pr-author', 'author', '--json']
            for options, expected in (([], []), (['--allow-bypass', 'required_reviews'], ['required_reviews'])):
                result = subprocess.run(command + options, cwd=repo, text=True, capture_output=True, check=True)
                self.assertEqual(json.loads(result.stdout)['proposed']['allowed_bypasses'], expected)
            for options in (['--allow-bypass', 'unknown'],
                            ['--allow-bypass', 'required_reviews', '--allow-bypass', 'required_reviews']):
                result = subprocess.run(command + options, cwd=repo, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)

    def test_review_decision_requires_explicit_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, repo, bindir = root / 'origin.git', root / 'repo', root / 'bin'
            bindir.mkdir()
            subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
            subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
            def git(*args):
                return subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
            git('config', 'user.name', 'Fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('remote', 'add', 'origin', str(remote))
            (repo / '.gitignore').write_text('.syncwheel/profile.local.json\n.syncwheel/ledger/\n')
            (repo / 'base.txt').write_text('base\n')
            git('add', '.')
            git('commit', '-qm', 'base')
            git('push', '-qu', 'origin', 'main')
            base = git('rev-parse', 'HEAD')
            git('switch', '-qc', 'pr/feature')
            (repo / 'feature.txt').write_text('feature\n')
            git('add', 'feature.txt')
            git('commit', '-qm', 'feature')
            head = git('rev-parse', 'HEAD')
            git('push', '-qu', 'origin', 'pr/feature')
            git('switch', '-qc', 'main-integration', 'main')
            git('cherry-pick', head)
            integration = git('rev-parse', 'HEAD')
            control = repo / '.syncwheel'
            control.mkdir()
            manifest = {'version': 1, 'repository_mode': 'delivery',
                        'authority': {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []},
                        'defaults': {'canonical_remote': 'origin', 'publication_remote': 'origin',
                                     'base_branch': 'main', 'base_ref': 'origin/main', 'integration_membership': 'legacy'},
                        'integration': {'branch': 'main-integration', 'base': 'origin/main',
                                        'strategy': 'cherry-pick', 'stacks': ['feature']},
                        'stacks': [{'id': 'feature', 'branch': 'pr/feature', 'base': 'origin/main',
                                    'target_remote': 'origin', 'target_branch': 'main',
                                    'integration_branch': 'main-integration', 'commits': [head],
                                    'integration_commits': [integration], 'state': 'published'}]}
            (control / 'manifest.json').write_text(json.dumps(manifest))
            policy = {'enabled': True, 'repository': 'example/project', 'base_branches': ['main'],
                      'merge_method': 'squash', 'allowed_bypasses': [], 'merge_actors': ['reviewer'],
                      'pr_authors': ['author'], 'checks': 'all'}
            observation = {'schemaVersion': 1, 'repository': 'example/project', 'identity': {'login': 'reviewer'},
                'repositoryInfo': {'id': 1000, 'permissions': {'admin': True},
                                   'allowMergeMethods': {'squash': True, 'merge': True, 'rebase': True}},
                'pr': {'number': 1, 'url': 'https://github.com/example/project/pull/1', 'state': 'OPEN',
                       'isDraft': False, 'headRefName': 'pr/feature', 'headRefOid': head, 'baseRefName': 'main',
                       'baseRefOid': base, 'headRepository': 'example/project', 'author': 'author',
                       'commitAuthors': [{'sha': head, 'login': 'author'}], 'mergeable': 'MERGEABLE',
                       'mergeStateStatus': 'BLOCKED', 'checks': [{'name': 'ci', 'status': 'SUCCESS', 'conclusion': 'SUCCESS'}],
                       'review': {'decision': 'REVIEW_REQUIRED', 'changesRequested': False, 'threads': [], 'unresolvedThreads': []},
                       'mergeCommit': None},
                'rules': {'branchProtection': {'required_pull_request_reviews': {'required_approving_review_count': 1}},
                          'rulesets': []}}
            observation['acceptanceAuthority'] = {
                'schemaVersion': 1, 'kind': 'githubAcceptanceAuthority', 'host': 'github.com',
                'repository': {'id': 1000, 'full_name': 'example/project'},
                'branch': 'main', 'base_sha': base, 'root_tree_sha': git('rev-parse', base + '^{tree}'),
                'path': '.syncwheel/acceptance-policy.json', 'state': 'absent', 'blob_sha': None, 'content': None}
            adapter = bindir / 'syncwheel-github'
            adapter.write_text('#!/usr/bin/env python3\nimport json,os,sys\njson.load(sys.stdin)\nprint(json.dumps({"ok": True, "observation": json.loads(os.environ["OBSERVATION"])}))\n')
            adapter.chmod(0o755)
            env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ['PATH'])
            command = [sys.executable, str(CLI), 'stack', 'merge-pr', 'feature', '--json']
            for decision, bypasses, status, path in [('REVIEW_REQUIRED', [], 'blocked', 'normal'),
                    ('REVIEW_REQUIRED', ['required_reviews'], 'ready', 'admin-review-bypass'),
                    ('APPROVED', [], 'ready', 'normal')]:
                policy['allowed_bypasses'] = bypasses
                (control / 'profile.local.json').write_text(json.dumps({'github_pr_merge': policy}))
                observation['pr']['review']['decision'] = decision
                observation['pr']['mergeStateStatus'] = 'BLOCKED' if decision == 'REVIEW_REQUIRED' else 'CLEAN'
                env['OBSERVATION'] = json.dumps(observation)
                result = subprocess.run(command, cwd=repo, text=True, capture_output=True, env=env)
                plan = json.loads(result.stdout)
                self.assertEqual(plan['status'], status, plan)
                self.assertEqual(plan['path'], path, plan)
                if not bypasses and decision == 'REVIEW_REQUIRED':
                    self.assertIn('required_reviews_not_authorized', [b['code'] for b in plan['blockers']])
            observation['pr']['review']['decision'] = 'REVIEW_REQUIRED'
            observation['pr']['mergeStateStatus'] = 'BLOCKED'
            observation['rules']['branchProtection'] = {}
            env['OBSERVATION'] = json.dumps(observation)
            result = subprocess.run(command, cwd=repo, text=True, capture_output=True, env=env)
            plan = json.loads(result.stdout)
            self.assertEqual(plan['status'], 'blocked', plan)
            self.assertIn('review_rule_unproven', [b['code'] for b in plan['blockers']])


if __name__ == '__main__':
    unittest.main()
