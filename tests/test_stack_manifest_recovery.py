"""Withdraw only a native, ledger-proven declaration from a dirty control file."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import unittest

import test_syncwheel as fixtures
from test_retire_copy import file_tree


class StackManifestRecoveryTest(unittest.TestCase):
    reason = 'Withdraw the declaration created in the wrong manifest'
    environment = {'SYNCWHEEL_UPDATE_MODE': 'off', 'GIT_OPTIONAL_LOCKS': '0',
                   'GIT_NO_LAZY_FETCH': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                   'SYNCWHEEL_LANE_OWNER': 'fixture-recovery-owner'}

    def setUp(self):
        self.f = fixtures.SyncwheelFixtureTest()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.repo = self.f.repo
        self.module = self.f.load_syncwheel_module()
        self.manifest = self.repo / '.syncwheel/manifest.json'
        self.git('switch', '-qc', 'source-original')
        (self.repo / 'change.txt').write_text('preserve both commit generations\n')
        self.git('add', 'change.txt')
        self.git('commit', '-qm', 'Source change')
        self.original = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('switch', '-q', 'main')
        data = self.f.read_manifest()
        data.update(version=2, syncwheel_tracking='git-tracked',
                    syncwheel_worktree_root='var/syncwheel', stacks=[])
        data['coordination'] = {'mode': 'disabled', 'id': 'default', 'remote': 'origin',
                                'state_branch': 'syncwheel/state/default'}
        data['defaults'].update(publication_remote='origin', integration_membership='required')
        data['integration'].update(branch='main-integration', stacks=[], strategy='cherry-pick')
        self.manifest.write_text(json.dumps(data, indent=2) + '\n')
        normalized, _ = self.module.load_manifest(self.repo, self.manifest)
        self.manifest.write_text(json.dumps(normalized, indent=2, sort_keys=True) + '\n')
        self.module.ensure_syncwheel_metadata_excluded(self.repo, 'git-tracked', 'var/syncwheel')
        self.git('add', '-f', '.syncwheel/manifest.json', '.gitignore')
        self.git('commit', '-qm', 'Track shared control')
        self.base = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('switch', '-qc', 'main-integration')
        self.origin = self.f.tmp / 'origin.git'
        self.git('clone', '--bare', '-q', self.repo, self.origin)
        self.git('remote', 'add', 'origin', self.origin)
        # Invoke the candidate itself from the installed native hooks.
        self.bin_dir = self.f.tmp / 'bin'
        self.bin_dir.mkdir()
        self.hook_log = self.f.tmp / 'hook-invocations.log'
        executable = self.bin_dir / 'syncwheel'
        executable.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> '
                              + shlex.quote(str(self.hook_log)) + '\nexec ' + shlex.join([
            sys.executable, str(fixtures.CLI),
        ]) + ' "$@"\n')
        executable.chmod(0o755)
        self.env = {**self.environment, 'PATH': str(self.bin_dir) + os.pathsep + os.environ['PATH']}
        self.veto_file = self.f.tmp / 'reference-veto'
        self.reference_log = self.f.tmp / 'reference-hook.log'
        user_hook = self.repo / '.git/hooks/reference-transaction'
        user_hook.write_text(
            '#!/bin/sh\n'
            'printf "%s auth=%s\\n" "${1:-}" "${'
            + self.module.MANAGED_REF_MOVE_AUTH_ENV + ':+present}" >> '
            + shlex.quote(str(self.reference_log)) + '\n'
            'if [ "${1:-}" = prepared ] && [ -e ' + shlex.quote(str(self.veto_file))
            + ' ]; then exit 7; fi\nexit 0\n'
        )
        user_hook.chmod(0o755)
        self.cli('hooks', 'install', '--apply')
        self.cli('stack', 'create', 'withdrawn', self.original, '--base', self.base,
                 '--branch', 'pr/withdrawn', '--target-remote', 'origin', '--target-branch', 'main')
        self.declared_head = self.git('rev-parse', 'HEAD').decode().strip()
        self.cli('stack', 'rebuild', 'withdrawn', '--replay-mode', 'plumbing')
        self.rebuilt = self.git('rev-parse', 'pr/withdrawn').decode().strip()
        self.assertNotEqual(self.original, self.rebuilt)
        self.lane = json.loads(self.f.run_cli(
            'worktree', 'open', 'retained-source', '--into', 'withdrawn',
            '--base', self.rebuilt, '--json',
            extra_env={**self.env, 'SYNCWHEEL_LANE_OWNER': 'another-fixture-owner'},
        ).stdout)['lane']
        self.lane_path = Path(self.lane['path'])
        self.lane_index = Path(self.git('rev-parse', '--path-format=absolute', '--git-path',
                                        'index', cwd=self.lane_path).decode().strip())
        self.cli('stack', 'sync', 'withdrawn')
        self.assertEqual(self.git('diff', '--name-only').decode().strip(), '.syncwheel/manifest.json')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), b'')
        self.before_events = self.module.load_ledger_events(self.repo, self.manifest)
        self.assertEqual(self.before_events[-1]['payload']['reason'], 'stack_sync')

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *map(str, args)], cwd=cwd or self.repo,
                              env={**os.environ, **getattr(self, 'env', self.environment)},
                              capture_output=True, check=True).stdout

    def cli(self, *args, expected=0):
        return self.f.run_cli(*args, expected=expected, extra_env=self.env)

    def arguments(self):
        return ('manifest', 'recover-stack', 'withdrawn', '--reason', self.reason)

    def preview(self, expected=0):
        result = self.cli(*self.arguments(), expected=expected)
        return json.loads(result.stdout) if expected == 0 else result

    def apply(self, plan, expected=0):
        return self.cli(*self.arguments(), '--plan-digest', plan['planDigest'], '--apply',
                        expected=expected)

    def snapshot(self):
        return {'files': file_tree(self.repo), 'refs': self.git('show-ref'),
                'remote': self.git('--git-dir', self.origin, 'show-ref')}

    def test_existing_commands_expose_dirty_control_deadlock(self):
        before = self.snapshot()
        # The dirty control gate is reached before the independent lane guard.
        refused = self.cli('stack', 'archive', 'withdrawn', '--reason', self.reason,
                           '--resume-intent', 'Use a personal manifest',
                           '--plan-digest', 'not-a-reviewed-plan', '--apply', expected=2)
        self.assertIn('primary checkout control manifest is dirty', refused.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_preview_is_observational_and_apply_requires_the_digest(self):
        before = self.snapshot()
        plan = self.preview()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(plan['stack'], 'withdrawn')
        self.assertGreaterEqual(set(plan['preservedCommits']), {self.original, self.rebuilt})
        self.cli(*self.arguments(), '--apply', expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_recovery_preserves_both_generations_and_compensates_the_ledger(self):
        plan = self.preview()
        refs = self.git('show-ref').decode().splitlines()
        protected = {path: (self.repo / path).read_bytes()
                     for path in ('alpha.txt', 'beta.txt', '.gitignore', '.git/info/exclude')}
        hooks = file_tree(self.repo / '.git/hooks')
        registry = self.module.governed_worktree_registry_path(self.repo).read_bytes()
        lane_files, lane_index = file_tree(self.lane_path), self.lane_index.read_bytes()
        worktrees = self.git('worktree', 'list', '--porcelain')
        remote = self.git('--git-dir', self.origin, 'show-ref')
        hook_lines = len(self.hook_log.read_text().splitlines())
        self.apply(plan)
        invoked = self.hook_log.read_text().splitlines()[hook_lines:]
        self.assertTrue(any('hooks ref-guard' in line for line in invoked), invoked)
        self.assertEqual(self.git('rev-parse', 'HEAD^').decode().strip(), self.declared_head)
        self.assertEqual(self.git('diff', '--name-only', 'HEAD^', 'HEAD').decode().strip(),
                         '.syncwheel/manifest.json')
        control = self.git('rev-parse', 'HEAD').decode().strip()
        self.assertTrue(self.module.is_manifest_only_commit(self.repo, control))
        self.assertTrue(self.module.integration_control_only_transition(
            self.repo, control, self.declared_head))
        self.assertEqual(self.git('status', '--porcelain'), b'')
        self.assertEqual(self.f.read_manifest()['stacks'], [])
        self.assertEqual(self.f.read_manifest()['integration']['stacks'], [])
        for ref, tip in plan['anchorRefs'].items():
            self.assertEqual(self.git('rev-parse', ref).decode().strip(), tip)
        for entry in refs:
            tip, ref = entry.split()
            if ref != 'refs/heads/main-integration':
                self.assertEqual(self.git('rev-parse', ref).decode().strip(), tip)
        self.assertEqual({path: (self.repo / path).read_bytes() for path in protected}, protected)
        self.assertEqual(file_tree(self.repo / '.git/hooks'), hooks)
        self.assertEqual(self.module.governed_worktree_registry_path(self.repo).read_bytes(), registry)
        self.assertEqual(file_tree(self.lane_path), lane_files)
        self.assertEqual(self.lane_index.read_bytes(), lane_index)
        self.assertEqual(self.git('worktree', 'list', '--porcelain').replace(
            self.git('rev-parse', 'HEAD').strip(), self.declared_head.encode()), worktrees)
        self.assertEqual(self.git('--git-dir', self.origin, 'show-ref'), remote)
        events = self.module.load_ledger_events(self.repo, self.manifest)
        self.assertEqual(events[:len(self.before_events)], self.before_events)
        terminal = [e for e in events if e['type'] == 'manifest_saved'
                    and e['payload']['reason'] == 'stack_withdraw']
        self.assertEqual(len(terminal), 1)
        state = self.module.load_ledger_state(self.repo, self.manifest)
        self.assertFalse(state['stacks']['withdrawn']['active_in_manifest'])
        # A repeated exact apply must not create another control commit or event.
        before = self.snapshot()
        self.apply(plan)
        self.assertEqual(self.snapshot(), before)

    def test_refusal_names_the_recovery_preview(self):
        refused = self.cli('stack', 'archive', 'withdrawn', '--reason', self.reason,
                           '--resume-intent', 'Use a personal manifest',
                           '--plan-digest', 'not-a-reviewed-plan', '--apply', expected=2)
        self.assertIn('manifest recover-stack', refused.stderr)
        self.assertIn('--repo ' + shlex.quote(str(self.repo)), refused.stderr)

    def prepare_declaration_absent_from_head(self):
        # Simulate a historical native declaration saved before its control
        # commit became visible. Only this synthetic fixture moves its primary.
        token = self.module.authorize_ref_move(self.repo)
        self.env[self.module.MANAGED_REF_MOVE_AUTH_ENV] = token
        try:
            self.git('update-ref', 'refs/heads/main-integration', self.base, self.declared_head)
            self.git('read-tree', self.base)
        finally:
            self.env.pop(self.module.MANAGED_REF_MOVE_AUTH_ENV)
            self.module.clear_ref_authorizations(self.repo)
        self.declared_head = self.base

    def test_already_desired_head_is_reused_without_an_empty_commit(self):
        self.prepare_declaration_absent_from_head()
        plan = self.preview()
        self.apply(plan)
        self.assertEqual(self.git('rev-parse', 'HEAD').decode().strip(), self.base)
        self.assertEqual(self.git('status', '--porcelain'), b'')
        for ref, tip in plan['anchorRefs'].items():
            self.assertEqual(self.git('rev-parse', ref).decode().strip(), tip)
        events = self.module.load_ledger_events(self.repo, self.manifest)
        terminal = [e for e in events if e['type'] == 'manifest_saved'
                    and e['payload']['reason'] == 'stack_withdraw']
        self.assertEqual(len(terminal), 1)
        before = self.snapshot()
        self.apply(plan)
        self.assertEqual(self.snapshot(), before)

    def test_unchanged_head_resumes_after_anchor_transaction(self):
        self.prepare_declaration_absent_from_head()
        plan = self.preview()
        driver = '''
import os, signal, syncwheel, syncwheel_stack_recovery
def checkpoint(stage):
    if stage == 'refs-updated':
        os.kill(os.getpid(), signal.SIGKILL)
syncwheel_stack_recovery.stack_recovery_checkpoint = checkpoint
raise SystemExit(syncwheel.main())
'''
        result = subprocess.run(
            [sys.executable, '-c', driver, *self.arguments(), '--plan-digest', plan['planDigest'], '--apply'],
            cwd=self.repo, env={**os.environ, **self.env, 'PYTHONPATH': str(fixtures.CLI.parent)},
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.apply(plan)
        self.assertEqual(self.git('rev-parse', 'HEAD').decode().strip(), self.base)
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_control_patch_publishes_through_a_personal_maintenance_stack(self):
        # Give the disposable delivery base the old declaration. The real remote
        # may already lack it, in which case there is no maintenance patch to send.
        self.git('--git-dir', self.origin, 'fetch', '-q', self.repo, self.declared_head)
        self.git('--git-dir', self.origin, 'update-ref', 'refs/heads/main', self.declared_head)
        self.git('fetch', '-q', 'origin', 'main')
        self.apply(self.preview())
        control = self.git('rev-parse', 'HEAD').decode().strip()
        primary = self.manifest.read_bytes()
        self.cli('init', '--personal', 'maintenance', '--canonical-remote', 'origin',
                 '--publication-remote', 'origin', '--base-branch', 'main',
                 '--integration-branch', 'integration/maintenance/main',
                 '--syncwheel-tracking', 'git-tracked', '--no-coordination',
                 '--worktree-root', 'var/syncwheel')
        self.cli('stack', 'create', 'control-maintenance', '--personal', 'maintenance',
                 '--branch', 'maintenance/withdraw-declaration', '--base', self.declared_head,
                 '--target-remote', 'origin', '--target-branch', 'main')
        lane = json.loads(self.cli(
            'worktree', 'open', 'control-maintenance', '--into', 'control-maintenance',
            '--base', self.declared_head, '--personal', 'maintenance', '--json',
        ).stdout)['lane']
        root = Path(lane['path'])
        # The base is byte-identical to the recovery parent in this fixture.
        self.git('restore', '--source', control, '--staged', '--worktree', '--',
                 '.syncwheel/manifest.json', cwd=root)
        self.assertEqual(self.git('diff', '--cached', '--name-only', cwd=root).decode().strip(),
                         '.syncwheel/manifest.json')
        self.git('commit', '-qm', 'Withdraw shared declaration', cwd=root)
        tip = self.git('rev-parse', 'HEAD', cwd=root).decode().strip()
        args = ('stack', 'set', 'control-maintenance', '--personal', 'maintenance',
                '--base', self.declared_head, '--adopt-tip', tip)
        plan = json.loads(self.cli(*args).stdout)
        self.cli(*args, '--plan-digest', plan['planDigest'], '--apply')
        self.cli('stack', 'push', 'control-maintenance', '--personal', 'maintenance', '--dry-run')
        self.cli('stack', 'push', 'control-maintenance', '--personal', 'maintenance')
        self.assertEqual(self.git('--git-dir', self.origin, 'rev-parse',
                                  'refs/heads/maintenance/withdraw-declaration').decode().strip(), tip)
        self.assertEqual(self.git('--git-dir', self.origin, 'rev-parse', 'main').decode().strip(),
                         self.declared_head)
        self.assertEqual(self.git('rev-parse', 'HEAD').decode().strip(), control)
        self.assertEqual(self.manifest.read_bytes(), primary)
        self.assertEqual(self.f.read_manifest()['integration']['stacks'], [])

    def test_rebuild_and_push_remedies_always_name_the_primary_checkout(self):
        for command in ('rebuild', 'push'):
            with self.subTest(command=command):
                before = self.snapshot()
                refused = self.f.run_cli(
                    'stack', command, 'withdrawn', '--repo', str(self.repo),
                    '--manifest', str(self.manifest), expected=2,
                    extra_env=self.env, cwd=self.lane_path,
                )
                self.assertIn('manifest recover-stack STACK', refused.stderr)
                self.assertIn('--repo ' + shlex.quote(str(self.repo)), refused.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_foreign_manifest_edit_refuses_without_writing(self):
        data = self.f.read_manifest()
        data['stacks'][0]['meta']['unrecorded'] = 'another session owns this'
        self.manifest.write_text(json.dumps(data, indent=2) + '\n')
        before = self.snapshot()
        self.preview(expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_manual_formatting_is_not_native_write_proof(self):
        self.manifest.write_bytes(self.manifest.read_bytes() + b'\n')
        before = self.snapshot()
        self.preview(expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_unrelated_manifest_setting_is_not_withdrawn(self):
        data = self.f.read_manifest()
        data['defaults']['publication_remote'] = 'another-remote'
        data['coordination']['remote'] = 'another-remote'
        self.manifest.write_text(json.dumps(data, indent=2) + '\n')
        before = self.snapshot()
        self.preview(expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_ledger_append_invalidates_the_reviewed_plan(self):
        plan = self.preview()
        self.module.append_ledger_event(
            self.repo, 'fixture_concurrent_event', {'reason': 'unrelated actor'}, self.manifest)
        before = self.snapshot()
        self.apply(plan, expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_selected_personal_manifest_is_refused_without_writing(self):
        path = self.repo / '.syncwheel/manifests/selected.local.json'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(self.manifest.read_bytes())
        before = self.snapshot()
        self.cli(*self.arguments(), '--personal', 'selected', expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_json_key_is_not_native_write_proof(self):
        raw = self.manifest.read_text()
        self.manifest.write_text(raw.replace('{', '{"version": 2,', 1))
        before = self.snapshot()
        self.preview(expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_unrelated_tracked_staged_and_untracked_dirt_refuses(self):
        product = self.repo / 'alpha.txt'
        original = product.read_bytes()
        for kind in ('unstaged', 'staged', 'untracked'):
            with self.subTest(kind=kind):
                path = self.repo / 'foreign.txt' if kind == 'untracked' else product
                path.write_bytes(b'preserve another writer\n')
                if kind == 'staged':
                    self.git('add', path.name)
                before = self.snapshot()
                self.preview(expected=2)
                self.assertEqual(self.snapshot(), before)
                if kind == 'untracked':
                    path.unlink()
                else:
                    product.write_bytes(original)
                    if kind == 'staged':
                        self.git('add', path.name)

    def test_stale_plan_refuses_after_another_manifest_edit(self):
        plan = self.preview()
        self.manifest.write_bytes(self.manifest.read_bytes() + b'\n')
        before = self.snapshot()
        self.apply(plan, expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_reviewed_reason_cannot_be_replaced_on_apply(self):
        plan = self.preview()
        before = self.snapshot()
        self.cli('manifest', 'recover-stack', 'withdrawn', '--reason', 'Different intent',
                 '--plan-digest', plan['planDigest'], '--apply', expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_missing_native_history_refuses_without_synthesizing_evidence(self):
        events_dir = self.module.ledger_events_dir(self.repo, self.manifest)
        for path in events_dir.glob('*.jsonl'):
            path.unlink()
        before = self.snapshot()
        self.preview(expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_late_source_ref_motion_refuses_the_reviewed_plan(self):
        plan = self.preview()
        self.git('update-ref', 'refs/heads/pr/withdrawn', self.original, self.rebuilt)
        before = self.snapshot()
        self.apply(plan, expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_native_publication_receipt_refuses_even_without_a_tracking_ref(self):
        self.module.append_ledger_event(self.repo, 'stack_pushed', {
            'stack': 'withdrawn', 'branch': 'pr/withdrawn', 'tip': self.rebuilt,
            'remote': 'origin',
        }, self.manifest)
        before = self.snapshot()
        self.preview(expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_remote_tracking_publication_refuses_even_without_a_push_receipt(self):
        self.git('update-ref', 'refs/remotes/origin/pr/withdrawn', self.rebuilt)
        before = self.snapshot()
        self.preview(expected=2)
        self.assertEqual(self.snapshot(), before)

    def test_native_merge_operation_evidence_refuses_withdrawal(self):
        kinds = ('github_pr_merge_started', 'github_pr_merge_prepared', 'github_pr_merge_receipt')
        for kind in kinds:
            with self.subTest(kind=kind):
                if kind != kinds[0]:
                    self.doCleanups()
                    self.setUp()
                self.module.append_ledger_event(self.repo, kind, {
                    'stack': 'withdrawn', 'operationId': 'fixture-operation',
                    'planDigest': 'fixture-plan', 'headSha': self.rebuilt,
                }, self.manifest)
                before = self.snapshot()
                self.preview(expected=2)
                self.assertEqual(self.snapshot(), before)

    def test_native_reference_hook_veto_preserves_checkout_and_can_resume(self):
        plan = self.preview()
        manifest = self.manifest.read_bytes()
        index = (self.repo / '.git/index').read_bytes()
        refs = self.git('show-ref')
        self.veto_file.touch()
        hook_lines = len(self.reference_log.read_text().splitlines())
        refused = self.apply(plan, expected=2)
        self.assertIn('recover-stack', refused.stderr)
        seen = self.reference_log.read_text().splitlines()[hook_lines:]
        self.assertIn('prepared auth=present', seen)
        self.assertEqual(self.manifest.read_bytes(), manifest)
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)
        self.assertEqual(self.git('show-ref'), refs)
        events = self.module.load_ledger_events(self.repo, self.manifest)
        self.assertFalse(any(e['type'] == 'manifest_saved' and
                             e['payload']['reason'] == 'stack_withdraw' for e in events))
        self.veto_file.unlink()
        self.apply(plan)
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_recovery_joins_the_native_manifest_lock(self):
        plan = self.preview()
        before_manifest = self.manifest.read_bytes()
        before_head = self.git('rev-parse', 'HEAD')
        with self.module.channel_mutation_lock(self.repo, self.manifest, 'fixture-writer'):
            process = subprocess.Popen(
                [sys.executable, '-m', 'syncwheel', *self.arguments(),
                 '--plan-digest', plan['planDigest'], '--apply'], cwd=self.repo,
                env={**os.environ, **self.env, 'PYTHONPATH': str(fixtures.CLI.parent)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                time.sleep(0.3)
                self.assertIsNone(process.poll())
                self.assertEqual(self.manifest.read_bytes(), before_manifest)
                self.assertEqual(self.git('rev-parse', 'HEAD'), before_head)
            except BaseException:
                process.kill()
                process.communicate(timeout=10)
                raise
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        self.assertEqual(self.git('status', '--porcelain'), b'')

    def test_sigkill_at_every_durable_boundary_resumes_exactly_once(self):
        # Each stage uses a fresh fixture so the original reviewed plan survives.
        stages = ('intent-saved', 'refs-updated', 'manifest-aligned', 'index-aligned',
                  'ledger-event-durable', 'ledger-saved')
        for stage in stages:
            with self.subTest(stage=stage):
                if stage != stages[0]:
                    self.doCleanups()
                    self.setUp()
                plan = self.preview()
                driver = r'''
import os, signal, sys
import syncwheel
import syncwheel_stack_recovery as recovery
stage = sys.argv.pop(1)
def checkpoint(current):
    if current == stage:
        os.kill(os.getpid(), signal.SIGKILL)
recovery.stack_recovery_checkpoint = checkpoint
raise SystemExit(syncwheel.main())
'''
                result = subprocess.run(
                    [sys.executable, '-c', driver, stage, *self.arguments(),
                     '--plan-digest', plan['planDigest'], '--apply'], cwd=self.repo,
                    env={**os.environ, **self.env, 'PYTHONPATH': str(fixtures.CLI.parent)},
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
                self.apply(plan)
                self.assertEqual(self.git('status', '--porcelain'), b'')
                self.assertEqual(self.git('rev-parse', 'HEAD^').decode().strip(), self.declared_head)
                events = self.module.load_ledger_events(self.repo, self.manifest)
                terminal = [e for e in events if e['type'] == 'manifest_saved'
                            and e['payload']['reason'] == 'stack_withdraw']
                self.assertEqual(len(terminal), 1)
                self.assertEqual(self.git('rev-parse', 'pr/withdrawn').decode().strip(), self.rebuilt)
                state = self.module.load_ledger_state(self.repo, self.manifest)
                checkpoint = self.module.ledger_checkpoint_path(self.repo, self.manifest)
                self.assertEqual(json.loads(checkpoint.read_text()), state)


if __name__ == '__main__':
    unittest.main()
