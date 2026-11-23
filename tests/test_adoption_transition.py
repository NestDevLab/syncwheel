"""Adopted-history to exact linear recipe safety tests."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from syncwheel_adoption_transition import verify_adoption_transition
from syncwheel_errors import SyncwheelError


class AdoptionTransitionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='syncwheel-adoption-transition-')
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.commit('base')
        self.base = self.git('rev-parse', 'HEAD')
        self.commit('first')
        self.first = self.git('rev-parse', 'HEAD')
        self.commit('second')
        self.second = self.git('rev-parse', 'HEAD')
        self.previous = {'id': 'feature', 'base': self.base,
                         'adopted_history': {'base': self.base, 'tip': self.first}}
        self.selected = {'id': 'feature', 'base': self.base,
                         'commits': [self.first, self.second]}

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def commit(self, text):
        (self.repo / 'file').write_text(text + '\n')
        self.git('add', 'file')
        self.git('commit', '-qm', text)

    def test_accepts_only_exact_linear_continuation(self):
        proof = verify_adoption_transition(self.repo, self.previous, self.selected,
                                           self.second, self.first)
        self.assertEqual(proof, {'base': self.base, 'oldTip': self.first, 'newTip': self.second})

    def test_rejects_changed_witness_missing_prefix_and_wrong_base(self):
        with self.assertRaisesRegex(SyncwheelError, 'published source differs'):
            verify_adoption_transition(self.repo, self.previous, self.selected, self.second, self.base)
        for commits in ([self.second], [self.second, self.first], [self.first, self.first, self.second]):
            candidate = {**self.selected, 'commits': commits}
            with self.subTest(commits=commits), self.assertRaises(SyncwheelError):
                verify_adoption_transition(self.repo, self.previous, candidate, self.second, self.first)
        with self.assertRaisesRegex(SyncwheelError, 'preserve the exact adopted base'):
            verify_adoption_transition(self.repo, self.previous, {**self.selected, 'base': self.first},
                                       self.second, self.first)

    def test_rejects_non_linear_selected_source(self):
        self.git('switch', '-q', '-c', 'side', self.first)
        (self.repo / 'other').write_text('side\n')
        self.git('add', 'other')
        self.git('commit', '-qm', 'side')
        self.git('switch', '-q', 'main')
        self.git('merge', '-q', '--no-ff', '-m', 'merge side', 'side')
        merge = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(SyncwheelError, 'linear source sequence'):
            verify_adoption_transition(self.repo, self.previous,
                                       {**self.selected, 'commits': [self.first, self.second, merge]},
                                       merge, self.first)


class AdoptionTransitionIntegrationTest(unittest.TestCase):
    def test_compose_transitions_adoption_and_preserves_pending_foreign_lane(self):
        self.exercise_transition(race=False)

    def test_compose_transition_ref_lease_rejects_concurrent_source_move(self):
        self.exercise_transition(race=True)

    def exercise_transition(self, *, race):
        import json
        import os
        import shutil
        from unittest import mock
        from tests.test_coordination import ActiveActiveCoordinationTest
        from tests.test_syncwheel import SyncwheelFixtureTest
        fixture = ActiveActiveCoordinationTest('runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        data = fixture.prepare_additive_compose('adoption-transition-integration', plan=False)
        repo, module = data['repo'], data['module']
        (repo / '.gitignore').unlink(missing_ok=True)
        fixture.git(repo, 'switch', '-q', 'integration/shared')
        (repo / '.gitignore').write_text(fixture.git(repo, 'show', 'HEAD:.gitignore').stdout)
        fixture.run_cli(repo, 'stack', 'create', 'foreign', '--branch', 'pr/foreign')
        foreign_data = json.loads(fixture.run_cli(
            repo, 'worktree', 'open', 'foreign-writer', '--into', 'foreign', '--json',
            extra_env={'SYNCWHEEL_LANE_OWNER': 'foreign-owner'}).stdout)['lane']
        foreign = Path(foreign_data['path'])
        (foreign / 'foreign.txt').write_text('foreign\n')
        fixture.git(foreign, 'add', 'foreign.txt')
        fixture.git(foreign, 'commit', '-qm', 'foreign')
        first = data['new_tip']
        base = fixture.git(repo, 'rev-parse', first + '^').stdout.strip()
        adoption_args = ['stack', 'set', 'new-stack', '--base', base, '--adopt-tip', first]
        adoption = json.loads(fixture.run_cli(repo, *adoption_args).stdout)
        fixture.run_cli(repo, *adoption_args, '--plan-digest', adoption['planDigest'], '--apply')
        source_data = json.loads(fixture.run_cli(
            repo, 'worktree', 'open', 'source-writer', '--into', 'new-stack', '--base', first,
            '--json', extra_env={'SYNCWHEEL_LANE_OWNER': 'source-owner'}).stdout)['lane']
        source = Path(source_data['path'])
        (source / 'follow-up.txt').write_text('follow up\n')
        fixture.git(source, 'add', 'follow-up.txt')
        fixture.git(source, 'commit', '-qm', 'follow-up')
        second = fixture.git(source, 'rev-parse', 'HEAD').stdout.strip()
        (source / 'pending.txt').write_text('pending\n')
        stable = fixture.tmp / 'stable-adoption-cli'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'scripts', stable,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        wrapper = stable / 'syncwheel'
        wrapper.write_text('#!/bin/sh\nexec /usr/bin/python3 "$(dirname "$0")/syncwheel.py" "$@"\n')
        wrapper.chmod(0o755)
        (fixture.bin_dir / 'syncwheel').unlink()
        (fixture.bin_dir / 'syncwheel').symlink_to(wrapper)
        fixture.run_cli(repo, 'hooks', 'install', '--apply')
        self.assertTrue(json.loads(fixture.run_cli(repo, 'hooks', 'status').stdout)['ready'])
        # Create the source checkout before the unrelated interrupted cleanup;
        # the later existing-worktree operation must not reconcile that cleanup.
        checkout = fixture.tmp / 'source-checkout'
        fixture.run_cli(repo, 'stack', 'git', 'new-stack', '--worktree', str(checkout), '--', 'status')
        shim = SyncwheelFixtureTest('runTest')
        shim.repo, shim.registry = repo, fixture.registry
        with mock.patch.dict(os.environ, {'SYNCWHEEL_LANE_OWNER': 'foreign-owner'}):
            shim.run_cli_until_cleanup_sigkill('after_cleanup_intent', 'worktree', 'release',
                                               'foreign-writer', '--reason', 'fixture', '--apply', '--json')
        registry = module.governed_worktree_registry_path(repo)
        foreign_record = registry.read_bytes()
        foreign_status = fixture.git(foreign, 'status', '--porcelain').stdout
        source_status = fixture.git(source, 'status', '--porcelain').stdout
        foreign_file = (foreign / 'foreign.txt').read_bytes()
        def unchanged():
            return (registry.read_bytes() == foreign_record and foreign.exists()
                    and fixture.git(foreign, 'status', '--porcelain').stdout == foreign_status
                    and (foreign / 'foreign.txt').read_bytes() == foreign_file
                    and fixture.git(source, 'status', '--porcelain').stdout == source_status)
        initial = json.loads(fixture.run_cli(
            repo, 'coordination', 'compose', '--stack', 'new-stack',
            '--known-base-state', data['base_tip'], '--known-base-manifest-digest',
            data['base_state']['manifest_digest']).stdout)
        initial_path = fixture.tmp / 'initial-plan.json'
        initial_path.write_text(json.dumps(initial))
        fixture.run_cli(repo, 'coordination', 'compose', '--apply', '--plan-file', str(initial_path))
        self.assertTrue(unchanged())
        base_tip, base_state = fixture.remote_state(data['origin'])
        fixture.run_cli(repo, 'stack', 'set', 'new-stack', first, second,
                        '--preserve-worktrees', 'preserve fixture lanes')
        fixture.run_cli(repo, 'stack', 'git', 'new-stack', '--', 'cherry-pick', '--ff', second)
        self.assertTrue(unchanged())
        preview = json.loads(fixture.run_cli(
            repo, 'coordination', 'compose', '--stack', 'new-stack', '--known-base-state', base_tip,
            '--known-base-manifest-digest', base_state['manifest_digest']).stdout)
        self.assertEqual(preview['adoptionTransition'], {'base': base, 'oldTip': first, 'newTip': second})
        before = fixture.git(repo, 'ls-remote', '--refs', 'origin').stdout
        plan_path = fixture.tmp / 'transition-plan.json'
        plan_path.write_text(json.dumps(preview))
        if race:
            # Put a competing commit into the bare object store without creating
            # a ref, then move the source immediately before the real atomic push.
            tree = fixture.git(repo, 'rev-parse', first + '^{tree}').stdout.strip()
            competitor = fixture.git(repo, 'commit-tree', tree, '-p', first,
                                     '-m', 'competing writer').stdout.strip()
            fixture.git(data['origin'], 'fetch', '-q', str(repo), competitor)
            original_push = module.run_authorized_push
            raced = False
            def race_push(repo_root, command, remote, refs, **kwargs):
                nonlocal raced
                if not raced and 'refs/heads/pr/new-stack' in refs:
                    raced = True
                    fixture.git(data['origin'], 'update-ref', 'refs/heads/pr/new-stack', competitor, first)
                return original_push(repo_root, command, remote, refs, **kwargs)
            manifest, manifest_path = module.load_manifest(repo)
            with mock.patch.object(module, 'run_authorized_push', side_effect=race_push):
                with self.assertRaises(module.SyncwheelError) as error:
                    module.apply_coordination_compose_stack_plan(repo, manifest, manifest_path, preview)
            self.assertTrue(raced)
            self.assertTrue(any(word in str(error.exception).lower() for word in ('lease', 'changed', 'rejected', 'stale')),
                            str(error.exception))
            self.assertTrue(unchanged())
            after = fixture.git(repo, 'ls-remote', '--refs', 'origin').stdout
            def refs(text):
                return {ref: sha for sha, ref in (line.split('\t') for line in text.splitlines())}
            old_refs, new_refs = refs(before), refs(after)
            self.assertEqual({ref for ref in old_refs.keys() | new_refs.keys()
                              if old_refs.get(ref) != new_refs.get(ref)}, {'refs/heads/pr/new-stack'})
            self.assertEqual(new_refs['refs/heads/pr/new-stack'], competitor)
            return
        fixture.run_cli(repo, 'coordination', 'compose', '--apply', '--plan-file', str(plan_path))
        self.assertTrue(unchanged())
        after = fixture.git(repo, 'ls-remote', '--refs', 'origin').stdout
        def refs(text):
            return {ref: sha for sha, ref in (line.split('\t') for line in text.splitlines())}
        old_refs, new_refs = refs(before), refs(after)
        changed = {ref for ref in old_refs.keys() | new_refs.keys() if old_refs.get(ref) != new_refs.get(ref)}
        self.assertEqual(changed, {'refs/heads/pr/new-stack',
                                   'refs/heads/syncwheel/claim/heads/pr/new-stack',
                                   'refs/heads/syncwheel/state/default'})
        self.assertEqual(new_refs['refs/heads/pr/new-stack'], second)
        fresh = fixture.clone(data['origin'], 'transition-fresh')
        fixture.git(fresh, 'switch', '-q', '-c', 'integration/shared', 'origin/integration/shared')
        fixture.disable_fixture_hooks(fresh)
        fixture.adopt_published_manifest(data['origin'], fresh)
        fixture.run_cli(fresh, 'stack', 'set', 'new-stack', '--published')
        for commit in (first, second):
            fixture.git(fresh, 'cat-file', '-e', commit + '^{commit}')
