import json
import contextlib
import io
import subprocess
import sys
import unittest
from unittest import mock

import test_managed_ref_guard as guard_fixtures


class MetadataPreflightTest(unittest.TestCase):
    def setUp(self):
        self.fixture = guard_fixtures.ManagedRefGuardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repo = self.fixture.repo
        self.module = guard_fixtures.syncwheel
        self.fixture._install_and_branch('main-integration')
        self.manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        self.ignore = self.repo / '.gitignore'
        self.ignore.write_text(
            '.syncwheel/ledger/\n.syncwheel/profile.local.json\n'
            '.syncwheel/manifests/*.local.json\n.syncwheel/manifests/*.local-ledger/\n'
        )
        self.git('add', '.gitignore', '.syncwheel/manifest.json')
        self.git('commit', '-qm', 'Track fixture metadata')
        self.product = self.repo / 'seed'

    def git(self, *args):
        return subprocess.check_output(
            ['git', *args], cwd=self.repo, env=self.fixture._syncwheel_authorized_env(),
        )

    def dirty_product(self):
        self.product.write_text('product edit\n')
        (self.repo / 'untracked-product').write_text('untracked edit\n')

    def state(self):
        return {
            'product': self.product.read_bytes(),
            'untracked': (self.repo / 'untracked-product').read_bytes(),
            'index': self.git('ls-files', '--stage'),
            'index_bytes': (self.repo / '.git' / 'index').read_bytes(),
            'ignore': self.ignore.read_bytes(),
            'exclude': (self.repo / '.git' / 'info' / 'exclude').read_bytes(),
            'hooks': {name: data for name, data in self.fixture.hook_bundle_bytes().items()
                      if name != '.syncwheel/profile.local.json'},
            'worktrees': self.git('worktree', 'list', '--porcelain'),
            'refs': self.git('for-each-ref', '--format=%(refname) %(objectname)'),
            'fetch_head': self.read_if_present(self.repo / '.git' / 'FETCH_HEAD'),
        }

    @staticmethod
    def read_if_present(path):
        return path.read_bytes() if path.exists() else None

    def cli(self, *args):
        result = self.fixture.run_syncwheel(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def authority_arguments(self, profile='lane'):
        return (
            'repo', 'authority', 'set', 'ai-managed', '--allow', 'source_change',
            '--apply', '--personal', profile,
        )

    def create_personal_manifest(self, profile, tracking):
        path = self.module.personal_manifest_path(self.repo, profile)
        manifest = self.fixture.write_selected_manifest(
            path, self.module.personal_integration_branch(profile),
        )
        manifest['syncwheel_tracking'] = tracking
        path.write_text(json.dumps(manifest))
        return path

    def test_personal_authority_preserves_primary_state_for_either_tracking_policy(self):
        self.dirty_product()
        for tracking in ('git-tracked', 'local-only'):
            with self.subTest(tracking=tracking):
                path = self.create_personal_manifest('lane', tracking)
                before = self.state()
                shared = self.manifest_path.read_bytes()
                self.cli(*self.authority_arguments())
                self.assertEqual(json.loads(path.read_text())['authority']['mode'], 'ai-managed')
                events = self.module.load_ledger_events(self.repo, path)
                self.assertEqual(events[-1]['payload']['reason'], 'repo_authority_set')
                self.assertEqual(self.manifest_path.read_bytes(), shared)
                self.assertEqual(self.state(), before)

    def test_private_pr_merge_policy_set_and_clear_preserve_primary_state(self):
        self.dirty_product()
        before = self.state()
        shared = self.manifest_path.read_bytes()
        self.cli(
            'repo', 'pr-merge-policy', 'set', 'github', '--repository', 'example/project',
            '--base', 'main', '--method', 'squash', '--allow-bypass', 'required_reviews',
            '--merge-actor', 'example', '--pr-author', 'author', '--apply',
        )
        profile = self.repo / '.syncwheel' / 'profile.local.json'
        self.assertIn('github_pr_merge', json.loads(profile.read_text()))
        self.assertEqual(self.state(), before)
        self.cli('repo', 'pr-merge-policy', 'clear', '--apply')
        self.assertNotIn('github_pr_merge', json.loads(profile.read_text()))
        self.assertEqual(self.state(), before)
        self.assertEqual(self.manifest_path.read_bytes(), shared)

    def test_private_pr_merge_policy_refuses_a_redirected_profile(self):
        personal = self.create_personal_manifest('lane', 'git-tracked')
        profile = self.repo / '.syncwheel' / 'profile.local.json'
        profile.symlink_to(personal)
        self.dirty_product()
        original = personal.read_bytes()
        for args in (
            ('set', 'github', '--repository', 'example/project', '--base', 'main',
             '--method', 'squash', '--allow-bypass', 'required_reviews',
             '--merge-actor', 'example', '--pr-author', 'author'),
            ('clear',),
        ):
            with self.subTest(command=args[0]):
                personal.write_bytes(original)
                before = self.state()
                events = self.module.load_ledger_events(self.repo, self.manifest_path)
                result = self.fixture.run_syncwheel('repo', 'pr-merge-policy', *args, '--apply')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('profile path is a symbolic link', result.stderr)
                self.assertEqual(profile.readlink(), personal)
                self.assertEqual(personal.read_bytes(), original)
                self.assertEqual(self.module.load_ledger_events(self.repo, self.manifest_path), events)
                self.assertEqual(self.state(), before)

    def test_profile_writer_refuses_a_redirected_profile_directory(self):
        directory = self.repo / '.syncwheel'
        target = self.repo / 'redirected-metadata'
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
        target_profile = target / 'profile.local.json'
        target_profile.write_text('{"test": true}\n')
        before = target_profile.read_bytes()

        with self.assertRaisesRegex(self.module.SyncwheelError, 'profile directory is a symbolic link'):
            self.module.save_repo_profile(self.repo, {'test': False})
        self.assertEqual(directory.readlink(), target)
        self.assertEqual(target_profile.read_bytes(), before)

    def prepare_stack(self, *, draft=False):
        manifest = json.loads(self.manifest_path.read_text())
        stack = manifest['stacks'][0]
        if draft:
            stack['state'] = 'draft'
            stack['branch'] = 'syncwheel/draft/feature'
            stack['publication'] = {'enabled': False}
        self.manifest_path.write_text(json.dumps(manifest))
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-qm', 'Prepare stack fixture')
        self.git('update-ref', f"refs/heads/{stack['branch']}", 'HEAD')

    def test_empty_draft_close_preserves_dirty_primary_and_refs(self):
        self.prepare_stack(draft=True)
        remote = self.fixture.temp_root / 'remote.git'
        self.git('clone', '--quiet', '--bare', str(self.repo), str(remote))
        self.git('--git-dir', str(remote), 'update-ref', 'refs/heads/main', 'HEAD')
        self.git('remote', 'add', 'origin', str(remote))
        self.dirty_product()
        before = self.state()
        self.cli('stack', 'close', 'feature')
        self.assertEqual(json.loads(self.manifest_path.read_text())['stacks'], [])
        self.assertEqual(self.state(), before)

    def test_uncoordinated_archive_retains_only_planned_refs_and_preserves_primary(self):
        self.prepare_stack()
        args = ('stack', 'archive', 'feature', '--reason', 'Defer change', '--resume-intent', 'Review later')
        plan = json.loads(self.cli(*args).stdout)
        self.dirty_product()
        before = self.state()
        self.cli(*args, '--plan-digest', plan['planDigest'], '--apply')
        self.assertEqual(json.loads(self.manifest_path.read_text())['stacks'], [])
        after = self.state()
        refs = set(before.pop('refs').decode().splitlines())
        observed = set(after.pop('refs').decode().splitlines())
        self.assertEqual(observed, refs | {f'{ref} {sha}' for ref, sha in plan['archiveRefs'].items()})
        self.assertEqual(after, before)

    def test_pending_shared_control_intent_refuses_personal_authority_on_dirty_primary(self):
        personal = self.create_personal_manifest('lane', 'git-tracked')
        self.module.append_ledger_event(self.repo, 'control_manifest_persistence_intent', {
            'operation_id': 'fixture-interruption',
            'integration_branch': 'main-integration',
            'command': 'syncwheel int rebuild',
        }, self.manifest_path)
        self.dirty_product()
        before = self.state()
        manifest = personal.read_bytes()
        events = self.module.load_ledger_events(self.repo, self.manifest_path)
        result = self.fixture.run_syncwheel(*self.authority_arguments())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('control manifest recovery is pending', result.stderr)
        self.assertIn('retry syncwheel int rebuild from a clean primary checkout', result.stderr)
        self.assertEqual(personal.read_bytes(), manifest)
        self.assertEqual(self.module.load_ledger_events(self.repo, self.manifest_path), events)
        self.assertEqual(self.state(), before)

        pushed = self.fixture.run_syncwheel('int', 'push')
        self.assertEqual(pushed.returncode, 2, pushed.stderr)
        self.assertIn('control manifest recovery is pending', pushed.stderr)
        self.assertEqual(self.module.load_ledger_events(self.repo, self.manifest_path), events)
        self.assertEqual(self.state(), before)

    def test_private_metadata_without_exclusions_is_refused_before_manifest_write(self):
        personal = self.create_personal_manifest('lane', 'git-tracked')
        self.ignore.write_text('.syncwheel/manifests/*.local.json\n')
        self.git('add', '.gitignore')
        self.git('commit', '-qm', 'Remove fixture ledger exclusion')
        self.dirty_product()
        before = self.state()
        original = personal.read_bytes()
        result = self.fixture.run_syncwheel(*self.authority_arguments())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('already ignored private metadata', result.stderr)
        self.assertEqual(personal.read_bytes(), original)
        self.assertEqual(self.module.load_ledger_events(self.repo, personal), [])
        self.assertEqual(self.state(), before)

    def test_empty_draft_scope_is_revalidated_after_manifest_lock(self):
        self.prepare_stack(draft=True)
        self.dirty_product()
        before = self.state()
        original_transaction = self.module.manifest_write_transaction

        @contextlib.contextmanager
        def raced_transaction(*args, **kwargs):
            with original_transaction(*args, **kwargs):
                manifest = json.loads(self.manifest_path.read_text())
                manifest['stacks'][0]['commits'] = [self.git('rev-parse', 'HEAD').decode().strip()]
                self.manifest_path.write_text(json.dumps(manifest))
                yield

        stderr = io.StringIO()
        arguments = ['syncwheel', 'stack', 'close', 'feature', '--repo', str(self.repo)]
        with mock.patch.object(self.module, 'manifest_write_transaction', side_effect=raced_transaction), \
                mock.patch.object(sys, 'argv', arguments), contextlib.redirect_stderr(stderr):
            self.assertEqual(self.module.main(), 2, stderr.getvalue())
        self.assertIn('metadata update scope changed before apply', stderr.getvalue())
        self.assertTrue(json.loads(self.manifest_path.read_text())['stacks'][0]['commits'])
        self.assertEqual(self.module.load_ledger_events(self.repo, self.manifest_path), [])
        self.assertEqual(self.state(), before)

    def test_personal_authority_refuses_staged_product_and_index_conflict(self):
        personal = self.create_personal_manifest('lane', 'git-tracked')
        self.dirty_product()
        self.git('add', 'seed')
        before = self.state()
        original = personal.read_bytes()
        result = self.fixture.run_syncwheel(*self.authority_arguments())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('unrelated staged paths: seed', result.stderr)
        self.assertEqual(personal.read_bytes(), original)
        self.assertEqual(self.state(), before)

        oid = self.git('rev-parse', 'HEAD:seed').decode().strip()
        self.git('update-index', '--force-remove', 'seed')
        subprocess.run(
            ['git', 'update-index', '--index-info'], cwd=self.repo, check=True,
            input=f'100644 {oid} 1\tseed\n100644 {oid} 2\tseed\n100644 {oid} 3\tseed\n'.encode(),
        )
        before = self.state()
        result = self.fixture.run_syncwheel(*self.authority_arguments())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('primary checkout has index conflicts', result.stderr)
        self.assertEqual(personal.read_bytes(), original)
        self.assertEqual(self.state(), before)

    def test_branch_deletion_and_shared_authority_keep_the_full_dirty_primary_guard(self):
        self.prepare_stack(draft=True)
        self.dirty_product()
        before = self.state()
        shared = self.manifest_path.read_bytes()
        for args in (
            ('stack', 'close', 'feature', '--delete-branch'),
            ('stack', 'close', 'feature', '--reason', 'merged'),
            ('repo', 'authority', 'set', 'ai-managed', '--allow', 'source_change', '--apply'),
        ):
            with self.subTest(command=args):
                result = self.fixture.run_syncwheel(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('primary checkout is dirty', result.stderr)
                self.assertEqual(self.state(), before)
                self.assertEqual(self.manifest_path.read_bytes(), shared)


if __name__ == '__main__':
    unittest.main()
