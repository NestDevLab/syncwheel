import importlib
import json
import unittest
from pathlib import Path
from unittest import mock

import test_syncwheel as fixtures


class AuthorityRecoveryTest(unittest.TestCase):
    branch = 'integration/review'

    def setUp(self):
        self.fixture = fixtures.SyncwheelFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.module = self.fixture.load_syncwheel_module()
        self.repo = self.fixture.repo
        self.shared_path = self.repo / '.syncwheel' / 'manifest.json'
        shared = self.fixture.read_manifest()
        shared['authority'] = {'mode': 'ai-managed', 'allow': ['source_change']}
        self.shared_path.write_text(json.dumps(shared) + '\n')
        self.ignore = self.repo / '.gitignore'
        self.ignore.write_text(
            '.syncwheel/profile.local.json\n.syncwheel/ledger/\n'
            '.syncwheel/manifests/*.local.json\n.syncwheel/manifests/*.local-ledger/\n'
        )
        self.fixture.git('add', '.gitignore', '.syncwheel/manifest.json')
        self.fixture.git('commit', '-qm', 'Declare shared authority')
        self.fixture.git('branch', self.branch, 'main')
        self.lane = self.fixture.tmp / 'review-lane'
        self.fixture.git('worktree', 'add', '--detach', '-q', str(self.lane))
        self.personal_path = self.module.personal_manifest_path(self.lane, 'review')
        self.personal_path.parent.mkdir(parents=True)
        personal = self.fixture.read_manifest()
        personal.pop('authority')
        personal['stacks'] = []
        personal['integration'] = {'branch': self.branch, 'base': 'main', 'stacks': []}
        self.personal_path.write_text(json.dumps(personal) + '\n')
        self.assertEqual(self.fixture.git('status', '--porcelain'), '')
        self.assertEqual(self.fixture.git('-C', str(self.lane), 'status', '--porcelain'), '')
        self.intent = self.interrupt_control_persistence()

    def interrupt_control_persistence(self):
        manifest, _ = self.module.load_manifest(self.lane, self.personal_path)
        replay_tip = self.fixture.git('rev-parse', self.branch)
        store = importlib.import_module('syncwheel_manifest_store')

        def interrupt(stage):
            if stage == 'intent_saved':
                raise RuntimeError('simulated process interruption after native intent')

        with (
            self.module.manifest_write_transaction(self.lane, self.personal_path),
            mock.patch.object(store, 'control_manifest_io_checkpoint', side_effect=interrupt),
            self.assertRaisesRegex(RuntimeError, 'simulated process interruption'),
        ):
            self.module.restore_control_manifest_after_integration_rebuild(
                self.lane, self.personal_path, manifest, replay_tip, 'control',
                reason='Exercise native interrupted control persistence',
                command='syncwheel int rebuild',
            )

        events = self.module.load_ledger_events(self.lane, self.personal_path)
        pending = self.module.pending_control_manifest_intents(events)
        self.assertEqual(len(pending), 1)
        self.assertEqual(self.fixture.git('rev-parse', self.branch), replay_tip)
        self.assertNotEqual(pending[0]['payload']['expected_control_commit'], replay_tip)
        return pending[0]['payload']

    def rebuild(self, *extra, expected=0):
        return self.fixture.run_cli(
            'int', 'rebuild', '--personal', 'review', *extra, cwd=self.lane, expected=expected,
            extra_env={'GIT_OPTIONAL_LOCKS': '0'},
        )

    def index_bytes(self, checkout):
        path = self.fixture.git(
            '-C', str(checkout), 'rev-parse', '--path-format=absolute', '--git-path', 'index',
        )
        return Path(path).read_bytes()

    def snapshot(self):
        ledger_root = self.module.ledger_root(self.lane, self.personal_path)
        return {
            'refs': self.fixture.git('for-each-ref'),
            'primary_index': self.index_bytes(self.repo),
            'lane_index': self.index_bytes(self.lane),
            'shared': self.shared_path.read_bytes(),
            'personal': self.personal_path.read_bytes(),
            'ignore': self.ignore.read_bytes(),
            'ledger': {
                path.relative_to(ledger_root): path.read_bytes()
                for path in ledger_root.rglob('*') if path.is_file()
            },
        }

    def test_missing_reason_refuses_before_consuming_pending_personal_control_intent(self):
        before = self.snapshot()

        result = self.rebuild(expected=2)

        self.assertIn('int rebuild requires --reason for an ai-managed repository', result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_explicit_reason_still_allows_native_personal_control_recovery(self):
        before = self.snapshot()

        self.rebuild('--reason', 'Resume reviewed integration work')

        after = self.snapshot()
        self.assertEqual(after['shared'], before['shared'])
        self.assertEqual(after['primary_index'], before['primary_index'])
        self.assertEqual(after['lane_index'], before['lane_index'])
        self.assertEqual(after['ignore'], before['ignore'])
        events = self.module.load_ledger_events(self.lane, self.personal_path)
        self.assertEqual(self.module.pending_control_manifest_intents(events), [])
        self.assertTrue(any(
            event['type'] == 'manifest_saved'
            and event['payload'].get('operation_id') == self.intent['operation_id']
            for event in events
        ))
        self.assertIn(self.intent['expected_control_commit'],
                      self.fixture.git('rev-list', self.branch).splitlines())
