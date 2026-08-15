import importlib.util
import json
import os
import subprocess
import tempfile
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

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, check=True):
        result = subprocess.run(
            ['git', *args], cwd=self.repo, text=True, capture_output=True
        )
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
        arguments = ['channel', 'create', channel, '--apply']
        for stack in stacks:
            arguments.extend(['--stack', stack])
        arguments.extend(extra)
        return json.loads(self.cli(*arguments).stdout)

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

    def test_pins_are_immutable_until_explicit_refresh(self):
        self.create(stacks=('a',))
        pinned = self.manifest()['channels'][0]['composition'][0]['branchRevision']
        advanced = self.advance_stack('a', 'a.txt', 'a two\n')
        diff = json.loads(self.cli('channel', 'diff', 'dev').stdout)
        self.assertTrue(diff['stacks'][0]['drifted'])
        self.assertEqual(self.manifest()['channels'][0]['composition'][0]['branchRevision'], pinned)
        self.cli('channel', 'refresh', 'dev', '--stack', 'a')
        self.assertEqual(self.manifest()['channels'][0]['composition'][0]['branchRevision'], pinned)
        self.cli('channel', 'refresh', 'dev', '--stack', 'a', '--apply')
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
        self.cli('channel', 'refresh', 'dev', '--apply')
        self.assertEqual(self.manifest()['channels'][0]['baseRevision'], advanced)

    def test_add_remove_replace_preserve_order_and_promote_copies_pins(self):
        self.create(stacks=('a',))
        self.cli('channel', 'add', 'dev', 'b', '--position', '0', '--apply')
        self.assertEqual(
            [entry['stack'] for entry in self.manifest()['channels'][0]['composition']],
            ['b', 'a'],
        )
        self.cli('channel', 'remove', 'dev', 'a', '--apply')
        self.cli('channel', 'replace', 'dev', 'b', 'a', '--apply')
        source_pin = self.manifest()['channels'][0]['composition']
        self.create('test', stacks=('b',))
        self.advance_stack('a', 'a.txt', 'after pin\n')
        self.cli('channel', 'promote', 'dev', 'test', '--apply')
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

    def test_publish_exact_lease_and_close_never_delete_remote(self):
        self.create(stacks=('a',))
        _, applied = self.apply_channel()
        publish_plan = self.plan(operation='publish')
        receipt = json.loads(self.cli(
            'channel', 'publish', 'dev', '--plan-digest', publish_plan['planDigest'], '--apply'
        ).stdout)
        self.assertEqual(receipt['publishedRevision'], applied['tip'])
        remote_before = self.git('ls-remote', 'origin', 'refs/heads/channel/dev').split()[0]
        closed = json.loads(self.cli(
            'channel', 'close', 'dev', '--delete-local', '--reason', 'expired', '--apply'
        ).stdout)
        self.assertTrue(closed['localRefDeleted'])
        self.assertFalse(closed['remoteRefDeleted'])
        self.assertEqual(
            self.git('ls-remote', 'origin', 'refs/heads/channel/dev').split()[0], remote_before
        )
        self.assertFalse(self.git('show-ref', '--verify', '--quiet', 'refs/heads/channel/dev', check=False))

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

        closed = json.loads(self.cli(
            'channel', 'close', 'dev', '--reason', 'expired', '--apply'
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
