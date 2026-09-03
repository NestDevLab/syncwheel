import ast
import importlib.util
import contextlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / 'scripts' / 'syncwheel.py'


class DeploymentChannelTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='syncwheel-channel-')
        self.root = Path(self.temp.name)
        self.remote = self.root / 'origin.git'
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '--bare', '-q', str(self.remote)], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.repo)], check=True)
        self.git('config', 'user.name', 'Channel Test')
        self.git('config', 'user.email', 'channel@example.invalid')
        (self.repo / 'base.txt').write_text('base\n')
        self.git('add', 'base.txt')
        self.git('commit', '-q', '-m', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', '-q', '-u', 'origin', 'main')

        self.a = self.make_stack('a', 'a.txt', 'a one\n')
        self.b = self.make_stack('b', 'b.txt', 'b one\n')
        self.git('switch', '-q', '-c', 'main-integration', 'main')
        self.write_manifest(version=2)
        # Coordination state now binds the manifest carried by the exact
        # integration tip, so channel fixtures must start from that invariant.
        self.git('add', '-f', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: persist integration control manifest')
        self.cli(
            'hooks', 'remove', '--disable',
            '--reason', 'channel fixture uses raw primary branch setup', '--apply',
        )

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, check=True):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        preserved_manifest = None
        if args and args[0] == 'switch' and manifest_path.exists():
            # Raw branch switches in these fixtures are product-history setup,
            # not control-manifest operations. Preserve the local control source
            # while Git changes whether that path is tracked on the target.
            preserved_manifest = manifest_path.read_bytes()
            tracked = subprocess.run(
                ['git', 'ls-files', '--error-unmatch', '--', '.syncwheel/manifest.json'],
                cwd=self.repo, text=True, capture_output=True,
            ).returncode == 0
            if tracked:
                subprocess.run(
                    ['git', 'checkout', '--', '.syncwheel/manifest.json'],
                    cwd=self.repo, check=True, capture_output=True, text=True,
                )
            else:
                manifest_path.unlink()
        result = subprocess.run(
            ['git', *args], cwd=self.repo, text=True, capture_output=True
        )
        if preserved_manifest is not None:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(preserved_manifest)
        if check and result.returncode:
            self.fail(result.stderr)
        return result.stdout.strip()

    def cli(self, *args, expected=0):
        env = os.environ.copy()
        env['SYNCWHEEL_UPDATE_MODE'] = 'off'
        result = subprocess.run(
            ['python3', str(CLI), *args], cwd=self.repo, text=True,
            capture_output=True, env=env,
        )
        if result.returncode != expected:
            self.fail(
                f'exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}'
            )
        return result

    def load_module(self):
        spec = importlib.util.spec_from_file_location('syncwheel_channel_test', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def make_stack(self, name, path, content):
        self.git('switch', '-q', '-C', f'pr/{name}', 'main')
        (self.repo / path).write_text(content)
        self.git('add', path)
        self.git('commit', '-q', '-m', f'{name} change')
        return self.git('rev-parse', 'HEAD')

    def advance_stack(self, name, path, content):
        self.git('switch', '-q', f'pr/{name}')
        with (self.repo / path).open('a') as handle:
            handle.write(content)
        self.git('add', path)
        self.git('commit', '-q', '-m', f'{name} advance')
        tip = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main-integration')
        return tip

    def write_manifest(self, version=2):
        manifest = {
            'version': version,
            'repository_mode': 'delivery',
            'syncwheel_tracking': 'git-tracked',
            'defaults': {
                'canonical_remote': 'origin',
                'publication_remote': 'origin',
                'base_branch': 'main',
                'base_ref': 'origin/main',
                'integration_membership': 'required',
            },
            'integration': {
                'branch': 'main-integration',
                'base': 'origin/main',
                'strategy': 'cherry-pick',
                'stacks': ['a', 'b'],
            },
            'stacks': [
                {
                    'id': 'a', 'branch': 'pr/a', 'base': 'origin/main',
                    'target_remote': 'origin', 'target_branch': 'main',
                    'integration_branch': 'main-integration', 'commits': [self.a],
                },
                {
                    'id': 'b', 'branch': 'pr/b', 'base': 'origin/main',
                    'target_remote': 'origin', 'target_branch': 'main',
                    'integration_branch': 'main-integration', 'commits': [self.b],
                },
            ],
            'coordination': {
                'mode': 'disabled', 'id': 'channel-test', 'remote': 'origin',
                'state_branch': 'syncwheel/state/channel-test',
                'gc': {
                    'worktree_grace_days': 7,
                    'backup_retention_days': 30,
                    'backup_keep': 2,
                },
            },
        }
        path = self.repo / '.syncwheel' / 'manifest.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + '\n')

    def manifest(self):
        return json.loads((self.repo / '.syncwheel' / 'manifest.json').read_text())

    def create(self, channel='dev', *extra, stacks=('a', 'b')):
        arguments = ['channel', 'create', channel]
        for stack in stacks:
            arguments.extend(['--stack', stack])
        arguments.extend(extra)
        return self.mutate(*arguments)

    def mutate(self, *arguments):
        preview = json.loads(self.cli(*arguments).stdout)
        return json.loads(self.cli(
            *arguments, '--plan-digest', preview['planDigest'], '--apply'
        ).stdout)

    def declare_stack_tip(self, stack, tip):
        data = self.manifest()
        entry = next(item for item in data['stacks'] if item['id'] == stack)
        entry['commits'] = self.git('rev-list', '--reverse', f"{entry['base']}..{tip}").splitlines()
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')

    def plan(self, channel='dev', operation='apply'):
        return json.loads(
            self.cli('channel', 'plan', channel, '--operation', operation).stdout
        )

    def apply_channel(self, channel='dev'):
        plan = self.plan(channel, 'apply')
        receipt = json.loads(self.cli(
            'channel', 'apply', channel, '--plan-digest', plan['planDigest'], '--apply'
        ).stdout)
        return plan, receipt

    def enable_active_coordination(self):
        data = self.manifest()
        data['coordination']['mode'] = 'active-active'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(data, indent=2) + '\n'
        )

    def publish_active_channel(self, channel='dev'):
        self.enable_active_coordination()
        plan = self.plan(channel, 'publish')
        receipt = json.loads(self.cli(
            'channel', 'publish', channel, '--plan-digest', plan['planDigest'], '--apply'
        ).stdout)
        return plan, receipt

    def pinned_channel_definition(self, module, manifest, channel_id, stack='a'):
        return {
            'id': channel_id,
            'branch': f'channel/{channel_id}',
            'lifecycle': 'shared',
            'base': manifest['defaults']['base_ref'],
            'baseRevision': module.commit_full_sha(
                self.repo, manifest['defaults']['base_ref']
            ),
            'remote': manifest['defaults']['publication_remote'],
            'composition': [module.pin_stack_for_channel(self.repo, manifest, stack)],
        }

    def test_create_explicitly_migrates_v2_and_pins_full_ordered_state(self):
        dry = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a', '--stack', 'b'
        ).stdout)
        self.assertEqual(dry['manifestMigration'], '2-to-3')
        self.assertEqual(self.manifest()['version'], 2)
        applied = self.create()
        self.assertEqual(applied['manifestMigration'], '2-to-3')
        manifest = self.manifest()
        self.assertEqual(manifest['version'], 3)
        channel = manifest['channels'][0]
        self.assertEqual([entry['stack'] for entry in channel['composition']], ['a', 'b'])
        self.assertEqual(channel['composition'][0]['branchRevision'], self.a)
        self.assertEqual(channel['composition'][0]['commits'], [self.a])
        self.assertTrue(all(len(commit) == 40 for entry in channel['composition'] for commit in entry['commits']))
        self.assertEqual(dry['observation']['newChannelRef'], {
            'localRevision': None,
            'remoteKnown': True,
            'remoteRevision': None,
        })

    def test_create_rejects_duplicate_stack_arguments_without_writing(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        before = manifest_path.read_bytes()
        for extra in ((), ('--apply',)):
            with self.subTest(extra=extra):
                rejected = self.cli(
                    'channel', 'create', 'dev', '--stack', 'a', '--stack', 'a',
                    *extra, expected=2,
                )
                self.assertIn('duplicate stack id(s): a', rejected.stderr)
                self.assertEqual(manifest_path.read_bytes(), before)
        listed = json.loads(self.cli('channel', 'list', '--json').stdout)
        self.assertEqual(listed['channels'], [])
        self.assertEqual(self.manifest()['version'], 2)

    def test_create_ref_must_be_observably_unowned_without_side_effects(self):
        module = self.load_module()
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        fixtures = (
            ('local-taken', True, False, 'existing unowned local branch', None),
            ('remote-taken', False, True, 'existing unowned remote branch', None),
            ('both-taken', True, True, 'existing unowned local branch', None),
            ('unknown-remote', False, False, 'remote observation is unknown', 'missing'),
        )
        for branch, local_exists, remote_exists, message, remote in fixtures:
            with self.subTest(branch=branch):
                full_branch = f'channel/{branch}'
                if local_exists:
                    self.git('branch', full_branch, self.a)
                if remote_exists:
                    self.git('push', '-q', 'origin', f'{self.a}:refs/heads/{full_branch}')
                manifest_before = manifest_path.read_bytes()
                refs_before = self.git('show-ref')
                remote_before = self.git('ls-remote', 'origin')
                ledger_before = module.load_ledger_events(self.repo, manifest_path)
                arguments = [
                    'channel', 'create', branch, '--branch', full_branch,
                    '--stack', 'a', '--apply',
                ]
                if remote:
                    arguments.extend(['--remote', remote])
                rejected = self.cli(*arguments, expected=2)
                self.assertIn(message, rejected.stderr)
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertEqual(self.git('show-ref'), refs_before)
                self.assertEqual(self.git('ls-remote', 'origin'), remote_before)
                self.assertEqual(
                    module.load_ledger_events(self.repo, manifest_path), ledger_before
                )

    def test_create_rechecks_remote_absence_under_manifest_lock(self):
        module = self.load_module()
        preview = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a'
        ).stdout)
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        before = manifest_path.read_bytes()
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            branch=None, base=None, remote=None, lifecycle='shared', expires_at=None,
            stack=['a'], apply=True, plan_digest=preview['planDigest'], operation_id=None,
        )
        absent = {'known': True, 'revision': None, 'error': None}
        appeared = {'known': True, 'revision': self.a, 'error': None}
        with mock.patch.object(
            module, 'channel_remote_observation', side_effect=[absent, appeared]
        ):
            with self.assertRaisesRegex(module.SyncwheelError, 'plan is stale'):
                module.command_channel_create(args)
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(module.load_ledger_events(self.repo, manifest_path), [])

    def test_invalid_draft_dependency_never_materializes_a_stack_ref(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        before = manifest_path.read_bytes()
        refs_before = self.git('show-ref')
        rejected = self.cli(
            'stack', 'create', 'c', '--draft', '--depends-on', 'ghost', expected=2
        )
        self.assertIn('requires manifest version 3', rejected.stderr)
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(self.git('show-ref'), refs_before)
        self.assertFalse(self.git(
            'show-ref', '--verify', '--quiet', 'refs/heads/syncwheel/draft/c',
            check=False,
        ))

        self.create('migration', stacks=())
        before = manifest_path.read_bytes()
        refs_before = self.git('show-ref')
        rejected = self.cli(
            'stack', 'create', 'c', '--draft', '--depends-on', 'ghost', expected=2
        )
        self.assertIn('depends_on unknown stack(s): ghost', rejected.stderr)
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertEqual(self.git('show-ref'), refs_before)
        self.assertFalse(self.git(
            'show-ref', '--verify', '--quiet', 'refs/heads/syncwheel/draft/c',
            check=False,
        ))

    def test_dependencies_require_v3_and_round_trip_in_schema3_state(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = self.manifest()
        next(item for item in data['stacks'] if item['id'] == 'b')['depends_on'] = ['a']
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        rejected = self.cli('stack', 'list', expected=2)
        self.assertIn('depends_on requires manifest version 3', rejected.stderr)

        self.write_manifest(version=2)
        self.create('migration', stacks=())
        data = self.manifest()
        next(item for item in data['stacks'] if item['id'] == 'b')['depends_on'] = ['a']
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        module = self.load_module()
        manifest, _ = module.load_manifest(self.repo)
        self.assertEqual(
            next(item for item in manifest['stacks'] if item['id'] == 'b')['depends_on'],
            ['a'],
        )
        snapshot = module.coordination_manifest_snapshot(manifest, self.repo)
        self.assertEqual(
            next(item for item in snapshot['stacks'] if item['id'] == 'b')['depends_on'],
            ['a'],
        )
        state = module.build_coordination_state(
            self.repo, manifest, module.coordination_config(manifest),
            {'tip': None, 'state': None}, {}, {}, 'test', 'partial', 'dependency-test',
        )
        self.assertEqual(state['schema_version'], 3)
        module.validate_coordination_state(json.loads(json.dumps(state)))

        downgraded = json.loads(json.dumps(state))
        downgraded['schema_version'] = 2
        downgraded['manifest']['version'] = 2
        downgraded['manifest_digest'] = module.canonical_json_digest(downgraded['manifest'])
        with self.assertRaisesRegex(module.SyncwheelError, 'depends_on requires manifest version 3'):
            module.validate_coordination_state(downgraded)

    def test_v2_base_chain_loads_unchanged_and_migration_derives_dependencies(self):
        self.git('switch', '-q', '-C', 'pr/b', 'pr/a')
        (self.repo / 'b.txt').write_text('dependent b\n')
        self.git('add', 'b.txt')
        self.git('commit', '-q', '-m', 'dependent b')
        dependent_b = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main-integration')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = self.manifest()
        stack_b = next(item for item in data['stacks'] if item['id'] == 'b')
        stack_b.update({'base': 'pr/a', 'commits': [dependent_b]})
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        before = manifest_path.read_bytes()

        module = self.load_module()
        loaded, _ = module.load_manifest(self.repo)
        self.assertNotIn(
            'depends_on', next(item for item in loaded['stacks'] if item['id'] == 'b')
        )
        self.assertEqual(manifest_path.read_bytes(), before)

        self.create(stacks=('a', 'b'))
        migrated = self.manifest()
        self.assertEqual(migrated['version'], 3)
        migrated_b = next(item for item in migrated['stacks'] if item['id'] == 'b')
        self.assertEqual(migrated_b['depends_on'], ['a'])
        channel_b = next(
            item for item in migrated['channels'][0]['composition'] if item['stack'] == 'b'
        )
        self.assertEqual(channel_b['dependsOn'], ['a'])
        module.load_manifest(self.repo)

    def test_channel_branch_cannot_overlap_stack_or_integration_ownership(self):
        stack_collision = self.cli(
            'channel', 'create', 'bad-stack', '--branch', 'pr/a', '--apply', expected=2
        )
        self.assertIn('overlaps a stack or integration branch', stack_collision.stderr)
        integration_collision = self.cli(
            'channel', 'create', 'bad-int', '--branch', 'main-integration', '--apply',
            expected=2,
        )
        self.assertIn('overlaps a stack or integration branch', integration_collision.stderr)
        state_collision = self.cli(
            'channel', 'create', 'bad-state', '--branch', 'syncwheel/state/channel-test',
            '--stack', 'a', expected=2,
        )
        self.assertIn('coordination.state_branch', state_collision.stderr)
        self.create(stacks=('a',))
        data = self.manifest()
        data['channels'][0]['branch'] = 'syncwheel/state/channel-test'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        persisted = self.cli('channel', 'list', expected=2)
        self.assertIn('coordination.state_branch', persisted.stderr)

    def test_channel_branch_cannot_claim_base_or_stack_target_refs(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        before = manifest_path.read_bytes()
        refs_before = self.git('show-ref')
        remote_before = self.git('ls-remote', 'origin')

        base = self.cli(
            'channel', 'create', 'dangerous-base', '--branch', 'main',
            '--stack', 'a', '--apply', expected=2,
        )
        self.assertIn('overlaps protected defaults.base_branch', base.stderr)
        target = self.cli(
            'channel', 'create', 'dangerous-target', '--branch', 'release',
            '--stack', 'a', '--apply', expected=2,
        )
        self.assertEqual(target.returncode, 2)
        # Add an explicit stack target collision and prove it is rejected as well.
        data = json.loads(before)
        stack = next(item for item in data['stacks'] if item['id'] == 'a')
        stack.update({'target_remote': 'origin', 'target_branch': 'release'})
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        collision_before = manifest_path.read_bytes()
        target = self.cli(
            'channel', 'create', 'dangerous-target', '--branch', 'release',
            '--stack', 'a', '--apply', expected=2,
        )
        self.assertIn('target branch owned by stack(s): a', target.stderr)
        self.assertEqual(manifest_path.read_bytes(), collision_before)
        self.assertEqual(self.git('show-ref'), refs_before)
        self.assertEqual(self.git('ls-remote', 'origin'), remote_before)

        # Persisted v3 channel declarations use the same central ownership check.
        self.create(stacks=('a',))
        data = self.manifest()
        data['channels'][0]['branch'] = 'main'
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        persisted_base = self.cli('channel', 'list', expected=2)
        self.assertIn('overlaps protected defaults.base_branch', persisted_base.stderr)
        data['channels'][0]['branch'] = 'release'
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        persisted_target = self.cli('channel', 'list', expected=2)
        self.assertIn('target branch owned by stack(s): a', persisted_target.stderr)

    def test_channel_branch_cannot_claim_symbolic_base_refs(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        original = self.manifest()
        fixtures = (
            ('own', None, ['--base', 'origin/release'], 'channel.base'),
            ('integration', ('integration', 'base'), [], 'integration.base'),
            ('stack', ('stack', 'base'), [], 'stack a base'),
        )
        for name, mutation, extra, authority in fixtures:
            with self.subTest(name=name):
                data = json.loads(json.dumps(original))
                if mutation == ('integration', 'base'):
                    data['integration']['base'] = 'origin/release'
                elif mutation == ('stack', 'base'):
                    next(item for item in data['stacks'] if item['id'] == 'a')['base'] = (
                        'origin/release'
                    )
                manifest_path.write_text(json.dumps(data, indent=2) + '\n')
                before = manifest_path.read_bytes()
                refs_before = self.git('show-ref')
                remote_before = self.git('ls-remote', 'origin')
                rejected = self.cli(
                    'channel', 'create', f'dangerous-{name}', '--branch', 'release',
                    '--stack', 'b', *extra, '--apply', expected=2,
                )
                self.assertIn('overlaps canonical symbolic base(s)', rejected.stderr)
                self.assertIn(authority, rejected.stderr)
                self.assertEqual(manifest_path.read_bytes(), before)
                self.assertEqual(self.git('show-ref'), refs_before)
                self.assertEqual(self.git('ls-remote', 'origin'), remote_before)

    def test_persisted_pin_must_match_declared_stack_projection(self):
        self.create(stacks=('a',))
        data = self.manifest()
        entry = data['channels'][0]['composition'][0]
        entry.update({
            'branch': 'pr/b', 'branchRevision': self.b, 'commits': [self.b],
        })
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        rejected = self.cli('channel', 'plan', 'dev', expected=2)
        self.assertIn('does not match stack branch', rejected.stderr)

    def test_persisted_channel_cannot_reference_an_unknown_stack(self):
        self.create(stacks=('a',))
        data = self.manifest()
        data['channels'][0]['composition'][0]['stack'] = 'ghost'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(data, indent=2) + '\n'
        )
        for arguments in (
            ('channel', 'list'),
            ('validate', '--json'),
            ('channel', 'plan', 'dev'),
        ):
            with self.subTest(arguments=arguments):
                rejected = self.cli(*arguments, expected=2)
                self.assertIn('references unknown stack: ghost', rejected.stderr)

    def test_stack_close_refuses_active_channel_references_without_side_effects(self):
        self.create(stacks=('a',))
        module = self.load_module()
        _, manifest_path = module.load_manifest(self.repo)

        for coordination_mode in ('disabled', 'active-active'):
            with self.subTest(coordination_mode=coordination_mode):
                data = self.manifest()
                data['coordination']['mode'] = coordination_mode
                manifest_path.write_text(json.dumps(data, indent=2) + '\n')
                manifest_before = manifest_path.read_bytes()
                remote_before = self.git('ls-remote', 'origin')
                ledger_before = module.load_ledger_events(self.repo, manifest_path)

                rejected = self.cli(
                    'stack', 'close', 'a', '--force', '--reason', 'abandoned',
                    expected=2,
                )
                self.assertIn('referenced by active channel(s): dev', rejected.stderr)
                self.assertIn('remove or replace', rejected.stderr)
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertEqual(self.git('ls-remote', 'origin'), remote_before)
                self.assertEqual(
                    module.load_ledger_events(self.repo, manifest_path), ledger_before
                )

    def test_stack_close_refuses_dependents_without_side_effects(self):
        self.create('migration', stacks=())
        data = self.manifest()
        next(item for item in data['stacks'] if item['id'] == 'b')['depends_on'] = ['a']
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        module = self.load_module()

        for coordination_mode in ('disabled', 'active-active'):
            with self.subTest(coordination_mode=coordination_mode):
                data['coordination']['mode'] = coordination_mode
                manifest_path.write_text(json.dumps(data, indent=2) + '\n')
                manifest_before = manifest_path.read_bytes()
                remote_before = self.git('ls-remote', 'origin')
                ledger_before = module.load_ledger_events(self.repo, manifest_path)

                rejected = self.cli('stack', 'close', 'a', '--force', expected=2)
                self.assertIn('required by dependent stack(s): b', rejected.stderr)
                self.assertIn('close or update', rejected.stderr)
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertEqual(self.git('ls-remote', 'origin'), remote_before)
                self.assertEqual(
                    module.load_ledger_events(self.repo, manifest_path), ledger_before
                )

    def test_branch_changing_stack_promote_refuses_channel_pins_without_side_effects(self):
        self.git('branch', '-m', 'pr/a', 'syncwheel/draft/a')
        data = self.manifest()
        stack = next(item for item in data['stacks'] if item['id'] == 'a')
        stack.update({'branch': 'syncwheel/draft/a', 'state': 'draft'})
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        self.create(stacks=('a',))
        module = self.load_module()

        for coordination_mode in ('disabled', 'active-active'):
            with self.subTest(coordination_mode=coordination_mode):
                data = self.manifest()
                data['coordination']['mode'] = coordination_mode
                manifest_path.write_text(json.dumps(data, indent=2) + '\n')
                manifest_before = manifest_path.read_bytes()
                refs_before = self.git('show-ref')
                remote_before = self.git('ls-remote', 'origin')
                ledger_before = module.load_ledger_events(self.repo, manifest_path)

                rejected = self.cli('stack', 'promote', 'a', expected=2)
                self.assertIn('promotion would change branch', rejected.stderr)
                self.assertIn('pinned by active channel(s): dev', rejected.stderr)
                self.assertEqual(manifest_path.read_bytes(), manifest_before)
                self.assertEqual(self.git('show-ref'), refs_before)
                self.assertEqual(self.git('ls-remote', 'origin'), remote_before)
                self.assertEqual(
                    module.load_ledger_events(self.repo, manifest_path), ledger_before
                )

    def test_state_only_stack_promote_keeps_pinned_channel_valid(self):
        data = self.manifest()
        next(item for item in data['stacks'] if item['id'] == 'a')['state'] = 'draft'
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        self.create(stacks=('a',))

        promoted = self.cli('stack', 'promote', 'a', '--branch', 'pr/a')
        self.assertIn('branch: pr/a (unchanged)', promoted.stdout)
        manifest = self.manifest()
        stack = next(item for item in manifest['stacks'] if item['id'] == 'a')
        self.assertEqual(stack['state'], 'published')
        self.assertEqual(stack['branch'], 'pr/a')
        self.assertEqual(manifest['channels'][0]['composition'][0]['branch'], 'pr/a')
        self.cli('channel', 'plan', 'dev')

    def test_stack_demote_refuses_shared_channel_without_side_effects(self):
        self.create(stacks=('a',))
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = self.manifest()
        data['coordination']['mode'] = 'active-active'
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        module = self.load_module()
        manifest_before = manifest_path.read_bytes()
        refs_before = self.git('show-ref')
        remote_before = self.git('ls-remote', 'origin')
        ledger_before = module.load_ledger_events(self.repo, manifest_path)

        rejected = self.cli('stack', 'demote', 'a', expected=2)
        self.assertIn('referenced by shared channel(s): dev', rejected.stderr)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(self.git('show-ref'), refs_before)
        self.assertEqual(self.git('ls-remote', 'origin'), remote_before)
        self.assertEqual(module.load_ledger_events(self.repo, manifest_path), ledger_before)

    def test_stack_demote_is_allowed_for_ephemeral_only_channel_references(self):
        self.create(
            'preview', '--lifecycle', 'ephemeral',
            '--expires-at', '2030-01-01T00:00:00Z', stacks=('a',),
        )
        demoted = self.cli('stack', 'demote', 'a')
        self.assertIn('demoted published -> draft', demoted.stdout)
        manifest = self.manifest()
        stack = next(item for item in manifest['stacks'] if item['id'] == 'a')
        self.assertEqual(stack['state'], 'draft')
        self.assertEqual(manifest['channels'][0]['lifecycle'], 'ephemeral')
        publish_plan = self.plan('preview', 'publish')
        self.assertEqual(publish_plan['operation'], 'publish')

    def test_dependency_closure_and_order_are_fail_closed(self):
        self.create('migration', stacks=())
        self.git('switch', '-q', '-C', 'pr/b', 'pr/a')
        (self.repo / 'b.txt').write_text('dependent b\n')
        self.git('add', 'b.txt')
        self.git('commit', '-q', '-m', 'dependent b')
        dependent_b = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main-integration')
        data = self.manifest()
        stack_b = next(item for item in data['stacks'] if item['id'] == 'b')
        stack_b.update({
            'base': 'pr/a', 'commits': [dependent_b], 'depends_on': ['a'],
        })
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')

        missing = self.cli(
            'channel', 'create', 'missing-dependency', '--stack', 'b', expected=2
        )
        self.assertIn('requires earlier dependency', missing.stderr)
        reversed_order = self.cli(
            'channel', 'create', 'reversed', '--stack', 'b', '--stack', 'a', expected=2
        )
        self.assertIn('requires earlier dependency', reversed_order.stderr)
        self.create(stacks=('a', 'b'))
        removal = self.cli('channel', 'remove', 'dev', 'a', expected=2)
        self.assertIn('requires earlier dependency', removal.stderr)

    def test_pins_are_immutable_until_explicit_refresh(self):
        self.create(stacks=('a',))
        pinned = self.manifest()['channels'][0]['composition'][0]['branchRevision']
        advanced = self.advance_stack('a', 'a.txt', 'a two\n')
        diff = json.loads(self.cli('channel', 'diff', 'dev').stdout)
        self.assertTrue(diff['stacks'][0]['drifted'])
        self.assertEqual(self.manifest()['channels'][0]['composition'][0]['branchRevision'], pinned)
        undeclared = self.cli(
            'channel', 'refresh', 'dev', '--stack', 'a', expected=2
        )
        self.assertIn('branch range must exactly match declared', undeclared.stderr)
        self.declare_stack_tip('a', advanced)
        self.mutate('channel', 'refresh', 'dev', '--stack', 'a')
        self.assertEqual(self.manifest()['channels'][0]['composition'][0]['branchRevision'], advanced)

    def test_base_is_pinned_until_refresh_deliberately_repins_it(self):
        self.create(stacks=('a',))
        pinned = self.manifest()['channels'][0]['baseRevision']
        self.git('switch', '-q', 'main')
        (self.repo / 'base-two.txt').write_text('base two\n')
        self.git('add', 'base-two.txt')
        self.git('commit', '-q', '-m', 'advance base')
        advanced = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'origin', 'main')
        self.git('switch', '-q', 'main-integration')
        plan = self.plan()
        self.assertEqual(plan['baseRevision'], pinned)
        self.assertEqual(plan['currentBaseRevision'], advanced)
        self.assertTrue(plan['baseDrifted'])
        self.git('switch', '-q', 'pr/a')
        self.git('reset', '-q', '--hard', 'origin/main')
        self.git('cherry-pick', self.a)
        rebuilt = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main-integration')
        self.declare_stack_tip('a', rebuilt)
        self.mutate('channel', 'refresh', 'dev')
        self.assertEqual(self.manifest()['channels'][0]['baseRevision'], advanced)

    def test_add_remove_replace_preserve_order_and_promote_copies_pins(self):
        self.create(stacks=('a',))
        self.mutate('channel', 'add', 'dev', 'b', '--position', '0')
        self.assertEqual(
            [entry['stack'] for entry in self.manifest()['channels'][0]['composition']],
            ['b', 'a'],
        )
        self.mutate('channel', 'remove', 'dev', 'a')
        self.mutate('channel', 'replace', 'dev', 'b', 'a')
        source_pin = self.manifest()['channels'][0]['composition']
        self.create('test', stacks=('b',))
        self.advance_stack('a', 'a.txt', 'after pin\n')
        self.mutate('channel', 'promote', 'dev', 'test')
        channels = {item['id']: item for item in self.manifest()['channels']}
        self.assertEqual(channels['test']['composition'], source_pin)
        module = self.load_module()
        self.assertEqual(
            module.channel_composition_digest(channels['test']),
            module.channel_composition_digest(channels['dev']),
        )
        comparison = json.loads(self.cli('channel', 'diff', 'dev', '--other', 'test').stdout)
        self.assertTrue(comparison['baseEqual'])
        self.assertTrue(comparison['compositionDigestEqual'])

    def test_plan_digest_apply_receipt_and_deployment_truth(self):
        self.create()
        plan, receipt = self.apply_channel()
        self.assertEqual(plan['planDigest'], receipt['planDigest'])
        self.assertEqual(plan['compositionDigest'], receipt['compositionDigest'])
        self.assertFalse(plan['deployment']['asserted'])
        self.assertFalse(receipt['deploymentAsserted'])
        self.assertEqual(self.git('rev-parse', 'channel/dev'), receipt['tip'])
        self.assertTrue((self.repo / 'a.txt').exists() is False)  # no channel worktree

    def test_stale_plan_and_conflicting_composition_fail_closed(self):
        self.create(stacks=('a',))
        plan = self.plan()
        self.git('update-ref', 'refs/heads/channel/dev', self.base)
        result = self.cli(
            'channel', 'apply', 'dev', '--plan-digest', plan['planDigest'], '--apply',
            expected=2,
        )
        self.assertIn('plan is stale', result.stderr)

        self.git('switch', '-q', '-C', 'pr/conflict-a', 'main')
        (self.repo / 'same.txt').write_text('a\n')
        self.git('add', 'same.txt')
        self.git('commit', '-q', '-m', 'conflict a')
        ca = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', '-C', 'pr/conflict-b', 'main')
        (self.repo / 'same.txt').write_text('b\n')
        self.git('add', 'same.txt')
        self.git('commit', '-q', '-m', 'conflict b')
        cb = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main-integration')
        data = self.manifest()
        data['stacks'].extend([
            {'id': 'ca', 'branch': 'pr/conflict-a', 'base': 'origin/main', 'target_remote': 'origin', 'target_branch': 'main', 'integration_branch': 'main-integration', 'commits': [ca]},
            {'id': 'cb', 'branch': 'pr/conflict-b', 'base': 'origin/main', 'target_remote': 'origin', 'target_branch': 'main', 'integration_branch': 'main-integration', 'commits': [cb]},
        ])
        data['integration']['stacks'].extend(['ca', 'cb'])
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        self.create('conflicts', stacks=('ca', 'cb'))
        conflict_plan = self.plan('conflicts')
        result = self.cli(
            'channel', 'apply', 'conflicts', '--plan-digest', conflict_plan['planDigest'],
            '--apply', expected=2,
        )
        self.assertIn('plumbing replay stopped', result.stderr)
        self.assertEqual(self.git('show-ref', '--verify', '--quiet', 'refs/heads/channel/conflicts', check=False), '')

        source_tips = {
            'ca': self.git('rev-parse', 'pr/conflict-a'),
            'cb': self.git('rev-parse', 'pr/conflict-b'),
        }
        self.git('switch', '-q', '-c', 'resolved-channel-tree', self.base)
        (self.repo / 'same.txt').write_text('resolved\n')
        self.git('add', 'same.txt')
        self.git('commit', '-q', '-m', 'channel-only resolution')
        resolution = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main-integration')
        resolved = self.mutate(
            'channel', 'resolve', 'conflicts', '--revision', resolution
        )
        self.assertEqual(resolved['channel']['resolution']['revision'], resolution)
        resolved_plan = self.plan('conflicts')
        self.assertEqual(
            resolved['channel']['resolution']['forPinDigest'], resolved_plan['pinDigest']
        )
        self.assertNotEqual(resolved_plan['pinDigest'], resolved_plan['compositionDigest'])
        _, receipt = self.apply_channel('conflicts')
        self.assertEqual(receipt['tip'], resolution)
        self.assertEqual(self.git('rev-parse', 'pr/conflict-a'), source_tips['ca'])
        self.assertEqual(self.git('rev-parse', 'pr/conflict-b'), source_tips['cb'])
        self.create('resolved-target', stacks=('a',))
        self.mutate('channel', 'promote', 'conflicts', 'resolved-target')
        promoted = {
            item['id']: item for item in self.manifest()['channels']
        }['resolved-target']
        self.assertEqual(promoted['resolution'], resolved['channel']['resolution'])
        module = self.load_module()
        source = {
            item['id']: item for item in self.manifest()['channels']
        }['conflicts']
        self.assertEqual(
            module.channel_composition_digest(promoted),
            module.channel_composition_digest(source),
        )
        clear_preview = json.loads(self.cli(
            'channel', 'resolve', 'conflicts', '--clear'
        ).stdout)
        self.assertIn('resolution', clear_preview['before']['channel'])
        self.assertNotIn('resolution', clear_preview['after']['channel'])
        self.cli(
            'channel', 'resolve', 'conflicts', '--clear', '--plan-digest',
            clear_preview['planDigest'], '--apply',
        )
        source = {
            item['id']: item for item in self.manifest()['channels']
        }['conflicts']
        self.assertNotIn('resolution', source)
        self.mutate('channel', 'remove', 'conflicts', 'cb')
        channel = next(
            item for item in self.manifest()['channels'] if item['id'] == 'conflicts'
        )
        self.assertNotIn('resolution', channel)

    def test_manifest_edits_require_exact_preview_digest(self):
        preview = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a'
        ).stdout)
        self.assertIn('operationId', preview)
        missing = self.cli(
            'channel', 'create', 'dev', '--stack', 'a', '--apply', expected=2
        )
        self.assertIn('--plan-digest is required', missing.stderr)
        wrong = self.cli(
            'channel', 'create', 'dev', '--stack', 'a', '--plan-digest', '0' * 64,
            '--apply', expected=2,
        )
        self.assertIn('mutation plan is stale', wrong.stderr)
        self.cli(
            'channel', 'create', 'dev', '--stack', 'a', '--plan-digest',
            preview['planDigest'], '--apply',
        )
        edit = json.loads(self.cli('channel', 'add', 'dev', 'b').stdout)
        data = self.manifest()
        data['integration']['note'] = 'changes observation digest'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        stale = self.cli(
            'channel', 'add', 'dev', 'b', '--plan-digest', edit['planDigest'], '--apply',
            expected=2,
        )
        self.assertIn('mutation plan is stale', stale.stderr)

    def test_plan_envelope_digests_and_full_before_after_are_exact(self):
        preview = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a'
        ).stdout)
        self.assertEqual(preview['kind'], 'channelPlan')
        self.assertEqual(preview['request'], {
            'operation': 'create',
            'channel': 'dev',
            'parameters': preview['context'],
        })
        self.assertEqual(preview['manifestDigestBefore'], preview['before']['manifestDigest'])
        self.assertIsNone(preview['before']['channel'])
        self.assertEqual(preview['after']['channel'], preview['channel'])
        self.assertNotEqual(preview['pinDigest'], preview['compositionDigest'])
        applied = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a', '--plan-digest',
            preview['planDigest'], '--apply',
        ).stdout)
        channel = self.manifest()['channels'][0]
        self.assertEqual(preview['after']['channel'], channel)
        self.assertEqual(preview['after']['manifestDigest'], applied['proposedManifestDigest'])
        close = json.loads(self.cli('channel', 'close', 'dev').stdout)
        self.assertEqual(close['before']['channel'], channel)
        self.assertIsNone(close['after']['channel'])
        apply_plan = self.plan()
        self.assertEqual(apply_plan['request'], {
            'operation': 'apply', 'channel': 'dev',
        })
        contract = json.loads(self.cli('channel', 'contract').stdout)
        self.assertIn('request', contract['schemas']['plan']['required'])
        required_action_fields = ('id', 'type', 'target', 'before', 'intendedAfter')
        self.assertEqual(
            contract['schemas']['plan']['actionRequired'],
            list(required_action_fields),
        )
        publish_plan = self.plan(operation='publish')
        for channel_plan in (preview, close, apply_plan, publish_plan):
            self.assertEqual(channel_plan['kind'], 'channelPlan')
            for action in channel_plan['actions']:
                for field in required_action_fields:
                    self.assertIn(field, action)
        self.assertIn('actionOutcomes', contract['schemas']['receipt']['required'])

    def test_publish_and_close_actions_match_coordination_semantics(self):
        self.create(stacks=('a',))
        self.apply_channel()
        inactive = self.plan(operation='publish')
        self.assertEqual(
            [action['id'] for action in inactive['actions']], ['publish-channel-ref']
        )
        self.assertNotIn('atomicGroup', inactive['actions'][0])

        data = self.manifest()
        data['coordination']['mode'] = 'active-active'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(data, indent=2) + '\n'
        )
        active = self.plan(operation='publish')
        self.assertEqual(
            [action['id'] for action in active['actions']],
            ['publish-channel-ref', 'publish-coordination-state'],
        )
        self.assertEqual(
            {action['atomicGroup'] for action in active['actions']},
            {'coordinated-publication'},
        )
        close = json.loads(self.cli(
            'channel', 'close', 'dev', '--delete-local'
        ).stdout)
        self.assertEqual(
            [action['id'] for action in close['actions']],
            [
                'publish-coordination-state', 'update-channel-manifest',
                'delete-local-channel-ref',
            ],
        )
        self.assertNotIn('delete-remote-channel-ref', {
            action['id'] for action in close['actions']
        })

    def test_operation_id_is_digest_independent_idempotent_and_collision_safe(self):
        first = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a', '--operation-id', 'request-one'
        ).stdout)
        second = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a', '--operation-id', 'request-two'
        ).stdout)
        self.assertEqual(first['planDigest'], second['planDigest'])
        self.assertNotEqual(first['operationId'], second['operationId'])
        applied = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a', '--operation-id', 'request-one',
            '--plan-digest', first['planDigest'], '--apply',
        ).stdout)
        replayed = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a', '--operation-id', 'request-one',
            '--plan-digest', first['planDigest'], '--apply',
        ).stdout)
        self.assertEqual(replayed['status'], 'succeeded')
        self.assertEqual(replayed['operationId'], 'request-one')
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = module.require_channel(manifest, 'dev')
        before_events = module.channel_operation_events(
            self.repo, manifest_path, applied['operationId']
        )
        with self.assertRaisesRegex(module.SyncwheelError, 'already terminal'):
            module.record_channel_operation_prepared(
                self.repo, manifest_path, applied, channel, {'kind': 'manifest'}
            )
        after_events = module.channel_operation_events(
            self.repo, manifest_path, applied['operationId']
        )
        self.assertEqual(len(after_events), len(before_events))

        collision_preview = json.loads(self.cli(
            'channel', 'create', 'test', '--stack', 'b', '--operation-id', 'request-one'
        ).stdout)
        collision = self.cli(
            'channel', 'create', 'test', '--stack', 'b', '--operation-id', 'request-one',
            '--plan-digest', collision_preview['planDigest'], '--apply', expected=2,
        )
        self.assertIn('operation id collision', collision.stderr)
        self.assertNotIn('test', [item['id'] for item in self.manifest()['channels']])

    def test_cancelled_and_unresolved_operations_never_replay_mutations(self):
        self.create(stacks=('a',))
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = module.require_channel(manifest, 'dev')
        plan = module.build_channel_plan(self.repo, manifest, channel, 'apply')
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            apply=True, plan_digest=plan['planDigest'], operation_id=None,
        )
        with mock.patch.object(
            module, 'channel_mutation_checkpoint', side_effect=KeyboardInterrupt()
        ):
            with self.assertRaisesRegex(module.SyncwheelError, 'cancelled before'):
                module.command_channel_apply(args)
        self.assertFalse(self.git(
            'show-ref', '--verify', '--quiet', 'refs/heads/channel/dev', check=False
        ))
        events = module.channel_operation_events(self.repo, manifest_path, plan['operationId'])
        self.assertEqual(events[-1]['type'], 'channel_operation_receipt')
        self.assertEqual(events[-1]['payload']['status'], 'cancelled')
        with self.assertRaisesRegex(module.SyncwheelError, 'already terminal'):
            module.command_channel_apply(args)

        pending_plan = module.build_channel_plan(
            self.repo, manifest, channel, 'apply', 'pending-operation'
        )
        module.record_channel_operation_prepared(
            self.repo, manifest_path, pending_plan, channel,
            {
                'kind': 'local-ref', 'ref': 'refs/heads/channel/dev',
                'expectedRevision': None, 'intendedRevision': self.a,
            },
        )
        pending_args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev', apply=True,
            plan_digest=pending_plan['planDigest'], operation_id='pending-operation',
        )
        with self.assertRaisesRegex(module.SyncwheelError, 'prepared without a terminal'):
            module.command_channel_apply(pending_args)
        self.assertFalse(self.git(
            'show-ref', '--verify', '--quiet', 'refs/heads/channel/dev', check=False
        ))
        module.record_channel_operation_receipt(
            self.repo, manifest_path, pending_plan, 'unknown', channel,
            'simulated indeterminate boundary', evidence={'localRevision': None},
        )
        with self.assertRaisesRegex(module.SyncwheelError, 'unknown outcome'):
            module.command_channel_apply(pending_args)

        boundary_plan = module.build_channel_plan(
            self.repo, manifest, channel, 'apply', 'boundary-operation'
        )
        boundary_args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev', apply=True,
            plan_digest=boundary_plan['planDigest'], operation_id='boundary-operation',
        )
        real_git = module.git

        def interrupt_after_update(repo_root, *git_args, **kwargs):
            if git_args[:2] == ('update-ref', 'refs/heads/channel/dev'):
                real_git(repo_root, *git_args, **kwargs)
                raise KeyboardInterrupt()
            return real_git(repo_root, *git_args, **kwargs)

        with mock.patch.object(module, 'git', side_effect=interrupt_after_update):
            with self.assertRaisesRegex(module.SyncwheelError, 'outcome is unknown'):
                module.command_channel_apply(boundary_args)
        self.assertTrue(self.git('rev-parse', '--verify', 'channel/dev'))
        boundary_events = module.channel_operation_events(
            self.repo, manifest_path, 'boundary-operation'
        )
        self.assertEqual(boundary_events[-1]['payload']['status'], 'unknown')

    def test_started_only_operation_hard_stops_and_reconciles_without_replay(self):
        self.create(stacks=('a',))
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = module.require_channel(manifest, 'dev')
        plan = module.build_channel_plan(
            self.repo, manifest, channel, 'apply', 'started-only'
        )
        mutation = {
            'kind': 'local-ref',
            'ref': 'refs/heads/channel/dev',
            'expectedRevision': None,
            'intendedRevision': self.a,
        }
        started = module.channel_operation_payload(plan, channel, mutation)
        started['status'] = 'started'
        module.append_ledger_event(
            self.repo, 'channel_operation_started', started, manifest_path
        )
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            apply=True, plan_digest=plan['planDigest'], operation_id='started-only',
        )
        with self.assertRaisesRegex(module.SyncwheelError, 'started without a terminal'):
            module.command_channel_apply(args)
        self.assertFalse(self.git(
            'show-ref', '--verify', '--quiet', 'refs/heads/channel/dev', check=False
        ))

        reconcile = json.loads(self.cli(
            'channel', 'operation', 'reconcile', 'started-only'
        ).stdout)
        self.assertEqual(reconcile['kind'], 'channelPlan')
        self.assertEqual(reconcile['request'], {
            'operation': 'reconcile-outcome', 'operationId': 'started-only',
        })
        self.assertEqual(reconcile['proposedStatus'], 'failed')
        outcome = json.loads(self.cli(
            'channel', 'operation', 'reconcile', 'started-only',
            '--plan-digest', reconcile['planDigest'], '--apply',
        ).stdout)
        self.assertEqual(outcome['status'], 'failed')
        self.assertEqual(outcome['expectedAfter'], mutation)
        self.assertFalse(self.git(
            'show-ref', '--verify', '--quiet', 'refs/heads/channel/dev', check=False
        ))

    def test_manifest_lock_is_global_and_concurrent_apply_rechecks_stale_plan(self):
        self.create(stacks=('a',))
        module = self.load_module()
        _, manifest_path = module.load_manifest(self.repo)
        with module.channel_mutation_lock(self.repo, manifest_path, 'dev') as first_path:
            pass
        with module.channel_mutation_lock(self.repo, manifest_path, 'other') as second_path:
            pass
        self.assertEqual(first_path, second_path)

        plan = self.plan()
        env = os.environ.copy()
        env['SYNCWHEEL_UPDATE_MODE'] = 'off'
        argv = [
            'python3', str(CLI), 'channel', 'apply', 'dev', '--plan-digest',
            plan['planDigest'], '--apply',
        ]
        first = subprocess.Popen(argv, cwd=self.repo, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env)
        second = subprocess.Popen(argv, cwd=self.repo, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env)
        results = [first.communicate(), second.communicate()]
        codes = [first.returncode, second.returncode]
        self.assertEqual(sorted(codes), [0, 2])
        failed = results[codes.index(2)][1]
        self.assertIn('stale', failed)

    def test_manifest_global_lock_preserves_channel_against_stack_writer(self):
        self.create('migration', stacks=())
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = {
            'id': 'raced',
            'branch': 'channel/raced',
            'lifecycle': 'shared',
            'base': manifest['defaults']['base_ref'],
            'baseRevision': module.commit_full_sha(
                self.repo, manifest['defaults']['base_ref']
            ),
            'remote': manifest['defaults']['publication_remote'],
            'composition': [module.pin_stack_for_channel(self.repo, manifest, 'a')],
        }
        env = os.environ.copy()
        env['SYNCWHEEL_UPDATE_MODE'] = 'off'
        argv = ['python3', str(CLI), 'stack', 'sync', 'a']
        with module.channel_mutation_lock(self.repo, manifest_path, 'test-owner'):
            process = subprocess.Popen(
                argv, cwd=self.repo, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env,
            )
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            fresh, _ = module.load_manifest(self.repo, manifest_path)
            fresh.setdefault('channels', []).append(channel)
            module.save_manifest(manifest_path, fresh)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        final, _ = module.load_manifest(self.repo, manifest_path)
        self.assertIn('raced', module.channel_map(final))
        self.assertEqual(
            module.require_stack(final, 'a')['commits'],
            module.rev_list(self.repo, 'origin/main..pr/a'),
        )

    def test_manifest_global_lock_makes_stack_demote_see_new_shared_channel(self):
        self.create('migration', stacks=())
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = {
            'id': 'raced',
            'branch': 'channel/raced',
            'lifecycle': 'shared',
            'base': manifest['defaults']['base_ref'],
            'baseRevision': module.commit_full_sha(
                self.repo, manifest['defaults']['base_ref']
            ),
            'remote': manifest['defaults']['publication_remote'],
            'composition': [module.pin_stack_for_channel(self.repo, manifest, 'a')],
        }
        env = os.environ.copy()
        env['SYNCWHEEL_UPDATE_MODE'] = 'off'
        argv = ['python3', str(CLI), 'stack', 'demote', 'a']
        with module.channel_mutation_lock(self.repo, manifest_path, 'test-owner'):
            process = subprocess.Popen(
                argv, cwd=self.repo, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env,
            )
            time.sleep(0.2)
            self.assertIsNone(process.poll())
            fresh, _ = module.load_manifest(self.repo, manifest_path)
            fresh.setdefault('channels', []).append(channel)
            module.save_manifest(manifest_path, fresh)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 2, (stdout, stderr))
        self.assertIn('referenced by shared channel(s): raced', stderr)
        final, _ = module.load_manifest(self.repo, manifest_path)
        self.assertIn('raced', module.channel_map(final))
        self.assertEqual(module.require_stack(final, 'a')['state'], 'published')

    def test_manifest_cas_refuses_bypass_lock_edit_for_pure_stack_writer(self):
        self.create('migration', stacks=())
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        external_channel = self.pinned_channel_definition(
            module, manifest, 'external-edit'
        )
        real_save = module.save_manifest
        injected = False

        def bypass_lock_then_save(path, proposed):
            nonlocal injected
            if not injected:
                injected = True
                raw = json.loads(manifest_path.read_text())
                raw.setdefault('channels', []).append(external_channel)
                manifest_path.write_text(json.dumps(raw, indent=2) + '\n')
            return real_save(path, proposed)

        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, stack='a'
        )
        with module.manifest_write_transaction(
            self.repo, manifest_path, 'cas-pure-writer'
        ):
            with mock.patch.object(
                module, 'save_manifest', side_effect=bypass_lock_then_save
            ):
                with self.assertRaisesRegex(
                    module.SyncwheelError, 'changed outside the active transaction'
                ):
                    module.command_stack_sync(args)
        final, _ = module.load_manifest(self.repo, manifest_path)
        self.assertIn('external-edit', module.channel_map(final))

    def test_manifest_cas_stops_branch_lifecycle_before_side_effect(self):
        self.create('migration', stacks=())
        self.git('branch', '-m', 'pr/a', 'syncwheel/draft/a')
        raw = self.manifest()
        stack = next(item for item in raw['stacks'] if item['id'] == 'a')
        stack.update({
            'branch': 'syncwheel/draft/a',
            'state': 'draft',
            'publication': {'enabled': False},
        })
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest_path.write_text(json.dumps(raw, indent=2) + '\n')
        module = self.load_module()
        manifest, _ = module.load_manifest(self.repo, manifest_path)
        external_channel = self.pinned_channel_definition(
            module, manifest, 'external-edit'
        )
        real_checkpoint = module.require_manifest_transaction_current
        injected = False

        def bypass_lock_then_check(path):
            nonlocal injected
            if not injected:
                injected = True
                current = json.loads(manifest_path.read_text())
                current.setdefault('channels', []).append(external_channel)
                manifest_path.write_text(json.dumps(current, indent=2) + '\n')
            return real_checkpoint(path)

        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, stack='a', branch=None
        )
        with module.manifest_write_transaction(
            self.repo, manifest_path, 'cas-lifecycle'
        ):
            with mock.patch.object(
                module, 'require_manifest_transaction_current',
                side_effect=bypass_lock_then_check,
            ):
                with self.assertRaisesRegex(
                    module.SyncwheelError, 'changed outside the active transaction'
                ):
                    module.command_stack_promote(args)
        final, _ = module.load_manifest(self.repo, manifest_path)
        self.assertIn('external-edit', module.channel_map(final))
        self.assertEqual(module.require_stack(final, 'a')['branch'], 'syncwheel/draft/a')
        self.assertTrue(self.git('rev-parse', '--verify', 'syncwheel/draft/a'))
        self.assertEqual(
            self.git('rev-parse', '--verify', 'pr/a', check=False), ''
        )

    def test_resume_apply_joins_manifest_global_lock(self):
        self.create('migration', stacks=())
        module = self.load_module()
        _, manifest_path = module.load_manifest(self.repo)
        env = os.environ.copy()
        env['SYNCWHEEL_UPDATE_MODE'] = 'off'
        argv = [
            'python3', str(CLI), 'resume', '--apply', '--no-fetch',
            '--rebuild', 'none', '--skip-integration', '--mode', 'standard',
        ]
        with module.channel_mutation_lock(self.repo, manifest_path, 'test-owner'):
            process = subprocess.Popen(
                argv, cwd=self.repo, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env,
            )
            time.sleep(0.2)
            self.assertIsNone(process.poll())
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, (stdout, stderr))

    def test_every_existing_delivery_manifest_writer_joins_global_lock(self):
        module = self.load_module()
        tree = ast.parse(CLI.read_text())
        functions = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        calls = {
            name: {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
            for name, node in functions.items()
        }

        def reaches_saver(name, trail=()):
            if name in trail:
                return False
            for callee in calls.get(name, set()):
                if callee in {'save_manifest', 'append_ledger_event'}:
                    return True
                if callee in functions and reaches_saver(callee, (*trail, name)):
                    return True
            return False

        statically_derived = {
            name for name in functions
            if name.startswith('command_') and reaches_saver(name)
        }
        registered = {
            command.__name__ for command in module.MANIFEST_SAVER_COMMANDS
        }
        self.assertEqual(registered, statically_derived)
        for command in (
            module.command_repo_authority_set,
            module.command_coordination_compose,
        ):
            with self.subTest(command=command.__name__):
                self.assertTrue(module.manifest_mutation_requested(mock.Mock(
                    func=command, apply=True,
                )))
                self.assertFalse(module.manifest_mutation_requested(mock.Mock(
                    func=command, apply=False,
                )))

    def test_old_git_uses_ephemeral_materialization_fallback(self):
        self.create(stacks=('a',))
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = module.require_channel(manifest, 'dev')
        plan = module.build_channel_plan(self.repo, manifest, channel, 'apply')
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            apply=True, plan_digest=plan['planDigest'],
        )
        with mock.patch.object(module, 'git_supports_write_tree', return_value=False):
            module.command_channel_apply(args)
        self.assertTrue(self.git('rev-parse', '--verify', 'channel/dev'))
        self.assertNotIn('syncwheel-replay-', self.git('worktree', 'list', '--porcelain'))
        self.assertIsNotNone(manifest_path)

    def test_local_apply_receipt_failure_is_reconcileable_without_replay(self):
        self.create(stacks=('a',))
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = module.require_channel(manifest, 'dev')
        plan = module.build_channel_plan(self.repo, manifest, channel, 'apply')
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            apply=True, plan_digest=plan['planDigest'],
        )
        real_append = module.append_ledger_event

        def fail_receipt(repo_root, event_type, payload, path=None):
            if event_type == 'channel_applied':
                raise OSError('simulated receipt failure')
            return real_append(repo_root, event_type, payload, path)

        with mock.patch.object(module, 'append_ledger_event', side_effect=fail_receipt):
            with self.assertRaisesRegex(module.SyncwheelError, 'requires reconcile-outcome'):
                module.command_channel_apply(args)
        outcomes = module.channel_operation_events(self.repo, manifest_path, plan['operationId'])
        intended = outcomes[0]['payload']['mutation']['intendedRevision']
        self.assertEqual(self.git('rev-parse', 'channel/dev'), intended)
        self.assertEqual((outcomes[-1]['payload'])['status'], 'unknown')
        reconcile_plan = json.loads(self.cli(
            'channel', 'operation', 'reconcile', plan['operationId']
        ).stdout)
        self.cli(
            'channel', 'operation', 'reconcile', plan['operationId'],
            '--plan-digest', reconcile_plan['planDigest'], '--apply',
        )
        outcomes = module.channel_operation_events(self.repo, manifest_path, plan['operationId'])
        self.assertEqual((outcomes[-1]['payload'])['status'], 'succeeded')

    def test_publish_receipt_failure_retains_intent_and_reconciles_observation(self):
        self.create(stacks=('a',))
        self.apply_channel()
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = module.require_channel(manifest, 'dev')
        plan = module.build_channel_plan(self.repo, manifest, channel, 'publish')
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            apply=True, plan_digest=plan['planDigest'],
        )
        real_receipt = module.record_channel_operation_receipt

        def fail_terminal_receipt(*call_args, **call_kwargs):
            if len(call_args) > 3 and call_args[3] == 'succeeded':
                raise OSError('simulated terminal receipt failure')
            return real_receipt(*call_args, **call_kwargs)

        with mock.patch.object(
            module, 'record_channel_operation_receipt', side_effect=fail_terminal_receipt
        ):
            with self.assertRaisesRegex(module.SyncwheelError, 'requires reconcile-outcome'):
                module.command_channel_publish(args)
        remote_tip = self.git('ls-remote', 'origin', 'refs/heads/channel/dev').split()[0]
        self.assertEqual(remote_tip, plan['currentRevision'])
        events = module.channel_operation_events(self.repo, manifest_path, plan['operationId'])
        self.assertEqual(events[0]['type'], 'channel_operation_started')
        self.assertEqual(events[-1]['type'], 'channel_operation_prepared')
        reconcile_plan = json.loads(self.cli(
            'channel', 'operation', 'reconcile', plan['operationId']
        ).stdout)
        self.cli(
            'channel', 'operation', 'reconcile', plan['operationId'],
            '--plan-digest', reconcile_plan['planDigest'], '--apply',
        )
        events = module.channel_operation_events(self.repo, manifest_path, plan['operationId'])
        self.assertEqual(events[-1]['payload']['status'], 'succeeded')

    def test_active_publish_reconcile_requires_ref_and_coordination_state(self):
        self.create(stacks=('a',))
        self.apply_channel()
        self.enable_active_coordination()
        self.git('push', '-q', 'origin', 'channel/dev:refs/heads/channel/dev')
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = module.require_channel(manifest, 'dev')
        plan = module.build_channel_plan(self.repo, manifest, channel, 'publish')
        self.assertEqual(plan['remoteRevision'], plan['currentRevision'])
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            apply=True, plan_digest=plan['planDigest'], operation_id=None,
        )
        with mock.patch.object(
            module, 'coordinated_publish',
            side_effect=module.SyncwheelError('simulated atomic publication failure'),
        ):
            with self.assertRaisesRegex(module.SyncwheelError, 'simulated atomic'):
                module.command_channel_publish(args)
        events = module.channel_operation_events(
            self.repo, manifest_path, plan['operationId']
        )
        self.assertEqual(
            [event['type'] for event in events],
            [
                'channel_operation_started', 'channel_operation_prepared',
                'channel_operation_receipt',
            ],
        )
        self.assertEqual(events[-1]['payload']['status'], 'unknown')

        reconcile = json.loads(self.cli(
            'channel', 'operation', 'reconcile', plan['operationId']
        ).stdout)
        self.assertEqual(reconcile['proposedStatus'], 'partial')
        self.assertEqual(reconcile['actions'], [{
            'id': 'append-terminal-receipt',
            'type': 'append-observed-terminal-outcome',
            'target': plan['operationId'],
            'before': {
                'status': 'unknown',
                'observation': reconcile['observation'],
            },
            'intendedAfter': {
                'status': 'partial', 'reconciled': True, 'mutationRetried': False,
            },
        }])
        outcome = json.loads(self.cli(
            'channel', 'operation', 'reconcile', plan['operationId'],
            '--plan-digest', reconcile['planDigest'], '--apply',
        ).stdout)
        self.assertEqual(outcome['status'], 'partial')
        self.assertEqual(
            [item['status'] for item in outcome['actionOutcomes']],
            ['succeeded', 'failed'],
        )
        self.assertFalse(outcome['reconciled'] is False)

    def test_active_publish_attempt_marks_both_atomic_actions_unknown(self):
        self.enable_active_coordination()
        module = self.load_module()
        failures = (
            ('sync-error', module.SyncwheelError('coordination failed')),
            ('runtime-error', RuntimeError('transport failed')),
            ('interrupt', KeyboardInterrupt()),
        )
        for channel_id, failure in failures:
            with self.subTest(channel=channel_id, failure=type(failure).__name__):
                self.create(channel_id, stacks=('a',))
                self.apply_channel(channel_id)
                manifest, manifest_path = module.load_manifest(self.repo)
                channel = module.require_channel(manifest, channel_id)
                plan = module.build_channel_plan(
                    self.repo, manifest, channel, 'publish'
                )
                args = mock.Mock(
                    repo=str(self.repo), manifest=None, personal=None,
                    channel=channel_id, apply=True,
                    plan_digest=plan['planDigest'], operation_id=None,
                )
                with mock.patch.object(
                    module, 'coordinated_publish', side_effect=failure
                ):
                    with self.assertRaises(module.SyncwheelError):
                        module.command_channel_publish(args)
                terminal = module.channel_operation_events(
                    self.repo, manifest_path, plan['operationId']
                )[-1]['payload']
                self.assertEqual(terminal['status'], 'unknown')
                self.assertEqual(
                    [item['status'] for item in terminal['actionOutcomes']],
                    ['unknown', 'unknown'],
                )

    def test_active_publish_and_close_compatibility_event_failure_keep_success_receipt(self):
        self.create(stacks=('a',))
        self.apply_channel()
        self.enable_active_coordination()
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = module.require_channel(manifest, 'dev')
        publish_plan = module.build_channel_plan(
            self.repo, manifest, channel, 'publish'
        )
        publish_args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            apply=True, plan_digest=publish_plan['planDigest'], operation_id=None,
        )
        real_append = module.append_ledger_event

        def fail_publish_compat(repo_root, event_type, payload, path=None):
            if event_type == 'channel_published':
                raise OSError('compatibility event unavailable')
            return real_append(repo_root, event_type, payload, path)

        with mock.patch.object(
            module, 'append_ledger_event', side_effect=fail_publish_compat
        ):
            with self.assertRaisesRegex(
                module.SyncwheelError, 'terminal operation receipt was recorded'
            ):
                module.command_channel_publish(publish_args)
        publish_terminal = module.channel_operation_events(
            self.repo, manifest_path, publish_plan['operationId']
        )[-1]['payload']
        self.assertEqual(publish_terminal['status'], 'succeeded')
        self.assertEqual(
            [item['status'] for item in publish_terminal['actionOutcomes']],
            ['succeeded', 'succeeded'],
        )

        close_preview = json.loads(self.cli(
            'channel', 'close', 'dev', '--reason', 'compatibility-event'
        ).stdout)
        close_args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            delete_local=False, reason='compatibility-event', apply=True,
            plan_digest=close_preview['planDigest'], operation_id=None,
        )

        def fail_close_compat(repo_root, event_type, payload, path=None):
            if event_type == 'channel_closed':
                raise OSError('compatibility event unavailable')
            return real_append(repo_root, event_type, payload, path)

        with mock.patch.object(
            module, 'append_ledger_event', side_effect=fail_close_compat
        ):
            with self.assertRaisesRegex(
                module.SyncwheelError, 'terminal operation receipt was recorded'
            ):
                module.command_channel_close(close_args)
        close_terminal = module.channel_operation_events(
            self.repo, manifest_path, close_preview['operationId']
        )[-1]['payload']
        self.assertEqual(close_terminal['status'], 'succeeded')
        self.assertEqual(
            [item['status'] for item in close_terminal['actionOutcomes']],
            ['succeeded', 'succeeded'],
        )

    def test_active_close_reconcile_classifies_remote_only_as_partial(self):
        self.create(stacks=('a',))
        self.apply_channel()
        self.publish_active_channel()
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        preview = json.loads(self.cli(
            'channel', 'close', 'dev', '--delete-local', '--reason', 'fault'
        ).stdout)
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            delete_local=True, reason='fault', apply=True,
            plan_digest=preview['planDigest'], operation_id=None,
        )
        with mock.patch.object(module, 'save_manifest', side_effect=OSError('pre-replace')):
            with mock.patch.object(
                module, 'record_channel_operation_receipt',
                side_effect=OSError('terminal receipt unavailable'),
            ):
                with self.assertRaisesRegex(module.SyncwheelError, 'requires operation reconcile'):
                    module.command_channel_close(args)
        self.assertIn('dev', module.channel_map(module.load_manifest(self.repo)[0]))
        self.assertEqual(
            self.git('rev-parse', '--verify', 'channel/dev'),
            preview['observation']['localRevision'],
        )
        events = module.channel_operation_events(
            self.repo, manifest_path, preview['operationId']
        )
        self.assertEqual(events[-1]['type'], 'channel_operation_prepared')

        reconcile = json.loads(self.cli(
            'channel', 'operation', 'reconcile', preview['operationId']
        ).stdout)
        self.assertEqual(reconcile['proposedStatus'], 'partial')
        outcome = json.loads(self.cli(
            'channel', 'operation', 'reconcile', preview['operationId'],
            '--plan-digest', reconcile['planDigest'], '--apply',
        ).stdout)
        self.assertEqual(outcome['status'], 'partial')
        self.assertEqual(
            [item['status'] for item in outcome['actionOutcomes']],
            ['succeeded', 'failed', 'not-attempted'],
        )

    def test_active_close_pre_replace_failure_records_precise_partial(self):
        self.create(stacks=('a',))
        self.apply_channel()
        self.publish_active_channel()
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        preview = json.loads(self.cli(
            'channel', 'close', 'dev', '--delete-local', '--reason', 'pre-failure'
        ).stdout)
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            delete_local=True, reason='pre-failure', apply=True,
            plan_digest=preview['planDigest'], operation_id=None,
        )
        with mock.patch.object(module, 'save_manifest', side_effect=OSError('pre-replace')):
            with self.assertRaisesRegex(module.SyncwheelError, 'outcome is partial'):
                module.command_channel_close(args)
        terminal = module.channel_operation_events(
            self.repo, manifest_path, preview['operationId']
        )[-1]['payload']
        self.assertEqual(terminal['status'], 'partial')
        self.assertEqual(
            [item['status'] for item in terminal['actionOutcomes']],
            ['succeeded', 'failed', 'not-attempted'],
        )
        self.assertIn('dev', module.channel_map(module.load_manifest(self.repo)[0]))

    def test_active_close_post_replace_durability_failure_is_unknown(self):
        self.create(stacks=('a',))
        self.apply_channel()
        self.publish_active_channel()
        module = self.load_module()
        _, manifest_path = module.load_manifest(self.repo)
        preview = json.loads(self.cli(
            'channel', 'close', 'dev', '--delete-local', '--reason', 'durability'
        ).stdout)
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            delete_local=True, reason='durability', apply=True,
            plan_digest=preview['planDigest'], operation_id=None,
        )
        real_save = module.save_manifest

        def replace_then_fail(path, manifest):
            real_save(path, manifest)
            raise module.ManifestDurabilityError('post-replace durability unknown')

        with mock.patch.object(module, 'save_manifest', side_effect=replace_then_fail):
            with self.assertRaisesRegex(module.SyncwheelError, 'outcome is unknown'):
                module.command_channel_close(args)
        terminal = module.channel_operation_events(
            self.repo, manifest_path, preview['operationId']
        )[-1]['payload']
        self.assertEqual(terminal['status'], 'unknown')
        self.assertEqual(
            [item['status'] for item in terminal['actionOutcomes']],
            ['succeeded', 'unknown', 'not-attempted'],
        )
        self.assertNotIn('dev', module.channel_map(module.load_manifest(self.repo)[0]))
        self.assertEqual(
            self.git('rev-parse', '--verify', 'channel/dev'),
            preview['observation']['localRevision'],
        )

    def test_active_close_receipt_failure_reconciles_complete_state(self):
        self.create(stacks=('a',))
        self.apply_channel()
        self.publish_active_channel()
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        preview = json.loads(self.cli(
            'channel', 'close', 'dev', '--reason', 'receipt-failure'
        ).stdout)
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            delete_local=False, reason='receipt-failure', apply=True,
            plan_digest=preview['planDigest'], operation_id=None,
        )
        real_receipt = module.record_channel_operation_receipt

        def fail_terminal_receipt(*call_args, **call_kwargs):
            if len(call_args) > 3 and call_args[3] == 'succeeded':
                raise OSError('terminal receipt unavailable')
            return real_receipt(*call_args, **call_kwargs)

        with mock.patch.object(
            module, 'record_channel_operation_receipt', side_effect=fail_terminal_receipt
        ):
            with self.assertRaisesRegex(module.SyncwheelError, 'requires reconcile-outcome'):
                module.command_channel_close(args)
        events = module.channel_operation_events(
            self.repo, manifest_path, preview['operationId']
        )
        self.assertEqual(events[-1]['type'], 'channel_operation_prepared')
        reconcile = json.loads(self.cli(
            'channel', 'operation', 'reconcile', preview['operationId']
        ).stdout)
        self.assertEqual(reconcile['proposedStatus'], 'succeeded')
        outcome = json.loads(self.cli(
            'channel', 'operation', 'reconcile', preview['operationId'],
            '--plan-digest', reconcile['planDigest'], '--apply',
        ).stdout)
        self.assertEqual(outcome['status'], 'succeeded')
        self.assertEqual(
            [item['status'] for item in outcome['actionOutcomes']],
            ['succeeded', 'succeeded'],
        )

    def test_contract_and_operation_inspection_are_read_only(self):
        contract = json.loads(self.cli('channel', 'contract').stdout)
        self.assertEqual(contract['contractVersion'], 1)
        self.assertEqual(contract['manifestVersion'], 3)
        self.assertFalse(contract['truth']['publishedBranchIsDeploymentProof'])
        created = self.create(stacks=('a',))
        operation_id = created['operationId']
        listed = json.loads(self.cli('channel', 'operation', 'list').stdout)
        self.assertIn(operation_id, [item['operationId'] for item in listed['operations']])
        shown = json.loads(self.cli(
            'channel', 'operation', 'show', operation_id
        ).stdout)
        self.assertEqual(shown['status'], 'succeeded')
        filtered = json.loads(self.cli(
            'channel', 'operation', 'list', '--channel', 'dev', '--status', 'succeeded'
        ).stdout)
        self.assertEqual([item['operationId'] for item in filtered['operations']], [operation_id])
        terminal = shown['events'][-1]
        self.assertEqual(terminal['type'], 'channel_operation_receipt')
        for field in ('before', 'expectedAfter', 'observedAfter', 'actions', 'status'):
            self.assertIn(field, terminal['payload'])
        started, prepared, receipt = [event['payload'] for event in shown['events']]
        for payload in (started, prepared, receipt):
            self.assertEqual(payload['request'], created['request'])
            self.assertEqual(payload['before'], created['before'])
            self.assertEqual(payload['after'], created['after'])
            self.assertEqual(payload['context'], created['context'])
            self.assertEqual(payload['actions'], created['actions'])
        self.assertLessEqual(started['startedAt'], prepared['preparedAt'])
        self.assertLessEqual(prepared['preparedAt'], receipt['completedAt'])
        self.assertEqual(
            [item['id'] for item in receipt['actionOutcomes']],
            [item['id'] for item in receipt['actions']],
        )
        self.assertTrue(all(
            item['status'] == 'succeeded' for item in receipt['actionOutcomes']
        ))
        receipts = json.loads(self.cli('channel', 'receipt', 'show', 'dev').stdout)
        self.assertEqual(receipts['receipts'][-1]['type'], 'channel_operation_receipt')

    def test_manifest_atomic_writer_failure_boundaries_and_mode(self):
        module = self.load_module()
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        os.chmod(manifest_path, 0o640)
        original = manifest_path.read_bytes()
        proposed = self.manifest()
        proposed['integration']['note'] = 'atomic replacement'

        with mock.patch.object(module.os, 'fsync', side_effect=OSError('write fsync')):
            with self.assertRaises(OSError):
                module.save_manifest(manifest_path, proposed)
        self.assertEqual(manifest_path.read_bytes(), original)
        self.assertEqual(list(manifest_path.parent.glob('.manifest.json.tmp-*')), [])

        with mock.patch.object(module.os, 'replace', side_effect=OSError('replace')):
            with self.assertRaises(OSError):
                module.save_manifest(manifest_path, proposed)
        self.assertEqual(manifest_path.read_bytes(), original)
        self.assertEqual(list(manifest_path.parent.glob('.manifest.json.tmp-*')), [])

        module.save_manifest(manifest_path, proposed)
        self.assertEqual(self.manifest()['integration']['note'], 'atomic replacement')
        self.assertEqual(os.stat(manifest_path).st_mode & 0o777, 0o640)

    def test_post_replace_directory_failures_are_durability_unknown(self):
        module = self.load_module()
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        proposed = self.manifest()
        proposed['integration']['note'] = 'replaced'
        real_fsync = module.os.fsync

        def fail_directory_fsync(descriptor):
            if os.path.isdir(f'/proc/self/fd/{descriptor}'):
                raise OSError('directory fsync')
            return real_fsync(descriptor)

        with mock.patch.object(module.os, 'fsync', side_effect=fail_directory_fsync):
            with self.assertRaises(module.ManifestDurabilityError):
                module.save_manifest(manifest_path, proposed)
        self.assertEqual(self.manifest()['integration']['note'], 'replaced')
        self.assertEqual(list(manifest_path.parent.glob('.manifest.json.tmp-*')), [])

        proposed['integration']['note'] = 'open failed after replace'
        real_open = module.os.open

        def fail_parent_open(path, *args, **kwargs):
            if Path(path) == manifest_path.parent:
                raise OSError('parent open')
            return real_open(path, *args, **kwargs)

        with mock.patch.object(module.os, 'open', side_effect=fail_parent_open):
            with self.assertRaises(module.ManifestDurabilityError):
                module.save_manifest(manifest_path, proposed)
        self.assertEqual(self.manifest()['integration']['note'], 'open failed after replace')
        self.assertEqual(list(manifest_path.parent.glob('.manifest.json.tmp-*')), [])

        proposed['integration']['note'] = 'close failed after replace'
        real_close = module.os.close

        def close_parent_then_fail(descriptor):
            if os.path.isdir(f'/proc/self/fd/{descriptor}'):
                real_close(descriptor)
                raise OSError('parent close')
            return real_close(descriptor)

        with mock.patch.object(module.os, 'close', side_effect=close_parent_then_fail):
            with self.assertRaises(module.ManifestDurabilityError):
                module.save_manifest(manifest_path, proposed)
        self.assertEqual(self.manifest()['integration']['note'], 'close failed after replace')
        self.assertEqual(list(manifest_path.parent.glob('.manifest.json.tmp-*')), [])

    def test_channel_manifest_directory_fsync_failure_records_unknown_receipt(self):
        preview = json.loads(self.cli(
            'channel', 'create', 'dev', '--stack', 'a'
        ).stdout)
        module = self.load_module()
        _, manifest_path = module.load_manifest(self.repo)
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            branch=None, lifecycle='shared', expires_at=None, stack=['a'],
            base=None, remote=None, apply=True, plan_digest=preview['planDigest'],
            operation_id=None,
        )
        real_fsync = module.os.fsync

        def fail_directory_fsync(descriptor):
            if os.path.isdir(f'/proc/self/fd/{descriptor}'):
                raise OSError('directory fsync')
            return real_fsync(descriptor)

        with mock.patch.object(module.os, 'fsync', side_effect=fail_directory_fsync):
            with self.assertRaisesRegex(module.SyncwheelError, 'durability is unknown'):
                module.command_channel_create(args)
        manifest, _ = module.load_manifest(self.repo, manifest_path)
        self.assertIn('dev', module.channel_map(manifest))
        terminal = module.channel_operation_events(
            self.repo, manifest_path, preview['operationId']
        )[-1]['payload']
        self.assertEqual(terminal['status'], 'unknown')
        self.assertEqual(
            [item['status'] for item in terminal['actionOutcomes']], ['unknown']
        )

    def test_publish_exact_lease_and_close_never_delete_remote(self):
        self.create(stacks=('a',))
        _, applied = self.apply_channel()
        publish_plan = self.plan(operation='publish')
        receipt = json.loads(self.cli(
            'channel', 'publish', 'dev', '--plan-digest', publish_plan['planDigest'], '--apply'
        ).stdout)
        self.assertEqual(receipt['publishedRevision'], applied['tip'])
        remote_before = self.git('ls-remote', 'origin', 'refs/heads/channel/dev').split()[0]
        closed = self.mutate(
            'channel', 'close', 'dev', '--delete-local', '--reason', 'expired'
        )
        self.assertTrue(closed['localRefDeleted'])
        self.assertFalse(closed['remoteRefDeleted'])
        self.assertEqual(
            self.git('ls-remote', 'origin', 'refs/heads/channel/dev').split()[0], remote_before
        )
        self.assertFalse(self.git('show-ref', '--verify', '--quiet', 'refs/heads/channel/dev', check=False))

    def test_close_reobserves_remote_under_lock_before_any_mutation(self):
        self.create(stacks=('a',))
        module = self.load_module()

        for coordination_mode, locked_observation, message in (
            (
                'disabled',
                {'known': False, 'revision': None, 'error': 'simulated outage'},
                'remote ref is unknown',
            ),
            (
                'active-active',
                {'known': True, 'revision': self.a, 'error': None},
                'remote ref changed',
            ),
        ):
            with self.subTest(coordination_mode=coordination_mode):
                data = self.manifest()
                data['coordination']['mode'] = coordination_mode
                (self.repo / '.syncwheel' / 'manifest.json').write_text(
                    json.dumps(data, indent=2) + '\n'
                )
                preview = json.loads(self.cli(
                    'channel', 'close', 'dev', '--reason', 'review injection'
                ).stdout)
                manifest, manifest_path = module.load_manifest(self.repo)
                channel = module.require_channel(manifest, 'dev')
                observed = module.channel_remote_observation(self.repo, channel)
                args = mock.Mock(
                    repo=str(self.repo), manifest=None, personal=None, channel='dev',
                    reason='review injection', delete_local=False, apply=True,
                    plan_digest=preview['planDigest'], operation_id=None,
                )
                with mock.patch.object(
                    module, 'channel_remote_observation',
                    side_effect=[observed, locked_observation],
                ):
                    with self.assertRaisesRegex(module.SyncwheelError, message):
                        module.command_channel_close(args)
                current, _ = module.load_manifest(self.repo, manifest_path)
                self.assertIn('dev', module.channel_map(current))
                self.assertEqual(
                    module.channel_operation_events(
                        self.repo, manifest_path, preview['operationId']
                    ),
                    [],
                )

    def test_shared_publish_refuses_draft_stacks_but_ephemeral_plan_is_allowed(self):
        data = self.manifest()
        data['stacks'][0]['state'] = 'draft'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        self.create('shared', stacks=('a',))
        result = self.cli(
            'channel', 'plan', 'shared', '--operation', 'publish', expected=2
        )
        self.assertIn('refuses draft stack', result.stderr)
        self.create(
            'preview', '--lifecycle', 'ephemeral',
            '--expires-at', '2030-01-01T00:00:00Z', stacks=('a',),
        )
        ephemeral = self.plan('preview', 'publish')
        self.assertEqual(ephemeral['lifecycle'], 'ephemeral')

    def test_ephemeral_requires_expiry_and_expiry_does_not_auto_cleanup(self):
        result = self.cli(
            'channel', 'create', 'preview', '--lifecycle', 'ephemeral', '--stack', 'a',
            '--apply', expected=2,
        )
        self.assertIn('requires --expires-at', result.stderr)
        self.create(
            'preview', '--lifecycle', 'ephemeral', '--expires-at', '2020-01-01T00:00:00Z',
            stacks=('a',),
        )
        validation = json.loads(self.cli('validate', '--json', expected=0).stdout)
        preview = next(item for item in validation['details']['channels'] if item['id'] == 'preview')
        self.assertTrue(preview['expired'])
        self.assertEqual(self.manifest()['channels'][0]['id'], 'preview')

    def test_coordination_protocol_v3_carries_channels_and_v2_state_stays_compatible(self):
        self.create(stacks=('a',))
        module = self.load_module()
        manifest, _ = module.load_manifest(self.repo)
        snapshot = module.coordination_manifest_snapshot(manifest, self.repo)
        self.assertEqual(snapshot['version'], 3)
        self.assertEqual(snapshot['channels'][0]['id'], 'dev')
        self.assertIn('refs/heads/channel/dev', module.managed_ref_names(manifest))
        state = module.build_coordination_state(
            self.repo, manifest, module.coordination_config(manifest),
            {'tip': None, 'state': None}, {}, {}, 'test', 'partial', 'test-install',
        )
        self.assertEqual(state['schema_version'], 3)
        module.validate_coordination_state(state)
        downgraded = json.loads(json.dumps(state))
        downgraded['schema_version'] = 2
        with self.assertRaisesRegex(module.SyncwheelError, 'incompatible'):
            module.validate_coordination_state(downgraded)

        self.write_manifest(version=2)
        manifest_v2, _ = module.load_manifest(self.repo)
        state_v2 = module.build_coordination_state(
            self.repo, manifest_v2, module.coordination_config(manifest_v2),
            {'tip': None, 'state': None}, {}, {}, 'test', 'partial', 'test-install',
        )
        self.assertEqual(state_v2['schema_version'], 2)
        module.validate_coordination_state(state_v2)

    def test_active_publication_atomically_carries_channel_and_v2_local_fails_closed(self):
        data = self.manifest()
        data['coordination']['mode'] = 'active-active'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        self.create(stacks=('a',))
        self.apply_channel()
        publish_plan = self.plan(operation='publish')
        receipt = json.loads(self.cli(
            'channel', 'publish', 'dev', '--plan-digest', publish_plan['planDigest'], '--apply'
        ).stdout)
        self.assertIsNotNone(receipt['coordinationState'])
        state_ref = 'refs/heads/syncwheel/state/channel-test'
        state_tip = self.git('ls-remote', 'origin', state_ref).split()[0]
        self.git('fetch', '-q', 'origin', state_ref)
        state = json.loads(self.git('show', 'FETCH_HEAD:.syncwheel/coordination-state.json'))
        self.assertEqual(state['schema_version'], 3)
        self.assertEqual(state['manifest']['channels'][0]['id'], 'dev')
        self.assertEqual(
            state['managed_refs']['refs/heads/channel/dev'], receipt['publishedRevision']
        )
        self.assertEqual(state_tip, receipt['coordinationState'])

        close_preview = json.loads(self.cli(
            'channel', 'close', 'dev', '--reason', 'expired'
        ).stdout)
        self.assertEqual(
            close_preview['observation']['coordinationStateRevision'], state_tip
        )
        closed = json.loads(self.cli(
            'channel', 'close', 'dev', '--reason', 'expired', '--plan-digest',
            close_preview['planDigest'], '--apply',
        ).stdout)
        self.assertFalse(closed['remoteRefDeleted'])
        self.assertEqual(
            self.git('ls-remote', 'origin', 'refs/heads/channel/dev').split()[0],
            receipt['publishedRevision'],
        )
        self.git('fetch', '-q', 'origin', state_ref)
        closed_state = json.loads(self.git('show', 'FETCH_HEAD:.syncwheel/coordination-state.json'))
        self.assertEqual(closed_state['schema_version'], 3)
        self.assertEqual(closed_state['manifest']['channels'], [])
        self.assertTrue(any(
            tombstone.get('ref') == 'refs/heads/channel/dev'
            for tombstone in closed_state['tombstones']
        ))
        module = self.load_module()
        _, manifest_path = module.load_manifest(self.repo)
        close_events = [
            event for event in module.channel_operation_events(self.repo, manifest_path)
            if event['payload'].get('operation') == 'close'
        ]
        self.assertEqual(close_events[0]['type'], 'channel_operation_started')
        self.assertEqual(close_events[-1]['payload']['status'], 'succeeded')

        self.write_manifest(version=2)
        data = self.manifest()
        data['coordination']['mode'] = 'active-active'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        rejected = self.cli('handoff', '--json', expected=2)
        self.assertIn('incompatible with local manifest version 2', rejected.stderr)

    def test_active_channel_remote_must_match_coordination_remote(self):
        data = self.manifest()
        data['coordination']['mode'] = 'active-active'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        rejected = self.cli(
            'channel', 'create', 'dev', '--remote', 'other', '--stack', 'a', '--apply',
            expected=2,
        )
        self.assertIn('must match coordination.remote', rejected.stderr)
        self.assertEqual(self.manifest()['version'], 2)

    def test_publish_lease_failure_is_bounded(self):
        self.create(stacks=('a',))
        self.apply_channel()
        module = self.load_module()
        manifest, manifest_path = module.load_manifest(self.repo)
        channel = module.require_channel(manifest, 'dev')
        plan = module.build_channel_plan(self.repo, manifest, channel, 'publish')
        args = mock.Mock(
            repo=str(self.repo), manifest=None, personal=None, channel='dev',
            apply=True, plan_digest=plan['planDigest'],
        )
        real_git = module.git

        def reject_push(repo_root, *git_args, **kwargs):
            if git_args and git_args[0] == 'push':
                return subprocess.CompletedProcess([], 1, '', 'stale info')
            return real_git(repo_root, *git_args, **kwargs)

        with mock.patch.object(module, 'git', side_effect=reject_push):
            with self.assertRaisesRegex(module.SyncwheelError, 'lease lost'):
                module.command_channel_publish(args)
        self.assertIsNotNone(manifest_path)


if __name__ == '__main__':
    unittest.main()
