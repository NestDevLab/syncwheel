import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / 'scripts' / 'syncwheel.py'
FIXTURE = REPO_ROOT / 'tests' / 'fixtures' / 'simple-repo'


class SyncwheelFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='syncwheel-test-'))
        self.repo = self.tmp / 'repo'
        self.registry = self.tmp / 'repos.json'
        shutil.copytree(FIXTURE, self.repo)
        self.init_fixture_repo()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args, expected=0):
        env = dict(**os.environ)
        env['SYNCWHEEL_REPO_REGISTRY'] = str(self.registry)
        result = subprocess.run(
            ['python3', str(CLI), *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            env=env,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"expected exit {expected}, got {result.returncode}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
        return result

    def git(self, *args):
        result = subprocess.run(
            ['git', *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"git command failed: {args}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        return result.stdout.strip()

    def read_manifest(self):
        return json.loads((self.repo / '.syncwheel' / 'manifest.json').read_text())

    def init_fixture_repo(self):
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Syncwheel Fixture')
        self.git('config', 'user.email', 'syncwheel@example.com')
        self.git('add', 'alpha.txt')
        self.git('commit', '-q', '-m', 'feat: add alpha')
        alpha_sha = self.git('rev-parse', '--short=7', 'HEAD')
        self.git('add', 'beta.txt')
        self.git('commit', '-q', '-m', 'feat: add beta')
        beta_sha = self.git('rev-parse', '--short=7', 'HEAD')
        self.git('branch', 'pr/feature-a', 'HEAD~1')
        self.git('branch', 'pr/feature-b', 'HEAD')
        manifest = {
            'version': 1,
            'defaults': {
                'canonical_remote': 'origin',
                'publication_remote': 'fork',
                'base_branch': 'main',
                'base_ref': 'main',
            },
            'integration': {
                'branch': 'main',
                'base': 'main',
                'stacks': ['feature-a', 'feature-b'],
            },
            'stacks': [
                {
                    'id': 'feature-a',
                    'branch': 'pr/feature-a',
                    'base': 'main',
                    'target_remote': 'origin',
                    'target_branch': 'main',
                    'integration_branch': 'main',
                    'commits': [alpha_sha],
                },
                {
                    'id': 'feature-b',
                    'branch': 'pr/feature-b',
                    'base': 'main',
                    'target_remote': 'origin',
                    'target_branch': 'main',
                    'integration_branch': 'main',
                    'commits': [alpha_sha, beta_sha],
                },
            ],
        }
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

    def test_validate_passes_for_fixture(self):
        result = self.run_cli('validate', expected=0)
        self.assertIn('OK', result.stdout)

    def test_plan_reports_no_actions_when_fixture_is_aligned(self):
        result = self.run_cli('plan', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertEqual(data, [])

    def test_status_json_reports_manifest_present(self):
        result = self.run_cli('status', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertTrue(data['manifest_present'])
        self.assertIn('validation', data)
        self.assertEqual(data['validation']['errors'], [])

    def test_stack_rebuild_worktree_commands_are_emitted(self):
        worktree = self.tmp / 'wt-feature-a'
        result = self.run_cli('stack', 'rebuild', 'feature-a', '--worktree', str(worktree), '--dry-run', expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/pr/feature-a-before-syncwheel-', result.stdout)
        self.assertIn('git worktree add -B pr/feature-a', result.stdout)
        self.assertIn('git -C', result.stdout)

    def test_stack_rebuild_in_place_commands_are_emitted(self):
        result = self.run_cli('stack', 'rebuild', 'feature-a', '--in-place', '--dry-run', expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/pr/feature-a-before-syncwheel-', result.stdout)
        self.assertIn('git reset --hard main', result.stdout)
        self.assertIn('git cherry-pick', result.stdout)

    def test_int_rebuild_merge_stack_commands_are_emitted(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['strategy'] = 'merge-stacks'
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        worktree = self.tmp / 'wt-integration'
        result = self.run_cli('int', 'rebuild', '--worktree', str(worktree), '--dry-run', expected=0)

        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/main-before-syncwheel-', result.stdout)
        self.assertIn('git worktree add -B main', result.stdout)
        self.assertIn("git -C", result.stdout)
        self.assertIn("merge --no-ff pr/feature-a -m 'Merge stack '", result.stdout)
        self.assertIn("merge --no-ff pr/feature-b -m 'Merge stack '", result.stdout)

    def test_int_rebuild_in_place_commands_are_emitted(self):
        result = self.run_cli('int', 'rebuild', '--in-place', '--dry-run', expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/main-before-syncwheel-', result.stdout)
        self.assertIn('git reset --hard main', result.stdout)
        self.assertIn('git cherry-pick', result.stdout)

    def test_int_rebuild_skips_empty_cherry_pick(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('int', 'rebuild', '--in-place', '--dry-run', expected=0)

        self.assertIn('git reset --hard main', result.stdout)
        self.assertNotIn('git cherry-pick', result.stdout)

    def test_in_place_apply_requires_current_target_branch(self):
        result = self.run_cli('stack', 'rebuild', 'feature-a', '--in-place', expected=2)
        self.assertIn('requires current branch', result.stderr)

    def test_stack_sync_updates_manifest_from_branch(self):
        self.git('switch', '-q', 'pr/feature-a')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')

        result = self.run_cli('stack', 'sync', 'feature-a', expected=0)
        self.assertIn('synced 1 commits', result.stdout)
        manifest = self.read_manifest()
        feature_a = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-a')
        self.assertEqual(len(feature_a['commits']), 1)

    def test_stack_set_and_add_update_manifest(self):
        beta = self.git('rev-parse', 'HEAD')
        self.run_cli('stack', 'set', 'feature-a', beta, expected=0)
        manifest = self.read_manifest()
        feature_a = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-a')
        self.assertEqual(feature_a['commits'], [beta])

        alpha = self.git('rev-parse', 'HEAD~1')
        self.run_cli('stack', 'add', 'feature-a', alpha, expected=0)
        manifest = self.read_manifest()
        feature_a = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-a')
        self.assertEqual(feature_a['commits'], [beta, alpha])

    def test_stack_push_is_emitted_with_passthrough_args(self):
        result = self.run_cli('stack', 'push', 'feature-a', '--dry-run', '--', '--force-with-lease', expected=0)
        self.assertIn('git push --force-with-lease fork pr/feature-a', result.stdout)

    def test_int_push_is_emitted_with_passthrough_args(self):
        result = self.run_cli('int', 'push', '--dry-run', '--', '--force-with-lease', expected=0)
        self.assertIn('git push --force-with-lease fork main', result.stdout)

    def test_stack_git_runs_in_stack_worktree(self):
        self.git('worktree', 'add', '-q', str(self.tmp / 'wt-feature-a'), 'pr/feature-a')
        result = self.run_cli('stack', 'git', 'feature-a', '--', 'branch', '--show-current', expected=0)
        self.assertEqual(result.stdout.strip(), 'pr/feature-a')

    def test_int_git_runs_in_integration_worktree(self):
        result = self.run_cli('int', 'git', '--', 'branch', '--show-current', expected=0)
        self.assertEqual(result.stdout.strip(), 'main')

    def test_stack_git_can_create_explicit_worktree(self):
        worktree = self.tmp / 'wt-feature-a'
        result = self.run_cli(
            'stack',
            'git',
            'feature-a',
            '--worktree',
            str(worktree),
            '--',
            'branch',
            '--show-current',
            expected=0,
        )

        self.assertEqual(result.stdout.strip(), 'pr/feature-a')
        self.assertTrue(worktree.exists())

    def test_int_git_can_create_explicit_worktree(self):
        self.git('branch', 'integration/test', 'main')
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        worktree = self.tmp / 'wt-integration'

        result = self.run_cli(
            'int',
            'git',
            '--worktree',
            str(worktree),
            '--',
            'branch',
            '--show-current',
            expected=0,
        )

        self.assertEqual(result.stdout.strip(), 'integration/test')
        self.assertTrue(worktree.exists())

    def test_validate_warns_for_unmapped_integration_commits(self):
        self.git('branch', 'integration/test', 'main')
        self.git('switch', '-q', 'integration/test')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('validate', '--json', expected=0)
        validation = json.loads(result.stdout)

        self.assertIn('not declared in any stack', '\n'.join(validation['warnings']))
        self.assertEqual(len(validation['details']['integration']['unmapped_commits']), 1)

    def test_plan_reports_unmapped_integration_commits(self):
        self.git('branch', 'integration/test', 'main')
        self.git('switch', '-q', 'integration/test')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('plan', '--json', expected=0)
        plan = json.loads(result.stdout)

        self.assertEqual(plan[-1]['type'], 'classify_integration_commits')
        self.assertEqual(len(plan[-1]['commits']), 1)

    def test_validate_fails_for_unknown_integration_strategy(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['strategy'] = 'octopus'
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        result = self.run_cli('validate', expected=1)
        self.assertIn('integration strategy must be one of', result.stdout + result.stderr)

    def test_validate_fails_when_commit_is_missing(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['stacks'][0]['commits'].append('deadbeef')
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        result = self.run_cli('validate', expected=1)
        self.assertIn('missing commit', result.stdout + result.stderr)

    def test_repo_alias_can_be_used_with_short_repo_flag(self):
        self.run_cli('repo', 'add', 'fixture', str(self.repo), expected=0)
        result = self.run_cli('status', '-r', 'fixture', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertTrue(data['manifest_present'])

    def test_short_repo_flag_accepts_direct_path(self):
        result = self.run_cli('status', '-r', str(self.repo), '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertTrue(data['manifest_present'])

    def test_repo_alias_can_store_default_manifest_path(self):
        custom_manifest = self.tmp / 'custom-manifest.json'
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        custom_manifest.write_text(manifest.read_text())

        self.run_cli('repo', 'add', 'fixture2', str(self.repo), '--manifest', str(custom_manifest), expected=0)
        result = self.run_cli('status', '-r', 'fixture2', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertEqual(data['manifest_path'], str(custom_manifest))


if __name__ == '__main__':
    unittest.main()
