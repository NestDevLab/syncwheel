import argparse
import ast
import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / 'scripts' / 'syncwheel.py'
PROVIDER_MODULE_PATH = Path(__file__).parents[1] / 'scripts' / 'syncwheel_revision_provider.py'
SPEC = importlib.util.spec_from_file_location('syncwheel_managed_ref_guard', MODULE_PATH)
syncwheel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(syncwheel)

# git >= 2.54 also runs the hook in a pre-lock "preparing" phase. A chain that
# rejects there aborts the transaction before the guard runs.
CHAIN_LOG_PHASE = 'printf "user-%s\\n" "${1:-unknown}" >>"$SYNCWHEEL_TEST_CLI_LOG"\n'
CHAIN_REJECTING_PREPARED = (
    '#!/bin/sh\n' + CHAIN_LOG_PHASE + '[ "${1:-}" = prepared ] || exit 0\nexit 7\n'
)
CHAIN_REJECTING_EVERY_PHASE = '#!/bin/sh\n' + CHAIN_LOG_PHASE + 'exit 7\n'


class ManagedRefGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temp.name)
        self.repo = self.temp_root / 'repo'
        self.repo.mkdir()
        self.bin_dir = self.temp_root / 'bin'
        self.bin_dir.mkdir()
        syncwheel_bin = self.bin_dir / 'syncwheel'
        syncwheel_bin.write_text(
            '#!/bin/sh\n'
            'if [ -n "${SYNCWHEEL_TEST_CLI_LOG:-}" ]; then\n'
            '  printf "%s\\n" guard >>"$SYNCWHEEL_TEST_CLI_LOG"\n'
            'fi\n'
            'exec "' + os.fspath(Path(os.sys.executable)) + '" "'
            + os.fspath(MODULE_PATH) + '" "$@"\n'
        )
        syncwheel_bin.chmod(0o755)
        self.hook_env = os.environ.copy()
        self.hook_env['PATH'] = os.fspath(self.bin_dir) + os.pathsep + self.hook_env['PATH']
        self.original_path = os.environ.get('PATH')
        os.environ['PATH'] = self.hook_env['PATH']
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=self.repo, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=self.repo, check=True)
        (self.repo / '.git' / 'info' / 'exclude').write_text('.test-*\n')
        (self.repo / 'seed').write_text('seed\n')
        subprocess.run(['git', 'add', 'seed'], cwd=self.repo, check=True)
        subprocess.run(['git', 'commit', '-qm', 'seed'], cwd=self.repo, check=True)
        (self.repo / '.syncwheel').mkdir()
        self.manifest = {
            'version': 2,
            'defaults': {
                'canonical_remote': 'origin', 'publication_remote': 'origin',
                'base_branch': 'main', 'base_ref': 'origin/main',
                'integration_membership': 'required',
            },
            'integration': {
                'branch': 'main-integration', 'base': 'origin/main',
                'strategy': 'cherry-pick', 'stacks': ['feature'],
            },
            'stacks': [{
                'id': 'feature', 'branch': 'pr/feature', 'base': 'origin/main',
                'target_remote': 'origin', 'target_branch': 'main',
                'integration_branch': 'main-integration', 'commits': [],
                'state': 'published', 'publication': {'enabled': True}, 'meta': {},
            }],
            'syncwheel_tracking': 'git-tracked',
            'coordination': {
                'mode': 'disabled', 'id': 'default', 'remote': 'origin',
                'state_branch': 'syncwheel/state/default',
                'gc': {'worktree_grace_days': 7, 'backup_retention_days': 30, 'backup_keep': 2},
            },
        }
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(self.manifest, indent=2) + '\n'
        )

    def tearDown(self):
        if self.original_path is None:
            os.environ.pop('PATH', None)
        else:
            os.environ['PATH'] = self.original_path
        self.temp.cleanup()

    def args(self):
        return types.SimpleNamespace(
            repo=str(self.repo), manifest=None, personal=None,
            remote_name='origin', remote_url='example.invalid/repo', event=None,
        )

    def run_syncwheel(self, *args):
        environment = self.hook_env.copy()
        environment[syncwheel.ENV_UPDATE_MODE] = 'off'
        environment[syncwheel.ENV_UPDATE_SETTINGS_PATH] = str(
            self.repo / '.test-update-settings.json'
        )
        environment[syncwheel.ENV_UPDATE_STATE_PATH] = str(
            self.repo / '.test-update-state.json'
        )
        return subprocess.run(
            [os.fspath(Path(os.sys.executable)), os.fspath(MODULE_PATH), *args],
            cwd=self.repo,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def hook_bundle_bytes(self):
        paths = []
        for name in syncwheel.MANAGED_REPOSITORY_HOOKS:
            _, hook, backup, metadata, _ = syncwheel.managed_hook_paths(self.repo, name)
            paths.extend((hook, backup, metadata))
        paths.append(self.repo / '.syncwheel' / 'profile.local.json')
        paths.append(syncwheel.primary_guard_path(self.repo))
        return {
            str(path.relative_to(self.repo)): path.read_bytes()
            for path in paths
            if path.exists()
        }

    def write_executable(self, path, body='#!/bin/sh\nexit 0\n'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)
        return path

    def descendant_commit(self, subject='descendant'):
        old = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True
        ).strip()
        tree = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD^{tree}'], cwd=self.repo, text=True
        ).strip()
        new = subprocess.check_output(
            ['git', 'commit-tree', tree, '-p', old, '-m', subject],
            cwd=self.repo,
            text=True,
        ).strip()
        return old, new

    def hook_order(self, log_path):
        """Recorded hook order without the pre-lock 'preparing' phase."""
        return [
            entry for entry in log_path.read_text().splitlines()
            if entry != 'user-preparing'
        ]

    def write_selected_manifest(self, path, branch):
        manifest = json.loads(json.dumps(self.manifest))
        manifest['integration']['branch'] = branch
        for stack in manifest['stacks']:
            stack['integration_branch'] = branch
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + '\n')
        return manifest

    def test_bundle_installs_primary_checkout_guards_and_reports_ready(self):
        result = syncwheel.install_managed_push_hook(self.repo, apply=True)
        self.assertTrue(result['ready'])
        self.assertEqual(
            set(result['hooks']),
            {'pre-push', 'pre-commit', 'post-checkout', 'reference-transaction'},
        )
        for hook in result['hooks'].values():
            self.assertTrue(hook['ready'])
            self.assertEqual(hook['status'], 'installed')
        reference_hook = syncwheel.managed_hook_paths(
            self.repo, 'reference-transaction'
        )[1]
        self.assertIn('\nset -u\n', reference_hook.read_text())

    def _install_and_branch(self, branch):
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        subprocess.run(
            ['git', 'checkout', '-qb', branch], cwd=self.repo,
            env=self._syncwheel_authorized_env(), check=True,
        )
        if (self.repo / '.gitignore').exists():
            subprocess.run(['git', 'add', '.gitignore'], cwd=self.repo, check=True)
            subprocess.run(
                ['git', 'commit', '-qm', 'syncwheel ignore rules'], cwd=self.repo,
                env=self._syncwheel_authorized_env(), check=True,
            )
        tips = []
        for name in ('one', 'two'):
            (self.repo / name).write_text(name + '\n')
            subprocess.run(['git', 'add', name], cwd=self.repo, check=True)
            subprocess.run(
                ['git', 'commit', '-qm', name], cwd=self.repo,
                env=self._syncwheel_authorized_env(), check=True,
            )
            tips.append(subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=self.repo, check=True,
                capture_output=True, text=True,
            ).stdout.strip())
        return tips

    def _clean_env(self):
        env = dict(self.hook_env)
        env.pop(syncwheel.MANAGED_REF_MOVE_AUTH_ENV, None)
        return env

    def _syncwheel_authorized_env(self):
        env = self._clean_env()
        env[syncwheel.MANAGED_REF_MOVE_AUTH_ENV] = syncwheel.authorize_ref_move(self.repo)
        return env

    def _reset_hard(self, target, env=None):
        return subprocess.run(
            ['git', 'reset', '--hard', target], cwd=self.repo,
            env=env if env is not None else self._clean_env(),
            capture_output=True, text=True,
        )

    def test_rewinding_a_managed_branch_is_refused(self):
        self._install_and_branch('main-integration')
        result = self._reset_hard('HEAD~1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing unauthorized primary integration ref move', result.stderr)
        self.assertEqual(
            subprocess.run(
                ['git', 'log', '-1', '--format=%s'], cwd=self.repo,
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            'two',
        )

    def test_primary_checkout_refuses_manual_integration_commit_with_named_remedies(self):
        self._install_and_branch('main-integration')
        (self.repo / 'three').write_text('three\n')
        subprocess.run(['git', 'add', 'three'], cwd=self.repo, check=True)
        result = subprocess.run(
            ['git', 'commit', '-qm', 'three'], cwd=self.repo,
            env=self._clean_env(), capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('primary checkout commit blocked', result.stderr)
        self.assertIn('syncwheel worktree open <lane> --into feature', result.stderr)
        self.assertIn('syncwheel stack capture-integration feature HEAD', result.stderr)
        self.assertNotIn('.. Use:', result.stderr)

    def test_syncwheel_authorization_permits_primary_control_commit(self):
        self._install_and_branch('main-integration')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        subprocess.run(['git', 'add', '.syncwheel/manifest.json'], cwd=self.repo, check=True)
        result = subprocess.run(
            ['git', 'commit', '-qm', 'syncwheel control'], cwd=self.repo,
            env=self._syncwheel_authorized_env(), capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_syncwheel_authorization_permits_a_rewind(self):
        self._install_and_branch('main-integration')
        env = dict(self.hook_env)
        env.pop(syncwheel.MANAGED_REF_MOVE_AUTH_ENV, None)
        env[syncwheel.MANAGED_REF_MOVE_AUTH_ENV] = syncwheel.authorize_ref_move(self.repo)
        result = self._reset_hard('HEAD~1', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_guard_fails_closed_when_its_cli_cannot_run(self):
        self._install_and_branch('main-integration')
        env = self._clean_env()
        (self.bin_dir / 'syncwheel').unlink()
        env['PATH'] = '/usr/bin:/bin'
        result = subprocess.run(
            ['git', 'commit', '-q', '--allow-empty', '-m', 'must be guarded'],
            cwd=self.repo, env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('syncwheel guard degraded: stable syncwheel CLI is unavailable', result.stderr)

    def test_installed_guard_does_not_fall_back_to_path_when_its_cli_is_missing(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        switched = subprocess.run(
            ['git', 'switch', '-c', 'feature'], cwd=self.repo,
            env=self.hook_env, capture_output=True, text=True,
        )
        self.assertNotEqual(switched.returncode, 0)
        (self.bin_dir / 'syncwheel').unlink()
        result = subprocess.run(
            ['git', 'commit', '-q', '--allow-empty', '-m', 'must be guarded'],
            cwd=self.repo, env=self.hook_env, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('syncwheel guard degraded: stable syncwheel CLI is unavailable', result.stderr)

    def test_owned_ref_moves_stay_out_of_the_ambient_environment(self):
        # Authorization must reach spawned Git only; leaking it into os.environ
        # would silently disarm the guard for everything else in the process.
        self.assertNotIn(syncwheel.MANAGED_REF_MOVE_AUTH_ENV, os.environ)

    def _seed_remote(self):
        """A separate repository with its own commit, so a fetch moves refs."""
        remote = self.temp_root / 'remote'
        subprocess.run(['git', 'init', '-q', str(remote)], check=True)
        subprocess.run(['git', 'config', 'user.name', 'Remote'], cwd=remote, check=True)
        subprocess.run(
            ['git', 'config', 'user.email', 'remote@example.invalid'], cwd=remote, check=True
        )
        (remote / 'upstream').write_text('upstream\n')
        subprocess.run(['git', 'add', 'upstream'], cwd=remote, check=True)
        subprocess.run(['git', 'commit', '-qm', 'upstream'], cwd=remote, check=True)
        subprocess.run(['git', 'branch', '-M', 'upstream-main'], cwd=remote, check=True)
        subprocess.run(
            ['git', 'remote', 'add', 'origin', str(remote)], cwd=self.repo, check=True
        )
        return remote

    def _run_git(self, *args, cwd=None):
        return subprocess.run(
            ['git', *args], cwd=cwd or self.repo, env=self._clean_env(),
            capture_output=True, text=True,
        )

    def _assert_guard_still_protects_its_surface(self, cause):
        old, new = self.descendant_commit('rejected while degraded')
        moved = self._run_git('update-ref', 'refs/heads/main-integration', new, old)
        self.assertNotEqual(moved.returncode, 0)
        self.assertIn('refs/heads/main-integration', moved.stderr)
        self.assertIn('syncwheel hooks install --apply', moved.stderr)
        self.assertIn(cause, moved.stderr)
        self.assertEqual(
            subprocess.check_output(
                ['git', 'rev-parse', 'main-integration'], cwd=self.repo, text=True
            ).strip(),
            old,
        )

        (self.repo / 'manual').write_text('manual\n')
        subprocess.run(['git', 'add', 'manual'], cwd=self.repo, check=True)
        committed = self._run_git('commit', '-qm', 'manual')
        self.assertNotEqual(committed.returncode, 0)
        self.assertIn('primary checkout commit blocked', committed.stderr)
        self.assertIn(cause, committed.stderr)

        authorized = subprocess.run(
            ['git', 'commit', '-qm', 'syncwheel control'], cwd=self.repo,
            env=self._syncwheel_authorized_env(), capture_output=True, text=True,
        )
        self.assertEqual(authorized.returncode, 0, authorized.stderr)

    def _assert_unguarded_transactions_pass(self):
        fetched = self._run_git('fetch', 'origin')
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(
            subprocess.run(
                ['git', 'rev-parse', '--verify', 'refs/remotes/origin/upstream-main'],
                cwd=self.repo, capture_output=True, text=True,
            ).returncode,
            0,
        )

        # A relative path is the shape that made the guard build a concatenated
        # path from the not-yet-created worktree.
        added = self._run_git('worktree', 'add', '--detach', 'var/worktrees/probe', 'HEAD')
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertTrue((self.repo / 'var' / 'worktrees' / 'probe').is_dir())

        tagged = self._run_git('tag', 'probe-tag')
        self.assertEqual(tagged.returncode, 0, tagged.stderr)

    def test_fetch_and_worktree_creation_survive_a_missing_guard_configuration(self):
        self._install_and_branch('main-integration')
        self._seed_remote()
        syncwheel.primary_guard_path(self.repo).unlink()

        self._assert_unguarded_transactions_pass()
        self._assert_guard_still_protects_its_surface('configuration is missing')

    def test_fetch_and_worktree_creation_survive_a_corrupt_guard_configuration(self):
        cases = (
            ('{"enabled": true, "integ', 'invalid JSON'),
            ('{"version": 1, "enabled": true}\n', 'non-empty integrationBranch'),
        )
        for index, (content, cause) in enumerate(cases):
            with self.subTest(cause=cause):
                if index:
                    self.tearDown()
                    self.setUp()
                self._install_and_branch('main-integration')
                self._seed_remote()
                syncwheel.primary_guard_path(self.repo).write_text(content)

                self._assert_unguarded_transactions_pass()
                self._assert_guard_still_protects_its_surface(cause)

    def test_armed_guard_leaves_fetch_and_worktree_creation_alone(self):
        # The regression this module exists for, asserted in the armed state the
        # repositories actually run in.
        self._install_and_branch('main-integration')
        self._seed_remote()

        self._assert_unguarded_transactions_pass()

        subprocess.run(['git', 'branch', 'scratch'], cwd=self.repo, check=True)
        moved = self._run_git('branch', '-f', 'scratch', 'HEAD~1')
        self.assertEqual(moved.returncode, 0, moved.stderr)

        old, new = self.descendant_commit('still refused while armed')
        refused = self._run_git('update-ref', 'refs/heads/main-integration', new, old)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('refusing unauthorized primary integration ref move', refused.stderr)

    def test_degraded_guard_refuses_other_branches_and_names_the_repair(self):
        # Nothing left distinguishes the integration ref from any other branch,
        # so every branch ref is refused; the refusal has to say how to fix it.
        self._install_and_branch('main-integration')
        subprocess.run(['git', 'branch', 'scratch'], cwd=self.repo, check=True)
        syncwheel.primary_guard_path(self.repo).unlink()

        moved = self._run_git('branch', '-f', 'scratch', 'HEAD~1')

        self.assertNotEqual(moved.returncode, 0)
        self.assertIn('configuration is missing', moved.stderr)
        self.assertIn('syncwheel hooks install --apply', moved.stderr)
        self.assertEqual(
            subprocess.check_output(
                ['git', 'rev-parse', 'scratch'], cwd=self.repo, text=True
            ).strip(),
            subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True
            ).strip(),
        )

    def _park_primary_off_integration(self):
        subprocess.run(
            ['git', '-c', 'core.hooksPath=/dev/null', 'checkout', '-q', '-b', 'parked'],
            cwd=self.repo, check=True,
        )
        subprocess.run(
            ['git', '-c', 'core.hooksPath=/dev/null', 'branch', '-f',
             'main-integration', 'HEAD~1'],
            cwd=self.repo, check=True,
        )

    def _assert_integration_ref_still_refused(self):
        before = subprocess.check_output(
            ['git', 'rev-parse', 'main-integration'], cwd=self.repo, text=True
        ).strip()
        head = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True
        ).strip()
        moved = self._run_git('update-ref', 'refs/heads/main-integration', head, before)

        self.assertNotEqual(moved.returncode, 0, moved.stdout)
        self.assertEqual(
            subprocess.check_output(
                ['git', 'rev-parse', 'main-integration'], cwd=self.repo, text=True
            ).strip(),
            before,
        )

    def _make_journal_manifest(self):
        manifest = json.loads(json.dumps(self.manifest))
        manifest.pop('integration', None)
        manifest['repository_mode'] = 'journal'
        manifest['stacks'] = []
        manifest['journal'] = {
            'branch': 'journal', 'remote': 'origin',
            'include': ['**'], 'exclude': ['.git/**'],
            'max_file_bytes': 10485760, 'interval': '30m',
        }
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(manifest, indent=2) + '\n'
        )

    def test_degraded_guard_protects_a_journal_repository_integration_ref(self):
        # A journal manifest declares no integration branch at all, so nothing
        # in the tree can name the ref guard.json was protecting.
        self._install_and_branch('main-integration')
        self._make_journal_manifest()
        subprocess.run(
            ['git', '-c', 'core.hooksPath=/dev/null', 'checkout', '-q', '-b', 'journal'],
            cwd=self.repo, check=True,
        )
        subprocess.run(
            ['git', '-c', 'core.hooksPath=/dev/null', 'branch', '-f',
             'main-integration', 'HEAD~1'],
            cwd=self.repo, check=True,
        )
        syncwheel.primary_guard_path(self.repo).unlink()

        self._assert_integration_ref_still_refused()

    def test_degraded_guard_protects_the_integration_ref_without_a_manifest(self):
        # The manifest normally lives on the integration branch; park the primary
        # elsewhere and there is nothing left to read.
        self._install_and_branch('main-integration')
        self._park_primary_off_integration()
        shutil.rmtree(self.repo / '.syncwheel')
        syncwheel.primary_guard_path(self.repo).unlink()

        self._assert_integration_ref_still_refused()

    def test_degraded_guard_protects_the_integration_ref_with_a_broken_manifest(self):
        self._install_and_branch('main-integration')
        self._park_primary_off_integration()
        (self.repo / '.syncwheel' / 'manifest.json').write_text('{ <<<<<<< HEAD\n')
        syncwheel.primary_guard_path(self.repo).unlink()

        self._assert_integration_ref_still_refused()

    def test_degraded_primary_refuses_a_manual_commit_off_the_integration_branch(self):
        # The primary is the shared projection whatever branch it is parked on,
        # and a degraded guard cannot prove otherwise.
        self._install_and_branch('main-integration')
        self._park_primary_off_integration()
        shutil.rmtree(self.repo / '.syncwheel')
        syncwheel.primary_guard_path(self.repo).unlink()
        (self.repo / 'manual').write_text('manual\n')
        subprocess.run(['git', 'add', 'manual'], cwd=self.repo, check=True)

        blocked = self._run_git('commit', '-qm', 'manual')

        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn('primary checkout commit blocked', blocked.stderr)
        self.assertIn('syncwheel hooks install --apply', blocked.stderr)
        authorized = subprocess.run(
            ['git', 'commit', '-qm', 'syncwheel control'], cwd=self.repo,
            env=self._syncwheel_authorized_env(), capture_output=True, text=True,
        )
        self.assertEqual(authorized.returncode, 0, authorized.stderr)

    def test_ref_authorization_directory_ignores_the_ambient_repository(self):
        # It decides where the nonce that permits a ref move is written, so it
        # has to resolve the same repository every other guard lookup does.
        other = self.temp_root / 'other'
        subprocess.run(['git', 'init', '-q', str(other)], check=True)
        expected = syncwheel.ref_auth_dir(self.repo)

        with mock.patch.dict(
            os.environ,
            {'GIT_DIR': str(other / '.git'), 'GIT_COMMON_DIR': str(other / '.git')},
            clear=False,
        ):
            self.assertEqual(syncwheel.ref_auth_dir(self.repo), expected)

    def test_guard_children_do_not_inherit_the_ambient_repository_environment(self):
        # Git exports these to its hooks. A child Git that inherits them ignores
        # the repository the guard named and works on the caller's instead.
        leaked = {
            'GIT_DIR': str(self.repo / '.git'),
            'GIT_WORK_TREE': 'var/worktrees/other',
            'GIT_INDEX_FILE': str(self.repo / '.git' / 'index'),
            'GIT_COMMON_DIR': str(self.repo / '.git'),
            'GIT_PREFIX': 'var/',
            'GIT_NAMESPACE': 'leaked',
        }
        with mock.patch.dict(os.environ, leaked, clear=False):
            environment = syncwheel.managed_process_env()
            override = syncwheel.managed_process_env({'GIT_INDEX_FILE': '/tmp/explicit'})

        for name in leaked:
            self.assertNotIn(name, environment)
        self.assertEqual(override['GIT_INDEX_FILE'], '/tmp/explicit')

    def test_merge_inside_a_linked_worktree_is_left_alone_by_the_guard(self):
        self._install_and_branch('main-integration')
        subprocess.run(
            ['git', 'add', '-f', '.syncwheel/manifest.json'], cwd=self.repo, check=True
        )
        subprocess.run(
            ['git', 'commit', '-qm', 'syncwheel manifest'], cwd=self.repo,
            env=self._syncwheel_authorized_env(), check=True,
        )
        opened = self.run_syncwheel(
            'worktree', 'open', 'lane1', '--into', 'feature', '--repo', str(self.repo)
        )
        self.assertEqual(opened.returncode, 0, opened.stderr)

        worktree = self.temp_root / 'linked'
        added = self._run_git('worktree', 'add', '--detach', str(worktree), 'HEAD')
        self.assertEqual(added.returncode, 0, added.stderr)
        for branch, name in (
            ('left-one', 'left-one.txt'),
            ('left-two', 'left-two.txt'),
            ('right', 'right.txt'),
        ):
            switched = self._run_git(
                'checkout', '-q', '-B', branch, 'main-integration', cwd=worktree
            )
            self.assertEqual(switched.returncode, 0, switched.stderr)
            (worktree / name).write_text(name + '\n')
            subprocess.run(['git', 'add', name], cwd=worktree, check=True)
            committed = self._run_git('commit', '-qm', name, cwd=worktree)
            self.assertEqual(committed.returncode, 0, committed.stderr)
        # save_state() only runs when the tree is not clean, and it is the step
        # that failed once the guard had rewritten this worktree's index.
        (worktree / 'untracked.txt').write_text('untracked\n')

        for branch in ('left-one', 'left-two'):
            merged = self._run_git('merge', '--no-edit', branch, cwd=worktree)
            self.assertEqual(merged.returncode, 0, merged.stderr)
            self.assertNotIn('stash failed', merged.stderr)

        # ORIG_HEAD is one of the transactions a merge makes on its own, and
        # "git stash create" is the step whose failure the merge reports as
        # "fatal: stash failed".
        refreshed = self._run_git('update-ref', 'ORIG_HEAD', 'HEAD', cwd=worktree)
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        stash = self._run_git('stash', 'create', cwd=worktree)

        self.assertEqual(stash.returncode, 0, stash.stderr)

    def test_ref_guard_leaves_transactions_outside_its_surface_alone(self):
        self._install_and_branch('main-integration')
        syncwheel.primary_guard_path(self.repo).unlink()
        payload = ''.join(
            f"{'a' * 40} {'b' * 40} {ref}\n" for ref in (
                'refs/remotes/origin/main',
                'refs/tags/v1',
                'refs/notes/commits',
                'HEAD',
                'ORIG_HEAD',
                'refs/stash',
                'refs/bisect/bad',
            )
        )
        warnings = io.StringIO()
        with mock.patch('sys.stdin', io.StringIO(payload)):
            with mock.patch('sys.stderr', warnings):
                self.assertEqual(
                    syncwheel.command_hooks_ref_guard(
                        types.SimpleNamespace(repo=self.repo, phase='prepared')
                    ),
                    0,
                )

        self.assertEqual(warnings.getvalue(), '')

    def test_unmanaged_branch_rewind_is_untouched(self):
        # Moved with "git branch -f" so the primary-checkout guard, which owns
        # which branch may be checked out, stays out of this assertion.
        self._install_and_branch('main-integration')
        subprocess.run(['git', 'branch', 'scratch'], cwd=self.repo, check=True)
        result = subprocess.run(
            ['git', 'branch', '-f', 'scratch', 'HEAD~1'], cwd=self.repo,
            env=self._clean_env(), capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rewinding_a_managed_stack_branch_is_refused(self):
        self._install_and_branch('main-integration')
        subprocess.run(['git', 'branch', 'pr/feature'], cwd=self.repo, check=True)
        result = subprocess.run(
            ['git', 'branch', '-f', 'pr/feature', 'HEAD~1'], cwd=self.repo,
            env=self._clean_env(), capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_managed_hook_callbacks_skip_startup_update_policy(self):
        callbacks = {
            'pre-commit': [
                'hooks', 'worktree-guard', '--event', 'pre-commit',
                '--repo', str(self.repo),
            ],
            'post-checkout': [
                'hooks', 'worktree-guard', '--event', 'post-checkout',
                '--repo', str(self.repo),
            ],
            'pre-push': [
                'hooks', 'guard', '--remote-name', 'origin',
                '--remote-url', 'example.invalid/repo', '--repo', str(self.repo),
            ],
        }
        for callback, argv in callbacks.items():
            with (
                self.subTest(callback=callback),
                mock.patch.object(os.sys, 'argv', [str(MODULE_PATH), *argv]),
                mock.patch.object(syncwheel, 'maybe_handle_startup_update_policy') as updater,
                mock.patch.object(syncwheel, 'execute_parsed_command', return_value=0) as execute,
            ):
                self.assertEqual(syncwheel.main(), 0)
                updater.assert_not_called()
                execute.assert_called_once()

    def test_managed_hook_callbacks_are_hermetic_without_update_state_or_network(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        bundle_before = self.hook_bundle_bytes()

        restricted_bin = self.repo / '.restricted-bin'
        restricted_bin.mkdir()
        network_log = self.repo / '.network-attempts'
        real_git = shutil.which('git')
        self.assertIsNotNone(real_git)
        fake_git = restricted_bin / 'git'
        fake_git.write_text(
            '#!/bin/sh\n'
            'case "${1:-}" in\n'
            '  fetch|ls-remote|push)\n'
            '    printf "%s\\n" "$*" >>"$SYNCWHEEL_TEST_NETWORK_LOG"\n'
            '    exit 97\n'
            '    ;;\n'
            'esac\n'
            f'exec {shlex.quote(real_git)} "$@"\n'
        )
        fake_git.chmod(0o755)
        for executable in ('cat', 'dirname', 'mktemp', 'rm'):
            target = shutil.which(executable)
            self.assertIsNotNone(target)
            (restricted_bin / executable).symlink_to(target)
        self.assertIsNone(shutil.which('syncwheel', path=str(restricted_bin)))

        restricted_home = self.repo / '.restricted-home'
        restricted_home.mkdir()
        poison_path = self.repo / '.network-poison'
        poison_path.mkdir()
        (poison_path / 'sitecustomize.py').write_text(
            'import os\n'
            'from pathlib import Path\n'
            'import socket\n'
            'import urllib.request\n'
            'def blocked(*args, **kwargs):\n'
            '    Path(os.environ["SYNCWHEEL_TEST_NETWORK_LOG"]).write_text("python-network\\n")\n'
            '    raise RuntimeError("network access is forbidden in managed hooks")\n'
            'socket.create_connection = blocked\n'
            'urllib.request.urlopen = blocked\n'
        )
        state_path = restricted_home / 'update-state.json'
        settings_path = restricted_home / 'settings.json'
        environment = os.environ.copy()
        environment.update({
            'HOME': str(restricted_home),
            'PATH': str(restricted_bin),
            'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONPATH': str(poison_path),
            'SYNCWHEEL_TEST_NETWORK_LOG': str(network_log),
            syncwheel.MANAGED_REF_MOVE_AUTH_ENV: syncwheel.authorize_ref_move(self.repo),
            syncwheel.ENV_UPDATE_INTERVAL_SECONDS: '0',
            syncwheel.ENV_UPDATE_MODE: 'auto',
            syncwheel.ENV_UPDATE_SETTINGS_PATH: str(settings_path),
            syncwheel.ENV_UPDATE_STATE_PATH: str(state_path),
        })

        head = subprocess.check_output(
            [real_git, 'rev-parse', 'HEAD'], cwd=self.repo, text=True
        ).strip()
        callbacks = {
            'pre-commit': ([], ''),
            'post-checkout': ([head, head, '1'], ''),
            'pre-push': (
                ['origin', 'example.invalid/repo'],
                f'HEAD {head} refs/heads/scratch {"0" * 40}\n',
            ),
        }
        for hook_name, (argv, payload) in callbacks.items():
            hook = syncwheel.managed_hook_paths(self.repo, hook_name)[1]
            result = subprocess.run(
                [str(hook), *argv],
                cwd=self.repo,
                env=environment,
                input=payload,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, f'{hook_name}: {result.stderr}')

        self.assertFalse(network_log.exists())
        self.assertFalse(settings_path.exists())
        self.assertFalse(state_path.exists())
        self.assertEqual(self.hook_bundle_bytes(), bundle_before)

    def test_primary_checkout_switch_warns_and_pre_commit_blocks(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        syncwheel.install_managed_push_hook(self.repo, apply=True)

        switched = subprocess.run(
            ['git', 'switch', '-c', 'feature'], cwd=self.repo,
            env=self.hook_env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(switched.returncode, 0)
        self.assertIn('branch mismatch detected after checkout', switched.stderr)
        self.assertNotIn('.. Use:', switched.stderr)
        self.assertEqual(
            subprocess.check_output(['git', 'branch', '--show-current'], cwd=self.repo, text=True).strip(),
            'feature',
        )

        (self.repo / 'blocked').write_text('blocked\n')
        subprocess.run(['git', 'add', 'blocked'], cwd=self.repo, check=True)
        committed = subprocess.run(
            ['git', 'commit', '-m', 'must not commit'], cwd=self.repo,
            env=self.hook_env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(committed.returncode, 0)
        self.assertIn('commit blocked', committed.stderr)
        self.assertIn('syncwheel stack capture-integration feature HEAD', committed.stderr)

    def test_feature_worktree_commit_remains_allowed(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        subprocess.run(['git', 'add', '.syncwheel/manifest.json'], cwd=self.repo, check=True)
        subprocess.run(['git', 'commit', '-qm', 'manifest'], cwd=self.repo, check=True)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        feature = self.repo.parent / f'{self.repo.name}-feature-wt'
        subprocess.run(
            ['git', 'worktree', 'add', '-b', 'feature', str(feature), 'HEAD'],
            cwd=self.repo, env=self.hook_env, check=True,
        )
        try:
            (feature / 'allowed').write_text('allowed\n')
            subprocess.run(['git', 'add', 'allowed'], cwd=feature, check=True)
            subprocess.run(
                ['git', 'commit', '-qm', 'allowed'], cwd=feature,
                env=self.hook_env, check=True,
            )
        finally:
            subprocess.run(['git', 'worktree', 'remove', str(feature)], cwd=self.repo, check=True)

    def test_required_hooks_remain_visible_without_auto_install(self):
        policy = syncwheel.ensure_managed_repository_hooks(self.repo, self.manifest)
        self.assertFalse(policy['ready'])
        self.assertTrue(policy['required'])

    def test_tracking_status_does_not_rewrite_missing_required_hooks(self):
        first = self.run_syncwheel(
            'repo', 'tracking', 'status', '--repo', str(self.repo), '--json'
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['syncwheel_tracking'], 'git-tracked')
        first_bundle = self.hook_bundle_bytes()
        self.assertEqual(first_bundle, {})

        second = self.run_syncwheel(
            'repo', 'tracking', 'status', '--repo', str(self.repo), '--json'
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.hook_bundle_bytes(), first_bundle)

    def test_hook_lifecycle_status_can_observe_an_uninstalled_bundle(self):
        result = self.run_syncwheel('hooks', 'status', '--repo', str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['ready'])
        self.assertEqual(self.hook_bundle_bytes(), {})

    def test_tracking_set_git_tracked_requires_explicit_hook_install(self):
        self.manifest['syncwheel_tracking'] = 'local-only'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(self.manifest, indent=2) + '\n'
        )

        result = self.run_syncwheel(
            'repo', 'tracking', 'set', 'git-tracked',
            '--repo', str(self.repo), '--apply', '--json',
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_normal_command_respects_local_only_and_reasoned_disable(self):
        self.manifest['syncwheel_tracking'] = 'local-only'
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        local_only = self.run_syncwheel(
            'repo', 'tracking', 'status', '--repo', str(self.repo), '--json'
        )
        self.assertEqual(local_only.returncode, 0, local_only.stderr)
        self.assertEqual(self.hook_bundle_bytes(), {})

        self.manifest['syncwheel_tracking'] = 'git-tracked'
        manifest_path.write_text(json.dumps(self.manifest, indent=2) + '\n')
        syncwheel.remove_managed_push_hook(
            self.repo, apply=True, disable=True, reason='explicit test opt-out'
        )
        disabled_before = self.hook_bundle_bytes()
        disabled = self.run_syncwheel(
            'repo', 'tracking', 'status', '--repo', str(self.repo), '--json'
        )
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertEqual(self.hook_bundle_bytes(), disabled_before)

    def test_missing_required_bundle_stays_pending_until_explicit_install(self):
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        syncwheel.primary_guard_path(self.repo).unlink()

        before = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertFalse(before['ready'])
        self.assertTrue(before['migrationPending'])
        self.assertTrue(before['degraded'])
        self.assertIn('configuration is missing', before['degradedCauses'][0])

        after = syncwheel.ensure_managed_repository_hooks(self.repo, self.manifest)
        self.assertTrue(after['migrationPending'])
        self.assertFalse(after['enforced'])

    def test_install_is_plan_first_idempotent_and_restores_existing_hook(self):
        subprocess.run(
            ['git', 'config', 'core.hooksPath', '.custom-hooks'], cwd=self.repo, check=True
        )
        hooks, hook, backup, _ = syncwheel.managed_push_hook_paths(self.repo)
        self.assertEqual(hooks, self.repo / '.custom-hooks')
        hooks.mkdir(parents=True, exist_ok=True)
        hook.write_text('#!/bin/sh\necho existing\n')
        hook.chmod(0o755)

        plan = syncwheel.install_managed_push_hook(self.repo)
        self.assertEqual(plan['action'], 'install')
        self.assertTrue(plan['chainExisting'])
        self.assertEqual(hook.read_text(), '#!/bin/sh\necho existing\n')

        installed = syncwheel.install_managed_push_hook(self.repo, apply=True)
        self.assertEqual(installed['status'], 'installed')
        self.assertTrue(backup.exists())
        self.assertEqual(syncwheel.install_managed_push_hook(self.repo, apply=True)['action'], 'none')

        removal = syncwheel.remove_managed_push_hook(self.repo)
        self.assertEqual(removal['action'], 'remove')
        syncwheel.remove_managed_push_hook(self.repo, apply=True, disable=True, reason='test cleanup')
        self.assertEqual(hook.read_text(), '#!/bin/sh\necho existing\n')
        self.assertFalse(backup.exists())

    def test_remove_refuses_modified_owned_hook(self):
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        _, hook, _, _ = syncwheel.managed_push_hook_paths(self.repo)
        hook.write_text(hook.read_text() + '# modified\n')
        with self.assertRaisesRegex(syncwheel.SyncwheelError, 'not owned'):
            syncwheel.remove_managed_push_hook(self.repo, apply=True, disable=True, reason='test')
        with self.assertRaisesRegex(syncwheel.SyncwheelError, 'stale or tampered'):
            syncwheel.install_managed_push_hook(self.repo, apply=True)

    def test_bundle_preflight_does_not_partially_install_on_conflict(self):
        hooks, pre_commit, backup, _, _ = syncwheel.managed_hook_paths(
            self.repo, 'pre-commit'
        )
        hooks.mkdir(parents=True, exist_ok=True)
        pre_commit.write_text('#!/bin/sh\necho foreign\n')
        backup.write_text('#!/bin/sh\necho retained\n')

        with self.assertRaisesRegex(syncwheel.SyncwheelError, 'chaining conflict'):
            syncwheel.install_managed_push_hook(self.repo, apply=True)
        self.assertFalse((hooks / 'pre-push').exists())
        self.assertEqual(pre_commit.read_text(), '#!/bin/sh\necho foreign\n')

    def test_changed_chained_hook_is_rebaselined_without_overwrite(self):
        _, hook, backup, _ = syncwheel.managed_push_hook_paths(self.repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('#!/bin/sh\necho existing\n')
        hook.chmod(0o755)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        backup.write_text('#!/bin/sh\necho replaced\n')

        status = syncwheel.managed_push_hook_status(self.repo)
        self.assertFalse(status['owned'])
        self.assertFalse(status['chainMatches'])
        self.assertTrue(status['chainRepairable'])
        self.assertEqual(status['status'], 'conflict')

        with self.assertRaisesRegex(syncwheel.SyncwheelError, 'not owned'):
            syncwheel.remove_managed_push_hook(self.repo, apply=True, disable=True, reason='test')

        plan = syncwheel.install_managed_push_hook(self.repo)
        self.assertEqual(
            plan['hooks']['pre-push']['action'], 'refresh-chain-metadata'
        )
        self.assertEqual(backup.read_text(), '#!/bin/sh\necho replaced\n')

        repaired = syncwheel.install_managed_push_hook(self.repo, apply=True)
        self.assertTrue(repaired['ready'])
        self.assertTrue(syncwheel.managed_push_hook_status(self.repo)['chainMatches'])
        syncwheel.remove_managed_push_hook(
            self.repo, apply=True, disable=True, reason='test cleanup'
        )
        self.assertEqual(hook.read_text(), '#!/bin/sh\necho replaced\n')

    def test_required_policy_migrates_then_reports_degraded_on_tamper(self):
        pending = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertTrue(pending['required'])
        self.assertTrue(pending['migrationPending'])
        self.assertFalse(pending['enforced'])

        syncwheel.install_managed_push_hook(self.repo, apply=True)
        active = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertTrue(active['enforced'])
        self.assertTrue(active['ready'])

        _, hook, _, _ = syncwheel.managed_push_hook_paths(self.repo)
        hook.write_text(hook.read_text() + '# tampered\n')
        degraded = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertEqual(degraded['status'], 'degraded')
        self.assertEqual(degraded['mode'], 'required-degraded')
        self.assertFalse(degraded['ready'])

    def test_reasoned_disable_is_persisted_and_visible(self):
        disabled = syncwheel.remove_managed_push_hook(
            self.repo, apply=True, disable=True, reason='external contribution clone'
        )
        self.assertEqual(disabled['action'], 'disable')
        policy = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertTrue(policy['disabled'])
        self.assertEqual(policy['disabledReason'], 'external contribution clone')

    def test_reasoned_disable_allows_a_manual_primary_commit_and_is_visible_in_status(self):
        self._install_and_branch('main-integration')
        disabled = self.run_syncwheel(
            'hooks', 'remove', '--disable', '--reason', 'deliberate recovery',
            '--apply', '--repo', str(self.repo),
        )
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        (self.repo / 'manual').write_text('manual\n')
        subprocess.run(['git', 'add', 'manual'], cwd=self.repo, check=True)
        committed = subprocess.run(
            ['git', 'commit', '-qm', 'deliberate manual recovery'], cwd=self.repo,
            env=self._clean_env(), capture_output=True, text=True,
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)
        status = self.run_syncwheel('hooks', 'status', '--repo', str(self.repo))
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertTrue(report['disabled'])
        self.assertEqual(report['disabledReason'], 'deliberate recovery')

    def test_dirty_primary_blocks_mutation_but_read_only_commands_warn(self):
        self._install_and_branch('main-integration')
        (self.repo / 'seed').write_text('changed\n')
        manifest_before = (self.repo / '.syncwheel' / 'manifest.json').read_text()

        mutation = self.run_syncwheel(
            'stack', 'set', 'feature', 'HEAD', '--repo', str(self.repo),
        )

        self.assertEqual(mutation.returncode, 2)
        self.assertIn('primary checkout is dirty: 1 tracked file', mutation.stderr)
        self.assertIn('not owned by the current user', mutation.stderr)
        self.assertIn('syncwheel worktree open <lane> --into feature', mutation.stderr)
        self.assertIn('syncwheel stack capture-integration feature HEAD', mutation.stderr)
        self.assertEqual((self.repo / '.syncwheel' / 'manifest.json').read_text(), manifest_before)

        status = self.run_syncwheel('status', '--repo', str(self.repo))
        self.assertEqual(status.returncode, 0, status.stderr)
        passthrough_status = self.run_syncwheel(
            'int', 'git', '--repo', str(self.repo), '--', 'status', '--short',
        )
        self.assertEqual(passthrough_status.returncode, 0, passthrough_status.stderr)
        passthrough_mutation = self.run_syncwheel(
            'int', 'git', '--repo', str(self.repo), '--', 'add', 'seed',
        )
        self.assertEqual(passthrough_mutation.returncode, 2)
        self.assertIn('syncwheel stack capture-integration feature HEAD', passthrough_mutation.stderr)
        self.assertEqual(
            subprocess.run(
                ['git', 'diff', '--cached', '--name-only'], cwd=self.repo,
                capture_output=True, text=True, check=True,
            ).stdout,
            '',
        )
        self.assertEqual(
            syncwheel.primary_checkout_dirty_warning_lines(self.repo, self.manifest),
            ['primary checkout is dirty: 1 tracked file not owned by the current user'],
        )
        tty_stderr = io.StringIO()
        tty_stderr.isatty = lambda: True
        with mock.patch.object(syncwheel.sys, 'stderr', tty_stderr):
            syncwheel.emit_primary_checkout_dirty_warnings(self.repo, self.manifest)
        self.assertIn(
            'WARNING: primary checkout is dirty: 1 tracked file not owned by the current user',
            tty_stderr.getvalue(),
        )

    def test_repo_local_syncwheel_state_is_not_primary_checkout_dirt(self):
        subprocess.run(
            ['git', 'add', '.syncwheel/manifest.json'], cwd=self.repo, check=True
        )
        subprocess.run(
            ['git', 'commit', '-qm', 'track the manifest'], cwd=self.repo, check=True
        )
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest_path.write_text(manifest_path.read_text() + '\n')

        self.assertEqual(syncwheel.primary_checkout_dirty_entries(self.repo), [])
        self.assertEqual(
            syncwheel.primary_checkout_dirty_warning_lines(self.repo, self.manifest), []
        )

        (self.repo / 'seed').write_text('changed\n')

        self.assertEqual(
            syncwheel.primary_checkout_dirty_warning_lines(self.repo, self.manifest),
            ['primary checkout is dirty: 1 tracked file not owned by the current user'],
        )

    def test_manifest_a_command_wrote_does_not_refuse_the_next_mutation(self):
        subprocess.run(
            ['git', 'add', '.syncwheel/manifest.json'], cwd=self.repo, check=True
        )
        subprocess.run(
            ['git', 'commit', '-qm', 'track the manifest'], cwd=self.repo, check=True
        )

        first = self.run_syncwheel(
            'stack', 'set', 'feature', 'HEAD', '--repo', str(self.repo),
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(
            subprocess.run(
                ['git', 'status', '--porcelain', '--', '.syncwheel'], cwd=self.repo,
                capture_output=True, text=True, check=True,
            ).stdout,
            ' M .syncwheel/manifest.json\n',
        )

        second = self.run_syncwheel(
            'stack', 'set', 'feature', 'HEAD', '--repo', str(self.repo),
        )

        self.assertEqual(second.returncode, 0, second.stderr)

        (self.repo / 'seed').write_text('changed\n')
        blocked = self.run_syncwheel(
            'stack', 'set', 'feature', 'HEAD', '--repo', str(self.repo),
        )

        self.assertEqual(blocked.returncode, 2)
        self.assertIn('primary checkout is dirty: 1 tracked file', blocked.stderr)

    def test_reasoned_disable_lifts_the_dirty_primary_refusal(self):
        self._install_and_branch('main-integration')
        (self.repo / 'seed').write_text('changed\n')

        blocked = self.run_syncwheel(
            'stack', 'set', 'feature', 'HEAD', '--repo', str(self.repo),
        )

        self.assertEqual(blocked.returncode, 2)
        self.assertIn('primary checkout is dirty', blocked.stderr)

        disabled = self.run_syncwheel(
            'hooks', 'remove', '--disable', '--reason', 'external contribution clone',
            '--apply', '--repo', str(self.repo),
        )

        self.assertEqual(disabled.returncode, 0, disabled.stderr)

        allowed = self.run_syncwheel(
            'stack', 'set', 'feature', 'HEAD', '--repo', str(self.repo),
        )

        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_guard_blocks_all_push_forms_targeting_managed_ref(self):
        zero = '0' * 40
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True).strip()
        cases = [
            f'HEAD {head} refs/heads/main-integration {zero}\n',
            f'refs/heads/alias {head} refs/heads/main-integration {zero}\n',
            f'(delete) {zero} refs/heads/main-integration {head}\n',
            f'HEAD {head} refs/heads/pr/feature {zero}\n',
        ]
        for payload in cases:
            with self.subTest(payload=payload), mock.patch('sys.stdin', io.StringIO(payload)):
                with self.assertRaisesRegex(syncwheel.SyncwheelError, 'raw git push blocked'):
                    syncwheel.command_hooks_guard(self.args())

    def test_guard_blocks_raw_push_to_the_delivery_branch(self):
        zero = '0' * 40
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True).strip()
        with mock.patch('sys.stdin', io.StringIO(f'HEAD {head} refs/heads/main {zero}\n')):
            with self.assertRaisesRegex(syncwheel.SyncwheelError, 'raw git push blocked.*stack land'):
                syncwheel.command_hooks_guard(self.args())

    def test_stack_land_authorization_permits_the_delivery_push(self):
        zero = '0' * 40
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True).strip()
        path, secret = syncwheel.authorize_syncwheel_push(self.repo, 'origin', ['refs/heads/main'])
        environment = {
            syncwheel.MANAGED_PUSH_AUTH_ENV: str(path),
            syncwheel.MANAGED_PUSH_SECRET_ENV: secret,
        }
        payload = f'HEAD {head} refs/heads/main {zero}\n'
        with mock.patch.dict(os.environ, environment), mock.patch('sys.stdin', io.StringIO(payload)):
            self.assertEqual(syncwheel.command_hooks_guard(self.args()), 0)

    def test_delivery_branch_is_guarded_but_not_managed(self):
        self.assertIn('refs/heads/main', syncwheel.delivery_ref_names(self.manifest))
        self.assertNotIn('refs/heads/main', syncwheel.managed_ref_names(self.manifest))

    def test_guard_allows_unmanaged_and_exact_single_use_authorization(self):
        zero = '0' * 40
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True).strip()
        unmanaged = f'HEAD {head} refs/heads/scratch {zero}\n'
        with mock.patch('sys.stdin', io.StringIO(unmanaged)):
            self.assertEqual(syncwheel.command_hooks_guard(self.args()), 0)

        managed = f'HEAD {head} refs/heads/main-integration {zero}\n'
        path, secret = syncwheel.authorize_syncwheel_push(
            self.repo, 'origin', ['refs/heads/main-integration']
        )
        environment = {
            syncwheel.MANAGED_PUSH_AUTH_ENV: str(path),
            syncwheel.MANAGED_PUSH_SECRET_ENV: secret,
        }
        with mock.patch.dict(os.environ, environment), mock.patch('sys.stdin', io.StringIO(managed)):
            self.assertEqual(syncwheel.command_hooks_guard(self.args()), 0)
        self.assertFalse(path.exists())
        with mock.patch.dict(os.environ, environment), mock.patch('sys.stdin', io.StringIO(managed)):
            with self.assertRaises(syncwheel.SyncwheelError):
                syncwheel.command_hooks_guard(self.args())

    def test_managed_refs_include_state_history_and_owned_journal(self):
        coordinated = json.loads(json.dumps(self.manifest))
        coordinated['coordination'] = {
            'mode': 'active-active', 'id': 'default', 'remote': 'origin',
            'state_branch': 'syncwheel/state/default', 'gc': {},
        }
        previous = {
            'state': {'managed_refs': {'refs/heads/retained/source': 'a' * 40}}
        }
        with mock.patch.object(syncwheel, 'read_remote_coordination_state', return_value=previous):
            refs = syncwheel.managed_push_refs(self.repo, coordinated)
        self.assertIn('refs/heads/main-integration', refs)
        self.assertIn('refs/heads/pr/feature', refs)
        self.assertIn('refs/heads/syncwheel/state/default', refs)
        self.assertIn('refs/heads/retained/source', refs)

        journal = {
            'repository_mode': 'journal',
            'journal': {'branch': 'journal/data'},
            'integration': {'branch': 'ignored'}, 'stacks': [],
            'coordination': {
                'mode': 'disabled', 'id': 'default', 'remote': 'origin',
                'state_branch': 'syncwheel/state/default', 'gc': {},
            },
        }
        self.assertIn('refs/heads/journal/data', syncwheel.managed_push_refs(self.repo, journal))

    def test_state_only_ref_error_names_historical_workflow(self):
        historical = 'refs/heads/retained/source'
        payload = f"HEAD {'a' * 40} {historical} {'0' * 40}\n"
        with mock.patch.object(
            syncwheel, 'managed_push_refs', return_value={historical}
        ), mock.patch('sys.stdin', io.StringIO(payload)):
            with self.assertRaises(syncwheel.SyncwheelError) as caught:
                syncwheel.command_hooks_guard(self.args())
        message = str(caught.exception)
        self.assertIn('syncwheel handoff', message)
        self.assertIn('historical-ref adoption/closure workflow', message)
        self.assertNotIn('syncwheel stack push', message)

    def test_guard_configuration_survives_a_missing_worktree_manifest(self):
        self._install_and_branch('main-integration')
        (self.repo / '.syncwheel' / 'manifest.json').unlink()
        result = subprocess.run(
            ['git', 'commit', '--allow-empty', '-m', 'manual'], cwd=self.repo,
            env=self._clean_env(), capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('primary checkout commit blocked', result.stderr)
        guard = syncwheel.load_primary_guard(self.repo)
        self.assertEqual(guard['integrationBranch'], 'main-integration')

    def test_ref_guard_blocks_an_unauthorized_fast_forward(self):
        self._install_and_branch('main-integration')
        payload = f"{'a' * 40} {'b' * 40} refs/heads/main-integration\n"
        with mock.patch('sys.stdin', io.StringIO(payload)):
            with self.assertRaisesRegex(syncwheel.SyncwheelError, 'unauthorized primary integration ref move'):
                syncwheel.command_hooks_ref_guard(types.SimpleNamespace(repo=self.repo, phase='prepared'))

    def test_installed_ref_guard_fails_closed_when_its_configuration_is_missing(self):
        self._install_and_branch('main-integration')
        syncwheel.primary_guard_path(self.repo).unlink()
        payload = f"{'a' * 40} {'b' * 40} refs/heads/main-integration\n"
        with mock.patch('sys.stdin', io.StringIO(payload)):
            with self.assertRaisesRegex(syncwheel.SyncwheelError, 'configuration is missing'):
                syncwheel.command_hooks_ref_guard(types.SimpleNamespace(repo=self.repo, phase='prepared'))

    def test_ref_guard_precedes_and_cannot_be_bypassed_by_failing_chain(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        _, hook, _, _, _ = syncwheel.managed_hook_paths(
            self.repo, 'reference-transaction'
        )
        order_log = self.temp_root / 'hook-order.log'
        self.write_executable(hook, CHAIN_REJECTING_PREPARED)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        old, new = self.descendant_commit()
        env = self._clean_env()
        env['SYNCWHEEL_TEST_CLI_LOG'] = str(order_log)

        moved = subprocess.run(
            ['git', 'update-ref', 'refs/heads/main-integration', new, old],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(moved.returncode, 0)
        self.assertEqual(
            subprocess.check_output(['git', 'rev-parse', 'main-integration'], cwd=self.repo, text=True).strip(),
            old,
        )
        order = self.hook_order(order_log)
        self.assertEqual(order[:2], ['guard', 'user-prepared'])
        self.assertIn('user-aborted', order)
        self.assertIn('refusing unauthorized primary integration ref move', moved.stderr)
        self.assertNotIn('stable syncwheel CLI failed', moved.stderr)

    def test_failing_chain_rejects_a_syncwheel_authorized_ref_move(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        _, hook, _, _, _ = syncwheel.managed_hook_paths(
            self.repo, 'reference-transaction'
        )
        order_log = self.temp_root / 'authorized-hook-order.log'
        self.write_executable(hook, CHAIN_REJECTING_PREPARED)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        old, new = self.descendant_commit('authorized descendant')
        env = self._syncwheel_authorized_env()
        env['SYNCWHEEL_TEST_CLI_LOG'] = str(order_log)

        moved = subprocess.run(
            ['git', 'update-ref', 'refs/heads/main-integration', new, old],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(moved.returncode, 0)
        self.assertEqual(
            subprocess.check_output(['git', 'rev-parse', 'main-integration'], cwd=self.repo, text=True).strip(),
            old,
        )
        order = self.hook_order(order_log)
        self.assertEqual(order[:2], ['guard', 'user-prepared'])
        self.assertIn('user-aborted', order)

    def test_chain_rejecting_every_phase_holds_the_managed_ref(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        _, hook, _, _, _ = syncwheel.managed_hook_paths(
            self.repo, 'reference-transaction'
        )
        order_log = self.temp_root / 'rejected-hook-order.log'
        self.write_executable(hook, CHAIN_REJECTING_EVERY_PHASE)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        old, new = self.descendant_commit('rejected descendant')
        env = self._syncwheel_authorized_env()
        env['SYNCWHEEL_TEST_CLI_LOG'] = str(order_log)

        moved = subprocess.run(
            ['git', 'update-ref', 'refs/heads/main-integration', new, old],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(moved.returncode, 0)
        self.assertEqual(
            subprocess.check_output(['git', 'rev-parse', 'main-integration'], cwd=self.repo, text=True).strip(),
            old,
        )
        self.assertIn('user-aborted', order_log.read_text().splitlines())

    def test_primary_guard_state_write_is_atomic(self):
        syncwheel.save_primary_guard(
            self.repo, self.manifest, enabled=False, reason='keep this state'
        )
        path = syncwheel.primary_guard_path(self.repo)
        before = path.read_bytes()

        with mock.patch.object(syncwheel.os, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaisesRegex(OSError, 'replace failed'):
                syncwheel.save_primary_guard(self.repo, self.manifest, enabled=True)

        self.assertEqual(path.read_bytes(), before)

    def test_guard_json_is_the_only_guard_state_for_hooks_and_status(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        profile_path = self.repo / '.syncwheel' / 'profile.local.json'
        profile_path.write_text(json.dumps({
            'hooks': {'mode': 'disabled', 'reason': 'stale legacy state'},
        }))

        enabled = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertTrue(enabled['enforced'])
        self.assertFalse(enabled['disabled'])

        syncwheel.save_primary_guard(
            self.repo, self.manifest, enabled=False, reason='guard json opt-out'
        )
        profile_path.write_text(json.dumps({'hooks': {'mode': 'required'}}))
        disabled = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertTrue(disabled['disabled'])
        self.assertEqual(disabled['disabledReason'], 'guard json opt-out')
        self.assertEqual(
            syncwheel.command_hooks_worktree_guard(types.SimpleNamespace(
                repo=self.repo, manifest=None, personal=None, event='pre-commit'
            )),
            0,
        )

    def test_personal_hook_lifecycle_uses_selected_branch_and_ledger(self):
        profile = 'alt'
        branch = syncwheel.personal_integration_branch(profile)
        manifest_path = syncwheel.personal_manifest_path(self.repo, profile)
        self.write_selected_manifest(manifest_path, branch)
        subprocess.run(['git', 'branch', '-m', branch], cwd=self.repo, check=True)

        installed = self.run_syncwheel(
            'hooks', 'install', '--apply', '--personal', profile,
            '--repo', str(self.repo),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(
            syncwheel.load_primary_guard(self.repo)['integrationBranch'], branch
        )
        status = self.run_syncwheel(
            'hooks', 'status', '--personal', profile, '--repo', str(self.repo)
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)['status'], 'ready')

        old, new = self.descendant_commit('personal integration descendant')
        moved = subprocess.run(
            ['git', 'update-ref', f'refs/heads/{branch}', new, old],
            cwd=self.repo,
            env=self._clean_env(),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(moved.returncode, 0)
        self.assertEqual(
            subprocess.check_output(
                ['git', 'rev-parse', branch], cwd=self.repo, text=True
            ).strip(),
            old,
        )

        removed = self.run_syncwheel(
            'hooks', 'remove', '--disable', '--reason', 'personal recovery',
            '--apply', '--personal', profile, '--repo', str(self.repo),
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        guard = syncwheel.load_primary_guard(self.repo)
        self.assertEqual(guard['integrationBranch'], branch)
        self.assertFalse(guard['enabled'])
        personal_events = syncwheel.load_ledger_events(self.repo, manifest_path)
        self.assertEqual(personal_events[-1]['type'], 'primary_guard_disabled')
        self.assertEqual(personal_events[-1]['payload']['reason'], 'personal recovery')
        self.assertEqual(
            syncwheel.load_ledger_events(
                self.repo, self.repo / '.syncwheel' / 'manifest.json'
            ),
            [],
        )

        shared_status = self.run_syncwheel(
            'hooks', 'status', '--repo', str(self.repo)
        )
        self.assertEqual(shared_status.returncode, 0, shared_status.stderr)
        shared_report = json.loads(shared_status.stdout)
        self.assertEqual(shared_report['status'], 'degraded')
        self.assertTrue(shared_report['disabled'])
        self.assertEqual(shared_report['disabledReason'], 'personal recovery')
        self.assertIn('hooks install --apply', ' '.join(shared_report['degradedCauses']))

    def test_guard_retarget_requires_reason_and_audits_selected_ledger(self):
        self._install_and_branch('main-integration')
        profile = 'alt'
        branch = syncwheel.personal_integration_branch(profile)
        manifest_path = syncwheel.personal_manifest_path(self.repo, profile)
        self.write_selected_manifest(manifest_path, branch)

        refused = self.run_syncwheel(
            'hooks', 'install', '--apply', '--personal', profile,
            '--repo', str(self.repo),
        )

        self.assertEqual(refused.returncode, 2)
        self.assertIn('retarget', refused.stderr)
        self.assertIn('--reason', refused.stderr)
        self.assertEqual(
            syncwheel.load_primary_guard(self.repo)['integrationBranch'],
            'main-integration',
        )
        self.assertEqual(syncwheel.load_ledger_events(self.repo, manifest_path), [])

        installed = self.run_syncwheel(
            'hooks', 'install', '--apply', '--personal', profile,
            '--reason', 'switch this clone to the alt profile',
            '--repo', str(self.repo),
        )

        self.assertEqual(installed.returncode, 0, installed.stderr)
        report = json.loads(installed.stdout)
        self.assertEqual(report['guardAction'], 'retarget')
        self.assertEqual(
            syncwheel.load_primary_guard(self.repo)['integrationBranch'], branch
        )
        events = syncwheel.load_ledger_events(self.repo, manifest_path)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['type'], 'primary_guard_retargeted')
        self.assertEqual(events[0]['payload'], {
            'actor': os.environ.get('USER', 'unknown'),
            'integrationBranch': branch,
            'phase': 'intent',
            'previousIntegrationBranch': 'main-integration',
            'reason': 'switch this clone to the alt profile',
        })

    def test_explicit_manifest_hook_lifecycle_uses_selected_branch_and_ledger(self):
        branch = 'integration/explicit/main'
        manifest_path = self.temp_root / 'explicit-manifest.json'
        self.write_selected_manifest(manifest_path, branch)

        installed = self.run_syncwheel(
            'hooks', 'install', '--apply', '--manifest', str(manifest_path),
            '--repo', str(self.repo),
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(
            syncwheel.load_primary_guard(self.repo)['integrationBranch'], branch
        )
        status = self.run_syncwheel(
            'hooks', 'status', '--manifest', str(manifest_path),
            '--repo', str(self.repo),
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)['status'], 'ready')

        removed = self.run_syncwheel(
            'hooks', 'remove', '--disable', '--reason', 'explicit recovery',
            '--apply', '--manifest', str(manifest_path), '--repo', str(self.repo),
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        events = syncwheel.load_ledger_events(self.repo, manifest_path)
        self.assertEqual(events[-1]['type'], 'primary_guard_disabled')
        self.assertEqual(events[-1]['payload']['reason'], 'explicit recovery')

    def test_incomplete_guard_state_blocks_ref_move_and_reports_degraded(self):
        self._install_and_branch('main-integration')
        syncwheel.atomic_write_private_json(
            syncwheel.primary_guard_path(self.repo),
            {'version': 1, 'enabled': True, 'reason': None},
            indent=2,
        )
        old, new = self.descendant_commit('blocked by incomplete guard')

        moved = subprocess.run(
            ['git', 'update-ref', 'refs/heads/main-integration', new, old],
            cwd=self.repo,
            env=self._clean_env(),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(moved.returncode, 0)
        self.assertEqual(
            subprocess.check_output(
                ['git', 'rev-parse', 'main-integration'], cwd=self.repo, text=True
            ).strip(),
            old,
        )
        status = self.run_syncwheel('hooks', 'status', '--repo', str(self.repo))
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertEqual(report['status'], 'degraded')
        self.assertFalse(report['ready'])
        self.assertIn('integrationBranch', ' '.join(report['degradedCauses']))

        full_status = self.run_syncwheel(
            'status', '--json', '--repo', str(self.repo)
        )
        self.assertEqual(full_status.returncode, 0, full_status.stderr)
        hooks = json.loads(full_status.stdout)['validation']['details']['hooks']
        self.assertEqual(hooks['status'], 'degraded')

    def test_corrupt_and_nonboolean_guard_state_fail_closed_and_report_degraded(self):
        self._install_and_branch('main-integration')
        cases = (
            ('{not-json\n', 'invalid JSON'),
            (json.dumps({
                'version': 1,
                'integrationBranch': 'main-integration',
                'enabled': 'false',
                'reason': None,
            }) + '\n', 'boolean enabled'),
            (json.dumps({
                'version': True,
                'integrationBranch': 'main-integration',
                'enabled': True,
                'reason': None,
            }) + '\n', 'version'),
        )
        for content, expected_cause in cases:
            with self.subTest(expected_cause=expected_cause):
                syncwheel.primary_guard_path(self.repo).write_text(content)
                old, new = self.descendant_commit(f'blocked by {expected_cause}')

                moved = subprocess.run(
                    ['git', 'update-ref', 'refs/heads/main-integration', new, old],
                    cwd=self.repo,
                    env=self._clean_env(),
                    capture_output=True,
                    text=True,
                )

                self.assertNotEqual(moved.returncode, 0)
                self.assertEqual(
                    subprocess.check_output(
                        ['git', 'rev-parse', 'main-integration'],
                        cwd=self.repo,
                        text=True,
                    ).strip(),
                    old,
                )
                status = self.run_syncwheel(
                    'hooks', 'status', '--repo', str(self.repo)
                )
                self.assertEqual(status.returncode, 0, status.stderr)
                report = json.loads(status.stdout)
                self.assertEqual(report['status'], 'degraded')
                self.assertFalse(report['ready'])
                self.assertIn(
                    expected_cause, ' '.join(report['degradedCauses'])
                )

    def test_non_utf8_guard_state_is_degraded_and_install_repairs_it(self):
        self._install_and_branch('main-integration')
        syncwheel.primary_guard_path(self.repo).write_bytes(b'\xff\xfe\x00binary')

        hooks_status = self.run_syncwheel(
            'hooks', 'status', '--repo', str(self.repo)
        )
        self.assertEqual(hooks_status.returncode, 0, hooks_status.stderr)
        self.assertNotIn('Traceback', hooks_status.stderr)
        hooks_report = json.loads(hooks_status.stdout)
        self.assertEqual(hooks_report['status'], 'degraded')
        self.assertIn('unreadable', ' '.join(hooks_report['degradedCauses']))

        status = self.run_syncwheel(
            'status', '--json', '--repo', str(self.repo)
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        status_hooks = json.loads(status.stdout)['validation']['details']['hooks']
        self.assertEqual(status_hooks['status'], 'degraded')
        self.assertIn('unreadable', ' '.join(status_hooks['degradedCauses']))

        validate = self.run_syncwheel(
            'validate', '--json', '--repo', str(self.repo)
        )
        self.assertNotIn('Traceback', validate.stderr)
        validate_hooks = json.loads(validate.stdout)['details']['hooks']
        self.assertEqual(validate_hooks['status'], 'degraded')
        self.assertIn('unreadable', ' '.join(validate_hooks['degradedCauses']))

        old, new = self.descendant_commit('blocked by non-UTF-8 guard state')
        moved = subprocess.run(
            ['git', 'update-ref', 'refs/heads/main-integration', new, old],
            cwd=self.repo,
            env=self._clean_env(),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(moved.returncode, 0)
        self.assertEqual(
            subprocess.check_output(
                ['git', 'rev-parse', 'main-integration'], cwd=self.repo, text=True
            ).strip(),
            old,
        )

        repaired = self.run_syncwheel(
            'hooks', 'install', '--apply', '--repo', str(self.repo)
        )
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertNotIn('Traceback', repaired.stderr)
        self.assertEqual(json.loads(repaired.stdout)['status'], 'installed')
        self.assertEqual(
            syncwheel.managed_push_guard_policy(self.repo, self.manifest)['status'],
            'ready',
        )

    def test_disabled_guard_without_reason_fails_closed_and_reports_degraded(self):
        self._install_and_branch('main-integration')
        syncwheel.atomic_write_private_json(
            syncwheel.primary_guard_path(self.repo),
            {
                'version': 1,
                'integrationBranch': 'main-integration',
                'enabled': False,
            },
            indent=2,
        )
        old, new = self.descendant_commit('blocked by unaudited disable')

        moved = subprocess.run(
            ['git', 'update-ref', 'refs/heads/main-integration', new, old],
            cwd=self.repo,
            env=self._clean_env(),
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(moved.returncode, 0)
        self.assertEqual(
            subprocess.check_output(
                ['git', 'rev-parse', 'main-integration'], cwd=self.repo, text=True
            ).strip(),
            old,
        )
        status = self.run_syncwheel('hooks', 'status', '--repo', str(self.repo))
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertEqual(report['status'], 'degraded')
        self.assertFalse(report['disabled'])
        self.assertIn('reason', ' '.join(report['degradedCauses']))
        self.assertEqual(
            syncwheel.load_ledger_events(
                self.repo, self.repo / '.syncwheel' / 'manifest.json'
            ),
            [],
        )

    def test_guard_branch_mismatch_is_degraded_with_reinstall_remedy(self):
        self._install_and_branch('main-integration')
        stale_manifest = json.loads(json.dumps(self.manifest))
        stale_manifest['integration']['branch'] = 'stale-integration'
        syncwheel.save_primary_guard(self.repo, stale_manifest)

        status = self.run_syncwheel('hooks', 'status', '--repo', str(self.repo))

        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertEqual(report['status'], 'degraded')
        self.assertFalse(report['ready'])
        causes = ' '.join(report['degradedCauses'])
        self.assertIn('stale-integration', causes)
        self.assertIn('main-integration', causes)
        self.assertIn('hooks install --apply', causes)

    def test_reenable_writes_guard_before_hooks_and_partial_failure_is_degraded(self):
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        syncwheel.remove_managed_push_hook(
            self.repo, apply=True, disable=True, reason='temporary recovery'
        )
        original = syncwheel.install_one_managed_hook
        observed_enabled = []

        def fail_partway(repo_root, hook_name, apply=False):
            if apply:
                observed_enabled.append(syncwheel.load_primary_guard(repo_root)['enabled'])
                if hook_name == 'post-checkout':
                    raise OSError('injected hook failure')
            return original(repo_root, hook_name, apply=apply)

        with mock.patch.object(
            syncwheel, 'install_one_managed_hook', side_effect=fail_partway
        ):
            with self.assertRaisesRegex(OSError, 'injected hook failure'):
                syncwheel.install_managed_push_hook(self.repo, apply=True)

        self.assertTrue(observed_enabled)
        self.assertTrue(all(observed_enabled))
        policy = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertTrue(policy['enforced'])
        self.assertTrue(policy['degraded'])
        self.assertEqual(policy['mode'], 'required-degraded')
        self.assertTrue(
            any('post-checkout' in cause for cause in policy['degradedCauses']),
            policy,
        )

    def test_disable_ledger_failure_preserves_enabled_guard_and_hooks(self):
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        before = self.hook_bundle_bytes()

        with mock.patch.object(
            syncwheel, 'append_ledger_event', side_effect=OSError('ledger append failed')
        ):
            with self.assertRaisesRegex(OSError, 'ledger append failed'):
                syncwheel.remove_managed_push_hook(
                    self.repo, apply=True, disable=True, reason='audited recovery'
                )

        self.assertEqual(self.hook_bundle_bytes(), before)
        self.assertTrue(syncwheel.load_primary_guard(self.repo)['enabled'])

    def test_concurrent_process_cleanup_preserves_live_foreign_nonce(self):
        owner_script = (
            'import importlib.util, os, sys, time\n'
            'spec = importlib.util.spec_from_file_location("guard_owner", sys.argv[1])\n'
            'module = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(module)\n'
            'print(module.authorize_ref_move(sys.argv[2]), flush=True)\n'
            'time.sleep(30)\n'
        )
        owner = subprocess.Popen(
            [os.sys.executable, '-c', owner_script, str(MODULE_PATH), str(self.repo)],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIsNotNone(owner.stdout)
        nonce = owner.stdout.readline().strip()
        nonce_path = syncwheel.ref_auth_dir(self.repo) / nonce
        try:
            self.assertTrue(nonce_path.exists())
            status = self.run_syncwheel('hooks', 'status', '--repo', str(self.repo))
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertTrue(nonce_path.exists())
        finally:
            owner.terminate()
            owner.communicate(timeout=5)
        syncwheel.clear_ref_authorizations(self.repo)
        self.assertFalse(nonce_path.exists())

    def test_ref_authorization_rejects_recycled_pid_identity(self):
        nonce = syncwheel.authorize_ref_move(self.repo)
        nonce_path = syncwheel.ref_auth_dir(self.repo) / nonce
        payload = json.loads(nonce_path.read_text())
        self.assertIsInstance(payload.get('pidStart'), str)
        payload['pidStart'] = 'recycled-process-start'
        syncwheel.atomic_write_private_json(nonce_path, payload)

        with mock.patch.dict(
            os.environ, {syncwheel.MANAGED_REF_MOVE_AUTH_ENV: nonce}, clear=False
        ):
            self.assertFalse(
                syncwheel.ref_move_authorized(self.repo, 'reference-transaction')
            )

    def test_stale_malformed_nonce_is_removed_with_audit_event(self):
        directory = syncwheel.ref_auth_dir(self.repo)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        malformed = directory / 'malformed-nonce'
        malformed.write_text('{not-json')

        syncwheel.clear_ref_authorizations(self.repo)

        self.assertTrue(malformed.exists())
        self.assertFalse(syncwheel.ref_auth_events_path(self.repo).exists())
        stale = time.time() - syncwheel.SYNCWHEEL_REF_AUTH_TTL_SECONDS - 5
        os.utime(malformed, (stale, stale))

        syncwheel.clear_ref_authorizations(self.repo)

        self.assertFalse(malformed.exists())
        events = [
            json.loads(line)
            for line in syncwheel.ref_auth_events_path(self.repo).read_text().splitlines()
        ]
        self.assertEqual(events[-1]['type'], 'ref_authorization_discarded')
        self.assertEqual(events[-1]['file'], malformed.name)
        self.assertEqual(events[-1]['reason'], 'malformed_or_unreadable')

    def test_cli_resolver_rejects_repository_lane_and_registered_worktree_shims(self):
        inside = self.write_executable(self.repo / 'bin' / 'syncwheel')
        common = self.write_executable(self.repo / '.git' / 'bin' / 'syncwheel')
        lane = self.write_executable(
            self.repo / '.syncwheel' / 'wt' / 'lane' / 'bin' / 'syncwheel'
        )
        configured_root = self.temp_root / 'configured-lanes'
        configured = self.write_executable(configured_root / 'lane' / 'bin' / 'syncwheel')
        configured_manifest = dict(self.manifest)
        configured_manifest['syncwheel_worktree_root'] = str(configured_root)
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(configured_manifest, indent=2) + '\n'
        )
        registered = self.temp_root / 'registered-worktree'
        subprocess.run(
            ['git', 'worktree', 'add', '-qb', 'resolver-worktree', str(registered), 'HEAD'],
            cwd=self.repo,
            check=True,
        )
        registered_shim = self.write_executable(registered / 'bin' / 'syncwheel')
        non_executable = self.temp_root / 'not-executable-syncwheel'
        non_executable.write_text('#!/bin/sh\nexit 0\n')
        unstable_interpreter = self.write_executable(
            self.temp_root / 'unstable-interpreter-syncwheel',
            '#!/definitely/missing/python\n',
        )
        path_interpreter = self.write_executable(
            self.temp_root / 'path-interpreter-syncwheel',
            '#!/usr/bin/env python3\n',
        )
        try:
            for candidate in (
                inside,
                common,
                lane,
                configured,
                registered_shim,
                non_executable,
                unstable_interpreter,
                path_interpreter,
            ):
                with self.subTest(candidate=candidate), mock.patch.object(
                    syncwheel.shutil, 'which', return_value=str(candidate)
                ):
                    self.assertIsNone(syncwheel.managed_hook_syncwheel_command(self.repo))
            with mock.patch.object(
                syncwheel.shutil, 'which', return_value=str(self.bin_dir / 'syncwheel')
            ):
                self.assertEqual(
                    syncwheel.managed_hook_syncwheel_command(self.repo),
                    shlex.quote(str((self.bin_dir / 'syncwheel').resolve())),
                )
        finally:
            registered_shim.unlink(missing_ok=True)
            registered_shim.parent.rmdir()
            subprocess.run(
                ['git', 'worktree', 'remove', str(registered)], cwd=self.repo, check=True
            )

    def test_tampered_hook_marks_guard_degraded_with_cause(self):
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        _, hook, _, _, _ = syncwheel.managed_hook_paths(self.repo, 'pre-commit')
        hook.write_text(hook.read_text() + '# tampered\n')

        policy = syncwheel.managed_push_guard_policy(self.repo, self.manifest)

        self.assertTrue(policy['degraded'])
        self.assertEqual(policy['status'], 'degraded')
        self.assertFalse(policy['ready'])
        self.assertTrue(
            any('pre-commit' in cause for cause in policy['degradedCauses']),
            policy,
        )

    def test_missing_or_lane_cli_is_degraded_and_never_generates_a_noop_hook(self):
        with mock.patch.object(syncwheel.shutil, 'which', return_value=None):
            self.assertIn('exit 1', syncwheel.managed_worktree_hook_content(self.repo, 'pre-commit', False))
            self.assertTrue(syncwheel.managed_hook_status(self.repo, 'pre-commit')['degraded'])
        with mock.patch.object(syncwheel.shutil, 'which', return_value='/tmp/repo/var/worktrees/lane/syncwheel'):
            self.assertIsNone(syncwheel.managed_hook_syncwheel_command(self.repo))

    def test_nonce_cannot_authorise_a_second_manual_commit(self):
        self._install_and_branch('main-integration')
        env = self._syncwheel_authorized_env()
        first = subprocess.run(['git', 'commit', '--allow-empty', '-m', 'control'], cwd=self.repo, env=env, capture_output=True, text=True)
        second = subprocess.run(['git', 'commit', '--allow-empty', '-m', 'reuse'], cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(second.returncode, 0)

    def test_remedy_commands_bypass_dirty_primary_preflight(self):
        self._install_and_branch('main-integration')
        (self.repo / 'seed').write_text('dirty\n')
        parser = syncwheel.build_parser()
        for argv in (
            ['worktree', 'open', 'lane', '--into', 'feature', '--repo', str(self.repo)],
            ['stack', 'capture-integration', 'feature', 'HEAD', '--repo', str(self.repo)],
            ['hooks', 'install', '--apply', '--repo', str(self.repo)],
            ['hooks', 'remove', '--disable', '--reason', 'recovery', '--apply', '--repo', str(self.repo)],
        ):
            args = parser.parse_args(argv)
            args.git_args = []
            self.assertTrue(syncwheel.primary_guard_remedy_requested(args))
        remedy_names = {
            function.__name__
            for function, behavior in syncwheel.command_behavior_table().items()
            if behavior['primaryGuardRemedy']
        }
        self.assertEqual(remedy_names, {
            'command_hooks_install',
            'command_hooks_remove',
            'command_stack_capture_integration',
            'command_worktree_open',
        })
        non_remedy = parser.parse_args([
            'stack', 'set', 'feature', 'HEAD', '--repo', str(self.repo)
        ])
        non_remedy.git_args = []
        self.assertTrue(syncwheel.syncwheel_mutation_requested(non_remedy))
        self.assertFalse(syncwheel.primary_guard_remedy_requested(non_remedy))

    def test_classify_integration_preview_is_read_only_and_apply_is_mutating(self):
        parser = syncwheel.build_parser()
        preview = parser.parse_args([
            'stack', 'classify-integration', 'feature', 'HEAD', '--repo', str(self.repo),
        ])
        apply = parser.parse_args([
            'stack', 'classify-integration', 'feature', 'HEAD', '--apply',
            '--plan-digest', 'digest', '--repo', str(self.repo),
        ])
        self.assertFalse(syncwheel.syncwheel_mutation_requested(preview))
        self.assertFalse(syncwheel.manifest_mutation_requested(preview))
        self.assertTrue(syncwheel.syncwheel_mutation_requested(apply))
        self.assertTrue(syncwheel.manifest_mutation_requested(apply))

        self._install_and_branch('main-integration')
        (self.repo / 'seed').write_text('dirty preview\n')
        result = self.run_syncwheel(
            'stack', 'classify-integration', 'feature', 'HEAD',
            '--repo', str(self.repo),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['kind'], 'stackIntegrationClassificationPlan')

    def test_command_registry_prepares_control_manifest_convergence(self):
        entrypoints = syncwheel.entrypoint_behavior_table()
        commands = syncwheel.command_behavior_table()
        self.assertEqual(
            commands,
            {
                function: behavior
                for function, behavior in entrypoints.items()
                if behavior['command']
            },
        )
        for function in (
            syncwheel.command_stack_push,
            syncwheel.command_int_rebuild,
            syncwheel.command_int_push,
        ):
            with self.subTest(function=function.__name__):
                self.assertEqual(
                    commands[function]['manifestMutates'], 'execute'
                )
                self.assertTrue(syncwheel.manifest_mutation_requested(
                    types.SimpleNamespace(func=function, dry_run=False)
                ))
                self.assertFalse(syncwheel.manifest_mutation_requested(
                    types.SimpleNamespace(func=function, dry_run=True)
                ))
        self.assertEqual(
            entrypoints[syncwheel.SyncwheelRevisionBackend.ensure_stack_owned][
                'manifestMutates'
            ],
            'internal',
        )

    def test_every_manifest_or_ledger_writer_command_has_mutation_metadata(self):
        module_paths = (MODULE_PATH, PROVIDER_MODULE_PATH)
        functions = {}
        definitions_by_name = {}
        for path in module_paths:
            module = path.stem
            tree = ast.parse(path.read_text())
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                key = (module, node.name)
                functions[key] = node
                definitions_by_name.setdefault(node.name, set()).add(key)

        calls = {}
        direct_command_writers = set()
        direct_private_savers = {'atomic_write_private_json'}
        for key, node in functions.items():
            called = set()
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                if isinstance(child.func, ast.Name):
                    called.add(child.func.id)
                    if (
                        key[1].startswith('command_')
                        and child.func.id in direct_private_savers
                    ):
                        direct_command_writers.add(key)
                elif isinstance(child.func, ast.Attribute):
                    called.add(child.func.attr)
                    if (
                        key[1].startswith('command_')
                        and child.func.attr in {
                            'atomic_write_private_json', 'write_text', 'write_bytes'
                        }
                    ):
                        direct_command_writers.add(key)
            calls[key] = called

        saver_names = {
            'append_ledger_event',
            'save_manifest',
            'save_manifest_with_ledger',
            'save_primary_guard',
        }
        method_savers = {'delete_journal', 'save_journal'}
        writers = {
            key for key in functions if key[1] in saver_names
        } | {
            key for key, called in calls.items() if called.intersection(method_savers)
        } | direct_command_writers
        changed = True
        while changed:
            changed = False
            for key, called in calls.items():
                callees = set().union(*(
                    definitions_by_name.get(name, set()) for name in called
                )) if called else set()
                if key not in writers and callees.intersection(writers):
                    writers.add(key)
                    changed = True

        writer_commands = {
            getattr(syncwheel, name)
            for module, name in writers
            if module == MODULE_PATH.stem
            and name.startswith('command_')
            and hasattr(syncwheel, name)
        }
        self.assertIn(syncwheel.command_revision_provider, writer_commands)
        self.assertEqual(
            syncwheel.entrypoint_behavior_table()[
                syncwheel.SyncwheelRevisionBackend.ensure_stack_owned
            ]['manifestMutates'],
            'internal',
        )
        behaviors = syncwheel.command_behavior_table()
        missing = sorted(
            func.__name__
            for func in writer_commands
            if behaviors.get(func, {}).get('mutates') in {None, 'never'}
        )
        self.assertEqual(missing, [])

    def test_staged_primary_change_is_blocked(self):
        self._install_and_branch('main-integration')
        (self.repo / 'seed').write_text('staged\n')
        subprocess.run(['git', 'add', 'seed'], cwd=self.repo, check=True)
        result = self.run_syncwheel('stack', 'set', 'feature', 'HEAD', '--repo', str(self.repo))
        self.assertEqual(result.returncode, 2)
        self.assertIn('primary checkout is dirty', result.stderr)

    def test_remove_apply_requires_disable_and_reason(self):
        self._install_and_branch('main-integration')
        result = self.run_syncwheel('hooks', 'remove', '--apply', '--repo', str(self.repo))
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires --disable --reason', result.stderr)

    def test_remove_disable_apply_requires_reason_without_removing_guard(self):
        self._install_and_branch('main-integration')
        before = self.hook_bundle_bytes()

        result = self.run_syncwheel(
            'hooks', 'remove', '--disable', '--apply', '--repo', str(self.repo)
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn('--disable requires --reason', result.stderr)
        self.assertEqual(self.hook_bundle_bytes(), before)

    def test_normal_ref_refusal_does_not_report_cli_failure(self):
        self._install_and_branch('main-integration')

        result = self._reset_hard('HEAD~1')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing unauthorized primary integration ref move', result.stderr)
        self.assertNotIn('stable syncwheel CLI failed', result.stderr)

    def test_unexecutable_cli_process_reports_degraded_remedy(self):
        self._install_and_branch('main-integration')
        self.write_executable(self.bin_dir / 'syncwheel', '#!/bin/sh\nexit 127\n')

        result = subprocess.run(
            ['git', 'commit', '--allow-empty', '-m', 'blocked'],
            cwd=self.repo,
            env=self._clean_env(),
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('stable syncwheel CLI failed to execute', result.stderr)
        self.assertIn('syncwheel hooks install --apply', result.stderr)


if __name__ == '__main__':
    unittest.main()
