"""Public CLI over real fixture Git and a controlled, read-only forge executable."""

import json
import os
from pathlib import Path
import unittest
from unittest import mock

import test_worktree_policy as fixtures
from test_scenario_acceptance import SourceFixture, fixture_receipt


class ScenarioAcceptanceCliTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WorktreePolicyTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        entry = os.environ.get('SYNCWHEEL_ACCEPTANCE_TEST_ENTRY')
        if entry:
            installed = mock.patch.object(fixtures.fixtures, 'CLI', Path(entry))
            installed.start()
            self.addCleanup(installed.stop)
        self.tmp = self.f.fixture.tmp
        self.cli = self.f.fixture.run_cli
        self.head = self.f.fixture.git('rev-parse', 'HEAD')
        manifest = self.f.fixture.read_manifest()
        canonical = manifest['defaults']['canonical_remote']
        configured = self.f.fixture.git('remote').splitlines()
        self.f.fixture.git('remote', 'set-url' if canonical in configured else 'add', canonical,
                           'https://github.com/example/project.git')
        self.receipt = fixture_receipt()
        self.receipt['binding'] = {'revisions': [self.head]}
        self.source = SourceFixture(self.receipt)
        self.source.review['commit_id'] = self.head
        self.remote = self.tmp / 'remote-source.json'
        self.input = self.tmp / 'receipt.json'
        self.locator = self.tmp / 'locator.json'
        self.input.write_text(json.dumps(self.receipt))
        self.locator.write_text(json.dumps(self.receipt['source']))
        self.f.settings.write_text(json.dumps({'acceptance_trust': {
            'github': {'example/project': [100]}}}))
        self.save_source()
        bin_dir = self.tmp / 'forge-bin'
        bin_dir.mkdir()
        gh = bin_dir / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json,os,sys
r=json.load(open(os.environ['FIXTURE_ACCEPTANCE_SOURCE']))
p=sys.argv[1:]
assert p[0]=='api',p
path=p[1]
if path=='user': value=r['principal']
elif path=='repos/example/project/pulls/7/reviews/17': value=r['review']
elif path=='repos/example/project/collaborators/approver/permission': value=r['permission']
else: raise AssertionError(path)
if os.environ.get('FIXTURE_CONCURRENT_PROFILE') and path.endswith('/permission'):
 from pathlib import Path
 p=Path(os.environ['FIXTURE_CONCURRENT_PROFILE'])
 current=json.loads(p.read_text())
 current['concurrent-writer']='preserved'
 p.write_text(json.dumps(current))
