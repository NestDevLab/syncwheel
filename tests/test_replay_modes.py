"""Regression harness for deterministic replay before execution modes diverge.

It asserts stability across two independent executions of the one current mode,
never equality with the original source commits. In particular, replaying onto a
moved base must produce different parent-dependent SHAs while remaining stable
between executions. Do not replace this comparison with source-SHA equality.
"""

import contextlib
import io
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
    create_repo,
    git,
    hermetic_environment,
    load_syncwheel_module,
    run_cli,
    write_manifest,
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

    def manifest_with_empty_stack(self, manifest):
        return {
            **manifest,
            'stacks': [{**stack, 'commits': []} for stack in manifest['stacks']],
        }

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

    def test_plumbing_replay_is_byte_identical_to_ephemeral_for_the_fixed_corpus(self):
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
                ephemeral = self.replay_once(
                    source_repo,
                    manifest,
                    stack,
                    f'{name}-ephemeral',
                    replay_mode='ephemeral',
                )
                plumbing = self.replay_once(
                    source_repo,
                    manifest,
                    stack,
                    f'{name}-plumbing',
                    replay_mode='plumbing',
                )
                self.assertEqual(plumbing, ephemeral)

    def test_plumbing_rebuild_passes_the_installed_ref_guard_without_an_external_handshake(self):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        clone = clone_repo(source_repo, self.tmp / 'guarded-plumbing', manifest)
        stack = next(item for item in manifest['stacks'] if item['id'] == stack_id)
        run_cli(clone, 'hooks', 'install', '--apply')
        run_cli(clone, 'stack', 'rebuild', stack_id, '--replay-mode', 'plumbing')
        declared = git(clone, 'rev-parse', stack['branch']).stdout.strip()
        tree = git(clone, 'rev-parse', f"{stack['branch']}^{{tree}}").stdout.strip()
        extra = git(clone, 'commit-tree', tree, '-p', stack['branch'], '-m', 'undeclared').stdout.strip()
        git(clone, 'update-ref', f"refs/heads/{stack['branch']}", extra)
        self.assertNotIn('SYNCWHEEL_REF_MOVE_AUTH', os.environ)

        run_cli(clone, 'stack', 'rebuild', stack_id, '--replay-mode', 'plumbing')

        self.assertEqual(git(clone, 'rev-parse', stack['branch']).stdout.strip(), declared)

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

    def test_plumbing_conflict_leaves_the_primary_checkout_unchanged_and_requires_desk(self):
        source_repo, base = create_repo(self.tmp, 'plumbing-conflict')
        git(source_repo, 'switch', '-q', '-c', 'topic', base)
        (source_repo / 'shared.txt').write_text('topic\n')
        git(source_repo, 'add', 'shared.txt')
        git(source_repo, 'commit', '-q', '-m', 'feat: conflicting topic')
        topic = git(source_repo, 'rev-parse', 'HEAD').stdout.strip()
        git(source_repo, 'switch', '-q', '-c', 'integration', base)
        (source_repo / 'shared.txt').write_text('integration\n')
        git(source_repo, 'add', 'shared.txt')
        git(source_repo, 'commit', '-q', '-m', 'feat: conflicting integration')
        manifest = write_manifest(
            source_repo,
            git(source_repo, 'rev-parse', 'HEAD').stdout.strip(),
            [topic],
        )
        clone = clone_repo(source_repo, self.tmp / 'plumbing-conflict-clone', manifest)
        before_status = git(clone, 'status', '--porcelain=v1', '--branch').stdout
        before_worktrees = self.worktree_entries(clone)
        module = load_syncwheel_module()
        plan = module.replay_plan(
            clone,
            manifest,
            module.replay_target(stack=manifest['stacks'][0]),
            'plumbing',
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = module.execute_replay(clone, plan, True)

        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['mode'], 'plumbing')
        self.assertEqual(result['conflict']['paths'], ['shared.txt'])
        self.assertEqual(git(clone, 'status', '--porcelain=v1', '--branch').stdout, before_status)
        self.assertEqual(self.worktree_entries(clone), before_worktrees)
        self.assertEqual(git(clone, 'show-ref', '--verify', '--quiet', 'refs/heads/pr/replay', expected=1).returncode, 1)

        failure = run_cli(clone, 'stack', 'rebuild', 'replay', '--replay-mode', 'plumbing', expected=2)

        self.assertIn('replay mode: plumbing', failure.stderr)
        self.assertIn('shared.txt', failure.stderr)
        self.assertIn('syncwheel stack rebuild replay --replay-mode desk', failure.stderr)
        self.assertEqual(git(clone, 'status', '--porcelain=v1', '--branch').stdout, before_status)
        self.assertEqual(self.worktree_entries(clone), before_worktrees)

    def test_empty_stack_desk_rebuild_refuses_without_mutation(self):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        empty_manifest = self.manifest_with_empty_stack(manifest)
        clone = clone_repo(source_repo, self.tmp / 'empty-desk', empty_manifest)
        desk = self.tmp / 'empty-desk-worktree'
        before_refs = git(clone, 'show-ref', '--head').stdout
        before_status = git(clone, 'status', '--porcelain=v1', '--branch').stdout
        before_worktrees = self.worktree_entries(clone)
        before_events = load_syncwheel_module().load_ledger_events(clone)

        failure = run_cli(
            clone,
            'stack',
            'rebuild',
            stack_id,
            '--replay-mode',
            'desk',
            '--worktree',
            str(desk),
            expected=2,
        )

        self.assertIn("stack 'replay' has no declared commits", failure.stderr)
        self.assertIn(
            'Author on the integration branch, then capture the integration commit(s) into the stack.',
            failure.stderr,
        )
        self.assertEqual(git(clone, 'show-ref', '--head').stdout, before_refs)
        self.assertEqual(git(clone, 'status', '--porcelain=v1', '--branch').stdout, before_status)
        self.assertEqual(self.worktree_entries(clone), before_worktrees)
        self.assertEqual(load_syncwheel_module().load_ledger_events(clone), before_events)
        self.assertFalse(desk.exists())
        self.assertEqual(
            git(clone, 'show-ref', '--verify', '--quiet', 'refs/heads/pr/replay', expected=1).returncode,
            1,
        )

    def test_empty_stack_auto_and_plumbing_rebuilds_remain_allowed(self):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        empty_manifest = self.manifest_with_empty_stack(manifest)
        base = empty_manifest['stacks'][0]['base']

        for mode in ('auto', 'plumbing'):
            with self.subTest(mode=mode):
                clone = clone_repo(source_repo, self.tmp / f'empty-{mode}', empty_manifest)
                before_worktrees = self.worktree_entries(clone)
                command = ['stack', 'rebuild', stack_id]
                if mode != 'auto':
                    command.extend(['--replay-mode', mode])

                run_cli(clone, *command)

                self.assertEqual(git(clone, 'rev-parse', 'pr/replay').stdout.strip(), base)
                self.assertEqual(self.worktree_entries(clone), before_worktrees)

    def test_reconcile_empty_stack_desk_rebuild_refuses_before_mutation(self):
        source_repo, manifest, _stack_id = build_linear_chain(self.tmp)
        empty_manifest = self.manifest_with_empty_stack(manifest)
        clone = clone_repo(source_repo, self.tmp / 'empty-desk-reconcile', empty_manifest)
        manifest_path = clone / '.syncwheel' / 'manifest.json'
        before_manifest = manifest_path.read_text()
        before_refs = git(clone, 'show-ref', '--head').stdout
        before_status = git(clone, 'status', '--porcelain=v1', '--branch').stdout
        before_worktrees = self.worktree_entries(clone)

        failure = run_cli(
            clone,
            'reconcile',
            '--no-fetch',
            '--apply',
            '--skip-integration',
            '--replay-mode',
            'desk',
            expected=2,
        )

        self.assertIn("stack 'replay' has no declared commits", failure.stderr)
        self.assertEqual(manifest_path.read_text(), before_manifest)
        self.assertEqual(git(clone, 'show-ref', '--head').stdout, before_refs)
        self.assertEqual(git(clone, 'status', '--porcelain=v1', '--branch').stdout, before_status)
        self.assertEqual(self.worktree_entries(clone), before_worktrees)

    def test_nonempty_materialized_stack_can_still_use_desk_for_validation(self):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        clone = clone_repo(source_repo, self.tmp / 'nonempty-desk', manifest)
        desk = self.tmp / 'nonempty-desk-worktree'

        run_cli(clone, 'stack', 'rebuild', stack_id)
        run_cli(
            clone,
            'stack',
            'rebuild',
            stack_id,
            '--replay-mode',
            'desk',
            '--worktree',
            str(desk),
        )

        event = [
            item for item in load_syncwheel_module().load_ledger_events(clone)
            if item['type'] == 'stack_rebuilt'
        ][-1]
        self.assertEqual(event['payload']['replay_mode'], 'desk')
        self.assertTrue(desk.is_dir())
        self.assertTrue(commit_log(clone, manifest['stacks'][0]['base'], 'pr/replay'))

    def test_plumbing_empty_commit_stops_like_ephemeral_replay(self):
        source_repo, manifest, stack_id, _base, _empty = build_empty_commit(self.tmp)
        ephemeral = clone_repo(source_repo, self.tmp / 'empty-ephemeral', manifest)
        plumbing = clone_repo(source_repo, self.tmp / 'empty-plumbing', manifest)

        expected = run_cli(ephemeral, 'stack', 'rebuild', stack_id, '--replay-mode', 'ephemeral', expected=2)
        actual = run_cli(plumbing, 'stack', 'rebuild', stack_id, '--replay-mode', 'plumbing', expected=2)

        self.assertEqual(actual.stderr, expected.stderr)
        self.assertIn('The previous cherry-pick is now empty', actual.stderr)

    def test_plumbing_falls_back_to_ephemeral_for_git_2_37(self):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        clone = clone_repo(source_repo, self.tmp / 'plumbing-git-2-37', manifest)
        fake_bin = self.tmp / 'fake-git-bin'
        fake_bin.mkdir()
        actual_git = shutil.which('git')
        self.assertIsNotNone(actual_git)
        fake_git = fake_bin / 'git'
        fake_git.write_text(
            '#!/bin/sh\n'
            'if test "$1" = "--version"; then\n'
            "  printf '%s\\n' 'git version 2.37.9'\n"
            '  exit 0\n'
            'fi\n'
            f'exec {actual_git} "$@"\n'
        )
        fake_git.chmod(0o755)

        with mock.patch.dict(os.environ, {'PATH': f'{fake_bin}{os.pathsep}{os.environ["PATH"]}'}):
            result = run_cli(clone, 'stack', 'rebuild', stack_id, '--replay-mode', 'plumbing')

        self.assertIn('git worktree add --detach', result.stdout)
        self.assertNotIn('git merge-tree --write-tree', result.stdout)
        self.assertTrue(commit_log(clone, manifest['stacks'][0]['base'], 'pr/replay'))

    def test_plumbing_refuses_a_checked_out_target_branch(self):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        clone = clone_repo(source_repo, self.tmp / 'plumbing-checked-out-target', manifest)
        git(clone, 'branch', 'pr/replay', manifest['stacks'][0]['base'])
        git(clone, 'switch', '-q', 'pr/replay')
        git(clone, 'add', '.syncwheel/manifest.json')
        git(clone, 'commit', '-q', '-m', 'test: add replay manifest')
        before = git(clone, 'status', '--porcelain=v1', '--branch').stdout

        failure = run_cli(clone, 'stack', 'rebuild', stack_id, '--replay-mode', 'plumbing', expected=2)

        self.assertIn("requires target branch 'pr/replay' to be unchecked out", failure.stderr)
        self.assertEqual(git(clone, 'status', '--porcelain=v1', '--branch').stdout, before)

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
