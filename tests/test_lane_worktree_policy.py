"""Lane settings bind to stable registry generations and never update lane state."""

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

import test_worktree_policy as policy_fixtures


class LaneWorktreePolicyTest(TestCase):
    def setUp(self):
        self.base = policy_fixtures.WorktreePolicyTest()
        self.addCleanup(self.base.doCleanups)
        self.base.setUp()
        self.fixture = self.base.fixture
        self.module = self.base.module
        self.profile = self.base.profile
        self.settings = self.base.settings

    def lane(self, name='first', stack='feature-a'):
        return json.loads(self.fixture.run_cli(
            'worktree', 'open', name, '--into', stack, '--json',
        ).stdout)['lane']

    def command(self, lane, action, *args, **kwargs):
        return self.fixture.run_cli('worktree', 'policy', lane['id'], action, *args, '--json', **kwargs)

    def set_policy(self, lane, *args):
        return self.command(lane, 'set', *args, '--apply')

    def record(self, lane):
        registry, path = self.module.load_governed_worktree_registry(self.fixture.repo)
        return registry, path, next(item for item in registry['lanes'] if item['id'] == lane['id'])

    def test_preview_apply_inheritance_clear_and_foreign_state_are_scoped(self):
        lane = self.lane()
        self.base.set_policy('self', '--expiry', 'off', '--writer-exclusivity', 'warn')
        self.profile.write_text(json.dumps({'sentinel': {'keep': True}}))
        registry, path, _ = self.record(lane)
        before = path.read_bytes()
        refs = self.fixture.git('show-ref')
        index = (self.fixture.repo / '.git/index').read_bytes()
        preview = json.loads(self.command(lane, 'set', '--expiry', 'enforced').stdout)
        self.assertFalse(preview['applied'])
        self.assertNotIn('lane_worktree_policies', json.loads(self.profile.read_text()))
        applied = json.loads(self.set_policy(lane, '--expiry', 'enforced').stdout)
        self.assertEqual(applied['effective'], {'expiry': 'enforced', 'writer_exclusivity': 'warn'})
        self.assertEqual(applied['sources'], {'expiry': 'lane', 'writer_exclusivity': 'global'})
        self.assertEqual(applied['lane_binding']['state'], 'bound')
        self.set_policy(lane, '--writer-exclusivity', 'block')
        cleared = json.loads(self.command(lane, 'clear', 'expiry', '--apply').stdout)
        self.assertEqual(cleared['effective'], {'expiry': 'off', 'writer_exclusivity': 'block'})
        self.assertEqual(cleared['sources'], {'expiry': 'global', 'writer_exclusivity': 'lane'})
        self.command(lane, 'clear', '--apply')
        self.assertEqual(json.loads(self.profile.read_text()), {'sentinel': {'keep': True}})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.fixture.git('show-ref'), refs)
        self.assertEqual((self.fixture.repo / '.git/index').read_bytes(), index)

    def test_expiry_and_writer_policy_are_resolved_for_each_observed_lane(self):
        first = self.lane()
        second = self.lane('second')
        self.base.set_policy('self', '--writer-exclusivity', 'block')
        self.set_policy(first, '--expiry', 'off', '--writer-exclusivity', 'off')
        self.assertEqual(self.module.effective_worktree_policy(Path(first['path']))['writer_exclusivity'], 'off')
        self.assertEqual(self.module.effective_worktree_policy(
            Path(first['path']), current_context=False)['writer_exclusivity'], 'block')
        registry, path, _ = self.record(first)
        for item in registry['lanes']:
            item['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)
        before = path.read_bytes()
        observed = json.loads(self.fixture.run_cli(
            'worktree', 'list', '--json', '--repo', str(self.fixture.repo), cwd=Path(first['path']),
        ).stdout)['lanes']
        codes = {item['id']: item['code'] for item in observed}
        self.assertIsNone(codes[first['id']])
        self.assertEqual(codes[second['id']], 'expired')
        self.assertEqual(path.read_bytes(), before)
        other = {'SYNCWHEEL_LANE_OWNER': 'writer-b'}
        self.fixture.run_cli('hooks', 'worktree-guard', '--event', 'pre-commit',
                             cwd=Path(first['path']), extra_env=other)
        blocked = self.fixture.run_cli('hooks', 'worktree-guard', '--event', 'pre-commit',
                                       cwd=Path(second['path']), extra_env=other, expected=2)
        self.assertIn(second['id'], blocked.stderr)
        refused = self.fixture.run_cli('worktree', 'open', 'third', '--into', 'feature-a', '--repo', str(self.fixture.repo),
                                       cwd=Path(first['path']), extra_env=other, expected=2)
        self.assertIn(second['id'], refused.stderr)

    def test_scoped_preflight_cannot_use_one_lanes_relaxation_for_another(self):
        first = self.lane()
        second = self.lane('second')
        self.set_policy(first, '--writer-exclusivity', 'off')
        _, registry_path, _ = self.record(first)
        before_registry = registry_path.read_bytes()
        command = ('stack', 'resolve-integration', 'feature-a', '--empty')
        refused = self.fixture.run_cli(*command, expected=2)
        self.assertIn('second [', refused.stderr)
        self.assertNotIn('first [', refused.stderr)
        self.set_policy(second, '--writer-exclusivity', 'block')
        refused = self.fixture.run_cli(*command, extra_env={'SYNCWHEEL_LANE_OWNER': 'writer-b'}, expected=2)
        self.assertIn('second belongs to writer', refused.stderr)
        self.fixture.run_cli(*command)
        self.assertEqual(registry_path.read_bytes(), before_registry)
        private = Path(second['path']) / 'preserve-this.txt'
        private.write_text('foreign lane data\n')
        second_index = Path(self.fixture.git('-C', second['path'], 'rev-parse', '--git-path', 'index'))
        before_index = second_index.read_bytes()
        before_refs = self.fixture.git('show-ref')
        before_manifest = (self.fixture.repo / '.syncwheel/manifest.json').read_bytes()
        refused = self.fixture.run_cli(*command, expected=2)
        self.assertIn('[dirty]', refused.stderr)
        self.assertEqual(private.read_text(), 'foreign lane data\n')
        self.assertEqual(second_index.read_bytes(), before_index)
        self.assertEqual(self.fixture.git('show-ref'), before_refs)
        self.assertEqual((self.fixture.repo / '.syncwheel/manifest.json').read_bytes(), before_manifest)
        self.assertEqual(registry_path.read_bytes(), before_registry)

    def test_stale_binding_is_ignored_and_requires_explicit_generation_to_rebind(self):
        lane = self.lane()
        self.set_policy(lane, '--expiry', 'off', '--writer-exclusivity', 'off')
        registry, path, current = self.record(lane)
        current['generation_token'] = 'replacement-generation'
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)
        before = path.read_bytes()
        status = json.loads(self.command(lane, 'status').stdout)
        self.assertEqual(status['lane_binding']['state'], 'stale')
        self.assertEqual(status['effective'], {'expiry': 'enforced', 'writer_exclusivity': 'legacy'})
        refused = self.command(lane, 'set', '--expiry', 'off', '--apply', expected=2)
        self.assertIn('--bind-generation', refused.stderr)
        self.command(lane, 'set', '--expiry', 'off', '--bind-generation', 'wrong', '--apply', expected=2)
        rebound = json.loads(self.set_policy(lane, '--expiry', 'off', '--bind-generation',
                                             'replacement-generation').stdout)
        self.assertEqual(rebound['effective'], {'expiry': 'off', 'writer_exclusivity': 'legacy'})
        self.assertEqual(rebound['lane_binding']['state'], 'bound')
        self.assertEqual(path.read_bytes(), before)
        # An obsolete foreign binding cannot poison observation of other lanes.
        profile = json.loads(self.profile.read_text())
        profile['lane_worktree_policies'][lane['id']]['generation_token'] = 'obsolete'
        self.profile.write_text(json.dumps(profile))
        opened = self.lane('unrelated', 'feature-b')
        observed = self.fixture.run_cli('worktree', 'list', '--json')
        self.assertIn('ignoring its overrides', observed.stderr)
        self.assertIn(opened['id'], observed.stdout)
        self.command(lane, 'clear', '--apply')
        self.assertNotIn('lane_worktree_policies', json.loads(self.profile.read_text()))

    def test_current_generation_malformed_bindings_are_diagnosed_and_recoverable(self):
        lane = self.lane()
        other = self.lane('unrelated', 'feature-b')
        self.profile.write_text(json.dumps({'sentinel': {'keep': True}}))
        self.set_policy(other, '--expiry', 'off', '--writer-exclusivity', 'warn')
        initial = json.loads(self.profile.read_text())
        _, registry_path, record = self.record(lane)
        generation = self.module.governed_worktree_generation_token(record)
        before_registry = registry_path.read_bytes()
        before_refs = self.fixture.git('show-ref')
        before_index = (self.fixture.repo / '.git/index').read_bytes()
        variants = (
            {'generation_token': generation, 'worktree_policy': {'expiry': 'bad'}},
            {'generation_token': generation, 'worktree_policy': False},
            {'generation_token': generation, 'worktree_policy': {'unknown': 'off'}},
            {'generation_token': generation, 'worktree_policy': {'expiry': 'off'}, 'unknown': True},
        )
        for entry in variants:
            with self.subTest(entry=entry):
                profile = json.loads(json.dumps(initial))
                profile['lane_worktree_policies'][lane['id']] = entry
                self.profile.write_text(json.dumps(profile))
                status = json.loads(self.command(lane, 'status').stdout)
                self.assertEqual(status['lane_binding']['state'], 'invalid')
                self.assertEqual(status['effective'], {'expiry': 'enforced', 'writer_exclusivity': 'legacy'})
                observed = self.fixture.run_cli('worktree', 'list', '--json')
                self.assertIn('ignoring its overrides', observed.stderr)
                self.assertIn(other['id'], observed.stdout)
                self.assertEqual(json.loads(self.command(other, 'status').stdout)['effective'],
                                 {'expiry': 'off', 'writer_exclusivity': 'warn'})
                before_profile = self.profile.read_bytes()
                self.command(lane, 'set', '--expiry', 'off', '--apply', expected=2)
                self.command(lane, 'clear', 'expiry', '--apply', expected=2)
                self.assertEqual(self.profile.read_bytes(), before_profile)
                rebound = json.loads(self.set_policy(lane, '--expiry', 'off',
                                                     '--bind-generation', generation).stdout)
                self.assertEqual(rebound['lane_binding']['state'], 'bound')
                self.assertEqual(rebound['effective'], {'expiry': 'off', 'writer_exclusivity': 'legacy'})
                self.profile.write_bytes(before_profile)
                self.command(lane, 'clear', '--apply')
                self.assertEqual(json.loads(self.profile.read_text()), initial)
        self.assertEqual(registry_path.read_bytes(), before_registry)
        self.assertEqual(self.fixture.git('show-ref'), before_refs)
        self.assertEqual((self.fixture.repo / '.git/index').read_bytes(), before_index)

    def test_owner_changes_and_legacy_generation_fallback_do_not_change_the_binding(self):
        lane = self.lane()
        registry, path, record = self.record(lane)
        record.pop('generation_token', None)
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)
        self.set_policy(lane, '--expiry', 'off')
        profile = self.profile.read_bytes()
        record['owner'] = 'a-different-owner:987654'
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)
        before = path.read_bytes()
        status = json.loads(self.command(lane, 'status').stdout)
        self.assertEqual(status['lane_binding']['state'], 'bound')
        self.assertEqual(status['effective']['expiry'], 'off')
        self.assertEqual(self.profile.read_bytes(), profile)
        self.assertEqual(path.read_bytes(), before)

    def test_set_rechecks_generation_under_registry_lock_before_profile_save(self):
        lane = self.lane()
        self.profile.write_text('{"sentinel":true}\n')
        before = self.profile.read_bytes()
        original_lock = self.module.governed_worktree_registry_lock

        @contextlib.contextmanager
        def replace_before_lock_body(repo):
            with original_lock(repo):
                registry, _, record = self.record(lane)
                record['generation_token'] = 'concurrent-replacement'
                self.module.save_governed_worktree_registry(repo, registry)
                yield

        args = SimpleNamespace(repo=str(self.fixture.repo), policy_scope='lane',
                               policy_action='set', lane=lane['id'], expiry='off',
                               writer_exclusivity=None, bind_generation=None, apply=True, json=True)
        with mock.patch.object(self.module, 'governed_worktree_registry_lock', replace_before_lock_body):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'generation changed concurrently'):
                self.module.command_worktree_policy(args)
        self.assertEqual(self.profile.read_bytes(), before)

    def test_profile_cas_preserves_concurrent_repository_and_other_lane_preferences(self):
        lane = self.lane()
        self.profile.write_text('{"sentinel":true}\n')
        original_lock = self.module.governed_worktree_registry_lock

        @contextlib.contextmanager
        def competing_profile_update(repo):
            with original_lock(repo):
                profile = self.module.load_repo_profile(repo)
                profile['worktree_policy'] = {'writer_exclusivity': 'block'}
                profile['lane_worktree_policies'] = {'unrelated': {
                    'generation_token': 'foreign-generation', 'worktree_policy': {'expiry': 'off'},
                }}
                self.module.save_repo_profile(repo, profile)
                yield

        args = SimpleNamespace(repo=str(self.fixture.repo), policy_scope='lane',
                               policy_action='set', lane=lane['id'], expiry='off',
                               writer_exclusivity=None, bind_generation=None, apply=True, json=True)
        with mock.patch.object(self.module, 'governed_worktree_registry_lock', competing_profile_update):
            with self.assertRaisesRegex(self.module.SyncwheelError, 'configuration changed concurrently'):
                self.module.command_worktree_policy(args)
        current = json.loads(self.profile.read_text())
        self.assertEqual(current['worktree_policy'], {'writer_exclusivity': 'block'})
        self.assertEqual(current['lane_worktree_policies'], {'unrelated': {
            'generation_token': 'foreign-generation', 'worktree_policy': {'expiry': 'off'},
        }})
        self.assertTrue(current['sentinel'])

    def test_two_lane_updates_preserve_each_other_and_repository_keys(self):
        first = self.lane()
        second = self.lane('second', 'feature-b')
        self.profile.write_text(json.dumps({'sentinel': {'keep': True}}))
        self.base.set_policy('repo', '--expiry', 'enforced')
        self.set_policy(first, '--expiry', 'off')
        before = json.loads(self.profile.read_text())
        self.set_policy(second, '--writer-exclusivity', 'warn')
        current = json.loads(self.profile.read_text())
        self.assertEqual(current['sentinel'], before['sentinel'])
        self.assertEqual(current['worktree_policy'], before['worktree_policy'])
        self.assertEqual(current['lane_worktree_policies'][first['id']],
                         before['lane_worktree_policies'][first['id']])
        self.command(second, 'clear', '--apply')
        self.assertEqual(json.loads(self.profile.read_text()), before)

    def test_missing_lane_binding_can_be_inspected_and_cleared_without_registry_writes(self):
        lane = self.lane()
        self.set_policy(lane, '--expiry', 'off')
        registry, path, record = self.record(lane)
        registry['lanes'].remove(record)
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)
        before = path.read_bytes()
        status = json.loads(self.command(lane, 'status').stdout)
        self.assertEqual(status['lane_binding']['state'], 'missing')
        self.assertEqual(status['effective']['expiry'], 'enforced')
        self.command(lane, 'set', '--expiry', 'off', '--apply', expected=2)
        self.command(lane, 'clear', '--apply')
        self.assertNotIn('lane_worktree_policies', json.loads(self.profile.read_text()))
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(Path(lane['path']).exists())
        self.command(lane, 'status', expected=2)

    def test_lane_relaxation_does_not_waive_physical_locks_or_foreign_cleanup_hold(self):
        lane = self.lane()
        self.set_policy(lane, '--expiry', 'off', '--writer-exclusivity', 'off')
        registry, path, record = self.record(lane)
        record['owner'] = 'different-owner'
        record['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.module.save_governed_worktree_registry(self.fixture.repo, registry)
        self.fixture.git('worktree', 'lock', '--reason', 'Retain foreign work', lane['path'])
        before = path.read_bytes()
        hold = self.module._governed_worktrees(self.fixture.repo).automatic_cleanup_hold()
        self.assertIsNotNone(hold(record))
        report = json.loads(self.fixture.run_cli('worktree', 'list', '--json').stdout)
        self.assertEqual(report['lanes'][0]['code'], 'locked')
        self.assertEqual(path.read_bytes(), before)
        private = json.loads(self.profile.read_text())
        self.assertEqual(private['lane_worktree_policies'][lane['id']]['generation_token'],
                         self.module.governed_worktree_generation_token(record))
