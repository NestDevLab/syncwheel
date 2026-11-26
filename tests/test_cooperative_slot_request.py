"""Provenance derives from channel contracts and actual immutable Git objects."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'scripts'), str(Path(__file__).parent)]
import test_channels
from _replay_support import hermetic_environment
from syncwheel_errors import SyncwheelError
from syncwheel_test_slot import derive_channel_request
from syncwheel_test_slot_native import NativeCooperativeJob


class CooperativeSlotRequestTest(unittest.TestCase):
    def setUp(self):
        self.environment = tempfile.TemporaryDirectory(prefix='syncwheel-request-environment-')
        self.addCleanup(self.environment.cleanup)
        root = Path(self.environment.name)
        environment = hermetic_environment(root)
        environment.update(XDG_CONFIG_HOME=str(root/'config'), XDG_STATE_HOME=str(root/'state'),
                           SYNCWHEEL_UPDATE_SETTINGS_PATH=str(root/'settings.json'),
                           SYNCWHEEL_REPO_REGISTRY=str(root/'registry.json'))
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start(); self.addCleanup(patch.stop)
        self.fixture = test_channels.DeploymentChannelTest()
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.fixture.create('selected', stacks=('a',))
        self.fixture.apply_channel('selected')
        self.module = self.fixture.load_module()
        self.plan = self.fixture.plan('selected')
        self.blob = self.fixture.git('hash-object', '-w', '.syncwheel/manifest.json')
        self.workspace = {'clean': True, 'head': self.fixture.git('rev-parse', 'channel/selected'),
                          'tree': self.fixture.git('rev-parse', 'channel/selected^{tree}'),
                          'path': str(self.fixture.repo), 'admin': str(self.fixture.repo/'.git'),
                          'device': 1, 'inode': 2, 'lockfiles': {}}
        self.native = NativeCooperativeJob.__new__(NativeCooperativeJob)
        self.native.workspace = self.fixture.repo
        self.native.api = self.module
        self.inputs = {'manifest_blob': self.blob, 'channel_plan': self.plan,
                       'argv': [sys.executable, '-c', 'print("fixture")'],
                       'environment': {'PATH': '/usr/bin:/bin'},
                       'observations': {'approved': True, 'all_checks_passed': True}}

    def derive(self, inputs=None):
        return derive_channel_request(inputs or self.inputs, self.workspace,
            read_manifest_blob=self.native.read_manifest_blob,
            validate_channel=lambda manifest, channel: self.module.require_channel_materialization_valid(
                self.fixture.repo, manifest, channel),
            observe_tools=lambda argv: {'verified_executable_fixture': argv[0]})

    def resign(self, plan):
        body = {key: value for key, value in plan.items() if key not in {'planDigest', 'operationId'}}
        return self.module.finalize_channel_plan(body)

    def test_exact_channel_and_blob_derive_facts_without_promoting_caller_claims(self):
        request = self.derive()
        self.assertEqual(request['verified_provenance']['manifest_blob'], self.blob)
        self.assertEqual(request['sources'], self.plan['composition'])
        self.assertEqual(request['tree'], self.workspace['tree'])
        self.assertEqual(request['reported_observations']['caller'], self.inputs['observations'])
        self.assertFalse(request['reported_observations']['origin_or_review_approval_verified'])
        self.assertNotIn('approved', request['verified_provenance'])

    def test_changed_plan_payload_is_rejected_before_it_can_be_a_request(self):
        changed = copy.deepcopy(self.inputs)
        changed['channel_plan']['baseRevision'] = '0' * 40
        with self.assertRaisesRegex(SyncwheelError, 'plan digest'):
            self.derive(changed)

    def test_self_consistent_forged_pins_still_disagree_with_the_actual_control_blob(self):
        changed = copy.deepcopy(self.inputs)
        changed['channel_plan']['composition'] = []
        changed['channel_plan'] = self.resign(changed['channel_plan'])
        with self.assertRaisesRegex(SyncwheelError, 'pins differ'):
            self.derive(changed)

    def test_a_different_real_blob_cannot_satisfy_the_reviewed_plan(self):
        import json
        changed = copy.deepcopy(self.inputs)
        manifest = self.fixture.manifest()
        manifest['integration']['branch'] = 'different-integration'
        path = Path(self.environment.name)/'different-manifest.json'
        path.write_text(json.dumps(manifest))
        changed['manifest_blob'] = self.fixture.git('hash-object', '-w', str(path))
        with self.assertRaisesRegex(SyncwheelError, 'immutable control manifest'):
            self.derive(changed)
        changed['manifest_blob'] = 'f' * 40
        with self.assertRaisesRegex(SyncwheelError, 'blob is unavailable'):
            self.derive(changed)

    def test_caller_cannot_supply_verified_candidate_or_tool_labels(self):
        for field in ('candidate', 'tree', 'workspace', 'tools', 'verified_provenance'):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.inputs)
                changed[field] = {'caller': 'claimed'}
                with self.assertRaisesRegex(SyncwheelError, 'accepts only'):
                    self.derive(changed)

    def test_invalid_command_and_namespace_are_rejected_before_reservation(self):
        changed = copy.deepcopy(self.inputs)
        changed['argv'] = []
        with self.assertRaisesRegex(SyncwheelError, 'argv'):
            self.derive(changed)
        for key in ('GIT_DIR', 'LD_PRELOAD', 'PYTHONPATH', 'DBUS_SESSION_BUS_ADDRESS'):
            changed = copy.deepcopy(self.inputs)
            changed['environment'][key] = 'different'
            with self.assertRaisesRegex(SyncwheelError, 'namespaces'):
                self.derive(changed)

    def test_channel_tree_projection_does_not_move_any_refs_or_leave_a_worktree(self):
        channel = self.module.require_channel(self.fixture.manifest(), 'selected')
        refs = self.fixture.git('show-ref')
        worktrees = self.fixture.git('worktree', 'list', '--porcelain')
        projected = self.module.materialize_channel_tree(self.fixture.repo, channel)
        self.assertEqual(projected, self.workspace['tree'])
        self.assertEqual(self.fixture.git('show-ref'), refs)
        self.assertEqual(self.fixture.git('worktree', 'list', '--porcelain'), worktrees)

    def test_wrong_workspace_head_refuses_before_native_reservation(self):
        native = self.native
        changed = copy.deepcopy(self.workspace)
        changed['head'] = self.fixture.base
        with mock.patch('syncwheel_test_slot_native.workspace_observation', return_value=changed):
            with self.assertRaisesRegex(SyncwheelError, 'does not match the selected'):
                native.derive_request(self.inputs)

    def test_empty_baseline_channel_is_a_valid_explicit_test_input(self):
        self.fixture.create('baseline', stacks=())
        self.fixture.apply_channel('baseline')
        channel = self.module.require_channel(self.fixture.manifest(), 'baseline')
        self.assertEqual(self.module.materialize_channel_tree(self.fixture.repo, channel),
                         self.fixture.git('rev-parse', self.fixture.base + '^{tree}'))
        inputs = dict(self.inputs, channel_plan=self.fixture.plan('baseline'),
                      manifest_blob=self.fixture.git('hash-object', '-w', '.syncwheel/manifest.json'))
        self.workspace['head'] = self.fixture.git('rev-parse', 'channel/baseline')
        self.workspace['tree'] = self.fixture.git('rev-parse', 'channel/baseline^{tree}')
        request = self.derive(inputs)
        self.assertEqual(request['sources'], [])

    def test_unrelated_missing_history_is_not_loaded_for_the_selected_recipe(self):
        import json
        manifest = self.fixture.manifest()
        foreign = next(stack for stack in manifest['stacks'] if stack['id'] == 'b')
        foreign.update(branch='pr/unavailable-foreign', base='origin/unavailable-foreign', commits=['f'*40])
        (self.fixture.repo/'.syncwheel/manifest.json').write_text(json.dumps(manifest))
        inputs = dict(self.inputs, channel_plan=self.fixture.plan('selected'),
                      manifest_blob=self.fixture.git('hash-object', '-w', '.syncwheel/manifest.json'))
        request = self.derive(inputs)
        self.assertEqual([source['stack'] for source in request['sources']], ['a'])

    def test_malformed_snapshot_and_boolean_schema_fail_as_request_errors(self):
        changed = copy.deepcopy(self.inputs)
        changed['channel_plan']['before'] = 'not-an-object'
        changed['channel_plan'] = self.resign(changed['channel_plan'])
        with self.assertRaisesRegex(SyncwheelError, 'snapshot differs'):
            self.derive(changed)
        changed = copy.deepcopy(self.inputs)
        changed['channel_plan']['schemaVersion'] = True
        changed['channel_plan'] = self.resign(changed['channel_plan'])
        with self.assertRaisesRegex(SyncwheelError, 'channel plan schema'):
            self.derive(changed)


if __name__ == '__main__':
    unittest.main()
