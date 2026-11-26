"""Resolve policy data without granting authority or fabricating execution proof."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from syncwheel_errors import SyncwheelError
from syncwheel_policy import (
    FIELD_VALUES, DEFAULT_PREFERENCES, PRESETS, resolve_preferences,
)
from syncwheel_worktree_policy import resolve_policy


def document(**values):
    return {'worktree_policy': values, 'unrelated': {'keep': True}}


class PolicyResolutionTest(unittest.TestCase):
    def test_absence_preserves_legacy_and_does_not_select_a_preset(self):
        resolved = resolve_preferences({})
        self.assertEqual(resolved['effective'], DEFAULT_PREFERENCES)
        self.assertEqual(set(resolved['sources'].values()), {'default'})
        self.assertEqual(resolve_policy({}, {}), {
            'effective': {'expiry': 'enforced', 'writer_exclusivity': 'legacy'},
            'sources': {'expiry': 'default', 'writer_exclusivity': 'default'},
            'global': {}, 'repo': {},
        })

    def test_presets_have_a_complete_valid_matrix_without_admin_or_authority_fields(self):
        for name, values in PRESETS.items():
            with self.subTest(preset=name):
                self.assertEqual(set(values), set(FIELD_VALUES))
                self.assertEqual(resolve_preferences(document(preset=name))['effective'], values)
                for field, value in values.items():
                    self.assertIn(value, FIELD_VALUES[field])
        self.assertEqual(PRESETS['minimal']['checks'], 'required')
        self.assertEqual(PRESETS['strict']['integration_validation'], 'required')
        self.assertEqual(PRESETS['standard']['independent_review'], 'required')
        self.assertNotIn('allowed_bypasses', FIELD_VALUES)
        self.assertNotIn('authority', FIELD_VALUES)

    def test_each_level_expands_its_preset_then_overlays_only_explicit_fields(self):
        global_document = document(preset='strict', expiry='off')
        repo = document(writer_exclusivity='warn')
        lane = document(checks='required')
        inputs = copy.deepcopy((global_document, repo, lane))
        result = resolve_preferences(global_document, repo, lane)
        self.assertEqual(result['effective']['expiry'], 'off')
        self.assertEqual(result['effective']['writer_exclusivity'], 'warn')
        self.assertEqual(result['effective']['checks'], 'required')
        self.assertEqual(result['sources']['expiry'], 'global')
        self.assertEqual(result['sources']['writer_exclusivity'], 'repo')
        self.assertEqual(result['sources']['checks'], 'lane')
        self.assertEqual(result['provenance']['tests'], {
            'scope': 'global', 'kind': 'preset', 'preset': 'strict',
        })
        self.assertEqual(result['provenance']['expiry']['kind'], 'override')
        self.assertEqual((global_document, repo, lane), inputs)

    def test_lane_preset_replaces_lower_values_and_clear_restores_each_parent(self):
        global_document = document(preset='strict')
        repo = document(preset='minimal', checks='all')
        lane = document(preset='disabled', expiry='enforced')
        selected = resolve_preferences(global_document, repo, lane)
        self.assertEqual(selected['effective']['writer_exclusivity'], 'off')
        self.assertEqual(selected['effective']['expiry'], 'enforced')
        del lane['worktree_policy']['preset']
        inherited = resolve_preferences(global_document, repo, lane)
        self.assertEqual(inherited['effective']['writer_exclusivity'], 'warn')
        self.assertEqual(inherited['effective']['checks'], 'all')
        del repo['worktree_policy']['checks']
        self.assertEqual(resolve_preferences(global_document, repo, lane)['effective']['checks'], 'required')
        del repo['worktree_policy']['preset']
        self.assertEqual(resolve_preferences(global_document, repo, lane)['effective']['checks'], 'all')
        self.assertEqual(resolve_preferences({}, {}, {})['effective'], DEFAULT_PREFERENCES)

    def test_disabled_and_lane_overrides_cannot_lower_a_supplied_floor(self):
        floor = {'expiry': 'enforced', 'writer_exclusivity': 'block',
                 'checks': 'required', 'independent_review': 'required'}
        result = resolve_preferences(document(preset='strict'), document(preset='disabled'),
                                     document(checks='none', expiry='off'), floor=floor)
        for field, value in floor.items():
            self.assertEqual(result['effective'][field], value)
            self.assertEqual(result['sources'][field], 'floor')
        self.assertEqual(result['requested']['expiry'], 'off')
        self.assertEqual(result['provenance']['expiry']['requested_provenance'],
                         {'scope': 'lane', 'kind': 'override'})
        self.assertEqual(result['floor'], floor)

    def test_floor_does_not_weaken_a_stricter_request_or_mutate_input(self):
        floor = {'checks': 'required', 'independent_review': 'sensitive'}
        original = dict(floor)
        result = resolve_preferences(document(preset='strict'), floor=floor)
        self.assertEqual(result['effective']['checks'], 'all')
        self.assertEqual(result['effective']['independent_review'], 'required')
        self.assertEqual(result['sources']['checks'], 'global')
        self.assertEqual(floor, original)
        self.assertEqual(resolve_preferences({}, floor={'checks': 'required'})['effective']['checks'], 'required')

    def test_invalid_preferences_or_floor_fail_closed_without_silently_dropping_fields(self):
        for bad in (document(preset='custom'), document(preset=[]), document(preset=None),
                    document(writer_exclusivity='legacy'), document(authority='off'),
                    {'worktree_policy': False}):
            with self.subTest(document=bad), self.assertRaises(SyncwheelError):
                resolve_preferences(bad)
        for floor in (False, {'checks': 'legacy'}, {'unknown': 'off'}, {'checks': []}):
            with self.subTest(floor=floor), self.assertRaises(SyncwheelError):
                resolve_preferences({}, floor=floor)

    def test_existing_worktree_api_keeps_its_schema_and_refuses_unwired_presets(self):
        result = resolve_policy(document(expiry='off'), document(writer_exclusivity='block'))
        self.assertEqual(result, {
            'effective': {'expiry': 'off', 'writer_exclusivity': 'block'},
            'sources': {'expiry': 'global', 'writer_exclusivity': 'repo'},
            'global': {'expiry': 'off'}, 'repo': {'writer_exclusivity': 'block'},
        })
        with self.assertRaises(SyncwheelError):
            resolve_policy(document(preset='minimal'))


if __name__ == '__main__':
    unittest.main()
