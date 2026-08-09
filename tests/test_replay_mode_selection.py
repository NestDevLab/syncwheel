"""Coverage for replay-mode selection: precedence, auto, and visibility.

The auto tier is git-version dependent, so the plumbing case is skipped where
merge-tree --write-tree is unavailable and the fallback case stubs a git 2.37
on PATH rather than assuming the local one.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from _replay_support import (
    build_linear_chain,
    clone_repo,
    git,
    hermetic_environment,
    load_syncwheel_module,
    run_cli,
)


def local_git_supports_write_tree():
    version = subprocess.run(
        ['git', '--version'], text=True, capture_output=True
    ).stdout.split()
    if len(version) < 3:
        return False
    try:
        major, minor = (int(part) for part in version[2].split('.')[:2])
    except ValueError:
        return False
    return (major, minor) >= (2, 38)


class ReplayModeSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='syncwheel-replay-mode-selection-'))
        self.environment_patch = mock.patch.dict(
            os.environ,
            hermetic_environment(self.tmp),
            clear=True,
        )
        self.environment_patch.start()

    def tearDown(self):
        self.environment_patch.stop()
        shutil.rmtree(self.tmp)

    def clone(self, name):
        source_repo, manifest, stack_id = build_linear_chain(self.tmp)
        return clone_repo(source_repo, self.tmp / name, manifest), manifest, stack_id

    def set_manifest_replay_mode(self, clone, mode):
        path = clone / '.syncwheel' / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['defaults']['replay_mode'] = mode
        path.write_text(json.dumps(manifest, indent=2) + '\n')

    def rebuilt_mode(self, clone):
        events = [
            item for item in load_syncwheel_module().load_ledger_events(clone)
            if item['type'] == 'stack_rebuilt'
        ]
        return events[-1]['payload']['replay_mode']

    def worktree_paths(self, clone):
        return [
            line for line in git(clone, 'worktree', 'list', '--porcelain').stdout.splitlines()
            if line.startswith('worktree ')
        ]

    def test_cli_flag_wins_over_the_repo_profile_and_the_manifest_default(self):
        clone, _manifest, stack_id = self.clone('cli-flag')
        self.set_manifest_replay_mode(clone, 'desk')
        run_cli(clone, 'replay-mode', 'desk')

        run_cli(clone, 'stack', 'rebuild', stack_id, '--replay-mode', 'ephemeral')

        self.assertEqual(self.rebuilt_mode(clone), 'ephemeral')

    def test_repo_profile_wins_over_the_manifest_default(self):
        clone, _manifest, stack_id = self.clone('repo-profile')
        self.set_manifest_replay_mode(clone, 'desk')
        before = self.worktree_paths(clone)
        run_cli(clone, 'replay-mode', 'ephemeral')

        run_cli(clone, 'stack', 'rebuild', stack_id)

        self.assertEqual(self.rebuilt_mode(clone), 'ephemeral')
        self.assertEqual(self.worktree_paths(clone), before)

    def test_manifest_default_wins_over_builtin_auto(self):
        clone, _manifest, stack_id = self.clone('manifest-default')
        self.set_manifest_replay_mode(clone, 'desk')
        before = self.worktree_paths(clone)

        run_cli(clone, 'stack', 'rebuild', stack_id)

        self.assertEqual(self.rebuilt_mode(clone), 'desk')
        self.assertGreater(len(self.worktree_paths(clone)), len(before))

    @unittest.skipUnless(
        local_git_supports_write_tree(), 'git merge-tree --write-tree is unavailable'
    )
    def test_builtin_auto_selects_plumbing_when_git_supports_write_tree(self):
        clone, _manifest, stack_id = self.clone('auto-plumbing')
        before = self.worktree_paths(clone)

        run_cli(clone, 'stack', 'rebuild', stack_id)

        self.assertEqual(self.rebuilt_mode(clone), 'plumbing')
        self.assertEqual(self.worktree_paths(clone), before)

    def test_builtin_auto_falls_back_to_ephemeral_on_git_2_37(self):
        clone, _manifest, stack_id = self.clone('auto-git-2-37')
        before = self.worktree_paths(clone)
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
            run_cli(clone, 'stack', 'rebuild', stack_id)

        self.assertEqual(self.rebuilt_mode(clone), 'ephemeral')
        self.assertEqual(self.worktree_paths(clone), before)

    def test_auto_falls_back_instead_of_failing_when_plumbing_does_not_apply(self):
        clone, _manifest, _stack_id = self.clone('auto-fallback')
        git(clone, 'branch', 'merged-integration', 'integration')
        module = load_syncwheel_module()

        # merge-stacks integration has no plumbing form, so auto descends.
        self.assertEqual(
            module.auto_replay_mode(
                clone, 'merged-integration', (None, False), plumbing_supported=False
            ),
            'ephemeral',
        )

    def test_plan_json_names_the_replay_mode_of_each_rebuild_action(self):
        clone, manifest, _stack_id = self.clone('plan-json')
        stack = manifest['stacks'][0]
        git(clone, 'branch', stack['branch'], stack['base'])

        plan = json.loads(run_cli(clone, 'plan', '--json').stdout)

        rebuild = next(item for item in plan if item['type'] == 'rebuild_pr_branch')
        refresh = next(item for item in plan if item['type'] == 'refresh_integration_for_stack')
        self.assertEqual(
            rebuild['replay_mode'],
            'plumbing' if local_git_supports_write_tree() else 'ephemeral',
        )
        # The clone's primary checkout stands on integration, so its rebuild is free.
        self.assertEqual(refresh['replay_mode'], 'in-place')

    def test_replay_mode_command_reads_and_clears_the_repo_local_default(self):
        clone, _manifest, _stack_id = self.clone('replay-mode-command')
        self.set_manifest_replay_mode(clone, 'desk')

        self.assertEqual(
            json.loads(run_cli(clone, 'replay-mode', '--json').stdout)['source'], 'manifest'
        )
        set_report = json.loads(run_cli(clone, 'replay-mode', 'plumbing', '--json').stdout)
        cleared = json.loads(run_cli(clone, 'replay-mode', '--clear', '--json').stdout)

        self.assertEqual((set_report['replay_mode'], set_report['source']), ('plumbing', 'profile'))
        self.assertEqual((cleared['replay_mode'], cleared['source']), ('desk', 'manifest'))
        self.assertIsNone(cleared['profile'])

    def test_repo_local_default_survives_alongside_an_existing_profile_key(self):
        clone, _manifest, _stack_id = self.clone('profile-merge')
        run_cli(clone, 'replay-mode', 'ephemeral')

        run_cli(clone, 'use', 'alice')

        profile = json.loads((clone / '.syncwheel' / 'profile.local.json').read_text())
        self.assertEqual(profile, {'personal': 'alice', 'replay_mode': 'ephemeral'})

    def test_manifest_replay_mode_survives_a_coordination_handoff(self):
        module = load_syncwheel_module()
        manifest = {
            'version': 2,
            'syncwheel_tracking': 'git-tracked',
            'defaults': {
                'canonical_remote': 'origin',
                'publication_remote': 'origin',
                'base_branch': 'main',
                'base_ref': 'origin/main',
                'replay_mode': 'plumbing',
            },
            'integration': {
                'branch': 'integration/shared',
                'base': 'origin/main',
                'strategy': 'cherry-pick',
                'stacks': [],
            },
            'coordination': {
                'mode': 'active-active',
                'id': 'shared',
                'remote': 'origin',
                'state_branch': 'syncwheel/state/shared',
                'gc': {'worktree_grace_days': 7, 'backup_retention_days': 30, 'backup_keep': 2},
            },
            'stacks': [],
        }

        snapshot = module.coordination_manifest_snapshot(manifest)
        restored = module.apply_coordination_snapshot(manifest, snapshot)

        # replay_mode changes how a ref is produced, not what it contains, so it
        # is local policy rather than shared topology.
        self.assertNotIn('replay_mode', snapshot['defaults'])
        self.assertEqual(restored['defaults']['replay_mode'], 'plumbing')


if __name__ == '__main__':
    unittest.main()
