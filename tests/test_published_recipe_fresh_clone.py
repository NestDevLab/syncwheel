"""Fresh-clone recovery of two independently published sources."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / 'scripts' / 'syncwheel.py'


class PublishedRecipeFreshCloneTest(unittest.TestCase):
    def test_contention_retry_and_fresh_clone_recipe_preserve_foreign_lane(self):
        with tempfile.TemporaryDirectory(prefix='syncwheel-cas-') as temp:
            root = Path(temp)
            remote, seed = root / 'remote.git', root / 'seed'
            home, config, template = root / 'home', root / 'config', root / 'empty-template'
            for path in (home, config, template):
                path.mkdir()
            inherited = {k: v for k, v in os.environ.items()
                         if not k.startswith('GIT_') and not k.startswith('SYNCWHEEL_')}
            env = dict(inherited, HOME=str(home), XDG_CONFIG_HOME=str(config),
                       GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TEMPLATE_DIR=str(template),
                       SYNCWHEEL_UPDATE_MODE='off', GIT_ALLOW_PROTOCOL='file',
                       GIT_TERMINAL_PROMPT='0', SYNCWHEEL_LANE_OWNER='fixture',
                       SYNCWHEEL_REPO_REGISTRY=str(root / 'registry.json'),
                       SYNCWHEEL_UPDATE_SETTINGS_PATH=str(root / 'settings.json'))

            def call(args, cwd=None, *, extra=None):
                e = dict(env, **(extra or {}))
                p = subprocess.run(args, cwd=cwd, env=e, text=True, capture_output=True, timeout=45)
                self.assertEqual(p.returncode, 0, f'{args}: {p.stderr}\n{p.stdout}')
                return p.stdout.strip()

            def git(cwd, *args):
                return call(['git', *args], cwd)

            def sw(cwd, *args):
                return call([sys.executable, str(CLI), *args], cwd)

            call(['git', 'init', '--bare', '-q', str(remote)])
            call(['git', 'init', '-q', '-b', 'main', str(seed)])
            git(seed, 'config', 'user.name', 'Fixture')
            git(seed, 'config', 'user.email', 'fixture@example.invalid')
            (seed / 'README').write_text('fixture\n')
            git(seed, 'add', 'README')
            git(seed, 'commit', '-qm', 'base')
            git(seed, 'remote', 'add', 'origin', str(remote))
            git(seed, 'push', '-qu', 'origin', 'main')
            call(['git', '--git-dir', str(remote), 'symbolic-ref', 'HEAD', 'refs/heads/main'])
            first, second = root / 'first', root / 'second'
            call(['git', 'clone', '-q', str(remote), str(first)])
            git(first, 'config', 'user.name', 'Fixture')
            git(first, 'config', 'user.email', 'fixture@example.invalid')
            sw(first, 'init', '--syncwheel-tracking', 'git-tracked', '--publication-remote',
               'origin', '--integration-branch', 'integration/shared')
            sw(first, 'hooks', 'install', '--apply')
            self.assertTrue(json.loads(sw(first, 'hooks', 'status'))['ready'])
            sw(first, 'int', 'push')
            call(['git', 'clone', '-q', '--single-branch', '--branch', 'integration/shared',
                  str(remote), str(second)])
            git(second, 'config', 'user.name', 'Fixture')
            git(second, 'config', 'user.email', 'fixture@example.invalid')
            git(second, 'fetch', '--quiet', '--no-tags', 'origin',
                'refs/heads/main:refs/remotes/origin/main')
            sw(second, 'hooks', 'install', '--apply')
            self.assertTrue(json.loads(sw(second, 'hooks', 'status'))['ready'])
            base = git(first, 'rev-parse', 'origin/main')
            sw(first, 'stack', 'create', 'foreign', '--branch', 'pr/foreign', '--base', base)
            foreign_record = json.loads(call(
                [sys.executable, str(CLI), 'worktree', 'open', 'foreign-lane', '--into',
                 'foreign', '--base', base, '--json'], first,
                extra={'SYNCWHEEL_LANE_OWNER': 'foreign-owner'}))['lane']
            self.assertEqual(foreign_record['owner'], 'foreign-owner')
            self.assertNotEqual(foreign_record['owner'], env['SYNCWHEEL_LANE_OWNER'])
            foreign = Path(foreign_record['path'])
            (foreign / 'staged.txt').write_text('staged\n')
            git(foreign, 'add', 'staged.txt')
            (foreign / 'staged.txt').write_text('staged and modified\n')
            (foreign / '.gitignore').write_text('ignored.txt\n')
            (foreign / 'ignored.txt').write_text('ignored\n')
            (foreign / 'untracked.txt').write_text('untracked\n')
            (foreign / 'ignored.txt').chmod(0o600)
            git(first, 'worktree', 'lock', '--reason', 'foreign fixture', str(foreign))

            def snapshot():
                registry = json.loads((first / '.git/syncwheel/governed-worktrees.json').read_text())
                lane = next(x for x in registry['lanes'] if x['id'] == 'foreign-lane')
                index = Path(git(foreign, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
                files = {}
                for name in ('staged.txt', '.gitignore', 'ignored.txt', 'untracked.txt'):
                    path = foreign / name
                    files[name] = (hashlib.sha256(path.read_bytes()).hexdigest(),
                                   stat.S_IMODE(path.stat().st_mode))
                worktrees = git(first, 'worktree', 'list', '--porcelain')
                block = next(x for x in worktrees.split('\n\n') if str(foreign) in x)
                return (lane, git(foreign, 'rev-parse', 'HEAD'), hashlib.sha256(index.read_bytes()).hexdigest(),
                        git(foreign, 'status', '--porcelain=v1', '--ignored', '--untracked-files=all'),
                        files, block)

            before_c = snapshot()
            state_ref = 'refs/heads/syncwheel/state/default'
            state = git(first, 'ls-remote', 'origin', state_ref).split()[0]
            raw = call(['git', '--git-dir', str(remote), 'show',
                        state + ':.syncwheel/coordination-state.json'])
            digest = json.loads(raw)['manifest_digest']
            plans = {}
            for name, repo in (('one', first), ('two', second)):
                sw(repo, 'stack', 'create', name, '--branch', 'pr/' + name, '--base', base)
                lane = Path(json.loads(sw(repo, 'worktree', 'open', name + '-lane', '--into', name,
                                          '--base', base, '--json'))['lane']['path'])
                (lane / (name + '.txt')).write_text(name + '\n')
                git(lane, 'add', name + '.txt')
                git(lane, 'commit', '-qm', name)
                tip = git(lane, 'rev-parse', 'HEAD')
                args = ['stack', 'set', name, '--base', base, '--adopt-tip', tip]
                preview = json.loads(sw(repo, *args))
                sw(repo, *args, '--plan-digest', preview['planDigest'], '--apply')
                plan = json.loads(sw(repo, 'coordination', 'compose', '--stack', name,
                                     '--known-base-state', state, '--known-base-manifest-digest', digest))
                self.assertEqual(plan['expectedRemoteStateTip'], state)
                plans[name] = plan
                (root / (name + '.json')).write_text(json.dumps(plan))

            def refs():
                lines = call(['git', '--git-dir', str(remote), 'for-each-ref',
                              '--format=%(refname) %(objectname)']).splitlines()
                return dict(line.split(' ', 1) for line in lines)

            before_refs = refs()
            bindir = root / 'bin'
            bindir.mkdir()
            wrapper = bindir / 'git'
            wrapper.write_text('''#!%s\nimport json, os, pathlib, subprocess, sys, time\na = sys.argv[1:]\nif 'push' in a and '--atomic' in a and '--dry-run' not in a:\n p = pathlib.Path(os.environ['BARRIER']); name = os.environ['WRITER']\n (p / name).write_text(json.dumps(a))\n end = time.monotonic() + 30\n while not all((p / x).exists() for x in ('one', 'two')):\n  if time.monotonic() > end: sys.exit(97)\n  time.sleep(.01)\nsys.exit(subprocess.call([%r, *a]))\n''' % (sys.executable, shutil.which('git')))
            wrapper.chmod(0o700)
            processes = {}
            results = {}
            try:
                for name, repo in (('one', first), ('two', second)):
                    e = dict(env, PATH=str(bindir) + os.pathsep + env['PATH'], BARRIER=str(root), WRITER=name)
                    processes[name] = subprocess.Popen([sys.executable, str(CLI), 'coordination', 'compose',
                        '--apply', '--plan-file', str(root / (name + '.json'))], cwd=repo, env=e,
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                for name, process in processes.items():
                    out, err = process.communicate(timeout=45)
                    results[name] = (process.returncode, out, err)
            finally:
                for process in processes.values():
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                for process in processes.values():
                    process.communicate()
            self.assertEqual(sorted(v[0] for v in results.values()), [0, 2], results)
            winner = next(k for k, v in results.items() if v[0] == 0)
            loser = next(k for k, v in results.items() if v[0] == 2)
            self.assertIn('atomic transaction failed', results[loser][2])
            after_refs = refs()
            changed = {k for k in before_refs.keys() | after_refs.keys()
                       if before_refs.get(k) != after_refs.get(k)}
            self.assertEqual(changed, {state_ref, 'refs/heads/pr/' + winner,
                                      'refs/heads/syncwheel/claim/heads/pr/' + winner})
            self.assertEqual(after_refs['refs/heads/pr/' + winner], plans[winner]['sourceTip'])
            state_parents = call(['git', '--git-dir', str(remote), 'rev-list',
                                  '--parents', '-n', '1', after_refs[state_ref]]).split()
            self.assertEqual(state_parents, [after_refs[state_ref], state])
            self.assertNotIn('refs/heads/pr/' + loser, after_refs)
            self.assertNotIn('refs/heads/syncwheel/claim/heads/pr/' + loser, after_refs)
            for name in ('one', 'two'):
                argv = json.loads((root / name).read_text())
                source = 'refs/heads/pr/' + name
                claim = 'refs/heads/syncwheel/claim/heads/pr/' + name
                self.assertIn('--atomic', argv)
                self.assertEqual({x for x in argv if x.startswith('--force-with-lease=')},
                                 {'--force-with-lease=' + source + ':', '--force-with-lease=' + claim + ':',
                                  '--force-with-lease=' + state_ref + ':' + state})
                specs = [x for x in argv if ':' in x and not x.startswith('--')]
                self.assertEqual(len(specs), 3)
                self.assertEqual({x.split(':', 1)[1] for x in specs}, {source, claim, state_ref})
                self.assertEqual(next(x.split(':', 1)[0] for x in specs if x.endswith(':' + source)),
                                 plans[name]['sourceTip'])
            self.assertEqual(snapshot(), before_c)

            # Retry after a CAS refusal, not after a crash. This must
            # incorporate the winning publication without rewriting its source.
            retry_repo = first if loser == 'one' else second
            retry_plan = json.loads(sw(retry_repo, 'coordination', 'compose', '--stack', loser,
                                       '--known-base-state', state,
                                       '--known-base-manifest-digest', digest))
            retry_file = root / 'retry-plan.json'
            retry_file.write_text(json.dumps(retry_plan))
            sw(retry_repo, 'coordination', 'compose', '--apply', '--plan-file', str(retry_file))
            self.assertEqual(snapshot(), before_c)
            final_refs = refs()
            for name in ('one', 'two'):
                self.assertEqual(final_refs['refs/heads/pr/' + name], plans[name]['sourceTip'])
            self.assertEqual(final_refs['refs/heads/integration/shared'],
                             before_refs['refs/heads/integration/shared'])
            final_state = json.loads(call(['git', '--git-dir', str(remote), 'show',
                                           final_refs[state_ref] + ':.syncwheel/coordination-state.json']))
            declared = [s['id'] for s in final_state['manifest']['stacks'] if s['id'] in ('one', 'two')]
            self.assertEqual(declared, [winner, loser])
            self.assertEqual([x for x in final_state['manifest']['integration']['stacks']
                              if x in ('one', 'two')], declared)

            # A new consumer uses only the published remote. In particular, it
            # must not borrow Git objects or a manifest from either old clone.
            fresh = root / 'fresh'
            call(['git', 'clone', '-q', '--single-branch', '--branch', 'integration/shared',
                  remote.as_uri(), str(fresh)])
            git(fresh, 'config', 'user.name', 'Fixture')
            git(fresh, 'config', 'user.email', 'fixture@example.invalid')
            git(fresh, 'fetch', '--quiet', '--no-tags', 'origin',
                'refs/heads/main:refs/remotes/origin/main')
            sw(fresh, 'hooks', 'install', '--apply')
            # The documented recovery path reads the published state and
            # explicitly redeclares/adopts stacks in its recorded order.
            observed_state = git(fresh, 'ls-remote', 'origin', state_ref).split()[0]
            self.assertEqual(observed_state, final_refs[state_ref])
            git(fresh, 'fetch', '--quiet', '--no-tags', '--no-write-fetch-head',
                '--refmap=', 'origin', state_ref)
            recovered_state = json.loads(git(fresh, 'show',
                                             observed_state + ':.syncwheel/coordination-state.json'))
            self.assertEqual(git(fresh, 'ls-remote', 'origin', state_ref).split()[0], observed_state)
            by_id = {s['id']: s for s in recovered_state['manifest']['stacks']}
            recovered_order = [x for x in recovered_state['manifest']['integration']['stacks']
                               if x in ('one', 'two')]
            self.assertEqual(recovered_order, declared)
            for name in recovered_order:
                entry = by_id[name]
                source_ref = 'refs/heads/' + entry['branch']
                tip = recovered_state['managed_refs'][source_ref]
                self.assertEqual(tip, plans[name]['sourceTip'])
                self.assertEqual(entry['adopted_history'], {'base': base, 'tip': tip})
                self.assertEqual(entry['commits'], [tip])
                self.assertEqual(git(fresh, 'ls-remote', 'origin', source_ref).split()[0], tip)
                absent = subprocess.run(['git', 'cat-file', '-e', tip + '^{commit}'],
                                        cwd=fresh, env=env, capture_output=True, timeout=10)
                self.assertNotEqual(absent.returncode, 0, f'{name} tip was already available')
                git(fresh, 'fetch', '--quiet', '--no-tags', 'origin',
                    source_ref + ':refs/remotes/origin/' + entry['branch'])
                self.assertEqual(git(fresh, 'rev-parse', 'refs/remotes/origin/' + entry['branch']), tip)
                git(fresh, 'cat-file', '-e', base + '^{commit}')
                git(fresh, 'cat-file', '-e', tip + '^{commit}')
                sw(fresh, 'stack', 'create', name, '--branch', entry['branch'], '--base', base)
                args = ['stack', 'set', name, '--base', base, '--adopt-tip', tip]
                adoption = json.loads(sw(fresh, *args))
                self.assertEqual(adoption['observedRemoteTip'], tip)
                sw(fresh, *args, '--plan-digest', adoption['planDigest'], '--apply')
                sw(fresh, 'stack', 'rebuild', name)
                self.assertEqual(git(fresh, 'rev-parse', entry['branch'] + '^{tree}'),
                                 call(['git', '--git-dir', str(remote), 'rev-parse', tip + '^{tree}']))
            local_manifest = json.loads((fresh / '.syncwheel/manifest.json').read_text())
            self.assertEqual([x for x in local_manifest['integration']['stacks']
                              if x in ('one', 'two')], declared)
            self.assertEqual(snapshot(), before_c)


if __name__ == '__main__':
    unittest.main()
