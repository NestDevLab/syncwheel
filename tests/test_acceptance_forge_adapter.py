"""Authenticated-host authority protocol over controlled GitHub API fixtures."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from test_scenario_acceptance import fixture_receipt, forge_authority_responses
import syncwheel_github as github


class ForgeAuthorityAdapterTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'owner'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.repo)], check=True)
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.repo), *args], text=True).strip()
        self.git = git
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        path = self.repo / '.syncwheel/acceptance-policy.json'
        path.parent.mkdir()
        receipt = fixture_receipt()
        path.write_text(json.dumps({'repository': 'example/project', 'scenarios': [
            {'id': receipt['scenario_id'], 'source': receipt['source']}]}))
        git('add', '.')
        git('commit', '-qm', 'Owner policy')
        self.responses = forge_authority_responses(self.repo / '.git')
        self.request = {'operation': 'observe_acceptance_authority',
                        'repository': 'example/project', 'baseBranch': 'main'}
        self.calls = []

    def read(self, argv, cwd):
        self.assertEqual(argv[:3], ['api', '--hostname', 'github.com'])
        self.calls.append(argv[-1])
        return {'value': copy.deepcopy(self.responses[argv[-1]])}

    def observe(self):
        with mock.patch.object(github, 'run_json', side_effect=self.read):
            return github.observe_acceptance_authority(self.request, str(self.repo))

    def key(self, suffix):
        return next(key for key in self.responses if suffix in key)

    def test_present_policy_binds_host_repository_ref_commit_tree_and_blob(self):
        result = self.observe()
        self.assertEqual(result['host'], 'github.com')
        self.assertEqual(result['repository'], {'id': 1000, 'full_name': 'example/project'})
        self.assertEqual(result['base_sha'], self.git('rev-parse', 'main'))
        self.assertEqual(result['root_tree_sha'], self.git('rev-parse', 'main^{tree}'))
        self.assertEqual(result['blob_sha'], self.git('rev-parse', 'main:.syncwheel/acceptance-policy.json'))
        self.assertEqual(result['state'], 'present')
        self.assertIn('scenarios', json.loads(result['content']))
        self.assertEqual(len(self.calls), 6)

    def test_absence_requires_complete_authenticated_tree(self):
        self.git('rm', '-q', '.syncwheel/acceptance-policy.json')
        self.git('commit', '-qm', 'Owner has no policy')
        self.responses = forge_authority_responses(self.repo / '.git')
        result = self.observe()
        self.assertEqual(result['state'], 'absent')
        self.assertIsNone(result['blob_sha'])
        self.assertIsNone(result['content'])
        self.responses[self.key('/git/trees/')]['truncated'] = True
        with self.assertRaisesRegex(github.AdapterError, 'incomplete tree'):
            self.observe()

    def test_unavailable_or_mismatched_objects_never_become_policy_absence(self):
        original = copy.deepcopy(self.responses)
        for label in ['repository', 'ref', 'commit', 'tree', 'blob', 'content']:
            with self.subTest(case=label):
                self.responses = copy.deepcopy(original)
                if label == 'repository':
                    self.responses['repos/example/project']['full_name'] = 'other/project'
                elif label == 'ref':
                    self.responses[self.key('/git/ref/')]['ref'] = 'refs/heads/other'
                elif label == 'commit':
                    self.responses[self.key('/git/commits/')]['sha'] = '0' * 40
                elif label == 'tree':
                    self.responses[self.key('/git/trees/')]['sha'] = '0' * 40
                elif label == 'blob':
                    self.responses[self.key('/git/blobs/')]['sha'] = '0' * 40
                else:
                    self.responses[self.key('/git/blobs/')]['content'] = 'e30='
                with self.assertRaises(github.AdapterError):
                    self.observe()
        with mock.patch.object(github, 'run_json', side_effect=github.AdapterError('HTTP 403')):
            with self.assertRaisesRegex(github.AdapterError, 'HTTP 403'):
                github.observe_acceptance_authority(self.request, str(self.repo))

    def test_malformed_complete_tree_cannot_be_claimed_as_absent_policy(self):
        self.responses[self.key('/git/trees/')]['tree'] = [None]
        with self.assertRaisesRegex(github.AdapterError, 'malformed authenticated'):
            self.observe()

    def test_native_adapter_entry_pins_host_even_with_a_different_environment_host(self):
        data = self.root / 'responses.json'
        data.write_text(json.dumps(self.responses))
        bindir = self.root / 'bin'
        bindir.mkdir()
        gh = bindir / 'gh'
        gh.write_text('''#!/usr/bin/env python3
import json,os,sys
assert os.environ['GH_HOST']=='github.com'
assert sys.argv[1:4]==['api','--hostname','github.com']
print(json.dumps(json.load(open(os.environ['FORGE_FIXTURE_RESPONSES']))[sys.argv[-1]]))
''')
        gh.chmod(0o755)
        env = dict(os.environ, GH_HOST='untrusted.invalid', FORGE_FIXTURE_RESPONSES=str(data),
                   PATH=str(bindir) + os.pathsep + os.environ['PATH'])
        result = subprocess.run([sys.executable, str(Path(github.__file__))], input=json.dumps(self.request),
                                cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['observation']['host'], 'github.com')
        self.assertEqual(payload['observation']['state'], 'present')


if __name__ == '__main__':
    unittest.main()
