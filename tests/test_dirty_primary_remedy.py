import json
import shlex
import unittest
from pathlib import Path

import test_coordination as coordination_fixtures
import test_metadata_preflight as metadata_fixtures
import test_stack_sync_metadata as sync_fixtures


class DirtyPrimaryRemedyTest(unittest.TestCase):
    def fixture(self, *, empty_manifest=False):
        fixture = metadata_fixtures.MetadataPreflightTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.module.ensure_syncwheel_metadata_excluded(fixture.repo, 'git-tracked')
        special = 'product [review].txt'
        (fixture.repo / special).write_text('original\n')
        (fixture.repo / 'product r.txt').write_text('leave this path alone\n')
        fixture.git('add', '--', '.gitignore', special, 'product r.txt')
        fixture.git('commit', '-qm', 'Add product fixture paths')
        if empty_manifest:
            manifest = json.loads(fixture.manifest_path.read_text())
            manifest['stacks'] = []
            manifest['integration']['stacks'] = []
            fixture.manifest_path.write_text(json.dumps(manifest))
            fixture.git('add', '.syncwheel/manifest.json')
            fixture.git('commit', '-qm', 'Remove fixture stack')
        else:
            fixture.prepare_stack(draft=True)
        base = fixture.git('rev-parse', 'HEAD').decode().strip()
        fixture.git('update-ref', 'refs/remotes/origin/main', base)
        return fixture, base, special

    @staticmethod
    def observed_state(fixture):
        module = fixture.module
        ledger = module.ledger_root(fixture.repo, fixture.manifest_path)
        registry = module.governed_worktree_registry_path(fixture.repo)
        product_paths = [
            fixture.product, fixture.repo / 'untracked-product',
            *fixture.repo.glob('product *.txt'),
        ]
        return {
            'repository': fixture.state(),
            'registry': fixture.read_if_present(registry),
            'manifest': fixture.manifest_path.read_bytes(),
            'ledger': {str(path.relative_to(ledger)): path.read_bytes()
                       for path in ledger.rglob('*') if path.is_file()},
            'product': {path.name: sync_fixtures.file_state(path) for path in product_paths},
        }

    def blocking_remedies(self, fixture):
        before = self.observed_state(fixture)
        result = fixture.fixture.run_syncwheel('int', 'git', '--', 'add', 'seed')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('primary checkout is dirty', result.stderr)
        self.assertEqual(self.observed_state(fixture), before)
        refusal = next(line for line in result.stderr.splitlines() if line.startswith('error:'))
        self.assertNotIn('capture-integration', refusal)
        return [shlex.split(command) for command in refusal.split('Use: ', 1)[1].split('; ')]

    def run_printed(self, fixture, argv, stack='feature'):
        self.assertEqual(argv[0], 'syncwheel')
        fixture.cli(*(stack if word == '<stack-id>' else word for word in argv[1:]))

    def test_printed_absorb_transfers_exact_literal_path_as_first_child(self):
        fixture, base, special = self.fixture()
        fixture.dirty_product()
        selected_path = fixture.repo / special
        selected_path.write_text('new product bytes\n')
        (fixture.repo / 'product r.txt').write_text('still owned by another writer\n')
        remedies = self.blocking_remedies(fixture)
        command = next(argv for argv in remedies if argv[:3] == ['syncwheel', 'stack', 'absorb']
                       and ':(literal)' + special in argv)
        self.assertIn('-N', command)
        self.assertIn('-m', command)
        self.assertIn('--repo', command)
        self.assertEqual(command[command.index('--repo') + 1], str(fixture.repo))
        self.assertEqual(command[command.index('--manifest') + 1], str(fixture.manifest_path))
        self.run_printed(fixture, command)

        branch = 'syncwheel/draft/feature'
        self.assertEqual(fixture.git('rev-parse', branch + '^').decode().strip(), base)
        fixture.git('merge-base', '--is-ancestor', base, 'HEAD')
        self.assertEqual(fixture.git('diff', '--name-only', base, 'HEAD').splitlines(),
                         [b'.syncwheel/manifest.json'])
        self.assertEqual(fixture.git('diff', '--name-only', base, branch).decode().strip(), special)
        self.assertEqual(fixture.git('show', branch + ':' + special), b'new product bytes\n')
        manifest = json.loads(fixture.manifest_path.read_text())
        self.assertEqual(manifest['stacks'][0]['commits'], [
            fixture.git('rev-parse', branch).decode().strip(),
        ])
        self.assertEqual(selected_path.read_bytes(), b'original\n')
        self.assertEqual((fixture.repo / 'product r.txt').read_bytes(), b'still owned by another writer\n')
        self.assertEqual(fixture.product.read_bytes(), b'product edit\n')

    def test_printed_draft_creation_unblocks_absorb_on_dirty_primary(self):
        fixture, base, _ = self.fixture(empty_manifest=True)
        fixture.dirty_product()
        remedies = self.blocking_remedies(fixture)
        create = next(argv for argv in remedies if argv[:3] == ['syncwheel', 'stack', 'create'])
        absorb = next(argv for argv in remedies if argv[:3] == ['syncwheel', 'stack', 'absorb'])
        self.run_printed(fixture, create)
        self.assertEqual(fixture.product.read_bytes(), b'product edit\n')
        self.run_printed(fixture, absorb)
        branch = 'syncwheel/draft/feature'
        self.assertEqual(fixture.git('rev-parse', branch + '^').decode().strip(), base)
        self.assertEqual(fixture.git('show', branch + ':seed'), b'product edit\n')
        self.assertEqual(fixture.product.read_bytes(), b'seed\n')

    def test_empty_amend_refuses_before_registry_or_primary_changes(self):
        fixture, base, _ = self.fixture()
        opened = json.loads(fixture.cli('worktree', 'open', 'foreign', '--json').stdout)
        old = Path(opened['lane']['path'])
        fixture.git('worktree', 'move', str(old), str(old.with_name('foreign-moved')))
        fixture.dirty_product()
        fixture.product.chmod(0o755)
        before = self.observed_state(fixture)

        result = fixture.fixture.run_syncwheel('stack', 'absorb', 'feature', 'seed')

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('has no commits to amend', result.stderr)
        self.assertIn('-N/--no-amend', result.stderr)
        self.assertEqual(fixture.git('rev-parse', 'syncwheel/draft/feature').decode().strip(), base)
        self.assertEqual(self.observed_state(fixture), before)

    def test_empty_stack_with_unowned_tip_refuses_first_commit_without_writes(self):
        fixture, base, _ = self.fixture()
        branch = 'syncwheel/draft/feature'
        foreign = fixture.git(
            'commit-tree', base + '^{tree}', '-p', base, '-m', 'Foreign commit',
        ).decode().strip()
        fixture.git('update-ref', 'refs/heads/' + branch, foreign)
        fixture.dirty_product()
        before = self.observed_state(fixture)

        result = fixture.fixture.run_syncwheel(
            'stack', 'absorb', 'feature', 'seed', '-N', '-m', 'First product patch',
        )

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('branch does not match its base', result.stderr)
        self.assertIn('new draft', result.stderr)
        self.assertEqual(fixture.git('rev-parse', branch).decode().strip(), foreign)
        self.assertEqual(self.observed_state(fixture), before)

    def test_first_absorb_materializes_missing_branch_from_declared_base(self):
        fixture, base, _ = self.fixture()
        branch = 'syncwheel/draft/feature'
        fixture.git('update-ref', '-d', 'refs/heads/' + branch)
        unrelated = fixture.git(
            'commit-tree', base + '^{tree}', '-p', base, '-m', 'Unrelated remote copy',
        ).decode().strip()
        fixture.git('update-ref', 'refs/remotes/origin/' + branch, unrelated)
        fixture.dirty_product()

        fixture.cli('stack', 'absorb', 'feature', 'seed', '-N', '-m', 'First product patch')

        self.assertEqual(fixture.git('rev-parse', branch + '^').decode().strip(), base)
        self.assertEqual(fixture.git('show', branch + ':seed'), b'product edit\n')
        self.assertEqual(fixture.git('rev-parse', 'origin/' + branch).decode().strip(), unrelated)

    def test_empty_amend_does_not_recover_pending_control_intent(self):
        fixture = coordination_fixtures.ActiveActiveCoordinationTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        origin = fixture.create_remote()
        repo = fixture.clone(origin, 'work')
        fixture.init_coordinated(repo)
        fixture.anchor_generated_metadata_in_replay_base(repo)
        fixture.run_cli(repo, 'int', 'push')
        fixture.run_cli(repo, 'stack', 'create', 'empty', '--draft')
        _manifest, selected = fixture.stage_owned_control_manifest_delta(repo)
        fixture.interrupt_legacy_control_persistence(repo, 'manifest_saved')
        module = fixture.load_module()
        ledger = module.ledger_root(repo, selected)
        self.assertEqual(len(module.pending_control_manifest_intents(
            module.load_ledger_events(repo, selected)
        )), 1)
        product = repo / 'README.md'
        product.write_text('Pending product patch\n')
        product.chmod(0o755)
        observer = sync_fixtures.StackSyncMetadataTest()
        before = observer.checkout_state(fixture, repo, origin)
        manifest_before = sync_fixtures.file_state(selected)
        ledger_before = sync_fixtures.directory_state(ledger)

        result = fixture.run_cli_unchecked(repo, 'stack', 'absorb', 'empty', 'README.md')

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('has no commits to amend', result.stderr)
        self.assertEqual(observer.checkout_state(fixture, repo, origin), before)
        self.assertEqual(sync_fixtures.file_state(selected), manifest_before)
        self.assertEqual(sync_fixtures.directory_state(ledger), ledger_before)
