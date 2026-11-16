import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import test_stack_adoption


class StackAdoptionEdgeTest(unittest.TestCase):
    def setUp(self):
        self.case = test_stack_adoption.StackAdoptionTest()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.f = self.case.f
        self.module = self.case.module

    def parsed_args(self, *extra):
        return self.module.build_parser().parse_args([
            *self.case.arguments(), '--repo', str(self.f.repo), *extra,
        ])

    def interrupt(self, plan, checkpoint):
        import syncwheel_stack_adoption as adoption

        def crash(stage):
            if stage == checkpoint:
                raise RuntimeError('simulated process interruption')

        args = self.parsed_args('--plan-digest', plan['planDigest'], '--apply')
        with self.module.manifest_write_transaction(self.f.repo, self.case.manifest_path), \
                mock.patch.object(adoption, 'adoption_checkpoint', side_effect=crash), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                self.module.command_stack_set(args)

    def assert_resume_refuses_without_writes(self, plan):
        manifest = self.case.manifest_path.read_bytes()
        refs = self.f.git('for-each-ref')
        result = self.f.run_cli(
            *self.case.arguments(), '--plan-digest', plan['planDigest'], '--apply', expected=2,
        )
        self.assertIn('state changed', result.stderr)
        self.assertEqual(self.case.manifest_path.read_bytes(), manifest)
        self.assertEqual(self.f.git('for-each-ref'), refs)

    def repository_state(self):
        registry = self.module.governed_worktree_registry_path(self.f.repo)
        return {
            'manifest': self.case.manifest_path.read_bytes(),
            'refs': self.f.git('for-each-ref'),
            'index_patch': self.f.git('diff', '--binary', '--cached'),
            'worktree_patch': self.f.git('diff', '--binary'),
            'registry': registry.read_bytes(),
            'ledger': self.module.load_ledger_events(self.f.repo, self.case.manifest_path),
        }

    def test_stack_sync_and_absorb_preserve_adopted_history_and_primary_patch(self):
        self.case.apply(self.case.preview())
        clean_state = self.repository_state()
        result = self.f.run_cli('stack', 'sync', 'replay', expected=2)
        self.assertIn('adopted history requires explicit stack set', result.stderr)
        self.assertEqual(self.repository_state(), clean_state)

        source = self.f.repo / 'pending.txt'
        source.write_text('staged change\n')
        self.f.git('add', source.name)
        source.write_text('additional working tree change\n')
        before = self.repository_state()
        result = self.f.run_cli('stack', 'absorb', 'replay', '--staged', expected=2)
        self.assertIn('adopted history requires explicit stack set', result.stderr)
        self.assertEqual(self.repository_state(), before)

    def test_resume_planner_requires_review_for_an_undeclared_adopted_stack_commit(self):
        self.case.apply(self.case.preview())
        later = self.f.git('commit-tree', self.case.tip + '^{tree}', '-p', self.case.tip,
                           '-m', 'Undeclared update')
        manifest, _ = self.module.load_manifest(self.f.repo)
        original = json.loads(json.dumps(manifest))
        diagnostic = {
            'commit': later,
            'short': later[:7],
            'subject': 'Undeclared update',
            'likely_stacks': [{'id': 'replay'}],
        }
        actions, proposed = self.module.plan_resume_mutations(self.f.repo, manifest, [diagnostic])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['type'], 'resume_manual_review')
        self.assertEqual(actions[0]['stack'], 'replay')
        self.assertEqual(actions[0]['commit'], later)
        self.assertEqual(actions[0]['reason'], 'adopted_history_requires_explicit_adoption')
        self.assertEqual(proposed, original)
        self.assertEqual(manifest, original)

    def test_reconcile_preflight_preserves_adopted_source_rebuild_and_alignment(self):
        self.case.apply(self.case.preview())
        self.f.git('update-ref', '-d', 'refs/heads/pr/replay', self.case.tip)
        before = self.repository_state()
        manifest, _ = self.module.load_manifest(self.f.repo)
        for action_type in ('rebuild_stack', 'align_stack_to_remote'):
            with self.subTest(action=action_type):
                with self.assertRaisesRegex(
                    self.module.SyncwheelError, 'adopted history requires explicit stack set',
                ):
                    self.module.preflight_reconcile_mutation_targets(
                        self.f.repo, manifest, [{'type': action_type, 'stack': 'replay'}], None,
                    )
                self.assertEqual(self.repository_state(), before)
        result = self.f.run_cli('reconcile', '--no-fetch', '--apply', expected=2)
        self.assertIn('adopted history requires explicit stack set', result.stderr)
        self.assertEqual(self.repository_state(), before)

    def test_symbolic_source_ref_is_refused_without_changing_its_target(self):
        self.f.git('update-ref', 'refs/heads/elsewhere', self.case.old_tip)
        self.f.git('symbolic-ref', 'refs/heads/pr/replay', 'refs/heads/elsewhere')
        before = self.case.manifest_path.read_bytes()
        result = self.f.run_cli(*self.case.arguments(), expected=2)
        self.assertIn('requires a direct source ref', result.stderr)
        self.assertEqual(self.f.git('symbolic-ref', 'refs/heads/pr/replay'), 'refs/heads/elsewhere')
        self.assertEqual(self.f.git('rev-parse', 'elsewhere'), self.case.old_tip)
        self.assertEqual(self.case.manifest_path.read_bytes(), before)

    def test_prepared_operation_refuses_a_symbolic_source_at_the_exact_tip(self):
        plan = self.case.preview()
        self.interrupt(plan, 'prepared')
        self.f.git('update-ref', 'refs/heads/elsewhere', self.case.tip)
        self.f.git('symbolic-ref', 'refs/heads/pr/replay', 'refs/heads/elsewhere')
        before = self.case.manifest_path.read_bytes()
        result = self.f.run_cli(
            *self.case.arguments(), '--plan-digest', plan['planDigest'], '--apply', expected=2,
        )
        self.assertIn('requires a direct source ref', result.stderr)
        self.assertEqual(self.f.git('symbolic-ref', 'refs/heads/pr/replay'), 'refs/heads/elsewhere')
        self.assertEqual(self.f.git('rev-parse', 'elsewhere'), self.case.tip)
        self.assertEqual(self.case.manifest_path.read_bytes(), before)

    def test_after_manifest_interruption_resumes_without_rewriting_either_state(self):
        plan = self.case.preview()
        self.interrupt(plan, 'manifest-updated')
        manifest = self.case.manifest_path.read_bytes()
        self.assertEqual(self.f.read_manifest()['stacks'][0]['adopted_history']['tip'], self.case.tip)
        self.assertEqual(self.f.git('rev-parse', 'pr/replay'), self.case.tip)
        self.case.apply(plan)
        self.assertEqual(self.case.manifest_path.read_bytes(), manifest)
        self.assertEqual(self.f.git('rev-parse', 'pr/replay'), self.case.tip)
        events = self.module.load_ledger_events(self.f.repo, self.case.manifest_path)
        self.assertEqual(events[-1]['type'], 'stack_tip_adopted')
        self.assertEqual(events[-1]['payload']['planDigest'], plan['planDigest'])

    def test_after_manifest_with_old_tip_is_an_invalid_recovery_state(self):
        plan = self.case.preview()
        self.interrupt(plan, 'manifest-updated')
        self.f.git('update-ref', 'refs/heads/pr/replay', self.case.old_tip, self.case.tip)
        self.assert_resume_refuses_without_writes(plan)

    def test_prepared_operation_refuses_a_changed_source(self):
        plan = self.case.preview()
        self.interrupt(plan, 'prepared')
        other_tip = self.f.git('commit-tree', self.case.old_tip + '^{tree}', '-p', self.case.old_tip,
                               '-m', 'Independent source update')
        self.f.git('update-ref', 'refs/heads/pr/replay', other_tip, self.case.old_tip)
        self.assert_resume_refuses_without_writes(plan)

    def test_prepared_operation_refuses_a_changed_manifest(self):
        plan = self.case.preview()
        self.interrupt(plan, 'prepared')
        manifest = self.f.read_manifest()
        manifest['stacks'][0]['target_branch'] = 'different-target'
        self.case.manifest_path.write_text(json.dumps(manifest) + '\n')
        self.assert_resume_refuses_without_writes(plan)

    def test_preview_only_observes_worktrees_and_does_not_write_repo_state(self):
        index_paths = [
            Path(self.f.git('-C', str(checkout), 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
            for checkout in (self.f.repo, self.case.lane['path'])
        ]
        original_indexes = [path.read_bytes() for path in index_paths]
        manifest = self.case.manifest_path.read_bytes()
        refs = self.f.git('for-each-ref')
        events = self.module.load_ledger_events(self.f.repo, self.case.manifest_path)
        registry = self.module.governed_worktree_registry_path(self.f.repo)
        lanes = registry.read_bytes()
        out = io.StringIO()
        with mock.patch.object(self.module, 'git', wraps=self.module.git) as git, \
                contextlib.redirect_stdout(out):
            self.assertEqual(self.module.command_stack_set(self.parsed_args()), 0)
        commands = [call.args[1:3] for call in git.call_args_list]
        self.assertTrue(all(command == ('worktree', 'list') for command in commands
                            if command[:1] == ('worktree',)), commands)
        writes = {'update-ref', 'update-index', 'read-tree', 'write-tree', 'commit-tree'}
        self.assertFalse(writes.intersection(command[0] for command in commands))
        self.assertEqual(json.loads(out.getvalue())['tip'], self.case.tip)
        self.assertEqual(self.f.git('for-each-ref'), refs)
        self.assertEqual(self.case.manifest_path.read_bytes(), manifest)
        self.assertEqual([path.read_bytes() for path in index_paths], original_indexes)
        self.assertEqual(self.module.load_ledger_events(self.f.repo, self.case.manifest_path), events)
        self.assertEqual(registry.read_bytes(), lanes)


if __name__ == '__main__':
    unittest.main()
