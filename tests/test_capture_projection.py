import contextlib
import copy
import io
import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_coordination as coordination_fixtures
import test_metadata_preflight as metadata_fixtures
import test_stack_sync_metadata as state_fixtures


class CaptureProjectionTest(unittest.TestCase):
    def capture_fixture(self, *, strategy='cherry-pick', conflicting=True):
        fixture = metadata_fixtures.MetadataPreflightTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare_stack(draft=True)
        base = fixture.git('rev-parse', 'HEAD').decode().strip()
        fixture.git('update-ref', 'refs/remotes/origin/main', base)

        def branch_commit(branch, path, text):
            worktree = fixture.fixture.temp_root / branch
            fixture.git('worktree', 'add', '-qb', branch, str(worktree), base)
            (worktree / path).write_text(text)
            fixture.git('-C', str(worktree), 'add', path)
            fixture.git('-C', str(worktree), 'commit', '-qm', branch)
            tip = fixture.git('-C', str(worktree), 'rev-parse', 'HEAD').decode().strip()
            fixture.git('worktree', 'remove', str(worktree))
            return tip

        candidate = branch_commit('capture-source', 'seed', 'capture edit\n')
        sibling = branch_commit(
            'sibling-source', 'seed' if conflicting else 'sibling.txt', 'sibling edit\n',
        )
        manifest = json.loads(fixture.manifest_path.read_text())
        manifest['stacks'].append({
            **manifest['stacks'][0], 'id': 'sibling', 'branch': 'sibling-source',
            'state': 'published', 'publication': {'enabled': True}, 'commits': [sibling],
        })
        manifest['integration'].update(strategy=strategy, stacks=['feature', 'sibling'])
        fixture.manifest_path.write_text(json.dumps(manifest))
        fixture.git('add', '.syncwheel/manifest.json')
        fixture.git('commit', '-qm', 'Fixture integration manifest')
        with (fixture.repo / '.git' / 'info' / 'exclude').open('a') as exclude:
            exclude.write('\nuntracked-product\n.syncwheel/wt/\n')
        opened = json.loads(fixture.cli('worktree', 'open', 'unrelated', '--json').stdout)
        self.lane_path = Path(opened['lane']['path'])
        self.lane_index = Path(fixture.git(
            '-C', str(self.lane_path), 'rev-parse', '--path-format=absolute', '--git-path', 'index',
        ).decode().strip())
        (fixture.repo / 'untracked-product').write_bytes(b'fixture\x00')
        # Initialize the native lock so byte comparisons cover an established ledger.
        with fixture.module._manifest_storage.ledger_write_lock(fixture.repo, fixture.manifest_path):
            pass
        return fixture, candidate

    def snapshot(self, fixture):
        repo, module, selected = fixture.repo, fixture.module, fixture.manifest_path
        return {
            **fixture.state(),
            'head': fixture.git('rev-parse', 'HEAD'),
            'selected': state_fixtures.file_state(selected),
            'ledger': state_fixtures.directory_state(module.ledger_root(repo, selected)),
            'registry': state_fixtures.file_state(module.governed_worktree_registry_path(repo)),
            'lane_index': state_fixtures.file_state(self.lane_index),
            'lane_files': state_fixtures.directory_state(self.lane_path),
        }

    def assert_full_projection_conflict_is_write_free(self, strategy):
        fixture, candidate = self.capture_fixture(strategy=strategy)
        module = fixture.module
        manifest, _ = module.load_manifest(fixture.repo, fixture.manifest_path)
        module.materialize_integration_projection(fixture.repo, manifest)
        proposed = copy.deepcopy(manifest)
        proposed['stacks'][0]['commits'] = [candidate]
        module.materialize_stack_projection(fixture.repo, proposed['stacks'][0])
        before = self.snapshot(fixture)

        result = fixture.fixture.run_syncwheel(
            'stack', 'capture-integration', 'feature', candidate,
        )

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('integration projection', result.stderr)
        self.assertIn('sibling', result.stderr)
        self.assertEqual(self.snapshot(fixture), before)

    def test_proposed_cherry_pick_composition_conflict_has_no_durable_writes(self):
        self.assert_full_projection_conflict_is_write_free('cherry-pick')

    def test_merge_stacks_projects_prospective_source_tip_before_any_durable_writes(self):
        self.assert_full_projection_conflict_is_write_free('merge-stacks')

    def test_clean_full_projection_allows_capture(self):
        fixture, candidate = self.capture_fixture(conflicting=False)
        result = fixture.cli('stack', 'capture-integration', 'feature', candidate)
        self.assertIn('captured 1 integration commit(s)', result.stdout)
        updated = json.loads(fixture.manifest_path.read_text())
        self.assertEqual(updated['stacks'][0]['commits'], [candidate])
        self.assertEqual(fixture.git('rev-parse', 'syncwheel/draft/feature').decode().strip(), candidate)

    def invoke_capture_in_process(self, fixture, candidate, *options):
        stderr = io.StringIO()
        argv = ['syncwheel', 'stack', 'capture-integration', 'feature', candidate,
                '--repo', str(fixture.repo), *options]
        with contextlib.chdir(fixture.repo), mock.patch.dict(
            'os.environ', fixture.fixture._clean_env(),
        ), mock.patch.object(
            sys, 'argv', argv
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            status = fixture.module.main()
        return status, stderr.getvalue()

    def capture_with_injected_drift(self, fixture, candidate, mutate, *options):
        module = fixture.module
        replay = module.ReplayEngine.materialize_integration_replay
        observed = []

        def replay_then_mutate(engine, manifest, *args, **kwargs):
            result = replay(engine, manifest, *args, **kwargs)
            stack = next(item for item in manifest['stacks'] if item['id'] == 'feature')
            if candidate in stack['commits'] and not observed:
                mutate()
                observed.append(self.snapshot(fixture))
            return result

        with mock.patch.object(
            module.ReplayEngine, 'materialize_integration_replay', replay_then_mutate,
        ):
            status, stderr = self.invoke_capture_in_process(fixture, candidate, *options)
        self.assertEqual(len(observed), 1, stderr)
        self.assertEqual(status, 2, stderr)
        self.assertEqual(self.snapshot(fixture), observed[0])
        return stderr

    def test_merge_dependency_drift_after_projection_refuses_before_writes(self):
        fixture, candidate = self.capture_fixture(strategy='merge-stacks', conflicting=False)
        replacement = fixture.git(
            'commit-tree', 'sibling-source^{tree}', '-p', 'sibling-source',
            '-m', 'Concurrent dependency advance',
        ).decode().strip()
        error = self.capture_with_injected_drift(
            fixture, candidate,
            lambda: fixture.git('update-ref', 'refs/heads/sibling-source', replacement),
        )
        self.assertRegex(error, r'changed|stale|drift')
        self.assertEqual(fixture.git('rev-parse', 'sibling-source').decode().strip(), replacement)

    def test_retained_lane_drift_after_projection_refuses_before_writes(self):
        fixture, candidate = self.capture_fixture(conflicting=False)
        lane = json.loads(fixture.cli(
            'worktree', 'open', 'retain', '--into', 'feature',
            '--base', 'origin/main', '--json',
        ).stdout)['lane']
        self.lane_path = Path(lane['path'])
        fixture.git('-C', str(self.lane_path), 'merge', '--ff-only', candidate)
        self.lane_index = Path(fixture.git(
            '-C', str(self.lane_path), 'rev-parse', '--path-format=absolute', '--git-path', 'index',
        ).decode().strip())
        late_file = self.lane_path / 'late-product.txt'
        error = self.capture_with_injected_drift(
            fixture, candidate, lambda: late_file.write_bytes(b'foreign edit\x00'),
            '--retain-captured-worktrees', 'Keep the validated lane',
        )
        self.assertRegex(error, r'worktree|lane')
        self.assertEqual(late_file.read_bytes(), b'foreign edit\x00')

    def test_handled_persistence_failure_rolls_back_source_and_metadata(self):
        fixture, candidate = self.capture_fixture(conflicting=False)
        module = fixture.module
        branch = 'syncwheel/draft/feature'
        before = self.snapshot(fixture)
        checkpoints = []

        def fail_after_ledger_replacement(phase):
            if phase == 'event_fsynced':
                checkpoints.append(fixture.git('rev-parse', branch).decode().strip())
                raise OSError('fixture ledger write failure')

        with mock.patch.object(
            module._manifest_storage, 'ledger_io_checkpoint', fail_after_ledger_replacement,
        ):
            status, stderr = self.invoke_capture_in_process(fixture, candidate)
        self.assertEqual(checkpoints, [candidate], stderr)
        self.assertEqual(status, 2, stderr)
        self.assertIn('fixture ledger write failure', stderr)
        self.assertEqual(self.snapshot(fixture), before)

    def test_post_capture_cleanup_failure_reports_success_and_preserves_lane(self):
        fixture, candidate = self.capture_fixture(conflicting=False)
        module = fixture.module
        before = self.snapshot(fixture)
        with mock.patch.object(
            module.GovernedWorktrees, 'capture_governed_worktrees_for_stack',
            side_effect=module.SyncwheelError('fixture cleanup failure'),
        ) as cleanup:
            status, stderr = self.invoke_capture_in_process(fixture, candidate)
        cleanup.assert_called_once()
        self.assertEqual(status, 0, stderr)
        self.assertIn('fixture cleanup failure', stderr)
        manifest = json.loads(fixture.manifest_path.read_text())
        self.assertEqual(manifest['stacks'][0]['commits'], [candidate])
        self.assertEqual(fixture.git('rev-parse', 'syncwheel/draft/feature').decode().strip(), candidate)
        after = self.snapshot(fixture)
        for key in ('registry', 'lane_index', 'lane_files', 'product', 'untracked'):
            self.assertEqual(after[key], before[key], key)

    def test_local_capture_does_not_stage_manifest_or_invoke_commit_hook(self):
        fixture, candidate = self.capture_fixture(conflicting=False)
        hook = fixture.repo / '.git' / 'hooks' / 'commit-msg'
        marker = hook.with_name('commit-msg-invoked')
        fixture.fixture.write_executable(
            hook, '#!/bin/sh\ntouch ' + shlex.quote(str(marker)) + '\nexit 1\n',
        )
        before = self.snapshot(fixture)

        result = fixture.fixture.run_syncwheel(
            'stack', 'capture-integration', 'feature', candidate,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('captured 1 integration commit(s)', result.stdout)
        self.assertFalse(marker.exists())
        self.assertEqual(fixture.git('rev-parse', 'HEAD'), before['head'])
        self.assertEqual(json.loads(fixture.manifest_path.read_text())['stacks'][0]['commits'],
                         [candidate])
        self.assertEqual(fixture.git('rev-parse', 'syncwheel/draft/feature').decode().strip(), candidate)
        saved = fixture.module.load_ledger_events(fixture.repo, fixture.manifest_path)
        self.assertEqual(saved[-1]['payload']['reason'], 'stack_capture_integration')
        after = self.snapshot(fixture)
        for key in ('index', 'index_bytes', 'registry', 'lane_index', 'lane_files',
                    'product', 'untracked'):
            self.assertEqual(after[key], before[key], key)

    def test_ref_transaction_preserves_concurrent_symbolic_target_or_backup(self):
        for change in ('target', 'backup'):
            with self.subTest(change=change):
                fixture, candidate = self.capture_fixture(conflicting=False)
                module = fixture.module
                repository = module._StackRepository
                original = repository.capture_ref_transaction
                observed = []
                target = 'refs/heads/syncwheel/draft/feature'
                anchor = 'refs/heads/unrelated-anchor'
                before = fixture.git('rev-parse', target).decode().strip()
                fixture.git('update-ref', anchor, before)
                expected = self.snapshot(fixture)

                def replace_with_symref(repo, operations):
                    backup = next(command.split()[1] for command in operations
                                  if command.startswith('create '))
                    ref = target if change == 'target' else backup
                    fixture.git('symbolic-ref', ref, anchor)
                    observed.append(ref)
                    expected['refs'] = fixture.git('for-each-ref', '--format=%(refname) %(objectname)')
                    return original(repo, operations)

                with mock.patch.object(repository, 'capture_ref_transaction', replace_with_symref):
                    status, stderr = self.invoke_capture_in_process(fixture, candidate)
                self.assertEqual(len(observed), 1, stderr)
                self.assertEqual(status, 2, stderr)
                ref = observed[0]
                self.assertEqual(fixture.git('symbolic-ref', ref).decode().strip(), anchor)
                self.assertEqual(fixture.git('rev-parse', anchor).decode().strip(), before)
                self.assertEqual(self.snapshot(fixture), expected)

    def test_unexpected_git_transaction_reply_aborts_instead_of_hanging(self):
        fixture, candidate = self.capture_fixture(conflicting=False)
        before = self.snapshot(fixture)
        child = r'''
import subprocess
import sys

import syncwheel

real_popen = subprocess.Popen

class InjectedReply:
    def __init__(self, stream):
        self.stream = stream

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def readline(self):
        reply = self.stream.readline()
        if reply == 'prepare: ok\n':
            return 'fixture unexpected prepared reply\n'
        return reply

def popen(argv, *args, **kwargs):
    process = real_popen(argv, *args, **kwargs)
    if argv == ['git', 'update-ref', '--stdin']:
        process.stdout = InjectedReply(process.stdout)
    return process

subprocess.Popen = popen
sys.argv = ['syncwheel', 'stack', 'capture-integration', 'feature', sys.argv[1]]
raise SystemExit(syncwheel.main())
'''
        environment = fixture.fixture._clean_env()
        environment['PYTHONPATH'] = str(Path(fixture.module.__file__).resolve().parent)
        result = subprocess.run(
            [sys.executable, '-c', child, candidate], cwd=fixture.repo,
            env=environment, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('prepare: ok', result.stderr)
        self.assertEqual(self.snapshot(fixture), before)

    def test_trial_checkout_remove_failure_reports_and_preserves_residue(self):
        fixture, candidate = self.capture_fixture(conflicting=False)
        module = fixture.module
        original = module._ReplayRepository.git
        leftovers = []
        before = self.snapshot(fixture)

        def fail_trial_remove(repository, root, *args, **kwargs):
            if args[:3] == ('worktree', 'remove', '--force'):
                trial = Path(args[3])
                if trial.name.startswith('syncwheel-stack-capture-'):
                    leftovers.append(trial)
                    return subprocess.CompletedProcess(args, 1, '', 'fixture remove veto')
            return original(repository, root, *args, **kwargs)

        with mock.patch.object(module._ReplayRepository, 'git', fail_trial_remove):
            status, stderr = self.invoke_capture_in_process(fixture, candidate)
        self.assertEqual(len(leftovers), 1, stderr)
        retained = leftovers[0]
        self.addCleanup(fixture.git, 'worktree', 'remove', '--force', str(retained))
        self.assertEqual(status, 2, stderr)
        self.assertIn(str(retained), stderr)
        self.assertIn('fixture remove veto', stderr)
        self.assertEqual((retained / 'seed').read_bytes(), b'capture edit\n')
        after = self.snapshot(fixture)
        self.assertIn(str(retained).encode(), after.pop('worktrees'))
        before.pop('worktrees')
        self.assertEqual(after, before)

    def test_capture_refuses_pending_promotion_with_publication_lock_intact(self):
        fixture = coordination_fixtures.ActiveActiveCoordinationTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        prepared = fixture.prepare_landed_promotion('capture-pending', 'feature')
        repo, origin, module = prepared['repo'], prepared['origin'], prepared['module']
        observer = state_fixtures.StackSyncMetadataTest()
        selected = repo / '.syncwheel' / 'manifest.json'
        ledger = module.ledger_root(repo, selected)
        tip = fixture.git(repo, 'rev-parse', 'scratch/feature').stdout.strip()
        before = observer.checkout_state(fixture, repo, origin)
        ledger_before = state_fixtures.directory_state(ledger)
        manifest_before = state_fixtures.file_state(selected)
        self.assertTrue(before['publication_lock_files'])

        refused = fixture.run_cli_unchecked(repo, 'stack', 'capture-integration', 'feature', tip)

        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('syncwheel stack promote feature', refused.stderr)
        self.assertEqual(observer.checkout_state(fixture, repo, origin), before)
        self.assertEqual(state_fixtures.directory_state(ledger), ledger_before)
        self.assertEqual(state_fixtures.file_state(selected), manifest_before)

    def test_capture_leaves_stale_publication_lock_intact(self):
        fixture = coordination_fixtures.ActiveActiveCoordinationTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        origin = fixture.create_remote()
        repo = fixture.clone(origin, 'stale')
        fixture.init_coordinated(repo)
        source = fixture.commit_on_branch(repo, 'pr/feature', 'feature.txt')
        fixture.run_cli(repo, 'stack', 'create', 'feature', source, '--branch', 'pr/feature')
        module = fixture.load_module()
        lock = module.coordination_publication_lock_path(repo)
        child = subprocess.run(
            [sys.executable, '-c',
             'import os, sys, syncwheel\n'
             'from pathlib import Path\n'
             'with syncwheel.coordination_publication_lock(Path(sys.argv[1])):\n'
             '    os._exit(19)\n', str(repo)],
            cwd=repo, capture_output=True, text=True,
        )
        self.assertEqual(child.returncode, 19, child.stderr)
        inode = lock.stat().st_ino
        observer = state_fixtures.StackSyncMetadataTest()
        selected = repo / '.syncwheel' / 'manifest.json'
        ledger = module.ledger_root(repo, selected)
        before = observer.checkout_state(fixture, repo, origin)
        ledger_before = state_fixtures.directory_state(ledger)
        manifest_before = state_fixtures.file_state(selected)

        refused = fixture.run_cli_unchecked(repo, 'stack', 'capture-integration', 'feature', source)

        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('syncwheel reconcile --apply', refused.stderr)
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(observer.checkout_state(fixture, repo, origin), before)
        self.assertEqual(state_fixtures.directory_state(ledger), ledger_before)
        self.assertEqual(state_fixtures.file_state(selected), manifest_before)

    def test_capture_refuses_pending_recovery_and_printed_remedy_allows_retry(self):
        fixture = coordination_fixtures.ActiveActiveCoordinationTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        origin, repo, _ = fixture.prepare_pending_control_manifest_intent('capture', 'intent_saved')
        module = fixture.load_module()
        selected = repo / '.syncwheel' / 'manifest.json'
        observer = state_fixtures.StackSyncMetadataTest()

        def snapshot():
            return {
                **observer.checkout_state(fixture, repo, origin),
                'head': fixture.git(repo, 'rev-parse', 'HEAD').stdout,
                'manifest': state_fixtures.file_state(selected),
                'ledger': state_fixtures.directory_state(module.ledger_root(repo, selected)),
            }

        manifest, _ = module.load_manifest(repo, selected)
        stack = next(s for s in manifest['stacks'] if s['id'] == 'feature-a')
        source_path = fixture.tmp / 'capture-source'
        fixture.git(repo, 'worktree', 'add', '--detach', str(source_path), stack['branch'])
        (source_path / 'follow-up.txt').write_text('new stack source\n')
        fixture.git(source_path, 'add', 'follow-up.txt')
        fixture.git(source_path, 'commit', '-qm', 'Add clean capture commit')
        source = fixture.git(source_path, 'rev-parse', 'HEAD').stdout.strip()
        fixture.git(repo, 'worktree', 'remove', str(source_path))
        command = ('stack', 'capture-integration', 'feature-a', source)
        before = snapshot()

        refused = fixture.run_cli_unchecked(repo, *command)

        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn('control manifest recovery is pending', refused.stderr)
        self.assertEqual(snapshot(), before)
        remedy = next(line[line.index('syncwheel reconcile --apply'):].strip()
                      for line in refused.stderr.splitlines()
                      if 'syncwheel reconcile --apply' in line)
        remedy = remedy.removesuffix(' before stack capture-integration')
        args = shlex.split(remedy)
        self.assertEqual(args[:3], ['syncwheel', 'reconcile', '--apply'])
        fixture.run_cli(repo, *args[1:])
        self.assertEqual(module.pending_control_manifest_intents(
            module.load_ledger_events(repo, selected),
        ), [])
        fixture.run_cli(repo, *command)
        updated, _ = module.load_manifest(repo, selected)
        captured = next(s for s in updated['stacks'] if s['id'] == 'feature-a')
        self.assertEqual(captured['commits'], [*stack['commits'], source])
