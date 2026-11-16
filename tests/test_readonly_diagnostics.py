import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import unittest
from unittest import mock

import test_journal
import test_syncwheel


class ReadOnlyDiagnosticsTest(unittest.TestCase):
    def setup_repository(self, fixture_class):
        self.fixture = fixture_class()
        self.fixture.setUp()
        self.module = test_syncwheel.SyncwheelFixtureTest().load_syncwheel_module()
        self.addCleanup(self.fixture.tearDown)
        self.repo = self.fixture.repo
        self.control = self.repo / '.syncwheel'
        self.git_dir = self.repo / '.git'
        self.manifest = self.control / 'manifest.json'
        self.cache = self.repo.parent / 'update-state.json'
        self.env = {
            **{key: value for key, value in os.environ.items() if not key.startswith('SYNCWHEEL_')},
            'GIT_ALLOW_PROTOCOL': 'file',
            'GIT_OPTIONAL_LOCKS': '1',
            'HOME': str(self.repo.parent),
            'XDG_CONFIG_HOME': str(self.repo.parent / 'config'),
            'SYNCWHEEL_UPDATE_MODE': 'off',
            'SYNCWHEEL_UPDATE_STATE_PATH': str(self.cache),
            'SYNCWHEEL_REPO_REGISTRY': str(self.repo.parent / 'repo-aliases.json'),
        }

    def cli(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(test_journal.CLI), *args],
            cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=20,
        )
        if expected is not None:
            self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return result

    def seed_private_state(self):
        # A cold startup cache would normally be overwritten, even in update mode off.
        self.cache.write_bytes(b'{ "fixture": true, "last_checked_epoch": 0 }\n')
        nonce_dir = self.git_dir / 'syncwheel' / 'ref-auth'
        nonce_dir.mkdir(parents=True, exist_ok=True)
        with subprocess.Popen([sys.executable, '-c', 'pass']) as exited:
            exited.wait()
        nonces = {
            'dead': {'pid': exited.pid, 'pidStart': 'proc:original'},
            'recycled': {'pid': os.getpid(), 'pidStart': 'proc:original'},
            'live': {'pid': os.getpid(), 'pidStart': self.module.process_start_identity(os.getpid())},
        }
        for name, payload in nonces.items():
            (nonce_dir / name).write_text(json.dumps(payload) + '\n')
        malformed = nonce_dir / 'malformed'
        malformed.write_bytes(b'{')
        os.utime(malformed, (1, 1))

    def snapshot(self):
        def files(root):
            state = {}
            for path in (root, *sorted(root.rglob('*'))):
                if path.is_symlink():
                    state[str(path.relative_to(root))] = ('link', os.readlink(path))
                elif path.exists():
                    mode = path.stat().st_mode
                    data = path.read_bytes() if stat.S_ISREG(mode) else None
                    state[str(path.relative_to(root))] = (mode, data)
            return state

        return {
            'manifest_profile_and_ledger': files(self.control),
            'nonce_registry_and_events': files(self.git_dir / 'syncwheel'),
            'index': (self.git_dir / 'index').read_bytes(),
            'linked_worktree_admin': files(self.git_dir / 'worktrees'),
            'fetch_head': files(self.git_dir / 'FETCH_HEAD'),
            'head': (self.git_dir / 'HEAD').read_bytes(),
            'refs': self.fixture.git('for-each-ref', '--format=%(refname) %(objectname)'),
            'worktrees': self.fixture.git('worktree', 'list', '--porcelain'),
            'update_cache': self.cache.read_bytes(),
            'git_configuration': files(self.git_dir / 'config'),
            'local_excludes': files(self.git_dir / 'info' / 'exclude'),
            'hooks': files(self.git_dir / 'hooks'),
        }

    def assert_preserved(self, before):
        after = self.snapshot()
        for key, state in before.items():
            with self.subTest(state=key):
                self.assertEqual(after[key], state)

    def test_journal_status_and_check_preserve_private_and_git_state(self):
        self.setup_repository(test_journal.JournalModeTest)
        (self.control / 'profile.local.json').unlink()
        self.cli('hooks', 'install', '--apply')
        with (self.git_dir / 'info' / 'exclude').open('a') as exclude:
            exclude.write('.syncwheel/ledger/\n')
        self.cli('repo', 'authority', 'set', 'human-gated', '--apply')
        self.assertTrue(list((self.control / 'ledger' / 'events').glob('*.jsonl')))
        self.seed_private_state()
        self.fixture.publish_from_other_clone({'remote.txt': 'remote only\n'})
        tracked = self.repo / 'notes.txt'
        os.utime(tracked, (1, 1))
        before = self.snapshot()
        for args in [('status', '--json'), ('ck', '--no-fetch', '--strict', '--json')]:
            with self.subTest(command=args[0]):
                report = json.loads(self.cli(*args).stdout)
                self.assertEqual(report['validation']['details']['journal']['branch'], 'journal')
                self.assertEqual(report['validation']['errors'], [])
                self.assert_preserved(before)

    def test_delivery_worktree_list_and_validator_preserve_private_and_git_state(self):
        self.setup_repository(test_syncwheel.SyncwheelFixtureTest)
        lane = json.loads(self.cli('worktree', 'open', 'diagnostic-lane', '--json').stdout)['lane']
        self.seed_private_state()
        before = self.snapshot()

        lanes = json.loads(self.cli('worktree', 'list', '--json').stdout)
        self.assertEqual([row['id'] for row in lanes['lanes']], [lane['id']])
        self.assert_preserved(before)

        for args in [('status', '--json'), ('check', '--no-fetch', '--json')]:
            with self.subTest(command=args[0]):
                validation = json.loads(self.cli(*args).stdout)['validation']
                self.assertIn('integration', validation['details'])
                self.assertEqual([stack['id'] for stack in validation['details']['stacks']],
                                 ['feature-a', 'feature-b'])
                self.assertEqual(validation['errors'], [])
                self.assert_preserved(before)

        # A cache written through ordinary fetch-enabled validation also stays byte-identical.
        self.cli('status', '--fetch', '--json')
        self.assertTrue((self.git_dir / 'syncwheel' / 'patch-ids-v2.sqlite3').is_file())
        self.seed_private_state()
        before = self.snapshot()
        self.cli('check', '--no-fetch', '--json')
        self.assert_preserved(before)

    def test_journal_worktree_list_refusal_preserves_state(self):
        self.setup_repository(test_journal.JournalModeTest)
        self.seed_private_state()
        before = self.snapshot()

        result = self.cli('worktree', 'list', '--json', expected=2)

        self.assertIn('forbidden for repository_mode="journal"', result.stderr)
        self.assert_preserved(before)

    def test_selected_canonical_manifest_redirect_still_refuses_without_writes(self):
        self.setup_repository(test_journal.JournalModeTest)
        self.seed_private_state()
        alternate = self.control / 'alternate.json'
        self.manifest.rename(alternate)
        self.manifest.symlink_to(alternate.name)
        before = self.snapshot()

        result = self.cli('status', '--manifest', str(self.manifest), '--json', expected=2)

        self.assertIn('cannot read canonical primary authority', result.stderr)
        self.assert_preserved(before)

    def test_diagnostic_preserves_outer_authorization_context(self):
        self.setup_repository(test_journal.JournalModeTest)
        self.seed_private_state()
        before = self.snapshot()
        auth_key = self.module.MANAGED_REF_MOVE_AUTH_ENV
        command = ['syncwheel', 'status', '--repo', str(self.repo), '--json']
        child_authorizations = []
        original_env = self.module.managed_process_env

        def inspect_child_env(*args, **kwargs):
            result = original_env(*args, **kwargs)
            child_authorizations.append(result.get(auth_key))
            return result

        with (
            mock.patch.dict(os.environ, {**self.env, auth_key: 'outer-authorization'}, clear=True),
            mock.patch.object(sys, 'argv', command),
            mock.patch.object(self.module, 'SYNCWHEEL_REF_AUTH_REPO', self.repo),
            mock.patch.object(self.module, 'SYNCWHEEL_OWNS_REF_MOVES', True),
            mock.patch.object(self.module, 'managed_process_env', side_effect=inspect_child_env),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(self.module.main(), 0)
            self.assertEqual(self.module.SYNCWHEEL_REF_AUTH_REPO, self.repo)
            self.assertTrue(self.module.SYNCWHEEL_OWNS_REF_MOVES)
            self.assertEqual(os.environ[auth_key], 'outer-authorization')

        self.assertTrue(child_authorizations)
        self.assertEqual(set(child_authorizations), {None})
        self.assert_preserved(before)

    def test_explicit_fetch_updates_stale_remote_tracking_ref(self):
        self.setup_repository(test_journal.JournalModeTest)
        initial = self.fixture.git('rev-parse', 'origin/journal')
        for command in ('status', 'check'):
            with self.subTest(command=command):
                latest = self.fixture.publish_from_other_clone({f'{command}.txt': 'new\n'})
                self.assertNotEqual(latest, initial)

                fetch_args = ('--fetch',) if command == 'status' else ()
                self.cli(command, *fetch_args, '--json')

                self.assertEqual(self.fixture.git('rev-parse', 'origin/journal'), latest)
                initial = latest

    def test_check_no_fetch_does_not_transfer_missing_promisor_blob(self):
        self.setup_repository(test_syncwheel.SyncwheelFixtureTest)
        origin = self.repo.parent / 'promisor.git'
        self.fixture.git('clone', '--bare', '--quiet', str(self.repo), str(origin))
        self.fixture.git('remote', 'add', 'origin', str(origin))
        self.fixture.git('config', 'extensions.partialClone', 'origin')
        self.fixture.git('config', 'remote.origin.promisor', 'true')
        self.fixture.git('--git-dir', str(origin), 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        missing_blob = self.fixture.git('rev-parse', 'HEAD:beta.txt')
        blob_path = self.git_dir / 'objects' / missing_blob[:2] / missing_blob[2:]
        blob_path.unlink()
        packets = self.repo.parent / 'git-packets'
        self.env['GIT_TRACE_PACKET'] = str(packets)
        self.seed_private_state()
        before = self.snapshot()

        result = self.cli('check', '--no-fetch', '--json', expected=None)

        self.assertFalse(packets.exists(), 'the diagnostic initiated a Git transport')
        self.assertFalse(blob_path.exists(), 'the missing object was materialized')
        self.assertEqual(list((self.git_dir / 'objects' / 'pack').iterdir()), [])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assert_preserved(before)

    def test_fetching_check_accepts_valid_alternate_over_malformed_shared_manifest(self):
        self.setup_repository(test_journal.JournalModeTest)
        alternate = self.control / 'alternate.json'
        alternate.write_bytes(self.manifest.read_bytes())
        self.manifest.write_bytes(b'{')
        latest = self.fixture.publish_from_other_clone({'remote.txt': 'remote only\n'})
        self.assertNotEqual(self.fixture.git('rev-parse', 'origin/journal'), latest)

        report = json.loads(self.cli('check', '--manifest', str(alternate), '--json').stdout)

        self.assertEqual(report['manifest_path'], str(alternate))
        self.assertEqual(report['validation']['errors'], [])
        self.assertEqual(self.fixture.git('rev-parse', 'origin/journal'), latest)
