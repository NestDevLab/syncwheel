import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location('syncwheel', Path(__file__).resolve().parents[1] / 'scripts' / 'syncwheel.py')
syncwheel = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(syncwheel)


class PatchIdCacheTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Patch ID Test')
        self.git('config', 'user.email', 'patch@example.test')
        self.commits = []
        self.write('root.txt', 'root\n')
        self.commit('root')
        self.write('normal.txt', 'normal\n')
        self.commit('normal')
        self.git('mv', 'normal.txt', 'renamed.txt')
        self.commit('rename')
        self.write('binary.bin', b'\0\1\2\3\xff')
        self.commit('binary')
        self.git('update-index', '--chmod=+x', 'renamed.txt')
        self.commit('mode')
        self.git('commit', '-q', '--allow-empty', '-m', 'empty')
        self.commits.append(self.git('rev-parse', 'HEAD').strip())
        self.git('branch', 'side')
        self.write('main.txt', 'main\n')
        self.commit('main')
        self.git('checkout', '-q', 'side')
        self.write('side.txt', 'side\n')
        self.commit('side')
        self.git('checkout', '-q', 'master')
        self.git('merge', '-q', '--no-ff', 'side', '-m', 'merge')
        self.commits.append(self.git('rev-parse', 'HEAD').strip())

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args, input_text=None):
        return subprocess.run(['git', *args], cwd=self.repo, input=input_text,
                              capture_output=True, text=True, check=True).stdout

    def write(self, name, contents):
        (self.repo / name).write_bytes(contents if isinstance(contents, bytes) else contents.encode())
        self.git('add', name)

    def commit(self, message):
        self.git('commit', '-q', '-m', message)
        self.commits.append(self.git('rev-parse', 'HEAD').strip())

    def legacy(self, commit):
        if len(self.git('rev-list', '--parents', '-n', '1', commit).split()) != 2:
            return None
        shown = self.git('show', '--format=', commit)
        patch = subprocess.run(['git', 'patch-id', '--stable'], input=shown,
                               text=True, capture_output=True, check=True).stdout.strip()
        return patch.split()[0] if patch else None

    def test_batch_matches_legacy_on_all_commit_shapes(self):
        expected = {commit: self.legacy(commit) for commit in self.commits}
        self.assertEqual(expected, syncwheel.commit_patch_ids(self.repo, self.commits))
        self.assertEqual({value for value in expected.values() if value},
                         syncwheel.patch_ids_reachable_from_ref(self.repo, 'HEAD'))
        with mock.patch.object(syncwheel, 'batch_uncached_patch_ids', side_effect=AssertionError('cache miss')):
            self.assertEqual(expected, syncwheel.commit_patch_ids(self.repo, self.commits))
        fresh_module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(fresh_module)
        with mock.patch.object(fresh_module, 'batch_uncached_patch_ids', side_effect=AssertionError('disk cache miss')):
            self.assertEqual(expected, fresh_module.commit_patch_ids(self.repo, self.commits))

    def test_config_change_invalidates_namespace(self):
        before = syncwheel.patch_id_semantics_key(self.repo)
        syncwheel.commit_patch_ids(self.repo, self.commits)
        self.git('config', 'diff.renames', 'false')
        syncwheel.patch_id_semantics_key.cache_clear()  # Models a new CLI invocation.
        after = syncwheel.patch_id_semantics_key(self.repo)
        self.assertNotEqual(before, after)
        self.assertEqual({commit: self.legacy(commit) for commit in self.commits},
                         syncwheel.commit_patch_ids(self.repo, self.commits))
        self.git('config', 'diff.algorithm', 'histogram')
        self.git('config', 'diff.mnemonicPrefix', 'true')
        self.git('config', 'core.quotePath', 'false')
        syncwheel.patch_id_semantics_key.cache_clear()
        self.assertEqual({commit: self.legacy(commit) for commit in self.commits},
                         syncwheel.commit_patch_ids(self.repo, self.commits))

    def test_attributes_change_invalidates_namespace(self):
        before = syncwheel.patch_id_semantics_key(self.repo)
        self.write('.gitattributes', '*.bin binary\n')
        syncwheel.patch_id_semantics_key.cache_clear()  # Models a new CLI invocation.
        self.assertNotEqual(before, syncwheel.patch_id_semantics_key(self.repo))

    def test_corrupt_entry_and_database_recompute(self):
        syncwheel.commit_patch_ids(self.repo, self.commits)
        path = syncwheel.git_common_dir(self.repo) / 'syncwheel' / 'patch-ids-v1.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('UPDATE patch_ids SET patch_id=? WHERE commit_sha=?',
                               ('corrupt', self.commits[1]))
        self.assertEqual(self.legacy(self.commits[1]), syncwheel.commit_patch_id(self.repo, self.commits[1]))
        path.write_bytes(b'corrupt database')
        self.assertEqual(self.legacy(self.commits[2]), syncwheel.commit_patch_id(self.repo, self.commits[2]))

    def test_concurrent_writers_share_common_git_directory(self):
        other = Path(self.temp.name) / 'other'
        self.git('worktree', 'add', '-q', '-b', 'other', str(other))
        self.assertEqual(syncwheel.git_common_dir(self.repo), syncwheel.git_common_dir(other))
        self.assertEqual(syncwheel.patch_id_semantics_key(self.repo),
                         syncwheel.patch_id_semantics_key(other))
        outputs = []
        errors = []
        def writer(repo):
            try:
                outputs.append(syncwheel.commit_patch_ids(repo, self.commits))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=writer, args=(repo,)) for repo in (self.repo, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        expected = {commit: self.legacy(commit) for commit in self.commits}
        self.assertEqual([expected, expected], outputs)

    def test_concurrent_process_writers(self):
        source = Path(__file__).resolve().parents[1] / 'scripts' / 'syncwheel.py'
        program = (
            'import importlib.util,json,sys; from pathlib import Path; '
            's=importlib.util.spec_from_file_location("sw",sys.argv[1]); '
            'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
            'print(json.dumps(m.commit_patch_ids(Path(sys.argv[2]),json.loads(sys.argv[3]))))'
        )
        args = ['python3', '-c', program, str(source), str(self.repo), json.dumps(self.commits)]
        processes = [subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True) for _ in range(2)]
        outputs = [process.communicate(timeout=30) for process in processes]
        for process, (_, stderr) in zip(processes, outputs):
            self.assertEqual(0, process.returncode, stderr)
        expected = {commit: self.legacy(commit) for commit in self.commits}
        self.assertEqual([expected, expected], [json.loads(stdout) for stdout, _ in outputs])

    def test_sha256_repository_resolves_abbreviated_commit(self):
        other = Path(self.temp.name) / 'sha256'
        init = subprocess.run(['git', 'init', '-q', '--object-format=sha256', str(other)],
                              capture_output=True, text=True)
        if init.returncode:
            self.skipTest('Git does not support SHA-256 repositories')
        subprocess.run(['git', 'config', 'user.name', 'Patch ID Test'], cwd=other, check=True)
        subprocess.run(['git', 'config', 'user.email', 'patch@example.test'], cwd=other, check=True)
        (other / 'file').write_text('root\n')
        subprocess.run(['git', 'add', 'file'], cwd=other, check=True)
        subprocess.run(['git', 'commit', '-qm', 'root'], cwd=other, check=True)
        (other / 'file').write_text('changed\n')
        subprocess.run(['git', 'commit', '-qam', 'changed'], cwd=other, check=True)
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=other, text=True).strip()
        self.assertEqual(64, len(commit))
        shown = subprocess.check_output(['git', 'show', '--format=', commit], cwd=other, text=True)
        legacy = subprocess.check_output(['git', 'patch-id', '--stable'], input=shown, text=True).split()[0]
        self.assertEqual(legacy, syncwheel.commit_patch_id(other, commit))
        self.assertEqual(syncwheel.commit_patch_id(other, commit),
                         syncwheel.commit_patch_id(other, commit[:40]))


if __name__ == '__main__':
    unittest.main()