print(json.dumps(value))
''')
        gh.chmod(0o755)
        env = mock.patch.dict(os.environ, {
            'PATH': str(bin_dir) + os.pathsep + ((str(Path(entry).parent) + os.pathsep) if entry else '') + os.environ['PATH'],
            'FIXTURE_ACCEPTANCE_SOURCE': str(self.remote)})
        env.start()
        self.addCleanup(env.stop)

    def save_source(self):
        self.remote.write_text(json.dumps({'review': self.source.review,
                                          'principal': self.source.principal,
                                          'permission': self.source.permission}))

    def command(self, *args, **kwargs):
        return json.loads(self.cli('repo', 'acceptance', *args, '--json', **kwargs).stdout)

    def test_provenance_preview_and_apply_never_claim_human_verification(self):
        before = self.f.fixture.git('rev-parse', 'HEAD')
        preview = self.command('record', '--receipt', str(self.input))
        self.assertFalse(preview['applied'])
        self.assertFalse(self.f.profile.exists())
        record = self.command('record', '--receipt', str(self.input), '--apply')
        entry = next(iter(record['proposed']['acceptance_evidence'].values()))
        self.assertEqual(entry['recorded_by'], 'writer-a')
        self.assertNotIn('source_verification', entry)
        self.assertEqual(self.f.fixture.git('rev-parse', 'HEAD'), before)

    def test_positive_live_source_recovery_and_readback_in_second_clone(self):
        result = self.command('status', '--source', str(self.locator))
        self.assertEqual(result['status'], 'applicable')
        clone = self.tmp / 'second-clone'
        self.f.fixture.git('clone', '-q', str(self.f.fixture.repo), str(clone))
        self.assertFalse((clone / '.syncwheel/profile.local.json').exists())
        import subprocess
        subprocess.run(['git', '-C', str(clone), 'remote', 'set-url',
                        self.f.fixture.read_manifest()['defaults']['canonical_remote'],
                        'https://github.com/example/project.git'], check=True)
        result = self.command('status', '--source', str(self.locator), cwd=clone)
        self.assertEqual(result['status'], 'applicable')
        self.assertEqual(result['receipt'], self.receipt)
        self.assertFalse((clone / '.syncwheel/profile.local.json').exists())

    def test_assessment_is_candidate_bound_and_new_revision_requires_new_assessment(self):
        self.f.fixture.git('config', 'user.name', 'Fixture')
        self.f.fixture.git('config', 'user.email', 'fixture@example.invalid')
        path = self.f.fixture.repo / 'diagnostic.txt'
        path.write_text('first diagnostic\n')
        self.f.fixture.git('add', 'diagnostic.txt')
        self.f.fixture.git('commit', '-qm', 'Add diagnostic')
        candidate = self.f.fixture.git('rev-parse', 'HEAD')
        result = self.command('status', '--receipt', str(self.input), expected=1)
        self.assertEqual(result['status'], 'renewal_required')
        self.assertEqual(result['delta']['paths'], ['A\tdiagnostic.txt'])
        result = self.command('assess', '--receipt', str(self.input), '--impact', 'unchanged',
                              '--rationale', 'Only diagnostic text changed.', '--reference', 'fixture-review', '--apply')
        self.assertEqual(result['status'], 'applicable')
        self.assertEqual(result['candidate'], {'revisions': [candidate]})
        self.assertTrue(result['applied'])
        result = self.command('status', '--acceptance-id', result['acceptance_id'])
        self.assertEqual(result['status'], 'applicable')
        self.f.fixture.git('commit', '--allow-empty', '-qm', 'New candidate')
        result = self.command('status', '--receipt', str(self.input), expected=1)
        self.assertEqual(result['status'], 'renewal_required')
        changed = self.command('assess', '--receipt', str(self.input), '--impact', 'uncertain',
                               '--rationale', 'Impact needs another decision.', '--reference', 'fixture-review',
                               '--apply', expected=1)
        self.assertEqual(changed['status'], 'renewal_required')

    def test_candidate_profile_and_external_booleans_cannot_establish_trust(self):
        self.f.settings.write_text('{}')
        self.f.profile.write_text(json.dumps({'acceptance_trust': {'github': {'example/project': [100]}}}))
        result = self.command('status', '--receipt', str(self.input), expected=1)
        self.assertEqual(result['status'], 'source_unverified')
        forged = dict(self.receipt, human_verified=True)
        self.input.write_text(json.dumps(forged))
        self.cli('repo', 'acceptance', 'record', '--receipt', str(self.input), '--apply', expected=2)
        self.assertNotIn('acceptance_evidence', json.loads(self.f.profile.read_text()))

    def test_live_dismissal_and_same_operator_invalidate_a_previously_valid_source(self):
        self.assertEqual(self.command('status', '--source', str(self.locator))['status'], 'applicable')
        self.source.review['state'] = 'DISMISSED'
        self.save_source()
        self.assertEqual(self.command('status', '--source', str(self.locator), expected=1)['status'], 'source_unverified')
        self.source.review['state'] = 'APPROVED'
        self.source.principal = {'id': 100, 'login': 'approver'}
        self.save_source()
        result = self.command('status', '--source', str(self.locator), expected=1)
        self.assertIn('source_author_indistinguishable_from_operator', result['source_reasons'])

    def test_unrelated_repository_scenario_and_old_candidate_cannot_reuse_signoff(self):
        canonical = self.f.fixture.read_manifest()['defaults']['canonical_remote']
        self.f.fixture.git('remote', 'set-url', canonical, 'https://github.com/unrelated/consumer.git')
        mismatch = self.command('status', '--receipt', str(self.input), expected=1)
        self.assertIn('repository_identity_mismatch', mismatch['source_reasons'])
        self.cli('repo', 'acceptance', 'status', '--source', str(self.locator), expected=2)
        self.f.fixture.git('remote', 'set-url', canonical, 'https://github.com/example/project.git')
        mismatch = self.command('status', '--receipt', str(self.input), '--scenario', 'different-scenario', expected=1)
        self.assertIn('scenario_identity_mismatch', mismatch['source_reasons'])
        self.f.fixture.git('commit', '--allow-empty', '-qm', 'New live candidate')
        refused = self.cli('repo', 'acceptance', 'status', '--receipt', str(self.input),
                           '--head', self.head, expected=2)
        self.assertIn('does not match the live', refused.stderr)

    def test_concurrent_profile_writer_survives_assessment_apply(self):
        self.f.profile.write_text('{}')
        with mock.patch.dict(os.environ, {'FIXTURE_CONCURRENT_PROFILE': str(self.f.profile)}):
            result = self.cli('repo', 'acceptance', 'assess', '--receipt', str(self.input),
                              '--impact', 'unchanged', '--rationale', 'Exact same commit.',
                              '--reference', 'fixture-reference', '--apply', expected=2)
        self.assertIn('changed concurrently', result.stderr)
        self.assertEqual(json.loads(self.f.profile.read_text()), {'concurrent-writer': 'preserved'})

    def test_ordered_composition_and_foreign_assessment_fail_through_public_entry(self):
        accepted = {'composition': {'base_sha': self.head, 'head_sha': self.head,
                                  'stacks': [{'id': 'producer', 'head_sha': self.head},
                                             {'id': 'consumer', 'head_sha': self.head}]}}
        self.receipt['binding'] = accepted
        self.source = SourceFixture(self.receipt)
        self.source.review['commit_id'] = self.head
        self.save_source()
        self.input.write_text(json.dumps(self.receipt))
        candidate = json.loads(json.dumps(accepted))
        candidate['composition']['stacks'].reverse()
        path = self.tmp / 'candidate.json'
        path.write_text(json.dumps(candidate))
        result = self.command('status', '--receipt', str(self.input), '--candidate', str(path), expected=1)
        self.assertFalse(result['exact_binding'])
        self.assertEqual(result['status'], 'renewal_required')
        forged = self.tmp / 'forged-assessment.json'
        forged.write_text(json.dumps({'schema_version': 1, 'kind': 'scenario_assessment',
                                     'acceptance_id': '0' * 64, 'candidate': candidate,
                                     'impact': 'unchanged', 'rationale': 'Forged candidate.',
                                     'assessed_at': '2026-01-16T10:00:00Z', 'reference': 'fixture'}))
        refused = self.cli('repo', 'acceptance', 'status', '--receipt', str(self.input),
                           '--candidate', str(path), '--assessment', str(forged), expected=2)
        self.assertIn('different acceptance or candidate', refused.stderr)
        payload = json.loads(forged.read_text())
        payload['acceptance_id'] = result['acceptance_id']
        payload['human_verified'] = True
        forged.write_text(json.dumps(payload))
        self.cli('repo', 'acceptance', 'status', '--receipt', str(self.input), '--candidate', str(path),
                 '--assessment', str(forged), expected=2)


if __name__ == '__main__':
    unittest.main()
