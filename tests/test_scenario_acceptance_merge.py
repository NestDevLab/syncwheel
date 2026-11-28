"""Canonical opt-in through the existing merge planner/apply with real Git refs."""

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import test_github_pr_merge as merge_fixture
from test_scenario_acceptance import SourceFixture, fixture_receipt, assessment

S = merge_fixture.SYNCWHEEL
A = S._acceptance


class AcceptanceMergeTest(unittest.TestCase):
    def setUp(self):
        self.f = merge_fixture.GithubPrMergeTest()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.receipt = fixture_receipt()
        self.receipt['binding'] = {'revisions': [self.f.head]}
        self.source = SourceFixture(self.receipt)
        self.source.review['commit_id'] = self.f.head
        self.f.git('remote', 'set-url', 'origin', 'https://github.com/example/project.git')
        self.f.git('config', 'url.' + str(self.f.remote) + '.insteadOf', 'https://github.com/example/project.git')
        self.f.manifest['authority'] = {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': []}
        self.f.manifest['scenario_acceptance'] = {
            'repository': 'example/project',
            'scenarios': [{'id': self.receipt['scenario_id'], 'source': self.receipt['source']}]}
        self.write_manifest()
        self.profile_path = self.f.repo / '.syncwheel/profile.local.json'
        policy = self.f.policy()
        old = policy['repository']
        policy['repository'] = 'example/project'
        policy['head_repositories'] = ['example/project']
        self.profile_path.write_text(json.dumps({'github_pr_merge': policy}))
        self.observed = json.loads(json.dumps(self.f.observation()).replace(old, 'example/project'))
        self.settings = self.f.tmp / 'settings.json'
        self.settings.write_text(json.dumps({'acceptance_trust': {'github': {'example/project': [100]}}}))
        env = mock.patch.dict(os.environ, {'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.settings),
                                         'SYNCWHEEL_UPDATE_MODE': 'off', 'SYNCWHEEL_LANE_OWNER': 'agent'})
        env.start()
        self.addCleanup(env.stop)
        valid = mock.patch.object(S, 'validate_manifest', return_value={'errors': []})
        valid.start()
        self.addCleanup(valid.stop)
        adapter = mock.patch.object(S, 'github_adapter_request', side_effect=self.adapter)
        adapter.start()
        self.addCleanup(adapter.stop)
        api = mock.patch.object(S._acceptance_github, 'run_json', side_effect=self.api)
        api.start()
        self.addCleanup(api.stop)
        self.requests = []
        self.source_reads = 0
        self.revoke_at = None

    def write_manifest(self):
        self.f.manifest_path.write_text(json.dumps(self.f.manifest))

    def adapter(self, repo, request):
        self.requests.append(request)
        if request['operation'] == 'merge':
            self.observed['pr']['state'] = 'MERGED'
            self.observed['pr']['mergeCommit'] = 'f' * 40
            return {'ok': True}
        return copy.deepcopy(self.observed)

    def api(self, argv, cwd):
        if '/reviews/' in argv[1]:
            self.source_reads += 1
            if self.revoke_at == self.source_reads:
                self.source.review['state'] = 'DISMISSED'
        return {'value': self.source.read_json(argv[1])}

    def plan(self, manifest=None):
        return S.build_github_pr_merge_plan(self.f.repo, manifest or self.f.manifest,
                                           self.f.manifest_path, 'feature', self.f.args())

    def test_exact_canonical_scenario_is_consumed_by_real_merge_plan(self):
        result = self.plan()
        self.assertEqual(result['status'], 'ready', result['blockers'])
        self.assertEqual(result['acceptanceEvidence']['candidate'], {'revisions': [self.f.head]})
        self.assertEqual(result['acceptanceEvidence']['scenarios'][0]['status'], 'applicable')
        selected = copy.deepcopy(self.f.manifest)
        selected.pop('scenario_acceptance')
        self.assertIsNotNone(self.plan(selected)['acceptanceEvidence'])

    def test_no_opt_in_leaves_legacy_delivery_without_source_queries(self):
        self.f.manifest.pop('scenario_acceptance')
        self.write_manifest()
        result = self.plan()
        self.assertIsNone(result['acceptanceEvidence'])
        self.assertEqual(self.source_reads, 0)
        self.assertEqual(result['status'], 'ready')

    def test_wrong_repository_or_required_scenario_blocks_planner(self):
        self.f.git('remote', 'set-url', 'origin', 'https://github.com/unrelated/consumer.git')
        result = self.plan()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['acceptanceEvidence']['blockers'][0]['code'], 'acceptance_repository_mismatch')
        self.assertEqual(self.source_reads, 0)
        self.f.git('remote', 'set-url', 'origin', 'https://github.com/example/project.git')
        self.f.manifest['scenario_acceptance']['scenarios'][0]['id'] = 'another-required-scenario'
        self.write_manifest()
        result = self.plan()
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('scenario_identity_mismatch', result['acceptanceEvidence']['scenarios'][0]['source_reasons'])

    def test_candidate_delta_requires_exact_assessment_and_changed_delta_reblocks(self):
        self.receipt['binding'] = {'revisions': [self.f.base]}
        self.source = SourceFixture(self.receipt)
        self.source.review['commit_id'] = self.f.base
        result = self.plan()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['acceptanceEvidence']['scenarios'][0]['status'], 'renewal_required')
        profile = json.loads(self.profile_path.read_text())
        candidate = {'revisions': [self.f.head]}
        delta = A.candidate_delta(self.receipt, candidate,
                                  lambda before, after: self.f.git('diff', '--name-status', before, after).splitlines())
        profile = A.record_receipts(profile, [self.receipt], actor='agent')
        profile['acceptance_assessments'] = {A.assessment_key(self.receipt, candidate): [
            {'assessment': assessment(self.receipt, candidate), 'actor': 'agent', 'delta': delta}]}
        self.profile_path.write_text(json.dumps(profile))
        self.assertEqual(self.plan()['status'], 'ready')
        profile['acceptance_assessments'][A.assessment_key(self.receipt, candidate)][0]['delta']['paths'] = []
        self.profile_path.write_text(json.dumps(profile))
        self.assertEqual(self.plan()['status'], 'blocked')

    def test_apply_rechecks_source_after_preview_and_under_lock(self):
        preview = self.plan()
        self.assertEqual(preview['status'], 'ready')
        self.revoke_at = self.source_reads + 2  # Apply build, then protected revalidation.
        before = self.f.git('rev-parse', 'pr/feature')
        with self.assertRaisesRegex(S.SyncwheelError, 'scenario acceptance changed before merge'):
            S.command_stack_merge_pr(self.f.args(apply=True, plan_digest=preview['planDigest']))
        self.assertFalse(any(r['operation'] == 'merge' for r in self.requests))
        self.assertEqual(self.f.git('rev-parse', 'pr/feature'), before)
        self.assertFalse(any(e['type'] == 'github_pr_merge_started'
                             for e in S.load_ledger_events(self.f.repo, self.f.manifest_path)))

    def test_public_cli_preview_apply_and_revocation_use_real_entrypoints(self):
        bin_dir = self.f.tmp / 'public-bin'
        bin_dir.mkdir()
        state_path = self.f.tmp / 'public-state.json'
        state_path.write_text(json.dumps({'observation': self.observed, 'source': {
            'review': self.source.review, 'principal': self.source.principal,
            'permission': self.source.permission}, 'merges': 0}))
        adapter = bin_dir / 'syncwheel-github'
        adapter.write_text("""#!/usr/bin/env python3
import json,os,sys,subprocess
p=os.environ['ACCEPTANCE_FIXTURE_STATE']; state=json.load(open(p)); req=json.load(sys.stdin)
if req['operation']=='merge':
 state['merges']+=1
 state['observation']['pr']['state']='MERGED'
 state['observation']['pr']['mergeCommit']=req['headSha']
 subprocess.run(['git','--git-dir='+os.environ['ACCEPTANCE_FIXTURE_REMOTE'],'update-ref','refs/heads/main',req['headSha']],check=True)
 json.dump(state,open(p,'w'))
 print(json.dumps({'ok':True}));sys.exit(0)
print(json.dumps({'ok':True,'observation':state['observation']}))
""")
        gh = bin_dir / 'gh'
        gh.write_text("""#!/usr/bin/env python3
import json,os,sys
state=json.load(open(os.environ['ACCEPTANCE_FIXTURE_STATE']))['source']; path=sys.argv[2]
if '/reviews/' in path: value=state['review']
elif path=='user': value=state['principal']
elif path.endswith('/permission'): value=state['permission']
else: raise AssertionError(path)
print(json.dumps(value))
""")
        adapter.chmod(0o755); gh.chmod(0o755)
        env=dict(os.environ, PATH=str(bin_dir)+os.pathsep+os.environ['PATH'],
                 ACCEPTANCE_FIXTURE_STATE=str(state_path), ACCEPTANCE_FIXTURE_REMOTE=str(self.f.remote))
        command=[sys.executable, str(merge_fixture.SCRIPTS/'syncwheel.py'), 'stack', 'merge-pr', 'feature', '--json']
        preview=subprocess.run(command,cwd=self.f.repo,env=env,text=True,capture_output=True)
        self.assertEqual(preview.returncode,0,preview.stderr)
        plan=json.loads(preview.stdout)
        self.assertEqual(plan['status'],'ready',plan['blockers'])
        state=json.loads(state_path.read_text());state['source']['review']['state']='DISMISSED'
        state_path.write_text(json.dumps(state))
        apply=command+['--apply','--operation-id',plan['operationId'],'--plan-digest',plan['planDigest']]
        refused=subprocess.run(apply,cwd=self.f.repo,env=env,text=True,capture_output=True)
        self.assertEqual(refused.returncode,2,refused.stderr)
        self.assertEqual(json.loads(state_path.read_text())['merges'],0)
        state['source']['review']['state']='APPROVED';state_path.write_text(json.dumps(state))
        result=subprocess.run(apply,cwd=self.f.repo,env=env,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'succeeded')
        self.assertEqual(json.loads(state_path.read_text())['merges'],1)

    def test_positive_apply_consumes_verified_signoff_and_pinned_candidate(self):
        preview = self.plan()
        out = io.StringIO()
        with mock.patch.object(S, 'github_pr_merge_postverify', return_value={'status': 'verified'}), contextlib.redirect_stdout(out):
            code = S.command_stack_merge_pr(self.f.args(apply=True, plan_digest=preview['planDigest']))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())['status'], 'succeeded')
        merges = [r for r in self.requests if r['operation'] == 'merge']
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]['headSha'], self.f.head)
        self.assertGreaterEqual(self.source_reads, 3)


if __name__ == '__main__':
    unittest.main()
