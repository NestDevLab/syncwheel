"""Retire only verified duplicate lane files, with recoverable cleanup."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import unittest

import test_syncwheel as fixtures


ROOT = Path(__file__).resolve().parents[1]


def file_tree(root):
    result = {}
    if not root.exists():
        return result
    for path in root.rglob('*'):
        if path.is_symlink():
            result[str(path.relative_to(root))] = ('symlink', os.readlink(path))
        elif path.is_file():
            result[str(path.relative_to(root))] = (
                'file', path.stat().st_mode & 0o777, path.read_bytes(),
            )
    return result


class RetireCopyTest(unittest.TestCase):
    reason = 'Retire files already declared in the stack'
    owner = {'SYNCWHEEL_LANE_OWNER': 'fixture-owner', 'SYNCWHEEL_UPDATE_MODE': 'off',
             'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1'}

    def setUp(self):
        self.f = fixtures.SyncwheelFixtureTest()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.repo = self.f.repo
        self.module = self.f.load_syncwheel_module()
        self.base = self.git('rev-parse', 'HEAD').decode().strip()
        remote = self.f.tmp / 'origin.git'
        self.git('init', '--bare', '-q', str(remote))
        self.git('remote', 'add', 'origin', str(remote))
        self.git('remote', 'add', 'fork', str(remote))
        self.git('push', '-q', 'origin', 'main')
        self.cli('stack', 'create', 'copy', '--branch', 'pr/copy', '--base', self.base,
                 '--target-remote', 'origin')
        self.lane = json.loads(self.cli('worktree', 'open', 'copy-lane', '--into', 'copy',
                                       '--base', self.base, '--json').stdout)['lane']
        self.path = Path(self.lane['path'])
        self.index = Path(os.fsdecode(self.git('rev-parse', '--path-format=absolute',
                                              '--git-path', 'index', cwd=self.path).strip()))
        self.primary_index = self.repo / '.git/index'
        self.manifest = self.repo / '.syncwheel/manifest.json'
        (self.path / 'alpha.txt').write_bytes(b'declared alpha\n')
        (self.path / 'new.bin').write_bytes(b'declared\x00file\xff')
        self.git('add', 'alpha.txt', 'new.bin', cwd=self.path)
        self.git('commit', '-qm', 'Declare copied files', cwd=self.path)
        self.tip = self.git('rev-parse', 'HEAD', cwd=self.path).decode().strip()
        command = ('stack', 'set', 'copy', '--base', self.base, '--adopt-tip', self.tip)
        plan = json.loads(self.cli(*command).stdout)
        self.cli(*command, '--plan-digest', plan['planDigest'], '--apply')
        self.git('reset', '--mixed', self.base, cwd=self.path)

    def git(self, *args, cwd=None, input_data=None):
        return subprocess.run(['git', *map(str, args)], cwd=cwd or self.repo,
                              env={**os.environ, **self.owner}, input=input_data,
                              capture_output=True, check=True).stdout

    def cli(self, *args, expected=0, owner=None):
        return self.f.run_cli(*args, expected=expected,
                              extra_env={**self.owner, **(owner or {})})

    def arguments(self, *extra):
        return ('worktree', 'retire-copy', 'copy-lane', '--reason', self.reason, *extra)

    def preview(self, *extra):
        return json.loads(self.cli(*self.arguments(*extra)).stdout)

    def apply(self, plan, *extra, expected=0, owner=None):
        return self.cli(*self.arguments(*extra), '--apply', '--confirm-writers-stopped',
                        '--plan-digest', plan['planDigest'], expected=expected, owner=owner)

    def snapshot(self):
        return {
            'lane': file_tree(self.path), 'lane_index': self.index.read_bytes(),
            'primary_index': self.primary_index.read_bytes(),
            'manifest': self.manifest.read_bytes(),
            'refs': self.git('for-each-ref', '--format=%(refname) %(objectname) %(symref)'),
            'control': file_tree(self.repo / '.syncwheel'),
            'private_control': file_tree(self.repo / '.git/syncwheel'),
            'ignore': file_tree(self.repo / '.git/info'),
            'registrations': self.git('worktree', 'list', '--porcelain', '-z'),
            'primary_product': [(self.repo / name).read_bytes() for name in ('alpha.txt', 'beta.txt')],
        }

    def assert_preview_refused_without_writes(self, *extra, owner=None):
        before = self.snapshot()
        result = self.cli(*self.arguments(*extra), expected=2, owner=owner)
        self.assertEqual(self.snapshot(), before)
        return result

    def test_preview_proves_one_declared_commit_without_writes(self):
        nonce = self.repo / '.git/syncwheel/ref-auth/stale'
        nonce.parent.mkdir(parents=True, exist_ok=True)
        nonce.write_bytes(b'{')
        os.utime(nonce, (1, 1))
        before = self.snapshot()
        plan = self.preview()
        self.assertEqual(plan['chosenCommit'], self.tip)
        self.assertEqual(len(plan['planDigest']), 64)
        self.assertTrue(plan['pathEvidence'])
        self.assertFalse(Path(plan['recoveryLocation']).exists())
        self.assertEqual(self.snapshot(), before)

    def test_apply_preserves_head_registry_and_recovers_patch_then_native_release_push(self):
        self.git('add', 'alpha.txt', cwd=self.path)
        before = self.snapshot()
        plan = self.preview()
        result = self.apply(plan)
        self.assertEqual(self.git('status', '--porcelain', cwd=self.path), b'')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.path).decode().strip(), self.base)
        after = self.snapshot()
        for key in ('primary_index', 'manifest', 'registrations', 'primary_product', 'ignore'):
            self.assertEqual(after[key], before[key], key)
        registry = self.module.governed_worktree_registry_path(self.repo)
        self.assertEqual(registry.read_bytes(), before['private_control'][str(registry.relative_to(self.repo / '.git/syncwheel'))][2])
        recovery = Path(plan['recoveryLocation'])
        self.assertTrue(recovery.is_dir(), result.stdout)
        self.assertEqual((recovery / 'index').read_bytes(), before['lane_index'])
        staged = recovery / 'staged.patch'
        unstaged = recovery / 'unstaged.patch'
        self.assertTrue(staged.read_bytes())
        self.assertTrue(unstaged.exists())
        with tarfile.open(recovery / 'untracked.tar') as archive:
            self.assertEqual(archive.getnames(), ['new.bin'])
            self.assertEqual(archive.extractfile('new.bin').read(), b'declared\x00file\xff')
        # Verify the saved layers reproduce the original files in a separate fixture checkout.
        restore = self.f.tmp / 'recovery-proof'
        self.git('worktree', 'add', '--detach', str(restore), self.base)
        self.git('apply', '--index', str(staged), cwd=restore)
        if unstaged.read_bytes():
            self.git('apply', str(unstaged), cwd=restore)
        self.assertEqual((restore / 'alpha.txt').read_bytes(), before['lane']['alpha.txt'][2])
        self.assertEqual(self.git('rev-parse', 'pr/copy').decode().strip(), self.tip)
        release = ('worktree', 'release', 'copy-lane', '--reason', self.reason, '--json')
        self.cli(*release)
        self.cli(*release, '--apply')
        self.cli('stack', 'push', 'copy', '--dry-run')
        self.cli('stack', 'push', 'copy')
        self.assertEqual(self.git('ls-remote', 'origin', 'refs/heads/pr/copy').split()[0].decode(), self.tip)

    def test_push_and_release_name_retire_preview(self):
        for command in (('stack', 'push', 'copy', '--dry-run'),
                        ('worktree', 'release', 'copy-lane', '--reason', self.reason)):
            with self.subTest(command=command):
                before = self.snapshot()
                result = self.cli(*command, expected=2)
                self.assertIn('worktree retire-copy copy-lane', result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_uncommitted_difference_refuses_without_recovery_writes(self):
        (self.path / 'new.bin').write_bytes(b'new unique work')
        self.assert_preview_refused_without_writes()

    def test_matching_working_bytes_cannot_hide_a_different_staged_blob(self):
        alpha = self.path / 'alpha.txt'
        alpha.write_bytes(b'unique staged work\n')
        self.git('add', 'alpha.txt', cwd=self.path)
        alpha.write_bytes(b'declared alpha\n')
        self.assert_preview_refused_without_writes()

    def test_release_keeps_unique_staged_blob_even_when_worktree_matches_head(self):
        self.git('reset', '--hard', self.base, cwd=self.path)
        (self.path / 'new.bin').unlink()
        alpha = self.path / 'alpha.txt'
        original = alpha.read_bytes()
        alpha.write_bytes(b'unique staged contribution\n')
        self.git('add', 'alpha.txt', cwd=self.path)
        alpha.write_bytes(original)
        before = self.snapshot()
        command = ('worktree', 'release', 'copy-lane', '--reason', self.reason)
        self.cli(*command, expected=2)
        self.assertEqual(self.snapshot(), before)
        self.cli(*command, '--apply', expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_git_replacement_tree_cannot_prove_declared_bytes(self):
        (self.path / 'alpha.txt').write_bytes(b'not in the actual declared commit\n')
        self.git('add', 'alpha.txt', 'new.bin', cwd=self.path)
        tree = self.git('write-tree', cwd=self.path).decode().strip()
        replacement = self.git('commit-tree', tree, '-p', self.base,
                               '-m', 'Replacement tree fixture').decode().strip()
        self.git('reset', '--mixed', self.base, cwd=self.path)
        self.git('replace', self.tip, replacement)
        self.assert_preview_refused_without_writes()

    def prepare_grafted_undeclared_copy(self, graft):
        (self.path / 'alpha.txt').write_bytes(b'only the undeclared commit has these bytes\n')
        self.git('add', 'alpha.txt', 'new.bin', cwd=self.path)
        tree = self.git('write-tree', cwd=self.path).decode().strip()
        unrelated = self.git('commit-tree', tree, '-p', self.base,
                             '-m', 'Unrelated comparison candidate').decode().strip()
        self.git('reset', '--mixed', self.base, cwd=self.path)
        graft.parent.mkdir(parents=True, exist_ok=True)
        graft.write_text(self.tip + ' ' + unrelated + '\n')

    def test_legacy_graft_cannot_invent_declared_history(self):
        graft = self.repo / '.git/info/grafts'
        self.prepare_grafted_undeclared_copy(graft)
        self.assert_preview_refused_without_writes()

    def test_environment_graft_cannot_invent_declared_history(self):
        graft = self.f.tmp / 'custom-grafts'
        self.prepare_grafted_undeclared_copy(graft)
        self.assert_preview_refused_without_writes(owner={'GIT_GRAFT_FILE': str(graft)})

    def test_reference_hook_refusal_prevents_cleanup_without_bypass(self):
        plan = self.preview()
        hook = self.repo / '.git/hooks/reference-transaction'
        hook.write_text('#!/bin/sh\nif [ "$1" = prepared ]; then exit 1; fi\nexit 0\n')
        hook.chmod(0o755)
        files = file_tree(self.path)
        index = self.index.read_bytes()
        result = self.apply(plan, expected=2)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(file_tree(self.path), files)
        self.assertEqual(self.index.read_bytes(), index)

    def test_matching_bytes_with_different_executable_mode_refuses(self):
        (self.path / 'alpha.txt').chmod(0o755)
        self.assert_preview_refused_without_writes()

    def test_different_declared_commits_cannot_jointly_prove_one_copy(self):
        self.git('reset', '--hard', self.tip, cwd=self.path)
        (self.path / 'alpha.txt').write_bytes(b'second alpha\n')
        (self.path / 'new.bin').write_bytes(b'second binary\x00')
        self.git('add', 'alpha.txt', 'new.bin', cwd=self.path)
        self.git('commit', '-qm', 'Second declared version', cwd=self.path)
        later = self.git('rev-parse', 'HEAD', cwd=self.path).decode().strip()
        data = json.loads(self.manifest.read_text())
        stack = next(s for s in data['stacks'] if s['id'] == 'copy')
        stack.pop('adopted_history')
        stack['commits'] = [self.tip, later]
        self.manifest.write_text(json.dumps(data, indent=2) + '\n')
        self.git('update-ref', 'refs/heads/pr/copy', later, self.tip)
        self.git('reset', '--mixed', self.base, cwd=self.path)
        (self.path / 'alpha.txt').write_bytes(b'declared alpha\n')
        self.assert_preview_refused_without_writes()

    def test_live_foreign_owner_cannot_clean_identical_copy(self):
        self.assert_preview_refused_without_writes(owner={'SYNCWHEEL_LANE_OWNER': 'other-owner'})

    def test_apply_requires_exact_digest_and_writer_confirmation(self):
        plan = self.preview()
        for options in (('--apply', '--plan-digest', plan['planDigest']),
                        ('--apply', '--confirm-writers-stopped', '--plan-digest', '0' * 64)):
            with self.subTest(options=options):
                before = self.snapshot()
                self.cli(*self.arguments(*options), expected=2)
                self.assertEqual(self.snapshot(), before)

    def test_apply_refuses_file_drift_after_preview(self):
        plan = self.preview()
        (self.path / 'alpha.txt').write_bytes(b'concurrent writer\n')
        before = self.snapshot()
        self.apply(plan, expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_apply_refuses_lane_generation_drift(self):
        plan = self.preview()
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        next(lane for lane in registry['lanes'] if lane['id'] == 'copy-lane')['generation_token'] = 'replacement-generation'
        self.module.save_governed_worktree_registry(self.repo, registry)
        before = self.snapshot()
        self.apply(plan, expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_undeclared_comparison_commit_is_not_a_preservation_proof(self):
        self.assert_preview_refused_without_writes('--commit', self.base)

    def test_identical_replacement_after_preview_still_changes_file_identity(self):
        plan = self.preview()
        replacement = self.path / 'replacement.tmp'
        replacement.write_bytes((self.path / 'alpha.txt').read_bytes())
        os.replace(replacement, self.path / 'alpha.txt')
        before = self.snapshot()
        self.apply(plan, expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_apply_refuses_source_ref_drift(self):
        plan = self.preview()
        later = self.git('commit-tree', self.base + '^{tree}', '-p', self.base,
                         '-m', 'Independent lane history').decode().strip()
        self.git('update-ref', 'refs/heads/' + self.lane['branch'], later, self.base)
        before = self.snapshot()
        self.apply(plan, expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_unrelated_index_lock_is_preserved(self):
        plan = self.preview()
        lock = self.index.with_name('index.lock')
        lock.write_bytes(b'foreign index operation')
        before = self.snapshot()
        self.apply(plan, expected=2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(lock.read_bytes(), b'foreign index operation')

    def test_ignored_unique_file_is_never_removed(self):
        (self.path / 'unique.secret').write_bytes(b'private fixture data')
        exclude = self.repo / '.git/info/exclude'
        exclude.write_text(exclude.read_text() + '\nunique.secret\n')
        self.assert_preview_refused_without_writes()

    def test_retention_lock_survives_duplicate_cleanup(self):
        self.git('worktree', 'lock', '--reason', 'Retain validation checkout', str(self.path))
        plan = self.preview()
        self.apply(plan)
        self.assertEqual(self.git('status', '--porcelain', cwd=self.path), b'')
        self.assertIn(b'locked Retain validation checkout', self.git('worktree', 'list', '--porcelain'))

    def test_recovery_location_symlink_cannot_redirect_cleanup(self):
        plan = self.preview()
        recovery = Path(plan['recoveryLocation'])
        outside = self.f.tmp / 'outside-recovery'
        outside.mkdir()
        (outside / 'keep.txt').write_bytes(b'other session data')
        recovery.parent.mkdir(parents=True, exist_ok=True)
        recovery.symlink_to(outside, target_is_directory=True)
        before = self.snapshot()
        self.apply(plan, expected=2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((outside / 'keep.txt').read_bytes(), b'other session data')

    def test_corrupt_recovery_bundle_refuses_before_cleaning_lane(self):
        plan = self.preview()
        files = file_tree(self.path)
        index = self.index.read_bytes()
        script = "import sys\nfrom pathlib import Path\nimport syncwheel, syncwheel_retire_copy\nsys.argv.pop(1)\noriginal = syncwheel_retire_copy.install_bytes\ndef corrupt(path, data, mode=0o600):\n    result = original(path, data, mode)\n    if Path(path).name == 'untracked.tar':\n        Path(path).write_bytes(b'corrupt archive')\n        print('INJECTED_ARCHIVE_CORRUPTION', file=sys.stderr)\n    return result\nsyncwheel_retire_copy.install_bytes = corrupt\nraise SystemExit(syncwheel.main())\n"
        result = subprocess.run(
            ['python3', '-c', script, plan['recoveryLocation'], *self.arguments(), '--apply',
             '--confirm-writers-stopped', '--plan-digest', plan['planDigest']],
            cwd=self.repo, env={**os.environ, **self.owner, 'PYTHONPATH': str(ROOT / 'scripts')},
            text=True, capture_output=True, timeout=30,
        )
        self.assertIn('INJECTED_ARCHIVE_CORRUPTION', result.stderr)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(file_tree(self.path), files)
        self.assertEqual(self.index.read_bytes(), index)

    def crash_at(self, plan, stage):
        script = '''import os, signal, sys
import syncwheel, syncwheel_retire_copy
stage = sys.argv.pop(1)
def crash(value):
    if value == stage:
        os.kill(os.getpid(), signal.SIGKILL)
syncwheel_retire_copy.retire_copy_checkpoint = crash
raise SystemExit(syncwheel.main())
'''
        process = subprocess.run(
            ['python3', '-c', script, stage, *self.arguments(), '--apply',
             '--confirm-writers-stopped', '--plan-digest', plan['planDigest']],
            cwd=self.repo, env={**os.environ, **self.owner, 'PYTHONPATH': str(ROOT / 'scripts')},
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(process.returncode, -signal.SIGKILL, process.stdout + process.stderr)

    def test_crash_after_recovery_keeps_original_files_and_resumes(self):
        before = file_tree(self.path)
        index = self.index.read_bytes()
        plan = self.preview()
        self.crash_at(plan, 'recovery-verified')
        self.assertEqual(file_tree(self.path), before)
        self.assertEqual(self.index.read_bytes(), index)
        self.apply(plan)
        self.assertEqual(self.git('status', '--porcelain', cwd=self.path), b'')

    def test_crash_after_quarantine_resumes_only_exact_retained_state(self):
        plan = self.preview()
        self.crash_at(plan, 'path-quarantined')
        self.apply(plan)
        self.assertEqual(self.git('status', '--porcelain', cwd=self.path), b'')

    def test_crash_retry_preserves_replacement_file(self):
        plan = self.preview()
        self.crash_at(plan, 'path-quarantined')
        (self.path / 'alpha.txt').write_bytes(b'new writer after interruption\n')
        before = file_tree(self.path)
        self.apply(plan, expected=2)
        self.assertEqual(file_tree(self.path), before)
        self.assertEqual((self.path / 'alpha.txt').read_bytes(), b'new writer after interruption\n')
    def test_crash_after_index_install_resumes_without_repeating_cleanup(self):
        plan = self.preview()
        self.crash_at(plan, 'index-installed')
        self.assertEqual(self.git('status', '--porcelain', cwd=self.path), b'')
        clean = file_tree(self.path)
        self.apply(plan)
        self.assertEqual(file_tree(self.path), clean)



if __name__ == '__main__':
    unittest.main()
