"""Cross-policy publication proof using controlled local Git fixtures."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

import test_scenario_acceptance_merge as signoff
from syncwheel_actor_recovery import GitRecoveryStore, STATE_REF, PATH, empty_state

S = signoff.S
REAL_VALIDATE = S.validate_manifest


class CombinedPolicySmoke(unittest.TestCase):
    def setUp(self):
        self.case = signoff.AcceptanceMergeTest('test_exact_canonical_scenario_is_consumed_by_real_merge_plan')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.f = self.case.f
        profile = json.loads(self.case.profile_path.read_text())
        profile['github_pr_merge']['allowed_bypasses'] = []
        self.case.profile_path.write_text(json.dumps(profile))
        self.case.observed['pr']['review']['decision'] = 'APPROVED'
        self.case.observed['pr']['mergeStateStatus'] = 'CLEAN'
        self.case.observed['pr']['checks'][0]['headSha'] = self.f.head
        self.actor = S._actor_recovery
        self.authority_ref = 'refs/heads/independent-actor-authority'
        self.store = GitRecoveryStore(self.f.repo, str(self.f.remote), STATE_REF,
                                      self.authority_ref, 'example/project')
        policy = {'enabled': True, 'repository': 'example/project', 'remote': str(self.f.remote),
                  'state_ref': STATE_REF, 'authority_ref': self.authority_ref,
                  'product_refs': ['refs/heads/main']}
        initial = empty_state('example/project')
        initial['policy'] = policy
        self.publish_document(STATE_REF, initial)
        self.records = {}
        for incident in ('one', 'two'):
            self.records[incident] = dict(kind='incident', repository='example/project',
                incident=incident, actor='responsible-agent', delivery='same-publication',
                proven=True, attributed=True, evidence='independent-source-' + incident)
            self.records['recover-' + incident] = dict(kind='recovery', repository='example/project',
                incident=incident, recovered=True, attribution_verified=True,
                evidence='verified-recovery-' + incident)
        self.records['publication'] = dict(kind='publication', repository='example/project',
            operation='merge-1', actor='responsible-agent', delivery='same-publication',
            head=self.f.head, base_sha=self.f.base, base_branch='main',
            target_ref='refs/heads/main', pull_request=25)
        self.records['review'] = dict(kind='review', repository='example/project',
            operation='merge-1', actor='responsible-agent', reviewer='independent-agent',
            head=self.f.head, outcome='approved', evidence='exact-reviewed-source')
        self.publish_document(self.authority_ref,
            {'schema_version': 1, 'repository': 'example/project', 'records': self.records})
        for incident in ('one', 'two'):
            self.store.apply(self.store.snapshot(), 'confirm', incident)
            self.store.apply(self.store.snapshot(), 'recover', 'recover-' + incident)
        self.effects = 0
        original_adapter = self.case.adapter

        def adapter(root, request):
            if request['operation'] == 'merge':
                self.assertFalse(request['admin'])
                self.assertEqual(request['headSha'], self.f.head)
                # A controlled local bare is the fake forge's server. The two
                # source verifiers and the real CLI must both pass before here.
                self.store.git('--git-dir=' + str(self.f.remote), 'update-ref',
                               'refs/heads/main', self.f.head, self.f.base)
                self.effects += 1
                self.case.observed['pr']['mergedAt'] = '2026-01-16T10:00:00Z'
            return original_adapter(root, request)
        self.adapter_patch = mock.patch.object(S, 'github_adapter_request', side_effect=adapter)
        self.adapter_patch.start()
        self.addCleanup(self.adapter_patch.stop)

    def publish_document(self, ref, document):
        blob = self.store.git('hash-object', '-w', '--stdin', input=json.dumps(document))
        sub = self.store.git('mktree', input='100644 blob ' + blob + '\tactor-recovery.json\n')
        tree = self.store.git('mktree', input='040000 tree ' + sub + '\t.syncwheel\n')
        old = self.store.git('ls-remote', '--refs', str(self.f.remote), ref)
        parents = ['-p', old.split()[0]] if old else []
        commit = self.store.git('commit-tree', tree, *parents, input='Independent fixture authority\n')
        self.store.git('push', str(self.f.remote), commit + ':' + ref)

    def cli(self, *, review=False, apply=None):
        argv = [str(S.__file__), 'stack', 'merge-pr', 'feature', '--repo', str(self.f.repo),
                '--operation-id', 'merge-1', '--actor-publication', 'publication', '--json']
        if review:
            argv += ['--actor-review', 'review']
        if apply is not None:
            argv += ['--apply', '--plan-digest', apply]
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', argv), \
             mock.patch.object(S, 'validate_manifest', side_effect=REAL_VALIDATE), \
             mock.patch.object(S, 'managed_hook_syncwheel_command',
                               return_value=[sys.executable, str(S.__file__)]), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = S.main()
        return code, json.loads(output.getvalue()) if output.getvalue().strip() else None, errors.getvalue()

    def test_same_publication_each_gate_blocks_independently_and_both_allow_one_effect(self):
        initial_tip = self.store.git('--git-dir=' + str(self.f.remote), 'rev-parse', 'refs/heads/main')
        code, actor_blocked, errors = self.cli()
        self.assertEqual(code, 0, errors)
        self.assertEqual(actor_blocked['acceptanceEvidence']['scenarios'][0]['status'], 'applicable')
        self.assertEqual(actor_blocked['actorRecovery']['status'], 'blocked')
        self.assertEqual(self.cli(apply=actor_blocked['planDigest'])[0], 2)
        self.assertEqual(self.effects, 0)
        self.assertEqual(self.store.snapshot()['state']['operations'], {})

        self.case.source.review['state'] = 'DISMISSED'
        code, signoff_blocked, errors = self.cli(review=True)
        self.assertEqual(code, 0, errors)
        self.assertEqual(signoff_blocked['actorRecovery']['status'], 'ready')
        self.assertEqual(signoff_blocked['acceptanceEvidence']['scenarios'][0]['status'], 'source_unverified')
        self.assertEqual(self.cli(review=True, apply=signoff_blocked['planDigest'])[0], 2)
        self.assertEqual(self.effects, 0)
        self.assertEqual(self.store.snapshot()['state']['operations'], {})

        self.case.source.review['state'] = 'APPROVED'
        code, ready, errors = self.cli(review=True)
        self.assertEqual(code, 0, errors)
        self.assertEqual(ready['status'], 'ready', ready['blockers'])
        self.assertEqual(self.store.git('--git-dir=' + str(self.f.remote), 'rev-parse', 'refs/heads/main'), initial_tip)
        code, receipt, errors = self.cli(review=True, apply=ready['planDigest'])
        self.assertEqual(code, 0, errors)
        self.assertEqual(receipt['status'], 'succeeded')
        self.assertEqual(self.effects, 1)
        state = self.store.snapshot()['state']
        self.assertEqual(state['reviewed_through']['responsible-agent'], ['one', 'two'])
        self.assertEqual(state['operations']['merge-1']['outcome']['outcome'], 'published')
        self.assertEqual(self.store.git('--git-dir=' + str(self.f.remote), 'rev-parse', 'refs/heads/main'), self.f.head)
        # Re-observe terminal state; no second forge mutation or debt consumption.
        self.assertEqual(self.cli(review=True, apply=ready['planDigest'])[0], 0)
        self.assertEqual(self.effects, 1)
        report = {
            'fixture_only': True,
            'entrypoint': 'real CLI main and merge-pr handler',
            'publication': {'repository': 'example/project', 'pull_request': 25,
                            'head': self.f.head, 'operation': 'merge-1', 'delivery': 'same-publication'},
            'actor_only_blocks': {'scenario': actor_blocked['acceptanceEvidence']['scenarios'][0]['status'],
                                  'actor': actor_blocked['actorRecovery']['status'], 'apply_exit': 2, 'effects': 0},
            'scenario_only_blocks': {'scenario': signoff_blocked['acceptanceEvidence']['scenarios'][0]['status'],
                                     'actor': signoff_blocked['actorRecovery']['status'], 'apply_exit': 2, 'effects': 0},
            'both_pass': {'status': ready['status'], 'apply_exit': code, 'receipt': receipt['status'],
                          'effects_after_reconciliation': self.effects,
                          'before': initial_tip, 'after': self.f.head,
                          'actor_review_consumed': state['reviewed_through']['responsible-agent']},
            'external_forge_mutations': 0,
        }
        if os.environ.get('SYNCWHEEL_COMBINED_SMOKE_RECEIPT'):
            Path(os.environ['SYNCWHEEL_COMBINED_SMOKE_RECEIPT']).write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
