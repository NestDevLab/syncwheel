"""Legacy publication preserves unrelated authoring and interrupted cleanup.

These tests start with already materialized feature branches. Authors commit
normally in separate worktrees during fixture setup; ``stack set`` declares
those commits and ``stack push`` publishes the existing refs. This is not a
claim that set/push reconstructs a branch from arbitrary external commits, or
that publishing two branches validates their combined integration candidate.
"""

import json
import os
import shutil
import signal
import unittest
from pathlib import Path
from unittest import mock

import test_syncwheel as fixtures


class IndependentPublicationTest(unittest.TestCase):
    foreign_owner = 'fixture-author-c'

    def setUp(self):
        environment = mock.patch.dict(os.environ, {
            'SYNCWHEEL_UPDATE_MODE': 'off',
            'SYNCWHEEL_LANE_OWNER': 'fixture-publisher',
            'GIT_ALLOW_PROTOCOL': 'file',
            'GIT_TERMINAL_PROMPT': '0',
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = fixtures.SyncwheelFixtureTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repo = self.fixture.repo
        self.git = self.fixture.git

        # Run the identical modules as a standalone bundle, with no installation
        # Git remote. Legacy startup refreshes its update cache even in off mode;
        # a source checkout would therefore fetch the developer's tool remote.
        bundle = self.fixture.tmp / 'tool-bundle'
        if getattr(self, 'runtime_cli', None) is None:
            self.fixture.copy_syncwheel_modules(bundle)
        else:
            bundle.mkdir()
        shutil.copy2(fixtures.REPO_ROOT / 'VERSION', bundle / 'VERSION')
        cli_path = mock.patch.object(fixtures, 'CLI',
                                     getattr(self, 'runtime_cli', None) or bundle / 'syncwheel.py')
        cli_path.start()
        self.addCleanup(cli_path.stop)
        local_update_state = mock.patch.dict(os.environ, {
            'SYNCWHEEL_REPO': str(self.repo),
            'SYNCWHEEL_PERSONAL': '',
            'SYNCWHEEL_UPDATE_STATE_PATH': str(self.fixture.tmp / 'update-state.json'),
            'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.fixture.tmp / 'update-settings.json'),
            'SYNCWHEEL_REMOTE_VERSION_URL': (bundle / 'VERSION').as_uri(),
        })
        local_update_state.start()
        self.addCleanup(local_update_state.stop)
        self.module = getattr(self, 'runtime_module', None) or self.fixture.load_syncwheel_module()
        expected_kind = getattr(self, 'runtime_install_kind', 'script')
        self.assertEqual(self.module.detect_syncwheel_install()['kind'], expected_kind)
        self.manifest_path = self.repo / '.syncwheel/manifest.json'
        self.origin = self.fixture.tmp / 'origin.git'
        self.git('init', '--bare', '-q', str(self.origin))
        self.git('remote', 'add', 'origin', str(self.origin))
        self.git('switch', '-qc', 'main-integration')

        manifest = {
            'version': 1,
            'repository_mode': 'delivery',
            'defaults': {
                'canonical_remote': 'origin',
                'publication_remote': 'origin',
                'base_branch': 'main',
                'base_ref': 'origin/main',
                'integration_membership': 'legacy',
            },
            'integration': {
                'branch': 'main-integration',
                'base': 'origin/main',
                'strategy': 'cherry-pick',
                'stacks': ['feature-a', 'feature-b'],
            },
            'stacks': [{
                'id': stack,
                'branch': f'pr/{stack}',
                'base': 'origin/main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'main-integration',
                'state': 'published',
                'commits': [],
            } for stack in ('feature-a', 'feature-b', 'feature-c')],
        }
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        (self.repo / '.gitignore').write_text('/cache/\n')
        self.cli('repo', 'tracking', 'set', 'git-tracked', '--apply')
        if self.git('diff', '--cached', '--name-only'):
            self.git('commit', '-qm', 'test: configure tracked legacy publication')

        # All fixture bootstrapping precedes the preservation snapshots. The
        # published A/B refs start at the base and advance independently below.
        self.base = self.git('rev-parse', 'HEAD')
        self.git('branch', '-f', 'main', self.base)
        for stack in ('feature-a', 'feature-b', 'feature-c'):
            self.git('branch', '-f', f'pr/{stack}', self.base)
        self.git('--git-dir=' + str(self.origin), 'fetch', '-q', str(self.repo),
                 'main:refs/heads/main', 'pr/feature-a:refs/heads/pr/feature-a',
                 'pr/feature-b:refs/heads/pr/feature-b', 'pr/feature-c:refs/heads/pr/feature-c')
        self.git('--git-dir=' + str(self.origin), 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.git('fetch', '-q', 'origin')
        self.tips = {}
        self.author_paths = {}
        for stack in ('feature-a', 'feature-b'):
            path = self.fixture.tmp / f'author-{stack}'
            self.git('worktree', 'add', '-q', str(path), f'pr/{stack}')
            (path / f'{stack}.txt').write_text(f'{stack}\n')
            self.git('-C', str(path), 'add', f'{stack}.txt')
            self.git('-C', str(path), 'commit', '-qm', f'feat: independent {stack}')
            self.tips[stack] = self.git('-C', str(path), 'rev-parse', 'HEAD')
            self.author_paths[stack] = path
            self.assertEqual(self.git('rev-parse', self.tips[stack] + '^'), self.base)

        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.git('ls-files', '.syncwheel/manifest.json'), '.syncwheel/manifest.json')
        recorded = json.loads(self.git('show', 'HEAD:.syncwheel/manifest.json'))
        self.assertEqual(recorded['version'], 1)
        self.assertEqual(recorded['repository_mode'], 'delivery')
        self.assertEqual(recorded['syncwheel_tracking'], 'git-tracked')
        self.assertNotIn('coordination', recorded)
        self.remote_baseline = {
            'refs/heads/main': self.base,
            'refs/heads/pr/feature-a': self.base,
            'refs/heads/pr/feature-b': self.base,
            'refs/heads/pr/feature-c': self.base,
        }
        self.assertEqual(self.remote_tips(), self.remote_baseline)

    def cli(self, *args, expected=0, owner='fixture-publisher'):
        return self.fixture.run_cli(
            *args, expected=expected,
            extra_env={'SYNCWHEEL_UPDATE_MODE': 'off', 'SYNCWHEEL_LANE_OWNER': owner},
        )

    def open_foreign_lane(self):
        opened = self.cli('worktree', 'open', 'author-c', '--into', 'feature-c', '--json',
                          owner=self.foreign_owner)
        self.foreign_lane = json.loads(opened.stdout)['lane']
        self.foreign_path = Path(self.foreign_lane['path'])
        self.assertEqual(self.foreign_record()['owner'], self.foreign_owner)
        self.assertEqual(self.foreign_record()['target'], 'feature-c')

    def dirty_foreign_lane(self):
        path = self.foreign_path
        (path / 'alpha.txt').write_bytes(b'staged foreign bytes\x00\n')
        self.git('-C', str(path), 'add', 'alpha.txt')
        (path / 'alpha.txt').write_bytes(b'unstaged foreign bytes\x00\n')
        (path / 'beta.txt').write_text('another unstaged change\n')
        (path / 'draft.bin').write_bytes(b'untracked foreign bytes\x00\xff')
        cache = path / 'cache'
        cache.mkdir()
        (cache / 'retained.bin').write_bytes(b'ignored validation bytes\x00\xff')
        (cache / 'source-link').symlink_to('../alpha.txt')
        self.assertEqual(
            self.git('-C', str(path), 'check-ignore', 'cache/retained.bin', 'cache/source-link'),
            'cache/retained.bin\ncache/source-link',
        )

    def foreign_record(self):
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        return next(lane for lane in registry['lanes'] if lane['id'] == self.foreign_lane['id'])

    def foreign_snapshot(self):
        path = self.foreign_path
        files = {}
        for item in path.rglob('*'):
            relative = str(item.relative_to(path))
            if item.is_symlink():
                files[relative] = ('symlink', os.readlink(item))
            elif item.is_file():
                files[relative] = ('file', item.read_bytes())
        return {
            'files': files,
            'head': self.git('-C', str(path), 'rev-parse', 'HEAD'),
            'attached_head': self.git('-C', str(path), 'symbolic-ref', 'HEAD'),
            'branch_tip': self.git('rev-parse', self.foreign_lane['branch']),
            'index_entries': self.git('-C', str(path), 'ls-files', '--stage', '-z'),
            'staged_diff': self.git('-C', str(path), 'diff', '--cached', '--binary'),
            'status': self.git('-C', str(path), 'status', '--porcelain', '--untracked-files=all'),
            'registry_record': self.foreign_record(),
            'worktree_record': self.module.find_worktree_record_for_branch(
                self.repo, self.foreign_lane['branch'],
            ),
        }

    def remote_tips(self):
        output = self.git('--git-dir=' + str(self.origin), 'for-each-ref',
                          '--format=%(refname) %(objectname)', 'refs/heads')
        return dict(line.split() for line in output.splitlines())

    def declare_and_push(self, stack, snapshot):
        # The source ref already exists at this exact author commit. No hidden
        # ref updates or rebuilds occur between stack set and stack push.
        self.assertEqual(self.git('rev-parse', f'pr/{stack}'), self.tips[stack])
        self.cli('stack', 'set', stack, self.tips[stack],
                 '--preserve-worktrees', 'Preserve independent authoring')
        self.assertEqual(self.foreign_snapshot(), snapshot)
        self.cli('stack', 'push', stack, '--remote', 'origin')
        self.assertEqual(self.foreign_snapshot(), snapshot)
        self.assertEqual(self.remote_tips()[f'refs/heads/pr/{stack}'], self.tips[stack])

    def assert_independent_publication(self):
        snapshot = self.foreign_snapshot()
        expected_remote = dict(self.remote_baseline)
        for stack in ('feature-a', 'feature-b'):
            with self.subTest(stack=stack):
                self.declare_and_push(stack, snapshot)
                expected_remote[f'refs/heads/pr/{stack}'] = self.tips[stack]
                self.assertEqual(self.remote_tips(), expected_remote)
                paths = self.git('--git-dir=' + str(self.origin), 'ls-tree', '-r', '--name-only',
                                 f'refs/heads/pr/{stack}').splitlines()
                self.assertIn(f'{stack}.txt', paths)
                other = 'feature-b' if stack == 'feature-a' else 'feature-a'
                self.assertNotIn(f'{other}.txt', paths)
                self.assertIn('.syncwheel/manifest.json', paths)

    def test_v1_tracked_set_and_push_preserve_dirty_foreign_worktree(self):
        self.open_foreign_lane()
        self.dirty_foreign_lane()
        self.git('worktree', 'lock', '--reason', 'Retain foreign authoring', str(self.foreign_path))

        self.assert_independent_publication()

    def test_v1_tracked_publication_preserves_foreign_cleanup_after_sigkill(self):
        self.open_foreign_lane()
        # Expire only this fixture lease so gc reaches a real cleanup intent.
        # The pending-cleanup state below must come from the killed CLI process.
        registry, _ = self.module.load_governed_worktree_registry(self.repo)
        record = next(lane for lane in registry['lanes'] if lane['id'] == self.foreign_lane['id'])
        record['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        self.module.save_governed_worktree_registry(self.repo, registry)
        result = self.fixture.run_cli_until_cleanup_sigkill(
            'after_cleanup_intent', 'gc', '--apply', '--no-fetch', '--json',
        )
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertEqual(self.foreign_record()['state'], 'captured_pending_cleanup')
        self.assertEqual(self.foreign_record()['pending_reason'], 'reaping')
        intents = [event for event in self.module.load_ledger_events(self.repo)
                   if event['type'] == 'governed_worktree_cleanup_intent'
                   and event['payload']['lane'] == self.foreign_lane['id']]
        self.assertEqual(len(intents), 1)
        self.assertTrue(self.module.find_worktree_record_for_branch(
            self.repo, self.foreign_lane['branch'],
        )['locked'])

        # The author resumes editing after cleanup died, before A/B publish.
        # Preserve the native cleanup lock and durable intent without recovery.
        self.dirty_foreign_lane()
        self.assert_independent_publication()
        self.assertEqual(self.foreign_record()['pending_reason'], 'reaping')
        cleanup_events = [event for event in self.module.load_ledger_events(self.repo)
                          if (event['type'].startswith('governed_worktree_cleanup')
                              or event['type'] in {'governed_worktree_reaped', 'governed_worktree_released'})
                          and event.get('payload', {}).get('lane') == self.foreign_lane['id']]
        self.assertEqual(cleanup_events, intents)

    def test_foreign_remote_update_refuses_forced_push_without_touching_foreign_worktree(self):
        self.open_foreign_lane()
        self.dirty_foreign_lane()
        self.git('worktree', 'lock', '--reason', 'Retain foreign authoring', str(self.foreign_path))
        snapshot = self.foreign_snapshot()
        self.declare_and_push('feature-a', snapshot)

        # A separate clone publishes between two attempts. Fetch deliberately
        # moves the tracking ref: a plain implicit lease alone would now pass.
        foreign_tip = self.fixture.foreign_commit_on_remote_stack(self.origin)
        self.git('fetch', '-q', 'origin')
        remote_before = self.remote_tips()
        refs_before = self.git('show-ref', '--heads')
        manifest_before = self.manifest_path.read_bytes()
        result = self.cli('stack', 'push', 'feature-a', '--remote', 'origin',
                          '--force-with-lease', expected=2)

        self.assertIn('stack does not own', result.stderr)
        self.assertIn(foreign_tip[:12], result.stderr)
        self.assertEqual(self.remote_tips(), remote_before)
        self.assertEqual(self.git('show-ref', '--heads'), refs_before)
        self.assertEqual(self.manifest_path.read_bytes(), manifest_before)
        self.assertEqual(self.foreign_snapshot(), snapshot)


if __name__ == '__main__':
    unittest.main()
