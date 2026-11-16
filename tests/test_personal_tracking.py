import contextlib
import importlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import test_metadata_preflight as fixtures


class PersonalTrackingTest(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.MetadataPreflightTest()
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.repo = self.case.repo
        self.module = self.case.module
        self.readme = self.repo / '.syncwheel' / 'manifests' / 'README.md'
        self.readme.parent.mkdir(exist_ok=True)
        self.readme.write_text('# Shared manifest documentation\n')
        self.case.ignore.write_text('*.cache\n')
        self.module.ensure_syncwheel_metadata_excluded(self.repo, 'git-tracked')
        self.case.git('add', '.gitignore', str(self.readme))
        self.case.git('commit', '-qm', 'Track shared ignore and manifest documentation')
        self.personal = self.case.create_personal_manifest('review', 'git-tracked')
        self.profile = self.repo / '.syncwheel' / 'profile.local.json'
        self.profile.write_text('{}\n')
        self.assertEqual(self.case.git('ls-files', '--', '.gitignore', str(self.readme)), (
            b'.gitignore\n.syncwheel/manifests/README.md\n'
        ))
        self.assertEqual(self.case.git('ls-files', '--', str(self.personal)), b'')
        self.case.git('check-ignore', '--quiet', str(self.personal))
        ledger = self.module.ledger_root(self.repo, self.personal)
        self.case.git('check-ignore', '--quiet', str(ledger / 'events' / '000001.jsonl'))
        self.assertEqual(self.case.git('status', '--porcelain'), b'')

    def cli(self, *args, cwd=None, extra_env=None):
        env = self.case.fixture._clean_env()
        env.pop('SYNCWHEEL_PERSONAL', None)
        env.pop('SYNCWHEEL_REPO', None)
        env.update(
            GIT_OPTIONAL_LOCKS='0',
            SYNCWHEEL_UPDATE_MODE='off',
            SYNCWHEEL_REPO_REGISTRY=str(self.case.fixture.temp_root / 'isolated-registry.json'),
        )
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(fixtures.guard_fixtures.MODULE_PATH), *args],
            cwd=cwd or self.repo, env=env, text=True, capture_output=True,
        )

    def snapshot(self):
        return {
            'ignore': self.case.ignore.read_bytes(),
            'readme': self.readme.read_bytes(),
            'shared': self.case.manifest_path.read_bytes(),
            'index': (self.repo / '.git' / 'index').read_bytes(),
            'staging': self.case.git('ls-files', '--stage'),
            'exclude': (self.repo / '.git' / 'info' / 'exclude').read_bytes(),
            'refs': self.case.git('for-each-ref'),
            'worktrees': self.case.git('worktree', 'list', '--porcelain'),
            'product': self.case.product.read_bytes(),
            'untracked': self.case.read_if_present(self.repo / 'untracked-product'),
            'hooks': self.case.fixture.hook_bundle_bytes(),
            'shared_ledger': self.module.load_ledger_events(self.repo, self.case.manifest_path),
        }

    def test_personal_local_only_does_not_modify_shared_tracking(self):
        before = self.snapshot()

        result = self.cli('repo', 'tracking', 'set', 'local-only', '--apply', '-p', 'review')

        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.snapshot()
        self.assertEqual([key for key in before if before[key] != after[key]], [])
        self.assertEqual(json.loads(self.personal.read_text())['syncwheel_tracking'], 'local-only')
        self.assertEqual(self.module.load_ledger_events(self.repo, self.personal)[-1]['payload']['reason'],
                         'repo_tracking_set')

    def assert_private_update(self, requested, *selection, cwd=None, extra_env=None, path=None):
        path = path or self.personal
        before = self.snapshot()
        original = json.loads(path.read_text())
        result = self.cli('repo', 'tracking', 'set', requested, '--apply', '--json',
                          *selection, cwd=cwd, extra_env=extra_env)

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['tracking_scope'], 'personal')
        self.assertEqual(report['syncwheel_tracking'], requested)
        self.assertFalse(report['manifest_tracked'])
        self.assertEqual(Path(report['manifest_path']).resolve(), path)
        shared_actions = ('git add', 'git rm', 'managed .gitignore', 'info/exclude')
        for action in report['actions']:
            self.assertFalse(any(item in action for item in shared_actions), action)
        current = json.loads(path.read_text())
        self.assertEqual(current['syncwheel_tracking'], requested)
        self.assertEqual(current['stacks'], original['stacks'])
        self.assertEqual(current['integration'], original['integration'])
        events = self.module.load_ledger_events(cwd or self.repo, path)
        self.assertEqual(events[-1]['payload']['reason'], 'repo_tracking_set')
        self.assertEqual(self.snapshot(), before)

    def assert_refusal_preserves_state(self, detail, *selection):
        before = self.snapshot()
        personal = self.personal.read_bytes()
        ledger = self.module.load_ledger_events(self.repo, self.personal)
        result = self.cli('repo', 'tracking', 'set', 'local-only', '--apply', *selection)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertRegex(result.stderr, detail)
        self.assertEqual(self.personal.read_bytes(), personal)
        self.assertEqual(self.module.load_ledger_events(self.repo, self.personal), ledger)
        self.assertEqual(self.snapshot(), before)

    def test_both_directions_with_explicit_manifest_and_dirty_primary(self):
        self.case.dirty_product()
        self.assert_private_update('local-only', '-M', str(self.personal))
        self.assert_private_update(
            'git-tracked', '-M', str(self.personal), extra_env={'GIT_OPTIONAL_LOCKS': '1'},
        )

    def test_personal_selection_via_profile_environment_and_alias(self):
        registry = self.case.fixture.temp_root / 'repo-registry.json'
        registry.write_text(json.dumps({
            'review-alias': {'path': str(self.repo), 'manifest': str(self.personal)},
        }))
        profile = json.loads(self.profile.read_text())

        self.assert_private_update('local-only', extra_env={'SYNCWHEEL_PERSONAL': 'review'})
        self.profile.write_text(json.dumps({**profile, 'personal': 'review'}))
        self.assert_private_update('git-tracked')
        self.profile.write_text(json.dumps(profile))
        self.assert_private_update('local-only', '--repo', 'review-alias',
                                   extra_env={'SYNCWHEEL_REPO_REGISTRY': str(registry)})

    def test_linked_worktree_keeps_both_indexes_and_checked_out_metadata(self):
        lane = self.case.fixture.temp_root / 'linked-review'
        self.case.git('worktree', 'add', '--detach', '-q', str(lane))
        lane_path = self.module.personal_manifest_path(lane, 'review')
        lane_path.write_bytes(self.personal.read_bytes())
        lane_index = Path(self.case.git(
            '-C', str(lane), 'rev-parse', '--path-format=absolute', '--git-path', 'index',
        ).decode().strip())
        files = (lane_index, lane / '.gitignore', lane / self.readme.relative_to(self.repo),
                 lane / '.syncwheel' / 'manifest.json', self.personal)
        before = [path.read_bytes() for path in files]
        self.case.dirty_product()

        self.assert_private_update('local-only', '-p', 'review', cwd=lane, path=lane_path)

        self.assertEqual([path.read_bytes() for path in files], before)
        self.assert_private_update('local-only', '-M', str(self.personal), cwd=lane)
        self.assertEqual([path.read_bytes() for path in files[:-1]], before[:-1])

    def test_personal_tracking_preview_has_no_shared_git_actions(self):
        before = self.snapshot()
        personal = self.personal.read_bytes()

        result = self.cli('repo', 'tracking', 'set', 'local-only', '-p', 'review')

        self.assertEqual(result.returncode, 0, result.stderr)
        for action in ('git add', 'git rm', 'managed .gitignore', 'info/exclude'):
            self.assertNotIn(action, result.stdout)
        status = self.cli('repo', 'tracking', 'status', '--json', '-p', 'review')
        self.assertEqual(status.returncode, 0, status.stderr)
        report = json.loads(status.stdout)
        self.assertEqual(report['tracking_scope'], 'personal')
        self.assertEqual(report['actions'], [])
        self.assertEqual(self.personal.read_bytes(), personal)
        self.assertEqual(self.module.load_ledger_events(self.repo, self.personal), [])
        self.assertEqual(self.snapshot(), before)

    def test_tracked_personal_manifest_is_refused_on_clean_primary(self):
        self.case.git('add', '-f', str(self.personal))
        self.case.git('commit', '-qm', 'Track a personal manifest for the refusal fixture')

        self.assert_refusal_preserves_state('untracked', '-p', 'review')

    def test_unignored_personal_ledger_is_refused_before_write(self):
        self.case.ignore.write_text(self.case.ignore.read_text().replace(
            '.syncwheel/manifests/*.local-ledger/\n', '',
        ))
        self.case.git('add', '.gitignore')
        self.case.git('commit', '-qm', 'Remove personal ledger exclusion')

        self.assert_refusal_preserves_state('ignored', '-p', 'review')

    def test_unignored_personal_manifest_is_refused_before_write(self):
        self.case.ignore.write_text(self.case.ignore.read_text().replace(
            '.syncwheel/manifests/*.local.json\n', '',
        ))
        self.case.git('add', '.gitignore')
        self.case.git('commit', '-qm', 'Remove personal manifest exclusion')

        self.assert_refusal_preserves_state('ignored', '-p', 'review')

    def test_personal_symlink_to_shared_manifest_is_refused_on_clean_primary(self):
        self.personal.unlink()
        self.personal.symlink_to(self.case.manifest_path)
        self.assertEqual(self.case.git('status', '--porcelain'), b'')

        for selection in (('-p', 'review'), ('-M', str(self.personal))):
            with self.subTest(selection=selection):
                self.assert_refusal_preserves_state('symbolic link|symlink', *selection)

        self.assertTrue(self.personal.is_symlink())
        self.assertEqual(self.personal.resolve(), self.case.manifest_path)

    def test_personal_tracking_refuses_staged_product_and_conflicts(self):
        self.case.dirty_product()
        self.case.git('add', 'seed')
        self.assert_refusal_preserves_state('unrelated staged paths', '-p', 'review')

        oid = self.case.git('rev-parse', 'HEAD:seed').decode().strip()
        self.case.git('update-index', '--force-remove', 'seed')
        subprocess.run(
            ['git', 'update-index', '--index-info'], cwd=self.repo, check=True,
            input=f'100644 {oid} 1\tseed\n100644 {oid} 2\tseed\n100644 {oid} 3\tseed\n'.encode(),
        )
        self.assert_refusal_preserves_state('index conflicts', '-p', 'review')

    def test_pending_shared_recovery_is_not_run_on_clean_or_dirty_primary(self):
        shared_path = self.case.manifest_path
        manifest, _ = self.module.load_manifest(self.repo, shared_path)
        branch = manifest['integration']['branch']
        tip = self.case.git('rev-parse', branch).decode().strip()
        manifest['defaults']['replay_mode'] = 'auto'
        store = importlib.import_module('syncwheel_manifest_store')

        def interrupt(stage):
            if stage == 'intent_saved':
                raise RuntimeError('simulated process interruption after native intent')

        with (
            self.module.manifest_write_transaction(self.repo, shared_path),
            mock.patch.object(store, 'control_manifest_io_checkpoint', side_effect=interrupt),
            self.assertRaisesRegex(RuntimeError, 'simulated process interruption'),
        ):
            self.module.restore_control_manifest_after_integration_rebuild(
                self.repo, shared_path, manifest, tip, 'control',
                reason='Exercise native interrupted control persistence',
                command='syncwheel int rebuild',
            )

        events = self.module.load_ledger_events(self.repo, shared_path)
        self.assertEqual(len(self.module.pending_control_manifest_intents(events)), 1)
        self.assertEqual(self.case.git('status', '--porcelain'), b'')
        for dirty in (False, True):
            with self.subTest(dirty_primary=dirty):
                if dirty:
                    self.case.dirty_product()
                self.assert_refusal_preserves_state('control manifest recovery is pending',
                                                    '-p', 'review')

    def test_profile_selection_race_cannot_update_shared_tracking(self):
        original_profile = self.profile.read_bytes()
        self.profile.write_text(json.dumps({**json.loads(original_profile), 'personal': 'review'}))
        selected = self.module.tracking_set_selection
        original_personal = self.personal.read_bytes()
        expected_snapshots = []

        def change_profile_after_first_selection(args):
            selection = selected(args)
            if not expected_snapshots:
                self.assertEqual(selection[0], 'personal')
                self.profile.write_bytes(original_profile)
                expected_snapshots.append(self.snapshot())
            return selection

        argv = [
            'syncwheel', 'repo', 'tracking', 'set', 'local-only', '--apply',
            '--repo', str(self.repo),
        ]
        with (
            mock.patch.object(sys, 'argv', argv),
            mock.patch.object(self.module, 'tracking_set_selection',
                              side_effect=change_profile_after_first_selection),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = self.module.main()

        self.assertEqual(len(expected_snapshots), 1)
        self.assertIn(result, (0, 2))
        self.assertEqual(self.snapshot(), expected_snapshots[0])
        if result:
            self.assertEqual(self.personal.read_bytes(), original_personal)
        else:
            self.assertEqual(json.loads(self.personal.read_text())['syncwheel_tracking'], 'local-only')

    def test_shared_tracking_race_cannot_bypass_pending_adoption(self):
        adopt = ('stack', 'set', 'feature', '--base', 'HEAD', '--adopt-tip', 'HEAD')
        preview = self.cli(*adopt)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        plan = json.loads(preview.stdout)
        args = self.module.build_parser().parse_args([
            *adopt, '--plan-digest', plan['planDigest'], '--apply', '--repo', str(self.repo),
        ])
        adoption = importlib.import_module('syncwheel_stack_adoption')

        def interrupt(stage):
            if stage == 'prepared':
                raise RuntimeError('simulated process interruption after native adoption intent')

        with (
            self.module.manifest_write_transaction(self.repo, self.case.manifest_path),
            mock.patch.object(adoption, 'adoption_checkpoint', side_effect=interrupt),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, 'simulated process interruption'),
        ):
            self.module.command_stack_set(args)

        events = self.module.load_ledger_events(self.repo, self.case.manifest_path)
        self.assertEqual(adoption.pending_adoptions(events), [plan])
        self.assertEqual(self.case.git('status', '--porcelain'), b'')
        selected = self.module.tracking_set_selection
        expected_snapshots = []

        def change_profile_after_first_selection(parsed):
            selection = selected(parsed)
            if not expected_snapshots:
                self.assertEqual(selection[0], 'shared')
                self.profile.write_text(json.dumps({'personal': 'review'}))
                expected_snapshots.append(self.snapshot())
            return selection

        personal_bytes = self.personal.read_bytes()
        stderr = io.StringIO()
        argv = [
            'syncwheel', 'repo', 'tracking', 'set', 'local-only', '--apply',
            '--repo', str(self.repo),
        ]
        with (
            mock.patch.object(sys, 'argv', argv),
            mock.patch.object(self.module, 'tracking_set_selection',
                              side_effect=change_profile_after_first_selection),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(stderr),
        ):
            result = self.module.main()

        self.assertEqual(len(expected_snapshots), 1)
        after = self.snapshot()
        changed = [key for key in after if after[key] != expected_snapshots[0][key]]
        self.assertEqual(changed, [], stderr.getvalue())
        self.assertEqual(result, 2, stderr.getvalue())
        self.assertIn('interrupted stack adoption', stderr.getvalue())
        self.assertEqual(self.personal.read_bytes(), personal_bytes)
        self.assertEqual(self.module.load_ledger_events(self.repo, self.personal), [])
