import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace


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

    def load_syncwheel_module(self):
        spec = importlib.util.spec_from_file_location('syncwheel_under_test', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

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

    def test_manifest_stack_state_defaults_to_published(self):
        module = self.load_syncwheel_module()

        manifest, _ = module.load_manifest(self.repo)

        self.assertEqual(manifest['stacks'][0]['state'], 'published')
        self.assertEqual(manifest['stacks'][0]['publication'], {'enabled': True})

    def test_requested_replay_mode_allows_reconcile_arguments_without_in_place(self):
        module = self.load_syncwheel_module()

        self.assertEqual(
            module.requested_replay_mode(SimpleNamespace(replay_mode='plumbing', worktree=None)),
            'plumbing',
        )

    def test_validate_manifest_rejects_an_unknown_stack_state(self):
        module = self.load_syncwheel_module()
        manifest, _ = module.load_manifest(self.repo)
        manifest['stacks'][0]['state'] = 'reviewing'

        validation = module.validate_manifest(self.repo, manifest)

        self.assertIn(
            'stack feature-a state must be one of: draft, published',
            validation['errors'],
        )

    def read_ledger_state(self):
        result = self.run_cli('ledger', 'show', '--json', expected=0)
        return json.loads(result.stdout)

    def repo_exclude_path(self):
        path = Path(self.git('rev-parse', '--git-path', 'info/exclude'))
        if not path.is_absolute():
            path = self.repo / path
        return path

    def expected_external_ledger_root(self, manifest_path):
        stem = manifest_path.stem
        if stem.endswith('-manifest'):
            trimmed = stem[:-len('-manifest')]
            stem = trimmed or stem
        return manifest_path.parent / f'{stem}-ledger'

    def assert_path_equal(self, left, right):
        self.assertEqual(Path(left).resolve(), Path(right).resolve())

    def tracked_status(self):
        return self.git('status', '--porcelain', '--untracked-files=no')

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

    def prepare_replay_stack(self):
        base = self.git('rev-parse', 'main~1')
        beta = self.git('rev-parse', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'main')
        data = self.read_manifest()
        data['stacks'] = [{
            'id': 'replay',
            'branch': 'pr/replay',
            'base': base,
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'main',
            'commits': [beta, gamma],
        }]
        data['integration']['stacks'] = ['replay']
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        return base, [beta, gamma]

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

    def test_replayed_patch_equivalent_commits_satisfy_stack_and_integration_projection(self):
        self.git('switch', '-q', '-c', 'source/original', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        original = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', 'main')
        Path(self.repo / 'delta.txt').write_text('delta\n')
        self.git('add', 'delta.txt')
        self.git('commit', '-q', '-m', 'feat: advance base')
        self.git('switch', '-q', '-c', 'pr/replayed')
        self.git('cherry-pick', original)
        replayed = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', '-c', 'integration/replayed', 'main')
        self.git('merge', '--no-ff', 'pr/replayed', '-m', "Merge stack 'replayed' into integration/replayed")

        data = self.read_manifest()
        data['integration'] = {
            'branch': 'integration/replayed',
            'base': 'main',
            'strategy': 'merge-stacks',
            'stacks': ['replayed'],
        }
        data['stacks'] = [{
            'id': 'replayed',
            'branch': 'pr/replayed',
            'base': 'main',
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'integration/replayed',
            'commits': [original],
        }]
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')

        module = self.load_syncwheel_module()
        manifest, _ = module.load_manifest(self.repo)
        self.assertEqual(module.commit_patch_id(self.repo, original), module.commit_patch_id(self.repo, replayed))
        validation = module.validate_manifest(self.repo, manifest)
        stack = validation['details']['stacks'][0]

        self.assertEqual(validation['errors'], [])
        self.assertEqual(stack['missing_from_branch'], [])
        self.assertEqual(stack['missing_from_integration'], [])
        self.assertEqual(stack['undeclared_branch_commits'], [])
        self.assertEqual(validation['details']['integration']['unmapped_commits'], [])
        self.assertEqual(module.build_plan(self.repo, manifest, validation), [])

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

    def test_check_strict_fails_when_branch_contains_undeclared_commits(self):
        self.git('switch', '-q', 'pr/feature-a')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add undeclared gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')

        result = self.run_cli('check', '--strict', '--no-fetch', '--json', expected=1)
        data = json.loads(result.stdout)
        stack = next(item for item in data['validation']['details']['stacks'] if item['id'] == 'feature-a')

        self.assertEqual(stack['undeclared_branch_commits'], [gamma])
        self.assertFalse(data['readiness']['ready'])
        self.assertIn('validation_warnings', data['readiness']['blockers'])

        result = self.run_cli('check', '--no-fetch', '--json', expected=0)
        self.assertFalse(json.loads(result.stdout)['readiness']['ready'])

    def test_check_strict_passes_only_when_validation_and_plan_are_clean(self):
        result = self.run_cli('check', '--strict', '--no-fetch', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assertTrue(data['readiness']['ready'])
        self.assertEqual(data['readiness']['blockers'], [])

    def test_check_strict_detects_undeclared_remote_only_stack_commits(self):
        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(origin)], check=True)
        self.git('remote', 'add', 'fork', str(origin))
        self.git('push', '-q', 'fork', 'main', 'pr/feature-a', 'pr/feature-b')

        self.git('switch', '-q', 'pr/feature-a')
        Path(self.repo / 'gamma.txt').write_text('remote gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add remote-only gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'fork', 'pr/feature-a')
        self.git('switch', '-q', 'main')
        self.git('branch', '-f', 'pr/feature-a', 'main~1')

        result = self.run_cli('check', '--strict', '--json', expected=1)
        data = json.loads(result.stdout)
        stack = next(item for item in data['validation']['details']['stacks'] if item['id'] == 'feature-a')

        self.assertEqual(stack['remote_relation'], 'local_behind')
        self.assertEqual(stack['undeclared_remote_commits'], [gamma])
        self.assertIn('validation_warnings', data['readiness']['blockers'])

    def test_primary_checkout_mismatch_blocks_read_only_handoff_commands(self):
        self.git('switch', '-c', 'feature/wrong-primary')

        for command in (
            ('status', '--json'),
            ('validate', '--json'),
            ('plan', '--json'),
            ('check', '--no-fetch', '--json'),
            ('handoff', '--no-fetch', '--json'),
        ):
            result = self.run_cli(*command, expected=1)
            data = json.loads(result.stdout)
            if isinstance(data, list):
                self.assertTrue(any(action['type'] == 'restore_primary_checkout' for action in data))
            else:
                validation = data.get('validation', data)
                self.assertTrue(any('primary worktree branch mismatch' in error for error in validation['errors']))

    def test_feature_worktree_is_allowed_when_primary_checkout_matches(self):
        feature = self.tmp / 'feature-worktree'
        self.git('worktree', 'add', '-q', '-b', 'feature/dedicated', str(feature), 'main')

        result = self.run_cli('status', '--json', cwd=feature)
        data = json.loads(result.stdout)

        self.assertEqual(data['snapshot']['current_branch'], 'feature/dedicated')
        self.assertEqual(data['snapshot']['primary_checkout']['branch'], 'main')
        self.assertTrue(data['snapshot']['primary_checkout']['compliant'])

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

    def test_personal_manifests_have_isolated_ledgers(self):
        module = self.load_syncwheel_module()
        alice = self.repo / '.syncwheel' / 'manifests' / 'alice.local.json'
        bob = self.repo / '.syncwheel' / 'manifests' / 'bob.local.json'

        self.assertEqual(
            module.ledger_root(self.repo, alice),
            self.repo / '.syncwheel' / 'manifests' / 'alice.local-ledger',
        )
        self.assertEqual(
            module.ledger_root(self.repo, bob),
            self.repo / '.syncwheel' / 'manifests' / 'bob.local-ledger',
        )
        self.assertNotEqual(
            module.ledger_root(self.repo, alice),
            module.ledger_root(self.repo, bob),
        )
        self.assertEqual(
            module.ledger_root(self.repo, self.repo / '.syncwheel' / 'manifest.json'),
            self.repo / '.syncwheel' / 'ledger',
        )

    def test_init_defaults_to_main_integration(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        manifest.unlink()

        self.run_cli('init', expected=0)
        data = self.read_manifest()

        self.assertEqual(data['integration']['branch'], 'main-integration')
        self.assertEqual(data['defaults']['integration_membership'], 'required')
        self.assertEqual(self.git('branch', '--show-current'), 'main-integration')

    def test_init_can_persist_syncwheel_tracking_policy(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        manifest.unlink()

        self.run_cli(
            'init',
            '--syncwheel-tracking',
            'git-tracked',
            '--no-coordination',
            '--worktree-root',
            'var/syncwheel',
            expected=0,
        )
        data = self.read_manifest()

        self.assertEqual(data['syncwheel_tracking'], 'git-tracked')
        self.assertEqual(data['version'], 2)
        self.assertEqual(data['coordination']['mode'], 'disabled')
        self.assertEqual(data['syncwheel_worktree_root'], 'var/syncwheel')

    def test_repo_tracking_status_reports_missing_policy(self):
        result = self.run_cli('repo', 'tracking', 'status', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assertIsNone(data['syncwheel_tracking'])
        self.assertFalse(data['syncwheel_tracking_present'])
        self.assertEqual(data['syncwheel_worktree_root'], '.syncwheel/wt')
        self.assertIn('syncwheel_tracking is not set', data['warnings'][0])

    def test_repo_tracking_set_git_tracked_stages_manifest_and_gitignore(self):
        result = self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', '--json', expected=0)
        data = json.loads(result.stdout)

        manifest = self.read_manifest()
        gitignore = (self.repo / '.gitignore').read_text()
        exclude = self.repo_exclude_path().read_text()
        tracked = self.git('ls-files', '.syncwheel/manifest.json', '.gitignore')

        self.assertEqual(manifest['syncwheel_tracking'], 'git-tracked')
        self.assertEqual(manifest['syncwheel_worktree_root'], '.syncwheel/wt')
        self.assertTrue(data['manifest_tracked'])
        self.assertIn('.syncwheel/manifest.json', tracked)
        self.assertIn('.gitignore', tracked)
        self.assertIn('# syncwheel managed metadata', gitignore)
        self.assertIn('.syncwheel/ledger/', gitignore)
        self.assertIn('.syncwheel/wt/', gitignore)
        self.assertNotIn('var/syncwheel/', gitignore)
        self.assertNotIn('.syncwheel/', exclude)

    def test_repo_tracking_set_git_tracked_cleans_legacy_worktree_root_from_managed_block(self):
        (self.repo / '.gitignore').write_text(
            '# syncwheel managed metadata\n'
            '.syncwheel/ledger/\n'
            '.syncwheel/profile.local.json\n'
            '.syncwheel/manifests/*.local.json\n'
            'var/syncwheel/\n'
        )

        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', '--json', expected=0)
        gitignore = (self.repo / '.gitignore').read_text()

        self.assertIn('.syncwheel/wt/', gitignore)
        self.assertNotIn('var/syncwheel/', gitignore)
        self.assertIn('# end syncwheel managed metadata', gitignore)

    def test_repo_tracking_set_local_only_uses_info_exclude_without_gitignore(self):
        result = self.run_cli('repo', 'tracking', 'set', 'local-only', '--apply', '--json', expected=0)
        data = json.loads(result.stdout)

        manifest = self.read_manifest()
        exclude = self.repo_exclude_path().read_text()

        self.assertEqual(manifest['syncwheel_tracking'], 'local-only')
        self.assertFalse(data['manifest_tracked'])
        self.assertFalse((self.repo / '.gitignore').exists())
        self.assertIn('# syncwheel local metadata', exclude)
        self.assertIn('.syncwheel/', exclude)
        self.assertNotIn('var/syncwheel/', exclude)

    def test_repo_tracking_migrates_git_tracked_to_local_only(self):
        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', expected=0)
        hook_path = self.tmp / 'hook-path-without-syncwheel'
        hook_path.mkdir()
        for executable in ('git', 'dirname'):
            target = shutil.which(executable)
            self.assertIsNotNone(target)
            (hook_path / executable).symlink_to(target)
        environment = os.environ.copy()
        environment['PATH'] = str(hook_path)
        self.assertIsNone(shutil.which('syncwheel', path=environment['PATH']))
        committed = subprocess.run(
            ['git', 'commit', '-q', '-m', 'test: track syncwheel manifest'],
            cwd=self.repo,
            env=environment,
            text=True,
            capture_output=True,
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)

        result = self.run_cli('repo', 'tracking', 'set', 'local-only', '--apply', '--json', expected=0)
        data = json.loads(result.stdout)
        exclude = self.repo_exclude_path().read_text()
        gitignore = (self.repo / '.gitignore').read_text() if (self.repo / '.gitignore').exists() else ''

        self.assertFalse(data['manifest_tracked'])
        self.assertEqual(self.git('ls-files', '.syncwheel/manifest.json'), '')
        self.assertIn('.syncwheel/', exclude)
        self.assertNotIn('var/syncwheel/', exclude)
        self.assertNotIn('# syncwheel managed metadata', gitignore)

    def test_explicit_legacy_worktree_root_is_preserved(self):
        result = self.run_cli(
            'repo',
            'tracking',
            'set',
            'git-tracked',
            '--worktree-root',
            'var/syncwheel',
            '--apply',
            '--json',
            expected=0,
        )
        data = json.loads(result.stdout)
        manifest = self.read_manifest()
        gitignore = (self.repo / '.gitignore').read_text()

        self.assertEqual(manifest['syncwheel_worktree_root'], 'var/syncwheel')
        self.assertEqual(data['syncwheel_worktree_root'], 'var/syncwheel')
        self.assertTrue(data['effective_worktree_root'].endswith('/var/syncwheel'))
        self.assertIn('var/syncwheel/', gitignore)

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

        self.assert_path_equal(data['manifest_path'], self.repo / '.syncwheel' / 'manifests' / 'alice.local.json')
        self.assertEqual(data['validation']['details']['stacks'][0]['id'], 'feature-c')

    def test_use_sets_repo_local_default_personal_manifest(self):
        self.run_cli('init', '--personal', 'alice', '--force', expected=0)
        self.run_cli('use', 'alice', expected=0)

        result = self.run_cli('check', '--no-fetch', '--json', expected=1)
        data = json.loads(result.stdout)

        self.assert_path_equal(data['manifest_path'], self.repo / '.syncwheel' / 'manifests' / 'alice.local.json')

    def test_use_shared_clears_repo_local_profile(self):
        self.run_cli('init', '--personal', 'alice', '--force', expected=0)
        self.run_cli('use', 'alice', expected=0)
        self.run_cli('use', '--shared', expected=0)

        result = self.run_cli('check', '--no-fetch', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assert_path_equal(data['manifest_path'], self.repo / '.syncwheel' / 'manifest.json')

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

        ledger = self.read_ledger_state()
        self.assertEqual(ledger['last_seq'], 1)
        self.assertIn('feature-c', ledger['manifest']['active_stacks'])
        self.assertEqual(ledger['stacks']['feature-c']['branch'], 'pr/alice/feature-c')

    def test_stack_create_includes_stack_by_default_when_membership_is_required(self):
        gamma = self.git('rev-parse', 'HEAD')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        self.run_cli(
            'stack',
            'create',
            'feature-required',
            gamma,
            '--branch',
            'pr/feature-required',
            expected=0,
        )

        updated = self.read_manifest()
        self.assertIn('feature-required', updated['integration']['stacks'])

    def test_stack_create_draft_materializes_its_branch_and_validates_without_warnings(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        self.run_cli('stack', 'create', 'exploration', '--draft')

        updated = self.read_manifest()
        draft = next(stack for stack in updated['stacks'] if stack['id'] == 'exploration')
        self.assertEqual(draft['branch'], 'syncwheel/draft/exploration')
        self.assertEqual(draft['state'], 'draft')
        self.assertEqual(draft['publication'], {'enabled': False})
        self.assertEqual(self.git('rev-parse', 'syncwheel/draft/exploration'), self.git('rev-parse', 'main'))
        self.assertIn('exploration', updated['integration']['stacks'])

        validation = json.loads(self.run_cli('validate', '--json').stdout)
        self.assertEqual(validation['errors'], [])
        self.assertEqual(validation['warnings'], [])

    def test_stack_capture_integration_materializes_a_draft_without_a_worktree(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest['integration'] = {
            'branch': 'integration/capture',
            'base': 'main',
            'stacks': [],
        }
        manifest['stacks'] = []
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('branch', 'integration/capture', 'main')
        self.git('switch', '-q', 'integration/capture')
        self.run_cli('stack', 'create', 'exploration', '--draft')

        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: capture gamma')
        gamma = self.git('rev-parse', 'HEAD')
        worktrees_before_capture = self.git('worktree', 'list', '--porcelain')

        result = self.run_cli('stack', 'capture-integration', 'exploration', 'HEAD')

        self.assertIn('exploration: captured 1 integration commit', result.stdout)
        self.assertEqual(self.git('worktree', 'list', '--porcelain'), worktrees_before_capture)
        updated = self.read_manifest()
        draft = next(stack for stack in updated['stacks'] if stack['id'] == 'exploration')
        self.assertEqual(draft['commits'], [gamma])
        self.assertEqual(self.git('branch', '--contains', gamma, 'syncwheel/draft/exploration'), 'syncwheel/draft/exploration')

        self.git('switch', '-q', 'main')
        integration_worktree = self.tmp / 'wt-capture-integration'
        self.run_cli('int', 'rebuild', '--worktree', str(integration_worktree))
        self.git('worktree', 'remove', '--force', str(integration_worktree))
        self.git('switch', '-q', 'integration/capture')

        validation = json.loads(self.run_cli('validate', '--json').stdout)
        self.assertEqual(validation['details']['integration']['unmapped_commits'], [])
        self.assertTrue(self.git('merge-base', '--is-ancestor', gamma, 'syncwheel/draft/exploration') == '')

    def test_stack_capture_integration_rolls_back_manifest_when_projection_fails(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        alpha = self.git('rev-parse', 'main~1')
        manifest['defaults']['integration_membership'] = 'required'
        manifest['integration'] = {
            'branch': 'integration/capture-conflict',
            'base': alpha,
            'stacks': ['feature-b'],
        }
        feature_b = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-b')
        feature_b['base'] = alpha
        feature_b['integration_branch'] = 'integration/capture-conflict'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('branch', 'integration/capture-conflict', 'main')
        self.git('switch', '-q', 'integration/capture-conflict')
        self.run_cli('stack', 'create', 'exploration', '--draft', '--base', alpha)

        Path(self.repo / 'beta.txt').write_text('beta changed on integration\n')
        self.git('add', 'beta.txt')
        self.git('commit', '-q', '-m', 'feat: integration-only beta follow-up')
        before_capture = manifest_path.read_text()

        failure = self.run_cli('stack', 'capture-integration', 'exploration', 'HEAD', expected=2)

        self.assertIn('projection failed after adding commits', failure.stderr)
        self.assertEqual(manifest_path.read_text(), before_capture)
        updated = self.read_manifest()
        draft = next(stack for stack in updated['stacks'] if stack['id'] == 'exploration')
        self.assertEqual(draft['commits'], [])

    def test_stack_capture_integration_draft_uses_its_branch_for_merge_stacks(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest['integration'] = {
            'branch': 'integration/capture-merge',
            'base': 'main',
            'strategy': 'merge-stacks',
            'stacks': [],
        }
        manifest['stacks'] = []
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('branch', 'integration/capture-merge', 'main')
        self.git('switch', '-q', 'integration/capture-merge')
        self.run_cli('stack', 'create', 'exploration', '--draft')

        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: merge captured gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.run_cli('stack', 'capture-integration', 'exploration', 'HEAD')

        self.git('switch', '-q', 'main')
        integration_worktree = self.tmp / 'wt-capture-merge'
        self.run_cli('int', 'rebuild', '--worktree', str(integration_worktree))
        self.assertEqual(self.git('merge-base', '--is-ancestor', gamma, 'integration/capture-merge'), '')
        self.git('worktree', 'remove', '--force', str(integration_worktree))

    def test_integration_diagnostics_offer_capture_into_a_new_draft(self):
        self.git('branch', 'integration/capture-diagnostics', 'main')
        self.git('switch', '-q', 'integration/capture-diagnostics')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: diagnose gamma')

        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['integration']['branch'] = 'integration/capture-diagnostics'
        manifest['integration']['base'] = 'main'
        manifest['integration']['stacks'] = []
        manifest['stacks'] = []
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        plan = json.loads(self.run_cli('plan', '--json').stdout)
        action = plan[-1]
        self.assertEqual(action['remedy']['type'], 'capture_integration_into_new_draft')
        self.assertIn('stack capture-integration <new-stack-id>', action['remedy']['commands'][1])

        check = self.run_cli('check', '--no-fetch')
        self.assertIn('remedy: capture into a new draft stack:', check.stdout)
        self.assertIn('stack capture-integration <new-stack-id>', check.stdout)

    def test_stack_push_rejects_a_draft_by_state(self):
        self.run_cli('stack', 'create', 'exploration', '--draft')

        failure = self.run_cli('stack', 'push', 'exploration', expected=2)

        self.assertIn('state draft', failure.stderr)

    def test_reconcile_push_rejects_a_draft_without_dropping_its_rebuild_action(self):
        self.run_cli('stack', 'create', 'exploration', '--draft')
        self.git('branch', '-D', 'syncwheel/draft/exploration')

        failure = self.run_cli(
            'reconcile',
            '--no-fetch',
            '--push',
            '--stack',
            'exploration',
            '--skip-integration',
            expected=2,
        )

        self.assertIn('rebuild_stack stack=exploration', failure.stdout)
        self.assertIn('push_stack_refused stack=exploration', failure.stdout)
        self.assertIn('state draft', failure.stderr)

    def test_stack_promote_matches_a_directly_created_published_stack(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        original = self.read_manifest()

        self.run_cli('stack', 'create', 'lifecycle')
        direct = next(stack for stack in self.read_manifest()['stacks'] if stack['id'] == 'lifecycle')
        manifest_path.write_text(json.dumps(original, indent=2) + '\n')

        self.run_cli('stack', 'create', 'lifecycle', '--draft')
        self.run_cli('stack', 'promote', 'lifecycle')
        promoted = next(stack for stack in self.read_manifest()['stacks'] if stack['id'] == 'lifecycle')

        self.assertEqual(promoted, direct)
        self.assertEqual(self.git('rev-parse', 'pr/lifecycle'), self.git('rev-parse', 'main'))
        self.assertNotEqual(
            subprocess.run(
                ['git', 'rev-parse', '--verify', '--quiet', 'syncwheel/draft/lifecycle'],
                cwd=self.repo,
                text=True,
                capture_output=True,
            ).returncode,
            0,
        )

    def test_stack_promote_reports_a_retained_reconcile_worktree_path(self):
        self.run_cli('stack', 'create', 'lifecycle', '--draft')
        old_branch = 'syncwheel/draft/lifecycle'
        retained = self.repo / '.syncwheel' / 'wt' / 'syncwheel-draft-lifecycle'
        self.git('worktree', 'add', str(retained), old_branch)

        result = self.run_cli('stack', 'promote', 'lifecycle')

        self.assertTrue(retained.exists())
        self.assertIn(f'worktree path retained (not moved): {retained}', result.stdout)
        branch = subprocess.run(
            ['git', '-C', str(retained), 'branch', '--show-current'],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(branch, 'pr/lifecycle')

    def test_stack_demote_refuses_an_open_github_pull_request(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['stacks'][0]['github'] = {'pr': 42}
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        failure = self.run_cli('stack', 'demote', 'feature-a', expected=2)

        self.assertIn('github.pr', failure.stderr)

    def test_required_membership_rejects_excluded_stack(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest['integration']['stacks'] = ['feature-a']
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli('validate', expected=1)

        self.assertIn('required integration membership excludes stack(s): feature-b', result.stdout)

    def test_manifest_require_integration_migrates_existing_stacks(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['integration']['stacks'] = ['feature-a']
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli('manifest', 'require-integration', '--json', expected=0)
        proposal = json.loads(result.stdout)

        self.assertEqual(proposal['add_to_integration'], ['feature-b'])
        self.assertFalse(proposal['apply'])

        self.run_cli('manifest', 'require-integration', '--apply', expected=0)
        updated = self.read_manifest()

        self.assertEqual(updated['defaults']['integration_membership'], 'required')
        self.assertEqual(updated['integration']['stacks'], ['feature-a', 'feature-b'])

    def test_spoke_alias_maps_to_stack_commands(self):
        list_result = self.run_cli('spoke', 'list', expected=0)
        self.assertIn('feature-a\tpr/feature-a\tcommits=1', list_result.stdout)

        show_result = self.run_cli('spoke', 'show', 'feature-b', expected=0)
        data = json.loads(show_result.stdout)
        self.assertEqual(data['id'], 'feature-b')
        self.assertEqual(data['branch'], 'pr/feature-b')

    def test_stack_add_accepts_integration_first_commit_on_current_projection(self):
        base = self.git('rev-parse', 'HEAD')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = self.read_manifest()
        data['defaults']['base_ref'] = base
        data['integration']['base'] = base
        for stack in data['stacks']:
            stack['base'] = base
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')

        result = self.run_cli('stack', 'add', 'feature-b', 'HEAD', expected=0)

        self.assertIn('feature-b: now has 3 commits', result.stdout)
        manifest = self.read_manifest()
        feature_b = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-b')
        self.assertEqual(feature_b['commits'][-1], gamma)

    def test_stack_add_rejects_integration_first_commit_on_stale_projection(self):
        base = self.git('rev-parse', 'HEAD')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = self.read_manifest()
        data['defaults']['base_ref'] = base
        data['integration']['base'] = base
        for stack in data['stacks']:
            stack['base'] = base

        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add stack gamma')
        stack_gamma = self.git('rev-parse', 'HEAD')
        feature_b = next(stack for stack in data['stacks'] if stack['id'] == 'feature-b')
        feature_b['commits'].append(stack_gamma)
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        self.git('switch', '-q', 'main')
        Path(self.repo / 'delta.txt').write_text('delta\n')
        self.git('add', 'delta.txt')
        self.git('commit', '-q', '-m', 'feat: add delta from stale integration')
        delta = self.git('rev-parse', 'HEAD')

        result = self.run_cli('stack', 'add', 'feature-b', 'HEAD', expected=2)

        self.assertIn('not created on top of the current manifest projection', result.stderr)
        manifest = self.read_manifest()
        feature_b = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-b')
        self.assertEqual(feature_b['commits'][-1], stack_gamma)
        self.assertNotIn(delta, feature_b['commits'])

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

    def test_stack_rebuild_replays_same_commit_to_same_sha(self):
        base, original_commits = self.prepare_replay_stack()
        worktree = self.tmp / 'wt-replay'

        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)

        self.assertEqual(
            self.git('rev-list', '--reverse', f'{base}..pr/replay').splitlines(),
            original_commits,
        )

    def test_stack_rebuild_twice_keeps_identical_tip(self):
        self.prepare_replay_stack()
        worktree = self.tmp / 'wt-replay'

        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)
        first_tip = self.git('rev-parse', 'pr/replay')
        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)

        self.assertEqual(self.git('rev-parse', 'pr/replay'), first_tip)

    def test_stack_rebuild_dry_run_emits_one_pinned_cherry_pick_per_commit(self):
        result = self.run_cli('stack', 'rebuild', 'feature-b', '--in-place', '--dry-run', expected=0)

        cherry_pick_lines = [line for line in result.stdout.splitlines() if ' cherry-pick ' in line]

        self.assertEqual(len(cherry_pick_lines), 2)
        self.assertTrue(all('GIT_COMMITTER_DATE=' in line for line in cherry_pick_lines))

    def test_stack_rebuild_disables_configured_gpg_signing(self):
        _, original_commits = self.prepare_replay_stack()
        worktree = self.tmp / 'wt-replay'
        self.git('config', 'commit.gpgsign', 'true')

        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)
        first_tip = self.git('rev-parse', 'pr/replay')
        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)

        self.assertEqual(first_tip, original_commits[-1])
        self.assertEqual(self.git('rev-parse', 'pr/replay'), first_tip)

    def test_merge_stacks_rebuild_uses_deterministic_stack_tip_metadata(self):
        base, _ = self.prepare_replay_stack()
        stack_worktree = self.tmp / 'wt-replay'
        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(stack_worktree), expected=0)

        data = self.read_manifest()
        data['integration'] = {
            'branch': 'integration/replay',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['replay'],
        }
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        integration_worktree = self.tmp / 'wt-integration-replay'

        self.run_cli('int', 'rebuild', '--worktree', str(integration_worktree), expected=0)
        first_tip = self.git('rev-parse', 'integration/replay')
        self.run_cli('int', 'rebuild', '--worktree', str(integration_worktree), expected=0)

        self.assertEqual(self.git('rev-parse', 'integration/replay'), first_tip)

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

    def test_stack_resolve_integration_keeps_source_projection_intact(self):
        self.git('switch', '-q', '-c', 'integration/test', 'main')
        Path(self.repo / 'gamma.txt').write_text('resolved integration\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'fix: resolve feature-b integration conflict')
        resolved = self.git('rev-parse', 'HEAD')

        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['integration']['branch'] = 'integration/test'
        manifest['integration']['base'] = 'main'
        manifest['integration']['stacks'] = ['feature-b']
        manifest['stacks'] = [manifest['stacks'][1]]
        manifest['stacks'][0]['integration_branch'] = 'integration/test'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        self.run_cli('stack', 'resolve-integration', 'feature-b', resolved, expected=0)
        updated = self.read_manifest()
        stack = updated['stacks'][0]
        self.assertEqual(stack['integration_commits'], [resolved])
        self.assertEqual(len(stack['commits']), 2)

        validation = json.loads(self.run_cli('validate', '--json', expected=0).stdout)
        self.assertEqual(validation['warnings'], [])
        self.assertEqual(json.loads(self.run_cli('plan', '--json', expected=0).stdout), [])

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

    def test_publish_uses_force_with_lease_by_default(self):
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
            'publish',
            '--no-fetch',
            '--stack',
            'publish',
            '--skip-integration',
            expected=0,
        )

        self.assertIn('git push --force-with-lease fork pr/publish', result.stdout)

    def test_sync_never_pushes(self):
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
            'sync',
            '--no-fetch',
            '--stack',
            'publish',
            '--skip-integration',
            expected=0,
        )

        self.assertNotIn('git push', result.stdout)

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

    def test_reconcile_accepts_stack_already_absorbed_by_base(self):
        module = self.load_syncwheel_module()
        absorbed = self.git('rev-parse', 'main~1')
        manifest = self.read_manifest()
        stack = next(item for item in manifest['stacks'] if item['id'] == 'feature-a')
        stack['base'] = 'main'
        stack['commits'] = [absorbed]

        report = module.stack_reconcile_report(self.repo, manifest, stack)

        self.assertTrue(report['absorbed'])
        self.assertTrue(report['local_matches_projection'])
        self.assertEqual(report['projected_tree'], module.ref_tree(self.repo, 'main'))

    def test_integration_projection_accepts_manifest_only_control_tree(self):
        module = self.load_syncwheel_module()
        manifest = self.read_manifest()
        base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', '-c', 'integration/control')
        manifest['integration'] = {
            'branch': 'integration/control',
            'base': base,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        Path(self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps({**manifest, 'control': 'recorded'}, indent=2) + '\n'
        )
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'syncwheel: record control state')

        report = module.integration_sync_report(self.repo, manifest)

        self.assertTrue(report['local_matches_projection'])

    def test_integration_projection_rejects_product_tree_difference(self):
        module = self.load_syncwheel_module()
        manifest = self.read_manifest()
        base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', '-c', 'integration/product')
        manifest['integration'] = {
            'branch': 'integration/product',
            'base': base,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        Path(self.repo / 'product.txt').write_text('drift\n')
        self.git('add', 'product.txt')
        self.git('commit', '-q', '-m', 'feat: unprojected product change')

        report = module.integration_sync_report(self.repo, manifest)

        self.assertFalse(report['local_matches_projection'])

    def test_reconcile_reports_dirty_working_tree_status(self):
        Path(self.repo / 'dirty.txt').write_text('dirty\n')

        result = self.run_cli('reconcile', '--no-fetch', expected=0)

        self.assertIn('working tree:', result.stdout)
        self.assertIn('?? dirty.txt', result.stdout)

        result = self.run_cli('reconcile', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        self.assertTrue(report['snapshot']['working_tree_dirty'])
        self.assertIn('?? dirty.txt', report['snapshot']['working_tree_status'])

    def test_stack_absorb_moves_integration_changes_to_stack(self):
        Path(self.repo / 'beta.txt').write_text('beta\nabsorbed\n')

        result = self.run_cli('stack', 'absorb', 'feature-b', 'beta.txt', expected=0)

        self.assertIn('feature-b: absorbed changes into pr/feature-b', result.stdout)
        self.assertEqual(self.tracked_status(), '')
        self.assertEqual(self.git('show', 'pr/feature-b:beta.txt'), 'beta\nabsorbed')
        manifest = self.read_manifest()
        feature_b = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-b')
        self.assertEqual(feature_b['commits'], self.git('rev-list', 'main..pr/feature-b').splitlines())

    def test_stack_absorb_can_absorb_staged_hunks_only(self):
        original = Path(self.repo / 'beta.txt').read_text()
        Path(self.repo / 'beta.txt').write_text(original + 'staged\n')
        self.git('add', 'beta.txt')
        Path(self.repo / 'alpha.txt').write_text('alpha\nunstaged\n')

        self.run_cli('stack', 'absorb', 'feature-b', '--staged', expected=0)

        self.assertEqual(Path(self.repo / 'beta.txt').read_text(), original)
        self.assertEqual(Path(self.repo / 'alpha.txt').read_text(), 'alpha\nunstaged\n')
        self.assertEqual(self.tracked_status(), 'M alpha.txt')

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

        self.assertEqual(updated_commit, self.git('rev-parse', 'pr/feature-b'))
        self.assertEqual(self.git('rev-list', '--count', f'{base}..pr/feature-b'), '1')
        self.assertEqual(self.git('rev-parse', 'pr/feature-b:beta.txt'), self.git('rev-parse', f'{updated_commit}:beta.txt'))
        self.assertEqual(self.git('rev-list', '--count', f'{base}..integration/reconcile'), '2')

    def prepare_reconcile_apply_worktree_scenario(self, worktree_root=None):
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

        data = self.read_manifest()
        data['syncwheel_tracking'] = 'local-only'
        if worktree_root is not None:
            data['syncwheel_worktree_root'] = worktree_root
        else:
            data.pop('syncwheel_worktree_root', None)
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
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        self.git('switch', '-q', 'integration/reconcile')

    def test_reconcile_apply_uses_default_worktree_root(self):
        self.prepare_reconcile_apply_worktree_scenario()

        self.run_cli(
            'reconcile', '--no-fetch', '--apply', '--skip-integration', '--replay-mode', 'desk', expected=0
        )

        self.assertTrue((self.repo / '.syncwheel' / 'wt' / 'pr-feature-b').exists())

    def test_reconcile_apply_preserves_explicit_legacy_worktree_root(self):
        self.prepare_reconcile_apply_worktree_scenario('var/syncwheel')

        self.run_cli(
            'reconcile', '--no-fetch', '--apply', '--skip-integration', '--replay-mode', 'desk', expected=0
        )

        self.assertTrue((self.repo / 'var' / 'syncwheel' / 'pr-feature-b').exists())

    def test_reconcile_apply_leaves_no_worktree_by_default(self):
        self.prepare_reconcile_apply_worktree_scenario()
        before = self.git('worktree', 'list', '--porcelain')

        self.run_cli('reconcile', '--no-fetch', '--apply', '--skip-integration', expected=0)

        self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)
        self.assertFalse((self.repo / '.syncwheel' / 'wt' / 'pr-feature-b').exists())

    def test_reconcile_apply_preflights_dirty_integration_before_rebuilding_a_stack(self):
        self.prepare_reconcile_apply_worktree_scenario()
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        before_manifest = manifest.read_text()
        before_stack = self.git('rev-parse', 'pr/feature-b')
        ledger = self.repo / '.syncwheel' / 'ledger'
        self.assertFalse(ledger.exists())
        Path(self.repo / 'alpha.txt').write_text('dirty integration\n')

        result = self.run_cli('reconcile', '--no-fetch', '--apply', expected=2)

        self.assertIn(f'{self.repo} is not clean', result.stderr)
        self.assertEqual(self.git('rev-parse', 'pr/feature-b'), before_stack)
        self.assertEqual(manifest.read_text(), before_manifest)
        self.assertFalse(ledger.exists())

    def test_check_leaves_reconcile_targets_manifest_and_ledger_unchanged(self):
        self.prepare_reconcile_apply_worktree_scenario()
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        before_manifest = manifest.read_text()
        before_stack = self.git('rev-parse', 'pr/feature-b')
        before_integration = self.git('rev-parse', 'integration/reconcile')
        ledger = self.repo / '.syncwheel' / 'ledger'
        self.assertFalse(ledger.exists())

        result = self.run_cli('check', '--no-fetch', '--json', expected=0)

        self.assertIn('plan', json.loads(result.stdout))
        self.assertEqual(self.git('rev-parse', 'pr/feature-b'), before_stack)
        self.assertEqual(self.git('rev-parse', 'integration/reconcile'), before_integration)
        self.assertEqual(manifest.read_text(), before_manifest)
        self.assertFalse(ledger.exists())

    def test_sync_rebuilds_local_projection_without_push(self):
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

        manifest_path = self.tmp / 'sync-manifest.json'
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
            'sync',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--worktree-root',
            str(self.tmp / 'worktrees'),
            expected=0,
        )
        updated = json.loads(manifest_path.read_text())
        ledger_root = self.expected_external_ledger_root(manifest_path)
        ledger_state = self.run_cli('ledger', 'show', '--manifest', str(manifest_path), '--json', expected=0)

        self.assertNotIn('git push', result.stdout)
        self.assertEqual(updated['stacks'][0]['commits'][0], self.git('rev-parse', 'pr/feature-b'))
        self.assertEqual(self.git('rev-list', '--count', f'{base}..integration/reconcile'), '2')
        self.assertTrue((ledger_root / 'events').exists())
        self.assertFalse((self.repo / '.syncwheel' / 'ledger').exists())
        self.assertNotIn('.syncwheel/', self.repo_exclude_path().read_text())
        self.assertEqual(self.git('status', '--short', '--untracked-files=all', '--', '.syncwheel/ledger'), '')
        self.assertGreater(json.loads(ledger_state.stdout)['last_seq'], 0)

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
        self.git('switch', '-q', 'integration/reconcile')

        self.assertNotEqual(local_stack, remote_stack)
        self.assertNotEqual(local_integration, remote_integration)

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--no-align-local-to-remote',
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
        (self.repo / 'openpack.json').write_text('{"version": "9.9.9"}\n')
        self.git(
            'add', 'scripts/demo.py', 'VERSION', 'CHANGELOG.md', 'README.md',
            'openpack.json',
        )
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
        docs = self.repo / 'docs'
        docs.mkdir(exist_ok=True)
        shutil.copy2(REPO_ROOT / 'docs' / 'sync_version.py', docs / 'sync_version.py')
        version = '1.0.0'
        (self.repo / 'VERSION').write_text(f'{version}\n')
        marker = f'<!-- syncwheel-version:start -->{version}<!-- syncwheel-version:end -->'
        (docs / 'index.html').write_text(f'hero {marker} footer {marker}\n')
        self.git('add', 'VERSION', 'docs/sync_version.py', 'docs/index.html')
        self.git('commit', '-q', '-m', 'test: add generated website')

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
        data['integration']['stacks'] = ['feature-b']
        data['stacks'] = [data['stacks'][1]]
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('validate', '--json', expected=0)
        validation = json.loads(result.stdout)

        self.assertIn('not declared in any stack', '\n'.join(validation['warnings']))
        self.assertEqual(len(validation['details']['integration']['unmapped_commits']), 1)

    def test_validate_accepts_manifest_only_integration_control_commit(self):
        self.git('switch', '-q', '-c', 'integration/test', 'main')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = self.read_manifest()
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'chore(syncwheel): update integration contract')

        validation = json.loads(self.run_cli('validate', '--json', expected=0).stdout)
        self.assertEqual(validation['warnings'], [])
        self.assertEqual(len(validation['details']['integration']['control_commits']), 1)

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
        data['integration']['stacks'] = ['feature-b']
        data['stacks'] = [data['stacks'][1]]
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('plan', '--json', expected=0)
        plan = json.loads(result.stdout)

        self.assertEqual(plan[-1]['type'], 'classify_integration_commits')
        self.assertEqual(len(plan[-1]['commits']), 1)

    def test_check_reports_unmapped_integration_commit_guidance(self):
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('branch', 'integration/test', 'HEAD')
        self.git('switch', '-q', 'integration/test')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('check', '--no-fetch', expected=0)

        self.assertIn('unmapped integration commits:', result.stdout)
        self.assertIn('feat: add gamma', result.stdout)
        self.assertIn('gamma.txt', result.stdout)
        self.assertIn('pr/feature-b', result.stdout)
        self.assertIn('likely stack owners:', result.stdout)
        self.assertIn('feature-b', result.stdout)
        self.assertIn('syncwheel stack add feature-b', result.stdout)

        result = self.run_cli('check', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        diagnostics = report['diagnostics']['unmapped_integration_commits']
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]['commit'], gamma)
        self.assertEqual(diagnostics[0]['likely_stacks'][0]['id'], 'feature-b')

    def test_check_warns_before_adding_same_subject_unmapped_commit(self):
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma remote\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        declared = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', '-c', 'integration/test', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma local\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['stacks'][1]['commits'].append(declared)
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('check', '--no-fetch', expected=0)

        self.assertIn('related declared commits:', result.stdout)
        self.assertIn('same_subject_declared_in_manifest', result.stdout)

    def test_reconcile_resume_mode_registers_single_likely_stack(self):
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('branch', 'integration/test', 'HEAD')
        self.git('switch', '-q', 'integration/test')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = ['feature-b']
        data['stacks'] = [data['stacks'][1]]
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('reconcile', '--mode', 'resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)

        self.assertEqual(report['mode'], 'resume')
        self.assertEqual(report['actions'][0]['type'], 'resume_add_commit')
        self.assertEqual(report['actions'][0]['stack'], 'feature-b')
        self.assertEqual(report['actions'][0]['commit'], gamma)
        updated = json.loads(manifest.read_text())
        self.assertNotIn(gamma, updated['stacks'][0]['commits'])

        self.run_cli('reconcile', '--mode', 'resume', '--no-fetch', '--apply', expected=0)
        updated = json.loads(manifest.read_text())
        self.assertIn(gamma, updated['stacks'][0]['commits'])

    def test_resume_alias_requires_manual_review_without_detected_owner(self):
        self.git('switch', '-q', '-c', 'integration/test', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat(DIGIT-17765): add gamma')
        gamma = self.git('rev-parse', 'HEAD')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = []
        data['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        self.git('switch', '-q', 'integration/test')

        result = self.run_cli('resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)

        self.assertEqual(report['mode'], 'resume')
        self.assertEqual(report['actions'][0]['type'], 'resume_manual_review')
        self.assertEqual(report['actions'][0]['reason'], 'owner_not_detected')
        self.assertEqual(report['actions'][0]['commit'], gamma)

        self.run_cli('resume', '--no-fetch', '--apply', expected=2)
        updated = json.loads(manifest.read_text())
        self.assertEqual(updated['integration']['stacks'], [])
        self.assertEqual(updated['stacks'], [])

    def test_resume_drops_an_integration_commit_absorbed_by_historical_merged_stack(self):
        self.git('switch', '-q', '-c', 'pr/feature-gamma', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        historical = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        self.run_cli(
            'stack', 'create', 'feature-gamma', historical,
            '--branch', 'pr/feature-gamma', '--include-in-integration', expected=0,
        )
        self.run_cli(
            'stack', 'close', 'feature-gamma', '--force',
            '--reason', 'merged-by-squash-tree-equivalent', expected=0,
        )

        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        Path(self.repo / 'delta.txt').write_text('delivered alongside gamma\n')
        self.git('add', 'gamma.txt', 'delta.txt')
        self.git('commit', '-q', '-m', 'feat: deliver gamma with delta')
        delivered = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', '-c', 'integration/test', f'{delivered}^')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'chore: replay gamma')
        duplicate = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', 'integration/test')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = []
        data['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        module = self.load_syncwheel_module()
        self.assertEqual(
            module.commit_patch_id(self.repo, historical),
            module.commit_patch_id(self.repo, duplicate),
        )
        self.assertNotIn(
            module.commit_patch_id(self.repo, duplicate),
            module.patch_ids_reachable_from_ref(self.repo, 'main'),
        )
        self.assertEqual(module.rev_list(self.repo, 'main..integration/test'), [duplicate])

        result = self.run_cli('resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        integration = report['validation']['details']['integration']
        self.assertEqual(integration['unmapped_commits'], [duplicate])
        self.assertEqual(integration['absorbed_patch_commits'], [])
        drop = next(action for action in report['actions'] if action['type'] == 'resume_drop_absorbed_commit')
        self.assertEqual(drop['commit'], duplicate)
        self.assertEqual(drop['stack'], 'feature-gamma')
        self.assertEqual(drop['matched_commit'], historical)
        self.assertFalse(any(action['type'] == 'resume_manual_review' for action in report['actions']))

        self.run_cli('resume', '--no-fetch', '--apply', expected=0)
        self.run_cli('reconcile', '--no-fetch', '--apply', expected=0)
        self.assertEqual(self.git('rev-parse', 'integration/test'), self.git('rev-parse', 'main'))
        self.assertNotEqual(self.git('rev-parse', 'integration/test'), duplicate)

    def test_resume_keeps_a_patch_equivalent_closed_stack_for_manual_review(self):
        self.git('switch', '-q', '-c', 'pr/feature-gamma', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        historical = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        self.run_cli(
            'stack', 'create', 'feature-gamma', historical,
            '--branch', 'pr/feature-gamma', '--include-in-integration', expected=0,
        )
        self.run_cli('stack', 'close', 'feature-gamma', '--force', '--reason', 'abandoned', expected=0)

        self.git('switch', '-q', '-c', 'integration/test', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'chore: replay gamma')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = []
        data['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        self.assertFalse(any(action['type'] == 'resume_drop_absorbed_commit' for action in report['actions']))
        manual = next(action for action in report['actions'] if action['type'] == 'resume_manual_review')
        self.assertEqual(manual['reason'], 'owner_not_detected')

    def test_resume_restores_historical_stack_from_ledger(self):
        self.git('switch', '-q', '-c', 'pr/feature-c', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('branch', 'integration/test', 'HEAD')
        self.git('switch', '-q', 'main')

        self.run_cli(
            'stack',
            'create',
            'feature-c',
            gamma,
            '--branch',
            'pr/feature-c',
            '--include-in-integration',
            expected=0,
        )
        Path(self.repo / 'delta.txt').write_text('delta\n')
        self.git('add', 'delta.txt')
        self.git('commit', '-q', '-m', 'feat: add delta')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = []
        data['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        self.git('switch', '-q', 'integration/test')
        self.git('add', '-f', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'chore: track recovery manifest')

        check = self.run_cli('check', '--no-fetch', '--json', expected=0)
        check_report = json.loads(check.stdout)
        check_diagnostics = check_report['diagnostics']['unmapped_integration_commits']
        self.assertEqual(check_diagnostics[0]['historical_stacks'][0]['id'], 'feature-c')

        result = self.run_cli('resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)

        self.assertEqual(report['mode'], 'resume')
        self.assertEqual(report['actions'][0]['type'], 'resume_restore_stack')
        self.assertEqual(report['actions'][0]['stack'], 'feature-c')
        self.assertEqual(report['actions'][1]['type'], 'resume_add_commit')
        self.assertEqual(report['actions'][1]['stack'], 'feature-c')

        self.run_cli('resume', '--no-fetch', '--apply', expected=0)
        updated = json.loads(manifest.read_text())
        self.assertEqual(updated['integration']['stacks'], ['feature-c'])
        self.assertEqual(updated['stacks'][0]['id'], 'feature-c')
        self.assertEqual(updated['stacks'][0]['branch'], 'pr/feature-c')
        self.assertEqual(updated['stacks'][0]['commits'], [self.git('rev-parse', 'pr/feature-c')])
        self.assertNotEqual(updated['stacks'][0]['commits'], [gamma])

    def test_in_place_replay_accepts_a_tracked_manifest_with_omitted_defaults(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        self.git('add', '-f', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: track manifest')
        module = self.load_syncwheel_module()
        raw_manifest = json.loads(manifest.read_text())
        normalized_manifest, _ = module.load_manifest(self.repo, manifest)

        self.assertNotEqual(
            module.manifest_digest(raw_manifest),
            module.manifest_digest(normalized_manifest),
        )
        with module.manifest_write_transaction(self.repo, manifest):
            module.acknowledge_in_place_manifest_replay(
                self.repo,
                manifest,
                self.git('rev-parse', 'HEAD'),
            )

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

    def test_common_short_json_flags_are_accepted(self):
        commands = [
            ('status', '-j'),
            ('plan', '-j'),
            ('check', '-F', '-j'),
            ('repo', 'tracking', 'status', '-j'),
        ]

        for command in commands:
            with self.subTest(command=command):
                result = self.run_cli(*command, expected=0)
                json.loads(result.stdout)

    def test_repo_alias_can_store_default_manifest_path(self):
        custom_manifest = self.tmp / 'custom-manifest.json'
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        custom_manifest.write_text(manifest.read_text())

        self.run_cli('repo', 'add', 'fixture2', str(self.repo), '--manifest', str(custom_manifest), expected=0)
        result = self.run_cli('status', '-r', 'fixture2', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assert_path_equal(data['manifest_path'], custom_manifest)

    def test_legacy_script_entrypoint_still_reports_version(self):
        result = self.run_custom_cli(CLI, '--version', expected=0, cwd=REPO_ROOT)

        self.assertIn(f"syncwheel {(REPO_ROOT / 'VERSION').read_text().strip()}", result.stdout)

    def test_install_kind_detection_identifies_git_checkout(self):
        fixture = self.init_syncwheel_install_fixture()
        syncwheel = self.load_syncwheel_module()

        detected = syncwheel.detect_syncwheel_install(root=fixture['install'], source_path=fixture['cli'])

        self.assertEqual(detected['kind'], 'git-clone')
        self.assertTrue(detected['git_repo'])
        self.assertEqual(detected['install_root'], fixture['install'])

    def test_install_kind_detection_identifies_plain_script_without_git(self):
        syncwheel = self.load_syncwheel_module()
        script_root = self.tmp / 'standalone-syncwheel'
        script_path = script_root / 'scripts' / 'syncwheel.py'
        script_path.parent.mkdir(parents=True)
        script_path.write_text('# placeholder\n')

        detected = syncwheel.detect_syncwheel_install(
            source_path=script_path,
            prefix=self.tmp / 'not-a-tool-venv',
            env={},
        )

        self.assertEqual(detected['kind'], 'script')
        self.assertFalse(detected['git_repo'])
        self.assertEqual(detected['install_root'], script_root)

    def test_install_kind_detection_identifies_uv_tool_environment(self):
        syncwheel = self.load_syncwheel_module()
        tool_dir = self.tmp / 'uv-tools'
        prefix = tool_dir / 'syncwheel'
        source_path = prefix / 'lib' / 'python3.12' / 'site-packages' / 'syncwheel.py'
        source_path.parent.mkdir(parents=True)
        source_path.write_text('# placeholder\n')
        (prefix / 'pyvenv.cfg').write_text('home = /usr/bin\n')

        detected = syncwheel.detect_syncwheel_install(
            source_path=source_path,
            prefix=prefix,
            env={'UV_TOOL_DIR': str(tool_dir)},
        )

        self.assertEqual(detected['kind'], 'uv-tool')
        self.assertFalse(detected['git_repo'])
        self.assertEqual(detected['install_root'], prefix)

    def test_self_update_command_selection_per_install_kind(self):
        syncwheel = self.load_syncwheel_module()

        self.assertEqual(
            syncwheel.build_self_update_commands({'install_kind': 'uv-tool'}),
            [['uv', 'tool', 'upgrade', 'syncwheel']],
        )
        self.assertEqual(
            syncwheel.build_self_update_commands({'install_kind': 'git-clone', 'upstream': 'origin/main'}),
            [['git', 'fetch', '--quiet', 'origin', '--tags'], ['git', 'merge', '--ff-only', 'origin/main']],
        )
        self.assertEqual(
            syncwheel.build_self_update_commands(
                {'install_kind': 'git-clone', 'upstream': 'origin/main'},
                fetch=False,
            ),
            [['git', 'merge', '--ff-only', 'origin/main']],
        )

    def test_remote_version_fetch_parses_version_without_network(self):
        syncwheel = self.load_syncwheel_module()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, _size):
                return b'\n  0.18.0\n'

        with mock.patch.object(syncwheel.urllib.request, 'urlopen', return_value=FakeResponse()) as urlopen:
            version = syncwheel.fetch_remote_version('https://example.invalid/VERSION')

        self.assertEqual(version, '0.18.0')
        urlopen.assert_called_once()

    def test_self_status_reports_script_install_kind_for_non_git_copy(self):
        standalone = self.tmp / 'standalone-syncwheel'
        cli = standalone / 'scripts' / 'syncwheel.py'
        cli.parent.mkdir(parents=True)
        shutil.copy2(CLI, cli)
        (standalone / 'VERSION').write_text('0.18.0\n')

        result = self.run_custom_cli(
            cli,
            'self',
            'status',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(self.tmp / 'standalone-update-state.json'),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.tmp / 'standalone-settings.json'),
            },
            cwd=standalone,
        )
        data = json.loads(result.stdout)

        self.assertEqual(data['status']['install_kind'], 'script')
        self.assertFalse(data['status']['can_self_update'])
        self.assertIn('not a git checkout', data['status']['reason'])

    def test_agentwheel_skill_status_skips_when_agentwheel_is_absent(self):
        syncwheel = self.load_syncwheel_module()

        with mock.patch.object(syncwheel.shutil, 'which', return_value=None):
            status = syncwheel.collect_agentwheel_syncwheel_skill_status(target_root=self.repo)

        self.assertFalse(status['available'])
        self.assertFalse(status['checked'])
        self.assertEqual(status['status'], 'unavailable')
        self.assertIsNone(status['installed'])
        self.assertIn('agentwheel not found', status['note'])

    def test_agentwheel_skill_status_treats_doctor_failure_as_nonfatal(self):
        syncwheel = self.load_syncwheel_module()
        result = subprocess.CompletedProcess(
            args=['agentwheel', 'doctor'],
            returncode=2,
            stdout='',
            stderr='unknown option: --json\n',
        )

        with mock.patch.object(syncwheel.shutil, 'which', return_value='/usr/bin/agentwheel'):
            with mock.patch.object(syncwheel.subprocess, 'run', return_value=result) as run_mock:
                status = syncwheel.collect_agentwheel_syncwheel_skill_status(target_root=self.repo)

        self.assertTrue(status['available'])
        self.assertFalse(status['checked'])
        self.assertEqual(status['status'], 'unknown')
        self.assertIn('unknown option', status['note'])
        run_mock.assert_called_once()

    def test_agentwheel_skill_status_reads_agentwheel_doctor_skills_array(self):
        syncwheel = self.load_syncwheel_module()
        result = subprocess.CompletedProcess(
            args=['agentwheel', 'doctor'],
            returncode=0,
            stdout=json.dumps({
                'skills': [
                    {'name': 'agentwheel', 'status': 'managed', 'present': True},
                    {'name': 'syncwheel', 'status': 'missing', 'present': False},
                ],
            }),
            stderr='',
        )

        with mock.patch.object(syncwheel.shutil, 'which', return_value='/usr/bin/agentwheel'):
            with mock.patch.object(syncwheel.subprocess, 'run', return_value=result):
                status = syncwheel.collect_agentwheel_syncwheel_skill_status(target_root=self.repo)

        self.assertTrue(status['available'])
        self.assertTrue(status['checked'])
        self.assertEqual(status['status'], 'missing')
        self.assertFalse(status['installed'])
        self.assertTrue(status['missing'])

    def test_self_status_reports_missing_agentwheel_skill(self):
        standalone = self.tmp / 'standalone-syncwheel'
        cli = standalone / 'scripts' / 'syncwheel.py'
        cli.parent.mkdir(parents=True)
        shutil.copy2(CLI, cli)
        (standalone / 'VERSION').write_text('0.18.0\n')

        fake_bin = self.tmp / 'fake-bin'
        fake_bin.mkdir()
        args_path = self.tmp / 'agentwheel-args.json'
        payload = json.dumps({'skill': {'name': 'syncwheel', 'installed': False}})
        fake_agentwheel = fake_bin / 'agentwheel'
        fake_agentwheel.write_text(
            '#!/usr/bin/env python3\n'
            'import json\n'
            'import sys\n'
            'from pathlib import Path\n'
            f'Path({str(args_path)!r}).write_text(json.dumps(sys.argv[1:]))\n'
            f'print({payload!r})\n'
        )
        fake_agentwheel.chmod(0o755)
        env = {
            'PATH': f'{fake_bin}{os.pathsep}{os.environ["PATH"]}',
            'SYNCWHEEL_UPDATE_STATE_PATH': str(self.tmp / 'standalone-update-state.json'),
            'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.tmp / 'standalone-settings.json'),
        }

        result = self.run_custom_cli(cli, 'self', 'status', '--json', expected=0, extra_env=env, cwd=standalone)
        data = json.loads(result.stdout)
        target_root = str(standalone.resolve())

        self.assertEqual(data['agentwheel_skill']['status'], 'missing')
        self.assertTrue(data['agentwheel_skill']['missing'])
        self.assertEqual(data['agentwheel_skill']['target_root'], target_root)
        self.assertIn('agentwheel install github:NestDevLab/syncwheel', data['agentwheel_skill']['install_command'])
        self.assertEqual(
            json.loads(args_path.read_text()),
            [
                'doctor',
                '--adapter',
                'codex',
                '--local',
                '--target-root',
                target_root,
                '--skill',
                'syncwheel',
                '--source',
                'github:NestDevLab/syncwheel',
                '--json',
            ],
        )

        human = self.run_custom_cli(cli, 'self', 'status', expected=0, extra_env=env, cwd=standalone)
        self.assertIn('agentwheel_skill: missing', human.stdout)
        self.assertIn('recommended: agentwheel install github:NestDevLab/syncwheel', human.stdout)

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
