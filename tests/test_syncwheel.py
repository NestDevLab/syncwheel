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

    def run_cli(self, *args, expected=0, extra_env=None, cwd=None):
        env = dict(**os.environ)
        env['SYNCWHEEL_REPO_REGISTRY'] = str(self.registry)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            ['python3', str(CLI), *args],
            cwd=cwd or self.repo,
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

    def run_custom_cli(self, cli_path, *args, expected=0, extra_env=None, cwd=None):
        env = dict(**os.environ)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            ['python3', str(cli_path), *args],
            cwd=cwd,
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

    def run_script(self, script_path, *args, expected=0, cwd=None):
        result = subprocess.run(
            ['python3', str(script_path), *args],
            cwd=cwd or self.repo,
            text=True,
            capture_output=True,
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

    def init_syncwheel_install_fixture(self):
        seed = self.tmp / 'syncwheel-seed'
        origin = self.tmp / 'syncwheel-origin.git'
        install = self.tmp / 'syncwheel-install'
        (seed / 'scripts').mkdir(parents=True)
        (seed / 'githooks').mkdir(parents=True)
        shutil.copy2(CLI, seed / 'scripts' / 'syncwheel.py')
        shutil.copy2(CLI.parent / 'check-version-bump.py', seed / 'scripts' / 'check-version-bump.py')
        shutil.copy2(REPO_ROOT / 'githooks' / 'pre-commit', seed / 'githooks' / 'pre-commit')
        (seed / 'VERSION').write_text('0.6.0\n')
        (seed / 'README.md').write_text('syncwheel fixture\n')

        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=seed, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Syncwheel Fixture'], cwd=seed, check=True)
        subprocess.run(['git', 'config', 'user.email', 'syncwheel@example.com'], cwd=seed, check=True)
        subprocess.run(['git', 'add', '.'], cwd=seed, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', 'syncwheel 0.6.0'], cwd=seed, check=True)
        subprocess.run(['git', 'clone', '--bare', str(seed), str(origin)], check=True)
        subprocess.run(['git', 'remote', 'add', 'origin', str(origin)], cwd=seed, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', 'main'], cwd=seed, check=True)
        subprocess.run(['git', 'clone', str(origin), str(install)], check=True)

        (seed / 'VERSION').write_text('0.7.0\n')
        subprocess.run(['git', 'add', 'VERSION'], cwd=seed, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', 'syncwheel 0.7.0'], cwd=seed, check=True)
        subprocess.run(['git', 'push', 'origin', 'main'], cwd=seed, check=True)

        return {
            'seed': seed,
            'origin': origin,
            'install': install,
            'cli': install / 'scripts' / 'syncwheel.py',
            'state': self.tmp / 'syncwheel-update-state.json',
            'settings': self.tmp / 'syncwheel-settings.json',
            'registry': self.tmp / 'syncwheel-registry.json',
        }

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

    def test_check_json_reports_validation_and_plan(self):
        result = self.run_cli('check', '--no-fetch', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assertIn('validation', data)
        self.assertEqual(data['validation']['errors'], [])
        self.assertEqual(data['plan'], [])

    def test_env_repo_allows_running_outside_target_repo(self):
        result = self.run_cli(
            'ck',
            '--no-fetch',
            '--json',
            expected=0,
            extra_env={'SYNCWHEEL_REPO': str(self.repo)},
            cwd=self.tmp,
        )
        data = json.loads(result.stdout)

        self.assertEqual(data['snapshot']['repo_root'], str(self.repo))

    def test_init_personal_creates_local_manifest_path(self):
        personal_manifest = self.repo / '.syncwheel' / 'manifests' / 'alice.local.json'

        result = self.run_cli('init', '--personal', 'alice', '--force', expected=0)

        self.assertEqual(result.stdout.strip(), str(personal_manifest))
        data = json.loads(personal_manifest.read_text())
        self.assertEqual(data['integration']['branch'], 'integration/alice/main')
        self.assertEqual(data['stacks'], [])

    def test_init_defaults_to_main_integration(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        manifest.unlink()

        self.run_cli('init', expected=0)
        data = self.read_manifest()

        self.assertEqual(data['integration']['branch'], 'main-integration')

    def test_personal_flag_selects_local_manifest_for_commands(self):
        self.run_cli('init', '--personal', 'alice', '--force', expected=0)
        gamma = self.git('rev-parse', 'HEAD')

        self.run_cli(
            'stack',
            'create',
            '-p',
            'alice',
            'feature-c',
            gamma,
            '--branch',
            'pr/alice/feature-c',
            '--include-in-integration',
            expected=0,
        )
        self.run_cli('s', 'set', '-p', 'alice', 'feature-c', 'HEAD~1..HEAD', expected=0)
        result = self.run_cli('st', '-p', 'alice', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assertEqual(
            data['manifest_path'],
            str(self.repo / '.syncwheel' / 'manifests' / 'alice.local.json'),
        )
        self.assertEqual(data['validation']['details']['stacks'][0]['id'], 'feature-c')

    def test_use_sets_repo_local_default_personal_manifest(self):
        self.run_cli('init', '--personal', 'alice', '--force', expected=0)
        self.run_cli('use', 'alice', expected=0)

        result = self.run_cli('check', '--no-fetch', '--json', expected=1)
        data = json.loads(result.stdout)

        self.assertEqual(
            data['manifest_path'],
            str(self.repo / '.syncwheel' / 'manifests' / 'alice.local.json'),
        )

    def test_use_shared_clears_repo_local_profile(self):
        self.run_cli('init', '--personal', 'alice', '--force', expected=0)
        self.run_cli('use', 'alice', expected=0)
        self.run_cli('use', '--shared', expected=0)

        result = self.run_cli('check', '--no-fetch', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assertEqual(data['manifest_path'], str(self.repo / '.syncwheel' / 'manifest.json'))

    def test_stack_create_adds_stack_without_hand_editing_manifest(self):
        gamma = self.git('rev-parse', 'HEAD')

        result = self.run_cli(
            's',
            'new',
            'feature-c',
            gamma,
            '--branch',
            'pr/alice/feature-c',
            '--purpose',
            'Exercise stack creation',
            '--include-in-integration',
            expected=0,
        )

        self.assertIn('feature-c: created pr/alice/feature-c with 1 commits', result.stdout)
        manifest = self.read_manifest()
        feature_c = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-c')
        self.assertEqual(feature_c['branch'], 'pr/alice/feature-c')
        self.assertEqual(feature_c['commits'], [gamma])
        self.assertEqual(feature_c['meta']['purpose'], 'Exercise stack creation')
        self.assertIn('feature-c', manifest['integration']['stacks'])

    def test_stack_rebuild_worktree_commands_are_emitted(self):
        worktree = self.tmp / 'wt-feature-a'
        result = self.run_cli('stack', 'rebuild', 'feature-a', '--worktree', str(worktree), '--dry-run', expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/pr/feature-a-before-syncwheel-', result.stdout)
        self.assertIn('git worktree add -B pr/feature-a', result.stdout)
        self.assertIn('git -C', result.stdout)

    def test_stack_rebuild_reuses_existing_stack_worktree(self):
        worktree = self.tmp / 'wt-feature-a'
        self.git('worktree', 'add', str(worktree), 'pr/feature-a')

        result = self.run_cli('stack', 'rebuild', 'feature-a', '--dry-run', expected=0)

        self.assertIn(f'git -C {worktree} reset --hard main', result.stdout)
        self.assertIn(f'git -C {worktree} cherry-pick', result.stdout)
        self.assertNotIn('git worktree add -B pr/feature-a', result.stdout)

    def test_stack_rebuild_in_place_commands_are_emitted(self):
        result = self.run_cli('stack', 'rebuild', 'feature-a', '--in-place', '--dry-run', expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/pr/feature-a-before-syncwheel-', result.stdout)
        self.assertIn('git reset --hard main', result.stdout)
        self.assertIn('git cherry-pick', result.stdout)

    def test_int_rebuild_merge_stack_commands_are_emitted(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        self.git('branch', 'integration/test', 'main')
        data['integration']['branch'] = 'integration/test'
        data['integration']['strategy'] = 'merge-stacks'
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        worktree = self.tmp / 'wt-integration'
        result = self.run_cli('int', 'rebuild', '--worktree', str(worktree), '--dry-run', expected=0)

        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/integration/test-before-syncwheel-', result.stdout)
        self.assertIn('git worktree add -B integration/test', result.stdout)
        self.assertIn("git -C", result.stdout)
        self.assertIn("merge --no-ff pr/feature-a -m 'Merge stack '", result.stdout)
        self.assertIn("merge --no-ff pr/feature-b -m 'Merge stack '", result.stdout)

    def test_int_rebuild_in_place_commands_are_emitted(self):
        result = self.run_cli('int', 'rebuild', '--in-place', '--dry-run', expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/main-before-syncwheel-', result.stdout)
        self.assertIn('git reset --hard main', result.stdout)
        self.assertIn('git cherry-pick', result.stdout)

    def test_int_rebuild_reuses_existing_integration_worktree(self):
        self.git('branch', 'integration/test', 'main')
        worktree = self.tmp / 'wt-integration'
        self.git('worktree', 'add', str(worktree), 'integration/test')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('int', 'rebuild', '--dry-run', expected=0)

        self.assertIn(f'git -C {worktree} reset --hard main', result.stdout)
        self.assertIn(f'git -C {worktree} cherry-pick', result.stdout)
        self.assertNotIn('git worktree add -B integration/test', result.stdout)

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

    def test_stack_push_has_explicit_force_with_lease_flag(self):
        result = self.run_cli('stack', 'push', 'feature-a', '--dry-run', '--force-with-lease', expected=0)
        self.assertIn('git push --force-with-lease fork pr/feature-a', result.stdout)

    def test_int_push_is_emitted_with_passthrough_args(self):
        result = self.run_cli('int', 'push', '--dry-run', '--', '--force-with-lease', expected=0)
        self.assertIn('git push --force-with-lease fork main', result.stdout)

    def test_reconcile_push_uses_force_with_lease_by_default(self):
        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'fork', str(origin))
        self.git('branch', 'pr/publish', 'main')
        manifest = self.read_manifest()
        manifest['stacks'].append({
            'id': 'publish',
            'branch': 'pr/publish',
            'base': 'main',
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'main',
            'commits': [],
        })
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli(
            'reconcile',
            '--no-fetch',
            '--apply',
            '--push',
            '--stack',
            'publish',
            '--skip-integration',
            expected=0,
        )

        self.assertIn('git push --force-with-lease fork pr/publish', result.stdout)

    def test_reconcile_push_can_disable_default_force_with_lease(self):
        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'fork', str(origin))
        self.git('branch', 'pr/publish', 'main')
        manifest = self.read_manifest()
        manifest['stacks'].append({
            'id': 'publish',
            'branch': 'pr/publish',
            'base': 'main',
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'main',
            'commits': [],
        })
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli(
            'reconcile',
            '--no-fetch',
            '--apply',
            '--push',
            '--no-force-with-lease',
            '--stack',
            'publish',
            '--skip-integration',
            expected=0,
        )

        self.assertIn('git push fork pr/publish', result.stdout)
        self.assertNotIn('--force-with-lease', result.stdout)

    def test_reconcile_reports_stack_and_integration_rebuild_plan(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        self.git('branch', 'integration/reconcile', base)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        self.git('switch', '-q', 'main')

        manifest_path = self.tmp / 'reconcile-manifest.json'
        data = self.read_manifest()
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--json',
            expected=0,
        )
        report = json.loads(result.stdout)

        self.assertEqual(
            [action['type'] for action in report['actions']],
            ['rebuild_stack', 'rebuild_integration'],
        )
        self.assertEqual(report['actions'][0]['reason'], 'local_branch_differs_from_manifest_projection')
        self.assertIn('working_tree_status', report['snapshot'])

    def test_reconcile_reports_dirty_working_tree_status(self):
        Path(self.repo / 'dirty.txt').write_text('dirty\n')

        result = self.run_cli('reconcile', '--no-fetch', expected=0)

        self.assertIn('working tree:', result.stdout)
        self.assertIn('?? dirty.txt', result.stdout)

        result = self.run_cli('reconcile', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        self.assertTrue(report['snapshot']['working_tree_dirty'])
        self.assertIn('?? dirty.txt', report['snapshot']['working_tree_status'])

    def test_reconcile_apply_rebuilds_stack_updates_manifest_and_rebuilds_integration(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        self.git('branch', 'integration/reconcile', base)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        self.git('switch', '-q', 'main')

        manifest_path = self.tmp / 'reconcile-manifest.json'
        data = self.read_manifest()
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--apply',
            '--worktree-root',
            str(self.tmp / 'worktrees'),
            expected=0,
        )
        updated = json.loads(manifest_path.read_text())
        updated_commit = updated['stacks'][0]['commits'][0]

        self.assertNotEqual(updated_commit, beta)
        self.assertEqual(self.git('rev-list', '--count', f'{base}..pr/feature-b'), '1')
        self.assertEqual(self.git('rev-parse', 'pr/feature-b:beta.txt'), self.git('rev-parse', f'{updated_commit}:beta.txt'))
        self.assertEqual(self.git('rev-list', '--count', f'{base}..integration/reconcile'), '2')

    def test_reconcile_aligns_local_to_remote_when_remote_matches_projection(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        manifest_path = self.tmp / 'align-manifest.json'
        data = self.read_manifest()
        data['defaults']['publication_remote'] = 'origin'
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        before_manifest = manifest_path.read_text()

        self.git('branch', 'integration/reconcile', base)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")

        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'origin', str(origin))
        self.git('fetch', 'origin', '--prune')

        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: local stale stack commit')
        stale_stack = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', 'integration/reconcile')
        self.git('clean', '-fd')
        Path(self.repo / 'integration-only.txt').write_text('local only\n')
        self.git('add', 'integration-only.txt')
        self.git('commit', '-q', '-m', 'debug: local integration only')

        result = self.run_cli('reconcile', '--manifest', str(manifest_path), '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        self.assertEqual(
            [action['type'] for action in report['actions']],
            ['align_stack_to_remote', 'align_integration_to_remote'],
        )

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--apply',
            '--push',
            expected=0,
        )

        self.assertIn('align_stack_to_remote', result.stdout)
        self.assertIn('align_integration_to_remote', result.stdout)
        self.assertNotIn('git push', result.stdout)
        self.assertNotEqual(self.git('rev-parse', 'pr/feature-b'), stale_stack)
        self.assertEqual(self.git('rev-parse', 'pr/feature-b'), self.git('rev-parse', 'origin/pr/feature-b'))
        self.assertEqual(
            self.git('rev-parse', 'integration/reconcile'),
            self.git('rev-parse', 'origin/integration/reconcile'),
        )
        self.assertEqual(manifest_path.read_text(), before_manifest)

    def test_reconcile_noops_when_rewritten_history_matches_projection(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        manifest_path = self.tmp / 'rewritten-manifest.json'
        data = self.read_manifest()
        data['defaults']['publication_remote'] = 'origin'
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        self.git('switch', '-q', '-c', 'rewritten-feature-b', base)
        self.git('cherry-pick', beta)
        self.git('commit', '--amend', '-m', 'feat: add beta rewritten')
        self.git('branch', '-f', 'pr/feature-b', 'HEAD')
        self.git('switch', '-q', '-c', 'integration/reconcile', base)
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")

        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'origin', str(origin))
        self.git('fetch', 'origin', '--prune')

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--json',
            expected=0,
        )
        report = json.loads(result.stdout)

        self.assertEqual(report['actions'], [])
        self.assertTrue(report['stacks'][0]['local_matches_projection'])
        self.assertTrue(report['stacks'][0]['remote_matches_projection'])
        self.assertEqual(report['stacks'][0]['relation'], 'aligned')
        self.assertEqual(
            self.git('rev-parse', 'pr/feature-b'),
            self.git('rev-parse', 'origin/pr/feature-b'),
        )
        self.assertNotEqual(self.git('rev-parse', 'pr/feature-b'), beta)

    def test_reconcile_can_align_diverged_matching_projection_history(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        manifest_path = self.tmp / 'diverged-matching-manifest.json'
        data = self.read_manifest()
        data['defaults']['publication_remote'] = 'origin'
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        before_manifest = manifest_path.read_text()

        self.git('switch', '-q', '-c', 'remote-feature-b', base)
        self.git('cherry-pick', beta)
        self.git('commit', '--amend', '-m', 'feat: add beta remote rewrite')
        remote_stack = self.git('rev-parse', 'HEAD')
        self.git('branch', '-f', 'pr/feature-b', remote_stack)
        self.git('switch', '-q', '-c', 'integration/reconcile', base)
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        remote_integration = self.git('rev-parse', 'HEAD')

        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'origin', str(origin))
        self.git('fetch', 'origin', '--prune')

        self.git('switch', '-q', 'remote-feature-b')
        self.git('reset', '--hard', base)
        self.git('cherry-pick', beta)
        self.git('commit', '--amend', '-m', 'feat: add beta local rewrite')
        local_stack = self.git('rev-parse', 'HEAD')
        self.git('branch', '-f', 'pr/feature-b', local_stack)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('reset', '--hard', base)
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        local_integration = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        self.git('clean', '-fd')

        self.assertNotEqual(local_stack, remote_stack)
        self.assertNotEqual(local_integration, remote_integration)

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--json',
            expected=0,
        )
        report = json.loads(result.stdout)
        self.assertEqual(report['actions'], [])
        self.assertEqual(report['stacks'][0]['relation'], 'diverged')
        self.assertEqual(report['integration']['relation'], 'diverged')
        self.assertTrue(report['stacks'][0]['local_matches_projection'])
        self.assertTrue(report['stacks'][0]['remote_matches_projection'])
        self.assertTrue(report['integration']['local_matches_projection'])
        self.assertTrue(report['integration']['remote_matches_projection'])

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--align-local-to-remote',
            '--json',
            expected=0,
        )
        report = json.loads(result.stdout)
        self.assertEqual(
            [action['type'] for action in report['actions']],
            ['align_stack_to_remote', 'align_integration_to_remote'],
        )
        self.assertEqual(report['actions'][0]['reason'], 'local_and_remote_match_projection')
        self.assertEqual(report['actions'][1]['reason'], 'local_and_remote_match_projection')

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--align-local-to-remote',
            '--apply',
            expected=0,
        )
        self.assertIn('align_stack_to_remote', result.stdout)
        self.assertIn('align_integration_to_remote', result.stdout)
        self.assertEqual(self.git('rev-parse', 'pr/feature-b'), remote_stack)
        self.assertEqual(self.git('rev-parse', 'integration/reconcile'), remote_integration)
        self.assertEqual(manifest_path.read_text(), before_manifest)

    def test_version_bump_guard_fails_for_cli_change_without_version_files(self):
        base = self.git('rev-parse', 'HEAD')
        script = self.repo / 'scripts' / 'demo.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('print("demo")\n')
        self.git('add', 'scripts/demo.py')
        self.git('commit', '-q', '-m', 'feat: add demo script')

        result = self.run_script(
            CLI.parent / 'check-version-bump.py',
            '--base',
            base,
            expected=1,
        )

        self.assertIn('Release-relevant changes require a version bump', result.stdout)
        self.assertIn('VERSION', result.stdout)
        self.assertIn('CHANGELOG.md', result.stdout)
        self.assertIn('README.md', result.stdout)

    def test_version_bump_guard_passes_with_version_and_changelog(self):
        base = self.git('rev-parse', 'HEAD')
        script = self.repo / 'scripts' / 'demo.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('print("demo")\n')
        (self.repo / 'VERSION').write_text('9.9.9\n')
        (self.repo / 'CHANGELOG.md').write_text('# Changelog\n\n## 9.9.9\n\n- Demo.\n')
        (self.repo / 'README.md').write_text('Current version: `9.9.9`\n')
        self.git('add', 'scripts/demo.py', 'VERSION', 'CHANGELOG.md', 'README.md')
        self.git('commit', '-q', '-m', 'feat: add demo script')

        result = self.run_script(
            CLI.parent / 'check-version-bump.py',
            '--base',
            base,
            expected=0,
        )

        self.assertIn('Version bump check passed', result.stdout)

    def test_version_bump_guard_checks_staged_files_for_hooks(self):
        script = self.repo / 'scripts' / 'demo.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('print("demo")\n')
        self.git('add', 'scripts/demo.py')

        result = self.run_script(
            CLI.parent / 'check-version-bump.py',
            '--staged',
            expected=1,
        )

        self.assertIn('Release-relevant changes require a version bump', result.stdout)
        self.assertIn('VERSION', result.stdout)

    def test_pre_commit_hook_runs_version_bump_guard(self):
        hook = REPO_ROOT / 'githooks' / 'pre-commit'
        script = self.repo / 'scripts' / 'demo.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('print("demo")\n')
        shutil.copy2(CLI.parent / 'check-version-bump.py', self.repo / 'scripts' / 'check-version-bump.py')
        self.git('add', 'scripts/demo.py')

        result = subprocess.run(
            [str(hook)],
            cwd=self.repo,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn('Release-relevant changes require a version bump', result.stdout)

    def test_int_sync_status_and_align_remote_with_local_git_remote(self):
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: add manifest')
        self.git('switch', '-q', '-c', 'pr/feature-c', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')

        manifest_path = self.tmp / 'integration-manifest.json'
        data = json.loads((self.repo / '.syncwheel' / 'manifest.json').read_text())
        data['defaults']['publication_remote'] = 'origin'
        data['integration'] = {
            'branch': 'integration/shared',
            'base': 'main',
            'strategy': 'merge-stacks',
            'stacks': ['feature-c'],
        }
        data['stacks'] = [
            {
                'id': 'feature-c',
                'branch': 'pr/feature-c',
                'base': 'main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/shared',
                'commits': [gamma],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        self.git('switch', '-q', '-c', 'integration/shared', 'main')
        self.git('merge', '--no-ff', 'pr/feature-c', '-m', "Merge stack 'feature-c' into integration/shared")

        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'origin', str(origin))
        self.git('push', '-u', 'origin', 'main', 'pr/feature-c', 'integration/shared')

        Path(self.repo / 'local-only.txt').write_text('local only\n')
        self.git('add', 'local-only.txt')
        self.git('commit', '-q', '-m', 'debug: local integration only')

        result = self.run_cli(
            'int',
            'sync-status',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--json',
            expected=0,
        )
        status = json.loads(result.stdout)

        self.assertEqual(status['sync']['relation'], 'local_ahead')
        self.assertEqual(status['sync']['ahead'], 1)
        self.assertTrue(status['sync']['remote_matches_projection'])
        self.assertFalse(status['sync']['local_matches_projection'])

        self.run_cli('int', 'align-remote', '--manifest', str(manifest_path), '--no-fetch', expected=0)

        self.assertFalse((self.repo / 'local-only.txt').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'origin/integration/shared'))
        backups = self.git('branch', '--list', 'backup/integration/shared-before-syncwheel-*')
        self.assertIn('backup/integration/shared-before-syncwheel-', backups)

    def test_manifest_compare_reports_shared_and_divergent_stacks(self):
        self.run_cli('init', '--personal', 'laptop', '--force', expected=0)
        shared_manifest = self.read_manifest()
        personal_path = self.repo / '.syncwheel' / 'manifests' / 'laptop.local.json'
        personal = json.loads(personal_path.read_text())
        personal['integration']['branch'] = 'integration/laptop/main'
        personal['integration']['stacks'] = ['feature-a', 'feature-c']
        personal['stacks'] = [
            dict(shared_manifest['stacks'][0]),
            {
                'id': 'feature-c',
                'branch': 'pr/laptop/feature-c',
                'base': 'main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/laptop/main',
                'commits': [self.git('rev-parse', 'HEAD')],
            },
        ]
        personal['stacks'][0]['commits'] = [self.git('rev-parse', 'HEAD')]
        personal_path.write_text(json.dumps(personal, indent=2) + '\n')

        result = self.run_cli('manifest', 'compare', '--other-personal', 'laptop', '--json', expected=0)
        comparison = json.loads(result.stdout)

        self.assertEqual(comparison['left_only'], ['feature-b'])
        self.assertEqual(comparison['right_only'], ['feature-c'])
        self.assertEqual([item['id'] for item in comparison['divergent_shared']], ['feature-a'])
        self.assertEqual(comparison['right_integration']['branch'], 'integration/laptop/main')

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

    def test_self_check_update_reports_newer_version_after_fetch(self):
        fixture = self.init_syncwheel_install_fixture()
        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'check-update',
            '--fetch',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        data = json.loads(result.stdout)
        self.assertTrue(data['update_available'])
        self.assertEqual(data['current_version'], '0.6.0')
        self.assertEqual(data['latest_version'], '0.7.0')

    def test_self_check_update_uses_origin_main_when_install_is_detached(self):
        fixture = self.init_syncwheel_install_fixture()
        subprocess.run(['git', 'checkout', '--detach', 'HEAD'], cwd=fixture['install'], check=True)

        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'check-update',
            '--fetch',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        data = json.loads(result.stdout)
        self.assertEqual(data['branch'], 'DETACHED')
        self.assertIsNone(data['upstream'])
        self.assertFalse(data['can_self_update'])
        self.assertTrue(data['update_available'])
        self.assertEqual(data['latest_version'], '0.7.0')
        self.assertIn('checking against origin/main', data['reason'])

    def test_self_check_update_falls_back_to_remote_head_when_origin_main_is_missing(self):
        fixture = self.init_syncwheel_install_fixture()
        subprocess.run(['git', 'checkout', '--detach', 'HEAD'], cwd=fixture['install'], check=True)
        subprocess.run(['git', 'branch', '-m', 'main', 'trunk'], cwd=fixture['seed'], check=True)
        subprocess.run(['git', 'push', 'origin', 'trunk'], cwd=fixture['seed'], check=True)
        subprocess.run(['git', '--git-dir', str(fixture['origin']), 'symbolic-ref', 'HEAD', 'refs/heads/trunk'], check=True)
        subprocess.run(['git', 'push', 'origin', '--delete', 'main'], cwd=fixture['seed'], check=True)
        subprocess.run(['git', 'update-ref', '-d', 'refs/remotes/origin/main'], cwd=fixture['install'], check=True)

        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'check-update',
            '--fetch',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        data = json.loads(result.stdout)
        self.assertEqual(data['branch'], 'DETACHED')
        self.assertTrue(data['update_available'])
        self.assertEqual(data['latest_version'], '0.7.0')
        self.assertIn('checking against origin/trunk', data['reason'])

    def test_self_update_fast_forwards_install(self):
        fixture = self.init_syncwheel_install_fixture()
        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'update',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        self.assertIn('updated syncwheel: 0.6.0 -> 0.7.0', result.stdout)
        self.assertEqual((fixture['install'] / 'VERSION').read_text().strip(), '0.7.0')

    def test_self_install_hooks_sets_core_hooks_path(self):
        fixture = self.init_syncwheel_install_fixture()

        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'install-hooks',
            expected=0,
            cwd=fixture['install'],
        )

        configured = subprocess.run(
            ['git', 'config', '--get', 'core.hooksPath'],
            cwd=fixture['install'],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(configured, 'githooks')
        self.assertIn('pre_commit: active', result.stdout)

    def test_self_status_reports_hook_status(self):
        fixture = self.init_syncwheel_install_fixture()
        subprocess.run(['git', 'config', 'core.hooksPath', 'githooks'], cwd=fixture['install'], check=True)

        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'status',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        data = json.loads(result.stdout)
        self.assertTrue(data['hooks']['active'])
        self.assertEqual(data['hooks']['configured_hooks_path'], 'githooks')

    def test_startup_notify_mode_emits_update_notice(self):
        fixture = self.init_syncwheel_install_fixture()
        result = self.run_custom_cli(
            fixture['cli'],
            'repo',
            'ls',
            expected=0,
            extra_env={
                'SYNCWHEEL_REPO_REGISTRY': str(fixture['registry']),
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
                'SYNCWHEEL_UPDATE_MODE': 'notify',
                'SYNCWHEEL_UPDATE_INTERVAL_SECONDS': '0',
            },
            cwd=fixture['install'],
        )
        self.assertIn('NOTICE: syncwheel update available (0.6.0 -> 0.7.0)', result.stderr)

    def test_startup_auto_mode_updates_before_normal_command(self):
        fixture = self.init_syncwheel_install_fixture()
        result = self.run_custom_cli(
            fixture['cli'],
            'repo',
            'ls',
            expected=0,
            extra_env={
                'SYNCWHEEL_REPO_REGISTRY': str(fixture['registry']),
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
                'SYNCWHEEL_UPDATE_MODE': 'auto',
                'SYNCWHEEL_UPDATE_INTERVAL_SECONDS': '0',
            },
            cwd=fixture['install'],
        )
        self.assertIn('syncwheel auto-updated 0.6.0 -> 0.7.0', result.stderr)
        self.assertEqual((fixture['install'] / 'VERSION').read_text().strip(), '0.7.0')


if __name__ == '__main__':
    unittest.main()
