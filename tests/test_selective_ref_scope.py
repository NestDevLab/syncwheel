"""A selected compose scope never upgrades foreign observation into alignment."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from syncwheel_errors import SyncwheelError
from syncwheel_selective_publication import (
    compose_ref_validation_scope, require_compose_ref_validation_scope,
    required_ref_values, foreign_ref_observations, same_compose_publication_plan, local_projection_observation,
)


class SelectiveRefScopeTest(unittest.TestCase):
    def fixture(self):
        foreign = [{'id': f'foreign-{i}', 'branch': f'pr/foreign-{i}',
                    'base': 'origin/main', 'commits': [str(i) * 40]} for i in range(9)]
        dependencies = [{'id': 'dependency', 'branch': 'pr/dependency', 'base': 'pr/leaf'},
                        {'id': 'leaf', 'branch': 'pr/leaf', 'base': 'origin/main'}]
        target = {'id': 'target', 'branch': 'pr/target', 'base': 'origin/pr/dependency'}
        stacks = foreign + dependencies + [target]
        manifest = {'version': 2, 'stacks': stacks,
                    'integration': {'branch': 'integration/shared'}}
        state = {'manifest': {'stacks': copy.deepcopy(stacks[:-1])},
                 'managed_refs': {f"refs/heads/{stack['branch']}": 'a' * 40
                                  for stack in stacks[:-1]}}
        state['managed_refs']['refs/heads/integration/shared'] = 'b' * 40
        return manifest, {'stacks': copy.deepcopy(stacks)}, state

    def test_nine_foreign_refs_are_preserved_but_real_base_dependencies_are_required(self):
        manifest, snapshot, state = self.fixture()
        before = copy.deepcopy((manifest, snapshot, state))
        scope = compose_ref_validation_scope(manifest, snapshot, state, 'target')
        self.assertEqual(scope['requiredStackIds'], ['dependency', 'leaf', 'target'])
        self.assertEqual(scope['requiredRefs'], ['refs/heads/pr/dependency',
                                              'refs/heads/pr/leaf', 'refs/heads/pr/target'])
        self.assertEqual(len(scope['preservedRefs']), 10)
        observed = dict(state['managed_refs'])
        observed['refs/heads/pr/foreign-0'] = None
        observed['refs/heads/pr/foreign-1'] = 'c' * 40
        report = foreign_ref_observations(scope, observed)
        self.assertEqual(report['refs/heads/pr/foreign-0']['status'], 'missing')
        self.assertEqual(report['refs/heads/pr/foreign-1']['status'], 'diverged')
        self.assertTrue(all(not item['qualified'] for item in report.values()))
        self.assertEqual(required_ref_values(observed, scope),
                         required_ref_values(state['managed_refs'], scope))
        self.assertEqual((manifest, snapshot, state), before)

    def test_an_explicit_integration_base_is_a_required_resource(self):
        manifest, snapshot, state = self.fixture()
        manifest['stacks'][-1]['base'] = 'origin/integration/shared'
        snapshot['stacks'][-1]['base'] = 'origin/integration/shared'
        scope = compose_ref_validation_scope(manifest, snapshot, state, 'target')
        self.assertIn('refs/heads/integration/shared', scope['requiredRefs'])
        self.assertNotIn('refs/heads/integration/shared', scope['preservedRefs'])

    def test_scope_tampering_or_non_compose_use_is_rejected(self):
        manifest, snapshot, state = self.fixture()
        scope = compose_ref_validation_scope(manifest, snapshot, state, 'target')
        require_compose_ref_validation_scope(scope, manifest, snapshot, state, 'compose-stack:target')
        for operation, candidate in [('stack:target', scope),
                                     ('compose-stack:target', {**scope, 'requiredRefs': []})]:
            with self.subTest(operation=operation), self.assertRaises(SyncwheelError):
                require_compose_ref_validation_scope(candidate, manifest, snapshot, state, operation)
        manifest['version'] = 3
        self.assertIsNone(compose_ref_validation_scope(manifest, snapshot, state, 'target'))
        with self.assertRaises(SyncwheelError):
            require_compose_ref_validation_scope(scope, manifest, snapshot, state, 'compose-stack:target')

    def test_missing_control_anchor_cannot_bootstrap_a_selective_operation(self):
        manifest, snapshot, state = self.fixture()
        state['managed_refs'].pop('refs/heads/integration/shared')
        with self.assertRaisesRegex(SyncwheelError, 'historical control anchor'):
            compose_ref_validation_scope(manifest, snapshot, state, 'target')

    def test_foreign_observations_do_not_lease_untouched_resources(self):
        scope = {'kind': 'test-scope'}
        old = {'refValidationScope': scope, 'planDigest': 'old',
               'foreignRefObservations': {'foreign': 'present'}, 'sourceTip': 'a'}
        new = {**old, 'planDigest': 'new', 'foreignRefObservations': {'foreign': 'missing'}}
        self.assertTrue(same_compose_publication_plan(old, new))
        self.assertFalse(same_compose_publication_plan(old, {**new, 'sourceTip': 'b'}))
        self.assertFalse(same_compose_publication_plan(old, {**new, 'refValidationScope': {}}))
        self.assertFalse(same_compose_publication_plan({'planDigest': 'a'}, {'planDigest': 'b'}))

    def test_control_anchor_detection_cannot_collide_with_a_real_stack_id(self):
        manifest, snapshot, state = self.fixture()
        manifest['stacks'][-1].update(id='__control_anchor__', base='origin/integration/shared')
        snapshot['stacks'][-1].update(id='__control_anchor__', base='origin/integration/shared')
        scope = compose_ref_validation_scope(manifest, snapshot, state, '__control_anchor__')
        self.assertIn('refs/heads/integration/shared', scope['requiredRefs'])

    def test_unqualified_local_projection_never_grants_missing_control_target_or_full_publish(self):
        def refuse(published, local):
            raise SyncwheelError('not a control suffix')
        with self.assertRaisesRegex(SyncwheelError, 'control suffix'):
            local_projection_observation(None, 'published', 'local',
                                         commit_available=lambda tip: True, prove_suffix=refuse)
        report = local_projection_observation({}, 'published', 'local',
                                               commit_available=lambda tip: True, prove_suffix=refuse)
        self.assertEqual(report, {'status': 'unqualified', 'controlSuffix': None})
        with self.assertRaisesRegex(SyncwheelError, 'control target is unavailable'):
            local_projection_observation({}, 'published', 'local',
                                         commit_available=lambda tip: False, prove_suffix=refuse)
