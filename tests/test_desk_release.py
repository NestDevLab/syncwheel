import contextlib
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_syncwheel as fixtures


def file_state(path):
    if path.is_symlink():
        return 'link', os.readlink(path)
    if path.is_file():
        return 'file', stat.S_IMODE(path.stat().st_mode), path.read_bytes()
    return None


def file_tree(root):
    return {str(path.relative_to(root)): data
            for path in root.rglob('*') if (data := file_state(path)) is not None}


class DeskReleaseTest(unittest.TestCase):
    stack = 'feature-b'
    branch = 'pr/feature-b'
    reason = 'Preserve validated desk work'

    def setUp(self):
        self.fixture = fixtures.SyncwheelFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repo = self.fixture.repo
        self.fixture.prepare_reconcile_apply_worktree_scenario()
        self.module = self.fixture.load_syncwheel_module()
        self.module.ensure_syncwheel_metadata_excluded(self.repo, 'local-only')
        self.manifest = self.repo / '.syncwheel' / 'manifest.json'
        self.desk = self.repo / '.syncwheel' / 'wt' / 'pr-feature-b'

    def git(self, *args, cwd=None):
        return subprocess.run(
            ['git', *map(str, args)], cwd=cwd or self.repo, check=True,
            capture_output=True, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
        ).stdout

    def cli(self, *args, expected=0):
        return self.fixture.run_cli(*args, expected=expected)

    def desk_git(self, *args):
        return self.git(*args, cwd=self.desk)

    def index_path(self, worktree):
        return Path(os.fsdecode(self.git(
            'rev-parse', '--path-format=absolute', '--git-path', 'index', cwd=worktree,
        ).strip()))

    def snapshot(self):
        common = self.repo / '.git'
        registry = self.module.governed_worktree_registry_path(self.repo)
        ledger = self.module.ledger_root(self.repo, self.manifest)
        return {
            'refs': self.git('for-each-ref', '--format=%(refname) %(objectname) %(symref)'),
            'head': self.git('rev-parse', 'HEAD'),
            'registry': file_state(registry),
            'git_syncwheel': file_tree(common / 'syncwheel'),
            'ledger': file_tree(ledger),
            'manifest': file_state(self.manifest),
            'index': file_state(self.index_path(self.repo)),
            'worktrees': self.git('worktree', 'list', '--porcelain', '-z'),
            'desk_exists': self.desk.exists(),
            'desk_files': file_tree(self.desk),
            'desk_index': file_state(self.index_path(self.desk)) if self.desk.is_dir() else None,
            'ignore': (file_state(self.repo / '.gitignore'), file_state(common / 'info' / 'exclude')),
            'primary_product': {name: file_state(self.repo / name) for name in ('alpha.txt', 'beta.txt')},
        }

    def rebuild_desk(self):
        self.cli('stack', 'rebuild', self.stack, '--replay-mode', 'desk')
        self.assertTrue(self.desk.is_dir())
        return self.desk_git('rev-parse', 'HEAD').decode().strip()

    def dirty_desk(self):
        (self.desk / 'beta.txt').write_bytes(b'staged state\n')
        self.desk_git('add', 'beta.txt')
        (self.desk / 'beta.txt').write_bytes(b'working state\n')
        (self.desk / 'beta.txt').chmod(0o755)
        (self.desk / 'alpha.txt').unlink()
        (self.desk / 'link').symlink_to('beta.txt')
        (self.desk / 'untracked.bin').write_bytes(b'\x00untracked\xff\n')

    def release_args(self, *options):
        return ('stack', 'release-desk', self.stack, '--reason', self.reason, *options)

    def release_preview(self, *options):
        return json.loads(self.cli(*self.release_args(*options)).stdout)

    def release_apply(self, plan, *options, expected=0):
        return self.cli(
            *self.release_args(*options), '--confirm-writers-stopped',
            '--plan-digest', plan['planDigest'], '--apply', expected=expected,
        )

    def invoke_release_apply(self, plan):
        stderr = io.StringIO()
        argv = ['syncwheel', *self.release_args(), '--confirm-writers-stopped',
                '--plan-digest', plan['planDigest'], '--apply']
        with contextlib.chdir(self.repo), mock.patch.object(
            sys, 'argv', argv,
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            status = self.module.main()
        return status, stderr.getvalue()

    def verify_removed(self, tip):
        self.assertFalse(self.desk.exists())
        self.assertNotIn(os.fsencode(self.desk), self.git('worktree', 'list', '--porcelain', '-z'))
        self.assertEqual(self.git('rev-parse', self.branch).decode().strip(), tip)

    def test_registered_desk_preview_preserves_everything(self):
        tip = self.rebuild_desk()
        self.dirty_desk()
        before = self.snapshot()
        listed = json.loads(self.cli('worktree', 'list', '--json').stdout)
        self.assertIn('lanes', listed)
        self.assertEqual([desk['path'] for desk in listed['desks']], [str(self.desk)])
        plan = self.release_preview()
        self.assertEqual(plan['stack'], self.stack)
        self.assertEqual(plan['path'], str(self.desk))
        self.assertEqual(plan['sourceRef'], f'refs/heads/{self.branch}')
        self.assertEqual(plan['sourceTip'], tip)
        self.assertEqual(set(plan['recoveryRefs']), {'tip', 'index', 'staged', 'working', 'receipt'})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.release_preview()['planDigest'], plan['planDigest'])

    def test_apply_removes_registration_and_recovers_distinct_index_and_working_tree(self):
        tip = self.rebuild_desk()
        self.dirty_desk()
        files = file_tree(self.desk)
        files.pop('.git')
        staged_diff = self.desk_git('diff', '--cached', '--binary')
        working_diff = self.desk_git('diff', '--binary')
        before = self.snapshot()
        plan = self.release_preview()
        applied = json.loads(self.release_apply(plan).stdout)
        self.assertEqual(applied['status'], 'released')
        self.assertEqual(applied['recoveryRefs'], plan['recoveryRefs'])
        self.verify_removed(tip)
        self.restore_and_compare(plan['recoveryRefs'], tip, files, staged_diff, working_diff,
                                 before['desk_index'][2])
        after = self.snapshot()
        for key in ('head', 'index', 'manifest', 'ignore', 'primary_product'):
            self.assertEqual(after[key], before[key], key)

    def restore_and_compare(self, refs, tip, files, staged_diff, working_diff, index):
        restored = self.fixture.tmp / 'restored'
        self.assertEqual(self.git('rev-parse', refs['tip']).decode().strip(), tip)
        self.assertEqual(self.git('rev-parse', refs['staged'] + '^').decode().strip(), tip)
        self.assertEqual(self.git('rev-parse', refs['working'] + '^'),
                         self.git('rev-parse', refs['staged']))
        self.assertEqual(self.git('cat-file', 'blob', refs['index']), index)
        receipt = json.loads(self.git('cat-file', 'blob', refs['receipt']))
        self.git('worktree', 'add', '--detach', '--no-checkout', restored, refs['tip'])
        self.git('read-tree', refs['working'], cwd=restored)
        self.git('checkout-index', '--all', cwd=restored)
        for name, attributes in receipt['files'].items():
            path = restored / name
            if attributes['type'] == 'directory':
                path.mkdir(parents=True, exist_ok=True)
            if attributes['type'] != 'symlink':
                path.chmod(attributes['mode'])
        self.index_path(restored).write_bytes(self.git('cat-file', 'blob', refs['index']))
        recovered = file_tree(restored)
        recovered.pop('.git')
        self.assertEqual(recovered, files)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=restored).decode().strip(), tip)
        self.git('diff', '--cached', '--exit-code', refs['staged'], cwd=restored)
        self.assertEqual(self.git('diff', '--cached', '--binary', cwd=restored), staged_diff)
        self.assertEqual(self.git('diff', '--binary', cwd=restored), working_diff)

    def test_explicit_legacy_worktree_release_requires_exact_path(self):
        self.desk.parent.mkdir(parents=True, exist_ok=True)
        self.git('worktree', 'add', self.desk, self.branch)
        tip = self.desk_git('rev-parse', 'HEAD').decode().strip()
        self.dirty_desk()
        before = self.snapshot()
        self.cli(*self.release_args(), expected=2)
        self.cli(*self.release_args('--legacy-worktree', str(self.desk.parent)), expected=2)
        self.assertEqual(self.snapshot(), before)
        legacy = ('--legacy-worktree', str(self.desk))
        plan = self.release_preview(*legacy)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(json.loads(self.release_apply(plan, *legacy).stdout)['status'], 'released')
        self.verify_removed(tip)

    def test_archive_remedy_executes_release_then_archive(self):
        tip = self.rebuild_desk()
        self.dirty_desk()
        args = ('stack', 'archive', self.stack, '--reason', 'Archive reviewed changes',
                '--resume-intent', 'Restore if needed')
        before = self.snapshot()

        blocked = self.cli(*args, expected=2)

        self.assertEqual(self.snapshot(), before)
        remedy = next(line[line.index('syncwheel stack release-desk'):].strip()
                      for line in blocked.stderr.splitlines() if 'syncwheel stack release-desk' in line)
        command = shlex.split(remedy)
        self.assertEqual(command[:4], ['syncwheel', 'stack', 'release-desk', self.stack])
        release = json.loads(self.cli(*command[1:]).stdout)
        self.cli(*command[1:], '--confirm-writers-stopped', '--plan-digest',
                 release['planDigest'], '--apply')
        self.verify_removed(tip)
        archive = json.loads(self.cli(*args).stdout)
        self.cli(*args, '--plan-digest', archive['planDigest'], '--apply')
        self.assertEqual(self.fixture.read_manifest()['stacks'], [])
        self.assertEqual(self.git('rev-parse', release['recoveryRefs']['tip']).decode().strip(), tip)

    def test_apply_requires_confirmation_of_stopped_writers(self):
        self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        before = self.snapshot()
        self.cli(*self.release_args(), '--plan-digest', plan['planDigest'], '--apply', expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_new_working_edit_invalidates_reviewed_plan(self):
        self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        (self.desk / 'beta.txt').write_bytes(b'new writer bytes\n')
        latest = self.snapshot()

        self.release_apply(plan, expected=2)

        self.assertEqual(self.snapshot(), latest)

    def test_source_ref_advance_invalidates_reviewed_plan(self):
        tip = self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        advanced = self.git('commit-tree', tip + '^{tree}', '-p', tip,
                            '-m', 'Concurrent source advance').decode().strip()
        self.git('update-ref', f'refs/heads/{self.branch}', advanced, tip)
        latest = self.snapshot()

        self.release_apply(plan, expected=2)

        self.assertEqual(self.snapshot(), latest)

    def test_legacy_release_refuses_primary_and_manual_ambiguous_path(self):
        manual = self.fixture.tmp / 'manual'
        self.git('worktree', 'add', manual, self.branch)
        self.desk = manual
        before = self.snapshot()
        self.cli(*self.release_args(), expected=2)
        self.cli(*self.release_args('--legacy-worktree', str(self.repo)), expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_release_refuses_moved_foreign_governed_lane(self):
        opened = self.fixture.run_cli(
            'worktree', 'open', 'foreign', '--json',
            extra_env={'SYNCWHEEL_LANE_OWNER': 'fixture-foreign'},
        )
        lane = json.loads(opened.stdout)['lane']
        old_path = Path(lane['path'])
        self.desk = old_path.with_name('moved-foreign')
        self.git('worktree', 'move', old_path, self.desk)
        self.desk_git('switch', '-qc', 'different-checked-out-branch')
        manifest = self.fixture.read_manifest()
        manifest['stacks'][0]['branch'] = 'different-checked-out-branch'
        self.manifest.write_text(json.dumps(manifest))
        before = self.snapshot()

        refused = self.cli(*self.release_args('--legacy-worktree', str(self.desk)), expected=2)

        self.assertIn('governed worktree lane foreign', refused.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_pending_lane_cleanup_preview_keeps_metadata_and_desk_identity(self):
        self.rebuild_desk()
        self.dirty_desk()
        self.cli('worktree', 'open', 'unrelated')
        self.fixture.run_cli_until_cleanup_sigkill(
            'after_cleanup_intent', 'worktree', 'release', 'unrelated',
            '--reason', 'Fixture interruption', '--apply',
        )
        before = self.snapshot()

        self.release_preview()
        self.assertEqual(self.snapshot(), before)
        listed = json.loads(self.cli('worktree', 'list', '--json').stdout)

        self.assertEqual([desk['path'] for desk in listed['desks']], [str(self.desk)])
        self.assertEqual(self.snapshot(), before)

    def test_ignored_bytes_refuse_before_any_removal(self):
        self.rebuild_desk()
        ignored = self.desk / 'ignored.bin'
        ignored.write_bytes(b'\x00retained ignored bytes')
        with (self.repo / '.git' / 'info' / 'exclude').open('a') as stream:
            stream.write('\nignored.bin\n')
        self.desk_git('check-ignore', str(ignored))
        before = self.snapshot()

        self.cli(*self.release_args(), expected=2)

        self.assertEqual(self.snapshot(), before)

    def test_nested_repo_refuses_before_any_removal(self):
        self.rebuild_desk()
        nested = self.desk / 'nested'
        self.git('init', '-q', nested)
        (nested / 'private.txt').write_text('nested repository work\n')
        before = self.snapshot()

        self.cli(*self.release_args(), expected=2)

        self.assertEqual(self.snapshot(), before)

    def test_non_regular_file_refuses_before_any_removal(self):
        self.rebuild_desk()
        fifo = self.desk / 'pipe'
        os.mkfifo(fifo)
        inode = fifo.lstat().st_ino
        before = self.snapshot()

        self.cli(*self.release_args(), expected=2)

        self.assertEqual(self.snapshot(), before)
        self.assertEqual(fifo.lstat().st_ino, inode)

    def test_unmerged_index_refuses_before_any_removal(self):
        self.rebuild_desk()
        beta = self.desk_git('rev-parse', 'HEAD:beta.txt').strip()
        alpha = self.desk_git('rev-parse', 'HEAD:alpha.txt').strip()
        subprocess.run(
            ['git', 'update-index', '--index-info'], cwd=self.desk, check=True,
            input=(b'0 ' + b'0' * len(beta) + b'\tbeta.txt\n'
                   + b'100644 ' + beta + b' 1\tbeta.txt\n'
                   + b'100644 ' + alpha + b' 2\tbeta.txt\n'),
        )
        self.assertTrue(self.desk_git('ls-files', '-u'))
        before = self.snapshot()

        self.cli(*self.release_args(), expected=2)

        self.assertEqual(self.snapshot(), before)

    def test_edit_after_recovery_refs_keeps_latest_checkout(self):
        tip = self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        original = self.module.DeskLifecycle.verify_refs
        calls = []

        def verify_then_edit(lifecycle, checked_plan, objects):
            original(lifecycle, checked_plan, objects)
            calls.append(checked_plan['planDigest'])
            (self.desk / 'beta.txt').write_bytes(b'latest concurrent writer\n')

        before = self.snapshot()
        with mock.patch.object(self.module.DeskLifecycle, 'verify_refs', verify_then_edit):
            status, stderr = self.invoke_release_apply(plan)
        self.assertEqual(calls, [plan['planDigest']])
        self.assertEqual(status, 2, stderr)
        self.assertEqual((self.desk / 'beta.txt').read_bytes(), b'latest concurrent writer\n')
        self.assertEqual(self.git('rev-parse', self.branch).decode().strip(), tip)
        after = self.snapshot()
        for key in ('manifest', 'head', 'index', 'desk_index', 'worktrees'):
            self.assertEqual(after[key], before[key], key)

    def interrupt_removal(self, plan, mutate=None):
        original = self.module._StackRepository.git
        held = []

        def fail_remove(repository, root, *args, **kwargs):
            if args[:3] == ('worktree', 'remove', '--force'):
                path = Path(args[3])
                held.append(path)
                if mutate is not None:
                    mutate(path)
                raise self.module.SyncwheelError('fixture removal failed')
            return original(repository, root, *args, **kwargs)

        with mock.patch.object(self.module._StackRepository, 'git', fail_remove):
            status, stderr = self.invoke_release_apply(plan)
        self.assertEqual(status, 2, stderr)
        self.assertIn('fixture removal failed', stderr)
        self.assertEqual(len(held), 1)
        return held[0]

    def test_failed_removal_retries_same_generation_without_touching_replacement(self):
        tip = self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        held = self.interrupt_removal(plan)
        self.assertTrue(held.is_dir())
        self.assertEqual((held / 'beta.txt').read_bytes(), b'working state\n')
        self.assertFalse(self.desk.exists())
        proof = {kind: self.git('rev-parse', ref) for kind, ref in plan['recoveryRefs'].items()}
        self.git('worktree', 'add', '-b', 'replacement', self.desk, 'main')
        (self.desk / 'replacement.bin').write_bytes(b'new worktree generation\x00')
        replacement = file_tree(self.desk)
        replacement_index = file_state(self.index_path(self.desk))
        refused = self.release_apply(plan, expected=2)
        self.assertIn('original desk path was replaced', refused.stderr)
        self.assertTrue(held.is_dir())
        self.assertEqual(file_tree(self.desk), replacement)
        self.assertEqual(file_state(self.index_path(self.desk)), replacement_index)
        (self.desk / 'replacement.bin').unlink()
        self.git('worktree', 'remove', self.desk)

        applied = json.loads(self.release_apply(plan).stdout)

        self.assertEqual(applied['status'], 'released')
        self.assertEqual(applied['planDigest'], plan['planDigest'])
        self.assertFalse(held.exists())
        self.assertNotIn(os.fsencode(held), self.git('worktree', 'list', '--porcelain', '-z'))
        self.assertFalse(self.desk.exists())
        self.assertEqual(self.git('rev-parse', self.branch).decode().strip(), tip)
        self.assertEqual({kind: self.git('rev-parse', ref) for kind, ref in plan['recoveryRefs'].items()},
                         proof)

    def test_retry_refuses_symbolic_quarantine_path_before_scanning_it(self):
        self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        held = self.interrupt_removal(plan)
        destination = held.with_name('desk-preserved-by-owner')
        held.rename(destination)
        held.symlink_to(destination, target_is_directory=True)
        preserved = file_tree(destination)
        before = self.snapshot()

        with mock.patch.object(self.module.DeskLifecycle, 'scan_checkout') as scan:
            status, stderr = self.invoke_release_apply(plan)

        self.assertEqual(status, 2, stderr)
        self.assertIn('symbolic link', stderr)
        scan.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(file_tree(destination), preserved)
        self.assertEqual(held.readlink(), destination)

    def test_archive_after_relocation_prints_exact_retry_for_original_intent(self):
        tip = self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        held = self.interrupt_removal(plan)

        blocked = self.cli(
            'stack', 'archive', self.stack, '--reason', 'Archive reviewed changes',
            '--resume-intent', 'Restore if needed', expected=2,
        )

        remedy = next(line[line.index('syncwheel stack release-desk'):].strip()
                      for line in blocked.stderr.splitlines() if 'syncwheel stack release-desk' in line)
        command = shlex.split(remedy)
        self.assertEqual(command[:4], ['syncwheel', 'stack', 'release-desk', self.stack])
        self.assertIn('--apply', command)
        self.assertEqual(command[command.index('--plan-digest') + 1], plan['planDigest'])
        applied = json.loads(self.cli(*command[1:]).stdout)
        self.assertEqual(applied['status'], 'released')
        self.assertFalse(held.exists())
        self.verify_removed(tip)

    def test_valid_refs_with_wrong_working_tree_cannot_release_desk(self):
        self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        original = self.module.DeskLifecycle.make_objects
        before = self.snapshot()

        def replace_working_tree(lifecycle, *args, **kwargs):
            objects = original(lifecycle, *args, **kwargs)
            staged = objects['staged']
            objects['working'] = self.git(
                'commit-tree', staged + '^{tree}', '-p', staged, '-m', 'Wrong recovery tree',
            ).decode().strip()
            return objects

        with mock.patch.object(
            self.module.DeskLifecycle, 'make_objects', autospec=True,
            side_effect=replace_working_tree,
        ) as make_objects:
            status, stderr = self.invoke_release_apply(plan)
        make_objects.assert_called_once()
        self.assertEqual(status, 2, stderr)
        self.assertIn('working tree does not reproduce', stderr)
        after = self.snapshot()
        for key in ('manifest', 'head', 'index', 'worktrees', 'desk_files', 'desk_index',
                    'registry', 'ledger'):
            self.assertEqual(after[key], before[key], key)

    def test_retry_completes_each_partial_removal_without_recreating_refs(self):
        for missing in ('checkout', 'admin'):
            with self.subTest(missing=missing):
                tip = self.rebuild_desk()
                self.dirty_desk()
                plan = self.release_preview()
                admin = Path(plan['snapshot']['admin'])
                held = self.interrupt_removal(
                    plan, lambda path: shutil.rmtree(path if missing == 'checkout' else admin),
                )
                self.assertEqual(held.exists(), missing == 'admin')
                self.assertEqual(admin.exists(), missing == 'checkout')
                proof = {kind: self.git('rev-parse', ref) for kind, ref in plan['recoveryRefs'].items()}
                if missing == 'checkout':
                    self.git('worktree', 'add', '-b', 'replacement-admin', self.desk, 'main')
                    (self.desk / 'replacement.bin').write_bytes(b'untouched replacement')
                    replacement = file_tree(self.desk)

                result = json.loads(self.release_apply(plan).stdout)

                self.assertEqual(result['status'], 'released')
                self.assertFalse(admin.exists())
                self.assertFalse(held.exists())
                self.assertNotIn(os.fsencode(held), self.git('worktree', 'list', '--porcelain', '-z'))
                if missing == 'checkout':
                    self.assertEqual(file_tree(self.desk), replacement)
                    self.assertEqual(self.desk_git('branch', '--show-current'), b'replacement-admin\n')
                    (self.desk / 'replacement.bin').unlink()
                    self.git('worktree', 'remove', self.desk)
                self.verify_removed(tip)
                self.assertEqual(
                    {kind: self.git('rev-parse', ref) for kind, ref in plan['recoveryRefs'].items()},
                    proof,
                )

    def test_retry_recreates_unanchored_objects_pruned_after_durable_intent(self):
        tip = self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        before = self.snapshot()
        registry = self.module.governed_worktree_registry_path(self.repo)
        intent = registry.parent / 'desk-release' / (plan['planDigest'] + '.json')

        with mock.patch.object(
            self.module._StackRepository, 'publish_desk_recovery_refs',
            side_effect=self.module.SyncwheelError('fixture interrupted before ref publication'),
        ) as publish:
            status, stderr = self.invoke_release_apply(plan)

        publish.assert_called_once()
        self.assertEqual(status, 2, stderr)
        self.assertIn('fixture interrupted before ref publication', stderr)
        prepared = json.loads(intent.read_bytes())
        self.assertEqual(prepared['plan'], plan)
        after = self.snapshot()
        for key in ('refs', 'head', 'index', 'manifest', 'worktrees', 'desk_files', 'desk_index'):
            self.assertEqual(after[key], before[key], key)
        working = prepared['objects']['working']
        self.git('cat-file', '-e', working)
        self.git('prune', '--expire=now')
        pruned = subprocess.run(
            ['git', 'cat-file', '-e', working], cwd=self.repo, capture_output=True,
        )
        self.assertNotEqual(pruned.returncode, 0)

        applied = json.loads(self.release_apply(plan).stdout)

        self.assertEqual(applied['status'], 'released')
        self.verify_removed(tip)
        for kind, ref in plan['recoveryRefs'].items():
            self.assertEqual(self.git('rev-parse', ref).decode().strip(), prepared['objects'][kind])

    def test_retry_after_removal_started_refuses_missing_recovery_object(self):
        self.rebuild_desk()
        self.dirty_desk()
        plan = self.release_preview()
        held = self.interrupt_removal(plan)
        working = self.git('rev-parse', plan['recoveryRefs']['working']).decode().strip()
        objects = Path(os.fsdecode(self.git('rev-parse', '--absolute-git-dir').strip())) / 'objects'
        (objects / working[:2] / working[2:]).unlink()
        preserved = file_tree(held)
        before = self.snapshot()

        result = self.release_apply(plan, expected=2)

        self.assertTrue(result.stderr)
        self.assertEqual(file_tree(held), preserved)
        self.assertEqual(self.snapshot(), before)
