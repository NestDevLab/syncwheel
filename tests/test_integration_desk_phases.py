"""Real desk replay keeps late resolutions out of ordinary stack projections."""

import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from _replay_support import (
    create_repo, commit_file, finish_source_scenario, clone_repo, git, run_cli,
    load_syncwheel_module,
)
import test_replay_modes as replay_support


class IntegrationDeskPhaseTest(unittest.TestCase):
    setUp = replay_support.ReplayModesTest.setUp
    tearDown = replay_support.ReplayModesTest.tearDown

    def fixture(self, empty=False):
        source, base = create_repo(self.tmp, 'phase-source')
        git(source, 'switch', '-qc', 'source/ordinary', base)
        ordinary = commit_file(source, 'ordinary.txt', 'ordinary\n', 'ordinary contribution')
        git(source, 'switch', '-qc', 'source/producer', base)
        producer = commit_file(source, 'feature.txt', 'producer\n', 'producer contribution')
        consumer = commit_file(source, 'feature.txt', 'consumer\n', 'consumer contribution')
        git(source, 'switch', '-qc', 'source/tail', base)
        tail = commit_file(source, 'feature.txt', 'consumer\n' if empty else 'historical tail\n', 'late contribution')
        following = commit_file(source, 'following.txt', 'following tail\n', 'following contribution')
        manifest = finish_source_scenario(source, base, [ordinary])
        manifest['syncwheel_tracking'] = 'local-only'
        template = dict(manifest['stacks'][0])
        manifest['stacks'] = []
        for sid, commits in [('ordinary', [ordinary]), ('producer', [producer]), ('consumer', [consumer])]:
            stack = dict(template, id=sid, branch='pr/' + sid, commits=commits)
            if sid == 'ordinary':
                stack.update(integration_commits=[ordinary], integration_only_commits=[tail, following])
            manifest['stacks'].append(stack)
        manifest['integration']['stacks'] = ['ordinary', 'producer', 'consumer']
        repo = clone_repo(source, self.tmp / 'control', manifest)
        for stack in manifest['stacks']:
            git(repo, 'branch', stack['branch'], stack['commits'][-1])
        desk = self.tmp / 'desk'
        module = load_syncwheel_module()
        path = repo / '.syncwheel/manifest.json'
        sources = {stack['branch']: git(repo, 'rev-parse', stack['branch']).stdout.strip() for stack in manifest['stacks']}
        return repo, desk, module, path, sources, ordinary, producer, consumer, tail, following

    def test_cherry_pick_tail_capture_uses_late_parent_and_tail_list(self):
        self.check_tail_capture(legacy=False)

    def test_legacy_cherry_pick_tail_capture_uses_late_parent_and_tail_list(self):
        self.check_tail_capture(legacy=True)

    def test_cherry_pick_empty_tail_keeps_following_step_and_ownership(self):
        self.check_tail_capture(legacy=False, empty=True)

    def test_legacy_cherry_pick_empty_tail_keeps_following_step_and_ownership(self):
        self.check_tail_capture(legacy=True, empty=True)

    def test_legacy_empty_tail_capture_records_missing_ownership_proof(self):
        self.check_tail_capture(legacy=True, empty=True, missing_empty_marker=True)

    def test_legacy_empty_tail_without_exact_proof_preserves_receipt_and_wip(self):
        repo, desk, module, path, sources, *_ = self.fixture(empty=True)
        run_cli(repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(desk), expected=2)
        receipt_path = module.integration_desk_receipt_path(repo, path)
        legacy = json.loads(receipt_path.read_text())
        tail = legacy['operation']['tip']
        legacy['operation'].pop('empty', None)
        for step in legacy['steps']:
            step.pop('integration_only', None)
        receipt_path.write_text(json.dumps(legacy) + '\n')
        wip = desk / 'untracked.txt'
        wip.write_bytes(b'peer work in progress\x00\n')
        before = (path.read_bytes(), receipt_path.read_bytes(), git(desk, 'ls-files', '--stage').stdout,
                  git(desk, 'rev-parse', 'HEAD', 'CHERRY_PICK_HEAD').stdout,
                  len(module.load_ledger_events(repo, path)))
        failed = run_cli(repo, 'stack', 'resolve-integration', 'ordinary', '--worktree', str(desk), expected=2)
        self.assertIn('empty integration-only resolution lacks exact ownership proof', failed.stderr)
        self.assertEqual((path.read_bytes(), receipt_path.read_bytes(), git(desk, 'ls-files', '--stage').stdout,
                          git(desk, 'rev-parse', 'HEAD', 'CHERRY_PICK_HEAD').stdout,
                          len(module.load_ledger_events(repo, path))), before)
        self.assertEqual(wip.read_bytes(), b'peer work in progress\x00\n')
        self.assertIn(tail, json.loads(path.read_text())['stacks'][0]['integration_only_commits'])
        for branch, tip in sources.items():
            self.assertEqual(git(repo, 'rev-parse', branch).stdout.strip(), tip)

    def test_saved_empty_tail_without_ownership_proof_is_not_completed(self):
        repo, desk, module, path, *_ = self.fixture(empty=True)
        run_cli(repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(desk), expected=2)
        receipt_path = module.integration_desk_receipt_path(repo, path)
        saved = json.loads(receipt_path.read_text())
        manifest, _ = module.load_manifest(repo, path)
        stack = next(item for item in manifest['stacks'] if item['id'] == 'ordinary')
        stack['integration_only_commits'].pop(0)
        saved['capture'] = {
            'commit': saved['head'], 'tree': git(desk, 'write-tree').stdout.strip(),
            'context': {'stack': 'ordinary', 'commits': [], 'worktree': str(desk)},
            'inputs': module.integration_desk_inputs(repo, manifest, path),
        }
        path.write_text(json.dumps(manifest) + '\n')
        receipt_path.write_text(json.dumps(saved) + '\n')
        wip = desk / 'untracked.txt'
        wip.write_text('peer work in progress\n')
        before = path.read_bytes(), receipt_path.read_bytes(), git(desk, 'ls-files', '--stage').stdout
        failed = run_cli(repo, 'stack', 'resolve-integration', 'ordinary', '--worktree', str(desk), expected=2)
        self.assertIn('saved empty integration-only capture lacks exact ownership proof', failed.stderr)
        self.assertEqual((path.read_bytes(), receipt_path.read_bytes(), git(desk, 'ls-files', '--stage').stdout), before)
        self.assertEqual(wip.read_text(), 'peer work in progress\n')

    def test_legacy_ready_empty_tail_is_recognized_before_capture(self):
        repo, desk, module, path, _, ordinary, _, _, tail, following = self.fixture(empty=True)
        run_cli(repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(desk), expected=2)
        receipt_path = module.integration_desk_receipt_path(repo, path)
        legacy = json.loads(receipt_path.read_text())
        legacy.update(status='ready')
        legacy.pop('operation')
        for step in legacy['steps']:
            step.pop('integration_only', None)
        receipt_path.write_text(json.dumps(legacy) + '\n')
        stopped = run_cli(repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(desk), expected=2)
        self.assertIn('Empty integration-only step', stopped.stderr)
        recovered = json.loads(receipt_path.read_text())
        self.assertTrue(recovered['operation']['empty'])
        self.assertEqual(recovered['operation']['tip'], tail)
        run_cli(repo, 'stack', 'resolve-integration', 'ordinary', '--worktree', str(desk))
        stack = next(s for s in json.loads(path.read_text())['stacks'] if s['id'] == 'ordinary')
        self.assertEqual(stack['integration_commits'], [ordinary])
        self.assertEqual(stack['integration_only_commits'], [following])
        run_cli(repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(desk))
        self.assertEqual((repo / 'feature.txt').read_text(), 'consumer\n')
        self.assertEqual((repo / 'following.txt').read_text(), 'following tail\n')

    def test_saved_tail_capture_with_ordinary_parent_is_refused(self):
        repo, desk, module, path, sources, *_ = self.fixture()
        run_cli(repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(desk), expected=2)
        receipt_path = module.integration_desk_receipt_path(repo, path)
        receipt = json.loads(receipt_path.read_text())
        wrong_parent = receipt['stack_bases']['ordinary']
        for step in receipt['steps']:
            step.pop('integration_only', None)
        receipt_path.write_text(json.dumps(receipt) + '\n')
        (desk / 'feature.txt').write_text('reviewed tail\n')
        git(desk, 'add', 'feature.txt')
        manifest, _ = module.load_manifest(repo, path)
        stack = next(item for item in manifest['stacks'] if item['id'] == 'ordinary')
        args = SimpleNamespace(specs=[], empty=False, worktree=str(desk))
        # Preserve a write-ahead capture with the old wrong-parent shape.
        with mock.patch.object(module, 'integration_desk_capture_parent', return_value=wrong_parent), \
             mock.patch.object(module, 'finish_integration_desk_capture', side_effect=RuntimeError('interrupt capture')):
            with self.assertRaisesRegex(RuntimeError, 'interrupt capture'):
                with module.manifest_write_transaction(repo, path):
                    module.capture_integration_desk(repo, manifest, path, stack, args)
        before = (path.read_bytes(), receipt_path.read_bytes(), git(desk, 'write-tree').stdout)
        failed = run_cli(repo, 'stack', 'resolve-integration', 'ordinary', '--worktree', str(desk), expected=2)
        self.assertIn('different phase parent', failed.stderr)
        self.assertEqual((path.read_bytes(), receipt_path.read_bytes(), git(desk, 'write-tree').stdout), before)
        for branch, tip in sources.items():
            self.assertEqual(git(repo, 'rev-parse', branch).stdout.strip(), tip)

    def test_legacy_ambiguous_tail_phase_is_not_guessed(self):
        repo, desk, module, path, *_ = self.fixture()
        run_cli(repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(desk), expected=2)
        manifest, _ = module.load_manifest(repo, path)
        receipt = json.loads(module.integration_desk_receipt_path(repo, path).read_text())
        step = receipt['steps'][receipt['next']]
        step.pop('integration_only', None)
        stack = next(item for item in manifest['stacks'] if item['id'] == 'ordinary')
        stack['integration_commits'].append(step['argv'][-1])
        before = json.dumps(receipt)
        with self.assertRaisesRegex(module.SyncwheelError, 'phase is ambiguous'):
            module.normalize_integration_desk_step_phases(manifest, receipt)
        self.assertEqual(json.dumps(receipt), before)

    def check_tail_capture(self, legacy, empty=False, missing_empty_marker=False):
        repo, desk, module, path, sources, ordinary, producer, consumer, tail, following = self.fixture(empty)
        stopped = run_cli(repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(desk), expected=2)
        receipt_path = module.integration_desk_receipt_path(repo, path)
        receipt = json.loads(receipt_path.read_text())
        step = receipt['steps'][receipt['next']]
        self.assertEqual(step['argv'][-1], tail)
        self.assertEqual(step['stack_id'], 'ordinary')
        late_parent = receipt['head']
        self.assertNotEqual(late_parent, receipt['stack_bases']['ordinary'])
        self.assertEqual(git(desk, 'show', 'HEAD:feature.txt').stdout, 'consumer\n')
        if legacy:
            for item in receipt['steps']:
                item.pop('integration_only', None)
            receipt_path.write_text(json.dumps(receipt) + '\n')
        else:
            self.assertTrue(step.get('integration_only'))
        if empty:
            self.assertTrue(receipt.get('operation', {}).get('empty'))
        else:
            self.assertIn('resolution desk', stopped.stderr)
            (desk / 'feature.txt').write_text('reviewed tail\n')
            git(desk, 'add', 'feature.txt')
        if missing_empty_marker:
            receipt['operation'].pop('empty', None)
            receipt_path.write_text(json.dumps(receipt) + '\n')
        run_cli(repo, 'stack', 'resolve-integration', 'ordinary', '--worktree', str(desk))
        saved = json.loads(path.read_text())
        stack = next(item for item in saved['stacks'] if item['id'] == 'ordinary')
        self.assertEqual(stack['commits'], [ordinary])
        self.assertEqual(stack['integration_commits'], [ordinary])
        if empty:
            self.assertEqual(stack['integration_only_commits'], [following])
            events = module.load_ledger_events(repo, path)
            proofs = [e['payload'].get('context', {}).get('empty_integration_only') for e in events]
            self.assertTrue(any(p and p['commit'] == tail and p['head'] == late_parent for p in proofs))
        else:
            replacement, unchanged = stack['integration_only_commits']
            self.assertEqual(unchanged, following)
            self.assertEqual(git(repo, 'rev-parse', replacement + '^').stdout.strip(), late_parent)
            self.assertEqual(git(repo, 'diff', '--name-only', late_parent, replacement).stdout.splitlines(), ['feature.txt'])
        for branch, tip in sources.items():
            self.assertEqual(git(repo, 'rev-parse', branch).stdout.strip(), tip)
        run_cli(repo, 'int', 'rebuild', '--replay-mode', 'desk', '--worktree', str(desk))
        self.assertEqual((repo / 'feature.txt').read_text(), 'consumer\n' if empty else 'reviewed tail\n')
        self.assertEqual((repo / 'following.txt').read_text(), 'following tail\n')
        plan = module.replay_plan(repo, saved, module.replay_target(integration=saved['integration'], worktree=desk, return_tree=True), 'desk')
        picks = [item['argv'][-1] for item in plan['steps'] if 'cherry-pick' in item.get('argv', [])]
        self.assertEqual(picks.count(producer), 1)
        self.assertLess(picks.index(producer), picks.index(consumer))
        run_cli(repo, 'int', 'rebuild', '--replay-mode', 'ephemeral')
        self.assertEqual((repo / 'feature.txt').read_text(), 'consumer\n' if empty else 'reviewed tail\n')
