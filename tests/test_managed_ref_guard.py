import importlib.util
import io
import json
import os
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

    def test_bundle_installs_primary_checkout_guards_and_reports_ready(self):
        result = syncwheel.install_managed_push_hook(self.repo, apply=True)
        self.assertTrue(result['ready'])
        self.assertEqual(
            set(result['hooks']), {'pre-push', 'pre-commit', 'post-checkout'}
        )
        for hook in result['hooks'].values():
            self.assertTrue(hook['ready'])
            self.assertEqual(hook['status'], 'installed')

    def test_primary_checkout_switch_warns_and_pre_commit_blocks(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        syncwheel.install_managed_push_hook(self.repo, apply=True)

        switched = subprocess.run(
            ['git', 'switch', '-c', 'feature'], cwd=self.repo,
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
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.assertNotEqual(committed.returncode, 0)
        self.assertIn('commit blocked', committed.stderr)

    def test_feature_worktree_commit_remains_allowed(self):
        subprocess.run(['git', 'branch', '-m', 'main-integration'], cwd=self.repo, check=True)
        subprocess.run(['git', 'add', '.syncwheel/manifest.json'], cwd=self.repo, check=True)
        subprocess.run(['git', 'commit', '-qm', 'manifest'], cwd=self.repo, check=True)
        syncwheel.install_managed_push_hook(self.repo, apply=True)
        feature = self.repo.parent / f'{self.repo.name}-feature-wt'
        subprocess.run(
            ['git', 'worktree', 'add', '-b', 'feature', str(feature), 'HEAD'],
            cwd=self.repo, check=True,
        )
        try:
            (feature / 'allowed').write_text('allowed\n')
            subprocess.run(['git', 'add', 'allowed'], cwd=feature, check=True)
            subprocess.run(['git', 'commit', '-qm', 'allowed'], cwd=feature, check=True)
        finally:
            subprocess.run(['git', 'worktree', 'remove', str(feature)], cwd=self.repo, check=True)

    def test_required_hooks_auto_bootstrap_for_mutating_commands(self):
        policy = syncwheel.ensure_managed_repository_hooks(self.repo, self.manifest)
        self.assertTrue(policy['ready'])
        self.assertTrue(policy['enforced'])
        self.assertTrue(all(item['ready'] for item in policy['hooks'].values()))

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
