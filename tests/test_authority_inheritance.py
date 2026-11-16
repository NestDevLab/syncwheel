import contextlib
import copy
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

import test_github_pr_merge as fixtures


class AuthorityInheritanceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.GithubPrMergeTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.module = fixtures.SYNCWHEEL
        self.primary = self.fixture.repo
        self.shared_path = self.fixture.manifest_path
        self.lane = self.fixture.tmp / 'lane'
        self.fixture.git('worktree', 'add', '--detach', '-q', str(self.lane))
        self.personal_path = self.module.personal_manifest_path(self.lane, 'review')
        self.personal_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_manifest(self.personal_path)
        self.write_manifest(self.shared_path, self.policy('source_change', 'runtime_change'))
        (self.lane / '.syncwheel' / 'profile.local.json').write_text(json.dumps({
            'github_pr_merge': self.fixture.policy(),
        }))

    @staticmethod
    def policy(*allowed):
        return {'mode': 'ai-managed' if allowed else 'human-gated', 'allow': list(allowed)}

    def write_manifest(self, path, authority=None):
        manifest = copy.deepcopy(self.fixture.manifest)
        if authority is not None:
            manifest['authority'] = authority
        path.write_text(json.dumps(manifest) + '\n')

    def merge_args(self, *, apply=False, plan_digest=None, alias=False):
        args = self.fixture.args(apply=apply, plan_digest=plan_digest)
        args.repo = str(self.lane)
        if alias:
            args.manifest = str(self.shared_path.parent / '..' / '.syncwheel' / 'manifest.json')
        else:
            args.personal = 'review'
        return args

    def merge(self, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.module.command_stack_merge_pr(self.merge_args(**kwargs))
        self.assertEqual(result, 0)
        return json.loads(output.getvalue())

    def git_in(self, checkout, *args):
        return subprocess.check_output(['git', '-C', str(checkout), *args])

    def state(self):
        lane_index = Path(self.git_in(
            self.lane, 'rev-parse', '--path-format=absolute', '--git-path', 'index',
        ).decode().strip())
        return {
            'personal': self.personal_path.read_bytes(),
            'shared': self.shared_path.read_bytes(),
            'primary_index': (self.primary / '.git' / 'index').read_bytes(),
            'lane_index': lane_index.read_bytes(),
            'primary_ignore': (self.primary / '.gitignore').read_bytes(),
            'lane_ignore': (self.lane / '.gitignore').read_bytes(),
            'refs': self.fixture.git('for-each-ref'),
            'worktrees': self.fixture.git('worktree', 'list', '--porcelain'),
            'source': (self.lane / 'feature.txt').read_bytes(),
            'ledger': self.module.load_ledger_events(self.lane, self.personal_path),
        }

    def test_missing_personal_authority_inherits_shared_merge_permission(self):
        before = self.state()
        with mock.patch.object(self.module, 'github_adapter_request',
                               return_value=self.fixture.observation()) as adapter:
            plan = self.merge()

        adapter.assert_called_once()
        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['authority']['mode'], 'ai-managed')
        self.assertEqual(set(plan['authority']['allow']), {'source_change', 'runtime_change'})
        self.assertEqual(plan['authorityProvenance']['source'], 'inherited')
        self.assertEqual(self.state(), before)
        self.assertNotIn('authority', json.loads(self.personal_path.read_text()))

    def assert_merge_refused_without_adapter(self, detail, **kwargs):
        before = self.state()
        with mock.patch.object(self.module, 'github_adapter_request') as adapter:
            with self.assertRaisesRegex(self.module.SyncwheelError, detail):
                self.merge(**kwargs)
        adapter.assert_not_called()
        self.assertEqual(self.state(), before)

    def test_human_gated_personal_override_blocks_permission(self):
        self.write_manifest(self.personal_path, self.policy())

        self.assert_merge_refused_without_adapter("source_change allowed")

    def test_personal_runtime_permission_does_not_inherit_source_permission(self):
        self.write_manifest(self.personal_path, self.policy('runtime_change'))

        self.assert_merge_refused_without_adapter("source_change allowed")

    def test_personal_source_only_policy_can_merge_without_inheriting_runtime(self):
        self.write_manifest(self.personal_path, self.policy('source_change'))
        before = self.state()
        with mock.patch.object(self.module, 'github_adapter_request',
                               return_value=self.fixture.observation()):
            plan = self.merge()

        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['authority']['allow'], ['source_change'])
        self.assertEqual(self.state(), before)

    def test_explicit_personal_escalation_is_refused_before_adapter(self):
        self.write_manifest(self.shared_path, self.policy('source_change'))
        self.write_manifest(self.personal_path, self.policy('source_change', 'runtime_change'))

        self.assert_merge_refused_without_adapter(
            'selected authority exceeds canonical primary authority.*runtime_change',
        )

    def test_primary_revocation_between_preview_and_apply_blocks_merge(self):
        with mock.patch.object(self.module, 'github_adapter_request',
                               return_value=self.fixture.observation()):
            plan = self.merge()

        self.write_manifest(self.shared_path, self.policy())
        self.assert_merge_refused_without_adapter(
            "source_change allowed", apply=True, plan_digest=plan['planDigest'],
        )

    def test_primary_revocation_during_apply_observation_blocks_merge(self):
        with mock.patch.object(self.module, 'github_adapter_request',
                               return_value=self.fixture.observation()):
            plan = self.merge()
        before = self.state()

        def observe_then_revoke(_repo, request):
            self.assertNotEqual(request['operation'], 'merge')
            self.write_manifest(self.shared_path, self.policy())
            return self.fixture.observation()

        with mock.patch.object(self.module, 'github_adapter_request',
                               side_effect=observe_then_revoke) as adapter:
            with self.assertRaisesRegex(self.module.SyncwheelError, 'authority changed during observation'):
                self.merge(apply=True, plan_digest=plan['planDigest'])

        self.assertTrue(adapter.called)
        after = self.state()
        self.assertNotEqual(after.pop('shared'), before.pop('shared'))
        self.assertEqual(after, before)

    def test_malformed_shared_authority_prevents_local_grant(self):
        self.write_manifest(self.shared_path, {'mode': 'invalid'})
        self.write_manifest(self.personal_path, self.policy('source_change'))

        self.assert_merge_refused_without_adapter(r"authority\.mode")

    def test_invalid_shared_json_prevents_local_grant(self):
        self.shared_path.write_text('{')
        self.write_manifest(self.personal_path, self.policy('source_change'))

        self.assert_merge_refused_without_adapter('cannot read canonical primary authority')

    def test_alias_of_shared_manifest_is_classified_as_shared(self):
        before = self.state()
        with mock.patch.object(self.module, 'github_adapter_request',
                               return_value=self.fixture.observation()):
            plan = self.merge(alias=True)

        self.assertEqual(plan['status'], 'ready')
        self.assertEqual(plan['authority']['mode'], 'ai-managed')
        self.assertEqual(plan['authorityProvenance']['source'], 'shared')
        self.assertEqual(self.state(), before)

    def test_merge_using_inherited_authority_does_not_persist_the_grant(self):
        before = self.state()
        opened = self.fixture.observation()
        merged = self.fixture.observation(state='MERGED')
        with mock.patch.object(self.module, 'github_adapter_request', return_value=opened):
            plan = self.merge()

        with (
            mock.patch.object(self.module, 'github_adapter_request',
                              side_effect=[opened, {'ok': True}, merged]) as adapter,
            mock.patch.object(self.module, 'github_pr_merge_postverify',
                              return_value={'status': 'verified'}),
        ):
            receipt = self.merge(apply=True, plan_digest=plan['planDigest'])

        self.assertEqual(receipt['status'], 'succeeded')
        requests = [call.args[1] for call in adapter.call_args_list]
        self.assertEqual(sum(request['operation'] == 'merge' for request in requests), 1)
        after = self.state()
        ledger = after.pop('ledger')
        self.assertEqual(ledger[-1]['type'], 'github_pr_merge_receipt')
        self.assertEqual(ledger[:-3], before.pop('ledger'))
        self.assertEqual(after, before)
        self.assertNotIn('authority', json.loads(self.personal_path.read_text()))
