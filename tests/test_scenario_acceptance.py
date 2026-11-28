"""Live source-owner fixtures, not self-attested human approval or real accounts."""

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import syncwheel_acceptance as acceptance
from syncwheel_errors import SyncwheelError


def fixture_receipt():
    return {'schema_version': 1, 'kind': 'scenario_acceptance',
            'scenario_id': 'checkout-confirmation', 'scenario': 'Confirm a pending checkout once.',
            'person': 'approver', 'accepted_on': '2026-01-15',
            'binding': {'revisions': ['a' * 40]},
            'source': {'kind': 'github_review', 'repository': 'example/project',
                       'pull_request': 7, 'review_id': 17}}


class SourceFixture:
    def __init__(self, receipt=None):
        self.receipt = copy.deepcopy(receipt or fixture_receipt())
        payload = {k: v for k, v in self.receipt.items() if k != 'source'}
        self.review = {'id': 17, 'user': {'id': 100, 'login': 'approver', 'type': 'User'},
                       'state': 'APPROVED', 'commit_id': 'a' * 40,
                       'submitted_at': '2026-01-15T10:00:00Z',
                       'pull_request_url': 'https://api.github.com/repos/example/project/pulls/7',
                       'body': json.dumps(payload)}
        self.principal = {'id': 200, 'login': 'automation'}
        self.permission = {'permission': 'write', 'user': {'id': 100}}
        self.calls = []

    def read_json(self, path):
        self.calls.append(path)
        if path == 'repos/example/project/pulls/7/reviews/17':
            return copy.deepcopy(self.review)
        if path == 'user':
            return copy.deepcopy(self.principal)
        if path == 'repos/example/project/collaborators/approver/permission':
            return copy.deepcopy(self.permission)
        raise AssertionError(path)

    def observe(self, source, trusted=(100,)):
        return acceptance.observe_github_signoff(source, trusted_human_ids=trusted,
                                                  read_json=self.read_json)


def assessment(receipt, candidate, impact='unchanged'):
    return {'schema_version': 1, 'kind': 'scenario_assessment',
            'acceptance_id': acceptance.receipt_id(receipt), 'candidate': candidate,
            'impact': impact, 'rationale': 'Only the diagnostic text changed.',
            'assessed_at': '2026-01-16T10:00:00Z', 'reference': 'fixture-delta-review'}


