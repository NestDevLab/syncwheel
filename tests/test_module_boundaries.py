"""Compatibility at the CLI/domain boundaries, including isolated file loading."""
import ast
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def load_facade(name='module_boundary_facade'):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / 'syncwheel.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ModuleBoundaryTest(unittest.TestCase):
    def test_file_loading_outside_source_directory_without_module_registration(self):
        code = '''
import importlib.util, pathlib, sys
path = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('unregistered_facade', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert 'unregistered_facade' not in sys.modules
assert module.source_checkout_root() == path.parents[1]
assert module.SOURCE_ROOT == path.parents[1]
assert module.VERSION == (path.parents[1] / 'VERSION').read_text().strip()
assert module.build_parser().parse_args(['status']).func is module.command_status
'''
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [sys.executable, '-I', '-c', code, str(SCRIPTS / 'syncwheel.py')],
                cwd=directory, check=True, capture_output=True, text=True,
            )

    def test_parser_binds_current_facade_handlers(self):
        module = load_facade()
        replacement = lambda args: 0
        with mock.patch.object(module, 'command_status', replacement):
            args = module.build_parser().parse_args(['status'])
            self.assertIs(args.func, replacement)
            self.assertEqual(module.command_behavior(args)['mutates'], 'never')
        self.assertIs(module.build_parser().parse_args(['status']).func, module.command_status)

    def test_domains_share_the_facade_exception_identity(self):
        module = load_facade()
        import syncwheel_errors
        import syncwheel_manifest
        import syncwheel_coordination
        self.assertIs(module.SyncwheelError, syncwheel_errors.SyncwheelError)
        self.assertIs(module.ManifestDurabilityError.__bases__[0], module.SyncwheelError)
        for action in (
            lambda: syncwheel_manifest.normalize_manifest_data({}, Path('manifest.json')),
            lambda: syncwheel_coordination.coordination_public_remote_ref_parts(
                {}, 'base', branch_is_valid=lambda value: True,
            ),
        ):
            with self.assertRaises(module.SyncwheelError):
                action()

    def test_journal_admission_uses_current_facade_status_and_reader(self):
        module = load_facade()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'note.md').write_text('safe content')
            with mock.patch.object(module, 'journal_status_entries', return_value=[
                {'status': '??', 'path': 'note.md'},
            ]) as status, mock.patch.object(
                module, 'journal_read_admitted_file',
                return_value=(b'safe content', ('fingerprint',), '100644'),
            ) as reader:
                admitted, excluded, rejected, fingerprints, blobs = module.journal_admission(
                    root, {'include': ['**'], 'exclude': [], 'max_file_bytes': 1024},
                )
            status.assert_called_once_with(root)
            reader.assert_called_once_with(root / 'note.md', 1024)
            self.assertEqual([entry['path'] for entry in admitted], ['note.md'])
            self.assertEqual((excluded, rejected), ([], []))
            self.assertEqual(fingerprints['note.md'], ('fingerprint',))
            self.assertEqual(blobs['note.md']['content'], b'safe content')

    def test_coordination_validation_keeps_git_validation_patchable(self):
        module = load_facade()
        ref = {'kind': 'remote-ref', 'role': 'canonical', 'ref': 'refs/heads/main'}
        with mock.patch.object(module, 'run') as run:
            run.return_value.returncode = 0
            self.assertEqual(module.coordination_public_remote_ref_parts(ref, 'base'),
                             ('canonical', 'refs/heads/main'))
            run.assert_called_once_with(['git', 'check-ref-format', 'refs/heads/main'], check=False)
        with mock.patch.object(module, 'is_valid_coordination_branch_ref', return_value=False):
            with self.assertRaises(module.SyncwheelError):
                module.coordination_public_remote_ref_parts(ref, 'base')

    def test_domains_do_not_import_the_cli_facade(self):
        modules = ('manifest', 'index', 'journal', 'ledger', 'coordination', 'errors', 'replay', 'worktrees', 'coordination_state', 'publication_intents')
        for name in modules:
            with self.subTest(module=name):
                tree = ast.parse((SCRIPTS / f'syncwheel_{name}.py').read_text())
                imports = {
                    alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names
                }
                imports.update(node.module for node in ast.walk(tree)
                               if isinstance(node, ast.ImportFrom))
                self.assertNotIn('syncwheel', imports)
                self.assertNotIn('syncwheel_cli', imports)


if __name__ == '__main__':
    unittest.main()
