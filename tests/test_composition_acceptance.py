"""Selected compositions retain their own evidence and unrelated source work."""

import json
import os
import sys
import tempfile
import subprocess
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

    def test_broken_prerequisite_blocks_only_its_selected_dependent(self):
        fixture = self.fixture
        broken = fixture.make_stack('g', 'g.py', 'def broken(:\n')
        fixture.git('switch', '-q', 'main-integration')
        fixture.declare_stack_tip('g', broken)
        before = self.sources()
        refused = fixture.cli('channel', 'create', 'without-g', '--stack', 'j', expected=2)
        self.assertIn('requires earlier dependency', refused.stderr)
        fixture.create('dependent', stacks=('g', 'j'))
        _, dependent = fixture.apply_channel('dependent')
        g_source = fixture.git('show', f"{dependent['tip']}:g.py")
        with self.assertRaises(SyntaxError):
            compile(g_source, 'g.py', 'exec')
        fixture.create('independent', stacks=('d',))
        _, independent = fixture.apply_channel('independent')
        compile(fixture.git('show', f"{independent['tip']}:d.py"), 'd.py', 'exec')
        self.assertEqual(self.sources(), before)
        fixture.mutate('channel', 'close', 'dependent')
        abandoned = fixture.cli('stack', 'close', 'g', '--reason', 'abandoned', expected=2)
        self.assertIn('depend', abandoned.stderr)
        self.assertEqual(fixture.git('rev-parse', 'channel/dependent'), dependent['tip'])
        data = fixture.manifest()
        self.assertEqual(next(item for item in data['stacks'] if item['id'] == 'j')['depends_on'], ['g'])

    def test_actual_check_detects_undeclared_semantic_dependency(self):
        fixture = self.fixture
        # The commit graph alone cannot infer an import's runtime requirement.
        hidden = fixture.make_stack('d', 'd.py', 'from a import VALUE\nassert VALUE == "a"\n')
        fixture.git('switch', '-q', 'main-integration')
        fixture.declare_stack_tip('d', hidden)
        fixture.create('hidden', stacks=('d',))
        _, receipt = fixture.apply_channel('hidden')
        export = fixture.root / 'check-export'
        export.mkdir()
        (export / 'd.py').write_text(fixture.git('show', f"{receipt['tip']}:d.py"))
        command = [sys.executable, '-I', '-c',
                   'import runpy,sys; sys.path.insert(0,sys.argv[1]); runpy.run_path(sys.argv[1]+"/d.py")',
                   str(export)]
        checked = subprocess.run(command,
                                 cwd=export, capture_output=True, text=True)
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn('ModuleNotFoundError', checked.stderr)
        self.assertNotIn('a.py', fixture.git('ls-tree', '--name-only', receipt['tip']).splitlines())
        fixture.mutate('channel', 'close', 'hidden')
        data = fixture.manifest()
        next(item for item in data['stacks'] if item['id'] == 'd')['depends_on'] = ['a']
        (fixture.repo / '.syncwheel/manifest.json').write_text(json.dumps(data) + '\n')
        fixture.create('declared', stacks=('a', 'd'))
        _, declared = fixture.apply_channel('declared')
        for path in ('a.py', 'd.py'):
            (export / path).write_text(fixture.git('show', f"{declared['tip']}:{path}"))
        checked = subprocess.run(command, cwd=export, capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(fixture.git('rev-parse', 'channel/hidden'), receipt['tip'])

    def test_promoting_a_draft_does_not_select_it_for_the_existing_composition(self):
        fixture = self.fixture
        fixture.create('selected', stacks=('d', 'e'))
        _, receipt = fixture.apply_channel('selected')
        selected = next(c for c in fixture.manifest()['channels'] if c['id'] == 'selected')
        fixture.cli('stack', 'promote', 'a', '--branch', 'pr/a')
        self.assertEqual(next(c for c in fixture.manifest()['channels'] if c['id'] == 'selected'), selected)
        self.assertEqual(fixture.git('rev-parse', 'channel/selected'), receipt['tip'])
        self.assertNotIn('a.py', fixture.git('ls-tree', '--name-only', receipt['tip']).splitlines())
        self.assertEqual(next(s for s in fixture.manifest()['stacks'] if s['id'] == 'a')['state'], 'published')

    def test_integrated_prerequisite_is_present_in_the_new_pinned_base(self):
        fixture = self.fixture
        data = fixture.manifest()
        # Keep old source ranges stable when the remote delivery base advances.
        for stack in data['stacks']:
            stack['base'] = fixture.base
        fixture.git('push', '-q', 'origin', 'pr/g:main')
        integrated = fixture.git('rev-parse', 'origin/main')
        fixture.git('switch', '-q', '-c', 'pr/j-updated', integrated)
        (fixture.repo / 'j.py').write_text('from g import VALUE\nassert VALUE == "g"\n')
        fixture.git('add', 'j.py')
        fixture.git('commit', '-q', '-m', 'dependent on delivered base')
        tip = fixture.git('rev-parse', 'HEAD')
        fixture.git('switch', '-q', 'main-integration')
        updated = dict(next(s for s in data['stacks'] if s['id'] == 'j'))
        updated.update(id='j-updated', branch='pr/j-updated', base='origin/main', commits=[tip])
        data['stacks'].append(updated)
        data['integration']['stacks'].append('j-updated')
        (fixture.repo / '.syncwheel/manifest.json').write_text(json.dumps(data) + '\n')
        fixture.create('new-base', stacks=('g', 'j-updated'))
        plan, receipt = fixture.apply_channel('new-base')
        self.assertEqual(plan['baseRevision'], integrated)
        self.assertEqual(fixture.git('merge-base', '--is-ancestor', integrated, receipt['tip']), '')
        self.assertEqual(fixture.git('show', f"{receipt['tip']}:g.py"), "VALUE = 'g'")
        self.assertEqual(fixture.git('show', f"{receipt['tip']}:j.py"), 'from g import VALUE\nassert VALUE == "g"')
        self.assertEqual(next(s for s in fixture.manifest()['stacks'] if s['id'] == 'j-updated')['depends_on'], ['g'])
        channel = next(c for c in fixture.manifest()['channels'] if c['id'] == 'new-base')
        pins_before = json.loads(json.dumps(channel['composition']))
        module = fixture.load_module()
        with mock.patch.object(module.ReplayEngine, 'git_supports_write_tree', return_value=False):
            fallback = module.materialize_channel_tip(fixture.repo, channel, plan)
        self.assertEqual(fixture.git('rev-parse', fallback + '^{tree}'),
                         fixture.git('rev-parse', receipt['tip'] + '^{tree}'))
        self.assertEqual(channel['composition'], pins_before)
        self.assertEqual(module.materialize_channel_tree(fixture.repo, channel),
                         fixture.git('rev-parse', receipt['tip'] + '^{tree}'))

        twin = fixture.make_stack('g-twin', 'g.py', "VALUE = 'g'\n")
        fixture.git('switch', '-q', 'main-integration')
        data = fixture.manifest()
        data['stacks'].append(dict(id='g-twin', branch='pr/g-twin', base=fixture.base,
            target_remote='origin', target_branch='main', integration_branch='main-integration', commits=[twin]))
        data['integration']['stacks'].append('g-twin')
        (fixture.repo / '.syncwheel/manifest.json').write_text(json.dumps(data) + '\n')
        fixture.create('equivalent-patch', stacks=('g-twin',))
        twin_plan = fixture.plan('equivalent-patch')
        refused = fixture.cli('channel', 'apply', 'equivalent-patch', '--plan-digest',
                              twin_plan['planDigest'], '--apply', expected=2)
        self.assertIn('empty', refused.stderr)
        twin_channel = next(c for c in fixture.manifest()['channels'] if c['id'] == 'equivalent-patch')
        with self.assertRaisesRegex(module.SyncwheelError, 'empty'):
            module.materialize_channel_tree(fixture.repo, twin_channel)
        self.assertEqual(fixture.git('show-ref', '--verify', 'refs/heads/channel/equivalent-patch', check=False), '')


if __name__ == '__main__':
    unittest.main()