class ScenarioAcceptanceTest(unittest.TestCase):
    def test_live_fixture_identity_permission_and_exact_scenario_positive(self):
        f = SourceFixture()
        result = acceptance.applicability(f.receipt, f.receipt['binding'], actor='agent',
                                           observe_source=f.observe)
        self.assertEqual(result['status'], 'applicable')
        self.assertFalse(result['renewal_required'])
        self.assertEqual(f.calls, ['repos/example/project/pulls/7/reviews/17', 'user',
                                   'repos/example/project/collaborators/approver/permission'])

    def test_agent_delta_reuses_signoff_only_for_exact_assessed_candidate(self):
        f = SourceFixture()
        candidate = {'revisions': ['b' * 40]}
        raw = assessment(f.receipt, candidate)
        result = acceptance.applicability(f.receipt, candidate, actor='agent',
                                           assessment=raw, observe_source=f.observe)
        self.assertEqual(result['status'], 'applicable')
        self.assertEqual(result['assessment']['assessed_by'], 'agent')
        self.assertNotIn('assessed_by', raw)
        for impact in ['changed', 'uncertain']:
            with self.subTest(impact=impact):
                raw['impact'] = impact
                result = acceptance.applicability(f.receipt, candidate, actor='agent',
                                                   assessment=raw, observe_source=f.observe)
                self.assertEqual(result['status'], 'renewal_required')
        with self.assertRaisesRegex(SyncwheelError, 'different acceptance or candidate'):
            acceptance.applicability(f.receipt, {'revisions': ['c' * 40]}, actor='agent',
                                     assessment=raw, observe_source=f.observe)
        missing = acceptance.applicability(f.receipt, candidate, actor='agent', observe_source=f.observe)
        self.assertTrue(missing['renewal_required'])

    def test_no_self_attestation_or_trust_in_account_type_alone(self):
        f = SourceFixture()
        self.assertEqual(f.observe(f.receipt['source'], trusted=())['source_verification'], 'unverified')
        f.principal = {'id': 100, 'login': 'approver'}
        observed = f.observe(f.receipt['source'])
        self.assertIn('source_author_indistinguishable_from_operator', observed['reasons'])
        result = acceptance.applicability(f.receipt, f.receipt['binding'], actor='agent', observe_source=f.observe)
        self.assertEqual(result['status'], 'source_unverified')
        forged = dict(f.receipt, human_verified=True)
        with self.assertRaises(SyncwheelError):
            acceptance.normalize_receipt(forged)
        with self.assertRaises(SyncwheelError):
            acceptance.observe_github_signoff(f.receipt['source'], trusted_human_ids=[True], read_json=f.read_json)

    def test_source_is_reobserved_after_handoff_and_revocation(self):
        f = SourceFixture()
        cached = acceptance.record_receipts({'other': 1}, [f.receipt], actor='agent-one')
        key = acceptance.receipt_id(f.receipt)
        serialized = json.loads(json.dumps(cached))
        incoming = serialized[acceptance.EVIDENCE_KEY][key]['receipt']
        result = acceptance.applicability(incoming, incoming['binding'], actor='agent-two', observe_source=f.observe)
        self.assertEqual(result['status'], 'applicable')
        f.review['state'] = 'DISMISSED'
        result = acceptance.applicability(incoming, incoming['binding'], actor='agent-two', observe_source=f.observe)
        self.assertEqual(result['status'], 'source_unverified')
        f.review['state'] = 'APPROVED'
        payload = json.loads(f.review['body'])
        payload['scenario'] = 'A changed scenario.'
        f.review['body'] = json.dumps(payload)
        with self.assertRaisesRegex(SyncwheelError, 'source changed'):
            acceptance.applicability(incoming, incoming['binding'], actor='agent-two', observe_source=f.observe)
        # A new clone can recover the receipt from the shared source locator alone.
        recovered = f.observe(incoming['source'])
        self.assertEqual(recovered['receipt']['scenario'], 'A changed scenario.')
        self.assertNotEqual(recovered['receipt_id'], key)

    def test_permission_bot_principal_and_missing_permission_fail_closed(self):
        for name, mutate, reason in [
                ('permission', lambda f: f.permission.update(permission='read'), 'source_author_not_authorized'),
                ('identity', lambda f: f.permission.update(user={'id': 900}), 'source_author_permission_identity_mismatch'),
                ('bot', lambda f: f.review['user'].update(type='Bot'), 'source_author_not_trusted_human'),
                ('principal', lambda f: f.principal.clear(), 'operator_identity_unverified')]:
            with self.subTest(case=name):
                f = SourceFixture()
                mutate(f)
                self.assertIn(reason, f.observe(f.receipt['source'])['reasons'])

    def test_source_identity_content_revision_and_date_cannot_be_forged(self):
        for name, mutate in [
                ('id', lambda f: f.review.update(id=18)),
                ('repo', lambda f: f.review.update(pull_request_url='https://api.github.com/repos/other/project/pulls/7')),
                ('revision', lambda f: f.review.update(commit_id='b' * 40)),
                ('date', lambda f: f.review.update(submitted_at='2026-01-16T10:00:00Z')),
                ('body', lambda f: f.review.update(body='Approved')),
                ('person', lambda f: f.review['user'].update(login='other'))]:
            with self.subTest(case=name):
                f = SourceFixture()
                mutate(f)
                with self.assertRaises(SyncwheelError):
                    f.observe(f.receipt['source'])

    def test_composition_order_and_base_are_part_of_acceptance_and_assessment(self):
        receipt = fixture_receipt()
        receipt['binding'] = {'composition': {'base_sha': 'b' * 40, 'head_sha': 'a' * 40,
                                              'stacks': [{'id': 'producer', 'head_sha': 'c' * 40},
                                                         {'id': 'consumer', 'head_sha': 'd' * 40}]}}
        f = SourceFixture(receipt)
        original = copy.deepcopy(receipt['binding'])
        reordered = copy.deepcopy(original)
        reordered['composition']['stacks'].reverse()
        self.assertNotEqual(acceptance.normalize_binding(original), acceptance.normalize_binding(reordered))
        self.assertEqual(acceptance.applicability(receipt, reordered, actor='agent', observe_source=f.observe)['status'], 'renewal_required')
        changed_base = copy.deepcopy(original)
        changed_base['composition']['base_sha'] = 'e' * 40
        raw = assessment(receipt, reordered)
        with self.assertRaisesRegex(SyncwheelError, 'different acceptance or candidate'):
            acceptance.applicability(receipt, changed_base, actor='agent', assessment=raw, observe_source=f.observe)
        malformed = copy.deepcopy(original)
        malformed['composition']['stacks'].append(malformed['composition']['stacks'][0])
        with self.assertRaisesRegex(SyncwheelError, 'duplicate stack'):
            acceptance.normalize_binding(malformed)

    def test_receipts_remain_provenance_and_preserve_original_actor_and_profile(self):
        raw = fixture_receipt()
        profile = {'execution_evidence': {'unrelated': {}}, 'hooks': {'enabled': True}}
        result = acceptance.record_receipts(profile, [raw], actor='agent-one')
        repeated = acceptance.record_receipts(result, [raw], actor='agent-two')
        self.assertEqual(result, repeated)
        record = next(iter(result[acceptance.EVIDENCE_KEY].values()))
        self.assertEqual(record, {'receipt': raw, 'recorded_by': 'agent-one'})
        self.assertNotIn(acceptance.EVIDENCE_KEY, profile)
        self.assertEqual(result['execution_evidence'], profile['execution_evidence'])
        for field, invalid in [('accepted_on', 'yesterday'), ('scenario', ''), ('schema_version', True)]:
            with self.subTest(field=field), self.assertRaises(SyncwheelError):
                acceptance.normalize_receipt(dict(raw, **{field: invalid}))
        with self.assertRaises(SyncwheelError):
            acceptance.normalize_binding({'revisions': ['main']})

    def test_non_forge_reference_records_provenance_without_claiming_verification(self):
        raw = fixture_receipt()
        raw['source'] = {'kind': 'provenance', 'reference': 'human-decision-record-17'}
        stored = acceptance.record_receipts({}, [raw], actor='agent')
        self.assertIn(acceptance.receipt_id(raw), stored[acceptance.EVIDENCE_KEY])
        result = acceptance.applicability(
            raw, raw['binding'], actor='agent',
            observe_source=lambda source: acceptance.provenance_observation(raw))
        self.assertEqual(result['status'], 'source_unverified')
        with self.assertRaisesRegex(SyncwheelError, 'requires a GitHub review source'):
            SourceFixture().observe(raw['source'])

    def test_decoded_json_is_not_a_live_source_observation(self):
        f = SourceFixture()
        forged = json.loads(json.dumps(f.observe(f.receipt['source'])))
        with self.assertRaisesRegex(SyncwheelError, 'live acceptance source changed'):
            acceptance.applicability(f.receipt, f.receipt['binding'], actor='agent',
                                     observe_source=lambda source: forged)

    def test_operator_assessment_cannot_supply_its_own_identity_or_approval(self):
        raw = fixture_receipt()
        value = assessment(raw, raw['binding'])
        for field, forged in [('assessed_by', 'human'), ('human_verified', True)]:
            with self.subTest(field=field), self.assertRaises(SyncwheelError):
                acceptance.normalize_assessment(dict(value, **{field: forged}), actor='agent')
        self.assertEqual(acceptance.normalize_assessment(value, actor='agent')['assessed_by'], 'agent')


