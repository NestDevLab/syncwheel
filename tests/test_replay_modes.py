"""Regression harness for deterministic replay before execution modes diverge.

It asserts stability across two independent executions of the one current mode,
never equality with the original source commits. In particular, replaying onto a
moved base must produce different parent-dependent SHAs while remaining stable
between executions. Do not replace this comparison with source-SHA equality.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from _replay_support import (
    EMPTY_COMMIT_POLICY,
    build_binary_file,
    build_empty_commit,
    build_file_mode_change,
    build_linear_chain,
    build_merge_commit,
    build_moved_base,
    build_rename,
    clone_at_base,
    clone_repo,
    commit_log,
    git,
    hermetic_environment,
    load_syncwheel_module,
    run_cli,
)


class ReplayModesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='syncwheel-replay-modes-'))
        self.environment_patch = mock.patch.dict(
            os.environ,
            hermetic_environment(self.tmp),
            clear=True,
        )
        self.environment_patch.start()

    def tearDown(self):
        self.environment_patch.stop()
        shutil.rmtree(self.tmp)

    def replay_once(self, source_repo, manifest, stack, attempt, replay_mode='auto'):
        clone = clone_repo(source_repo, self.tmp / f'{source_repo.name}-{attempt}', manifest)
        worktree = clone.parent / f'{clone.name}-replay-worktree'
        command = ['stack', 'rebuild', stack['id']]
        if replay_mode != 'auto':
            command.extend(['--replay-mode', replay_mode])
        if replay_mode in ('auto', 'desk'):
            command.extend(['--worktree', str(worktree)])
        run_cli(clone, *command)
        return commit_log(clone, stack['base'], stack['branch'])

    def worktree_entries(self, repo_path):
        return [
            line for line in git(repo_path, 'worktree', 'list', '--porcelain').stdout.splitlines()
            if line.startswith('worktree ')
        ]

    def assert_replay_is_stable(self, builder):
        source_repo, manifest, stack_id = builder(self.tmp)
        stack = next(item for item in manifest['stacks'] if item['id'] == stack_id)

        first = self.replay_once(source_repo, manifest, stack, 'first')
        # Git timestamps have one-second resolution. This makes a missing pinned
        # committer date deterministically observable instead of intermittently
        # passing when the two independent replays land in the same second.
        time.sleep(1.1)
        second = self.replay_once(source_repo, manifest, stack, 'second')

        self.assertEqual(second, first)
        self.assertTrue(first, 'the replayed branch must contain commit identities')
        return source_repo, stack, first

    def test_current_replay_mode_is_stable_for_the_fixed_corpus(self):
        scenarios = {
            'linear chain': build_linear_chain,
            'moved base': build_moved_base,
            'binary file': build_binary_file,
            'rename': build_rename,
            'file mode change': build_file_mode_change,
        }
        for name, builder in scenarios.items():
            with self.subTest(scenario=name):
                self.assert_replay_is_stable(builder)

    def test_moved_base_is_not_required_to_match_source_commit_shas(self):
        repo_path, _stack, replayed = self.assert_replay_is_stable(build_moved_base)
        source = commit_log(repo_path, 'main', 'source')

        self.assertNotEqual(replayed, source)

    def test_ephemeral_replay_is_byte_identical_to_desk_for_the_fixed_corpus(self):
        scenarios = {
            'linear chain': build_linear_chain,
            'moved base': build_moved_base,
            'binary file': build_binary_file,
            'rename': build_rename,
            'file mode change': build_file_mode_change,
        }
        for name, builder in scenarios.items():
            with self.subTest(scenario=name):
                source_repo, manifest, stack_id = builder(self.tmp)
                stack = next(item for item in manifest['stacks'] if item['id'] == stack_id)
                desk = self.replay_once(source_repo, manifest, stack, f'{name}-desk')
                ephemeral = self.replay_once(
                    source_repo,
                    manifest,
                    stack,
                    f'{name}-ephemeral',
                    replay_mode='ephemeral',
                )
                self.assertEqual(ephemeral, desk)

    def test_ephemeral_stack_rebuild_leaves_no_worktree(self):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        clone = clone_repo(source_repo, self.tmp / 'ephemeral-stack', manifest)
        before = self.worktree_entries(clone)

        run_cli(clone, 'stack', 'rebuild', stack_id, '--replay-mode', 'ephemeral')

        self.assertEqual(self.worktree_entries(clone), before)
        event = [
            item for item in load_syncwheel_module().load_ledger_events(clone)
            if item['type'] == 'stack_rebuilt'
        ][-1]
        self.assertEqual(event['payload']['after_tip'], git(clone, 'rev-parse', 'pr/replay').stdout.strip())

    def test_ephemeral_integration_rebuild_leaves_no_worktree(self):
        source_repo, manifest, _stack_id = build_linear_chain(self.tmp)
        clone = clone_repo(source_repo, self.tmp / 'ephemeral-integration', manifest)
        git(clone, 'add', '.syncwheel/manifest.json')
        git(clone, 'commit', '-q', '-m', 'test: add replay manifest')
        before = self.worktree_entries(clone)

        run_cli(clone, 'int', 'rebuild', '--replay-mode', 'ephemeral')

        self.assertEqual(self.worktree_entries(clone), before)
        event = [
            item for item in load_syncwheel_module().load_ledger_events(clone)
            if item['type'] == 'integration_rebuilt'
        ][-1]
        self.assertEqual(event['payload']['after_tip'], git(clone, 'rev-parse', 'integration').stdout.strip())

    def test_ephemeral_replay_removes_its_worktree_after_a_mid_replay_failure(self):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        stack = manifest['stacks'][0]
        git(source_repo, 'switch', '-q', '-c', 'failure-topic', stack['base'])
        (source_repo / 'topic.txt').write_text('topic\n')
        git(source_repo, 'add', 'topic.txt')
        git(source_repo, 'commit', '-q', '-m', 'test: topic for merge failure')
        git(source_repo, 'switch', '-q', '-c', 'failure-merge', stack['base'])
        (source_repo / 'integration.txt').write_text('integration\n')
        git(source_repo, 'add', 'integration.txt')
        git(source_repo, 'commit', '-q', '-m', 'test: integration for merge failure')
        git(source_repo, 'merge', '--no-ff', 'failure-topic', '-m', 'test: merge replay failure')
        merge = git(source_repo, 'rev-parse', 'HEAD').stdout.strip()
        failing_manifest = {
            **manifest,
            'stacks': [{
                **manifest['stacks'][0],
                'commits': [*manifest['stacks'][0]['commits'], merge],
            }],
        }
        clone = clone_repo(source_repo, self.tmp / 'ephemeral-failure', failing_manifest)
        before = self.worktree_entries(clone)

        run_cli(
            clone,
            'stack',
            'rebuild',
            stack_id,
            '--replay-mode',
            'ephemeral',
            expected=2,
        )

        self.assertEqual(self.worktree_entries(clone), before)

    def test_plumbing_mode_reports_that_it_is_not_available(self):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        clone = clone_repo(source_repo, self.tmp / 'plumbing-unavailable', manifest)

        failure = run_cli(clone, 'stack', 'rebuild', stack_id, '--replay-mode', 'plumbing', expected=2)

        self.assertIn('replay mode plumbing is not available yet', failure.stderr)

    def test_merge_commit_rejection_and_validation_are_stable(self):
        repo_path, manifest, _stack_id, base, merge = build_merge_commit(self.tmp)
        module = load_syncwheel_module()
        validation = module.validate_manifest(repo_path, manifest)

        # Merge commits are recorded separately, never assigned to a stack by the
        # integration classifier, so the stack remains empty before replay fails.
        self.assertEqual(validation['details']['integration']['merge_commits'], [merge])
        self.assertNotIn(merge, validation['details']['integration']['unmapped_commits'])
        self.assertNotIn(merge, validation['details']['integration']['declared_commits'])

        failures = []
        for attempt in ('first', 'second'):
            clone = clone_at_base(repo_path, self.tmp / f'merge-{attempt}', base)
            failure = git(clone, 'cherry-pick', merge, expected=128)
            failures.append(failure.stderr)

        self.assertEqual(failures[1], failures[0])
        self.assertIn('is a merge but no -m option was given', failures[0])

    def test_empty_commit_policy_is_stop_with_a_stable_failure(self):
        repo_path, _manifest, _stack_id, base, empty = build_empty_commit(self.tmp)
        self.assertEqual(EMPTY_COMMIT_POLICY, 'stop')

        failures = []
        for attempt in ('first', 'second'):
            clone = clone_at_base(repo_path, self.tmp / f'empty-{attempt}', base)
            failure = git(clone, 'cherry-pick', empty, expected=1)
            failures.append(failure.stdout + failure.stderr)

        self.assertEqual(failures[1], failures[0])
        self.assertIn('The previous cherry-pick is now empty', failures[0])


if __name__ == '__main__':
    unittest.main()
