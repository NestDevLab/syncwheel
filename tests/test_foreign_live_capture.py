import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import test_syncwheel as fixtures


class ForeignLiveCaptureTest(unittest.TestCase):
    owner = {'SYNCWHEEL_LANE_OWNER': 'fixture-owner'}
    other = {'SYNCWHEEL_LANE_OWNER': 'fixture-other'}
    stack = 'independent'
    lane_id = 'live-source'

    def setUp(self):
        self.fixture = fixtures.SyncwheelFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repo = self.fixture.repo
        self.module = self.fixture.load_syncwheel_module()

    def git(self, *args):
        return self.fixture.git(*args)

    def cli(self, *args, owner=None, expected=0):
        return self.fixture.run_cli(
            *args, extra_env=self.other if owner is None else owner, expected=expected,
        )

    def make_stack(self):
        self.cli('stack', 'create', self.stack, '--branch', f'pr/{self.stack}',
                 '--include-in-integration')

    def open_lane(self, *, targeted=True):
        if targeted:
            self.make_stack()
        response = self.cli(
            'worktree', 'open', self.lane_id,
            *(['--into', self.stack] if targeted else []), '--json', owner=self.owner,
        )
        self.lane = json.loads(response.stdout)['lane']
        self.assertEqual(self.lane['owner'], self.owner['SYNCWHEEL_LANE_OWNER'])
        expires = datetime.fromisoformat(self.lane['lease_expires_at'])
        self.assertGreater(expires, datetime.now(timezone.utc))
        self.path = Path(self.lane['path'])
        self.product = self.path / 'authored.txt'
        self.product.write_bytes(b'committed lane work\n')
        self.git('-C', str(self.path), 'add', self.product.name)
        self.git('-C', str(self.path), 'commit', '-qm', 'Commit lane work')
        self.tip = self.git('-C', str(self.path), 'rev-parse', 'HEAD')
        self.index = Path(self.git(
            '-C', str(self.path), 'rev-parse', '--path-format=absolute', '--git-path', 'index',
        ))
        self.assertEqual(self.git('-C', str(self.path), 'status', '--porcelain'), '')

    def observe_lane(self):
        registry, path = self.module.load_governed_worktree_registry(self.repo)
        refs = self.git('for-each-ref', '--format=%(refname) %(objectname)',
                        'refs/syncwheel/recovery')
        files = {
            str(item.relative_to(self.path)): (item.stat().st_mode, item.read_bytes())
            for item in self.path.rglob('*') if item.is_file()
        }
        return {
            'registry': path.read_bytes(),
            'record': next(lane for lane in registry['lanes'] if lane['id'] == self.lane_id),
            'branch_tip': self.module.ref_tip(self.repo, self.lane['branch']),
            'registration': self.module.find_worktree_record_for_branch(
                self.repo, self.lane['branch']
            ),
            'index': self.index.read_bytes(),
            'files': files,
            'recovery_refs': refs,
        }

    def assert_recipe(self):
        manifest = self.fixture.read_manifest()
        stack = next(stack for stack in manifest['stacks'] if stack['id'] == self.stack)
        self.assertEqual(stack['commits'], [self.tip])

    def assert_foreign_capture_preserves_lane(self, *command):
        before = self.observe_lane()
        result = self.cli(*command)
        self.assert_recipe()
        self.assertTrue(self.path.is_dir(), result.stdout + result.stderr)
        self.assertEqual(self.observe_lane(), before)
        return result

    def test_create_records_commit_without_capturing_foreign_lane(self):
        self.open_lane(targeted=False)
        self.assert_foreign_capture_preserves_lane(
            'stack', 'create', self.stack, self.tip, '--branch', f'pr/{self.stack}',
        )

    def test_add_records_commit_without_capturing_foreign_lane(self):
        self.open_lane()
        self.assert_foreign_capture_preserves_lane('stack', 'add', self.stack, self.tip)

    def test_capture_records_commit_without_capturing_foreign_lane(self):
        self.open_lane()
        self.assert_foreign_capture_preserves_lane(
            'stack', 'capture-integration', self.stack, self.tip,
        )
        self.assertEqual(self.git('rev-parse', f'pr/{self.stack}'), self.tip)

    def test_foreign_lane_is_preserved_with_dirt_and_git_lock(self):
        self.open_lane()
        self.product.write_bytes(b'uncommitted edits\n')
        self.product.chmod(0o755)
        (self.path / 'untracked.bin').write_bytes(b'fixture\x00')
        self.git('worktree', 'lock', '--reason', 'Keep foreign validation', str(self.path))
        self.assert_foreign_capture_preserves_lane('stack', 'add', self.stack, self.tip)

    def test_foreign_lane_with_invalid_lease_is_preserved(self):
        self.open_lane()
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = 'invalid'
        self.module.save_governed_worktree_registry(self.repo, registry)
        self.assert_foreign_capture_preserves_lane('stack', 'add', self.stack, self.tip)

    def test_subsequent_mutation_preserves_foreign_pending_cleanup_and_moved_path(self):
        self.open_lane()
        command = ('stack', 'add', self.stack, self.tip)
        self.assert_foreign_capture_preserves_lane(*command)
        self.mark_lane_pending_and_moved()

        self.assert_foreign_capture_preserves_lane(*command)

    def mark_lane_pending_and_moved(self):
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['state'] = 'captured_pending_cleanup'
        self.module.save_governed_worktree_registry(self.repo, registry)
        moved = self.path.with_name('moved-foreign-lane')
        self.git('worktree', 'move', str(self.path), str(moved))
        self.path = moved

    def test_capture_does_not_finish_foreign_interrupted_release(self):
        self.open_lane()
        release = ('worktree', 'release', self.lane_id, '--reason', 'Preserve fixture',
                   '--apply', '--json')
        with mock.patch.dict('os.environ', self.owner):
            self.fixture.run_cli_until_cleanup_sigkill('after_cleanup_intent', *release)
        record = self.observe_lane()['record']
        self.assertEqual(record['pending_reason'], 'reaping')
        self.assertEqual(record['cleanup_event_type'], 'governed_worktree_released')

        self.assert_foreign_capture_preserves_lane(
            'stack', 'capture-integration', self.stack, self.tip,
        )

    def assert_classification_preserves_lane(self):
        self.git('merge', '--ff-only', self.tip)
        before = self.observe_lane()
        command = ('stack', 'classify-integration', self.stack, self.tip)
        preview = json.loads(self.cli(*command).stdout)
        self.cli(*command, '--apply', '--plan-digest', preview['planDigest'])
        manifest = self.fixture.read_manifest()
        stack = next(stack for stack in manifest['stacks'] if stack['id'] == self.stack)
        self.assertEqual(stack['integration_only_commits'], [self.tip])
        self.assertEqual(self.observe_lane(), before)

    def test_classification_does_not_capture_foreign_lane(self):
        self.open_lane()
        self.assert_classification_preserves_lane()

    def test_classification_preserves_foreign_pending_cleanup_and_moved_path(self):
        self.open_lane()
        self.mark_lane_pending_and_moved()
        self.assert_classification_preserves_lane()

    def assert_released_tip(self):
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.module.ref_tip(self.repo, self.lane['branch']))
        self.assertIsNone(self.module.find_worktree_record_for_branch(self.repo, self.lane['branch']))
        self.assertIn(self.tip, self.git(
            'for-each-ref', '--format=%(objectname)', 'refs/syncwheel/recovery/lanes',
        ).splitlines())

    def test_same_owner_capture_keeps_native_cleanup(self):
        self.open_lane()
        self.cli('stack', 'capture-integration', self.stack, self.tip, owner=self.owner)
        self.assert_recipe()
        self.assert_released_tip()

    def test_explicit_owner_release_keeps_commit_and_allows_retry(self):
        self.open_lane()
        command = ('stack', 'add', self.stack, self.tip)
        held = self.assert_foreign_capture_preserves_lane(*command)
        self.assertIn('owner fixture-owner live', held.stderr)
        release = ('worktree', 'release', self.lane_id, '--reason',
                   'Preserve committed fixture work', '--json')
        preview = json.loads(self.cli(*release, owner=self.owner).stdout)
        self.assertFalse(preview['applied'])
        result = json.loads(self.cli(*release, '--apply', owner=self.owner).stdout)
        self.assertTrue(result['applied'])
        self.assertEqual(self.git('rev-parse', result['lane']['recovery_ref']), self.tip)
        self.assert_released_tip()
        self.cli(*command)
        self.assert_recipe()
