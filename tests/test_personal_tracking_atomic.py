import contextlib
import io
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_personal_tracking as fixtures


class PersonalTrackingAtomicTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PersonalTrackingTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = self.fixture.case
        self.root = self.fixture.repo
        self.module = self.fixture.module
        self.profile = self.fixture.profile
        self.case.ignore.write_text('*.cache\n.syncwheel/manifests/\n')
        self.module.ensure_syncwheel_metadata_excluded(self.root, 'git-tracked', 'var/syncwheel')
        shared = json.loads(self.case.manifest_path.read_text())
        shared['syncwheel_worktree_root'] = 'var/syncwheel'
        self.case.manifest_path.write_text(json.dumps(shared))
        self.case.git('add', '.gitignore', '.syncwheel/manifest.json')
        self.case.git('commit', '-qm', 'Record shared tracking layout')
        self.personal = self.case.create_personal_manifest('site', 'local-only')
        selected = json.loads(self.personal.read_text())
        selected.pop('syncwheel_worktree_root', None)
        self.personal.write_text(json.dumps(selected))
        self.personal.chmod(0o640)
        self.profile.write_text(json.dumps({'personal': 'site'}))
        self.ledger = self.module.ledger_root(self.root, self.personal)

    @staticmethod
    def image(path):
        return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)

    def snapshot(self):
        shared = self.fixture.snapshot()
        paths = {
            'index': self.root / '.git' / 'index',
            'ignore': self.case.ignore,
            'exclude': self.root / '.git' / 'info' / 'exclude',
            'product': self.case.product,
            'untracked': self.root / 'untracked-product',
            'manifest': self.case.manifest_path,
            'readme': self.fixture.readme,
        }
        shared['modes'] = {
            name: stat.S_IMODE(path.stat().st_mode) if path.exists() else None
            for name, path in paths.items()
        }
        return {
            'shared': shared,
            'manifest': self.image(self.personal),
            'profile': self.image(self.profile),
            # The lock may be established on a first write and must survive for
            # waiters. Only event/checkpoint files constitute the logical ledger.
            'ledger': {
                str(path.relative_to(self.ledger)): self.image(path)
                for path in self.ledger.rglob('*')
                if path.is_file() and path.relative_to(self.ledger).as_posix() != 'ledger.lock'
            },
        }

    def test_distinct_worktree_root_and_static_ignore_keep_tracking_private(self):
        self.case.dirty_product()
        before = self.snapshot()
        result = self.fixture.cli(
            'repo', 'tracking', 'set', 'git-tracked', '--personal', 'site',
            '--apply', '--json', extra_env={'GIT_OPTIONAL_LOCKS': '1'},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['tracking_scope'], 'personal')
        self.assertFalse(report['manifest_tracked'])
        self.assertEqual(report['syncwheel_worktree_root'], '.syncwheel/wt')
        after = self.snapshot()
        for key in ('shared', 'profile'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after['manifest'][1], before['manifest'][1])
        self.assertEqual(json.loads(self.personal.read_text())['syncwheel_tracking'], 'git-tracked')
        self.assertEqual(len(self.module.load_ledger_events(self.root, self.personal)), 1)

    def run_fault(self, phase, action=None):
        store = self.module._manifest_storage

        def fail(checkpoint):
            if checkpoint == phase:
                if action:
                    action()
                raise OSError(f'fault at {phase}')

        stderr = io.StringIO()
        args = [
            'syncwheel', 'repo', 'tracking', 'set', 'git-tracked', '--repo',
            str(self.root), '--personal', 'site', '--apply',
        ]
        with (
            mock.patch.object(sys, 'argv', args),
            mock.patch.object(store, 'ledger_io_checkpoint', side_effect=fail),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(self.module.main(), 2, stderr.getvalue())
        self.assertIn(f'fault at {phase}', stderr.getvalue())
        return stderr.getvalue()

    def test_first_save_rolls_back_after_staging_half_event_and_after_each_install(self):
        before = self.snapshot()
        for phase in (
            'event_payload_half_written', 'personal_tracking_manifest_replaced',
            'event_fsynced', 'checkpoint_replaced',
        ):
            with self.subTest(phase=phase):
                error = self.run_fault(phase)
                self.assertNotIn('rollback incomplete', error)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.module.load_ledger_events(self.root, self.personal), [])
        self.assertTrue((self.ledger / 'ledger.lock').is_file())

    def test_existing_segment_and_checkpoint_revert_after_replacement(self):
        seeded = self.fixture.cli('repo', 'tracking', 'set', 'local-only', '--personal', 'site', '--apply')
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        segment = next((self.ledger / 'events').glob('*.jsonl'))
        checkpoint = self.ledger / 'checkpoints' / 'latest.json'
        segment.chmod(0o640)
        checkpoint.chmod(0o640)
        lock = self.ledger / 'ledger.lock'
        lock_inode = lock.stat().st_ino
        before = self.snapshot()
        self.run_fault('checkpoint_replaced')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(lock.stat().st_ino, lock_inode)

    def test_foreign_manifest_replacement_is_preserved_during_rollback(self):
        before = self.snapshot()
        foreign = self.personal.with_name('foreign.local.json')
        data = b'{"owner": "foreign"}\n'

        def replace_manifest():
            foreign.write_bytes(data)
            os.replace(foreign, self.personal)

        error = self.run_fault('checkpoint_replaced', replace_manifest)
        self.assertIn('rollback incomplete', error)
        self.assertIn('file changed after write', error)
        backups = list(self.personal.parent.glob('.tracking-*.tmp'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(self.image(backups[0]), before['manifest'])
        self.assertIn(str(backups[0]), error)
        after = self.snapshot()
        self.assertEqual(after['manifest'][0], data)
        for key in ('shared', 'profile', 'ledger'):
            self.assertEqual(after[key], before[key], key)


    def test_personal_manifest_cannot_alias_active_profile(self):
        for kind, link in (('symbolic link', os.symlink), ('regular, unlinked', os.link)):
            with self.subTest(kind=kind):
                self.personal.unlink()
                link(self.profile, self.personal)
                before = self.snapshot()
                identity = self.personal.lstat().st_ino
                result = self.fixture.cli(
                    'repo', 'tracking', 'set', 'git-tracked', '--personal', 'site', '--apply',
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(kind, result.stderr)
                self.assertEqual(self.personal.lstat().st_ino, identity)
                self.assertEqual(self.snapshot(), before)


    def test_cleanup_failure_after_publication_reports_applied_success(self):
        before = self.snapshot()
        failed = []
        unlink = Path.unlink
        published = False

        def observe(phase):
            nonlocal published
            if phase == 'checkpoint_replaced':
                published = True

        def refuse_cleanup(path, *args, **kwargs):
            if published and not failed and path.name.startswith('.tracking-') and path.exists():
                failed.append(path)
                raise OSError('fixture cleanup failure')
            return unlink(path, *args, **kwargs)

        args = [
            'syncwheel', 'repo', 'tracking', 'set', 'git-tracked', '--repo',
            str(self.root), '--personal', 'site', '--apply', '--json',
        ]
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, 'argv', args),
            mock.patch.object(self.module._manifest_storage, 'ledger_io_checkpoint', side_effect=observe),
            mock.patch.object(Path, 'unlink', autospec=True, side_effect=refuse_cleanup),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            result = self.module.main()
        self.assertEqual(result, 0, err.getvalue())
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0].is_file())
        self.assertIn('applied', err.getvalue())
        self.assertIn(str(failed[0]), err.getvalue())
        report = json.loads(out.getvalue())
        self.assertTrue(report['apply'])
        self.assertEqual(report['syncwheel_tracking'], 'git-tracked')
        events = self.module.load_ledger_events(self.root, self.personal)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['payload']['reason'], 'repo_tracking_set')
        checkpoint = self.ledger / 'checkpoints' / 'latest.json'
        self.assertEqual(json.loads(checkpoint.read_text()), self.module._manifest_storage.reduce_ledger_state(events))
        after = self.snapshot()
        self.assertEqual(after['shared'], before['shared'])
        self.assertEqual(after['profile'], before['profile'])
        self.assertEqual(json.loads(self.personal.read_text())['syncwheel_tracking'], 'git-tracked')


    def test_cleanup_failure_before_commit_preserves_original_error(self):
        before = self.snapshot()
        failed = []
        unlink = Path.unlink

        def refuse_cleanup(path, *args, **kwargs):
            if (not failed and path.parent == self.personal.parent
                    and path.name.startswith('.tracking-') and path.exists()):
                failed.append(path)
                raise OSError('fixture cleanup failure')
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', autospec=True, side_effect=refuse_cleanup):
            error = self.run_fault('event_payload_half_written')
        self.assertIn('interrupted', error)
        self.assertIn('fixture cleanup failure', error)
        self.assertIn('fault at event_payload_half_written', error)
        self.assertEqual(len(failed), 1)
        self.assertEqual(self.snapshot(), before)
