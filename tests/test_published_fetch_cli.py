"""End-to-end tests of bounded published-recipe recovery through the CLI."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


CLI = Path(__file__).resolve().parents[1] / 'scripts' / 'syncwheel.py'
ALT = 'refs/heads/fixture/alternative'
STATE = 'refs/heads/syncwheel/state/default'


class PublishedFetchCliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='syncwheel-published-fetch-')
        cls.root = Path(cls.temp.name)
        cls.env = dict(os.environ, SYNCWHEEL_UPDATE_MODE='off', GIT_ALLOW_PROTOCOL='file',
                       SYNCWHEEL_LANE_OWNER='fixture-owner',
                       SYNCWHEEL_REPO_REGISTRY=str(cls.root / 'repos.json'),
                       SYNCWHEEL_UPDATE_SETTINGS_PATH=str(cls.root / 'settings.json'))
        cls.remote = cls.root / 'remote.git'
        seed, owner = cls.root / 'seed', cls.root / 'owner'
        cls.call(['git', 'init', '--bare', '-q', str(cls.remote)])
        cls.call(['git', 'init', '-q', '-b', 'main', str(seed)])
        cls.git(seed, 'config', 'user.name', 'Fixture')
        cls.git(seed, 'config', 'user.email', 'fixture@example.invalid')
        (seed / 'README').write_text('base\n')
        cls.git(seed, 'add', 'README')
        cls.git(seed, 'commit', '-qm', 'base')
        cls.git(seed, 'remote', 'add', 'origin', str(cls.remote))
        cls.git(seed, 'push', '-qu', 'origin', 'main')
        cls.call(['git', '--git-dir', str(cls.remote), 'symbolic-ref', 'HEAD', 'refs/heads/main'])
        cls.call(['git', 'clone', '-q', str(cls.remote), str(owner)])
        cls.git(owner, 'config', 'user.name', 'Fixture')
        cls.git(owner, 'config', 'user.email', 'fixture@example.invalid')
        cls.sw(owner, 'init', '--syncwheel-tracking', 'git-tracked', '--publication-remote', 'origin',
               '--integration-branch', 'integration/shared')
        cls.sw(owner, 'int', 'push')
        cls.sw(owner, 'stack', 'create', 'feature', '--branch', 'pr/feature')
        cls.git(owner, 'switch', '-qc', 'author', 'origin/main')
        (owner / 'side.txt').write_text('side\n')
        cls.git(owner, 'add', 'side.txt')
        cls.git(owner, 'commit', '-qm', 'side')
        (owner / 'feature.txt').write_text('feature\n')
        cls.git(owner, 'add', 'feature.txt')
        cls.git(owner, 'commit', '-qm', 'feature')
        cls.original = cls.git(owner, 'rev-parse', 'HEAD').stdout.strip()
        cls.git(owner, 'push', 'origin', 'author:' + ALT)
        cls.git(owner, 'switch', 'integration/shared')
        cls.sw(owner, 'stack', 'set', 'feature', cls.original, '--preserve-worktrees', 'fixture')
        cls.sw(owner, 'stack', 'rebuild', 'feature')
        cls.sw(owner, 'stack', 'push', 'feature')
        cls.sw(owner, 'int', 'rebuild')
        cls.sw(owner, 'int', 'push')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def call(cls, argv, cwd=None, env=None, check=True):
        result = subprocess.run(argv, cwd=cwd, env=env or cls.env, text=True, capture_output=True)
        if check and result.returncode:
            raise AssertionError(f'{argv!r}: {result.returncode}\n{result.stdout}\n{result.stderr}')
        return result

    @classmethod
    def git(cls, repo, *args, check=True):
        return cls.call(['git', *args], repo, check=check)

    @classmethod
    def sw(cls, repo, *args, env=None, check=True):
        return cls.call(['python3', str(CLI), *args], repo, env, check)

    def make_clone(self, name):
        bare, repo = self.root / (name + '.git'), self.root / name
        self.call(['git', 'clone', '--bare', '-q', 'file://' + str(self.remote), str(bare)])
        self.call(['git', 'clone', '-q', '--single-branch', '--branch', 'integration/shared',
                   'file://' + str(bare), str(repo)])
        self.git(repo, 'config', 'user.name', 'Fixture')
        self.git(repo, 'config', 'user.email', 'fixture@example.invalid')
        self.git(repo, 'fetch', '--quiet', '--no-tags', 'origin', 'refs/heads/main:refs/remotes/origin/main')
        self.sw(repo, 'hooks', 'install', '--apply')
        self.git(repo, 'tag', 'fixture-observed', 'HEAD')
        return bare, repo

    def snapshot(self, repo):
        path = repo / '.git' / 'FETCH_HEAD'
        ledger = repo / '.syncwheel' / 'ledger' / 'events' / '000001.jsonl'
        digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        return {'refs': self.git(repo, 'for-each-ref', '--format=%(refname) %(objectname)').stdout.splitlines(),
                'fetch_head': digest(path), 'manifest': digest(repo / '.syncwheel' / 'manifest.json'),
                'ledger': digest(ledger)}

    def test_explicit_fetch_and_rejections(self):
        cases = ('selected', 'wrong-tip', 'missing-ref', 'unreachable', 'source-drift', 'state-drift',
                 'unpaired', 'empty', 'bad-combination', 'local-only', 'legacy')
        for case in cases:
            with self.subTest(case=case):
                bare, repo = self.make_clone(case)
                if case == 'local-only':
                    self.git(repo, 'fetch', '--quiet', '--no-tags', '--no-write-fetch-head', '--refmap=', 'origin', ALT)
                if case == 'legacy':
                    self.git(repo, 'config', '--replace-all', 'remote.origin.fetch', '+refs/heads/*:refs/remotes/origin/*')
                present = self.git(repo, 'cat-file', '-e', self.original + '^{commit}', check=False).returncode == 0
                self.assertEqual(present, case == 'local-only')
                before = self.snapshot(repo)
                env = self.env.copy()
                ref = 'refs/heads/fixture/missing' if case == 'missing-ref' else ('refs/heads/main' if case in ('unreachable', 'local-only') else ALT)
                tip = ('0' * 40 if case == 'wrong-tip' else
                       self.call(['git', '--git-dir', str(bare), 'rev-parse', 'refs/heads/main']).stdout.strip()
                       if case in ('unreachable', 'local-only') else self.original)
                if case in ('source-drift', 'state-drift'):
                    bindir = self.root / (case + '-bin')
                    bindir.mkdir()
                    selected = ALT if case == 'source-drift' else STATE
                    replacement_ref = 'refs/heads/main' if case == 'source-drift' else STATE + '^'
                    replacement = self.call(['git', '--git-dir', str(bare), 'rev-parse', replacement_ref]).stdout.strip()
                    wrapper = bindir / 'git'
                    real_git = shutil.which('git')
                    wrapper.write_text('#!/usr/bin/python3\nimport subprocess,sys\na=sys.argv[1:]\n'
                        'r=subprocess.run([' + repr(real_git) + ',*a])\n'
                        'if r.returncode==0 and "fetch" in a and ' + repr(ALT) + ' in a:\n'
                        ' subprocess.run([' + repr(real_git) + ',"--git-dir",' + repr(str(bare)) +
                        ',"update-ref",' + repr(selected) + ',' + repr(replacement) + '],check=True)\n'
                        'sys.exit(r.returncode)\n')
                    wrapper.chmod(0o755)
                    env['PATH'] = str(bindir) + os.pathsep + self.env['PATH']
                options = [] if case == 'legacy' else ['--published-fetch-ref', '' if case == 'empty' else ref]
                if case not in ('unpaired', 'legacy'):
                    options += ['--published-fetch-tip', '' if case == 'empty' else tip]
                if case == 'bad-combination':
                    options += ['--base', 'origin/main']
                result = self.sw(repo, 'stack', 'set', 'feature', '--published', *options, env=env, check=False)
                after = self.snapshot(repo)
                if case in ('selected', 'local-only', 'legacy'):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.git(repo, 'cat-file', '-e', self.original + '^{commit}', check=False).returncode, 0)
                    if case == 'legacy':
                        self.assertIn('legacy remote fetch', result.stderr)
                    else:
                        self.assertEqual(before['fetch_head'], after['fetch_head'])
                        keep = lambda refs: [x for x in refs if not x.startswith('refs/heads/integration/shared ')]
                        self.assertEqual(keep(before['refs']), keep(after['refs']))
                        integration = lambda refs: next(x.split()[1] for x in refs if x.startswith('refs/heads/integration/shared '))
                        old_tip, new_tip = integration(before['refs']), integration(after['refs'])
                        if old_tip != new_tip:
                            self.git(repo, 'merge-base', '--is-ancestor', old_tip, new_tip)
                            paths = self.git(repo, 'diff', '--name-only', old_tip, new_tip).stdout.splitlines()
                            self.assertEqual(paths, ['.syncwheel/manifest.json'])
                else:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
