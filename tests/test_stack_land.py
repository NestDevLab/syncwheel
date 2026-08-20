import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / 'scripts' / 'syncwheel.py'


class StackLandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='syncwheel-stack-land-test-'))
        self.origin = self.tmp / 'origin.git'
        self.seed = self.tmp / 'seed'
        self.repo = self.tmp / 'repo'
        subprocess.run(['git', 'init', '--bare', str(self.origin)], check=True, capture_output=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.seed)], check=True)
        self.git(self.seed, 'config', 'user.name', 'Fixture')
        self.git(self.seed, 'config', 'user.email', 'fixture@example.com')
        (self.seed / 'README.md').write_text('seed\n')
        self.git(self.seed, 'add', 'README.md')
        self.git(self.seed, 'commit', '-qm', 'seed')
        self.git(self.seed, 'remote', 'add', 'origin', str(self.origin))
        self.git(self.seed, 'push', '-u', 'origin', 'main')
        subprocess.run(
            ['git', '--git-dir', str(self.origin), 'symbolic-ref', 'HEAD', 'refs/heads/main'],
            check=True, capture_output=True,
        )
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(self.repo)], check=True)
        self.git(self.repo, 'config', 'user.name', 'Fixture')
        self.git(self.repo, 'config', 'user.email', 'fixture@example.com')
        self.registry = self.tmp / 'repos.json'

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def git(self, repo, *args):
        result = subprocess.run(['git', *args], cwd=repo, text=True, capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr or result.stdout)
        return result.stdout.strip()

    def cli(self, *args, expected=0):
        env = dict(os.environ, SYNCWHEEL_UPDATE_MODE='off', SYNCWHEEL_REPO_REGISTRY=str(self.registry))
        result = subprocess.run(
            ['python3', str(CLI), *args], cwd=self.repo, text=True, capture_output=True, env=env,
        )
        if result.returncode != expected:
            raise AssertionError(
                f'expected {expected}, got {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
            )
        return result

    def configure_stack(self, checks=None, landing=True):
        self.git(self.repo, 'switch', '-qc', 'syncwheel/draft/feature', 'origin/main')
        (self.repo / 'feature.txt').write_text('feature\n')
        self.git(self.repo, 'add', 'feature.txt')
        self.git(self.repo, 'commit', '-qm', 'feature')
        source = self.git(self.repo, 'rev-parse', 'HEAD')
        self.git(self.repo, 'switch', '-qc', 'main-integration', 'origin/main')
        self.git(self.repo, 'cherry-pick', source)
        integration = self.git(self.repo, 'rev-parse', 'HEAD')
        manifest = {
            'version': 1,
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
                'id': 'feature', 'branch': 'syncwheel/draft/feature', 'base': 'origin/main',
                'target_remote': 'origin', 'target_branch': 'main', 'integration_branch': 'main-integration',
                'commits': [source], 'integration_commits': [integration], 'state': 'draft',
            }],
        }
        if landing:
            manifest['landing'] = {'mode': 'direct', 'strategy': 'merge', 'checks': checks}
        path = self.repo / '.syncwheel' / 'manifest.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + '\n')
        with (self.repo / '.git' / 'info' / 'exclude').open('a') as handle:
            handle.write('.syncwheel/\n')
        return source

    def test_plan_then_exact_lease_fast_forward_and_idempotent_receipt(self):
        source = self.configure_stack({
            'id': 'quality', 'local': {
                'scope': 'stack', 'argv': ['python3', '-c', "from pathlib import Path; assert Path('feature.txt').exists()"],
            },
        })
        preview = json.loads(self.cli('stack', 'land', 'feature', '--operation-id', 'land-1').stdout)
        self.assertEqual(preview['status'], 'ready')
        self.assertEqual(preview['candidate']['kind'], 'fast-forward')
        applied = json.loads(self.cli(
            'stack', 'land', 'feature', '--operation-id', 'land-1',
            '--plan-digest', preview['planDigest'], '--apply',
        ).stdout)
        self.assertEqual(applied['status'], 'succeeded')
        remote_tip = subprocess.run(
            ['git', '--git-dir', str(self.origin), 'rev-parse', 'refs/heads/main'],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        self.assertEqual(remote_tip, source)
        repeated = json.loads(self.cli(
            'stack', 'land', 'feature', '--operation-id', 'land-1',
            '--plan-digest', preview['planDigest'], '--apply',
        ).stdout)
        self.assertEqual(repeated['status'], 'succeeded')

    def test_pr_requirement_returns_a_non_mutating_pr_route(self):
        self.configure_stack({'id': 'remote-ci', 'pr': {'checks': ['test']}})
        preview = json.loads(self.cli('stack', 'land', 'feature').stdout)
        self.assertEqual(preview['status'], 'requires-pr')
        self.assertIn('stack promote feature', preview['next'])

    def test_diverged_delivery_uses_a_deterministic_merge_commit(self):
        source = self.configure_stack()
        self.git(self.repo, 'switch', '-q', 'main')
        (self.repo / 'delivery.txt').write_text('delivery\n')
        self.git(self.repo, 'add', 'delivery.txt')
        self.git(self.repo, 'commit', '-qm', 'delivery')
        delivery = self.git(self.repo, 'rev-parse', 'HEAD')
        self.git(self.repo, 'push', 'origin', 'main')
        self.git(self.repo, 'switch', '-qC', 'main-integration', delivery)
        self.git(self.repo, 'cherry-pick', source)
        integration = self.git(self.repo, 'rev-parse', 'HEAD')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['integration']['base'] = delivery
        manifest['stacks'][0]['integration_commits'] = [integration]
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        preview = json.loads(self.cli('stack', 'land', 'feature', '--operation-id', 'merge-1').stdout)
        self.assertEqual(preview['candidate']['kind'], 'merge')
        self.cli(
            'stack', 'land', 'feature', '--operation-id', 'merge-1',
            '--plan-digest', preview['planDigest'], '--apply',
        )
        parents = subprocess.run(
            ['git', '--git-dir', str(self.origin), 'show', '-s', '--format=%P', 'refs/heads/main'],
            text=True, capture_output=True, check=True,
        ).stdout.split()
        self.assertEqual(parents, [delivery, source])

    def test_disabled_policy_requires_the_explicit_per_request_bypass(self):
        self.configure_stack(landing=False)
        refused = self.cli('stack', 'land', 'feature', expected=2)
        self.assertIn('direct landing is disabled', refused.stderr)
        preview = json.loads(self.cli('stack', 'land', 'feature', '--allow-direct').stdout)
        self.assertEqual(preview['status'], 'ready')

    def test_overrides_need_a_reason_and_are_recorded_in_the_plan(self):
        self.configure_stack({'id': 'broken', 'local': {'scope': 'stack', 'argv': ['false']}})
        refused = self.cli('stack', 'land', 'feature', '--override-requirement', 'broken', expected=2)
        self.assertIn('--override-reason', refused.stderr)
        preview = json.loads(self.cli(
            'stack', 'land', 'feature', '--override-requirement', 'broken',
            '--override-reason', 'authorized exception',
        ).stdout)
        self.assertEqual(preview['checks']['status'], 'overridden')
        self.assertEqual(preview['request']['overrideReason'], 'authorized exception')
