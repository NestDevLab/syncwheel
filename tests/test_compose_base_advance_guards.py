"""Reject unsafe base advances using real, isolated Git histories."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from syncwheel_compose_base_advance import verify_compose_base_advance
from syncwheel_errors import SyncwheelError


class ComposeBaseAdvanceGuardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='syncwheel-advance-guards-')
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                        GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        self.git('init', '-q', '-b', 'main')
        self.tree = self.git('mktree', input_text='')
        self.base = self.commit('base')
        self.first = self.commit('first', self.base)
        self.old_tip = self.commit('second', self.first)
        self.new_base = self.commit('new base', self.base)
        self.new_tip = self.commit('merged candidate', self.old_tip, self.new_base)
        self.git('update-ref', 'refs/heads/main', self.new_base)
        self.git('update-ref', 'refs/heads/pr/feature', self.old_tip)
        self.previous = {'base': self.base, 'commits': [self.first, self.old_tip]}
        self.selected = {'base': self.new_base, 'commits': [self.new_tip],
                         'adopted_history': {'base': self.new_base, 'tip': self.new_tip}}

    def git(self, *args, input_text=None):
        p = subprocess.run(['git', '-C', str(self.repo), *args], env=self.env,
                           input=input_text, text=True, capture_output=True, check=True)
        return p.stdout.strip()

    def commit(self, message, *parents):
        args = ['commit-tree', self.tree]
        for parent in parents:
            args += ['-p', parent]
        return self.git(*args, '-m', message)

    def refs(self):
        return self.git('for-each-ref', '--format=%(refname) %(objectname)')

    def reject(self, previous=None, selected=None, new_tip=None, reason=''):
        before = self.refs()
        with self.assertRaisesRegex(SyncwheelError, reason):
            verify_compose_base_advance(self.repo, previous or self.previous,
                selected or self.selected, self.old_tip, new_tip or self.new_tip, self.new_base)
        self.assertEqual(self.refs(), before)

    def test_incomplete_and_reordered_recipe(self):
        for commits in ([self.old_tip], [self.old_tip, self.first]):
            with self.subTest(commits=commits):
                self.reject(previous={'base': self.base, 'commits': commits}, reason='recipe')

    def test_wrong_recipe_parent(self):
        wrong = self.commit('wrong parent', self.base)
        self.reject(previous={'base': self.base, 'commits': [wrong, self.old_tip]},
                    reason='linear published recipe')

    def test_old_tip_is_not_retained(self):
        other = self.commit('other branch', self.base)
        candidate = self.commit('other merge', other, self.new_base)
        selected = {'base': self.new_base, 'commits': [candidate],
                    'adopted_history': {'base': self.new_base, 'tip': candidate}}
        self.reject(selected=selected, new_tip=candidate, reason='preserved source and base ancestry')

    def test_merge_parent_order_is_wrong(self):
        candidate = self.commit('reversed merge', self.new_base, self.old_tip)
        selected = {'base': self.new_base, 'commits': [candidate],
                    'adopted_history': {'base': self.new_base, 'tip': candidate}}
        self.reject(selected=selected, new_tip=candidate, reason='two-parent merge')

    def test_grafted_history_is_refused(self):
        graft = self.repo / '.git' / 'info' / 'grafts'
        graft.write_text(self.old_tip + ' ' + self.base + '\n')
        self.reject(reason='grafted history')

    def test_consecutive_ancestral_base_merges_are_proven(self):
        middle = self.commit('middle base', self.base)
        final_base = self.commit('final base', middle)
        prior = self.commit('previous base merge', self.old_tip, middle)
        candidate = self.commit('current base merge', prior, final_base)
        selected = {'base': final_base, 'commits': [candidate],
                    'adopted_history': {'base': final_base, 'tip': candidate}}
        before = self.refs()
        proof = verify_compose_base_advance(self.repo, self.previous, selected,
                                           self.old_tip, candidate, final_base)
        self.assertEqual(proof['priorAncestralMerges'], [{'commit': prior, 'secondParent': middle}])
        self.assertEqual(self.refs(), before)

    def test_unrelated_previous_base_merge_is_rejected(self):
        foreign = self.commit('foreign', self.base)
        prior = self.commit('foreign merge', self.old_tip, foreign)
        candidate = self.commit('current merge', prior, self.new_base)
        selected = {'base': self.new_base, 'commits': [candidate],
                    'adopted_history': {'base': self.new_base, 'tip': candidate}}
        self.reject(selected=selected, new_tip=candidate, reason='unrelated or reordered prior base merge')

    def test_reordered_previous_base_merges_are_rejected(self):
        middle = self.commit('middle base', self.base)
        final_base = self.commit('final base', middle)
        later_first = self.commit('later first', self.old_tip, final_base)
        earlier_second = self.commit('earlier second', later_first, middle)
        candidate = self.commit('current merge', earlier_second, final_base)
        selected = {'base': final_base, 'commits': [candidate],
                    'adopted_history': {'base': final_base, 'tip': candidate}}
        before = self.refs()
        with self.assertRaisesRegex(SyncwheelError, 'unrelated or reordered prior base merge'):
            verify_compose_base_advance(self.repo, self.previous, selected,
                                       self.old_tip, candidate, final_base)
        self.assertEqual(self.refs(), before)

    def test_octopus_previous_merge_is_rejected(self):
        prior = self.commit('octopus', self.old_tip, self.base, self.new_base)
        candidate = self.commit('current merge', prior, self.new_base)
        selected = {'base': self.new_base, 'commits': [candidate],
                    'adopted_history': {'base': self.new_base, 'tip': candidate}}
        self.reject(selected=selected, new_tip=candidate, reason='non-linear feature-side history')


if __name__ == '__main__':
    unittest.main()
