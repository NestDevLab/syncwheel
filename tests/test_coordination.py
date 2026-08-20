import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / 'scripts' / 'syncwheel.py'


class ActiveActiveCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='syncwheel-coordination-test-'))
        self.bin_dir = self.tmp / 'bin'
        self.bin_dir.mkdir()
        (self.bin_dir / 'syncwheel').symlink_to(CLI)
        self.settings = self.tmp / 'settings.json'
        self.registry = self.tmp / 'repos.json'
        self.environment = {
            'SYNCWHEEL_UPDATE_MODE': 'off',
            'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.settings),
            'SYNCWHEEL_REPO_REGISTRY': str(self.registry),
            'PATH': f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        self.environment_patch = mock.patch.dict(os.environ, self.environment, clear=False)
        self.environment_patch.start()

    def tearDown(self):
        self.environment_patch.stop()
        shutil.rmtree(self.tmp)

    def git(self, repo, *args, expected=0):
        result = subprocess.run(
            ['git', *args],
            cwd=repo,
            text=True,
            capture_output=True,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"git {args} expected {expected}, got {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def run_cli(self, repo, *args, expected=0):
        env = dict(os.environ)
        env.update(self.environment)
        result = subprocess.run(
            ['python3', str(CLI), *args],
            cwd=repo,
            text=True,
            capture_output=True,
            env=env,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"syncwheel {args} expected {expected}, got {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def load_module(self):
        spec = importlib.util.spec_from_file_location('syncwheel_coordination_under_test', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def create_remote(self, name='origin'):
        origin = self.tmp / f'{name}.git'
        seed = self.tmp / f'{name}-seed'
        subprocess.run(['git', 'init', '--bare', str(origin)], check=True, capture_output=True, text=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(seed)], check=True)
        self.git(seed, 'config', 'user.name', 'Syncwheel Fixture')
        self.git(seed, 'config', 'user.email', 'syncwheel-fixture@example.com')
        (seed / 'README.md').write_text('seed\n')
        self.git(seed, 'add', 'README.md')
        self.git(seed, 'commit', '-q', '-m', 'chore: seed')
        self.git(seed, 'remote', 'add', 'origin', str(origin))
        self.git(seed, 'push', '-u', 'origin', 'main')
        subprocess.run(
            ['git', '--git-dir', str(origin), 'symbolic-ref', 'HEAD', 'refs/heads/main'],
            check=True,
            capture_output=True,
            text=True,
        )
        return origin

    def clone(self, origin, name):
        path = self.tmp / name
        subprocess.run(['git', 'clone', '-q', str(origin), str(path)], check=True)
        self.git(path, 'config', 'user.name', f'Syncwheel {name}')
        self.git(path, 'config', 'user.email', f'syncwheel-{name}@example.com')
        return path

    def init_coordinated(self, repo, integration='integration/shared', integration_membership='legacy'):
        self.git(repo, 'branch', integration, 'origin/main')
        self.run_cli(
            repo,
            'init',
            '--syncwheel-tracking',
            'git-tracked',
            '--publication-remote',
            'origin',
            '--integration-branch',
            integration,
        )
        self.disable_fixture_hooks(repo)
        return self.set_integration_membership(repo, integration_membership)

    def disable_fixture_hooks(self, repo):
        self.run_cli(
            repo,
            'hooks', 'remove', '--disable',
            '--reason', 'coordination fixture uses raw primary branch setup', '--apply',
        )

    def set_integration_membership(self, repo, integration_membership):
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        # Coordination fixtures model pre-existing repositories. Keep their
        # historical optional integration behavior explicit, while dedicated
        # initialization coverage exercises the required default.
        manifest['defaults']['integration_membership'] = integration_membership
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        return manifest

    def remote_state(self, origin, coordination_id='default'):
        ref = f'refs/heads/syncwheel/state/{coordination_id}'
        tip = subprocess.run(
            ['git', '--git-dir', str(origin), 'rev-parse', ref],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        payload = subprocess.run(
            ['git', '--git-dir', str(origin), 'show', f'{ref}:.syncwheel/coordination-state.json'],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        return tip, json.loads(payload)

    def commit_on_branch(self, repo, branch, filename):
        previous = self.git(repo, 'branch', '--show-current').stdout.strip()
        self.git(repo, 'switch', '-q', '-c', branch, 'origin/main')
        (repo / filename).write_text(f'{branch}\n')
        self.git(repo, 'add', filename)
        self.git(repo, 'commit', '-q', '-m', f'feat: {branch}')
        sha = self.git(repo, 'rev-parse', 'HEAD').stdout.strip()
        self.git(repo, 'switch', '-q', previous)
        return sha

    def test_new_git_tracked_init_defaults_to_v2_and_local_only_is_disabled(self):
        origin = self.create_remote()
        tracked = self.clone(origin, 'tracked')
        manifest = self.init_coordinated(tracked, integration_membership='required')

        self.assertEqual(manifest['version'], 2)
        self.assertEqual(manifest['defaults']['integration_membership'], 'required')
        self.assertEqual(manifest['coordination']['mode'], 'active-active')
        self.assertEqual(manifest['coordination']['remote'], 'origin')
        self.assertEqual(manifest['coordination']['state_branch'], 'syncwheel/state/default')
        self.assertEqual(self.git(tracked, 'branch', '--show-current').stdout.strip(), 'integration/shared')
        forced = self.run_cli(tracked, 'int', 'push', '--force-with-lease', expected=2)
        self.assertIn('manages atomic and exact lease flags itself', forced.stderr)
        non_publish_merge = self.run_cli(
            tracked,
            'reconcile',
            '--apply',
            '--push',
            '--accept-merge',
            expected=2,
        )
        self.assertIn('only available through publish --accept-merge', non_publish_merge.stderr)

        local_only = self.clone(origin, 'local-only')
        self.run_cli(local_only, 'init', '--syncwheel-tracking', 'local-only')
        local_manifest = json.loads((local_only / '.syncwheel' / 'manifest.json').read_text())
        self.assertEqual(local_manifest['version'], 2)
        self.assertEqual(local_manifest['coordination']['mode'], 'disabled')
        self.assertEqual(local_manifest['coordination']['id'], 'default')
        self.assertEqual(local_manifest['coordination']['state_branch'], 'syncwheel/state/default')
        opt_in = self.run_cli(local_only, 'coordination', 'init', '--apply', expected=2)
        self.assertIn('opt-in', opt_in.stderr)
        self.run_cli(local_only, 'coordination', 'init', '--remote', 'origin', '--apply')
        opt_in_manifest = json.loads((local_only / '.syncwheel' / 'manifest.json').read_text())
        self.assertEqual(opt_in_manifest['coordination']['mode'], 'active-active')

        legacy = self.clone(origin, 'legacy')
        self.run_cli(legacy, 'init')
        legacy_manifest = json.loads((legacy / '.syncwheel' / 'manifest.json').read_text())
        self.assertEqual(legacy_manifest['version'], 1)

        standalone = self.tmp / 'standalone'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(standalone)], check=True)
        self.git(standalone, 'config', 'user.name', 'Standalone')
        self.git(standalone, 'config', 'user.email', 'standalone@example.com')
        self.git(standalone, 'commit', '--allow-empty', '-qm', 'chore: seed')
        failure = self.run_cli(standalone, 'init', '--syncwheel-tracking', 'git-tracked', expected=2)
        self.assertIn('configured publication remote', failure.stderr)

    def test_v1_push_behavior_remains_uncoordinated(self):
        origin = self.create_remote()
        legacy = self.clone(origin, 'legacy')
        self.git(legacy, 'branch', 'integration/legacy', 'origin/main')
        self.run_cli(legacy, 'init', '--integration-branch', 'integration/legacy')

        self.run_cli(legacy, 'int', 'push', '--remote', 'origin')
        self.git(legacy, 'ls-remote', '--exit-code', 'origin', 'refs/heads/integration/legacy')
        state = subprocess.run(
            ['git', '--git-dir', str(origin), 'show-ref', '--verify', '--quiet', 'refs/heads/syncwheel/state/default'],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(state.returncode, 0)

    def test_state_is_append_only_and_safe_for_public_transport(self):
        origin = self.create_remote()
        first = self.clone(origin, 'first')
        self.init_coordinated(first)
        self.run_cli(first, 'int', 'push')
        first_tip, first_state = self.remote_state(origin)

        self.assertIsNone(first_state['parent_state'])
        self.assertEqual(first_state['publication_scope'], 'integration')
        self.assertEqual(first_state['projection_status'], 'partial')
        self.assertIn('installation_id', first_state)
        serialized = json.dumps(first_state, sort_keys=True)
        self.assertNotIn(str(first), serialized)
        self.assertNotIn('syncwheel-first@example.com', serialized)
        self.assertNotIn('syncwheel_worktree_root', serialized)
        self.assertNotIn('syncwheel_tracking', first_state['manifest'])
        identity = subprocess.run(
            ['git', '--git-dir', str(origin), 'show', '-s', '--format=%an <%ae>|%cn <%ce>', first_tip],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(
            identity,
            'Syncwheel Coordination <coordination@syncwheel.invalid>|'
            'Syncwheel Coordination <coordination@syncwheel.invalid>',
        )

        second = self.clone(origin, 'second')
        self.git(second, 'fetch', 'origin', 'integration/shared:refs/remotes/origin/integration/shared')
        self.git(second, 'branch', 'integration/shared', 'origin/integration/shared')
        self.run_cli(
            second,
            'init',
            '--syncwheel-tracking',
            'git-tracked',
            '--publication-remote',
            'origin',
            '--integration-branch',
            'integration/shared',
        )
        self.disable_fixture_hooks(second)
        self.run_cli(second, 'int', 'push')
        second_tip, second_state = self.remote_state(origin)

        self.assertNotEqual(first_tip, second_tip)
        self.assertEqual(second_state['parent_state'], first_tip)
        handoff = json.loads(self.run_cli(second, 'handoff', '--json').stdout)
        self.assertEqual(handoff['coordination']['state_status'], 'published')
        self.assertEqual(handoff['coordination']['manifest_relation'], 'aligned')

    def test_public_snapshot_omits_local_remote_aliases(self):
        module = self.load_module()
        manifest = {
            'version': 2,
            'syncwheel_tracking': 'git-tracked',
            'defaults': {
                'canonical_remote': 'alice-laptop',
                'publication_remote': 'alice-laptop',
                'base_branch': 'main',
                'base_ref': 'alice-laptop/main',
            },
            'integration': {
                'branch': 'integration/shared',
                'base': 'alice-laptop/main',
                'strategy': 'cherry-pick',
                'stacks': [],
            },
            'coordination': {
                'mode': 'active-active',
                'id': 'shared',
                'remote': 'alice-laptop',
                'state_branch': 'syncwheel/state/shared',
                'gc': {'worktree_grace_days': 7, 'backup_retention_days': 30, 'backup_keep': 2},
            },
            'stacks': [{
                'id': 'feature-a',
                'branch': 'pr/feature-a',
                'base': 'alice-laptop/main',
                'target_remote': 'alice-laptop',
                'target_branch': 'main',
                'integration_branch': 'integration/shared',
                'commits': ['feature-a'],
            }],
        }

        snapshot = module.coordination_manifest_snapshot(manifest)
        serialized = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn('alice-laptop', serialized)
        self.assertEqual(
            snapshot['defaults'],
            {
                'base_branch': 'main',
                'base_ref': {
                    'kind': 'remote-ref',
                    'role': 'canonical',
                    'ref': 'refs/heads/main',
                },
            },
        )
        self.assertEqual(
            snapshot['integration']['base'],
            {
                'kind': 'remote-ref',
                'role': 'canonical',
                'ref': 'refs/heads/main',
            },
        )
        self.assertNotIn('target_remote', snapshot['stacks'][0])
        self.assertNotIn('remote', snapshot['coordination'])

        other_checkout = json.loads(json.dumps(manifest))
        other_checkout['defaults'].update({
            'canonical_remote': 'workstation',
            'publication_remote': 'workstation',
            'base_ref': 'workstation/main',
        })
        other_checkout['integration']['base'] = 'workstation/main'
        other_checkout['coordination']['remote'] = 'workstation'
        other_checkout['stacks'][0].update({
            'base': 'workstation/main',
            'target_remote': 'workstation',
        })
        self.assertEqual(snapshot, module.coordination_manifest_snapshot(other_checkout))

        restored = module.apply_coordination_snapshot(manifest, snapshot)
        self.assertEqual(restored['defaults']['canonical_remote'], 'alice-laptop')
        self.assertEqual(restored['defaults']['publication_remote'], 'alice-laptop')
        self.assertEqual(restored['defaults']['base_ref'], 'alice-laptop/main')
        self.assertEqual(restored['integration']['base'], 'alice-laptop/main')
        self.assertEqual(restored['coordination']['remote'], 'alice-laptop')
        self.assertEqual(restored['stacks'][0]['target_remote'], 'alice-laptop')

    def test_published_state_keeps_the_legacy_coordination_snapshot_and_digest(self):
        module = self.load_module()
        manifest = {
            'version': 2,
            'syncwheel_tracking': 'git-tracked',
            'defaults': {
                'canonical_remote': 'alice-laptop',
                'publication_remote': 'alice-laptop',
                'base_branch': 'main',
                'base_ref': 'alice-laptop/main',
            },
            'integration': {
                'branch': 'integration/shared',
                'base': 'alice-laptop/main',
                'strategy': 'cherry-pick',
                'stacks': ['feature-a'],
            },
            'coordination': {
                'mode': 'active-active',
                'id': 'shared',
                'remote': 'alice-laptop',
                'state_branch': 'syncwheel/state/shared',
                'gc': {'worktree_grace_days': 7, 'backup_retention_days': 30, 'backup_keep': 2},
            },
            'stacks': [{
                'id': 'feature-a',
                'branch': 'pr/feature-a',
                'base': 'alice-laptop/main',
                'target_remote': 'alice-laptop',
                'target_branch': 'main',
                'integration_branch': 'integration/shared',
                'commits': ['feature-a'],
            }],
        }

        snapshot = module.coordination_manifest_snapshot(manifest)

        self.assertEqual(
            json.dumps(snapshot, sort_keys=True, separators=(',', ':')),
            '{"coordination":{"gc":{"backup_keep":2,"backup_retention_days":30,"worktree_grace_days":7},"id":"shared","mode":"active-active","state_branch":"syncwheel/state/shared"},"defaults":{"base_branch":"main","base_ref":{"kind":"remote-ref","ref":"refs/heads/main","role":"canonical"}},"integration":{"base":{"kind":"remote-ref","ref":"refs/heads/main","role":"canonical"},"branch":"integration/shared","stacks":["feature-a"],"strategy":"cherry-pick"},"stacks":[{"base":{"kind":"remote-ref","ref":"refs/heads/main","role":"canonical"},"branch":"pr/feature-a","commits":["feature-a"],"id":"feature-a","integration_branch":"integration/shared","target_branch":"main"}],"version":2}',
        )
        self.assertEqual(
            module.coordination_manifest_digest(manifest),
            'c96c05ff86ecd527db0ec077d8efbe457db0659f69bf108315dd28fecd10b38b',
        )

    def test_coordination_snapshot_round_trip_preserves_draft_state(self):
        module = self.load_module()
        manifest = {
            'version': 2,
            'defaults': {
                'canonical_remote': 'origin',
                'publication_remote': 'origin',
                'base_branch': 'main',
                'base_ref': 'origin/main',
            },
            'integration': {
                'branch': 'integration/shared',
                'base': 'origin/main',
                'strategy': 'cherry-pick',
                'stacks': ['draft-a'],
            },
            'coordination': {
                'mode': 'active-active',
                'id': 'shared',
                'remote': 'origin',
                'state_branch': 'syncwheel/state/shared',
                'gc': {'worktree_grace_days': 7, 'backup_retention_days': 30, 'backup_keep': 2},
            },
            'stacks': [{
                'id': 'draft-a',
                'state': 'draft',
                'branch': 'syncwheel/draft/draft-a',
                'base': 'origin/main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/shared',
                'commits': ['draft-a'],
            }],
        }

        restored = module.apply_coordination_snapshot(
            manifest,
            module.coordination_manifest_snapshot(manifest),
        )

        self.assertEqual(restored['stacks'][0]['state'], 'draft')
        self.assertEqual(restored['stacks'][0]['publication'], {'enabled': False})

    def test_public_snapshot_rejects_an_unmapped_remote_with_a_different_tip(self):
        origin = self.create_remote()
        private = self.create_remote('private')
        repo = self.clone(origin, 'private-alias')
        manifest = self.init_coordinated(repo)
        private_seed = self.clone(private, 'private-publisher')
        (private_seed / 'private.txt').write_text('private\n')
        self.git(private_seed, 'add', 'private.txt')
        self.git(private_seed, 'commit', '-q', '-m', 'feat: private remote main')
        self.git(private_seed, 'push', 'origin', 'main')
        self.git(repo, 'remote', 'add', 'private-host', str(private))
        self.git(repo, 'fetch', 'private-host')
        self.git(repo, 'branch', 'private-host/main', 'origin/main')
        manifest['defaults']['base_ref'] = 'private-host/main'
        manifest['integration']['base'] = 'private-host/main'

        module = self.load_module()
        self.assertNotEqual(
            self.git(repo, 'rev-parse', 'origin/main').stdout.strip(),
            self.git(repo, 'rev-parse', 'refs/remotes/private-host/main').stdout.strip(),
        )
        with self.assertRaisesRegex(module.SyncwheelError, 'unrecognized local remote alias'):
            module.coordination_manifest_snapshot(manifest, repo)

    def test_coordination_state_rejects_an_invalid_typed_remote_ref(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'invalid-state')
        self.init_coordinated(repo)
        self.run_cli(repo, 'int', 'push')
        state_tip, state = self.remote_state(origin)
        state['manifest']['defaults']['base_ref'] = {
            'kind': 'remote-ref',
            'role': 'canonical',
            'ref': 'refs/heads/',
        }
        module = self.load_module()
        state['manifest_digest'] = module.canonical_json_digest(state['manifest'])
        invalid_commit = module.create_coordination_state_commit(repo, state, state_tip)

        with self.assertRaisesRegex(module.SyncwheelError, 'invalid typed remote ref'):
            module.coordination_state_from_commit(repo, invalid_commit, 'default')

    def test_explicit_refs_are_not_rewritten_by_a_remote_named_refs(self):
        module = self.load_module()
        manifest = {
            'version': 2,
            'defaults': {
                'canonical_remote': 'refs',
                'publication_remote': 'origin',
                'base_branch': 'main',
                'base_ref': 'refs/heads/main',
            },
            'integration': {
                'branch': 'integration/shared',
                'base': 'refs/heads/main',
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
        self.assertEqual(snapshot['defaults']['base_ref'], 'refs/heads/main')
        self.assertEqual(snapshot['integration']['base'], 'refs/heads/main')

    def test_public_snapshot_distinguishes_canonical_and_publication_roles(self):
        module = self.load_module()
        manifest = {
            'version': 2,
            'defaults': {
                'canonical_remote': 'upstream',
                'publication_remote': 'fork',
                'base_branch': 'main',
                'base_ref': 'upstream/main',
            },
            'integration': {
                'branch': 'integration/shared',
                'base': 'upstream/main',
                'strategy': 'cherry-pick',
                'stacks': [],
            },
            'coordination': {
                'mode': 'active-active',
                'id': 'shared',
                'remote': 'fork',
                'state_branch': 'syncwheel/state/shared',
                'gc': {'worktree_grace_days': 7, 'backup_retention_days': 30, 'backup_keep': 2},
            },
            'stacks': [],
        }
        publication_base = json.loads(json.dumps(manifest))
        publication_base['defaults']['base_ref'] = 'fork/main'
        publication_base['integration']['base'] = 'fork/main'

        canonical_snapshot = module.coordination_manifest_snapshot(manifest)
        publication_snapshot = module.coordination_manifest_snapshot(publication_base)
        self.assertEqual(
            canonical_snapshot['defaults']['base_ref'],
            {
                'kind': 'remote-ref',
                'role': 'canonical',
                'ref': 'refs/heads/main',
            },
        )
        self.assertEqual(
            publication_snapshot['defaults']['base_ref'],
            {
                'kind': 'remote-ref',
                'role': 'publication',
                'ref': 'refs/heads/main',
            },
        )
        self.assertNotEqual(canonical_snapshot, publication_snapshot)

    def test_public_ref_round_trip_preserves_roles_and_explicit_refs(self):
        module = self.load_module()
        manifest = {
            'version': 2,
            'defaults': {
                'canonical_remote': 'upstream',
                'publication_remote': 'fork',
                'base_branch': 'main',
                'base_ref': 'upstream/main',
            },
            'integration': {
                'branch': 'integration/shared',
                'base': 'fork/main',
                'strategy': 'cherry-pick',
                'stacks': [],
            },
            'coordination': {
                'mode': 'active-active',
                'id': 'shared',
                'remote': 'fork',
                'state_branch': 'syncwheel/state/shared',
                'gc': {'worktree_grace_days': 7, 'backup_retention_days': 30, 'backup_keep': 2},
            },
            'stacks': [],
        }
        restored = module.apply_coordination_snapshot(
            manifest,
            module.coordination_manifest_snapshot(manifest),
        )
        self.assertEqual(restored['defaults']['base_ref'], 'upstream/main')
        self.assertEqual(restored['integration']['base'], 'fork/main')

        explicit = json.loads(json.dumps(manifest))
        explicit['defaults']['base_ref'] = 'refs/heads/main'
        explicit['integration']['base'] = 'refs/syncwheel/coordination/canonical/main'
        restored = module.apply_coordination_snapshot(
            explicit,
            module.coordination_manifest_snapshot(explicit),
        )
        self.assertEqual(restored['defaults']['base_ref'], 'refs/heads/main')
        self.assertEqual(
            restored['integration']['base'],
            'refs/syncwheel/coordination/canonical/main',
        )

    def test_equivalent_lease_loss_aligns_tree_equivalent_local_ref(self):
        origin = self.create_remote()
        first = self.clone(origin, 'first')
        self.init_coordinated(first)
        self.run_cli(first, 'int', 'push')
        _, state = self.remote_state(origin)
        remote_tip = state['managed_refs']['refs/heads/integration/shared']

        second = self.clone(origin, 'second')
        self.git(second, 'branch', 'integration/shared', 'origin/integration/shared')
        self.run_cli(
            second,
            'init',
            '--syncwheel-tracking',
            'git-tracked',
            '--publication-remote',
            'origin',
            '--integration-branch',
            'integration/shared',
        )
        self.disable_fixture_hooks(second)
        self.git(second, 'switch', '-q', 'integration/shared')
        self.git(second, 'commit', '--allow-empty', '-qm', 'chore: equivalent local projection')
        local_tip = self.git(second, 'rev-parse', 'HEAD').stdout.strip()
        self.git(second, 'switch', '-q', 'main')

        module = self.load_module()
        manifest, _ = module.load_manifest(second)
        config = module.coordination_config(manifest)
        changed = {'refs/heads/integration/shared': local_tip}
        race = module.classify_coordination_race(
            second,
            manifest,
            config,
            {'tip': None, 'state': None},
            changed,
            'partial',
        )
        self.assertEqual(race['status'], 'equivalent')
        aligned = module.align_equivalent_coordination_refs(second, config, race['latest']['state'], changed)
        self.assertEqual(aligned[0]['to'], remote_tip)
        self.assertEqual(self.git(second, 'rev-parse', 'integration/shared').stdout.strip(), remote_tip)

    def test_partial_stack_push_and_full_publish_have_distinct_state(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'publisher')
        self.init_coordinated(repo)
        feature_sha = self.commit_on_branch(repo, 'pr/feature-a', 'feature-a.txt')
        self.run_cli(repo, 'stack', 'create', 'feature-a', feature_sha, '--branch', 'pr/feature-a')
        self.run_cli(repo, 'stack', 'push', 'feature-a')
        _, partial = self.remote_state(origin)

        self.assertEqual(partial['publication_scope'], 'stack:feature-a')
        self.assertEqual(partial['projection_status'], 'partial')
        self.assertIn('refs/heads/pr/feature-a', partial['managed_refs'])

        self.run_cli(repo, 'publish')
        _, full = self.remote_state(origin)
        self.assertEqual(full['publication_scope'], 'full')
        self.assertEqual(full['projection_status'], 'convergent')
        self.assertEqual(full['managed_refs']['refs/heads/pr/feature-a'], feature_sha)

    def test_coordinated_promote_transfers_draft_branch_ownership_with_a_tombstone(self):
        origin = self.create_remote()
        first = self.clone(origin, 'promote-first')
        self.init_coordinated(first)
        draft_sha = self.commit_on_branch(first, 'scratch/draft-a', 'draft-a.txt')
        self.run_cli(first, 'stack', 'create', 'draft-a', draft_sha, '--draft')

        module = self.load_module()
        manifest, manifest_path = module.load_manifest(first)
        old_branch = 'syncwheel/draft/draft-a'
        old_ref = f'refs/heads/{old_branch}'
        old_tip = module.ref_tip(first, old_branch)
        with contextlib.redirect_stdout(io.StringIO()):
            module.coordinated_publish(
                first,
                manifest,
                manifest_path,
                {old_ref: old_tip},
                'seed:draft-a',
                'partial',
            )

        candidate, candidate_path = module.load_manifest(first)
        candidate_stack = candidate['stacks'][0]
        candidate_stack['branch'] = 'pr/draft-a'
        candidate_stack['state'] = 'published'
        candidate_stack['publication'] = {'enabled': True}
        self.git(first, 'branch', 'pr/draft-a', old_branch)
        with self.assertRaisesRegex(module.SyncwheelError, 'changing a managed branch ownership'):
            module.coordinated_publish(
                first,
                candidate,
                candidate_path,
                {'refs/heads/pr/draft-a': old_tip},
                'unpermitted:rename',
                'partial',
            )
        self.git(first, 'branch', '-D', 'pr/draft-a')

        second = self.clone(origin, 'promote-second')
        self.init_coordinated(second)
        _, initial_state = self.remote_state(origin)
        second_manifest, second_path = module.load_manifest(second)
        module.save_manifest(
            second_path,
            module.apply_coordination_snapshot(second_manifest, initial_state['manifest']),
        )

        self.run_cli(first, 'stack', 'promote', 'draft-a')
        _, promoted_state = self.remote_state(origin)
        promoted = promoted_state['manifest']['stacks'][0]
        self.assertEqual(promoted['branch'], 'pr/draft-a')
        self.assertNotIn('state', promoted)
        self.assertEqual(promoted_state['managed_refs']['refs/heads/pr/draft-a'], old_tip)
        tombstone = next(item for item in promoted_state['tombstones'] if item['stack'] == 'draft-a')
        self.assertEqual(tombstone['ref'], old_ref)
        self.assertEqual(tombstone['remote_tip'], old_tip)
        self.git(first, 'ls-remote', '--exit-code', 'origin', old_ref)

        handoff = json.loads(self.run_cli(second, 'handoff', '--json').stdout)
        self.assertEqual(handoff['coordination']['state_status'], 'published')
        self.assertEqual(handoff['coordination']['manifest_relation'], 'local_proposal_differs')

    def test_published_draft_source_ref_lets_a_second_clone_rebuild_from_the_manifest(self):
        origin = self.create_remote()
        first = self.clone(origin, 'draft-first')
        self.init_coordinated(first)
        draft_sha = self.commit_on_branch(first, 'scratch/exploration', 'exploration.txt')
        self.run_cli(first, 'stack', 'create', 'exploration', draft_sha, '--draft')
        draft_branch = 'syncwheel/draft/exploration'
        draft_ref = f'refs/heads/{draft_branch}'

        self.run_cli(
            first,
            'reconcile',
            '--apply',
            '--push',
            '--stack',
            'exploration',
            '--skip-integration',
        )

        _, state = self.remote_state(origin)
        published = next(item for item in state['manifest']['stacks'] if item['id'] == 'exploration')
        self.assertEqual(published['state'], 'draft')
        self.assertEqual(
            state['managed_refs'][draft_ref],
            self.git(first, 'rev-parse', draft_branch).stdout.strip(),
        )
        self.git(first, 'ls-remote', '--exit-code', 'origin', draft_ref)

        second = self.clone(origin, 'draft-second')
        self.init_coordinated(second)
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(second)
        module.save_manifest(
            manifest_path,
            module.apply_coordination_snapshot(manifest, state['manifest']),
        )
        self.git(second, 'rev-parse', '--verify', '--quiet', draft_ref, expected=1)
        self.assertEqual(self.git(second, 'remote').stdout.split(), ['origin'])
        self.git(second, 'cat-file', '-e', state['managed_refs'][draft_ref])

        rebuild = self.run_cli(
            second,
            'reconcile',
            '--apply',
            '--stack',
            'exploration',
            '--skip-integration',
            '--rebuild',
            'all',
        )

        self.assertIn('rebuild_stack stack=exploration', rebuild.stdout)
        self.assertEqual(
            self.git(second, 'rev-parse', f'{draft_branch}^{{tree}}').stdout.strip(),
            self.git(second, 'rev-parse', f"{state['managed_refs'][draft_ref]}^{{tree}}").stdout.strip(),
        )
        self.assertEqual(
            self.git(second, 'show', f'{draft_branch}:exploration.txt').stdout,
            'scratch/exploration\n',
        )
        rebuilt = next(
            item
            for item in json.loads((second / '.syncwheel' / 'manifest.json').read_text())['stacks']
            if item['id'] == 'exploration'
        )
        self.assertEqual(rebuilt['state'], 'draft')
        self.assertEqual(len(rebuilt['commits']), 1)

    def test_partial_publish_can_adopt_new_stack_without_rebuilding_integration(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'partial-stack-adoption')
        self.init_coordinated(repo)
        self.run_cli(repo, 'int', 'push')
        integration_before = self.git(
            repo, 'ls-remote', 'origin', 'refs/heads/integration/shared'
        ).stdout.split()[0]

        stack_tip = self.commit_on_branch(repo, 'scratch/new-stack', 'new-stack.txt')
        self.git(repo, 'branch', 'pr/new-stack', stack_tip)
        self.run_cli(
            repo, 'stack', 'create', 'new-stack', stack_tip,
            '--branch', 'pr/new-stack',
        )
        self.run_cli(
            repo, 'reconcile', '--apply', '--push', '--stack', 'new-stack',
            '--skip-integration', '--rebuild', 'none',
        )

        _, state = self.remote_state(origin)
        self.assertEqual(state['projection_status'], 'partial')
        self.assertEqual(
            state['managed_refs']['refs/heads/pr/new-stack'], stack_tip
        )
        self.assertEqual(
            self.git(repo, 'ls-remote', 'origin', 'refs/heads/integration/shared').stdout.split()[0],
            integration_before,
        )

    def test_partial_stack_adoption_predicate_fails_closed(self):
        module = self.load_module()
        remote = {
            'branch': 'integration/shared', 'base': 'origin/main',
            'strategy': 'cherry-pick', 'stacks': ['existing-a', 'existing-b'],
        }
        local = {
            **remote, 'stacks': ['existing-a', 'existing-b', 'new-stack'],
        }
        self.assertTrue(module.integration_partial_stack_adoption_allowed(
            remote, local, {'new-stack'}, {'new-stack'}
        ))

        changed_shape = {**local, 'strategy': 'merge-stacks'}
        self.assertFalse(module.integration_partial_stack_adoption_allowed(
            remote, changed_shape, {'new-stack'}, {'new-stack'}
        ))
        reordered = {**local, 'stacks': ['existing-b', 'existing-a', 'new-stack']}
        self.assertFalse(module.integration_partial_stack_adoption_allowed(
            remote, reordered, {'new-stack'}, {'new-stack'}
        ))
        removed = {**local, 'stacks': ['existing-a', 'new-stack']}
        self.assertFalse(module.integration_partial_stack_adoption_allowed(
            remote, removed, {'new-stack'}, {'new-stack'}
        ))
        self.assertFalse(module.integration_partial_stack_adoption_allowed(
            remote, local, {'new-stack'}, set()
        ))
        self.assertFalse(module.integration_partial_stack_adoption_allowed(
            remote, local, {'new-stack'}, {'new-stack'}, {'stack': 'existing-a'}
        ))
    def test_draft_push_to_the_target_remote_is_refused_by_state(self):
        origin = self.create_remote()
        forge = self.create_remote('forge')
        repo = self.clone(origin, 'draft-target')
        self.git(repo, 'remote', 'add', 'forge', str(forge))
        self.init_coordinated(repo)
        draft_sha = self.commit_on_branch(repo, 'scratch/exploration', 'exploration.txt')
        self.run_cli(
            repo,
            'stack',
            'create',
            'exploration',
            draft_sha,
            '--draft',
            '--target-remote',
            'forge',
        )
        self.run_cli(repo, 'stack', 'push', 'exploration')

        failure = self.run_cli(repo, 'stack', 'push', 'exploration', '--remote', 'forge', expected=2)

        self.assertIn('state draft', failure.stderr)
        self.assertIn("remote 'forge'", failure.stderr)
        self.git(repo, 'ls-remote', '--exit-code', 'origin', 'refs/heads/syncwheel/draft/exploration')
        self.assertEqual(
            self.git(repo, 'ls-remote', 'forge', 'refs/heads/syncwheel/draft/exploration').stdout.strip(),
            '',
        )

    def test_coordinated_demote_allows_only_the_explicit_state_transition(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'demote')
        self.init_coordinated(repo)
        feature_sha = self.commit_on_branch(repo, 'pr/feature-a', 'feature-a.txt')
        self.run_cli(repo, 'stack', 'create', 'feature-a', feature_sha, '--branch', 'pr/feature-a')
        self.run_cli(repo, 'stack', 'push', 'feature-a')

        self.run_cli(repo, 'stack', 'demote', 'feature-a')

        _, state = self.remote_state(origin)
        draft = state['manifest']['stacks'][0]
        self.assertEqual(draft['branch'], 'pr/feature-a')
        self.assertEqual(draft['state'], 'draft')
        self.assertEqual(state['managed_refs']['refs/heads/pr/feature-a'], feature_sha)

    def test_coordination_domains_cannot_claim_the_same_managed_ref(self):
        origin = self.create_remote()
        first = self.clone(origin, 'owner-one')
        self.init_coordinated(first)
        self.run_cli(first, 'int', 'push')

        second = self.clone(origin, 'owner-two')
        self.git(second, 'branch', 'integration/shared', 'origin/integration/shared')
        self.run_cli(
            second,
            'init',
            '--syncwheel-tracking',
            'git-tracked',
            '--publication-remote',
            'origin',
            '--integration-branch',
            'integration/shared',
            '--coordination-id',
            'second-domain',
        )
        self.disable_fixture_hooks(second)
        failure = self.run_cli(second, 'int', 'push', expected=2)
        self.assertIn('already owned by another coordination domain', failure.stderr)

    def test_stale_manifest_cannot_drop_a_remotely_published_stack(self):
        origin = self.create_remote()
        stale = self.clone(origin, 'stale')
        self.init_coordinated(stale)
        self.run_cli(stale, 'int', 'push')

        current = self.clone(origin, 'current')
        self.git(current, 'branch', 'integration/shared', 'origin/integration/shared')
        self.run_cli(
            current,
            'init',
            '--syncwheel-tracking',
            'git-tracked',
            '--publication-remote',
            'origin',
            '--integration-branch',
            'integration/shared',
        )
        self.disable_fixture_hooks(current)
        self.set_integration_membership(current, 'legacy')
        remote_sha = self.commit_on_branch(current, 'pr/remote-change', 'remote.txt')
        self.run_cli(current, 'stack', 'create', 'remote-change', remote_sha, '--branch', 'pr/remote-change')
        self.run_cli(current, 'stack', 'push', 'remote-change')

        stale_sha = self.commit_on_branch(stale, 'pr/stale-change', 'stale.txt')
        self.run_cli(stale, 'stack', 'create', 'stale-change', stale_sha, '--branch', 'pr/stale-change')
        failure = self.run_cli(stale, 'stack', 'push', 'stale-change', expected=2)
        self.assertIn('would drop remote-managed stack(s): remote-change', failure.stderr)
        self.git(stale, 'ls-remote', '--exit-code', 'origin', 'refs/heads/pr/remote-change')
        missing = self.git(
            stale,
            'ls-remote',
            '--exit-code',
            'origin',
            'refs/heads/pr/stale-change',
            expected=2,
        )
        self.assertEqual(missing.returncode, 2)

    def test_stack_close_publishes_tombstone_without_deleting_remote_branch(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'close')
        self.init_coordinated(repo)
        feature_sha = self.commit_on_branch(repo, 'pr/feature-a', 'feature-a.txt')
        self.run_cli(repo, 'stack', 'create', 'feature-a', feature_sha, '--branch', 'pr/feature-a')
        self.run_cli(repo, 'stack', 'push', 'feature-a')

        self.run_cli(repo, 'stack', 'close', 'feature-a', '--force', '--reason', 'abandoned')
        _, state = self.remote_state(origin)
        tombstone = next(item for item in state['tombstones'] if item['stack'] == 'feature-a')
        self.assertEqual(tombstone['reason'], 'abandoned')
        self.git(repo, 'ls-remote', '--exit-code', 'origin', 'refs/heads/pr/feature-a')

    def test_worktree_unlock_releases_a_closed_stack_lock(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'unlock')
        self.init_coordinated(repo)
        feature_sha = self.commit_on_branch(repo, 'pr/feature-a', 'feature-a.txt')
        self.run_cli(repo, 'stack', 'create', 'feature-a', feature_sha, '--branch', 'pr/feature-a')
        self.run_cli(repo, 'stack', 'push', 'feature-a')
        self.run_cli(repo, 'worktree', 'lock', 'feature-a')
        self.run_cli(repo, 'stack', 'close', 'feature-a', '--force')

        self.run_cli(repo, 'worktree', 'unlock', 'feature-a')
        module = self.load_module()
        _, coordination = module.coordination_profile(repo)
        self.assertNotIn('feature-a', coordination['locks'])

    def test_recreated_branch_is_not_gc_candidate_and_supersedes_tombstone(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'recreated')
        self.init_coordinated(repo)
        feature_sha = self.commit_on_branch(repo, 'pr/reused', 'feature-a.txt')
        self.run_cli(repo, 'stack', 'create', 'closed-stack', feature_sha, '--branch', 'pr/reused')
        self.run_cli(repo, 'stack', 'push', 'closed-stack')
        self.run_cli(repo, 'stack', 'close', 'closed-stack', '--force')
        state_tip, closed_state = self.remote_state(origin)
        state_info = {'tip': state_tip, 'state': json.loads(json.dumps(closed_state))}
        state_info['state']['tombstones'][0]['closed_at'] = '2020-01-01T00:00:00+00:00'

        self.run_cli(repo, 'stack', 'create', 'recreated-stack', feature_sha, '--branch', 'pr/reused')
        module = self.load_module()
        manifest, _ = module.load_manifest(repo)
        plan = module.coordination_gc_plan(repo, manifest, fetch=False, state_info=state_info)
        self.assertEqual(plan['candidates'], [])
        self.assertTrue(any('active in the current manifest' in item for item in plan['skipped']))

        self.run_cli(repo, 'stack', 'push', 'recreated-stack')
        _, republished_state = self.remote_state(origin)
        self.assertFalse(any(
            item.get('ref') == 'refs/heads/pr/reused'
            for item in republished_state['tombstones']
        ))

    def test_gc_requires_the_tombstone_original_remote_tip(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'tombstone-tip')
        self.init_coordinated(repo)
        original_tip = self.commit_on_branch(repo, 'pr/recreated', 'original.txt')
        self.git(repo, 'push', 'origin', 'pr/recreated')
        self.git(repo, 'switch', '-q', 'pr/recreated')
        (repo / 'recreated.txt').write_text('recreated\n')
        self.git(repo, 'add', 'recreated.txt')
        self.git(repo, 'commit', '-q', '-m', 'feat: recreate branch')
        recreated_tip = self.git(repo, 'rev-parse', 'HEAD').stdout.strip()
        self.git(repo, 'push', 'origin', 'pr/recreated')
        self.git(repo, 'switch', '-q', 'main')

        module = self.load_module()
        manifest, _ = module.load_manifest(repo)
        state_info = {
            'tip': 'test-state',
            'state': {
                'schema_version': 1,
                'coordination_id': 'default',
                'manifest': module.coordination_manifest_snapshot(manifest),
                'manifest_digest': module.coordination_manifest_digest(manifest),
                'managed_refs': {'refs/heads/pr/recreated': recreated_tip},
                'tombstones': [{
                    'stack': 'closed-stack',
                    'branch': 'pr/recreated',
                    'ref': 'refs/heads/pr/recreated',
                    'closed_at': '2020-01-01T00:00:00+00:00',
                    'remote_tip': original_tip,
                }],
            },
        }
        plan = module.coordination_gc_plan(repo, manifest, fetch=False, state_info=state_info)
        self.assertEqual(plan['candidates'], [])
        self.assertTrue(any('no longer matches the tombstone tip' in item for item in plan['skipped']))

    def test_atomic_rejection_does_not_publish_any_ref_or_state(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'atomic')
        self.init_coordinated(repo)
        good_sha = self.commit_on_branch(repo, 'pr/good', 'good.txt')
        reject_sha = self.commit_on_branch(repo, 'pr/reject', 'reject.txt')
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        for stack_id, branch, sha in (
            ('good', 'pr/good', good_sha),
            ('reject', 'pr/reject', reject_sha),
        ):
            manifest['stacks'].append({
                'id': stack_id,
                'branch': branch,
                'base': 'origin/main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/shared',
                'commits': [sha],
            })
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        hook = origin / 'hooks' / 'pre-receive'
        hook.write_text(
            '#!/bin/sh\n'
            'while read old new ref; do\n'
            '  if [ "$ref" = "refs/heads/pr/reject" ]; then exit 1; fi\n'
            'done\n'
            'exit 0\n'
        )
        hook.chmod(0o755)
        module = self.load_module()
        loaded, loaded_path = module.load_manifest(repo)

        with self.assertRaises(module.SyncwheelError):
            module.coordinated_publish(
                repo,
                loaded,
                loaded_path,
                {
                    'refs/heads/pr/good': good_sha,
                    'refs/heads/pr/reject': reject_sha,
                },
                'partial',
                'partial',
            )

        for ref in ('refs/heads/pr/good', 'refs/heads/pr/reject', 'refs/heads/syncwheel/state/default'):
            result = subprocess.run(
                ['git', '--git-dir', str(origin), 'rev-parse', '--verify', '--quiet', ref],
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0, ref)

    def test_disjoint_stack_changes_are_mergeable_but_overlaps_are_not(self):
        module = self.load_module()
        base = {
            'version': 2,
            'syncwheel_tracking': 'git-tracked',
            'defaults': {'canonical_remote': 'origin', 'publication_remote': 'origin', 'base_branch': 'main', 'base_ref': 'origin/main'},
            'integration': {'branch': 'integration/shared', 'base': 'origin/main', 'strategy': 'cherry-pick', 'stacks': []},
            'coordination': {
                'mode': 'active-active',
                'id': 'shared',
                'remote': 'origin',
                'state_branch': 'syncwheel/state/shared',
                'gc': {'worktree_grace_days': 7, 'backup_retention_days': 30, 'backup_keep': 2},
            },
            'stacks': [
                {'id': 'a', 'branch': 'pr/a', 'base': 'origin/main', 'target_remote': 'origin', 'target_branch': 'main', 'integration_branch': 'integration/shared', 'commits': ['a1']},
                {'id': 'b', 'branch': 'pr/b', 'base': 'origin/main', 'target_remote': 'origin', 'target_branch': 'main', 'integration_branch': 'integration/shared', 'commits': ['b1']},
            ],
        }
        local = json.loads(json.dumps(base))
        remote = json.loads(json.dumps(base))
        local['stacks'][0]['commits'] = ['a2']
        remote['stacks'][1]['commits'] = ['b2']

        mergeable = module.merge_coordination_snapshots(base, local, remote)
        self.assertEqual(mergeable['status'], 'mergeable')
        self.assertEqual(mergeable['local_stacks'], ['a'])
        self.assertEqual(mergeable['remote_stacks'], ['b'])
        local['syncwheel_worktree_root'] = 'local-private-root'
        local['stacks'][0]['meta'] = {'local_note': 'keep-local'}
        applied = module.apply_coordination_snapshot(local, mergeable['merged'])
        self.assertEqual(applied['syncwheel_worktree_root'], 'local-private-root')
        self.assertEqual(applied['stacks'][0]['meta'], {'local_note': 'keep-local'})

        overlap = json.loads(json.dumps(remote))
        overlap['stacks'][0]['commits'] = ['other-a']
        conflict = module.merge_coordination_snapshots(base, local, overlap)
        self.assertEqual(conflict['status'], 'conflict')
        self.assertEqual(conflict['reason'], 'overlapping_stack_changes')

        integration_change = json.loads(json.dumps(remote))
        integration_change['integration']['stacks'] = ['b']
        conflict = module.merge_coordination_snapshots(base, local, integration_change)
        self.assertEqual(conflict['status'], 'conflict')
        self.assertEqual(conflict['reason'], 'shared_integration_or_defaults_changed')

    def test_accept_merge_preflights_targets_before_persisting_the_merge(self):
        module = self.load_module()
        manifest = {
            'version': 2,
            'syncwheel_tracking': 'git-tracked',
            'defaults': {'publication_remote': 'origin'},
            'integration': {'branch': 'integration/shared', 'stacks': []},
            'coordination': {'mode': 'active-active'},
            'stacks': [],
        }
        manifest_path = self.tmp / 'manifest.json'
        calls = []

        def accept_merge(_repo_root, current, _manifest_path, *, persist=True, **_kwargs):
            calls.append(persist)
            return current

        args = SimpleNamespace(
            repo=str(self.tmp),
            repo_path=None,
            manifest=None,
            personal=None,
            accept_merge=True,
            command='publish',
            fetch=False,
            mode='standard',
            stack=[],
            remote=None,
            worktree_root=None,
            json=False,
            apply=True,
            push=True,
        )
        dirty_target = module.SyncwheelError('dirty integration target')
        with contextlib.ExitStack() as patches:
            patches.enter_context(mock.patch.object(module, 'resolve_repo_root', return_value=self.tmp))
            patches.enter_context(
                mock.patch.object(module, 'require_manifest', return_value=(manifest, manifest_path))
            )
            patches.enter_context(
                mock.patch.object(module, 'apply_pending_coordination_merge', side_effect=accept_merge)
            )
            patches.enter_context(
                mock.patch.object(module, 'validate_manifest', return_value={'errors': []})
            )
            patches.enter_context(
                mock.patch.object(module, 'effective_worktree_root', return_value=None)
            )
            patches.enter_context(
                mock.patch.object(module, 'integration_sync_report', return_value={})
            )
            patches.enter_context(
                mock.patch.object(module, 'reconcile_actions', return_value=[{'type': 'rebuild_integration'}])
            )
            patches.enter_context(
                mock.patch.object(module, 'integration_commit_diagnostics', return_value=[])
            )
            patches.enter_context(
                mock.patch.object(module, 'collect_repo_snapshot', return_value={})
            )
            patches.enter_context(mock.patch.object(module, 'print_reconcile_report'))
            patches.enter_context(
                mock.patch.object(
                    module,
                    'preflight_reconcile_mutation_targets',
                    side_effect=dirty_target,
                )
            )
            with self.assertRaisesRegex(module.SyncwheelError, 'dirty integration target'):
                module.command_reconcile(args)

        self.assertEqual(calls, [False])

    def test_gc_requires_an_old_tombstone_and_respects_local_locks(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'gc')
        self.init_coordinated(repo)
        stale_sha = self.commit_on_branch(repo, 'pr/stale', 'stale.txt')
        self.git(repo, 'push', 'origin', 'pr/stale')
        worktree = repo / '.syncwheel' / 'wt' / 'pr-stale'
        self.git(repo, 'worktree', 'add', str(worktree), 'pr/stale')
        module = self.load_module()
        manifest, _ = module.load_manifest(repo)
        old = '2020-01-01T00:00:00+00:00'
        state_info = {
            'tip': 'test-state',
            'state': {
                'schema_version': 1,
                'coordination_id': 'default',
                'manifest': module.coordination_manifest_snapshot(manifest),
                'manifest_digest': module.coordination_manifest_digest(manifest),
                'managed_refs': {'refs/heads/pr/stale': stale_sha},
                'tombstones': [
                    {
                        'stack': 'stale',
                        'branch': 'pr/stale',
                        'ref': 'refs/heads/pr/stale',
                        'closed_at': old,
                        'remote_tip': stale_sha,
                    }
                ],
            },
        }
        module.save_repo_profile(repo, {'coordination': {'locks': {'stale': {'created_at': old}}}})
        locked = module.coordination_gc_plan(repo, manifest, fetch=False, state_info=state_info)
        self.assertEqual(locked['candidates'], [])
        self.assertTrue(any('lock' in item for item in locked['skipped']))

        module.save_repo_profile(repo, {'coordination': {}})
        ready = module.coordination_gc_plan(repo, manifest, fetch=False, state_info=state_info)
        self.assertEqual([item['type'] for item in ready['candidates']], ['remove_worktree', 'delete_branch'])

        original_plan = module.coordination_gc_plan
        calls = {'count': 0}

        def add_lock_after_initial_plan(*args, **kwargs):
            result = original_plan(*args, **kwargs)
            calls['count'] += 1
            if calls['count'] == 1:
                module.save_repo_profile(repo, {'coordination': {'locks': {'stale': {'created_at': old}}}})
            return result

        with mock.patch.object(module, 'coordination_gc_plan', side_effect=add_lock_after_initial_plan):
            raced = module.run_coordination_gc(repo, manifest, apply=True, fetch=False, state_info=state_info)
        self.assertTrue(worktree.exists())
        self.assertTrue(any('no longer eligible' in item for item in raced['skipped']))

        module.save_repo_profile(repo, {'coordination': {}})
        module.run_coordination_gc(repo, manifest, apply=True, fetch=False, state_info=state_info)
        self.assertFalse(worktree.exists())
        self.assertNotEqual(self.git(repo, 'show-ref', '--verify', '--quiet', 'refs/heads/pr/stale', expected=1).returncode, 0)

    def test_repair_is_reviewed_exact_cas_and_preserves_parent_payload(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'repair')
        self.init_coordinated(repo)
        self.run_cli(repo, 'int', 'push')
        parent_tip, parent = self.remote_state(origin)
        branch = 'integration/shared'
        (repo / 'after-state.txt').write_text('advanced under fixture freeze\n')
        self.git(repo, 'add', 'after-state.txt')
        self.git(repo, 'commit', '-qm', 'test: advance managed ref')
        advanced = self.git(repo, 'rev-parse', 'HEAD').stdout.strip()
        self.git(repo, 'push', '--no-verify', 'origin', branch)

        module = self.load_module()
        manifest, _ = module.load_manifest(repo)
        ref = f'refs/heads/{branch}'
        plan, _ = module.coordination_repair_plan(repo, manifest, ref, 'fixture-freeze')
        self.assertEqual(plan['status'], 'repair-required')
        self.assertEqual(plan['expectedStateTip'], parent_tip)
        self.assertEqual(plan['expectedRemoteTip'], advanced)

        class FrozenFixtureBackend(module.CoordinationRepairBackend):
            name = 'fixture-freeze'

            def preflight(self, **_kwargs):
                return {'freeze': 'verified-fixture'}

            postflight = preflight

            def apply(self, **kwargs):
                observed = module.remote_ref_tips(
                    kwargs['repo_root'], kwargs['remote'], list(kwargs['guarded_refs'])
                )
                if observed != kwargs['guarded_refs']:
                    raise module.SyncwheelError('fixture freeze lost before state CAS')
                result = module.git(
                    kwargs['repo_root'],
                    'push',
                    '--no-verify',
                    f"--force-with-lease={kwargs['state_ref']}:{kwargs['expected_state_tip']}",
                    kwargs['remote'],
                    f"{kwargs['new_state_tip']}:{kwargs['state_ref']}",
                    check=False,
                )
                if result.returncode:
                    raise module.SyncwheelError('fixture state CAS rejected')
                return {'freeze': 'verified-fixture'}

        result = module.apply_coordination_repair_plan(
            repo, manifest, plan, backend=FrozenFixtureBackend()
        )
        self.assertEqual(result['status'], 'repaired')
        child_tip, child = self.remote_state(origin)
        self.assertEqual(child_tip, result['state_tip'])
        self.assertEqual(child['parent_state'], parent_tip)
        self.assertEqual(child['managed_refs'][ref], advanced)
        for key in ('manifest', 'manifest_digest', 'tombstones'):
            self.assertEqual(child[key], parent[key])
        for managed_ref, tip in parent['managed_refs'].items():
            if managed_ref != ref:
                self.assertEqual(child['managed_refs'][managed_ref], tip)

        noop, _ = module.coordination_repair_plan(repo, manifest, ref, 'fixture-freeze')
        self.assertEqual(noop['status'], 'noop')
        noop_result = module.apply_coordination_repair_plan(
            repo, manifest, noop, backend=FrozenFixtureBackend()
        )
        self.assertEqual(noop_result['status'], 'noop')
        self.assertEqual(self.remote_state(origin)[0], child_tip)
        self.run_cli(repo, 'int', 'push')

        stack_tip = self.commit_on_branch(repo, 'pr/after-repair', 'after-repair.txt')
        self.run_cli(
            repo,
            'stack',
            'create',
            'after-repair',
            stack_tip,
            '--branch',
            'pr/after-repair',
        )
        self.run_cli(repo, 'stack', 'push', 'after-repair')
        self.run_cli(repo, 'stack', 'close', 'after-repair', '--force')
        _, closed_state = self.remote_state(origin)
        self.assertTrue(
            any(item.get('stack') == 'after-repair' for item in closed_state['tombstones'])
        )

    def test_repair_rejects_plan_drift_wrong_lease_and_unsupported_github(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'repair-races')
        self.init_coordinated(repo)
        self.run_cli(repo, 'int', 'push')
        no_op = self.git(
            repo,
            'push',
            '--atomic',
            '--dry-run',
            '--force-with-lease=refs/heads/integration/shared:' + ('0' * 40),
            'origin',
            'refs/heads/integration/shared:refs/heads/integration/shared',
        )
        self.assertIn('up-to-date', (no_op.stdout + no_op.stderr).lower())
        (repo / 'race.txt').write_text('race\n')
        self.git(repo, 'add', 'race.txt')
        self.git(repo, 'commit', '-qm', 'test: race')
        self.git(repo, 'push', '--no-verify', 'origin', 'integration/shared')
        module = self.load_module()
        manifest, _ = module.load_manifest(repo)
        ref = 'refs/heads/integration/shared'
        github_plan, _ = module.coordination_repair_plan(repo, manifest, ref)

        def backend_plan(name):
            candidate = json.loads(json.dumps(github_plan))
            candidate['freezeBackend'] = name
            unsigned = {key: value for key, value in candidate.items() if key != 'planDigest'}
            candidate['planDigest'] = module.canonical_json_digest(unsigned)
            return candidate

        plan = backend_plan('fixture-wrong-lease')

        tampered = json.loads(json.dumps(plan))
        tampered['expectedRemoteTip'] = '0' * 40
        with self.assertRaisesRegex(module.SyncwheelError, 'digest'):
            module.apply_coordination_repair_plan(repo, manifest, tampered)
        with self.assertRaisesRegex(module.SyncwheelError, 'GitHub branch locks can be bypassed'):
            module.apply_coordination_repair_plan(repo, manifest, github_plan)

        class WrongLeaseBackend(module.CoordinationRepairBackend):
            name = 'fixture-wrong-lease'

            def preflight(self, **_kwargs):
                return {'freeze': 'verified-fixture'}

            postflight = preflight

            def apply(self, **_kwargs):
                raise module.SyncwheelError('fixture state CAS rejected: wrong lease')

        state_before = self.remote_state(origin)[0]
        with self.assertRaisesRegex(module.SyncwheelError, 'wrong lease'):
            module.apply_coordination_repair_plan(
                repo, manifest, plan, backend=WrongLeaseBackend()
            )
        self.assertEqual(self.remote_state(origin)[0], state_before)

        class LostOutcomeBackend(module.CoordinationRepairBackend):
            name = 'fixture-lost-outcome'

            def preflight(self, **_kwargs):
                return {'freeze': 'verified-fixture'}

            postflight = preflight

            def apply(self, **_kwargs):
                return {'claimed': 'success'}

        with self.assertRaisesRegex(module.SyncwheelError, 'outcome is unknown'):
            module.apply_coordination_repair_plan(
                repo, manifest, backend_plan('fixture-lost-outcome'), backend=LostOutcomeBackend()
            )

        stale = json.loads(json.dumps(plan))
        stale['expectedStateTip'] = '1' * 40
        unsigned = {key: value for key, value in stale.items() if key != 'planDigest'}
        stale['planDigest'] = module.canonical_json_digest(unsigned)
        with self.assertRaisesRegex(module.SyncwheelError, 'reviewed plan drifted'):
            module.apply_coordination_repair_plan(
                repo, manifest, stale, backend=WrongLeaseBackend()
            )

    def test_github_lock_backend_stops_before_state_cas(self):
        module = self.load_module()
        with self.assertRaisesRegex(module.SyncwheelError, 'GitHub branch locks can be bypassed'):
            module.GitHubLockCoordinationRepairBackend().preflight()

    def test_repair_rejects_malformed_reviewed_plan(self):
        module = self.load_module()
        with self.assertRaisesRegex(module.SyncwheelError, 'must be a JSON object'):
            module.apply_coordination_repair_plan(Path('.'), {}, [])
        with self.assertRaisesRegex(module.SyncwheelError, 'plan is missing'):
            module.apply_coordination_repair_plan(Path('.'), {}, {})
        origin = self.create_remote()
        repo = self.clone(origin, 'malformed-repair-plan')
        self.init_coordinated(repo)
        plan_path = repo / 'repair-plan.json'
        plan_path.write_text('[]\n')
        result = self.run_cli(
            repo, 'coordination', 'repair', '--apply', '--plan-file', str(plan_path), expected=2
        )
        self.assertIn('plan must be a JSON object', result.stderr)
