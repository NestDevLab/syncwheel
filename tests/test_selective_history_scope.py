"""Only unchanged recipes outside the real dependency closure may defer history."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from syncwheel_coordination import unchanged_foreign_history_scope


class SelectiveHistoryScopeTest(unittest.TestCase):
    def test_nine_unrelated_recipes_remain_deferred_without_author_metadata(self):
        remote = {'stacks': [{'id': f'foreign-{i}', 'commits': [str(i) * 40]}
                             for i in range(9)]}
        composed = copy.deepcopy(remote)
        composed['stacks'].append({'id': 'target', 'commits': ['f' * 40]})
        before = copy.deepcopy((remote, composed))
        self.assertEqual(unchanged_foreign_history_scope(remote, composed, 'target'),
                         [f'foreign-{i}' for i in range(9)])
        self.assertEqual((remote, composed), before)

    def test_transitive_dependency_is_never_deferred_with_foreign_recipes(self):
        remote = {'stacks': [{'id': 'dependency', 'depends_on': ['broken-leaf']},
                             {'id': 'broken-leaf', 'commits': ['f' * 40]},
                             {'id': 'foreign'}]}
        composed = copy.deepcopy(remote)
        composed['stacks'].append({'id': 'target', 'depends_on': ['dependency']})
        self.assertEqual(unchanged_foreign_history_scope(remote, composed, 'target'),
                         ['foreign'])

    def test_changed_foreign_recipe_is_not_remote_unchanged(self):
        remote = {'stacks': [{'id': 'foreign', 'commits': ['a' * 40]}]}
        composed = {'stacks': [{'id': 'foreign', 'commits': ['b' * 40]}, {'id': 'target'}]}
        self.assertEqual(unchanged_foreign_history_scope(remote, composed, 'target'), [])

    def test_legacy_base_owner_closure_uses_complete_localized_manifest(self):
        localized = [
            {'id': 'target', 'branch': 'pr/target', 'base': 'origin/pr/middle'},
            {'id': 'middle', 'branch': 'pr/middle', 'base': 'pr/leaf'},
            {'id': 'leaf', 'branch': 'pr/leaf', 'base': 'origin/main'},
            {'id': 'foreign', 'branch': 'pr/foreign', 'base': 'origin/main'},
        ]
        remote = {'stacks': copy.deepcopy(localized[1:])}
        composed = {'stacks': copy.deepcopy(localized)}
        for stack in composed['stacks']:
            stack['base'] = {'kind': 'remote-ref', 'ref': 'refs/heads/main'}
        remote['stacks'] = copy.deepcopy(composed['stacks'][1:])
        self.assertEqual(unchanged_foreign_history_scope(
            remote, composed, 'target', localized_stacks=localized), ['foreign'])
