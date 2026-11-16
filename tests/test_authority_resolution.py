import json
import os
import stat
import subprocess
import sys
import unittest

import test_managed_ref_guard as guard_fixtures


class AuthorityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = guard_fixtures.ManagedRefGuardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.repo
        self.shared = self.root / '.syncwheel' / 'manifest.json'
        self.selected = self.root / '.syncwheel' / 'selected.json'
        self.fixture.write_selected_manifest(self.selected, 'lane-integration')
        manifest = self.fixture.manifest
        manifest['authority'] = {
            'mode': 'ai-managed',
            'allow': ['source_change'],
        }
        self.shared.write_text(json.dumps(manifest))

    def test_selected_manifest_inherits_primary_authority(self):
        result = self.fixture.run_syncwheel(
            'repo', 'authority', 'status', '--manifest', str(self.selected), '--json',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['authority']['mode'], 'ai-managed')
        self.assertFalse(report['authority_present'])
        self.assertEqual(report['authority_provenance']['source'], 'inherited')
        self.assertNotIn('authority', json.loads(self.selected.read_text()))

    def test_explicit_narrower_policy_keeps_shared_denials(self):
        shared = json.loads(self.shared.read_text())
        shared['authority']['deny'] = ['runtime_change']
        self.shared.write_text(json.dumps(shared))
        self.write_selected_authority({'mode': 'ai-managed', 'allow': ['source_change']})
        before = self.selected.read_bytes()
        result = self.fixture.run_syncwheel(
            'repo', 'authority', 'status', '--manifest', str(self.selected), '--json',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['authority']['allow'], ['source_change'])
        self.assertIn('runtime_change', report['authority']['deny'])
        self.assertEqual(report['authority_provenance']['source'], 'selected')
        self.assertEqual(self.selected.read_bytes(), before)

    def test_explicit_null_remains_human_gated_and_shared_rebuild_reason_is_required(self):
        self.write_selected_authority(None)
        result = self.fixture.run_syncwheel(
            'repo', 'authority', 'status', '--manifest', str(self.selected), '--json',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['authority']['mode'], 'human-gated')
        self.assertTrue(report['authority_present'])
        self.assertEqual(report['authority_provenance']['source'], 'selected')
        result = self.fixture.run_syncwheel(
            'int', 'rebuild', '--manifest', str(self.selected),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('int rebuild requires --reason for an ai-managed repository', result.stderr)

    def test_local_escalation_is_rejected_in_reports_and_setter(self):
        before = self.selected.read_bytes()
        for flags in ((), ('--apply',)):
            with self.subTest(flags=flags):
                result = self.fixture.run_syncwheel(
                    'repo', 'authority', 'set', 'ai-managed', '--allow', 'runtime_change',
                    '--manifest', str(self.selected), *flags,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('selected authority exceeds canonical primary authority', result.stderr)
                self.assertIn('runtime_change', result.stderr)
                self.assertEqual(self.selected.read_bytes(), before)
        self.write_selected_authority({'mode': 'ai-managed', 'allow': ['runtime_change']})
        result = self.fixture.run_syncwheel(
            'repo', 'authority', 'status', '--manifest', str(self.selected), '--json',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('selected authority exceeds canonical primary authority', result.stderr)

    def test_revoked_local_policy_can_be_repaired_without_widening_shared_policy(self):
        self.fixture._install_and_branch('main-integration')
        with (self.root / '.gitignore').open('a') as ignore:
            ignore.write(
                '\n.syncwheel/manifests/*.local.json\n.syncwheel/manifests/*.local-ledger/\n'
            )
        for command in (
            ('add', '.gitignore', '.syncwheel/manifest.json'),
            ('commit', '-qm', 'Track shared authority'),
        ):
            subprocess.run(
                ['git', *command], cwd=self.root,
                env=self.fixture._syncwheel_authorized_env(), check=True,
            )
        self.selected.unlink()
        module = guard_fixtures.syncwheel
        self.selected = module.personal_manifest_path(self.root, 'lane')
        self.fixture.write_selected_manifest(self.selected, module.personal_integration_branch('lane'))
        self.write_selected_authority({'mode': 'ai-managed', 'allow': ['runtime_change']})
        shared = self.shared.read_bytes()
        result = self.fixture.run_syncwheel(
            'repo', 'authority', 'set', 'human-gated', '--apply', '--personal', 'lane', '--json',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['authority']['mode'], 'human-gated')
        self.assertEqual(report['authority_provenance']['source'], 'selected')
        self.assertEqual(self.shared.read_bytes(), shared)

    def test_missing_canonical_authority_defaults_and_cannot_grant_locally(self):
        for missing_manifest in (False, True):
            with self.subTest(missing_manifest=missing_manifest):
                if missing_manifest:
                    self.shared.unlink()
                else:
                    shared = json.loads(self.shared.read_text())
                    shared.pop('authority')
                    self.shared.write_text(json.dumps(shared))
                result = self.fixture.run_syncwheel(
                    'repo', 'authority', 'status', '--manifest', str(self.selected), '--json',
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report['authority']['mode'], 'human-gated')
                self.assertEqual(report['authority_provenance']['source'], 'default')
                result = self.fixture.run_syncwheel(
                    'repo', 'authority', 'set', 'ai-managed', '--allow', 'source_change',
                    '--manifest', str(self.selected),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('selected authority exceeds canonical primary authority', result.stderr)

    def test_canonical_path_alias_reports_shared_source(self):
        alias = self.root / '.test-shared-alias.json'
        alias.symlink_to(self.shared)
        result = self.fixture.run_syncwheel(
            'repo', 'authority', 'status', '--manifest', str(alias), '--json',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['authority_provenance']['source'], 'shared')

    def test_malformed_or_redirected_canonical_policy_fails_closed(self):
        self.shared.write_text('{')
        self.assert_canonical_read_refused()
        self.shared.write_text(json.dumps({'version': 2, 'authority': {'mode': 'invalid'}}))
        self.assert_canonical_read_refused()
        self.shared.write_text(json.dumps({'version': 2, 'defaults': []}))
        self.assert_canonical_read_refused()
        self.shared.unlink()
        self.shared.symlink_to(self.selected)
        self.assert_canonical_read_refused()
        self.shared.unlink()
        self.shared.symlink_to(self.root / '.test-absent')
        self.assert_canonical_read_refused()

    def test_redirected_canonical_directory_fails_closed(self):
        external_selected = self.root / '.test-selected.json'
        self.selected.rename(external_selected)
        self.selected = external_selected
        actual = self.root / '.test-control'
        self.shared.parent.rename(actual)
        self.shared.parent.symlink_to(actual, target_is_directory=True)
        self.assert_canonical_read_refused()

    def test_default_shared_selection_refuses_fifo_or_symlink(self):
        for kind in ('fifo', 'symlink-to-fifo', 'dangling-symlink'):
            with self.subTest(kind=kind):
                self.shared.unlink()
                if kind == 'fifo':
                    os.mkfifo(self.shared)
                else:
                    target = self.root / '.test-manifest-fifo'
                    if kind == 'symlink-to-fifo':
                        os.mkfifo(target)
                    else:
                        target.unlink()
                    self.shared.symlink_to(target)
                before = self.shared.lstat()
                index = (self.root / '.git' / 'index').read_bytes()
                result = subprocess.run(
                    [sys.executable, str(guard_fixtures.MODULE_PATH),
                     'repo', 'authority', 'status', '--json'],
                    cwd=self.root,
                    env={**self.fixture.hook_env, 'SYNCWHEEL_UPDATE_MODE': 'off'},
                    capture_output=True, text=True, timeout=3,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('cannot read canonical primary authority', result.stderr)
                after = self.shared.lstat()
                self.assertEqual(stat.S_IFMT(after.st_mode), stat.S_IFMT(before.st_mode))
                self.assertEqual(after.st_ino, before.st_ino)
                self.assertEqual((self.root / '.git' / 'index').read_bytes(), index)

    def assert_canonical_read_refused(self):
        result = self.fixture.run_syncwheel(
            'repo', 'authority', 'status', '--manifest', str(self.selected), '--json',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cannot read canonical primary authority', result.stderr)

    def write_selected_authority(self, authority):
        manifest = json.loads(self.selected.read_text())
        manifest['authority'] = authority
        self.selected.write_text(json.dumps(manifest))
