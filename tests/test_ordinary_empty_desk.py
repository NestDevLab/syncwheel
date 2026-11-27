"""Resume a frozen ordinary no-op without changing its source ownership."""
import json
import os
import sys
import contextlib
import io
from pathlib import Path
import unittest
from unittest import mock

from _replay_support import create_repo, commit_file, finish_source_scenario, clone_repo, git, run_cli, load_syncwheel_module
import test_replay_modes as fixtures


class OrdinaryEmptyDeskTest(unittest.TestCase):
    setUp = fixtures.ReplayModesTest.setUp
    tearDown = fixtures.ReplayModesTest.tearDown

    def fixture(self):
        source, base = create_repo(self.tmp, 'source')
        git(source, 'switch', '-qc', 'first', base)
        first = commit_file(source, 'value.txt', 'same\n', 'First source')
        git(source, 'switch', '-qc', 'second', base)
        second = commit_file(source, 'value.txt', 'same\n', 'Second independent source')
        last = commit_file(source, 'last.txt', 'after\n', 'Following operation')
        manifest = finish_source_scenario(source, base, [first])
        manifest['syncwheel_tracking'] = 'local-only'
        template = manifest['stacks'][0]
        manifest['stacks'] = [dict(template, id='first', branch='pr/first', commits=[first]),
                              dict(template, id='second', branch='pr/second', commits=[second, last])]
        manifest['integration']['stacks'] = ['first', 'second']
        self.repo = clone_repo(source, self.tmp / 'control', manifest)
        for stack in manifest['stacks']:
            git(self.repo, 'branch', stack['branch'], stack['commits'][-1])
        self.desk = self.tmp / 'desk'
        self.path = self.repo / '.syncwheel/manifest.json'
        self.module = load_syncwheel_module()
        self.args = self.module.build_parser().parse_args([
            'int', 'rebuild', '--repo', str(self.repo), '--replay-mode', 'desk', '--worktree', str(self.desk)])
        # Produce the actual pre-fix ready receipt: the new continuation is absent.
        previous = Path.cwd()
        try:
            os.chdir(self.repo)
            with mock.patch.object(self.module, 'continue_ordinary_empty_desk', return_value=False, create=True), \
                    mock.patch.object(sys, 'argv', ['syncwheel', 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(self.desk)]), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.module.main(), 2)
        finally:
            os.chdir(previous)
        self.receipt_path = self.module.integration_desk_receipt_path(self.repo, self.path)
        self.manifest_bytes = self.path.read_bytes()
        self.normalized_manifest = self.module.load_manifest(self.repo, self.path)[0]
        self.source_refs = git(self.repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/pr/').stdout
        self.second = second
        saved = json.loads(self.receipt_path.read_text())
        self.assertEqual(saved['status'], 'ready')
        self.assertEqual(saved['next'], 1)
        self.assertEqual(git(self.desk, 'rev-parse', 'CHERRY_PICK_HEAD').stdout.strip(), second)
        return self.normalized_manifest

    def resume(self, expected=0):
        return run_cli(self.repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(self.desk), expected=expected)

    def events(self):
        return [e for e in self.module.load_ledger_events(self.repo)
                if e['type'] == 'integration_ordinary_empty_proven']

    def test_old_ready_receipt_finishes_final_replay_and_repeats_deterministically(self):
        self.fixture()
        self.resume()
        saved = json.loads(self.receipt_path.read_text())
        self.assertEqual(saved['next'], len(saved['steps']))
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['payload']['stack'], 'second')
        self.assertEqual(self.events()[0]['payload']['commit'], self.second)
        self.assertEqual(self.module.load_manifest(self.repo, self.path)[0], self.normalized_manifest)
        self.assertEqual(git(self.repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/pr/').stdout, self.source_refs)
        self.assertEqual(git(self.repo, 'show', 'integration:last.txt').stdout, 'after\n')
        tip = git(self.repo, 'rev-parse', 'integration').stdout
        self.resume()
        run_cli(self.repo, 'int', 'rebuild', '--replay-mode', 'ephemeral')
        self.assertEqual(git(self.repo, 'rev-parse', 'integration').stdout, tip)
        self.assertEqual(len(self.events()), 1)

    def test_untracked_work_refuses_without_changing_open_receipt(self):
        self.fixture()
        payload = b'untracked\x00work'
        (self.desk / 'work.bin').write_bytes(payload)
        before = self.receipt_path.read_bytes()
        self.resume(expected=2)
        self.assertEqual(self.receipt_path.read_bytes(), before)
        self.assertEqual((self.desk / 'work.bin').read_bytes(), payload)
        self.assertEqual(self.events(), [])

    def test_index_lock_is_respected_and_retained(self):
        self.fixture()
        lock = Path(git(self.desk, 'rev-parse', '--path-format=absolute', '--git-path', 'index').stdout.strip() + '.lock')
        lock.write_bytes(b'peer')
        self.resume(expected=2)
        self.assertEqual(lock.read_bytes(), b'peer')
        self.assertEqual(self.events(), [])

    def test_crash_after_operation_clear_resumes_once(self):
        manifest = self.fixture()
        original = self.module.git
        def interrupted(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args[:4] == ('update-ref', '--no-deref', '-d', 'CHERRY_PICK_HEAD'):
                raise self.module.SyncwheelError('simulated interruption after exact pseudoref CAS')
            return result
        with mock.patch.object(self.module, 'git', side_effect=interrupted):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'simulated interruption'):
                self.module.rebuild_integration_desk(self.repo, manifest, self.path, self.args)
        self.assertIn('ordinary_empty_pending', json.loads(self.receipt_path.read_text()))
        self.assertEqual(len(self.events()), 1)
        self.resume()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.module.load_manifest(self.repo, self.path)[0], self.normalized_manifest)

    def test_staged_and_unstaged_work_refuse_without_ownership_event(self):
        self.fixture()
        before = self.receipt_path.read_bytes()
        (self.desk / 'value.txt').write_text('peer unstaged\n')
        self.resume(expected=2)
        self.assertEqual((self.desk / 'value.txt').read_text(), 'peer unstaged\n')
        git(self.desk, 'add', 'value.txt')
        self.resume(expected=2)
        self.assertEqual(self.receipt_path.read_bytes(), before)
        self.assertEqual(self.events(), [])
        self.assertEqual(git(self.desk, 'show', ':value.txt').stdout, 'peer unstaged\n')

    def test_foreign_pick_and_revert_are_preserved(self):
        self.fixture()
        foreign = git(self.repo, 'rev-parse', 'pr/first').stdout.strip()
        git(self.desk, 'update-ref', 'CHERRY_PICK_HEAD', foreign)
        before = self.receipt_path.read_bytes()
        self.resume(expected=2)
        self.assertEqual(git(self.desk, 'rev-parse', 'CHERRY_PICK_HEAD').stdout.strip(), foreign)
        git(self.desk, 'update-ref', 'CHERRY_PICK_HEAD', self.second)
        git(self.desk, 'update-ref', 'REVERT_HEAD', foreign)
        self.resume(expected=2)
        self.assertEqual(git(self.desk, 'rev-parse', 'REVERT_HEAD').stdout.strip(), foreign)
        self.assertEqual(self.receipt_path.read_bytes(), before)
        self.assertEqual(self.events(), [])

    def test_manifest_lease_change_refuses_the_same_receipt(self):
        self.fixture()
        changed = json.loads(self.path.read_text())
        changed['stacks'][1]['commits'].reverse()
        self.path.write_text(json.dumps(changed) + '\n')
        before = self.receipt_path.read_bytes()
        self.resume(expected=2)
        self.assertEqual(self.receipt_path.read_bytes(), before)
        self.assertEqual(self.events(), [])

    def test_concurrent_receipt_change_before_admission_is_preserved(self):
        manifest = self.fixture()
        original = self.module.read_integration_desk
        changed = {'done': False}
        def peer(*args):
            result = original(*args)
            if not changed['done']:
                data = json.loads(self.receipt_path.read_text())
                data['peer_evidence'] = 'retained'
                self.receipt_path.write_text(json.dumps(data) + '\n')
                changed['done'] = True
            return result
        with mock.patch.object(self.module, 'read_integration_desk', side_effect=peer):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'receipt changed'):
                self.module.rebuild_integration_desk(self.repo, manifest, self.path, self.args)
        self.assertEqual(json.loads(self.receipt_path.read_text())['peer_evidence'], 'retained')
        self.assertEqual(self.events(), [])

    def test_crash_after_ownership_event_before_clear_deduplicates(self):
        manifest = self.fixture()
        original = self.module.append_ledger_event
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise self.module.SyncwheelError('simulated interruption after durable event')
        with mock.patch.object(self.module, 'append_ledger_event', side_effect=interrupted):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'simulated interruption'):
                self.module.rebuild_integration_desk(self.repo, manifest, self.path, self.args)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(git(self.desk, 'rev-parse', 'CHERRY_PICK_HEAD').stdout.strip(), self.second)
        self.resume()
        self.assertEqual(len(self.events()), 1)

    def test_fresh_ephemeral_replay_does_not_ignore_unproven_empty_pick(self):
        self.fixture()
        failed = run_cli(self.repo, 'int', 'rebuild', '--replay-mode', 'ephemeral', expected=2)
        self.assertIn('empty', failed.stderr)
        self.assertEqual(self.events(), [])

    def test_concurrent_work_after_ownership_proof_preserves_open_pick(self):
        manifest = self.fixture()
        original = self.module.append_ledger_event
        def peer(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.desk / 'peer.bin').write_bytes(b'concurrent\x00work')
            return result
        with mock.patch.object(self.module, 'append_ledger_event', side_effect=peer):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'state changed'):
                self.module.rebuild_integration_desk(self.repo, manifest, self.path, self.args)
        self.assertEqual((self.desk / 'peer.bin').read_bytes(), b'concurrent\x00work')
        self.assertEqual(git(self.desk, 'rev-parse', 'CHERRY_PICK_HEAD').stdout.strip(), self.second)
        self.assertIn('ordinary_empty_pending', json.loads(self.receipt_path.read_text()))
        self.assertEqual(len(self.events()), 1)

    def test_hidden_index_work_is_refused(self):
        self.fixture()
        git(self.desk, 'update-index', '--assume-unchanged', 'value.txt')
        (self.desk / 'value.txt').write_text('concealed peer work\n')
        self.resume(expected=2)
        self.assertEqual((self.desk / 'value.txt').read_text(), 'concealed peer work\n')
        self.assertEqual(git(self.desk, 'rev-parse', 'CHERRY_PICK_HEAD').stdout.strip(), self.second)
        self.assertEqual(self.events(), [])

    def test_git_peer_cannot_move_head_or_stage_during_continuation(self):
        manifest = self.fixture()
        original = self.module.append_ledger_event
        head = git(self.desk, 'rev-parse', 'HEAD').stdout.strip()
        other = git(self.repo, 'rev-parse', 'pr/second').stdout.strip()
        attempted = []
        def peer(*args, **kwargs):
            result = original(*args, **kwargs)
            attempted.append(git(self.desk, 'update-ref', 'HEAD', other, head, expected=128))
            blob = git(self.desk, 'rev-parse', 'HEAD:value.txt').stdout.strip()
            attempted.append(git(self.desk, 'update-index', '--cacheinfo', '100644', blob, 'peer.txt', expected=128))
            return result
        with mock.patch.object(self.module, 'append_ledger_event', side_effect=peer):
            self.assertTrue(self.module.continue_ordinary_empty_desk(
                self.repo, manifest, self.path, self.receipt_path,
                json.loads(self.receipt_path.read_text()), self.desk))
        self.assertEqual(len(attempted), 2)
        self.assertEqual(git(self.desk, 'rev-parse', 'HEAD').stdout.strip(), head)
        self.assertNotIn('peer.txt', git(self.desk, 'ls-files').stdout)
        self.resume()
        self.assertEqual(len(self.events()), 1)
