"""Black-box tests for coordinated publication after a base advance."""
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / 'scripts' / 'syncwheel.py'


class ComposeBaseAdvanceCliTest(unittest.TestCase):
    def test_selected_advance_applies_with_scoped_refs(self):
        self.run_fixture('apply')

    def test_linear_published_recipe_advance(self):
        self.run_fixture('linear')

    def test_target_drift_rejects_advance(self):
        self.run_fixture('drift')

    def test_target_race_does_not_assert_qualification(self):
        self.run_fixture('race')

    def run_fixture(self, mode):
        with tempfile.TemporaryDirectory(prefix='syncwheel-advance-') as temp:
            root = Path(temp)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith('GIT_') and not k.startswith('SYNCWHEEL_')}
            home = root / 'home'
            home.mkdir()
            env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_ALLOW_PROTOCOL='file',
                       SYNCWHEEL_UPDATE_MODE='off', SYNCWHEEL_LANE_OWNER='fixture',
                       SYNCWHEEL_REPO_REGISTRY=str(root / 'registry.json'),
                       SYNCWHEEL_UPDATE_SETTINGS_PATH=str(root / 'settings.json'))

            def run(argv, cwd=None, check=True):
                p = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=45)
                if check:
                    self.assertEqual(p.returncode, 0, f'{argv}: {p.stdout}\n{p.stderr}')
                return p

            def git(repo, *args):
                return run(['git', *args], repo).stdout.strip()

            def sw(repo, *args, check=True):
                return run([sys.executable, str(CLI), *args], repo, check)

            remote, seed, repo = root / 'remote.git', root / 'seed', root / 'owner'
            run(['git', 'init', '--bare', '-q', str(remote)])
            run(['git', 'init', '-q', '-b', 'main', str(seed)])
            git(seed, 'config', 'user.name', 'Fixture')
            git(seed, 'config', 'user.email', 'fixture@example.invalid')
            (seed / 'base.txt').write_text('base\n')
            git(seed, 'add', 'base.txt')
            git(seed, 'commit', '-qm', 'base')
            git(seed, 'remote', 'add', 'origin', str(remote))
            git(seed, 'push', '-qu', 'origin', 'main')
            run(['git', '--git-dir', str(remote), 'symbolic-ref', 'HEAD', 'refs/heads/main'])
            run(['git', 'clone', '-q', str(remote), str(repo)])
            git(repo, 'config', 'user.name', 'Fixture')
            git(repo, 'config', 'user.email', 'fixture@example.invalid')
            sw(repo, 'init', '--syncwheel-tracking', 'git-tracked', '--publication-remote', 'origin',
               '--integration-branch', 'integration/shared')
            sw(repo, 'hooks', 'install', '--apply')
            sw(repo, 'int', 'push')
            state_ref = 'refs/heads/syncwheel/state/default'
            base_state = git(repo, 'ls-remote', 'origin', state_ref).split()[0]
            base_json = json.loads(run(['git', '--git-dir', str(remote), 'show',
                                       base_state + ':.syncwheel/coordination-state.json']).stdout)
            old_base = git(repo, 'rev-parse', 'origin/main')
            sw(repo, 'stack', 'create', 'foreign', '--branch', 'pr/foreign', '--base', old_base)
            foreign = Path(json.loads(sw(repo, 'worktree', 'open', 'foreign-lane', '--into', 'foreign',
                                         '--base', old_base, '--json').stdout)['lane']['path'])
            (foreign / 'foreign.txt').write_text('foreign\n')
            git(foreign, 'add', 'foreign.txt')
            git(foreign, 'commit', '-qm', 'foreign')
            foreign_tip = git(foreign, 'rev-parse', 'HEAD')
            args = ['stack', 'set', 'foreign', '--base', old_base, '--adopt-tip', foreign_tip]
            plan = json.loads(sw(repo, *args).stdout)
            sw(repo, *args, '--plan-digest', plan['planDigest'], '--apply')
            plan = json.loads(sw(repo, 'coordination', 'compose', '--stack', 'foreign',
                                 '--known-base-state', base_state, '--known-base-manifest-digest',
                                 base_json['manifest_digest']).stdout)
            planfile = root / 'foreign-plan.json'
            planfile.write_text(json.dumps(plan))
            sw(repo, 'coordination', 'compose', '--apply', '--plan-file', str(planfile))
            base_state = git(repo, 'ls-remote', 'origin', state_ref).split()[0]
            base_json = json.loads(run(['git', '--git-dir', str(remote), 'show',
                                       base_state + ':.syncwheel/coordination-state.json']).stdout)
            if mode == 'linear':
                git(seed, 'switch', '-qc', 'feature-fixture')
                (seed / 'first.txt').write_text('first\n')
                git(seed, 'add', 'first.txt')
                git(seed, 'commit', '-qm', 'first feature')
                first = git(seed, 'rev-parse', 'HEAD')
                (seed / 'second.txt').write_text('second\n')
                git(seed, 'add', 'second.txt')
                git(seed, 'commit', '-qm', 'second feature')
                old_tip = git(seed, 'rev-parse', 'HEAD')
                git(seed, 'switch', 'main')
                git(repo, 'fetch', str(seed), old_tip)
                git(repo, 'branch', 'pr/feature', old_tip)
                sw(repo, 'stack', 'create', 'feature', first, old_tip,
                   '--branch', 'pr/feature', '--base', old_base)
                lane = Path(json.loads(sw(repo, 'worktree', 'open', 'feature-lane', '--into', 'feature',
                                          '--base', old_tip, '--json').stdout)['lane']['path'])
            else:
                sw(repo, 'stack', 'create', 'feature', '--branch', 'pr/feature', '--base', old_base)
                lane = Path(json.loads(sw(repo, 'worktree', 'open', 'feature-lane', '--into', 'feature',
                                          '--base', old_base, '--json').stdout)['lane']['path'])
                (lane / 'feature.txt').write_text('feature\n')
                git(lane, 'add', 'feature.txt')
                git(lane, 'commit', '-qm', 'feature')
                old_tip = git(lane, 'rev-parse', 'HEAD')
                args = ['stack', 'set', 'feature', '--base', old_base, '--adopt-tip', old_tip]
                plan = json.loads(sw(repo, *args).stdout)
                sw(repo, *args, '--plan-digest', plan['planDigest'], '--apply')
            plan = json.loads(sw(repo, 'coordination', 'compose', '--stack', 'feature',
                                 '--known-base-state', base_state, '--known-base-manifest-digest',
                                 base_json['manifest_digest']).stdout)
            planfile = root / 'plan.json'
            planfile.write_text(json.dumps(plan))
            sw(repo, 'coordination', 'compose', '--apply', '--plan-file', str(planfile))
            published = git(repo, 'ls-remote', 'origin', state_ref).split()[0]
            state = json.loads(run(['git', '--git-dir', str(remote), 'show',
                                    published + ':.syncwheel/coordination-state.json']).stdout)
            (seed / 'base2.txt').write_text('advance\n')
            git(seed, 'add', 'base2.txt')
            git(seed, 'commit', '-qm', 'advance base')
            git(seed, 'push', 'origin', 'main')
            git(repo, 'fetch', 'origin', 'main')
            new_base = git(seed, 'rev-parse', 'HEAD')
            git(lane, 'merge', '--no-ff', '-qm', 'merge base', new_base)
            new_tip = git(lane, 'rev-parse', 'HEAD')
            args = ['stack', 'set', 'feature', '--base', new_base, '--adopt-tip', new_tip]
            plan = json.loads(sw(repo, *args).stdout)
            sw(repo, *args, '--plan-digest', plan['planDigest'], '--apply')
            command = ['coordination', 'compose', '--stack', 'feature', '--known-base-state', published,
                       '--known-base-manifest-digest', state['manifest_digest']]
            result = sw(repo, *command)
            advance = json.loads(result.stdout)
            self.assertEqual(advance['baseAdvance']['oldTip'], old_tip)
            self.assertEqual(advance['baseAdvance']['newTip'], new_tip)
            self.assertEqual(advance['baseAdvance']['newBase'], new_base)
            if mode == 'race':
                (seed / 'base3.txt').write_text('race\n')
                git(seed, 'add', 'base3.txt')
                git(seed, 'commit', '-qm', 'race base advance')
                raced_base = git(seed, 'rev-parse', 'HEAD')
                bindir = root / 'bin'
                bindir.mkdir()
                wrapper = bindir / 'git'
                real_git = shutil.which('git')
                wrapper.write_text('#!' + sys.executable + '\nimport os, subprocess, sys\n'
                    'a=sys.argv[1:]\n'
                    'if "push" in a and "--atomic" in a and "--dry-run" not in a:\n'
                    ' r=subprocess.run([' + repr(real_git) + ',"-C",' + repr(str(seed)) +
                    ',"push","origin","main"])\n'
                    ' if r.returncode: sys.exit(r.returncode)\n'
                    'sys.exit(subprocess.call([' + repr(real_git) + ',*a]))\n')
                wrapper.chmod(0o700)
                planfile.write_text(json.dumps(advance))
                race_env = dict(env, PATH=str(bindir) + os.pathsep + env['PATH'])
                applied = subprocess.run([sys.executable, str(CLI), 'coordination', 'compose',
                    '--apply', '--plan-file', str(planfile)], cwd=repo, env=race_env,
                    text=True, capture_output=True, timeout=45)
                self.assertEqual(applied.returncode, 0, applied.stderr)
                qualification = json.loads(applied.stdout[applied.stdout.find('{'):])['base_qualification']
                self.assertEqual(qualification['status'], 'unqualified')
                self.assertEqual(qualification['expectedTargetTip'], new_base)
                self.assertEqual(qualification['observedTargetTip'], raced_base)
                ledger = repo / '.syncwheel' / 'ledger' / 'events' / '000001.jsonl'
                events = [json.loads(line) for line in ledger.read_text().splitlines()]
                receipt = next(event['payload']['context']['base_qualification']
                               for event in reversed(events)
                               if event.get('type') == 'manifest_saved'
                               and event.get('payload', {}).get('reason') == 'coordination_compose_stack'
                               and 'base_qualification' in event['payload'].get('context', {}))
                self.assertEqual(receipt, qualification)
                self.assertEqual(git(repo, 'ls-remote', 'origin', 'refs/heads/main').split()[0], raced_base)
                self.assertEqual(git(repo, 'ls-remote', 'origin', 'refs/heads/pr/feature').split()[0], new_tip)
                self.assertNotEqual(new_base, raced_base)
                return
            if mode in ('apply', 'linear'):
                def refs():
                    out = run(['git', '--git-dir', str(remote), 'for-each-ref',
                               '--format=%(refname) %(objectname)']).stdout.splitlines()
                    return dict(line.split(' ', 1) for line in out)
                before = refs()
                planfile.write_text(json.dumps(advance))
                sw(repo, 'coordination', 'compose', '--apply', '--plan-file', str(planfile))
                after = refs()
                changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
                self.assertEqual(changed, {state_ref, 'refs/heads/pr/feature',
                    'refs/heads/syncwheel/claim/heads/pr/feature'})
                self.assertEqual(after['refs/heads/pr/feature'], new_tip)
                self.assertEqual(after['refs/heads/main'], new_base)
                final_state = json.loads(run(['git', '--git-dir', str(remote), 'show',
                    after[state_ref] + ':.syncwheel/coordination-state.json']).stdout)
                foreign_before = next(x for x in state['manifest']['stacks'] if x['id'] == 'foreign')
                foreign_after = next(x for x in final_state['manifest']['stacks'] if x['id'] == 'foreign')
                self.assertEqual(foreign_after, foreign_before)
                self.assertEqual(after['refs/heads/pr/foreign'], foreign_tip)
                return
            (seed / 'base3.txt').write_text('drift\n')
            git(seed, 'add', 'base3.txt')
            git(seed, 'commit', '-qm', 'advance again')
            git(seed, 'push', 'origin', 'main')
            refused = sw(repo, *command, check=False)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('observed advanced target tip', refused.stderr)


if __name__ == '__main__':
    unittest.main()
