import contextlib
import importlib.util
import io
import json
import os
import signal
import shutil
import subprocess
import tempfile
import time
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
        manifest_path = Path(repo) / '.syncwheel' / 'manifest.json'
        preserved_manifest = None
        if args and args[0] == 'switch' and manifest_path.exists():
            # Fixture-only Git switches create product history. Preserve the
            # local control source while branches differ on whether it is
            # tracked by the integration control commit.
            preserved_manifest = manifest_path.read_bytes()
            tracked = subprocess.run(
                ['git', 'ls-files', '--error-unmatch', '--', '.syncwheel/manifest.json'],
                cwd=repo, text=True, capture_output=True,
            ).returncode == 0
            if tracked:
                subprocess.run(
                    ['git', 'checkout', '--', '.syncwheel/manifest.json'],
                    cwd=repo, check=True, capture_output=True, text=True,
                )
            else:
                manifest_path.unlink()
        result = subprocess.run(
            ['git', *args],
            cwd=repo,
            text=True,
            capture_output=True,
        )
        if preserved_manifest is not None:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(preserved_manifest)
        if result.returncode != expected:
            raise AssertionError(
                f"git {args} expected {expected}, got {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def run_cli(self, repo, *args, expected=0, extra_env=None):
        env = dict(os.environ)
        env.update(self.environment)
        if extra_env:
            env.update(extra_env)
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

    def wait_for_path(self, path, process, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if Path(path).exists():
                return
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f'process exited before checkpoint: {process.returncode}\n'
                    f'STDOUT:\n{stdout}\nSTDERR:\n{stderr}'
                )
            time.sleep(0.01)
        process.kill()
        stdout, stderr = process.communicate()
        self.fail(
            f'timed out waiting for checkpoint {path}\n'
            f'STDOUT:\n{stdout}\nSTDERR:\n{stderr}'
        )

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

    def track_fixture_ignore_in_replay_base(self, repo):
        self.git(repo, 'add', '.gitignore')
        self.git(repo, 'commit', '-q', '-m', 'test: track Syncwheel ignore policy')
        self.git(repo, 'push', '-q', 'origin', 'HEAD:main')

    def set_integration_membership(self, repo, integration_membership):
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        # Coordination fixtures model pre-existing repositories. Keep their
        # historical optional integration behavior explicit, while dedicated
        # initialization coverage exercises the required default.
        module = self.load_module()
        manifest, _ = module.load_manifest(repo, manifest_path)
        manifest['defaults']['integration_membership'] = integration_membership
        with module.manifest_write_transaction(repo, manifest_path, 'fixture-membership'):
            module.save_manifest_with_ledger(
                repo,
                manifest_path,
                manifest,
                'fixture_integration_membership',
            )
        persisted, _ = module.load_manifest(repo, manifest_path)
        return persisted

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

    def prepare_tree_equivalent_repair(self, name='tree-equivalent-repair'):
        origin = self.create_remote(name)
        repo = self.clone(origin, name)
        self.init_coordinated(repo)
        branch = 'integration/shared'
        self.git(repo, 'switch', '-q', branch)
        (repo / 'projection.txt').write_text('stable projection\n')
        self.git(repo, 'add', 'projection.txt')
        self.git(repo, 'commit', '-qm', 'test: recorded integration projection')
        self.run_cli(repo, 'int', 'push')
        parent_tip, parent = self.remote_state(origin)
        ref = f'refs/heads/{branch}'
        recorded = parent['managed_refs'][ref]
        tree = self.git(repo, 'rev-parse', f'{recorded}^{{tree}}').stdout.strip()
        recorded_parent = self.git(repo, 'rev-parse', f'{recorded}^').stdout.strip()
        observed = self.git(
            repo,
            'commit-tree',
            tree,
            '-p',
            recorded_parent,
            '-m',
            'test: equivalent remote integration projection',
        ).stdout.strip()
        fixture_ref = 'refs/heads/fixture/tree-equivalent-object'
        self.git(repo, 'push', '-q', 'origin', f'{observed}:{fixture_ref}')
        subprocess.run(
            [
                'git', '--git-dir', str(origin), 'update-ref',
                ref, observed, recorded,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                'git', '--git-dir', str(origin), 'update-ref',
                '-d', fixture_ref, observed,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        module = self.load_module()
        manifest, _ = module.load_manifest(repo)
        plan, _ = module.coordination_repair_plan(
            repo,
            manifest,
            ref,
            module.COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND,
        )
        return {
            'origin': origin,
            'repo': repo,
            'module': module,
            'manifest': manifest,
            'ref': ref,
            'parent_tip': parent_tip,
            'parent': parent,
            'recorded': recorded,
            'observed': observed,
            'tree': tree,
            'plan': plan,
        }

    def prepare_fast_forward_repair(self, name='fast-forward-repair'):
        origin = self.create_remote(name)
        repo = self.clone(origin, name)
        self.init_coordinated(repo)
        self.run_cli(repo, 'int', 'push')
        parent_tip, parent = self.remote_state(origin)
        branch = 'integration/shared'
        ref = f'refs/heads/{branch}'
        recorded = parent['managed_refs'][ref]
        self.git(repo, 'switch', '-q', branch)
        (repo / 'reviewed-advance.txt').write_text('reviewed fast-forward\n')
        self.git(repo, 'add', 'reviewed-advance.txt')
        self.git(repo, 'commit', '-qm', 'test: reviewed managed-ref advance')
        first_advance = self.git(repo, 'rev-parse', 'HEAD').stdout.strip()
        (repo / 'reviewed-advance-2.txt').write_text('second reviewed fast-forward\n')
        self.git(repo, 'add', 'reviewed-advance-2.txt')
        self.git(repo, 'commit', '-qm', 'test: second reviewed managed-ref advance')
        observed = self.git(repo, 'rev-parse', 'HEAD').stdout.strip()
        self.git(repo, 'push', '--no-verify', 'origin', branch)
        module = self.load_module()
        manifest, _ = module.load_manifest(repo)
        plan, _ = module.coordination_repair_plan(
            repo,
            manifest,
            ref,
            module.COORDINATION_REPAIR_FAST_FORWARD_BACKEND,
        )
        return {
            'origin': origin,
            'repo': repo,
            'module': module,
            'manifest': manifest,
            'ref': ref,
            'parent_tip': parent_tip,
            'parent': parent,
            'recorded': recorded,
            'advance_commits': [first_advance, observed],
            'observed': observed,
            'plan': plan,
        }

    def legacy_state_manifest_digest(self, repo, state):
        """Reproduce the 0.42.0 digest: the normalized public snapshot, not the file."""
        module = self.load_module()
        integration_ref = f"refs/heads/{state['manifest']['integration']['branch']}"
        committed = json.loads(
            self.git(
                repo,
                'show',
                f"{state['managed_refs'][integration_ref]}:.syncwheel/manifest.json",
            ).stdout
        )
        return module.canonical_json_digest(
            module.coordination_manifest_snapshot(committed, repo, state)
        )

    def republish_state_manifest_digest(self, repo, digest, coordination_id='default'):
        module = self.load_module()
        origin_ref = f'refs/heads/syncwheel/state/{coordination_id}'
        state_tip, state = self.remote_state(self.origin_of(repo), coordination_id)
        state['manifest_digest'] = digest
        rewritten = module.create_coordination_state_commit(
            repo, state, state.get('parent_state')
        )
        self.git(repo, 'push', '-q', '--force', 'origin', f'{rewritten}:{origin_ref}')
        return rewritten, state

    def origin_of(self, repo):
        return Path(
            self.git(repo, 'remote', 'get-url', 'origin').stdout.strip()
        )

    def prepare_legacy_digest_state(self, name, integration_membership='legacy'):
        origin = self.create_remote(name)
        repo = self.clone(origin, name)
        manifest = self.init_coordinated(
            repo, integration_membership=integration_membership
        )
        self.run_cli(repo, 'int', 'push')
        module = self.load_module()
        _, published = self.remote_state(origin)
        legacy_digest = self.legacy_state_manifest_digest(repo, published)
        self.assertNotEqual(legacy_digest, published['manifest_digest'])
        legacy_tip, legacy_state = self.republish_state_manifest_digest(
            repo, legacy_digest
        )
        self.assertEqual(
            legacy_digest, module.canonical_json_digest(legacy_state['manifest'])
        )
        return {
            'origin': origin,
            'repo': repo,
            'module': module,
            'manifest': manifest,
            'legacy_tip': legacy_tip,
            'legacy_state': legacy_state,
            'legacy_digest': legacy_digest,
            'control_digest': published['manifest_digest'],
        }

    def prepare_orphaned_digest_state(self, name='orphaned-digest'):
        """Reproduce a digest orphaned by a fast-forward repair over real manifest drift.

        A fast-forward repair preserves the parent's manifest_digest byte for
        byte (topology-only, by design). If the advance it repairs also
        carries a real change to the committed control manifest, the
        preserved digest stops matching either recognized form.
        """
        origin = self.create_remote(name)
        repo = self.clone(origin, name)
        self.init_coordinated(repo)
        self.run_cli(repo, 'int', 'push')
        parent_tip, parent = self.remote_state(origin)
        branch = 'integration/shared'
        ref = f'refs/heads/{branch}'
        recorded = parent['managed_refs'][ref]
        self.git(repo, 'switch', '-q', branch)
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        manifest_data = json.loads(manifest_path.read_text())
        manifest_data['orphaned_digest_fixture'] = True
        manifest_path.write_text(json.dumps(manifest_data, indent=2, sort_keys=True) + '\n')
        self.git(repo, 'add', '.syncwheel/manifest.json')
        self.git(repo, 'commit', '-qm', 'test: real control manifest drift on the integration tip')
        (repo / 'unreviewed-advance.txt').write_text('unreviewed advance\n')
        self.git(repo, 'add', 'unreviewed-advance.txt')
        self.git(repo, 'commit', '-qm', 'test: second advance commit')
        observed = self.git(repo, 'rev-parse', 'HEAD').stdout.strip()
        self.git(repo, 'push', '--no-verify', 'origin', branch)
        module = self.load_module()
        manifest, _ = module.load_manifest(repo)
        ff_plan, _ = module.coordination_repair_plan(
            repo, manifest, ref, module.COORDINATION_REPAIR_FAST_FORWARD_BACKEND,
        )
        self.assertEqual(ff_plan['status'], 'repair-required')
        ff_result = module.apply_coordination_repair_plan(repo, manifest, ff_plan)
        self.assertEqual(ff_result['status'], 'repaired')
        orphaned_tip, orphaned_state = self.remote_state(origin)
        self.assertEqual(orphaned_state['manifest_digest'], parent['manifest_digest'])
        committed = json.loads(
            self.git(repo, 'show', f'{observed}:.syncwheel/manifest.json').stdout
        )
        control_digest = module.manifest_digest(committed)
        self.assertNotEqual(orphaned_state['manifest_digest'], control_digest)
        return {
            'origin': origin,
            'repo': repo,
            'module': module,
            'manifest': manifest,
            'ref': ref,
            'parent_tip': parent_tip,
            'parent': parent,
            'recorded': recorded,
            'observed': observed,
            'orphaned_tip': orphaned_tip,
            'orphaned_state': orphaned_state,
            'orphaned_digest': orphaned_state['manifest_digest'],
            'control_digest': control_digest,
        }

    def prepare_additive_compose(self, name='additive-compose'):
        origin = self.create_remote(name)
        repo = self.clone(origin, name)
        self.init_coordinated(repo, integration_membership='required')
        integration_commits = []
        for index in (1, 2):
            path = repo / f'unmapped-{index}.txt'
            path.write_text(f'unmapped {index}\n')
            self.git(repo, 'add', path.name)
            self.git(repo, 'commit', '-qm', f'test: unmapped integration {index}')
            integration_commits.append(self.git(repo, 'rev-parse', 'HEAD').stdout.strip())
        self.run_cli(repo, 'int', 'push')
        base_tip, base_state = self.remote_state(origin)
        base_manifest = json.loads((repo / '.syncwheel' / 'manifest.json').read_text())

        orphan_tip = self.commit_on_branch(repo, 'pr/orphan', 'orphan.txt')
        self.run_cli(repo, 'stack', 'create', 'orphan', orphan_tip, '--branch', 'pr/orphan')
        self.run_cli(repo, 'stack', 'push', 'orphan')
        remote_tip, remote_state = self.remote_state(origin)

        (repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(base_manifest, indent=2) + '\n'
        )
        new_tip = self.commit_on_branch(repo, 'pr/new-stack', 'new-stack.txt')
        self.run_cli(
            repo, 'stack', 'create', 'new-stack', new_tip, '--branch', 'pr/new-stack'
        )
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(repo)
        plan, proposed, _ = module.coordination_compose_stack_plan(
            repo,
            manifest,
            'new-stack',
            base_tip,
            base_state['manifest_digest'],
        )
        return {
            'origin': origin,
            'repo': repo,
            'module': module,
            'manifest': manifest,
            'manifest_path': manifest_path,
            'base_tip': base_tip,
            'base_state': base_state,
            'remote_tip': remote_tip,
            'remote_state': remote_state,
            'orphan_tip': orphan_tip,
            'new_tip': new_tip,
            'integration_tip': remote_state['managed_refs'][
                'refs/heads/integration/shared'
            ],
            'integration_commits': integration_commits,
            'plan': plan,
            'proposed': proposed,
        }

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
        self.track_fixture_ignore_in_replay_base(first)
        self.run_cli(first, 'int', 'push')
        first_tip, first_state = self.remote_state(origin)
        module = self.load_module()
        integration_ref = 'refs/heads/integration/shared'
        integration_tip = first_state['managed_refs'][integration_ref]
        committed_manifest = json.loads(
            self.git(
                first, 'show', f'{integration_tip}:.syncwheel/manifest.json'
            ).stdout
        )

        self.assertIsNone(first_state['parent_state'])
        self.assertEqual(first_state['publication_scope'], 'integration')
        self.assertEqual(first_state['projection_status'], 'partial')
        self.assertIn('installation_id', first_state)
        serialized = json.dumps(first_state, sort_keys=True)
        self.assertNotIn(str(first), serialized)
        self.assertNotIn('syncwheel-first@example.com', serialized)
        self.assertNotIn('syncwheel_worktree_root', serialized)
        self.assertNotIn('syncwheel_tracking', first_state['manifest'])
        self.assertEqual(
            first_state['manifest_digest'],
            module.manifest_digest(committed_manifest),
        )
        self.assertNotEqual(
            first_state['manifest_digest'],
            module.canonical_json_digest(first_state['manifest']),
        )
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
        self.run_cli(
            second, 'int', 'rebuild',
            '--reason', 'adopt reviewed cross-clone control manifest',
        )
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

    def test_coordination_manifest_digest_uses_the_full_control_manifest(self):
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
            module.manifest_digest(manifest),
        )
        self.assertNotEqual(
            module.coordination_manifest_digest(manifest),
            module.canonical_json_digest(snapshot),
        )

    def test_state_digest_fetch_accepts_historical_integration_tip_object(self):
        module = self.load_module()
        tip = 'a' * 40
        committed = {'version': 2, 'integration': {'branch': 'integration/shared'}}
        state = {
            'manifest': {'integration': {'branch': 'integration/shared'}},
            'managed_refs': {'refs/heads/integration/shared': tip},
        }
        fetched = SimpleNamespace(returncode=0)

        with mock.patch.object(
            module, 'commit_exists', side_effect=[False, True],
        ), mock.patch.object(
            module, 'git', return_value=fetched,
        ) as git_call, mock.patch.object(
            module, 'manifest_from_tree', return_value=committed,
        ):
            observed = module.coordination_state_manifest_digest(
                self.tmp, state, 'origin'
            )

        self.assertEqual(observed, module.manifest_digest(committed))
        git_call.assert_called_once_with(
            self.tmp,
            'fetch',
            '--quiet',
            'origin',
            'refs/heads/integration/shared',
            check=False,
        )

    def test_guard_accepts_both_recorded_digest_forms_and_reports_which(self):
        fixture = self.prepare_legacy_digest_state('legacy-digest-forms')
        module = fixture['module']
        repo = fixture['repo']

        legacy = module.verify_coordination_state_manifest_digest(
            repo, fixture['legacy_state'], 'origin'
        )
        self.assertEqual(
            legacy['form'], module.COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT
        )
        self.assertEqual(legacy['recorded_digest'], fixture['legacy_digest'])
        self.assertEqual(legacy['control_manifest_digest'], fixture['control_digest'])

        control = dict(
            fixture['legacy_state'], manifest_digest=fixture['control_digest']
        )
        self.assertEqual(
            module.verify_coordination_state_manifest_digest(
                repo, control, 'origin'
            )['form'],
            module.COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST,
        )

        unrelated = dict(fixture['legacy_state'], manifest_digest='0' * 64)
        with self.assertRaisesRegex(
            module.SyncwheelError, 'does not match the control manifest'
        ):
            module.verify_coordination_state_manifest_digest(
                repo, unrelated, 'origin'
            )

    def test_publish_over_a_legacy_state_migrates_the_recorded_digest(self):
        fixture = self.prepare_legacy_digest_state('legacy-digest-publish')
        module = fixture['module']
        repo = fixture['repo']
        _, manifest_path = module.load_manifest(repo)
        self.git(repo, 'switch', '-q', 'integration/shared')
        (repo / 'after-legacy.txt').write_text('after legacy\n')
        self.git(repo, 'add', 'after-legacy.txt')
        self.git(repo, 'commit', '-qm', 'test: integration commit above a legacy state')

        published = self.run_cli(repo, 'int', 'push')
        self.assertIn('migrated the legacy coordination state', published.stdout)

        child_tip, child = self.remote_state(fixture['origin'])
        self.assertEqual(child['parent_state'], fixture['legacy_tip'])
        committed = json.loads(
            self.git(
                repo,
                'show',
                f"{child['managed_refs']['refs/heads/integration/shared']}"
                ':.syncwheel/manifest.json',
            ).stdout
        )
        self.assertEqual(child['manifest_digest'], module.manifest_digest(committed))
        self.assertNotEqual(child['manifest_digest'], fixture['legacy_digest'])
        self.assertEqual(
            module.verify_coordination_state_manifest_digest(
                repo, child, 'origin'
            )['form'],
            module.COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST,
        )

        migrations = [
            event for event in module.load_ledger_events(repo, manifest_path)
            if event['type'] == 'coordination_state_digest_migrated'
        ]
        self.assertEqual(len(migrations), 1)
        self.assertEqual(
            migrations[0]['payload'],
            {
                'coordination_id': 'default',
                'parent_state': fixture['legacy_tip'],
                'state_tip': child_tip,
                'from_form': module.COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT,
                'from_digest': fixture['legacy_digest'],
                'to_form': module.COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST,
                'to_digest': child['manifest_digest'],
            },
        )

        (repo / 'after-migration.txt').write_text('after migration\n')
        self.git(repo, 'add', 'after-migration.txt')
        self.git(repo, 'commit', '-qm', 'test: integration commit above a migrated state')
        again = self.run_cli(repo, 'int', 'push')
        self.assertNotIn('migrated the legacy coordination state', again.stdout)
        self.assertEqual(
            len([
                event for event in module.load_ledger_events(repo, manifest_path)
                if event['type'] == 'coordination_state_digest_migrated'
            ]),
            1,
        )

    def test_stack_push_over_a_legacy_state_migrates_the_recorded_digest(self):
        fixture = self.prepare_legacy_digest_state('legacy-digest-stack')
        module = fixture['module']
        repo = fixture['repo']

        stack_tip = self.commit_on_branch(repo, 'pr/legacy', 'legacy-stack.txt')
        self.run_cli(
            repo, 'stack', 'create', 'legacy', stack_tip, '--branch', 'pr/legacy'
        )
        pushed = self.run_cli(repo, 'stack', 'push', 'legacy')

        self.assertIn('migrated the legacy coordination state', pushed.stdout)
        _, state = self.remote_state(fixture['origin'])
        self.assertEqual(state['parent_state'], fixture['legacy_tip'])
        self.assertEqual(
            state['managed_refs']['refs/heads/pr/legacy'], stack_tip
        )
        self.assertNotEqual(state['manifest_digest'], fixture['legacy_digest'])
        self.assertEqual(
            module.verify_coordination_state_manifest_digest(
                repo, state, 'origin'
            )['form'],
            module.COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST,
        )

    def test_compose_accepts_a_legacy_base_and_remote_state_digest(self):
        fixture = self.prepare_legacy_digest_state(
            'legacy-digest-compose', integration_membership='required'
        )
        module = fixture['module']
        repo = fixture['repo']

        compose_tip = self.commit_on_branch(repo, 'pr/composed', 'composed.txt')
        self.run_cli(
            repo, 'stack', 'create', 'composed', compose_tip, '--branch', 'pr/composed'
        )
        manifest, _ = module.load_manifest(repo)
        plan, _, _ = module.coordination_compose_stack_plan(
            repo,
            manifest,
            'composed',
            fixture['legacy_tip'],
            fixture['legacy_digest'],
        )

        self.assertEqual(plan['status'], 'publish-required')
        self.assertEqual(plan['knownBaseStateTip'], fixture['legacy_tip'])
        self.assertEqual(plan['expectedRemoteStateTip'], fixture['legacy_tip'])
        self.assertEqual(plan['integrationManifestDigest'], fixture['control_digest'])

    def test_repair_migrates_a_legacy_state_digest_instead_of_reporting_noop(self):
        fixture = self.prepare_legacy_digest_state('legacy-digest-repair')
        module = fixture['module']
        repo = fixture['repo']
        manifest, _ = module.load_manifest(repo)
        ref = 'refs/heads/integration/shared'

        plan, _ = module.coordination_repair_plan(
            repo, manifest, ref, module.COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND
        )
        self.assertEqual(plan['status'], 'digest-migration-required')
        self.assertEqual(plan['expectedRecordedTip'], plan['expectedRemoteTip'])
        self.assertEqual(
            plan['stateDigestForm'],
            module.COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT,
        )
        self.assertEqual(plan['recordedManifestDigest'], fixture['legacy_digest'])
        self.assertEqual(plan['expectedManifestDigest'], fixture['control_digest'])

        github_plan, _ = module.coordination_repair_plan(repo, manifest, ref)
        self.assertEqual(github_plan['status'], 'digest-migration-required')
        with self.assertRaisesRegex(
            module.SyncwheelError, 'digest migration requires the'
        ):
            module.apply_coordination_repair_plan(repo, manifest, github_plan)

        pushes = []
        original_push = module.run_authorized_push

        def capture_push(repo_root, command, remote, refs, check=True):
            pushes.append({'command': command, 'refs': refs})
            return original_push(repo_root, command, remote, refs, check=check)

        with mock.patch.object(module, 'run_authorized_push', side_effect=capture_push):
            result = module.apply_coordination_repair_plan(repo, manifest, plan)

        self.assertEqual(result['status'], 'repaired')
        self.assertEqual(
            result['backend'], module.COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND
        )
        self.assertEqual(result['manifest_digest'], fixture['control_digest'])
        self.assertEqual(result['previous_manifest_digest'], fixture['legacy_digest'])
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]['refs'], [plan['stateRef']])
        self.assertFalse(any(f':{ref}' in item for item in pushes[0]['command']))

        child_tip, child = self.remote_state(fixture['origin'])
        self.assertEqual(child_tip, result['state_tip'])
        self.assertEqual(child['parent_state'], fixture['legacy_tip'])
        self.assertEqual(child['changed_refs'], {})
        self.assertEqual(child['manifest'], fixture['legacy_state']['manifest'])
        self.assertEqual(child['manifest_digest'], fixture['control_digest'])
        self.assertEqual(
            child['managed_refs'], fixture['legacy_state']['managed_refs']
        )
        self.assertEqual(
            child['repair_evidence']['proof'],
            module.COORDINATION_REPAIR_DIGEST_MIGRATION_PROOF,
        )
        self.assertEqual(
            child['repair_evidence']['recordedManifestDigest'],
            fixture['legacy_digest'],
        )
        self.assertEqual(
            module.verify_coordination_state_manifest_digest(
                repo, child, 'origin'
            )['form'],
            module.COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST,
        )

        settled, _ = module.coordination_repair_plan(
            repo, manifest, ref, module.COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND
        )
        self.assertEqual(settled['status'], 'noop')

    def test_repair_heals_an_orphaned_digest_instead_of_reporting_noop(self):
        fixture = self.prepare_orphaned_digest_state('orphaned-digest-heal')
        module = fixture['module']
        repo = fixture['repo']
        manifest = fixture['manifest']
        ref = fixture['ref']

        with self.assertRaisesRegex(
            module.SyncwheelError, 'does not match the control manifest'
        ):
            module.verify_coordination_state_manifest_digest(
                repo, fixture['orphaned_state'], 'origin'
            )
        stale_plan, _ = module.coordination_repair_plan(repo, manifest, ref)
        self.assertEqual(stale_plan['status'], 'digest-heal-required')

        self.git(repo, 'switch', '-q', 'integration/shared')
        (repo / 'after-orphan.txt').write_text('after orphan\n')
        self.git(repo, 'add', 'after-orphan.txt')
        self.git(repo, 'commit', '-qm', 'test: integration commit above an orphaned digest')
        blocked = self.run_cli(repo, 'int', 'push', expected=2)
        self.assertIn(
            'coordination state manifest_digest does not match the control manifest',
            blocked.stderr,
        )
        self.git(repo, 'reset', '-q', '--hard', 'HEAD~1')

        plan, _ = module.coordination_repair_plan(
            repo, manifest, ref, module.COORDINATION_REPAIR_DIGEST_HEAL_BACKEND,
        )
        self.assertEqual(plan['status'], 'digest-heal-required')
        self.assertEqual(plan['repairClass'], module.COORDINATION_REPAIR_DIGEST_HEAL_CLASS)
        self.assertEqual(plan['expectedRecordedTip'], plan['expectedRemoteTip'])
        self.assertEqual(
            plan['stateDigestForm'], module.COORDINATION_STATE_DIGEST_FORM_ORPHANED
        )
        self.assertEqual(plan['recordedManifestDigest'], fixture['orphaned_digest'])
        self.assertEqual(plan['expectedManifestDigest'], fixture['control_digest'])

        github_plan, _ = module.coordination_repair_plan(repo, manifest, ref)
        self.assertEqual(github_plan['status'], 'digest-heal-required')
        with self.assertRaisesRegex(module.SyncwheelError, 'digest heal requires the'):
            module.apply_coordination_repair_plan(repo, manifest, github_plan)

        result = module.apply_coordination_repair_plan(repo, manifest, plan)
        self.assertEqual(result['status'], 'repaired')
        self.assertEqual(result['backend'], module.COORDINATION_REPAIR_DIGEST_HEAL_BACKEND)
        self.assertEqual(result['manifest_digest'], fixture['control_digest'])
        self.assertEqual(result['previous_manifest_digest'], fixture['orphaned_digest'])

        child_tip, child = self.remote_state(fixture['origin'])
        self.assertEqual(child_tip, result['state_tip'])
        self.assertEqual(child['parent_state'], fixture['orphaned_tip'])
        self.assertEqual(child['changed_refs'], {})
        self.assertEqual(child['manifest'], fixture['orphaned_state']['manifest'])
        self.assertEqual(child['manifest_digest'], fixture['control_digest'])
        self.assertEqual(child['managed_refs'], fixture['orphaned_state']['managed_refs'])
        self.assertEqual(
            child['repair_evidence']['proof'], module.COORDINATION_REPAIR_DIGEST_HEAL_PROOF
        )
        self.assertEqual(
            child['repair_evidence']['recordedManifestDigest'], fixture['orphaned_digest']
        )
        self.assertEqual(
            module.verify_coordination_state_manifest_digest(repo, child, 'origin')['form'],
            module.COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST,
        )

        settled, _ = module.coordination_repair_plan(
            repo, manifest, ref, module.COORDINATION_REPAIR_DIGEST_HEAL_BACKEND,
        )
        self.assertEqual(settled['status'], 'noop')

        manifest_path = repo / '.syncwheel' / 'manifest.json'
        healed = [
            event for event in module.load_ledger_events(repo, manifest_path)
            if event['type'] == 'coordination_state_digest_healed'
        ]
        self.assertEqual(len(healed), 1)
        self.assertEqual(healed[0]['payload']['from_digest'], fixture['orphaned_digest'])
        self.assertEqual(healed[0]['payload']['to_digest'], fixture['control_digest'])
        self.assertEqual(
            healed[0]['payload']['repair_class'], module.COORDINATION_REPAIR_DIGEST_HEAL_CLASS
        )

    def test_repair_prefers_topology_repair_over_digest_heal(self):
        """A ref that also needs its own topology fixed must not be healed alone."""
        fixture = self.prepare_orphaned_digest_state('orphaned-digest-ref-drift')
        module = fixture['module']
        repo = fixture['repo']
        manifest = fixture['manifest']
        ref = fixture['ref']

        (repo / 'further-unreviewed-drift.txt').write_text('further drift\n')
        self.git(repo, 'add', 'further-unreviewed-drift.txt')
        self.git(repo, 'commit', '-qm', 'test: ref drifts again after the digest was orphaned')
        self.git(repo, 'push', '--no-verify', 'origin', 'integration/shared')

        plan, _ = module.coordination_repair_plan(
            repo, manifest, ref, module.COORDINATION_REPAIR_DIGEST_HEAL_BACKEND,
        )
        self.assertEqual(plan['status'], 'repair-required')
        self.assertNotIn('repairClass', plan)

    def test_repair_stays_fatal_when_the_registered_tip_is_unreadable(self):
        """An unreadable registered tip must never be silently offered as healable."""
        fixture = self.prepare_orphaned_digest_state('orphaned-digest-unreadable')
        module = fixture['module']
        repo = fixture['repo']
        manifest = fixture['manifest']
        ref = fixture['ref']

        origin = fixture['origin']
        state_tip, state = self.remote_state(origin)
        broken = dict(state)
        broken['managed_refs'] = dict(state['managed_refs'])
        broken['managed_refs'][ref] = '0' * 40
        origin_ref = 'refs/heads/syncwheel/state/default'
        rewritten = module.create_coordination_state_commit(
            repo, broken, broken.get('parent_state')
        )
        self.git(repo, 'push', '-q', '--force', 'origin', f'{rewritten}:{origin_ref}')

        with self.assertRaisesRegex(
            module.SyncwheelError, 'coordination state integration tip object is unavailable'
        ):
            module.coordination_repair_plan(
                repo, manifest, ref, module.COORDINATION_REPAIR_DIGEST_HEAL_BACKEND,
            )

    def test_v2_manifest_and_snapshot_both_reject_derived_provenance(self):
        module = self.load_module()
        origin = self.create_remote('v2-derived-provenance')
        repo = self.clone(origin, 'v2-derived-provenance')
        manifest = self.init_coordinated(
            repo, integration_membership='required'
        )
        manifest['integration']['derived_provenance'] = []
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        snapshot = module.coordination_manifest_snapshot(
            {**manifest, 'integration': {
                key: value for key, value in manifest['integration'].items()
                if key != 'derived_provenance'
            }}
        )
        snapshot['integration']['derived_provenance'] = []

        with self.assertRaisesRegex(
            module.SyncwheelError,
            'integration.derived_provenance requires manifest version 3',
        ):
            module.load_manifest(repo, manifest_path)
        with self.assertRaisesRegex(
            module.SyncwheelError,
            'integration.derived_provenance requires version 3',
        ):
            module.validate_coordination_snapshot_refs(snapshot)

    def test_derived_paths_survive_snapshot_and_compose_classifies_published_derived_tip(self):
        module = self.load_module()
        origin = self.create_remote('derived-paths-compose')
        repo = self.clone(origin, 'derived-paths-compose')
        self.init_coordinated(repo, integration_membership='required')
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['version'] = 3
        manifest['integration']['derived_paths'] = ['locks/']
        manifest.setdefault('channels', [])
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git(repo, 'add', '.syncwheel/manifest.json')
        self.git(repo, 'commit', '-qm', 'test: configure derived projections')
        (repo / 'locks').mkdir()
        (repo / 'locks' / 'codex.lock').write_text('derived\n')
        self.git(repo, 'add', 'locks/codex.lock')
        blob = self.git(repo, 'rev-parse', ':locks/codex.lock').stdout.strip()
        paths_digest = module.derived_projection_paths_digest(
            {'locks/codex.lock': blob}
        )
        self.git(
            repo, 'commit', '-q', '-m', 'test: publish derived projection', '-m',
            'Syncwheel-Derived-Projection: coordination-derived\n'
            f'Syncwheel-Derived-Paths: {paths_digest}',
        )
        derived_tip = self.git(repo, 'rev-parse', 'HEAD').stdout.strip()
        record = {
            'operation_id': 'coordination-derived',
            'commit': derived_tip,
            'paths': ['locks/codex.lock'],
            'paths_digest': paths_digest,
            'composition_digest': module.integration_composition_digest(manifest),
        }
        module.record_common_derived_provenance(repo, manifest, record)
        module.append_ledger_event(
            repo, 'revision_provider_derived_commit', record, manifest_path
        )
        self.run_cli(repo, 'int', 'push')
        base_tip, base_state = self.remote_state(origin)
        base_manifest = json.loads(manifest_path.read_text())

        self.assertEqual(
            base_state['manifest']['integration']['derived_paths'], ['locks/']
        )
        self.assertEqual(
            base_state['manifest']['integration']['derived_provenance'],
            [{
                'operation_id': 'coordination-derived',
                'commit': derived_tip,
                'paths': ['locks/codex.lock'],
                'paths_digest': paths_digest,
                'composition_digest': module.integration_composition_digest(manifest),
            }],
        )
        orphan_tip = self.commit_on_branch(repo, 'pr/derived-orphan', 'orphan.txt')
        self.run_cli(
            repo, 'stack', 'create', 'derived-orphan', orphan_tip,
            '--branch', 'pr/derived-orphan',
        )
        self.run_cli(repo, 'stack', 'push', 'derived-orphan')
        manifest_path.write_text(json.dumps(base_manifest, indent=2) + '\n')
        new_tip = self.commit_on_branch(repo, 'pr/derived-local', 'local.txt')
        self.run_cli(
            repo, 'stack', 'create', 'derived-local', new_tip,
            '--branch', 'pr/derived-local',
        )
        local_manifest, _ = module.load_manifest(repo, manifest_path)

        plan, _proposed, _remote = module.coordination_compose_stack_plan(
            repo,
            local_manifest,
            'derived-local',
            base_tip,
            base_state['manifest_digest'],
        )

        integration_tip = self.git(
            repo, 'rev-parse', 'integration/shared'
        ).stdout.strip()
        self.assertEqual(plan['status'], 'publish-required')
        self.assertEqual(plan['expectedIntegrationTip'], integration_tip)
        self.git(
            repo, 'merge-base', '--is-ancestor', derived_tip, integration_tip,
        )
        self.assertEqual(plan['unmappedIntegrationCommits'], [])
        self.assertEqual(
            plan['composedSnapshot']['integration']['derived_paths'], ['locks/']
        )
        self.assertEqual(
            plan['composedSnapshot']['integration']['derived_provenance'],
            base_state['manifest']['integration']['derived_provenance'],
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

    def prepare_control_manifest_sigkill_recovery(self, name, stage):
        origin = self.create_remote(name=f'{name}-origin')
        repo = self.clone(origin, name)
        self.init_coordinated(repo)
        # Keep the fixture's generated ignore policy in the replay base so a
        # manifest-less checkout is still clean enough for the retry itself.
        self.git(repo, 'add', '.gitignore')
        self.git(repo, 'commit', '-q', '-m', 'test: track Syncwheel ignore policy')
        self.git(repo, 'push', '-q', 'origin', 'HEAD:main')
        feature_sha = self.commit_on_branch(
            repo, 'pr/feature-a', f'{name}-feature.txt'
        )
        self.run_cli(
            repo,
            'stack', 'create', 'feature-a', feature_sha,
            '--branch', 'pr/feature-a',
        )
        self.git(repo, 'switch', '-q', 'integration/shared')
        self.run_cli(repo, 'int', 'push')
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        self.assertTrue(manifest_path.exists())

        killed = self.run_cli(
            repo,
            'int', 'rebuild', '--in-place',
            '--reason', f'interrupt control persistence at {stage}',
            expected=-signal.SIGKILL,
            extra_env={'SYNCWHEEL_TEST_CONTROL_MANIFEST_SIGKILL': stage},
        )
        self.assertEqual(killed.returncode, -signal.SIGKILL)
        self.assertFalse(manifest_path.exists())
        module = self.load_module()
        events = module.load_ledger_events(repo, manifest_path)
        pending = module.pending_control_manifest_intents(events)
        self.assertEqual(len(pending), 1)
        intent = pending[0]['payload']
        current_tip = self.git(
            repo, 'rev-parse', 'integration/shared'
        ).stdout.strip()
        expected_tip = (
            intent['replay_tip'] if stage == 'intent_saved'
            else intent['expected_control_commit']
        )
        self.assertEqual(current_tip, expected_tip)
        return origin, repo, feature_sha, intent

    def test_entrypoint_retries_recover_after_ref_sigkill_removed_manifest(self):
        retries = {
            'stack-push': ('stack', 'push', 'feature-a'),
            'int-rebuild': (
                'int', 'rebuild', '--in-place',
                '--reason', 'retry interrupted integration rebuild',
            ),
            'int-push': ('int', 'push'),
        }
        for name, retry in retries.items():
            with self.subTest(retry=name):
                origin, repo, feature_sha, intent = (
                    self.prepare_control_manifest_sigkill_recovery(
                        f'ref-kill-{name}', 'ref_updated'
                    )
                )

                self.run_cli(repo, *retry)

                module = self.load_module()
                recovered, manifest_path = module.load_manifest(repo)
                self.assertIn('feature-a', module.stack_map(recovered))
                self.assertEqual(
                    module.pending_control_manifest_intents(
                        module.load_ledger_events(repo, manifest_path)
                    ),
                    [],
                )
                receipts = [
                    event for event in module.load_ledger_events(repo, manifest_path)
                    if event['type'] == 'manifest_saved'
                    and event['payload'].get('operation_id') == intent['operation_id']
                ]
                self.assertEqual(len(receipts), 1)
                self.assertEqual(
                    self.git(repo, 'status', '--porcelain', '--untracked-files=no').stdout,
                    '',
                )
                if name == 'stack-push':
                    _state_tip, state = self.remote_state(origin)
                    self.assertEqual(
                        state['managed_refs']['refs/heads/pr/feature-a'], feature_sha
                    )
                    self.assertEqual(
                        state['managed_refs']['refs/heads/integration/shared'],
                        intent['expected_control_commit'],
                    )

    def test_entrypoint_retries_discover_intent_before_replay_tip_subject(self):
        retries = {
            'stack-push': ('stack', 'push', 'feature-a'),
            'int-rebuild': (
                'int', 'rebuild', '--in-place',
                '--reason', 'retry intent-first integration rebuild',
            ),
            'int-push': ('int', 'push'),
        }
        for name, retry in retries.items():
            with self.subTest(retry=name):
                origin, repo, feature_sha, intent = (
                    self.prepare_control_manifest_sigkill_recovery(
                        f'intent-kill-{name}', 'intent_saved'
                    )
                )
                replay_subject = self.git(
                    repo, 'show', '-s', '--format=%s', intent['replay_tip']
                ).stdout.strip()
                self.assertNotEqual(
                    replay_subject, 'chore: restore Syncwheel control manifest'
                )

                self.run_cli(repo, *retry)

                module = self.load_module()
                recovered, manifest_path = module.load_manifest(repo)
                self.assertIn('feature-a', module.stack_map(recovered))
                events = module.load_ledger_events(repo, manifest_path)
                self.assertEqual(module.pending_control_manifest_intents(events), [])
                receipts = [
                    event for event in events
                    if event['type'] == 'manifest_saved'
                    and event['payload'].get('operation_id') == intent['operation_id']
                ]
                self.assertEqual(len(receipts), 1)
                if name == 'stack-push':
                    _state_tip, state = self.remote_state(origin)
                    self.assertEqual(
                        state['managed_refs']['refs/heads/pr/feature-a'], feature_sha
                    )
                    self.assertEqual(
                        state['managed_refs']['refs/heads/integration/shared'],
                        intent['expected_control_commit'],
                    )

    def test_external_manifest_writers_share_branch_lock_and_never_rewind_cas(self):
        origin = self.create_remote(name='control-lock-origin')
        repo = self.clone(origin, 'control-lock-writers')
        self.init_coordinated(repo)
        self.git(repo, 'switch', '-q', 'integration/shared')
        module = self.load_module()
        baseline, _ = module.load_manifest(repo)
        first_manifest = json.loads(json.dumps(baseline))
        first_manifest['defaults']['replay_mode'] = 'plumbing'
        second_manifest = json.loads(json.dumps(baseline))
        second_manifest['defaults']['replay_mode'] = 'ephemeral'
        first_path = self.tmp / 'first-external-manifest.json'
        second_path = self.tmp / 'second-external-manifest.json'
        first_path.write_text(json.dumps(first_manifest, indent=2) + '\n')
        second_path.write_text(json.dumps(second_manifest, indent=2) + '\n')
        (repo / '.syncwheel' / 'manifest.json').unlink()
        runner = self.tmp / 'control-manifest-writer.py'
        runner.write_text(
            'import importlib.util, sys\n'
            'from pathlib import Path\n'
            'cli, repo, manifest_path = map(Path, sys.argv[1:4])\n'
            'spec = importlib.util.spec_from_file_location("syncwheel_writer", cli)\n'
            'module = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(module)\n'
            'manifest, _ = module.load_manifest(repo, manifest_path)\n'
            'branch = manifest["integration"]["branch"]\n'
            'replay_tip = module.ref_tip(repo, branch)\n'
            'with module.manifest_write_transaction(repo, manifest_path, "test-writer"):\n'
            '    module.restore_control_manifest_after_integration_rebuild(\n'
            '        repo, manifest_path, manifest, replay_tip, "control",\n'
            '        reason="concurrent external writer", command="test writer",\n'
            '    )\n'
        )
        ready = self.tmp / 'first-ref-updated'
        release = self.tmp / 'release-first-writer'
        second_ready = self.tmp / 'second-ref-updated'
        second_release = self.tmp / 'release-second-writer'
        first_env = dict(os.environ)
        first_env.update(self.environment)
        first_env.update({
            'SYNCWHEEL_TEST_CONTROL_MANIFEST_PAUSE': 'ref_updated',
            'SYNCWHEEL_TEST_CONTROL_MANIFEST_READY': str(ready),
            'SYNCWHEEL_TEST_CONTROL_MANIFEST_RELEASE': str(release),
        })
        argv = ['python3', str(runner), str(CLI), str(repo)]
        first = subprocess.Popen(
            [*argv, str(first_path)], cwd=repo, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=first_env,
        )
        try:
            self.wait_for_path(ready, first)
            first_control = self.git(
                repo, 'rev-parse', 'integration/shared'
            ).stdout.strip()
            second = subprocess.Popen(
                [*argv, str(second_path)], cwd=repo, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={
                    **os.environ,
                    **self.environment,
                    'SYNCWHEEL_TEST_CONTROL_MANIFEST_PAUSE': 'ref_updated',
                    'SYNCWHEEL_TEST_CONTROL_MANIFEST_READY': str(second_ready),
                    'SYNCWHEEL_TEST_CONTROL_MANIFEST_RELEASE': str(second_release),
                },
            )
            time.sleep(0.5)
            self.assertIsNone(second.poll(), 'second writer bypassed repository+branch lock')
            self.assertFalse(
                second_ready.exists(),
                'second writer reached its ref CAS while the first held the branch lock',
            )
            release.write_text('continue\n')
            first_stdout, first_stderr = first.communicate(timeout=20)
            self.wait_for_path(second_ready, second)
            second_release.write_text('continue\n')
            second_stdout, second_stderr = second.communicate(timeout=20)
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()
            if 'second' in locals() and second.poll() is None:
                second.kill()
                second.communicate()

        self.assertEqual(first.returncode, 0, (first_stdout, first_stderr))
        self.assertEqual(second.returncode, 0, (second_stdout, second_stderr))
        final_tip = self.git(repo, 'rev-parse', 'integration/shared').stdout.strip()
        self.assertNotEqual(final_tip, first_control)
        self.assertEqual(
            self.git(repo, 'rev-parse', f'{final_tip}^').stdout.strip(), first_control
        )
        committed = module.manifest_from_tree(
            repo, final_tip, module.integration_manifest_path(repo)
        )
        self.assertEqual(
            module.manifest_digest(committed), module.manifest_digest(second_manifest)
        )

    def test_stack_push_retry_completes_interrupted_control_manifest_before_publish(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'stack-push-control-retry')
        self.init_coordinated(repo)
        feature_sha = self.commit_on_branch(
            repo, 'pr/feature-a', 'feature-a-control-retry.txt'
        )
        self.run_cli(
            repo,
            'stack', 'create', 'feature-a', feature_sha,
            '--branch', 'pr/feature-a',
        )
        self.git(repo, 'switch', '-q', 'integration/shared')
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(repo)
        args = SimpleNamespace(
            repo=str(repo), manifest=None, personal=None, stack='feature-a',
            remote=None, dry_run=False, force_with_lease=False,
            git_args=[], command='stack',
        )

        def crash_after_ref(stage):
            if stage == 'ref_updated':
                raise RuntimeError('simulated crash after ref CAS')

        with module.manifest_write_transaction(repo, manifest_path):
            with mock.patch.object(
                module, 'control_manifest_io_checkpoint', side_effect=crash_after_ref,
            ):
                with self.assertRaisesRegex(RuntimeError, 'after ref CAS'):
                    module.command_stack_push(args)

        control_commit = self.git(
            repo, 'rev-parse', 'integration/shared'
        ).stdout.strip()
        interrupted_status = self.git(
            repo, 'status', '--porcelain', '--untracked-files=no'
        ).stdout
        self.assertIn('.syncwheel/manifest.json', interrupted_status)
        interrupted_events = module.load_ledger_events(repo, manifest_path)
        self.assertEqual(
            interrupted_events[-1]['type'], 'control_manifest_persistence_intent'
        )
        self.assertFalse(any(
            event['type'] == 'manifest_saved'
            and event['payload'].get('control_commit') == control_commit
            for event in interrupted_events
        ))

        self.run_cli(repo, 'stack', 'push', 'feature-a')

        self.assertEqual(
            self.git(repo, 'status', '--porcelain', '--untracked-files=no').stdout,
            '',
        )
        events = module.load_ledger_events(repo, manifest_path)
        receipts = [
            event for event in events
            if event['type'] == 'manifest_saved'
            and event['payload'].get('control_commit') == control_commit
        ]
        self.assertEqual(len(receipts), 1)
        _state_tip, state = self.remote_state(origin)
        self.assertEqual(
            state['managed_refs']['refs/heads/integration/shared'], control_commit
        )
        self.assertEqual(
            state['managed_refs']['refs/heads/pr/feature-a'], feature_sha
        )

    def test_coordinated_publish_retry_recovers_control_manifest_before_remote_state(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'coordinated-control-retry')
        self.init_coordinated(repo)
        self.git(repo, 'switch', '-q', 'integration/shared')
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(repo)

        def crash_after_ref(stage):
            if stage == 'ref_updated':
                raise RuntimeError('simulated crash after ref CAS')

        with module.manifest_write_transaction(repo, manifest_path):
            with mock.patch.object(
                module, 'control_manifest_io_checkpoint', side_effect=crash_after_ref,
            ):
                with self.assertRaisesRegex(RuntimeError, 'after ref CAS'):
                    module.coordinated_publish(
                        repo, manifest, manifest_path, {},
                        'control-retry', 'partial',
                    )
        control_commit = self.git(
            repo, 'rev-parse', 'integration/shared'
        ).stdout.strip()

        with module.manifest_write_transaction(repo, manifest_path):
            result = module.coordinated_publish(
                repo, manifest, manifest_path, {},
                'control-retry', 'partial',
            )

        self.assertEqual(result['status'], 'published')
        self.assertEqual(
            self.git(repo, 'status', '--porcelain', '--untracked-files=no').stdout,
            '',
        )
        receipts = [
            event for event in module.load_ledger_events(repo, manifest_path)
            if event['type'] == 'manifest_saved'
            and event['payload'].get('control_commit') == control_commit
        ]
        self.assertEqual(len(receipts), 1)
        _state_tip, state = self.remote_state(origin)
        self.assertEqual(
            state['managed_refs']['refs/heads/integration/shared'], control_commit
        )

    def test_int_push_retry_recovers_control_manifest_before_publish(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'int-push-control-retry')
        self.init_coordinated(repo)
        self.git(repo, 'switch', '-q', 'integration/shared')
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(repo)
        args = SimpleNamespace(
            repo=str(repo), manifest=None, personal=None, remote=None,
            dry_run=False, force_with_lease=False, git_args=[], command='int',
        )

        def crash_after_ref(stage):
            if stage == 'ref_updated':
                raise RuntimeError('simulated crash after ref CAS')

        with module.manifest_write_transaction(repo, manifest_path):
            with mock.patch.object(
                module, 'control_manifest_io_checkpoint', side_effect=crash_after_ref,
            ):
                with self.assertRaisesRegex(RuntimeError, 'after ref CAS'):
                    module.command_int_push(args)
        control_commit = self.git(
            repo, 'rev-parse', 'integration/shared'
        ).stdout.strip()

        self.run_cli(repo, 'int', 'push')

        self.assertEqual(
            self.git(repo, 'status', '--porcelain', '--untracked-files=no').stdout,
            '',
        )
        receipts = [
            event for event in module.load_ledger_events(repo, manifest_path)
            if event['type'] == 'manifest_saved'
            and event['payload'].get('control_commit') == control_commit
        ]
        self.assertEqual(len(receipts), 1)
        _state_tip, state = self.remote_state(origin)
        self.assertEqual(
            state['managed_refs']['refs/heads/integration/shared'], control_commit
        )

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

    def test_publish_allows_an_exact_stack_rebase_after_the_base_advances(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'exact-stack-rebase')
        self.init_coordinated(repo)

        source = self.commit_on_branch(repo, 'scratch/feature-rebase', 'feature.txt')
        self.git(repo, 'branch', 'pr/feature-rebase', source)
        self.run_cli(
            repo,
            'stack',
            'create',
            'feature-rebase',
            source,
            '--branch',
            'pr/feature-rebase',
        )
        self.run_cli(repo, 'stack', 'push', 'feature-rebase')

        advance = self.clone(origin, 'advance-base')
        (advance / 'base.txt').write_text('advanced base\n')
        self.git(advance, 'add', 'base.txt')
        self.git(advance, 'commit', '-q', '-m', 'chore: advance base')
        self.git(advance, 'push', 'origin', 'main')
        self.git(repo, 'fetch', 'origin')

        self.run_cli(
            repo,
            'reconcile',
            '--apply',
            '--push',
            '--stack',
            'feature-rebase',
            '--skip-integration',
        )

        rebased = self.git(repo, 'rev-parse', 'pr/feature-rebase').stdout.strip()
        self.assertNotEqual(rebased, source)
        self.assertEqual(
            self.git(repo, 'show', 'pr/feature-rebase:feature.txt').stdout,
            'scratch/feature-rebase\n',
        )
        self.assertEqual(
            self.git(repo, 'show', 'pr/feature-rebase:base.txt').stdout,
            'advanced base\n',
        )
        _, state = self.remote_state(origin)
        self.assertEqual(
            state['managed_refs']['refs/heads/pr/feature-rebase'],
            rebased,
        )
        stack = next(item for item in state['manifest']['stacks'] if item['id'] == 'feature-rebase')
        self.assertEqual(stack['commits'], [rebased])

    def test_exact_stack_rebase_proof_rejects_changed_source_content(self):
        origin = self.create_remote()
        repo = self.clone(origin, 'rebase-content-drift')
        self.init_coordinated(repo)

        source = self.commit_on_branch(repo, 'scratch/content-drift', 'feature.txt')
        self.git(repo, 'branch', 'pr/content-drift', source)
        self.run_cli(
            repo,
            'stack',
            'create',
            'content-drift',
            source,
            '--branch',
            'pr/content-drift',
        )
        self.run_cli(repo, 'stack', 'push', 'content-drift')

        advance = self.clone(origin, 'advance-content-base')
        (advance / 'base.txt').write_text('advanced base\n')
        self.git(advance, 'add', 'base.txt')
        self.git(advance, 'commit', '-q', '-m', 'chore: advance base')
        self.git(advance, 'push', 'origin', 'main')
        self.git(repo, 'fetch', 'origin')

        self.git(repo, 'switch', '-q', '-c', 'scratch/changed-source', 'origin/main')
        (repo / 'feature.txt').write_text('changed source content\n')
        self.git(repo, 'add', 'feature.txt')
        self.git(repo, 'commit', '-q', '-m', 'feat: changed source content')
        changed = self.git(repo, 'rev-parse', 'HEAD').stdout.strip()
        self.git(repo, 'switch', '-q', 'integration/shared')
        self.git(repo, 'branch', '-f', 'pr/content-drift', changed)

        manifest_path = repo / '.syncwheel' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['stacks'][0]['commits'] = [changed]
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        module = self.load_module()
        config = module.coordination_config(manifest)
        state = module.read_remote_coordination_state(
            repo,
            config,
            fetch=True,
            local_manifest_version=manifest['version'],
        )['state']
        self.assertFalse(module.coordination_stack_ref_is_exact_rebase(
            repo,
            manifest,
            state['manifest'],
            'content-drift',
            state['managed_refs']['refs/heads/pr/content-drift'],
        ))

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
        self.track_fixture_ignore_in_replay_base(first)
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
        self.run_cli(
            second, 'int', 'rebuild',
            '--reason', 'adopt reviewed cross-domain control manifest',
        )
        failure = self.run_cli(second, 'int', 'push', expected=2)
        self.assertIn('already owned by another coordination domain', failure.stderr)

    def test_stale_manifest_cannot_drop_a_remotely_published_stack(self):
        origin = self.create_remote()
        stale = self.clone(origin, 'stale')
        self.init_coordinated(stale)
        self.track_fixture_ignore_in_replay_base(stale)

        current = self.clone(origin, 'current')
        self.init_coordinated(current)

        # Both clones establish the same deterministic initial control object,
        # so each ledger has a local receipt before either authors a successor.
        self.run_cli(stale, 'int', 'push')
        self.run_cli(current, 'int', 'push')
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

    def test_tree_equivalent_repair_pushes_only_append_only_state(self):
        fixture = self.prepare_tree_equivalent_repair()
        module = fixture['module']
        plan = fixture['plan']
        self.assertEqual(plan['status'], 'repair-required')
        self.assertEqual(plan['proof'], module.COORDINATION_REPAIR_TREE_EQUIVALENT_PROOF)
        self.assertEqual(plan['expectedRecordedTree'], fixture['tree'])
        self.assertEqual(plan['expectedRemoteTree'], fixture['tree'])

        pushes = []
        original_push = module.run_authorized_push

        def capture_push(repo_root, command, remote, refs, check=True):
            pushes.append({'command': command, 'remote': remote, 'refs': refs})
            return original_push(repo_root, command, remote, refs, check=check)

        with mock.patch.object(module, 'run_authorized_push', side_effect=capture_push):
            result = module.apply_coordination_repair_plan(
                fixture['repo'], fixture['manifest'], plan
            )

        self.assertEqual(result['status'], 'repaired')
        self.assertEqual(result['backend'], module.COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND)
        self.assertEqual(result['proof'], module.COORDINATION_REPAIR_TREE_EQUIVALENT_PROOF)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]['refs'], [plan['stateRef']])
        self.assertNotIn('--atomic', pushes[0]['command'])
        self.assertEqual(
            pushes[0]['command'][-1],
            f"{result['state_tip']}:{plan['stateRef']}",
        )
        self.assertFalse(any(f":{fixture['ref']}" in item for item in pushes[0]['command']))

        child_tip, child = self.remote_state(fixture['origin'])
        self.assertEqual(child_tip, result['state_tip'])
        self.assertEqual(child['parent_state'], fixture['parent_tip'])
        self.assertEqual(child['managed_refs'][fixture['ref']], fixture['observed'])
        self.assertEqual(child['changed_refs'], {})
        self.assertEqual(child['repair_evidence']['planDigest'], plan['planDigest'])
        self.assertEqual(child['repair_evidence']['tree'], fixture['tree'])
        for key in ('manifest', 'manifest_digest', 'tombstones'):
            self.assertEqual(child[key], fixture['parent'][key])
        for managed_ref, tip in fixture['parent']['managed_refs'].items():
            if managed_ref != fixture['ref']:
                self.assertEqual(child['managed_refs'][managed_ref], tip)
        self.assertEqual(
            module.remote_ref_tips(fixture['repo'], 'origin', [fixture['ref']])[fixture['ref']],
            fixture['observed'],
        )

    def test_tree_equivalent_repair_rejects_tree_difference_before_plan(self):
        origin = self.create_remote('tree-different-repair')
        repo = self.clone(origin, 'tree-different-repair')
        self.init_coordinated(repo)
        self.run_cli(repo, 'int', 'push')
        branch = 'integration/shared'
        ref = f'refs/heads/{branch}'
        self.git(repo, 'switch', '-q', branch)
        (repo / 'different.txt').write_text('different tree\n')
        self.git(repo, 'add', 'different.txt')
        self.git(repo, 'commit', '-qm', 'test: different integration tree')
        self.git(repo, 'push', '--no-verify', 'origin', branch)
        module = self.load_module()
        manifest, _ = module.load_manifest(repo)
        with self.assertRaisesRegex(module.SyncwheelError, 'managed ref trees differ'):
            module.coordination_repair_plan(
                repo,
                manifest,
                ref,
                module.COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND,
            )
        github_plan, _ = module.coordination_repair_plan(repo, manifest, ref, 'github-lock')
        self.assertEqual(github_plan['status'], 'repair-required')
        with self.assertRaisesRegex(module.SyncwheelError, 'GitHub branch locks can be bypassed'):
            module.apply_coordination_repair_plan(repo, manifest, github_plan)

    def test_fast_forward_repair_records_bounded_ancestry_and_pushes_only_state(self):
        fixture = self.prepare_fast_forward_repair()
        module = fixture['module']
        plan = fixture['plan']
        self.assertEqual(plan['status'], 'repair-required')
        self.assertEqual(plan['proof'], module.COORDINATION_REPAIR_FAST_FORWARD_PROOF)
        self.assertEqual(plan['expectedAdvanceCommits'], fixture['advance_commits'])
        self.assertEqual(plan['expectedAdvanceCommitCount'], 2)
        self.assertNotEqual(plan['expectedRecordedTree'], plan['expectedRemoteTree'])
        with mock.patch.object(module, 'COORDINATION_REPAIR_MAX_ADVANCE_COMMITS', 1):
            with self.assertRaisesRegex(module.SyncwheelError, 'bounded commit interval'):
                module.coordination_repair_plan(
                    fixture['repo'],
                    fixture['manifest'],
                    fixture['ref'],
                    module.COORDINATION_REPAIR_FAST_FORWARD_BACKEND,
                )

        tampered = json.loads(json.dumps(plan))
        tampered['expectedAdvanceCommitCount'] = 3
        unsigned = {key: value for key, value in tampered.items() if key != 'planDigest'}
        tampered['planDigest'] = module.canonical_json_digest(unsigned)
        with self.assertRaisesRegex(module.SyncwheelError, 'fast-forward proof is invalid'):
            module.apply_coordination_repair_plan(
                fixture['repo'], fixture['manifest'], tampered
            )

        pushes = []
        original_push = module.run_authorized_push

        def capture_push(repo_root, command, remote, refs, check=True):
            pushes.append({'command': command, 'remote': remote, 'refs': refs})
            return original_push(repo_root, command, remote, refs, check=check)

        with mock.patch.object(module, 'run_authorized_push', side_effect=capture_push):
            result = module.apply_coordination_repair_plan(
                fixture['repo'], fixture['manifest'], plan
            )

        self.assertEqual(result['status'], 'repaired')
        self.assertEqual(result['backend'], module.COORDINATION_REPAIR_FAST_FORWARD_BACKEND)
        self.assertEqual(result['proof'], module.COORDINATION_REPAIR_FAST_FORWARD_PROOF)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]['refs'], [plan['stateRef']])
        self.assertFalse(any(f":{fixture['ref']}" in item for item in pushes[0]['command']))

        child_tip, child = self.remote_state(fixture['origin'])
        evidence = child['repair_evidence']
        self.assertEqual(child_tip, result['state_tip'])
        self.assertEqual(child['parent_state'], fixture['parent_tip'])
        self.assertEqual(child['managed_refs'][fixture['ref']], fixture['observed'])
        self.assertEqual(child['changed_refs'], {})
        self.assertEqual(evidence['planDigest'], plan['planDigest'])
        self.assertEqual(evidence['recordedTree'], plan['expectedRecordedTree'])
        self.assertEqual(evidence['observedTree'], plan['expectedRemoteTree'])
        self.assertEqual(evidence['advanceCommitCount'], 2)
        self.assertEqual(evidence['advanceCommitsDigest'], plan['expectedAdvanceCommitsDigest'])
        for key in ('manifest', 'manifest_digest', 'tombstones'):
            self.assertEqual(child[key], fixture['parent'][key])

    def test_fast_forward_repair_rejects_non_descendants_and_review_drift(self):
        fixture = self.prepare_fast_forward_repair('fast-forward-review-drift')
        module = fixture['module']
        repo = fixture['repo']
        (repo / 'later.txt').write_text('later advance\n')
        self.git(repo, 'add', 'later.txt')
        self.git(repo, 'commit', '-qm', 'test: later managed-ref advance')
        self.git(repo, 'push', '--no-verify', 'origin', 'integration/shared')
        with self.assertRaisesRegex(module.SyncwheelError, 'reviewed plan drifted'):
            module.apply_coordination_repair_plan(
                repo, fixture['manifest'], fixture['plan']
            )
        self.assertEqual(self.remote_state(fixture['origin'])[0], fixture['parent_tip'])

        origin = self.create_remote('non-fast-forward-repair')
        unrelated = self.clone(origin, 'non-fast-forward-repair')
        self.init_coordinated(unrelated)
        branch = 'integration/shared'
        ref = f'refs/heads/{branch}'
        self.git(unrelated, 'switch', '-q', branch)
        (unrelated / 'recorded.txt').write_text('recorded history\n')
        self.git(unrelated, 'add', 'recorded.txt')
        self.git(unrelated, 'commit', '-qm', 'test: recorded managed-ref tip')
        self.run_cli(unrelated, 'int', 'push')
        self.git(unrelated, 'switch', '-q', '-c', 'unrelated-tip', 'origin/main')
        (unrelated / 'unrelated.txt').write_text('unrelated history\n')
        self.git(unrelated, 'add', 'unrelated.txt')
        self.git(unrelated, 'commit', '-qm', 'test: unrelated managed-ref tip')
        replacement = self.git(unrelated, 'rev-parse', 'HEAD').stdout.strip()
        fixture_ref = 'refs/heads/fixture/non-fast-forward-object'
        self.git(unrelated, 'push', '-q', 'origin', f'{replacement}:{fixture_ref}')
        subprocess.run(
            ['git', '--git-dir', str(origin), 'update-ref', ref, replacement],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ['git', '--git-dir', str(origin), 'update-ref', '-d', fixture_ref, replacement],
            check=True,
            capture_output=True,
            text=True,
        )
        manifest, _ = module.load_manifest(unrelated)
        with self.assertRaisesRegex(module.SyncwheelError, 'is not a descendant'):
            module.coordination_repair_plan(
                unrelated,
                manifest,
                ref,
                module.COORDINATION_REPAIR_FAST_FORWARD_BACKEND,
            )

    def test_tree_equivalent_repair_stops_on_pre_and_post_cas_drift(self):
        before = self.prepare_tree_equivalent_repair('tree-equivalent-pre-drift')
        module = before['module']
        later = self.git(
            before['repo'],
            'commit-tree',
            before['tree'],
            '-p',
            before['observed'],
            '-m',
            'test: drift before state CAS',
        ).stdout.strip()
        self.git(before['repo'], 'push', '--no-verify', 'origin', f"{later}:{before['ref']}")
        with self.assertRaisesRegex(module.SyncwheelError, 'reviewed plan drifted'):
            module.apply_coordination_repair_plan(
                before['repo'], before['manifest'], before['plan']
            )
        self.assertEqual(self.remote_state(before['origin'])[0], before['parent_tip'])

        after = self.prepare_tree_equivalent_repair('tree-equivalent-post-drift')
        module = after['module']
        final_tip = self.git(
            after['repo'],
            'commit-tree',
            after['tree'],
            '-p',
            after['observed'],
            '-m',
            'test: drift after state CAS',
        ).stdout.strip()

        class DriftAfterCasBackend(module.TreeEquivalentStateCasCoordinationRepairBackend):
            def apply(self, **kwargs):
                result = super().apply(**kwargs)
                module.git(
                    kwargs['repo_root'],
                    'push',
                    '--no-verify',
                    kwargs['remote'],
                    f"{final_tip}:{after['ref']}",
                )
                return result

        with self.assertRaisesRegex(module.SyncwheelError, 'guarded refs drifted'):
            module.apply_coordination_repair_plan(
                after['repo'],
                after['manifest'],
                after['plan'],
                backend=DriftAfterCasBackend(),
            )
        accepted_tip, accepted = self.remote_state(after['origin'])
        self.assertNotEqual(accepted_tip, after['parent_tip'])
        self.assertEqual(accepted['parent_state'], after['parent_tip'])
        self.assertEqual(accepted['managed_refs'][after['ref']], after['observed'])
        self.assertEqual(
            module.remote_ref_tips(after['repo'], 'origin', [after['ref']])[after['ref']],
            final_tip,
        )

    def test_tree_equivalent_repair_stops_on_state_lease_loss(self):
        fixture = self.prepare_tree_equivalent_repair('tree-equivalent-state-race')
        module = fixture['module']
        competing_state = json.loads(json.dumps(fixture['parent']))
        competing_state['publication_id'] = 'fixture-state-race'
        competing_state['parent_state'] = fixture['parent_tip']
        competing_state['created_at'] = module.iso_utc_now()
        competing_state['changed_refs'] = {}
        competing_state['publication_scope'] = 'fixture-state-race'
        competing_tip = module.create_coordination_state_commit(
            fixture['repo'], competing_state, fixture['parent_tip']
        )

        class LoseStateLeaseBackend(module.TreeEquivalentStateCasCoordinationRepairBackend):
            def apply(self, **kwargs):
                module.git(
                    kwargs['repo_root'],
                    'push',
                    '--no-verify',
                    f"--force-with-lease={kwargs['state_ref']}:{kwargs['expected_state_tip']}",
                    kwargs['remote'],
                    f"{competing_tip}:{kwargs['state_ref']}",
                )
                return super().apply(**kwargs)

        with self.assertRaisesRegex(module.SyncwheelError, 'state lease was lost'):
            module.apply_coordination_repair_plan(
                fixture['repo'],
                fixture['manifest'],
                fixture['plan'],
                backend=LoseStateLeaseBackend(),
            )
        self.assertEqual(self.remote_state(fixture['origin'])[0], competing_tip)

    def test_tree_equivalent_repair_stops_on_post_cas_ownership_race(self):
        fixture = self.prepare_tree_equivalent_repair('tree-equivalent-owner-race')
        module = fixture['module']
        competing_state = json.loads(json.dumps(fixture['parent']))
        competing_state['coordination_id'] = 'second-domain'
        competing_state['publication_id'] = 'fixture-owner-race'
        competing_state['parent_state'] = None
        competing_state['created_at'] = module.iso_utc_now()
        competing_state['changed_refs'] = {}
        competing_state['publication_scope'] = 'fixture-owner-race'
        competing_tip = module.create_coordination_state_commit(
            fixture['repo'], competing_state
        )
        competing_ref = 'refs/heads/syncwheel/state/second-domain'

        class OwnershipRaceBackend(module.TreeEquivalentStateCasCoordinationRepairBackend):
            def apply(self, **kwargs):
                result = super().apply(**kwargs)
                module.git(
                    kwargs['repo_root'],
                    'push',
                    '--no-verify',
                    kwargs['remote'],
                    f'{competing_tip}:{competing_ref}',
                )
                return result

        with self.assertRaisesRegex(
            module.SyncwheelError, 'already owned by another coordination domain'
        ):
            module.apply_coordination_repair_plan(
                fixture['repo'],
                fixture['manifest'],
                fixture['plan'],
                backend=OwnershipRaceBackend(),
            )

        accepted_tip, accepted = self.remote_state(fixture['origin'])
        self.assertNotEqual(accepted_tip, fixture['parent_tip'])
        self.assertEqual(accepted['parent_state'], fixture['parent_tip'])
        self.assertEqual(accepted['managed_refs'][fixture['ref']], fixture['observed'])
        self.assertEqual(
            self.remote_state(fixture['origin'], 'second-domain')[0], competing_tip
        )

    def test_compose_publishes_new_stack_and_preserves_remote_stack_and_unmapped_integration(self):
        fixture = self.prepare_additive_compose()
        module = fixture['module']
        plan = fixture['plan']
        self.assertEqual(plan['status'], 'publish-required')
        self.assertEqual(plan['remoteAddedStacks'], ['orphan'])
        self.assertEqual(plan['localAddedStacks'], ['new-stack'])
        self.assertEqual(plan['expectedIntegrationTip'], fixture['integration_tip'])
        self.assertEqual(plan['unmappedIntegrationCommits'], fixture['integration_commits'])
        self.assertFalse(plan['integrationMutation'])
        self.assertEqual(
            [stack['id'] for stack in plan['composedSnapshot']['stacks']],
            ['orphan', 'new-stack'],
        )

        pushes = []
        original_push = module.run_authorized_push

        def capture_push(repo_root, command, remote, refs, check=True):
            pushes.append({'command': command, 'refs': refs})
            return original_push(repo_root, command, remote, refs, check=check)

        with mock.patch.object(module, 'run_authorized_push', side_effect=capture_push):
            result = module.apply_coordination_compose_stack_plan(
                fixture['repo'],
                fixture['manifest'],
                fixture['manifest_path'],
                plan,
            )

        self.assertEqual(result['status'], 'composed')
        self.assertFalse(result['integration_mutated'])
        self.assertEqual(result['unmapped_integration_commits'], fixture['integration_commits'])
        self.assertEqual(len(pushes), 1)
        command = pushes[0]['command']
        self.assertIn('--atomic', command)
        self.assertEqual(
            set(pushes[0]['refs']),
            {'refs/heads/pr/new-stack', plan['stateRef']},
        )
        self.assertTrue(any(item.endswith(':refs/heads/pr/new-stack') for item in command))
        self.assertFalse(any(item.endswith(':refs/heads/pr/orphan') for item in command))
        self.assertFalse(any(item.endswith(f":{plan['integrationRef']}") for item in command))

        accepted_tip, accepted = self.remote_state(fixture['origin'])
        self.assertEqual(accepted_tip, result['remote_state_tip'])
        self.assertEqual(accepted['parent_state'], fixture['remote_tip'])
        self.assertEqual(accepted['projection_status'], 'partial')
        self.assertEqual(
            accepted['changed_refs'], {'refs/heads/pr/new-stack': fixture['new_tip']}
        )
        self.assertEqual(
            accepted['managed_refs']['refs/heads/pr/orphan'], fixture['orphan_tip']
        )
        self.assertEqual(
            accepted['managed_refs'][plan['integrationRef']], fixture['integration_tip']
        )
        self.assertEqual(
            [stack['id'] for stack in accepted['manifest']['stacks']],
            ['orphan', 'new-stack'],
        )
        persisted, _ = module.load_manifest(fixture['repo'], fixture['manifest_path'])
        self.assertEqual([stack['id'] for stack in persisted['stacks']], ['orphan', 'new-stack'])

    def test_compose_is_digest_bound_and_rejects_non_additive_or_drifted_inputs(self):
        fixture = self.prepare_additive_compose('compose-drift')
        module = fixture['module']
        with self.assertRaisesRegex(module.SyncwheelError, 'manifest digest does not match'):
            module.coordination_compose_stack_plan(
                fixture['repo'],
                fixture['manifest'],
                'new-stack',
                fixture['base_tip'],
                '0' * 64,
            )

        tampered = json.loads(json.dumps(fixture['plan']))
        tampered['expectedIntegrationTip'] = '0' * 40
        with self.assertRaisesRegex(module.SyncwheelError, 'plan digest does not match'):
            module.apply_coordination_compose_stack_plan(
                fixture['repo'], fixture['manifest'], fixture['manifest_path'], tampered
            )

        changed_manifest = json.loads(json.dumps(fixture['manifest']))
        changed_manifest['stacks'][0]['commits'] = [fixture['base_tip']]
        with self.assertRaisesRegex(module.SyncwheelError, 'reviewed plan drifted'):
            module.apply_coordination_compose_stack_plan(
                fixture['repo'], changed_manifest, fixture['manifest_path'], fixture['plan']
            )

        local_snapshot = module.coordination_manifest_snapshot(
            fixture['manifest'], fixture['repo']
        )
        remote_snapshot = json.loads(json.dumps(fixture['remote_state']['manifest']))
        remote_snapshot['stacks'][0].update({
            'id': 'new-stack',
            'branch': 'pr/new-stack',
            'commits': [fixture['orphan_tip']],
        })
        remote_snapshot['integration']['stacks'] = ['new-stack']
        with self.assertRaisesRegex(module.SyncwheelError, 'conflicting additions'):
            module.compose_additive_coordination_snapshots(
                fixture['base_state']['manifest'],
                local_snapshot,
                remote_snapshot,
                'new-stack',
            )

    def test_compose_replans_to_local_adoption_after_remote_success(self):
        fixture = self.prepare_additive_compose('compose-adoption-retry')
        module = fixture['module']
        with mock.patch.object(
            module,
            'save_manifest_with_ledger',
            side_effect=module.SyncwheelError('fixture manifest save failed'),
        ):
            with self.assertRaisesRegex(module.SyncwheelError, 'local adoption pending'):
                module.apply_coordination_compose_stack_plan(
                    fixture['repo'],
                    fixture['manifest'],
                    fixture['manifest_path'],
                    fixture['plan'],
                )

        accepted_tip, accepted = self.remote_state(fixture['origin'])
        self.assertEqual(accepted['parent_state'], fixture['remote_tip'])
        self.assertEqual(
            accepted['managed_refs']['refs/heads/pr/new-stack'], fixture['new_tip']
        )
        unchanged_local, _ = module.load_manifest(fixture['repo'], fixture['manifest_path'])
        self.assertEqual([stack['id'] for stack in unchanged_local['stacks']], ['new-stack'])

        adoption_plan, _, _ = module.coordination_compose_stack_plan(
            fixture['repo'],
            unchanged_local,
            'new-stack',
            fixture['base_tip'],
            fixture['base_state']['manifest_digest'],
        )
        self.assertEqual(adoption_plan['status'], 'adopt-only')
        with mock.patch.object(module, 'run_authorized_push') as push:
            result = module.apply_coordination_compose_stack_plan(
                fixture['repo'],
                unchanged_local,
                fixture['manifest_path'],
                adoption_plan,
            )
        push.assert_not_called()
        self.assertEqual(result['status'], 'adopted')
        self.assertEqual(result['remote_state_tip'], accepted_tip)
        persisted, _ = module.load_manifest(fixture['repo'], fixture['manifest_path'])
        self.assertEqual([stack['id'] for stack in persisted['stacks']], ['orphan', 'new-stack'])

    def test_compose_stops_when_remote_state_lease_moves_after_plan(self):
        fixture = self.prepare_additive_compose('compose-state-race')
        module = fixture['module']
        competing = json.loads(json.dumps(fixture['remote_state']))
        competing['publication_id'] = 'fixture-compose-state-race'
        competing['parent_state'] = fixture['remote_tip']
        competing['created_at'] = module.iso_utc_now()
        competing['changed_refs'] = {}
        competing['publication_scope'] = 'fixture-compose-state-race'
        competing_tip = module.create_coordination_state_commit(
            fixture['repo'], competing, fixture['remote_tip']
        )
        state_ref = fixture['plan']['stateRef']
        self.git(
            fixture['repo'],
            'push',
            '--no-verify',
            f'--force-with-lease={state_ref}:{fixture["remote_tip"]}',
            'origin',
            f'{competing_tip}:{state_ref}',
        )

        with self.assertRaisesRegex(module.SyncwheelError, 'reviewed plan drifted'):
            module.apply_coordination_compose_stack_plan(
                fixture['repo'],
                fixture['manifest'],
                fixture['manifest_path'],
                fixture['plan'],
            )
        self.assertEqual(self.remote_state(fixture['origin'])[0], competing_tip)
        self.assertEqual(
            self.git(
                fixture['repo'], 'ls-remote', 'origin', 'refs/heads/pr/new-stack'
            ).stdout.strip(),
            '',
        )

    def test_compose_publisher_is_bound_to_reviewed_state_tip(self):
        fixture = self.prepare_additive_compose('compose-publisher-race')
        module = fixture['module']
        original_publish = module.coordinated_publish

        def publish_competitor_then_continue(*args, **kwargs):
            competitor_kwargs = dict(kwargs)
            competitor_kwargs.pop('expected_coordination_state_tip', None)
            competitor_kwargs.pop('expected_observed_refs', None)
            original_publish(*args, **competitor_kwargs)
            return original_publish(*args, **kwargs)

        with mock.patch.object(
            module,
            'coordinated_publish',
            side_effect=publish_competitor_then_continue,
        ):
            with self.assertRaisesRegex(module.SyncwheelError, 'remote state changed'):
                module.apply_coordination_compose_stack_plan(
                    fixture['repo'],
                    fixture['manifest'],
                    fixture['manifest_path'],
                    fixture['plan'],
                )

        accepted_tip, accepted = self.remote_state(fixture['origin'])
        self.assertEqual(accepted['parent_state'], fixture['remote_tip'])
        self.assertEqual(
            accepted['managed_refs']['refs/heads/pr/new-stack'], fixture['new_tip']
        )
        adoption_plan, _, _ = module.coordination_compose_stack_plan(
            fixture['repo'],
            fixture['manifest'],
            'new-stack',
            fixture['base_tip'],
            fixture['base_state']['manifest_digest'],
        )
        self.assertEqual(adoption_plan['status'], 'adopt-only')
        self.assertEqual(adoption_plan['expectedRemoteStateTip'], accepted_tip)

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

    def prepare_published_integration(self, name):
        origin = self.create_remote(name=f'{name}-origin')
        repo = self.clone(origin, name)
        self.init_coordinated(repo)
        self.track_fixture_ignore_in_replay_base(repo)
        self.run_cli(repo, 'int', 'push')
        feature_sha = self.commit_on_branch(repo, 'pr/feature-a', f'{name}-a.txt')
        self.run_cli(
            repo, 'stack', 'create', 'feature-a', feature_sha, '--branch', 'pr/feature-a',
        )
        self.run_cli(repo, 'stack', 'push', 'feature-a')
        return origin, repo

    def diverge_manifest_from_integration_tip(self, repo, name, command):
        if command[:2] == ('int', 'push'):
            self.run_cli(repo, 'stack', 'demote', 'feature-a')
            return
        second_sha = self.commit_on_branch(repo, 'pr/feature-b', f'{name}-b.txt')
        self.run_cli(
            repo, 'stack', 'create', 'feature-b', second_sha, '--branch', 'pr/feature-b',
        )

    def test_dirty_integration_checkout_refuses_before_the_control_ref_moves(self):
        module = self.load_module()
        commands = {
            'int-push': ('int', 'push'),
            'stack-push': ('stack', 'push', 'feature-b'),
        }
        for name, command in commands.items():
            with self.subTest(command=name):
                label = f'dirty-integration-{name}'
                origin, repo = self.prepare_published_integration(label)
                self.diverge_manifest_from_integration_tip(repo, label, command)
                manifest_path = repo / '.syncwheel' / 'manifest.json'
                (repo / 'README.md').write_text('unrelated local edit\n')
                before = self.git(repo, 'rev-parse', 'integration/shared').stdout.strip()

                failure = self.run_cli(repo, *command, expected=2)

                self.assertIn(str(repo), failure.stderr)
                self.assertIn('README.md', failure.stderr)
                self.assertIn(f'rerun: syncwheel {command[0]} {command[1]}', failure.stderr)
                self.assertEqual(
                    self.git(repo, 'rev-parse', 'integration/shared').stdout.strip(),
                    before,
                )
                self.assertEqual(
                    module.pending_control_manifest_intents(
                        module.load_ledger_events(repo, manifest_path)
                    ),
                    [],
                )
                self.assertEqual(
                    (repo / 'README.md').read_text(), 'unrelated local edit\n'
                )

                self.git(repo, 'checkout', '--', 'README.md')
                self.run_cli(repo, *command)

                self.assertEqual(
                    module.pending_control_manifest_intents(
                        module.load_ledger_events(repo, manifest_path)
                    ),
                    [],
                )
                tip = self.git(repo, 'rev-parse', 'integration/shared').stdout.strip()
                self.assertNotEqual(tip, before)
                _state_tip, state = self.remote_state(origin)
                self.assertEqual(
                    state['managed_refs']['refs/heads/integration/shared'], tip
                )

    def test_alignment_obstruction_after_the_cas_completes_the_operation(self):
        module = self.load_module()
        origin, repo = self.prepare_published_integration('dirty-after-cas')
        self.run_cli(repo, 'stack', 'demote', 'feature-a')
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        ready = self.tmp / 'dirty-after-cas-ready'
        release = self.tmp / 'dirty-after-cas-release'
        process = subprocess.Popen(
            ['python3', str(CLI), 'int', 'push'],
            cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={
                **os.environ,
                **self.environment,
                'SYNCWHEEL_TEST_CONTROL_MANIFEST_PAUSE': 'ref_updated',
                'SYNCWHEEL_TEST_CONTROL_MANIFEST_READY': str(ready),
                'SYNCWHEEL_TEST_CONTROL_MANIFEST_RELEASE': str(release),
            },
        )
        try:
            self.wait_for_path(ready, process)
            (repo / 'README.md').write_text('edited while the ref advanced\n')
            release.write_text('continue\n')
            stdout, stderr = process.communicate(timeout=60)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

        self.assertEqual(process.returncode, 0, (stdout, stderr))
        self.assertIn('still carries uncommitted changes', stderr)
        self.assertIn(str(repo), stderr)
        self.assertIn('README.md', stderr)
        self.assertIn('syncwheel int push', stderr)
        self.assertEqual(
            module.pending_control_manifest_intents(
                module.load_ledger_events(repo, manifest_path)
            ),
            [],
        )
        self.assertEqual(
            (repo / 'README.md').read_text(), 'edited while the ref advanced\n'
        )
        tip = self.git(repo, 'rev-parse', 'integration/shared').stdout.strip()
        committed = module.manifest_from_tree(
            repo, tip, module.integration_manifest_path(repo)
        )
        local, _ = module.load_manifest(repo, manifest_path)
        self.assertEqual(
            module.manifest_digest(committed), module.manifest_digest(local)
        )
        self.assertEqual(
            self.git(repo, 'status', '--porcelain').stdout, ' M README.md\n'
        )

    def prepare_pending_control_manifest_intent(self, name, stage):
        origin = self.create_remote(name=f'{name}-origin')
        repo = self.clone(origin, name)
        self.init_coordinated(repo)
        self.track_fixture_ignore_in_replay_base(repo)
        feature_sha = self.commit_on_branch(repo, 'pr/feature-a', f'{name}-a.txt')
        self.run_cli(
            repo, 'stack', 'create', 'feature-a', feature_sha, '--branch', 'pr/feature-a',
        )
        self.run_cli(repo, 'stack', 'push', 'feature-a')
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        killed = self.run_cli(
            repo,
            'int', 'rebuild', '--in-place',
            '--reason', f'interrupt control persistence at {stage}',
            expected=-signal.SIGKILL,
            extra_env={'SYNCWHEEL_TEST_CONTROL_MANIFEST_SIGKILL': stage},
        )
        self.assertEqual(killed.returncode, -signal.SIGKILL)
        module = self.load_module()
        pending = module.pending_control_manifest_intents(
            module.load_ledger_events(repo, manifest_path)
        )
        self.assertEqual(len(pending), 1)
        return origin, repo, pending[0]['payload']

    def test_manifest_writer_outside_the_entrypoints_settles_a_pending_intent(self):
        module = self.load_module()
        _origin, repo, intent = self.prepare_pending_control_manifest_intent(
            'settle-on-create', 'manifest_saved'
        )
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        second_sha = self.commit_on_branch(repo, 'pr/feature-b', 'feature-b.txt')

        self.run_cli(
            repo, 'stack', 'create', 'feature-b', second_sha, '--branch', 'pr/feature-b',
        )

        events = module.load_ledger_events(repo, manifest_path)
        self.assertEqual(module.pending_control_manifest_intents(events), [])
        terminal = [
            event for event in events
            if event['type'] in {
                'manifest_saved', 'control_manifest_persistence_abandoned',
            }
            and (event['payload'] or {}).get('operation_id') == intent['operation_id']
        ]
        self.assertEqual(len(terminal), 1)
        current, _ = module.load_manifest(repo, manifest_path)
        self.assertEqual(sorted(module.stack_map(current)), ['feature-a', 'feature-b'])

        for command in (
            ('stack', 'push', 'feature-b'),
            ('int', 'push'),
            ('int', 'rebuild', '--in-place', '--reason', 'retry after the interruption'),
            ('int', 'rebuild', '--reason', 'adopt reviewed control manifest proposal'),
        ):
            self.run_cli(repo, *command)
        self.assertEqual(
            module.pending_control_manifest_intents(
                module.load_ledger_events(repo, manifest_path)
            ),
            [],
        )

    def write_reviewed_control_proposal(self, repo, source_manifest):
        proposal = json.loads(json.dumps(source_manifest))
        proposal['defaults']['replay_mode'] = 'in-place'
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(proposal, indent=2) + '\n')
        return proposal

    def test_reviewed_proposal_remedy_abandons_a_superseded_intent(self):
        module = self.load_module()
        _origin, repo, intent = self.prepare_pending_control_manifest_intent(
            'reviewed-proposal', 'intent_saved'
        )
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        committed = module.manifest_from_tree(
            repo, intent['expected_control_commit'],
            module.integration_manifest_path(repo),
        )
        proposal = self.write_reviewed_control_proposal(repo, committed)

        failure = self.run_cli(repo, 'int', 'push', expected=2)
        self.assertIn(
            'control manifest changed after the interrupted persistence intent',
            failure.stderr,
        )
        remedy = "syncwheel int rebuild --reason 'adopt reviewed control manifest proposal'"
        self.assertIn(remedy, failure.stderr)

        self.run_cli(
            repo, 'int', 'rebuild',
            '--reason', 'adopt reviewed control manifest proposal',
        )

        events = module.load_ledger_events(repo, manifest_path)
        self.assertEqual(module.pending_control_manifest_intents(events), [])
        abandoned = [
            event for event in events
            if event['type'] == 'control_manifest_persistence_abandoned'
            and event['payload']['operation_id'] == intent['operation_id']
        ]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0]['payload']['outcome'], 'superseded_local_proposal')
        adopted, _ = module.load_manifest(repo, manifest_path)
        self.assertEqual(
            module.manifest_digest(adopted), module.manifest_digest(proposal)
        )

    def test_superseded_source_after_the_control_commit_receipts_the_intent(self):
        module = self.load_module()
        _origin, repo, intent = self.prepare_pending_control_manifest_intent(
            'superseded-receipt', 'manifest_saved'
        )
        manifest_path = repo / '.syncwheel' / 'manifest.json'
        source, _ = module.load_manifest(repo, manifest_path)
        proposal = self.write_reviewed_control_proposal(repo, source)
        self.assertEqual(
            self.git(repo, 'rev-parse', 'integration/shared').stdout.strip(),
            intent['expected_control_commit'],
        )

        self.run_cli(repo, 'int', 'push')

        events = module.load_ledger_events(repo, manifest_path)
        self.assertEqual(module.pending_control_manifest_intents(events), [])
        receipts = [
            event for event in events
            if event['type'] == 'manifest_saved'
            and (event['payload'] or {}).get('operation_id') == intent['operation_id']
        ]
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0]['payload']['context']['source_superseded'])
        self.assertEqual(
            receipts[0]['payload']['context']['persisted_manifest_digest'],
            intent['expected_manifest_digest'],
        )
        kept, _ = module.load_manifest(repo, manifest_path)
        self.assertEqual(
            module.manifest_digest(kept), module.manifest_digest(proposal)
        )

    def prepare_cross_clone_control_divergence(self, name):
        """A second clone whose tracked manifest diverges from the integration tip."""
        origin = self.create_remote(name=f'{name}-origin')
        first = self.clone(origin, f'{name}-first')
        self.init_coordinated(first)
        self.track_fixture_ignore_in_replay_base(first)
        feature_sha = self.commit_on_branch(first, 'pr/feature-a', f'{name}.txt')
        self.run_cli(
            first, 'stack', 'create', 'feature-a', feature_sha, '--branch', 'pr/feature-a',
        )
        self.run_cli(first, 'stack', 'push', 'feature-a')
        self.run_cli(first, 'int', 'push')

        second = self.clone(origin, f'{name}-second')
        self.git(
            second, 'fetch', 'origin',
            'integration/shared:refs/remotes/origin/integration/shared',
        )
        self.git(second, 'branch', 'integration/shared', 'origin/integration/shared')
        self.run_cli(
            second,
            'init',
            '--syncwheel-tracking', 'git-tracked',
            '--publication-remote', 'origin',
            '--integration-branch', 'integration/shared',
        )
        self.disable_fixture_hooks(second)
        return origin, second

    def test_divergence_remedy_accepts_the_tracked_manifest_it_replaces(self):
        module = self.load_module()
        # plumbing and desk are unreachable here by construction: both need the
        # integration branch unchecked out, and the divergent manifest is a
        # tracked file inside the checkout that carries it.
        for mode in ('in-place', 'ephemeral'):
            with self.subTest(replay_mode=mode):
                _origin, second = self.prepare_cross_clone_control_divergence(
                    f'divergence-{mode}'
                )
                manifest_path = second / '.syncwheel' / 'manifest.json'
                self.assertEqual(
                    self.git(second, 'status', '--porcelain').stdout,
                    ' M .syncwheel/manifest.json\n',
                )
                local, _ = module.load_manifest(second, manifest_path)

                failure = self.run_cli(second, 'int', 'push', expected=2)
                remedy = (
                    "syncwheel int rebuild --reason "
                    "'adopt reviewed control manifest proposal'"
                )
                self.assertIn(remedy, failure.stderr)

                self.run_cli(
                    second, 'int', 'rebuild', '--replay-mode', mode,
                    '--reason', 'adopt reviewed control manifest proposal',
                )

                adopted, _ = module.load_manifest(second, manifest_path)
                self.assertEqual(
                    module.manifest_digest(adopted), module.manifest_digest(local)
                )
                tip = self.git(second, 'rev-parse', 'integration/shared').stdout.strip()
                committed = module.manifest_from_tree(
                    second, tip, module.integration_manifest_path(second)
                )
                self.assertEqual(
                    module.manifest_digest(committed), module.manifest_digest(local)
                )

    def test_int_push_without_a_local_integration_branch_fails_closed(self):
        origin = self.create_remote(name='absent-integration-origin')
        repo = self.clone(origin, 'absent-integration')
        self.init_coordinated(repo)
        self.track_fixture_ignore_in_replay_base(repo)
        self.git(repo, 'switch', '-q', '-c', 'work', 'origin/main')
        self.git(repo, 'branch', '-D', 'integration/shared')

        failure = self.run_cli(repo, 'int', 'push', expected=2)

        self.assertIn(
            'cannot publish an empty managed ref: refs/heads/integration/shared',
            failure.stderr,
        )
        self.assertNotIn('Traceback', failure.stderr)
