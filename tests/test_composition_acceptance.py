"""Selected compositions retain their own evidence and unrelated source work."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import test_channels
from _replay_support import hermetic_environment


class CompositionAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.environment_root = tempfile.TemporaryDirectory(prefix='composition-environment-')
        self.addCleanup(self.environment_root.cleanup)
        root = Path(self.environment_root.name)
        environment = hermetic_environment(root)
        environment.update({
            'XDG_CONFIG_HOME': str(root / 'config'),
            'XDG_STATE_HOME': str(root / 'state'),
            'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(root / 'settings.json'),
            'SYNCWHEEL_REPO_REGISTRY': str(root / 'registry.json'),
        })
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.fixture = test_channels.DeploymentChannelTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.create('schema-migration', stacks=())
        data = self.fixture.manifest()
        data['stacks'] = []
        data['integration']['stacks'] = []
        for name in 'abcdefghij':
            path = 'choice.txt' if name in ('e', 'f') else f'{name}.py'
            content = f'{name}\n' if name in ('e', 'f') else f'VALUE = {name!r}\n'
            if name == 'i':
                content = 'def broken(:\n'
            if name == 'j':
                content = 'from g import VALUE\nassert VALUE == "g"\n'
            tip = self.fixture.make_stack(name, path, content)
            entry = {
                'id': name, 'branch': f'pr/{name}', 'base': 'origin/main',
                'target_remote': 'origin', 'target_branch': 'main',
                'integration_branch': 'main-integration', 'commits': [tip],
            }
            if name in 'abc':
                entry['state'] = 'draft'
            if name == 'j':
                entry['depends_on'] = ['g']
            data['stacks'].append(entry)
            data['integration']['stacks'].append(name)
        self.fixture.git('switch', '-q', 'main-integration')
        (self.fixture.repo / '.syncwheel/manifest.json').write_text(json.dumps(data) + '\n')

    def sources(self):
        return {name: self.fixture.git('rev-parse', f'pr/{name}') for name in 'abcdefghij'}

    def test_conflicting_choices_are_independent_and_failed_union_preserves_valid_tip(self):
        fixture = self.fixture
        before_sources = self.sources()
        fixture.create('choice-e', stacks=('d', 'e'))
        e_plan, e_receipt = fixture.apply_channel('choice-e')
        fixture.create('hotfix-f', stacks=('d', 'f'))
        _, f_receipt = fixture.apply_channel('hotfix-f')
        self.assertEqual(fixture.git('show', 'channel/choice-e:choice.txt'), 'e')
        self.assertEqual(fixture.git('show', 'channel/hotfix-f:choice.txt'), 'f')
        self.assertNotEqual(e_receipt['compositionDigest'], f_receipt['compositionDigest'])
        self.assertEqual([pin['stack'] for pin in e_plan['composition']], ['d', 'e'])
        absent = fixture.git('ls-tree', '--name-only', 'channel/choice-e')
        self.assertNotIn('h.py', absent.splitlines())
        self.assertNotIn('i.py', absent.splitlines())

        # A required union is explicit; failure never replaces the previous tip.
        fixture.mutate('channel', 'add', 'choice-e', 'f')
        union = fixture.plan('choice-e')
        self.assertNotEqual(union['compositionDigest'], e_receipt['compositionDigest'])
        failed = fixture.cli('channel', 'apply', 'choice-e', '--plan-digest',
                             union['planDigest'], '--apply', expected=2)
        self.assertIn('plumbing replay stopped', failed.stderr)
        self.assertEqual(fixture.git('rev-parse', 'channel/choice-e'), e_receipt['tip'])
        self.assertEqual(fixture.git('rev-parse', 'channel/hotfix-f'), f_receipt['tip'])

        # Removing E changes the recipe; its old green receipt is not a proof of F.
        fixture.mutate('channel', 'remove', 'choice-e', 'e')
        replacement = fixture.plan('choice-e')
        self.assertNotEqual(replacement['compositionDigest'], e_receipt['compositionDigest'])
        self.assertNotEqual(replacement['compositionDigest'], union['compositionDigest'])
        self.assertEqual(fixture.git('rev-parse', 'channel/choice-e'), e_receipt['tip'])
        _, replacement_receipt = fixture.apply_channel('choice-e')
        self.assertEqual(replacement_receipt['compositionDigest'], replacement['compositionDigest'])
        self.assertEqual(fixture.git('show', 'channel/choice-e:choice.txt'), 'f')
        self.assertEqual(self.sources(), before_sources)

    def test_green_unwanted_and_ready_broken_sources_are_not_selected_implicitly(self):
        fixture = self.fixture
        compile(fixture.git('show', 'pr/h:h.py'), 'h.py', 'exec')
        with self.assertRaises(SyntaxError):
            compile(fixture.git('show', 'pr/i:i.py'), 'i.py', 'exec')
        fixture.create('selected', stacks=('d', 'e'))
        plan, receipt = fixture.apply_channel('selected')
        self.assertEqual([pin['stack'] for pin in plan['composition']], ['d', 'e'])
        paths = fixture.git('ls-tree', '--name-only', receipt['tip']).splitlines()
        self.assertNotIn('h.py', paths)
        self.assertNotIn('i.py', paths)

        # Materialization alone is not a successful behavioral check.
        fixture.create('broken', stacks=('i',))
        _, broken_receipt = fixture.apply_channel('broken')
        self.assertFalse(broken_receipt['deploymentAsserted'])
        with self.assertRaises(SyntaxError):
            compile(fixture.git('show', 'channel/broken:i.py'), 'i.py', 'exec')
        self.assertEqual(fixture.git('rev-parse', 'channel/selected'), receipt['tip'])


if __name__ == '__main__':
    unittest.main()