if __name__ == '__main__':
    unittest.main()



def forge_authority_responses(git_dir, repository='example/project', branch='main', repository_id=1000):
    """Controlled forge API objects from an independent bare owner repository."""
    import base64
    import subprocess
    def git(*args):
        return subprocess.check_output(['git', '--git-dir=' + str(git_dir), *args])
    base = git('rev-parse', 'refs/heads/' + branch).decode().strip()
    root = git('rev-parse', base + '^{tree}').decode().strip()
    prefix = 'repos/' + repository
    result = {prefix: {'id': repository_id, 'full_name': repository},
              prefix + '/git/ref/heads/' + branch: {'ref': 'refs/heads/' + branch,
                                                     'object': {'type': 'commit', 'sha': base}},
              prefix + '/git/commits/' + base: {'sha': base, 'tree': {'sha': root}}}
    def tree(sha):
        entries = []
        for line in git('ls-tree', '-z', sha).split(b'\0'):
            if not line:
                continue
            header, path = line.decode().split('\t', 1)
            mode, kind, oid = header.split()
            entries.append({'mode': mode, 'type': kind, 'sha': oid, 'path': path})
        result[prefix + '/git/trees/' + sha] = {'sha': sha, 'truncated': False, 'tree': entries}
        return entries
    for directory in tree(root):
        if directory['path'] == '.syncwheel' and directory['type'] == 'tree':
            for policy in tree(directory['sha']):
                if policy['path'] == 'acceptance-policy.json' and policy['type'] == 'blob':
                    payload = git('cat-file', 'blob', policy['sha'])
                    result[prefix + '/git/blobs/' + policy['sha']] = {
                        'sha': policy['sha'], 'encoding': 'base64', 'size': len(payload),
                        'content': base64.b64encode(payload).decode()}
    return result
