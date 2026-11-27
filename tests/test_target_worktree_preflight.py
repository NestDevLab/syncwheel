"""Stack mutations do not complete lifecycle operations belonging to other stacks."""

import copy
import hashlib
import json
from pathlib import Path
import unittest

import test_syncwheel as fixtures


class TargetWorktreePreflightTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SyncwheelFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.module = self.fixture.load_syncwheel_module()
        self.repo = self.fixture.repo
        manifest = self.fixture.read_manifest()
        selected = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-a')
        selected['base'] = self.fixture.git('rev-parse', 'pr/feature-a')
        selected['commits'] = []
        (self.repo / '.syncwheel/manifest.json').write_text(json.dumps(manifest) + '\n')

    def open(self, name, target='feature-b'):
        return json.loads(self.fixture.run_cli(
            'worktree', 'open', name, '--into', target, '--json',
        ).stdout)['lane']

    def prepare_foreign_lanes(self):
        terminal = self.open('terminal')
        released = json.loads(self.fixture.run_cli(
            'worktree', 'release', terminal['id'], '--reason', 'Delivered', '--apply', '--json',
        ).stdout)['lane']
        pending = self.open('pending')
        self.fixture.run_cli_until_cleanup_sigkill(
            'after_cleanup_intent', 'worktree', 'release', pending['id'],
            '--reason', 'Interrupted release', '--apply', '--json',
        )
        active = self.open('active')
        path = Path(active['path'])
        (path / 'staged.txt').write_bytes(b'staged\n')
        self.fixture.git('-C', str(path), 'add', 'staged.txt')
        (path / 'staged.txt').write_bytes(b'unstaged\n')
        (path / 'untracked.bin').write_bytes(b'private wip\x00')
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        registry['lanes'].append(released)
        self.module.save_governed_worktree_registry(self.repo, registry)
        self.foreign = {'terminal', 'pending', 'active'}
        # Retained lock recovery files must not be swept by a stack operation.
        self.stale = self.module.governed_worktree_lock_path(self.repo).with_name(
            'governed-worktrees.lock.stale-fixture')
        self.stale.write_bytes(b'preserved lock evidence\n')

    def snapshot(self):
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        records = [lane for lane in registry['lanes'] if lane['id'] in self.foreign]
        files = {}
        for lane in records:
            path = Path(lane['path'])
            if path.exists():
                files[lane['id']] = {
                    str(item.relative_to(path)): item.read_bytes()
                    for item in path.rglob('*') if item.is_file()
                }
                index = self.fixture.git('-C', str(path), 'rev-parse', '--path-format=absolute', '--git-path', 'index')
                files[lane['id']]['INDEX'] = Path(index).read_bytes()
        events = [event for event in self.module.load_ledger_events(self.repo)
                  if (event.get('payload') or {}).get('lane') in self.foreign]
        registrations = [self.module.find_worktree_record_for_branch(self.repo, lane['branch'])
                         for lane in records]
        return copy.deepcopy(records), files, events, registrations, self.stale.read_bytes(), self.fixture.git(
            'for-each-ref', '--format=%(refname) %(objectname)', 'refs/syncwheel')

    def test_fast_forward_and_add_preserve_foreign_pending_terminal_and_wip(self):
        self.prepare_foreign_lanes()
        target = self.fixture.tmp / 'target'
        self.fixture.git('worktree', 'add', '-q', str(target), 'pr/feature-a')
        self.fixture.git('switch', '-q', '-c', 'next-source', 'pr/feature-a')
        (self.repo / 'next.txt').write_text('next source\n')
        self.fixture.git('add', 'next.txt')
        self.fixture.git('commit', '-qm', 'Next source')
        tip = self.fixture.git('rev-parse', 'HEAD')
        self.fixture.git('switch', '-q', 'main')
        before = self.snapshot()
        self.fixture.run_cli('stack', 'git', 'feature-a', '--worktree', str(target),
                             '--', 'merge', '--ff-only', tip)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fixture.git('-C', str(target), 'rev-parse', 'HEAD'), tip)
        digest = hashlib.sha256((self.repo / '.syncwheel/manifest.json').read_bytes()).hexdigest()
        self.fixture.run_cli('stack', 'add', 'feature-a', tip, '--at-source', tip, digest)
        self.assertEqual(self.snapshot(), before)
        stack = next(stack for stack in self.fixture.read_manifest()['stacks'] if stack['id'] == 'feature-a')
        self.assertIn(tip, stack['commits'])

    def source_candidate(self):
        base = self.fixture.git('rev-parse', 'pr/feature-a')
        tip = self.fixture.git('commit-tree', base + '^{tree}', '-p', base, '-m', 'Candidate')
        peer = self.fixture.git('commit-tree', base + '^{tree}', '-p', base, '-m', 'Concurrent peer')
        return base, tip, peer

    def test_fast_forward_rejects_diverged_peer_preserving_recipe_and_foreign_lanes(self):
        self.prepare_foreign_lanes()
        target = self.fixture.tmp / 'target'
        self.fixture.git('worktree', 'add', '-q', str(target), 'pr/feature-a')
        base, tip, peer = self.source_candidate()
        self.fixture.git('update-ref', 'refs/heads/pr/feature-a', peer, base)
        path = self.repo / '.syncwheel/manifest.json'
        manifest = self.fixture.read_manifest()
        next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-a')['commits'] = [peer]
        path.write_text(json.dumps(manifest) + '\n')
        reviewed = path.read_bytes()
        before = self.snapshot()
        self.fixture.run_cli('stack', 'git', 'feature-a', '--worktree', str(target),
                             '--', 'merge', '--ff-only', tip, expected=128)
        self.assertEqual(path.read_bytes(), reviewed)
        self.assertEqual(self.fixture.git('rev-parse', 'pr/feature-a'), peer)
        self.assertEqual(self.snapshot(), before)

    def guarded_add_rejects_peer(self, change_manifest):
        self.prepare_foreign_lanes()
        base, tip, peer = self.source_candidate()
        self.fixture.git('update-ref', 'refs/heads/pr/feature-a', tip, base)
        path = self.repo / '.syncwheel/manifest.json'
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if change_manifest:
            manifest = self.fixture.read_manifest()
            next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-a')['commits'] = [peer]
            path.write_text(json.dumps(manifest) + '\n')
        else:
            self.fixture.git('update-ref', 'refs/heads/pr/feature-a', peer, tip)
        reviewed = path.read_bytes()
        source = self.fixture.git('rev-parse', 'pr/feature-a')
        before = self.snapshot()
        result = self.fixture.run_cli('stack', 'add', 'feature-a', tip, '--at-source', tip, digest, expected=2)
        self.assertIn('manifest has changed' if change_manifest else 'expected', result.stderr)
        self.assertEqual(path.read_bytes(), reviewed)
        self.assertEqual(self.fixture.git('rev-parse', 'pr/feature-a'), source)
        self.assertEqual(self.snapshot(), before)

    def test_guarded_add_rejects_changed_manifest_and_preserves_peer_declaration(self):
        self.guarded_add_rejects_peer(True)

    def test_guarded_add_rejects_changed_source_and_preserves_peer_tip(self):
        self.guarded_add_rejects_peer(False)

    def test_add_captures_target_without_recovering_foreign_lifecycle(self):
        selected = self.open('selected', 'feature-a')
        self.prepare_foreign_lanes()
        path = Path(selected['path'])
        (path / 'selected.txt').write_text('selected source\n')
        self.fixture.git('-C', str(path), 'add', 'selected.txt')
        self.fixture.git('-C', str(path), 'commit', '-qm', 'Selected source')
        tip = self.fixture.git('-C', str(path), 'rev-parse', 'HEAD')
        before = self.snapshot()
        self.fixture.run_cli('stack', 'add', 'feature-a', tip)
        # Capture may create its own recovery ref, so compare foreign refs separately.
        after = self.snapshot()
        self.assertEqual(after[:-1], before[:-1])
        self.assertTrue(set(before[-1].splitlines()).issubset(after[-1].splitlines()))
        self.assertFalse(path.exists())
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        record = next(lane for lane in registry['lanes'] if lane['id'] == selected['id'])
        self.assertEqual(record['state'], 'reaped')

    def test_capture_does_not_restore_foreign_missing_intent_or_repair_moved_path(self):
        selected = self.open('selected', 'feature-a')
        path = Path(selected['path'])
        (path / 'selected.txt').write_text('selected source\n')
        self.fixture.git('-C', str(path), 'add', 'selected.txt')
        self.fixture.git('-C', str(path), 'commit', '-qm', 'Selected source')
        tip = self.fixture.git('-C', str(path), 'rev-parse', 'HEAD')
        self.prepare_foreign_lanes()
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        active = next(lane for lane in registry['lanes'] if lane['id'] == 'active')
        moved = Path(active['path']).with_name('moved-active')
        self.fixture.git('worktree', 'move', active['path'], str(moved))
        registry['lanes'] = [lane for lane in registry['lanes'] if lane['id'] != 'pending']
        self.module.save_governed_worktree_registry(self.repo, registry)
        records = copy.deepcopy([lane for lane in registry['lanes'] if lane['id'] in self.foreign])
        self.module.reconcile_governed_worktrees(
            self.repo, self.fixture.read_manifest(), candidate_ids=set(),
            repair_all_paths=True, target_stacks={'feature-a'},
        )
        self.fixture.run_cli('stack', 'add', 'feature-a', tip)
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        self.assertEqual([lane for lane in registry['lanes'] if lane['id'] in self.foreign], records)
        self.assertEqual((moved / 'staged.txt').read_bytes(), b'unstaged\n')
        self.assertEqual((moved / 'untracked.bin').read_bytes(), b'private wip\x00')
        self.assertTrue(self.stale.exists())

    def test_stack_git_completes_target_pending_cleanup_only(self):
        selected = self.open('selected', 'feature-a')
        self.fixture.run_cli_until_cleanup_sigkill(
            'after_cleanup_intent', 'worktree', 'release', selected['id'],
            '--reason', 'Interrupted selected release', '--apply', '--json',
        )
        self.prepare_foreign_lanes()
        before = self.snapshot()
        target = self.fixture.tmp / 'target'
        self.fixture.run_cli('stack', 'git', 'feature-a', '--worktree', str(target),
                             '--', 'status', '--short')
        after = self.snapshot()
        self.assertEqual(after[:-1], before[:-1])
        self.assertTrue(set(before[-1].splitlines()).issubset(after[-1].splitlines()))
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        self.assertNotIn(selected['id'], {lane['id'] for lane in registry['lanes']})
        self.assertFalse(Path(selected['path']).exists())

    def test_selected_gc_recovers_only_selected_terminal_and_global_gc_recovers_all(self):
        self.prepare_foreign_lanes()
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        terminal = next(lane for lane in registry['lanes'] if lane['id'] == 'terminal')
        # An explicit selected reconciliation must not replay another lane's intent.
        self.module.reconcile_governed_worktrees(
            self.repo, self.fixture.read_manifest(), candidate_ids={terminal['id']},
        )
        after, _ = self.module.load_governed_worktree_registry(self.repo)
        self.assertNotIn(terminal['id'], {lane['id'] for lane in after['lanes']})
        self.assertIn('pending', {lane['id'] for lane in after['lanes']})
        self.assertTrue(self.stale.exists())
        self.fixture.run_cli('gc', '--apply', '--json')
        self.assertFalse(self.stale.exists())
        after, _ = self.module.load_governed_worktree_registry(self.repo)
        self.assertNotIn('pending', {lane['id'] for lane in after['lanes']})
        self.assertIn('active', {lane['id'] for lane in after['lanes']})
