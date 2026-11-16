import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

import test_coordination as coordination_fixtures
import test_metadata_preflight as metadata_fixtures


def file_state(path):
    if not path.is_file():
        return None
    return path.read_bytes(), path.stat().st_mode


def directory_state(root):
    return {str(path.relative_to(root)): file_state(path)
            for path in sorted(root.rglob('*')) if path.is_file()}


class StackSyncMetadataTest(unittest.TestCase):
    def metadata_fixture(self):
        fixture = metadata_fixtures.MetadataPreflightTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        tip = fixture.git(
            'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'Fixture stack commit',
        ).decode().strip()
        fixture.git('update-ref', 'refs/heads/pr/feature', tip)
        return fixture, tip

    def coordination_fixture(self):
        fixture = coordination_fixtures.ActiveActiveCoordinationTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        return fixture

    def checkout_state(self, fixture, repo, origin):
        module = fixture.load_module()
        common = repo / '.git'
        paths = (
            repo / 'README.md', repo / 'untracked-product', repo / '.gitignore',
            common / 'index', common / 'info/exclude', common / 'FETCH_HEAD',
            module.governed_worktree_registry_path(repo),
        )
        return {
            'files': {str(path): file_state(path) for path in paths},
            'publication_lock_files': {
                path.name: file_state(path)
                for path in (common / 'syncwheel').glob('coordination-publication*')
            },
            'refs': fixture.git(repo, 'for-each-ref').stdout,
            'remote_refs': fixture.git(origin, 'for-each-ref').stdout,
            'worktrees': fixture.git(repo, 'worktree', 'list', '--porcelain').stdout,
        }

    def assert_sync_event(self, module, repo, manifest_path):
        events = module.load_ledger_events(repo, manifest_path)
        event = events[-1]
        self.assertEqual(event['type'], 'manifest_saved')
        self.assertEqual(event['payload']['reason'], 'stack_sync')

    def test_uncoordinated_git_tracked_manifest_preserves_dirty_primary(self):
        fixture, tip = self.metadata_fixture()
        fixture.dirty_product()
        fixture.product.chmod(0o755)
        before = fixture.state()
        mode = fixture.product.stat().st_mode

        fixture.cli('stack', 'sync', 'feature')

        manifest = json.loads(fixture.manifest_path.read_text())
        self.assertEqual(manifest['stacks'][0]['commits'], [tip])
        self.assertEqual(fixture.state(), before)
        self.assertEqual(fixture.product.stat().st_mode, mode)
        self.assert_sync_event(fixture.module, fixture.repo, fixture.manifest_path)

    def test_coordinated_personal_manifest_works_offline_on_clean_and_dirty_primary(self):
        fixture = self.coordination_fixture()
        origin = fixture.create_remote()
        repo = fixture.clone(origin, 'work')
        fixture.init_coordinated(repo)
        tip = fixture.commit_on_branch(repo, 'pr/feature', 'stack-product.txt')
        fixture.run_cli(repo, 'stack', 'create', 'feature', tip, '--branch', 'pr/feature')
        fixture.git(repo, 'switch', '-q', 'integration/shared')
        shared = repo / '.syncwheel' / 'manifest.json'
        shared_state = file_state(shared)
        manifest = json.loads(shared.read_text())
        self.assertEqual(manifest['coordination']['mode'], 'active-active')
        manifest['syncwheel_tracking'] = 'local-only'
        manifest['stacks'][0]['commits'] = []
        module = fixture.load_module()
        personal = module.personal_manifest_path(repo, 'fixture')
        personal.parent.mkdir(exist_ok=True)
        ignore = repo / '.git' / 'info' / 'exclude'
        with ignore.open('a') as handle:
            handle.write('.syncwheel/manifests/*.local.json\n.syncwheel/manifests/*.local-ledger/\n')
        executable = fixture.bin_dir / 'syncwheel'
        executable.unlink()
        executable.write_text('#!/bin/sh\nexec ' + shlex.join([
            sys.executable, str(coordination_fixtures.CLI),
        ]) + ' "$@"\n')
        executable.chmod(0o755)
        fixture.run_cli(repo, 'hooks', 'install', '--apply')
        self.assertTrue(json.loads(fixture.run_cli(repo, 'hooks', 'status').stdout)['enforced'])
        self.assertEqual(fixture.git(repo, 'status', '--porcelain').stdout, '')
        shared_ledger = directory_state(module.ledger_root(repo, shared))

        for dirty in (False, True):
            with self.subTest(dirty=dirty):
                personal.write_text(json.dumps(manifest))
                if dirty:
                    (repo / 'README.md').write_text('unrelated edit\n')
                    (repo / 'README.md').chmod(0o755)
                    (repo / 'untracked-product').write_bytes(b'\x00private test data')
                trace = fixture.tmp / f'git-{dirty}.jsonl'
                before = self.checkout_state(fixture, repo, origin)
                fixture.run_cli(
                    repo, 'stack', 'sync', 'feature', '--personal', 'fixture',
                    extra_env={'GIT_ALLOW_PROTOCOL': '', 'GIT_TRACE2_EVENT': str(trace)},
                )
                self.assertEqual(json.loads(personal.read_text())['stacks'][0]['commits'], [tip])
                self.assertEqual(self.checkout_state(fixture, repo, origin), before)
                self.assertEqual(file_state(shared), shared_state)
                self.assertEqual(directory_state(module.ledger_root(repo, shared)), shared_ledger)
                self.assert_sync_event(module, repo, personal)
                git_commands = [json.loads(line) for line in trace.read_text().splitlines()]
                self.assertFalse(
                    {'fetch', 'push', 'ls-remote'} &
                    {event.get('name') for event in git_commands if event.get('event') == 'cmd_name'}
                )

    def test_sync_neither_repairs_nor_reaps_an_unrelated_lane(self):
        fixture, _tip = self.metadata_fixture()
        opened = json.loads(fixture.cli('worktree', 'open', 'unrelated', '--json').stdout)
        old = Path(opened['lane']['path'])
        moved = old.with_name('unrelated-moved')
        fixture.git('worktree', 'move', str(old), str(moved))
        registry = fixture.module.governed_worktree_registry_path(fixture.repo)
        raw = registry.read_bytes()
        worktrees = fixture.git('worktree', 'list', '--porcelain')
        lane_ref = fixture.git('rev-parse', opened['lane']['branch'])

        fixture.cli('stack', 'sync', 'feature')

        self.assertEqual(registry.read_bytes(), raw)
        self.assertEqual(fixture.git('worktree', 'list', '--porcelain'), worktrees)
        self.assertEqual(fixture.git('rev-parse', opened['lane']['branch']), lane_ref)
        self.assertTrue(moved.is_dir())

    def assert_pending_refusal(self, fixture, repo, origin, stack, remedy, *selector):
        module = fixture.load_module()
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        manifest_before = file_state(manifest_path)
        ledger_root = module.ledger_root(repo, manifest_path)
        ledger_before = directory_state(ledger_root)
        before = self.checkout_state(fixture, repo, origin)

        result = fixture.run_cli_unchecked(repo, 'stack', 'sync', stack, *selector)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertRegex(result.stderr, remedy)
        self.assertEqual(file_state(manifest_path), manifest_before)
        self.assertEqual(directory_state(ledger_root), ledger_before)
        self.assertEqual(self.checkout_state(fixture, repo, origin), before)
        return result

    def test_sync_refuses_shared_recovery_under_personal_selection(self):
        fixture = self.coordination_fixture()
        origin, repo, _ = fixture.prepare_pending_control_manifest_intent(
            'pending', 'manifest_saved',
        )
        module = fixture.load_module()
        shared = repo / '.syncwheel' / 'manifest.json'
        personal = module.personal_manifest_path(repo, 'selected')
        personal.parent.mkdir(exist_ok=True)
        personal.write_bytes(shared.read_bytes())
        profile = module.load_repo_profile(repo)
        module.save_repo_profile(repo, {**profile, 'personal': 'selected'})
        fixture.environment['SYNCWHEEL_PERSONAL'] = 'selected'
        personal_before = file_state(personal)
        personal_ledger = directory_state(module.ledger_root(repo, personal))

        result = self.assert_pending_refusal(
            fixture, repo, origin, 'feature-a',
            r'control manifest recovery is pending.*syncwheel reconcile --apply',
        )
        self.assert_remedy_selection(result, 'syncwheel reconcile --apply', repo)
        remedy = result.stderr.split('syncwheel reconcile', 1)[1].split(' before stack sync', 1)[0]
        fixture.run_cli(repo, 'reconcile', *shlex.split(remedy))
        shared_events = module.load_ledger_events(repo, shared)
        self.assertEqual(module.pending_control_manifest_intents(shared_events), [])
        self.assertEqual(file_state(personal), personal_before)
        self.assertEqual(directory_state(module.ledger_root(repo, personal)), personal_ledger)

    def assert_remedy_selection(self, result, invocation, repo):
        self.assertIn(invocation, result.stderr)
        remedy = result.stderr.partition(invocation)[2].splitlines()[0]
        words = shlex.split(remedy)
        self.assertEqual(words[words.index('--repo') + 1], str(repo))
        self.assertEqual(
            words[words.index('--manifest') + 1], str(repo / '.syncwheel' / 'manifest.json'),
        )

    def test_sync_refuses_landed_pending_promotion(self):
        fixture = self.coordination_fixture()
        origin = fixture.create_remote()
        repo = fixture.clone(origin, 'work')
        fixture.init_coordinated(repo)
        source = fixture.commit_on_branch(repo, 'scratch/promote', 'promote.txt')
        fixture.run_cli(repo, 'stack', 'create', 'pending', source, '--draft')
        fixture.run_cli_sigkill_after(repo, 'authorized_push', 'stack', 'promote', 'pending')

        selected = repo / '.syncwheel' / 'manifest.json'
        result = self.assert_pending_refusal(
            fixture, repo, origin, 'pending', r'syncwheel stack promote pending',
            '--repo', str(repo), '--manifest', str(selected),
        )
        self.assert_remedy_selection(result, 'syncwheel stack promote pending', repo)

    def test_sync_refuses_stale_publication_lock_without_repair(self):
        fixture = self.coordination_fixture()
        origin = fixture.create_remote()
        repo = fixture.clone(origin, 'work')
        fixture.init_coordinated(repo)
        source = fixture.commit_on_branch(repo, 'pr/feature', 'stack.txt')
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

        self.assert_pending_refusal(
            fixture, repo, origin, 'feature', r'syncwheel reconcile --apply',
        )
        self.assertEqual(lock.stat().st_ino, inode)
