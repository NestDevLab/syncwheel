import importlib.util
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / 'scripts' / 'syncwheel.py'
SPEC = importlib.util.spec_from_file_location('syncwheel_managed_ref_guard', MODULE_PATH)
syncwheel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(syncwheel)


class ManagedRefGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.bin_dir = self.repo / '.test-bin'
        self.bin_dir.mkdir()
        syncwheel_bin = self.bin_dir / 'syncwheel'
        syncwheel_bin.write_text(
            '#!/bin/sh\nexec "' + os.fspath(Path(os.sys.executable)) + '" "'
            + os.fspath(MODULE_PATH) + '" "$@"\n'
        )
        syncwheel_bin.chmod(0o755)
        self.hook_env = os.environ.copy()
        self.hook_env['PATH'] = os.fspath(self.bin_dir) + os.pathsep + self.hook_env['PATH']
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=self.repo, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'], cwd=self.repo, check=True)
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
        return {
            str(path.relative_to(self.repo)): path.read_bytes()
            for path in paths
            if path.exists()
        }

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

    def _install_and_branch(self, branch):
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        subprocess.run(['git', 'checkout', '-qb', branch], cwd=self.repo, check=True)
        tips = []
        for name in ('one', 'two'):
            (self.repo / name).write_text(name + '\n')
            subprocess.run(['git', 'add', name], cwd=self.repo, check=True)
            subprocess.run(['git', 'commit', '-qm', name], cwd=self.repo, check=True)
            tips.append(subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=self.repo, check=True,
                capture_output=True, text=True,
            ).stdout.strip())
        return tips

    def _clean_env(self):
        env = dict(self.hook_env)
        env.pop(syncwheel.MANAGED_REF_MOVE_AUTH_ENV, None)
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
        self.assertIn('refusing to rewind managed ref', result.stderr)
        self.assertEqual(
            subprocess.run(
                ['git', 'log', '-1', '--format=%s'], cwd=self.repo,
                check=True, capture_output=True, text=True,
            ).stdout.strip(),
            'two',
        )

    def test_advancing_a_managed_branch_is_allowed(self):
        self._install_and_branch('main-integration')
        (self.repo / 'three').write_text('three\n')
        subprocess.run(['git', 'add', 'three'], cwd=self.repo, check=True)
        result = subprocess.run(
            ['git', 'commit', '-qm', 'three'], cwd=self.repo,
            env=self.hook_env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_syncwheel_authorization_permits_a_rewind(self):
        self._install_and_branch('main-integration')
        env = dict(self.hook_env)
        env.pop(syncwheel.MANAGED_REF_MOVE_AUTH_ENV, None)
        env[syncwheel.MANAGED_REF_MOVE_AUTH_ENV] = '1'
        result = self._reset_hard('HEAD~1', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_guard_fails_open_when_it_cannot_run(self):
        # A local safety guard that cannot execute must not be able to brick the
        # repository, so an unusable PATH has to leave ordinary Git working.
        self._install_and_branch('main-integration')
        env = self._clean_env()
        # Git stays reachable; the syncwheel shim in .test-bin does not.
        env['PATH'] = '/usr/bin:/bin'
        result = subprocess.run(
            ['git', 'commit', '-q', '--allow-empty', '-m', 'still works'],
            cwd=self.repo, env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_owned_ref_moves_stay_out_of_the_ambient_environment(self):
        # Authorization must reach spawned Git only; leaking it into os.environ
        # would silently disarm the guard for everything else in the process.
        self.assertNotIn(syncwheel.MANAGED_REF_MOVE_AUTH_ENV, os.environ)

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
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing to rewind managed ref', result.stderr)

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

    def test_required_hooks_auto_bootstrap_for_mutating_commands(self):
        policy = syncwheel.ensure_managed_repository_hooks(self.repo, self.manifest)
        self.assertTrue(policy['ready'])
        self.assertTrue(policy['enforced'])
        self.assertTrue(all(item['ready'] for item in policy['hooks'].values()))

    def test_tracking_status_bootstraps_missing_required_hooks_and_is_idempotent(self):
        first = self.run_syncwheel(
            'repo', 'tracking', 'status', '--repo', str(self.repo), '--json'
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['syncwheel_tracking'], 'git-tracked')
        first_bundle = self.hook_bundle_bytes()
        self.assertEqual(
            set(syncwheel.MANAGED_REPOSITORY_HOOKS),
            {
                path.name
                for path in (self.repo / '.git' / 'hooks').iterdir()
                if path.name in syncwheel.MANAGED_REPOSITORY_HOOKS
            },
        )
        self.assertEqual(
            json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())['hooks'],
            {'mode': 'required'},
        )

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

    def test_tracking_set_git_tracked_converges_hooks_in_the_same_command(self):
        self.manifest['syncwheel_tracking'] = 'local-only'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(self.manifest, indent=2) + '\n'
        )

        result = self.run_syncwheel(
            'repo', 'tracking', 'set', 'git-tracked',
            '--repo', str(self.repo), '--apply', '--json',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(syncwheel.managed_hook_bundle_status(self.repo)['ready'])
        self.assertEqual(
            json.loads((self.repo / '.syncwheel' / 'profile.local.json').read_text())['hooks'],
            {'mode': 'required'},
        )

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

    def test_auto_bootstrap_persists_required_mode_for_a_ready_bundle(self):
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        profile_path = self.repo / '.syncwheel' / 'profile.local.json'
        profile_path.unlink()

        before = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertTrue(before['ready'])
        self.assertTrue(before['migrationPending'])

        after = syncwheel.ensure_managed_repository_hooks(self.repo, self.manifest)
        self.assertTrue(after['ready'])
        self.assertTrue(after['enforced'])
        self.assertFalse(after['migrationPending'])

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
        syncwheel.remove_managed_push_hook(self.repo, apply=True)
        self.assertEqual(hook.read_text(), '#!/bin/sh\necho existing\n')
        self.assertFalse(backup.exists())

    def test_remove_refuses_modified_owned_hook(self):
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        _, hook, _, _ = syncwheel.managed_push_hook_paths(self.repo)
        hook.write_text(hook.read_text() + '# modified\n')
        with self.assertRaisesRegex(syncwheel.SyncwheelError, 'not owned'):
            syncwheel.remove_managed_push_hook(self.repo, apply=True)
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

    def test_chained_hook_tamper_is_reported_and_refuses_lifecycle(self):
        _, hook, backup, _ = syncwheel.managed_push_hook_paths(self.repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text('#!/bin/sh\necho existing\n')
        hook.chmod(0o755)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        backup.write_text('#!/bin/sh\necho replaced\n')

        status = syncwheel.managed_push_hook_status(self.repo)
        self.assertFalse(status['owned'])
        self.assertFalse(status['chainMatches'])
        self.assertEqual(status['status'], 'conflict')
        with self.assertRaisesRegex(syncwheel.SyncwheelError, 'not owned'):
            syncwheel.remove_managed_push_hook(self.repo, apply=True)
        with self.assertRaisesRegex(syncwheel.SyncwheelError, 'stale or tampered'):
            syncwheel.install_managed_push_hook(self.repo, apply=True)

    def test_required_policy_migrates_then_fails_closed_on_tamper(self):
        pending = syncwheel.managed_push_guard_policy(self.repo, self.manifest)
        self.assertTrue(pending['required'])
        self.assertTrue(pending['migrationPending'])
        self.assertFalse(pending['enforced'])

        syncwheel.install_managed_push_hook(self.repo, apply=True)
        active = syncwheel.require_managed_push_guard(self.repo, self.manifest)
        self.assertTrue(active['enforced'])
        self.assertTrue(active['ready'])

        _, hook, _, _ = syncwheel.managed_push_hook_paths(self.repo)
        hook.write_text(hook.read_text() + '# tampered\n')
        with self.assertRaisesRegex(syncwheel.SyncwheelError, 'missing, stale, or tampered'):
            syncwheel.require_managed_push_guard(self.repo, self.manifest)

    def test_reasoned_disable_is_persisted_and_visible(self):
        disabled = syncwheel.remove_managed_push_hook(
            self.repo, apply=True, disable=True, reason='external contribution clone'
        )
        self.assertEqual(disabled['action'], 'disable')
        policy = syncwheel.require_managed_push_guard(self.repo, self.manifest)
        self.assertTrue(policy['disabled'])
        self.assertEqual(policy['disabledReason'], 'external contribution clone')

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


if __name__ == '__main__':
    unittest.main()
