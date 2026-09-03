import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / 'scripts' / 'syncwheel.py'


class JournalModeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='syncwheel-journal-')
        self.root = Path(self.temp.name)
        self.remote = self.root / 'origin.git'
        self.repo = self.root / 'repo'
        self.stable_cli_dir = self.root / 'stable-cli'
        self.stable_cli_dir.mkdir()
        stable_cli = self.stable_cli_dir / 'syncwheel'
        stable_cli.write_text(
            '#!/bin/sh\nexec '
            + shlex.quote(sys.executable) + ' ' + shlex.quote(str(CLI)) + ' "$@"\n'
        )
        stable_cli.chmod(0o755)
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'journal', str(self.repo)], check=True)
        self.git('config', 'user.name', 'Journal Test')
        self.git('config', 'user.email', 'journal@example.invalid')
        (self.repo / 'notes.txt').write_text('one\n')
        self.git('add', 'notes.txt')
        self.git('commit', '-q', '-m', 'initial')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-q', '-u', 'origin', 'journal')
        manifest = {
            'version': 1,
            'repository_mode': 'journal',
            'syncwheel_tracking': 'git-tracked',
            'journal': {
                'branch': 'journal', 'remote': 'origin',
                'include': ['**'], 'exclude': ['excluded/**'],
                'max_file_bytes': 32, 'interval': '30m',
            },
        }
        path = self.repo / '.syncwheel' / 'manifest.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'configure journal')
        self.git('push', '-q', 'origin', 'journal')
        (self.repo / '.syncwheel' / 'profile.local.json').write_text(json.dumps({
            'hooks': {
                'mode': 'disabled',
                'reason': 'journal fixture exercises raw remote state transitions',
            }
        }, indent=2) + '\n')
        with (self.repo / '.git' / 'info' / 'exclude').open('a') as handle:
            handle.write('.syncwheel/profile.local.json\n')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, check=True):
        result = subprocess.run(['git', *args], cwd=self.repo, text=True, capture_output=True)
        if check and result.returncode:
            self.fail(result.stderr)
        return result.stdout.strip()

    def cli(self, *args, expected=0, env=None):
        command_env = os.environ.copy()
        command_env['SYNCWHEEL_UPDATE_MODE'] = 'off'
        command_env['PATH'] = os.pathsep.join(
            (str(self.stable_cli_dir), command_env.get('PATH', ''))
        )
        if env:
            command_env.update(env)
        result = subprocess.run(
            ['python3', str(CLI), *args], cwd=self.repo, text=True,
            capture_output=True, env=command_env,
        )
        if result.returncode != expected:
            self.fail(f'exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}')
        return result

    def load_module(self):
        spec = importlib.util.spec_from_file_location('syncwheel_journal_test', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_snapshot_modified_new_deleted_excluded_and_idempotent(self):
        (self.repo / 'notes.txt').write_text('two\n')
        (self.repo / 'new.txt').write_text('new\n')
        (self.repo / 'delete.txt').write_text('delete\n')
        self.git('add', 'delete.txt')
        self.git('commit', '-q', '-m', 'seed deletion')
        self.git('push', '-q', 'origin', 'journal')
        (self.repo / 'delete.txt').unlink()
        (self.repo / 'excluded').mkdir()
        (self.repo / 'excluded' / 'keep.txt').write_text('local\n')
        before_tree = self.git('status', '--porcelain')

        planned = json.loads(self.cli('journal', 'snapshot').stdout)
        self.assertEqual({item['path'] for item in planned['admitted']}, {'notes.txt', 'new.txt', 'delete.txt'})
        self.assertEqual([item['path'] for item in planned['excluded']], ['excluded/keep.txt'])
        applied = json.loads(self.cli('journal', 'snapshot', '--apply').stdout)
        self.assertTrue(applied['changed'])
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        self.assertTrue((self.repo / 'excluded' / 'keep.txt').exists())
        second = json.loads(self.cli('journal', 'snapshot', '--apply').stdout)
        self.assertFalse(second['changed'])
        self.assertIn('excluded/', self.git('status', '--porcelain'))
        self.assertTrue(before_tree)

    def test_rejects_oversize_secret_sensitive_and_dirty_index(self):
        (self.repo / 'large.txt').write_text('x' * 33)
        result = self.cli('journal', 'snapshot', expected=2)
        self.assertIn('oversize', result.stderr)
        (self.repo / 'large.txt').unlink()
        (self.repo / 'secret.txt').write_text('-----BEGIN PRIVATE KEY-----\n')
        result = self.cli('journal', 'snapshot', expected=2)
        self.assertIn('high-confidence secret', result.stderr)
        (self.repo / 'secret.txt').unlink()
        (self.repo / '.env').write_text('SAFE=value\n')
        result = self.cli('journal', 'snapshot', expected=2)
        self.assertIn('sensitive path', result.stderr)
        (self.repo / '.env').unlink()
        (self.repo / 'notes.txt').write_text('staged\n')
        self.git('add', 'notes.txt')
        result = self.cli('journal', 'snapshot', expected=2)
        self.assertIn('clean real index', result.stderr)

    def test_secret_after_first_mebibyte_is_rejected(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest_path.read_text())
        data['journal']['max_file_bytes'] = 2 * 1024 * 1024
        manifest_path.write_text(json.dumps(data))
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'raise limit')
        (self.repo / 'late-secret.bin').write_bytes(
            b'x' * (1024 * 1024 + 64) + b'-----BEGIN PRIVATE KEY-----\n'
        )
        result = self.cli('journal', 'snapshot', expected=2)
        self.assertIn('high-confidence secret', result.stderr)

    def test_concurrent_content_change_stops(self):
        module = self.load_module()
        manifest, _ = module.load_manifest(self.repo)
        (self.repo / 'notes.txt').write_text('changed\n')
        original = module.journal_read_admitted_file

        def changing(path, limit):
            value = original(path, limit)
            path.write_text('raced\n')
            return value

        with mock.patch.object(module, 'journal_read_admitted_file', side_effect=changing):
            with self.assertRaisesRegex(module.SyncwheelError, 'content changed'):
                module.journal_snapshot(self.repo, manifest, apply=True)

    def test_real_index_lock_blocks_concurrent_add_without_data_loss(self):
        module = self.load_module()
        manifest, _ = module.load_manifest(self.repo)
        (self.repo / 'notes.txt').write_text('changed\n')
        (self.repo / 'excluded').mkdir()
        (self.repo / 'excluded' / 'concurrent.txt').write_text('concurrent\n')
        original = module.journal_file_fingerprint
        attempts = []

        def attempt_add(path):
            if not attempts:
                attempts.append(subprocess.run(
                    ['git', 'add', 'excluded/concurrent.txt'], cwd=self.repo,
                    text=True, capture_output=True,
                ))
            return original(path)

        with mock.patch.object(module, 'journal_file_fingerprint', side_effect=attempt_add):
            result = module.journal_snapshot(self.repo, manifest, apply=True)
        self.assertTrue(result['changed'])
        self.assertNotEqual(attempts[0].returncode, 0)
        self.assertEqual(self.git('diff', '--cached', '--name-only'), '')
        self.assertIn('excluded/', self.git('status', '--porcelain'))

    def test_symlink_swap_during_admission_stops_without_external_read(self):
        module = self.load_module()
        manifest, _ = module.load_manifest(self.repo)
        target = self.repo / 'notes.txt'
        outside = self.root / 'outside-secret.txt'
        target.write_text('changed\n')
        outside.write_text('outside secret\n')
        real_open = module.os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if Path(path) == target and not swapped:
                swapped = True
                target.unlink()
                target.symlink_to(outside)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(module.os, 'open', side_effect=swap_before_open):
            with self.assertRaisesRegex(module.SyncwheelError, 'rejected content'):
                module.journal_snapshot(self.repo, manifest, apply=False)
        self.assertTrue(target.is_symlink())

    def test_publish_equal_then_remote_ahead_and_diverged_stop(self):
        (self.repo / 'notes.txt').write_text('two\n')
        self.cli('journal', 'snapshot', '--apply')
        (self.repo / 'new.txt').write_text('new\n')
        self.cli('journal', 'snapshot', '--apply')
        published = json.loads(self.cli('journal', 'publish', '--apply').stdout)
        self.assertEqual(published['published_tip'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/journal').split()[0], published['published_tip'])

        other = self.root / 'other'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(other)], check=True)
        subprocess.run(['git', 'checkout', '-q', 'journal'], cwd=other, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Other'], cwd=other, check=True)
        subprocess.run(['git', 'config', 'user.email', 'other@example.invalid'], cwd=other, check=True)
        (other / 'ahead.txt').write_text('ahead\n')
        subprocess.run(['git', 'add', 'ahead.txt'], cwd=other, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', 'ahead'], cwd=other, check=True)
        subprocess.run(['git', 'push', '-q', 'origin', 'journal'], cwd=other, check=True)
        result = self.cli('journal', 'publish', '--apply', expected=2)
        self.assertIn('remote tip mismatch', result.stderr)
        (self.repo / 'local.txt').write_text('local\n')
        self.cli('journal', 'snapshot', '--apply')
        result = self.cli('journal', 'publish', '--apply', expected=2)
        self.assertIn('remote tip mismatch', result.stderr)

    def test_publish_bootstraps_missing_remote_journal_ref(self):
        subprocess.run(
            ['git', '--git-dir', str(self.remote), 'update-ref', '-d', 'refs/heads/journal'],
            check=True,
        )
        self.git('fetch', '-q', '--prune', 'origin')
        (self.repo / 'notes.txt').write_text('bootstrap\n')

        published = json.loads(self.cli('journal', 'publish', '--apply').stdout)

        self.assertEqual(published['expected_remote_tip'], None)
        self.assertEqual(published['published_tip'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(
            self.git('ls-remote', 'origin', 'refs/heads/journal').split()[0],
            published['published_tip'],
        )

    def test_publish_lease_loss_stops(self):
        module = self.load_module()
        args = mock.Mock(repo=None, manifest=None, personal=None, apply=True)
        (self.repo / 'notes.txt').write_text('two\n')
        real_git = module.git

        def reject_push(repo_root, *git_args, **kwargs):
            if git_args and git_args[0] == 'push':
                return subprocess.CompletedProcess([], 1, '', 'stale info')
            return real_git(repo_root, *git_args, **kwargs)

        old_cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.object(module, 'git', side_effect=reject_push):
                with self.assertRaisesRegex(module.SyncwheelError, 'lease lost'):
                    module.command_journal_publish(args)
        finally:
            os.chdir(old_cwd)

    def test_scheduler_is_hermetic_dry_run_apply_status_remove(self):
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        log = self.root / 'systemctl.log'
        systemctl = fake_bin / 'systemctl'
        systemctl.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0\n')
        systemctl.chmod(0o755)
        unit_dir = self.root / 'systemd'
        odd_executable = self.root / 'bin % space' / 'sync"wheel'
        odd_executable.parent.mkdir()
        odd_executable.write_text('#!/bin/sh\n')
        odd_executable.chmod(0o755)
        env = {
            'SYNCWHEEL_SYSTEMD_USER_DIR': str(unit_dir),
            'SYNCWHEEL_SYSTEMCTL': str(systemctl),
            'SYNCWHEEL_EXECUTABLE': str(odd_executable),
        }
        plan = json.loads(self.cli('journal', 'schedule', 'install', env=env).stdout)
        self.assertFalse(Path(plan['service_path']).exists())
        self.assertFalse(log.exists())
        self.cli('journal', 'schedule', 'install', '--apply', env=env)
        service = Path(plan['service_path']).read_text()
        timer = Path(plan['timer_path']).read_text()
        self.assertIn('bin %% space', service)
        self.assertIn('sync\\"wheel', service)
        self.assertIn(str(self.repo.resolve()), service)
        self.assertIn('OnUnitInactiveSec=30m', timer)
        self.assertIn('Persistent=true', timer)
        self.cli('journal', 'schedule', 'install', '--apply', env=env)
        Path(plan['service_path']).write_text('foreign\n')
        collision = self.cli('journal', 'schedule', 'remove', '--apply', expected=2, env=env)
        self.assertIn('collision', collision.stderr)
        Path(plan['service_path']).write_text(service)
        status = json.loads(self.cli('journal', 'schedule', 'status', env=env).stdout)
        self.assertTrue(status['installed'])
        self.assertTrue(status['enabled'])
        self.cli('journal', 'schedule', 'remove', env=env)
        self.assertTrue(Path(plan['service_path']).exists())
        self.cli('journal', 'schedule', 'remove', '--apply', env=env)
        self.assertFalse(Path(plan['service_path']).exists())

    def test_scheduler_install_refuses_an_unresolvable_cli(self):
        bare_bin = self.root / 'bare-bin'
        bare_bin.mkdir()
        (bare_bin / 'git').symlink_to(shutil.which('git'))
        (bare_bin / 'python3').symlink_to(sys.executable)
        result = self.cli(
            'journal', 'schedule', 'install', '--apply', expected=2,
            env={'PATH': str(bare_bin)},
        )
        self.assertIn('stable syncwheel CLI is not resolvable', result.stderr)

    def test_scheduler_disable_failure_preserves_units(self):
        unit_dir = self.root / 'systemd-failure'
        systemctl = self.root / 'failing-systemctl'
        systemctl.write_text('#!/bin/sh\n[ "$2" = disable ] && exit 9\nexit 0\n')
        systemctl.chmod(0o755)
        env = {
            'SYNCWHEEL_SYSTEMD_USER_DIR': str(unit_dir),
            'SYNCWHEEL_SYSTEMCTL': str(systemctl),
            'SYNCWHEEL_EXECUTABLE': str(CLI),
        }
        installed = json.loads(self.cli(
            'journal', 'schedule', 'install', '--apply', env=env
        ).stdout)
        result = self.cli('journal', 'schedule', 'remove', '--apply', expected=2, env=env)
        self.assertIn('command failed', result.stderr)
        self.assertTrue(Path(installed['service_path']).exists())
        self.assertTrue(Path(installed['timer_path']).exists())

    def test_delivery_commands_are_forbidden(self):
        result = self.cli('stack', 'list', expected=2)
        self.assertIn('forbidden', result.stderr)
        result = self.cli('plan', expected=2)
        self.assertIn('forbidden', result.stderr)

    def test_manifest_requires_explicit_allowlist_and_size_but_defaults_interval(self):
        module = self.load_module()
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest_path.read_text())
        del data['journal']['interval']
        manifest_path.write_text(json.dumps(data))
        manifest, _ = module.load_manifest(self.repo)
        self.assertEqual(manifest['journal']['interval'], '30m')
        del data['journal']['include']
        manifest_path.write_text(json.dumps(data))
        with self.assertRaisesRegex(module.SyncwheelError, 'journal.include'):
            module.load_manifest(self.repo)

    def test_primary_checkout_accepts_the_journal_branch(self):
        module = self.load_module()
        manifest, _ = module.load_manifest(self.repo)

        primary = module.primary_checkout_state(self.repo, manifest)

        self.assertEqual(primary['branch'], 'journal')
        self.assertEqual(primary['expected_branch'], 'journal')
        self.assertTrue(primary['compliant'])


if __name__ == '__main__':
    unittest.main()
