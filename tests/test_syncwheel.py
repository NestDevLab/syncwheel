import contextlib
import importlib.util
import io
import json
import os
import shlex
import signal
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / 'scripts' / 'syncwheel.py'
FIXTURE = REPO_ROOT / 'tests' / 'fixtures' / 'simple-repo'


class SyncwheelFixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='syncwheel-test-'))
        self.repo = self.tmp / 'repo'
        self.registry = self.tmp / 'repos.json'
        shutil.copytree(FIXTURE, self.repo)
        self.init_fixture_repo()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args, expected=0, extra_env=None, cwd=None):
        env = dict(**os.environ)
        env['SYNCWHEEL_REPO_REGISTRY'] = str(self.registry)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            ['python3', str(CLI), *args],
            cwd=cwd or self.repo,
            text=True,
            capture_output=True,
            env=env,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"expected exit {expected}, got {result.returncode}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
        return result

    def run_custom_cli(self, cli_path, *args, expected=0, extra_env=None, cwd=None):
        env = dict(**os.environ)
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            ['python3', str(cli_path), *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            env=env,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"expected exit {expected}, got {result.returncode}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
        return result

    def run_cli_until_cleanup_sigkill(self, stage, *args):
        script = r'''
import importlib.util
import json
import os
import signal
import sys

cli_path, repo_path, checkpoint, raw_args = sys.argv[1:]
spec = importlib.util.spec_from_file_location('syncwheel_sigkill_test', cli_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def kill_at(stage):
    if stage == checkpoint:
        os.kill(os.getpid(), signal.SIGKILL)

module.governed_worktree_cleanup_checkpoint = kill_at
os.chdir(repo_path)
sys.argv = [cli_path, *json.loads(raw_args)]
raise SystemExit(module.main())
'''
        env = dict(os.environ)
        env['SYNCWHEEL_REPO_REGISTRY'] = str(self.registry)
        result = subprocess.run(
            [
                'python3', '-c', script, str(CLI), str(self.repo), stage,
                json.dumps(list(args)),
            ],
            cwd=self.repo,
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(
            result.returncode,
            -signal.SIGKILL,
            f'checkpoint {stage!r} was not reached\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}',
        )
        return result

    def run_cli_until_registry_lock_sigkill(self, lock_state, *args):
        script = r'''
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path

cli_path, repo_path, lock_state, raw_args = sys.argv[1:]
spec = importlib.util.spec_from_file_location('syncwheel_registry_lock_sigkill_test', cli_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lock_path = module.governed_worktree_lock_path(Path(repo_path)).resolve(strict=False)

if lock_state == 'empty':
    original_open = module.os.open

    def kill_after_exclusive_create(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            descriptor = original_open(path, flags, mode)
        else:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if (
            flags & os.O_EXCL
            and Path(path).resolve(strict=False) == lock_path
        ):
            os.kill(os.getpid(), signal.SIGKILL)
        return descriptor

    module.os.open = kill_after_exclusive_create
elif lock_state == 'truncated':
    original_write_all = module._write_all

    def kill_during_metadata_write(descriptor, payload):
        descriptor_path = Path(f'/proc/self/fd/{descriptor}').resolve(strict=False)
        if descriptor_path == lock_path:
            os.write(descriptor, payload[:max(1, len(payload) // 2)])
            os.fsync(descriptor)
            os.kill(os.getpid(), signal.SIGKILL)
        return original_write_all(descriptor, payload)

    module._write_all = kill_during_metadata_write
else:
    raise AssertionError(lock_state)

os.chdir(repo_path)
sys.argv = [cli_path, *json.loads(raw_args)]
raise SystemExit(module.main())
'''
        env = dict(os.environ)
        env['SYNCWHEEL_REPO_REGISTRY'] = str(self.registry)
        result = subprocess.run(
            [
                'python3', '-c', script, str(CLI), str(self.repo), lock_state,
                json.dumps(list(args)),
            ],
            cwd=self.repo,
            text=True,
            capture_output=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(
            result.returncode,
            -signal.SIGKILL,
            f'lock state {lock_state!r} was not reached\n'
            f'STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}',
        )
        return result

    def start_registry_lock_holder(self, ready_path):
        script = r'''
import importlib.util
import os
import signal
import sys
from pathlib import Path

cli_path, repo_path, ready_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location('syncwheel_registry_lock_holder_test', cli_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with module.governed_worktree_registry_lock(Path(repo_path)):
    Path(ready_path).write_text(str(os.getpid()), encoding='utf-8')
    signal.pause()
'''
        env = dict(os.environ)
        env['SYNCWHEEL_REPO_REGISTRY'] = str(self.registry)
        holder = subprocess.Popen(
            ['python3', '-c', script, str(CLI), str(self.repo), str(ready_path)],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        deadline = time.monotonic() + 10
        while not ready_path.exists():
            if time.monotonic() >= deadline:
                stdout, stderr = holder.communicate(timeout=5)
                self.fail(
                    f'registry lock holder did not become ready\n'
                    f'STDOUT:\n{stdout}\nSTDERR:\n{stderr}'
                )
            time.sleep(0.01)
        return holder

    def run_cli_pair_concurrently(self, first, second):
        env = dict(os.environ)
        env['SYNCWHEEL_REPO_REGISTRY'] = str(self.registry)
        processes = [
            subprocess.Popen(
                ['python3', str(CLI), *args],
                cwd=self.repo,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            for args in (first, second)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            results.append(SimpleNamespace(
                returncode=process.returncode, stdout=stdout, stderr=stderr,
            ))
        return results

    def lane_release_reason_recorded(self, module, lane_id, reason):
        for event in module.load_ledger_events(self.repo):
            if event['type'] not in {
                'governed_worktree_released', 'governed_worktree_release_noted',
            }:
                continue
            payload = event.get('payload') or {}
            if payload.get('lane') == lane_id and payload.get('reason') == reason:
                return True
        return False

    def start_registry_lock_race(self, label, trace_path, hold, ready_path=None):
        script = r'''
import importlib.util
import os
import signal
import sys
import time
from pathlib import Path

cli_path, repo_path, label, trace_path, hold, ready_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location('syncwheel_registry_lock_race_test', cli_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lock_path = module.governed_worktree_lock_path(Path(repo_path)).resolve(strict=False)
original_open = module.os.open


def trace(mark):
    descriptor = original_open(trace_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, (mark + '\n').encode('utf-8'))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if ready_path:
    stopped = []

    def stop_after_exclusive_create(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            descriptor = original_open(path, flags, mode)
        else:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if (
            not stopped
            and flags & os.O_EXCL
            and Path(path).resolve(strict=False) == lock_path
        ):
            stopped.append(True)
            Path(ready_path).write_text(str(os.getpid()), encoding='utf-8')
            os.kill(os.getpid(), signal.SIGSTOP)
        return descriptor

    module.os.open = stop_after_exclusive_create

with module.governed_worktree_registry_lock(Path(repo_path)):
    trace(label + '-enter')
    time.sleep(float(hold))
    trace(label + '-exit')
'''
        env = dict(os.environ)
        env['SYNCWHEEL_REPO_REGISTRY'] = str(self.registry)
        return subprocess.Popen(
            [
                'python3', '-c', script, str(CLI), str(self.repo), label,
                str(trace_path), str(hold), str(ready_path or ''),
            ],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

    def await_condition(self, predicate, message, timeout=20):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail(message)
            time.sleep(0.01)

    def process_state(self, pid):
        try:
            raw = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
        except FileNotFoundError:
            return None
        closing = raw.rfind(')')
        fields = raw[closing + 2:].split() if closing >= 0 else []
        return fields[0] if fields else None

    def run_script(self, script_path, *args, expected=0, cwd=None):
        result = subprocess.run(
            ['python3', str(script_path), *args],
            cwd=cwd or self.repo,
            text=True,
            capture_output=True,
        )
        if result.returncode != expected:
            raise AssertionError(
                f"expected exit {expected}, got {result.returncode}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
        return result

    def load_syncwheel_module(self):
        spec = importlib.util.spec_from_file_location('syncwheel_under_test', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def git(self, *args):
        result = subprocess.run(
            ['git', *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"git command failed: {args}\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        return result.stdout.strip()

    def read_manifest(self):
        return json.loads((self.repo / '.syncwheel' / 'manifest.json').read_text())

    def test_manifest_stack_state_defaults_to_published(self):
        module = self.load_syncwheel_module()

        manifest, _ = module.load_manifest(self.repo)

        self.assertEqual(manifest['stacks'][0]['state'], 'published')
        self.assertEqual(manifest['stacks'][0]['publication'], {'enabled': True})

    def test_requested_replay_mode_allows_reconcile_arguments_without_in_place(self):
        module = self.load_syncwheel_module()

        self.assertEqual(
            module.requested_replay_mode(SimpleNamespace(replay_mode='plumbing', worktree=None)),
            'plumbing',
        )

    def test_validate_manifest_rejects_an_unknown_stack_state(self):
        module = self.load_syncwheel_module()
        manifest, _ = module.load_manifest(self.repo)
        manifest['stacks'][0]['state'] = 'reviewing'

        validation = module.validate_manifest(self.repo, manifest)

        self.assertIn(
            'stack feature-a state must be one of: draft, published',
            validation['errors'],
        )

    def read_ledger_state(self):
        result = self.run_cli('ledger', 'show', '--json', expected=0)
        return json.loads(result.stdout)

    def repo_exclude_path(self):
        path = Path(self.git('rev-parse', '--git-path', 'info/exclude'))
        if not path.is_absolute():
            path = self.repo / path
        return path

    def expected_external_ledger_root(self, manifest_path):
        stem = manifest_path.stem
        if stem.endswith('-manifest'):
            trimmed = stem[:-len('-manifest')]
            stem = trimmed or stem
        return manifest_path.parent / f'{stem}-ledger'

    def assert_path_equal(self, left, right):
        self.assertEqual(Path(left).resolve(), Path(right).resolve())

    def exercise_external_manifest_lane_capture(self, operation):
        manifest_path = self.tmp / f'{operation}-manifest.json'
        manifest = self.read_manifest()
        if operation == 'capture':
            stack_id = 'external-capture-stack'
            integration_branch = 'integration/external-capture'
            manifest['defaults']['integration_membership'] = 'required'
            manifest['integration'] = {
                'branch': integration_branch,
                'base': 'main',
                'stacks': [],
            }
            manifest['stacks'] = []
            self.git('branch', integration_branch, 'main')
            self.git('switch', '-q', integration_branch)
        elif operation == 'create':
            stack_id = 'external-created-stack'
        else:
            stack_id = 'feature-a'
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + '\n',
            encoding='utf-8',
        )
        if operation == 'capture':
            self.run_cli(
                'stack', 'create', stack_id, '--draft',
                '--manifest', str(manifest_path),
            )
        lane_id = f'external-{operation}'
        target = None if operation == 'create' else stack_id
        open_args = [
            'worktree', 'open', lane_id,
            '--manifest', str(manifest_path),
            '--json',
        ]
        if target:
            open_args.extend(['--into', target])
        opened = json.loads(self.run_cli(*open_args).stdout)
        original_generation = opened['lane']['generation_token']
        lane_path = Path(opened['lane']['path'])
        filename = f'{operation}-owned.txt'
        (lane_path / filename).write_text(
            f'{operation} owns this lane\n',
            encoding='utf-8',
        )
        subprocess.run(['git', 'add', filename], cwd=lane_path, check=True)
        subprocess.run(
            ['git', 'commit', '-qm', f'feat: exercise external {operation} capture'],
            cwd=lane_path,
            check=True,
        )
        commit = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=lane_path,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

        if operation == 'create':
            self.run_cli(
                'stack', 'create', stack_id, commit,
                '--branch', 'pr/external-created-stack',
                '--manifest', str(manifest_path),
            )
        elif operation == 'add':
            self.run_cli(
                'stack', 'add', stack_id, commit,
                '--manifest', str(manifest_path),
            )
        elif operation == 'capture':
            self.run_cli(
                'stack', 'capture-integration', stack_id, commit,
                '--manifest', str(manifest_path),
            )
        else:
            self.fail(f'unknown capture operation: {operation}')

        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        captured = next(item for item in registry['lanes'] if item['id'] == lane_id)
        self.assertEqual(captured['state'], 'reaped')
        self.assertEqual(captured['pending_reason'], 'ledger_pending')
        self.assertFalse(lane_path.exists())
        default_cleanup = [
            event for event in module.governed_worktree_cleanup_ledger(self.repo)['events']
            if event['type'].startswith('governed_worktree_')
        ]
        self.assertEqual(default_cleanup, [])
        external_cleanup = [
            event for event in module.load_ledger_events(self.repo, manifest_path)
            if event['type'].startswith('governed_worktree_')
        ]
        self.assertEqual(
            [event['type'] for event in external_cleanup],
            ['governed_worktree_cleanup_intent'],
        )

        self.run_cli(
            'gc', '--apply', '--no-fetch', '--json',
            '--manifest', str(manifest_path),
        )

        external_cleanup = [
            event for event in module.load_ledger_events(self.repo, manifest_path)
            if event['type'].startswith('governed_worktree_')
        ]
        self.assertEqual(
            [event['type'] for event in external_cleanup],
            ['governed_worktree_cleanup_intent', 'governed_worktree_reaped'],
        )
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

        reopen_args = [
            'worktree', 'open', lane_id,
            '--manifest', str(manifest_path),
            '--json', '--into', stack_id,
        ]
        reopened = json.loads(self.run_cli(*reopen_args).stdout)
        self.assertNotEqual(reopened['lane']['generation_token'], original_generation)
        shared_retry = json.loads(self.run_cli(
            'gc', '--apply', '--no-fetch', '--json'
        ).stdout)
        self.assertEqual(shared_retry['governed_worktree_failures'], [])
        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(len(registry['lanes']), 1)
        self.assertEqual(registry['lanes'][0]['id'], lane_id)
        self.assertEqual(
            registry['lanes'][0]['generation_token'],
            reopened['lane']['generation_token'],
        )

    def tracked_status(self):
        return self.git('status', '--porcelain', '--untracked-files=no')

    def init_fixture_repo(self):
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Syncwheel Fixture')
        self.git('config', 'user.email', 'syncwheel@example.com')
        self.git('add', 'alpha.txt')
        self.git('commit', '-q', '-m', 'feat: add alpha')
        alpha_sha = self.git('rev-parse', '--short=7', 'HEAD')
        self.git('add', 'beta.txt')
        self.git('commit', '-q', '-m', 'feat: add beta')
        beta_sha = self.git('rev-parse', '--short=7', 'HEAD')
        self.git('branch', 'pr/feature-a', 'HEAD~1')
        self.git('branch', 'pr/feature-b', 'HEAD')
        manifest = {
            'version': 1,
            'defaults': {
                'canonical_remote': 'origin',
                'publication_remote': 'fork',
                'base_branch': 'main',
                'base_ref': 'main',
            },
            'integration': {
                'branch': 'main',
                'base': 'main',
                'stacks': ['feature-a', 'feature-b'],
            },
            'stacks': [
                {
                    'id': 'feature-a',
                    'branch': 'pr/feature-a',
                    'base': 'main',
                    'target_remote': 'origin',
                    'target_branch': 'main',
                    'integration_branch': 'main',
                    'commits': [alpha_sha],
                },
                {
                    'id': 'feature-b',
                    'branch': 'pr/feature-b',
                    'base': 'main',
                    'target_remote': 'origin',
                    'target_branch': 'main',
                    'integration_branch': 'main',
                    'commits': [alpha_sha, beta_sha],
                },
            ],
        }
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

    def prepare_replay_stack(self):
        base = self.git('rev-parse', 'main~1')
        beta = self.git('rev-parse', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'main')
        data = self.read_manifest()
        data['stacks'] = [{
            'id': 'replay',
            'branch': 'pr/replay',
            'base': base,
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'main',
            'commits': [beta, gamma],
        }]
        data['integration']['stacks'] = ['replay']
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        return base, [beta, gamma]

    def init_syncwheel_install_fixture(self):
        seed = self.tmp / 'syncwheel-seed'
        origin = self.tmp / 'syncwheel-origin.git'
        install = self.tmp / 'syncwheel-install'
        (seed / 'scripts').mkdir(parents=True)
        (seed / 'githooks').mkdir(parents=True)
        shutil.copy2(CLI, seed / 'scripts' / 'syncwheel.py')
        shutil.copy2(CLI.parent / 'check-version-bump.py', seed / 'scripts' / 'check-version-bump.py')
        shutil.copy2(REPO_ROOT / 'githooks' / 'pre-commit', seed / 'githooks' / 'pre-commit')
        (seed / 'VERSION').write_text('0.6.0\n')
        (seed / 'README.md').write_text('syncwheel fixture\n')

        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=seed, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Syncwheel Fixture'], cwd=seed, check=True)
        subprocess.run(['git', 'config', 'user.email', 'syncwheel@example.com'], cwd=seed, check=True)
        subprocess.run(['git', 'add', '.'], cwd=seed, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', 'syncwheel 0.6.0'], cwd=seed, check=True)
        subprocess.run(['git', 'clone', '--bare', str(seed), str(origin)], check=True)
        subprocess.run(['git', 'remote', 'add', 'origin', str(origin)], cwd=seed, check=True)
        subprocess.run(['git', 'push', '-u', 'origin', 'main'], cwd=seed, check=True)
        subprocess.run(['git', 'clone', str(origin), str(install)], check=True)

        (seed / 'VERSION').write_text('0.7.0\n')
        subprocess.run(['git', 'add', 'VERSION'], cwd=seed, check=True)
        subprocess.run(['git', 'commit', '-q', '-m', 'syncwheel 0.7.0'], cwd=seed, check=True)
        subprocess.run(['git', 'push', 'origin', 'main'], cwd=seed, check=True)

        return {
            'seed': seed,
            'origin': origin,
            'install': install,
            'cli': install / 'scripts' / 'syncwheel.py',
            'state': self.tmp / 'syncwheel-update-state.json',
            'settings': self.tmp / 'syncwheel-settings.json',
            'registry': self.tmp / 'syncwheel-registry.json',
        }

    def test_validate_passes_for_fixture(self):
        result = self.run_cli('validate', expected=0)
        self.assertIn('OK', result.stdout)

    def test_derived_commit_is_classified_not_unmapped(self):
        base = self.git('rev-parse', 'HEAD')
        self.git('branch', 'derived-base', base)
        self.git('switch', '-q', '-c', 'derived-integration', 'derived-base')
        manifest = self.read_manifest()
        manifest['version'] = 3
        manifest['repository_mode'] = 'delivery'
        manifest['syncwheel_tracking'] = 'git-tracked'
        manifest['integration'].update(
            {
                'branch': 'derived-integration',
                'base': 'derived-base',
                'strategy': 'cherry-pick',
                'derived_paths': ['locks/'],
            }
        )
        manifest['coordination'] = {
            'mode': 'disabled',
            'id': 'derived-classification',
            'remote': manifest['defaults']['publication_remote'],
            'state_branch': 'syncwheel/state/derived-classification',
        }
        manifest.setdefault('channels', [])
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(manifest, indent=2) + '\n'
        )
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: configure derived projections')
        (self.repo / 'locks').mkdir()
        (self.repo / 'locks' / 'codex.lock').write_text('derived\n')
        self.git('add', 'locks/codex.lock')
        module = self.load_syncwheel_module()
        blob = self.git('rev-parse', ':locks/codex.lock')
        paths_digest = module.derived_projection_paths_digest(
            {'locks/codex.lock': blob}
        )
        self.git(
            'commit', '-q', '-m', 'test: derived projection', '-m',
            'Syncwheel-Derived-Projection: classified-derived\n'
            f'Syncwheel-Derived-Paths: {paths_digest}',
        )
        derived = self.git('rev-parse', 'HEAD')
        derived_record = {
            'operation_id': 'classified-derived',
            'commit': derived,
            'paths': ['locks/codex.lock'],
            'paths_digest': paths_digest,
            'composition_digest': module.integration_composition_digest(manifest),
        }
        module.record_common_derived_provenance(
            self.repo, manifest, derived_record
        )
        module.append_ledger_event(
            self.repo, 'revision_provider_derived_commit', derived_record
        )
        (self.repo / 'locks' / 'path-only.lock').write_text('not derived\n')
        self.git('add', 'locks/path-only.lock')
        self.git('commit', '-q', '-m', 'test: path-only lock commit')
        path_only = self.git('rev-parse', 'HEAD')
        (self.repo / 'locks' / 'digest-mismatch.lock').write_text('not derived\n')
        self.git('add', 'locks/digest-mismatch.lock')
        digest_mismatch_blob = self.git(
            'rev-parse', ':locks/digest-mismatch.lock'
        )
        digest_mismatch_paths_digest = module.derived_projection_paths_digest(
            {'locks/digest-mismatch.lock': digest_mismatch_blob}
        )
        self.git(
            'commit', '-q', '-m', 'test: mismatched derived digest', '-m',
            'Syncwheel-Derived-Projection: mismatched-derived\n'
            f"Syncwheel-Derived-Paths: {'0' * 64}",
        )
        digest_mismatch = self.git('rev-parse', 'HEAD')
        mismatched_record = {
            'operation_id': 'mismatched-derived',
            'commit': digest_mismatch,
            'paths': ['locks/digest-mismatch.lock'],
            'paths_digest': digest_mismatch_paths_digest,
            'composition_digest': module.integration_composition_digest(manifest),
        }
        module.record_common_derived_provenance(
            self.repo, manifest, mismatched_record
        )
        module.append_ledger_event(
            self.repo, 'revision_provider_derived_commit', mismatched_record
        )
        loaded, _ = module.load_manifest(self.repo)

        validation = module.validate_manifest(self.repo, loaded)

        self.assertEqual(validation['errors'], [])
        with self.subTest(classification='complete'):
            self.assertTrue(
                module.is_derived_projection_commit(self.repo, loaded, derived)
            )
        with self.subTest(classification='path-only'):
            self.assertFalse(
                module.is_derived_projection_commit(self.repo, loaded, path_only)
            )
        with self.subTest(classification='content-bound'):
            self.assertFalse(
                module.is_derived_projection_commit(
                    self.repo, loaded, digest_mismatch
                )
            )
        self.assertEqual(
            validation['details']['integration']['derived_commits'], [derived]
        )
        self.assertEqual(
            validation['details']['integration']['unmapped_commits'],
            [path_only, digest_mismatch],
        )

    def test_commit_changed_files_preserves_newline_and_leading_space(self):
        paths = ['odd/line\nbreak.txt', ' leading.txt']
        (self.repo / 'odd').mkdir()
        for path in paths:
            (self.repo / path).write_text(path + '\n')
        self.git('add', '--', *paths)
        self.git('commit', '-q', '-m', 'test: exact Git path parsing')
        commit = self.git('rev-parse', 'HEAD')
        module = self.load_syncwheel_module()

        self.assertEqual(
            set(module.commit_changed_files(self.repo, commit)),
            set(paths),
        )

    def test_leading_space_path_cannot_be_stripped_into_a_derived_prefix(self):
        module = self.load_syncwheel_module()
        manifest = self.read_manifest()
        manifest['version'] = 3
        manifest['repository_mode'] = 'delivery'
        manifest['syncwheel_tracking'] = 'git-tracked'
        manifest['integration'].update(
            {
                'branch': 'main',
                'base': 'main^',
                'strategy': 'cherry-pick',
                'derived_paths': ['locks/'],
            }
        )
        manifest['coordination'] = {
            'mode': 'disabled',
            'id': 'leading-space-classification',
            'remote': manifest['defaults']['publication_remote'],
            'state_branch': 'syncwheel/state/leading-space-classification',
        }
        manifest.setdefault('channels', [])
        (self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps(manifest, indent=2) + '\n'
        )
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: configure derived prefix')
        actual_path = ' locks/leading.lock'
        transformed_path = 'locks/leading.lock'
        (self.repo / ' locks').mkdir()
        (self.repo / actual_path).write_text('derived\n')
        self.git('add', '--', actual_path)
        malicious_digest = module.derived_projection_paths_digest(
            {transformed_path: None}
        )
        self.git(
            'commit',
            '-q',
            '-m',
            'test: leading-space path',
            '-m',
            'Syncwheel-Derived-Projection: leading-space\n'
            f'Syncwheel-Derived-Paths: {malicious_digest}',
        )
        commit = self.git('rev-parse', 'HEAD')
        misleading_record = {
            'operation_id': 'leading-space',
            'commit': commit,
            'paths': [transformed_path],
            'paths_digest': malicious_digest,
            'composition_digest': module.integration_composition_digest(manifest),
        }
        module.record_common_derived_provenance(
            self.repo, manifest, misleading_record
        )
        module.append_ledger_event(
            self.repo, 'revision_provider_derived_commit', misleading_record
        )
        loaded, _ = module.load_manifest(self.repo)

        self.assertFalse(
            module.is_derived_projection_commit(self.repo, loaded, commit)
        )
        self.assertIn(
            commit,
            module.validate_manifest(self.repo, loaded)['details']['integration'][
                'unmapped_commits'
            ],
        )

    def test_plan_reports_no_actions_when_fixture_is_aligned(self):
        result = self.run_cli('plan', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertEqual(data, [])

    def test_replayed_patch_equivalent_commits_satisfy_stack_and_integration_projection(self):
        self.git('switch', '-q', '-c', 'source/original', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        original = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', 'main')
        Path(self.repo / 'delta.txt').write_text('delta\n')
        self.git('add', 'delta.txt')
        self.git('commit', '-q', '-m', 'feat: advance base')
        self.git('switch', '-q', '-c', 'pr/replayed')
        self.git('cherry-pick', original)
        replayed = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', '-c', 'integration/replayed', 'main')
        self.git('merge', '--no-ff', 'pr/replayed', '-m', "Merge stack 'replayed' into integration/replayed")

        data = self.read_manifest()
        data['integration'] = {
            'branch': 'integration/replayed',
            'base': 'main',
            'strategy': 'merge-stacks',
            'stacks': ['replayed'],
        }
        data['stacks'] = [{
            'id': 'replayed',
            'branch': 'pr/replayed',
            'base': 'main',
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'integration/replayed',
            'commits': [original],
        }]
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')

        module = self.load_syncwheel_module()
        manifest, _ = module.load_manifest(self.repo)
        self.assertEqual(module.commit_patch_id(self.repo, original), module.commit_patch_id(self.repo, replayed))
        validation = module.validate_manifest(self.repo, manifest)
        stack = validation['details']['stacks'][0]

        self.assertEqual(validation['errors'], [])
        self.assertEqual(stack['missing_from_branch'], [])
        self.assertEqual(stack['missing_from_integration'], [])
        self.assertEqual(stack['undeclared_branch_commits'], [])
        self.assertEqual(validation['details']['integration']['unmapped_commits'], [])
        self.assertEqual(module.build_plan(self.repo, manifest, validation), [])

    def test_status_json_reports_manifest_present(self):
        result = self.run_cli('status', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertTrue(data['manifest_present'])
        self.assertIn('validation', data)
        self.assertEqual(data['validation']['errors'], [])

    def test_check_json_reports_validation_and_plan(self):
        result = self.run_cli('check', '--no-fetch', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assertIn('validation', data)
        self.assertEqual(data['validation']['errors'], [])
        self.assertEqual(data['plan'], [])

    def test_check_strict_fails_when_branch_contains_undeclared_commits(self):
        self.git('switch', '-q', 'pr/feature-a')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add undeclared gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')

        result = self.run_cli('check', '--strict', '--no-fetch', '--json', expected=1)
        data = json.loads(result.stdout)
        stack = next(item for item in data['validation']['details']['stacks'] if item['id'] == 'feature-a')

        self.assertEqual(stack['undeclared_branch_commits'], [gamma])
        self.assertFalse(data['readiness']['ready'])
        self.assertIn('validation_warnings', data['readiness']['blockers'])

        result = self.run_cli('check', '--no-fetch', '--json', expected=0)
        self.assertFalse(json.loads(result.stdout)['readiness']['ready'])

    def test_check_strict_passes_only_when_validation_and_plan_are_clean(self):
        result = self.run_cli('check', '--strict', '--no-fetch', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assertTrue(data['readiness']['ready'])
        self.assertEqual(data['readiness']['blockers'], [])

    def test_check_strict_detects_undeclared_remote_only_stack_commits(self):
        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(origin)], check=True)
        self.git('remote', 'add', 'fork', str(origin))
        self.git('push', '-q', 'fork', 'main', 'pr/feature-a', 'pr/feature-b')

        self.git('switch', '-q', 'pr/feature-a')
        Path(self.repo / 'gamma.txt').write_text('remote gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add remote-only gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('push', '-q', 'fork', 'pr/feature-a')
        self.git('switch', '-q', 'main')
        self.git('branch', '-f', 'pr/feature-a', 'main~1')

        result = self.run_cli('check', '--strict', '--json', expected=1)
        data = json.loads(result.stdout)
        stack = next(item for item in data['validation']['details']['stacks'] if item['id'] == 'feature-a')

        self.assertEqual(stack['remote_relation'], 'local_behind')
        self.assertEqual(stack['undeclared_remote_commits'], [gamma])
        self.assertIn('validation_warnings', data['readiness']['blockers'])

    def test_primary_checkout_mismatch_blocks_read_only_handoff_commands(self):
        self.git('switch', '-c', 'feature/wrong-primary')

        for command in (
            ('status', '--json'),
            ('validate', '--json'),
            ('plan', '--json'),
            ('check', '--no-fetch', '--json'),
            ('handoff', '--no-fetch', '--json'),
        ):
            result = self.run_cli(*command, expected=1)
            data = json.loads(result.stdout)
            if isinstance(data, list):
                self.assertTrue(any(action['type'] == 'restore_primary_checkout' for action in data))
            else:
                validation = data.get('validation', data)
                self.assertTrue(any('primary worktree branch mismatch' in error for error in validation['errors']))

    def test_feature_worktree_is_allowed_when_primary_checkout_matches(self):
        feature = self.tmp / 'feature-worktree'
        self.git('worktree', 'add', '-q', '-b', 'feature/dedicated', str(feature), 'main')

        result = self.run_cli('status', '--json', cwd=feature)
        data = json.loads(result.stdout)

        self.assertEqual(data['snapshot']['current_branch'], 'feature/dedicated')
        self.assertEqual(data['snapshot']['primary_checkout']['branch'], 'main')
        self.assertTrue(data['snapshot']['primary_checkout']['compliant'])

    def test_worktree_open_registers_a_light_lane_under_the_configured_root(self):
        result = self.run_cli('worktree', 'open', 'quick-fix', '--json')
        data = json.loads(result.stdout)
        lane = data['lane']

        self.assertEqual(lane['id'], 'quick-fix')
        self.assertFalse(lane['full'])
        self.assertEqual(lane['branch'], 'syncwheel/lane/quick-fix')
        self.assertTrue(Path(lane['path']).is_dir())
        self.assertTrue(Path(lane['path']).is_relative_to(self.repo / '.syncwheel' / 'wt'))
        registry = json.loads(Path(data['registry_path']).read_text())
        self.assertEqual(registry['version'], 1)
        self.assertEqual(registry['lanes'], [lane])

    def test_worktree_open_enforces_capacity_without_creating_a_fifth_lane(self):
        for number in range(4):
            self.run_cli('worktree', 'open', f'lane-{number}', '--json')

        result = self.run_cli('worktree', 'open', 'lane-4', '--json', expected=2)

        self.assertIn('capacity reached (4)', result.stderr)
        base = self.git('rev-parse', 'main')
        self.assertIn(
            f'syncwheel stack add feature-a {base}..syncwheel/lane/lane-0', result.stderr
        )
        self.assertFalse((self.repo / '.syncwheel' / 'wt' / 'syncwheel-lane-lane-4').exists())

    def test_expired_clean_lane_is_reaped_with_a_recovery_ref_before_next_open(self):
        opened = json.loads(self.run_cli(
            'worktree', 'open', 'expired', '--into', 'feature-a', '--json'
        ).stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        status = json.loads(self.run_cli('status', '--json').stdout)
        expired_status = status['governed_worktrees']['lanes'][0]
        self.assertEqual(expired_status['code'], 'expired')
        self.assertIn(
            f"syncwheel stack add feature-a {opened['lane']['base']}..syncwheel/lane/expired",
            expired_status['remedy'],
        )

        self.run_cli('worktree', 'open', 'next', '--json')

        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertFalse(any(item['id'] == 'expired' for item in registry['lanes']))
        self.assertFalse(Path(opened['lane']['path']).exists())
        branch = subprocess.run(
            ['git', 'show-ref', '--verify', '--quiet', 'refs/heads/syncwheel/lane/expired'],
            cwd=self.repo,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(branch.returncode, 0)

    def test_expired_committed_lane_is_reaped_only_after_anchoring_its_tip(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'expired-commit', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'saved.txt').write_text('recover this commit\n')
        subprocess.run(['git', 'add', 'saved.txt'], cwd=lane_path, check=True)
        subprocess.run(['git', 'commit', '-qm', 'feat: recoverable lane'], cwd=lane_path, check=True)
        tip = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=lane_path, text=True, capture_output=True, check=True
        ).stdout.strip()
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        self.run_cli('worktree', 'open', 'after-expiry', '--json')

        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertFalse(any(item['id'] == 'expired-commit' for item in registry['lanes']))
        event = next(event for event in self.read_ledger_state()['recent_events'] if event['type'] == 'governed_worktree_reaped')
        self.assertEqual(self.git('rev-parse', event['payload']['recovery_ref']), tip)
        self.assertFalse(lane_path.exists())

    def test_dry_run_does_not_reap_an_expired_lane(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'dry-run-expired', '--json').stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        self.run_cli('stack', 'rebuild', 'feature-a', '--dry-run')

        self.assertTrue(Path(opened['lane']['path']).is_dir())
        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'][0]['state'], 'active')

    def test_reconcile_preview_does_not_reap_an_expired_lane(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'reconcile-preview', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        self.git('worktree', 'remove', str(lane_path))
        module = self.load_syncwheel_module()
        registry, registry_path = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        registry_before = registry_path.read_bytes()
        branch_tip = module.ref_tip(self.repo, opened['lane']['branch'])

        preview = json.loads(self.run_cli('reconcile', '--no-fetch', '--json').stdout)

        self.assertFalse(preview['applied'])
        self.assertEqual(registry_path.read_bytes(), registry_before)
        self.assertEqual(module.ref_tip(self.repo, opened['lane']['branch']), branch_tip)
        self.assertEqual(self.read_ledger_state()['recent_events'], [])

    def test_stack_git_auto_worktree_is_an_explicit_reaping_mutation(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'before-stack-git', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        self.run_cli('stack', 'git', 'feature-a', '--auto-worktree', '--', 'status', '--short')

        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertFalse(any(lane['id'] == 'before-stack-git' for lane in registry['lanes']))
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
        events = self.read_ledger_state()['recent_events']
        self.assertEqual(
            [event['type'] for event in events],
            ['governed_worktree_cleanup_intent', 'governed_worktree_reaped'],
        )

    def test_reaping_gate_uses_apply_and_explicit_worktree_creation(self):
        module = self.load_syncwheel_module()
        for command in (
            module.command_reconcile,
            module.command_resume,
            module.command_stack_classify_integration,
            module.command_stack_land,
        ):
            with self.subTest(command=command.__name__, apply=False):
                self.assertFalse(module.governed_worktree_reaping_requested(
                    SimpleNamespace(func=command, apply=False)
                ))
            with self.subTest(command=command.__name__, apply=True):
                self.assertTrue(module.governed_worktree_reaping_requested(
                    SimpleNamespace(func=command, apply=True)
                ))
        for command in (module.command_stack_git, module.command_int_git):
            with self.subTest(command=command.__name__, existing=True):
                self.assertFalse(module.governed_worktree_reaping_requested(
                    SimpleNamespace(func=command, auto_worktree=False, worktree=None)
                ))
            with self.subTest(command=command.__name__, auto=True):
                self.assertTrue(module.governed_worktree_reaping_requested(
                    SimpleNamespace(func=command, auto_worktree=True, worktree=None)
                ))

    def test_expired_lane_can_be_reaped_through_repo_flag_outside_a_repository(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'outside-repo', '--json').stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        self.run_cli('worktree', 'open', 'next-outside', '--json', '-r', str(self.repo), cwd=self.tmp)

        self.assertFalse(Path(opened['lane']['path']).exists())

    def test_branch_advanced_pending_lane_blocks_a_mutating_rebuild(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'advanced', '--json').stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['state'] = 'captured_pending_cleanup'
        registry['lanes'][0]['pending_reason'] = 'branch_advanced'
        module.save_governed_worktree_registry(self.repo, registry)

        result = self.run_cli('stack', 'rebuild', 'feature-a', expected=2)

        self.assertIn('governed worktree recovery is required', result.stderr)
        self.assertTrue(Path(opened['lane']['path']).is_dir())

    def test_linked_worktree_uses_the_primary_configured_root(self):
        linked = self.tmp / 'linked'
        self.git('worktree', 'add', '-q', '-b', 'feature/linked', str(linked), 'main')

        opened = json.loads(self.run_cli(
            'worktree', 'open', 'linked-root', '--json', '-r', str(self.repo), cwd=linked
        ).stdout)
        status = json.loads(self.run_cli('status', '--json').stdout)
        lane = next(item for item in status['governed_worktrees']['lanes'] if item['id'] == 'linked-root')

        self.assertTrue(Path(opened['lane']['path']).is_relative_to(self.repo / '.syncwheel' / 'wt'))
        self.assertIsNone(lane['code'])

    def test_worktree_open_honours_a_declared_nondefault_root(self):
        manifest = self.read_manifest()
        manifest['syncwheel_worktree_root'] = 'var/syncwheel'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

        opened = json.loads(self.run_cli('worktree', 'open', 'declared-root', '--json').stdout)

        self.assertTrue(Path(opened['lane']['path']).is_relative_to(self.repo / 'var' / 'syncwheel'))

    def test_missing_expired_lane_outside_root_is_reaped_with_a_recovery_ref(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'missing-expired', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'saved.txt').write_text('recover this commit\n')
        subprocess.run(['git', 'add', 'saved.txt'], cwd=lane_path, check=True)
        subprocess.run(['git', 'commit', '-qm', 'feat: recover dead lane'], cwd=lane_path, check=True)
        tip = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=lane_path, text=True, capture_output=True, check=True
        ).stdout.strip()
        self.git('worktree', 'remove', str(lane_path))
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['path'] = str(self.tmp / 'outside-configured-root' / 'missing-expired')
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        status = json.loads(self.run_cli('status', '--json').stdout)
        reported = status['governed_worktrees']['lanes'][0]
        self.assertEqual(reported['code'], 'expired')
        self.run_cli('worktree', 'open', 'after-missing-expired', '--json')

        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertFalse(any(item['id'] == 'missing-expired' for item in registry['lanes']))
        event = next(event for event in self.read_ledger_state()['recent_events'] if event['type'] == 'governed_worktree_reaped')
        self.assertEqual(self.git('rev-parse', event['payload']['recovery_ref']), tip)
        self.assertIsNone(module.ref_tip(self.repo, 'syncwheel/lane/missing-expired'))

    def test_dead_owner_with_a_missing_lane_is_expired(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'dead-owner', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        self.git('worktree', 'remove', str(lane_path))
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['owner'] = f'agent@{module.socket.gethostname()}:999999999'
        module.save_governed_worktree_registry(self.repo, registry)

        status = json.loads(self.run_cli('status', '--json').stdout)

        self.assertEqual(status['governed_worktrees']['lanes'][0]['code'], 'expired')

    def test_worktree_release_removes_an_abandoned_clean_record_and_keeps_recovery(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'abandoned', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'saved.txt').write_text('recover this commit\n')
        subprocess.run(['git', 'add', 'saved.txt'], cwd=lane_path, check=True)
        subprocess.run(['git', 'commit', '-qm', 'feat: abandoned lane'], cwd=lane_path, check=True)
        tip = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=lane_path, text=True, capture_output=True, check=True
        ).stdout.strip()

        released = json.loads(self.run_cli(
            'worktree', 'release', 'abandoned', '--reason', 'superseded work', '--apply', '--json'
        ).stdout)

        self.assertEqual(released['lane']['id'], 'abandoned')
        self.assertEqual(released['reason'], 'superseded work')
        self.assertNotIn('pending_reason', released['lane'])
        self.assertEqual(self.git('rev-parse', released['lane']['recovery_ref']), tip)
        registry, _ = self.load_syncwheel_module().load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'], [])
        self.assertFalse(lane_path.exists())
        ledger = self.read_ledger_state()
        self.assertEqual(ledger['recent_events'][-1]['type'], 'governed_worktree_released')

    def test_worktree_release_accepts_a_clean_record_with_a_missing_path(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'missing-release', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'saved.txt').write_text('recover this release\n')
        subprocess.run(['git', 'add', 'saved.txt'], cwd=lane_path, check=True)
        subprocess.run(['git', 'commit', '-qm', 'feat: recover missing release'], cwd=lane_path, check=True)
        tip = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=lane_path, text=True, capture_output=True, check=True
        ).stdout.strip()
        self.git('worktree', 'remove', str(lane_path))

        status = json.loads(self.run_cli('status', '--json').stdout)
        self.assertEqual(status['governed_worktrees']['lanes'][0]['code'], 'unregistered_worktree')

        released = json.loads(self.run_cli(
            'worktree', 'release', 'missing-release',
            '--reason', 'worktree removed outside Syncwheel', '--apply', '--json'
        ).stdout)

        module = self.load_syncwheel_module()
        self.assertEqual(module.ref_tip(self.repo, released['lane']['recovery_ref']), tip)
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])
        self.assertNotIn(str(lane_path), self.git('worktree', 'list', '--porcelain'))
        events = self.read_ledger_state()['recent_events']
        self.assertEqual(
            [event['type'] for event in events],
            ['governed_worktree_cleanup_intent', 'governed_worktree_released'],
        )
        self.assertEqual(events[-1]['payload']['reason'], 'worktree removed outside Syncwheel')

    def test_worktree_release_is_a_dry_run_until_apply(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'release-preview', '--json').stdout)
        lane_path = Path(opened['lane']['path'])

        preview = json.loads(self.run_cli(
            'worktree', 'release', 'release-preview', '--reason', 'no longer needed', '--json'
        ).stdout)

        self.assertFalse(preview['applied'])
        self.assertTrue(lane_path.is_dir())
        registry, _ = self.load_syncwheel_module().load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'][0]['state'], 'active')
        self.assertEqual(self.read_ledger_state()['recent_events'], [])

    def test_worktree_release_retry_returns_the_terminal_after_lost_response_sigkill(self):
        self.run_cli('worktree', 'open', 'lost-response', '--json')
        reason = 'lost response'

        self.run_cli_until_cleanup_sigkill(
            'after_cleanup_record_removed',
            'worktree', 'release', 'lost-response',
            '--reason', reason, '--apply', '--json',
        )

        module = self.load_syncwheel_module()
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])
        terminal = next(
            event for event in module.load_ledger_events(self.repo)
            if event['type'] == 'governed_worktree_released'
        )
        retried = self.run_cli(
            'worktree', 'release', 'lost-response',
            '--reason', reason, '--apply', '--json',
        )
        output = json.loads(retried.stdout)

        self.assertTrue(output['applied'])
        self.assertTrue(output['idempotent'])
        self.assertEqual(output['terminal'], terminal)
        self.assertEqual(output['lane']['id'], 'lost-response')
        self.assertIn('recovered stale governed worktree registry lock', retried.stderr)

    def test_two_identical_worktree_releases_return_the_same_terminal(self):
        self.run_cli('worktree', 'open', 'release-twice', '--json')
        reason = 'same completed release'

        first = json.loads(self.run_cli(
            'worktree', 'release', 'release-twice',
            '--reason', reason, '--apply', '--json',
        ).stdout)
        terminal = next(
            event for event in self.load_syncwheel_module().load_ledger_events(self.repo)
            if event['type'] == 'governed_worktree_released'
        )
        second = json.loads(self.run_cli(
            'worktree', 'release', 'release-twice',
            '--reason', reason, '--apply', '--json',
        ).stdout)

        self.assertTrue(first['applied'])
        self.assertTrue(second['applied'])
        self.assertTrue(second['idempotent'])
        self.assertEqual(second['terminal'], terminal)
        self.assertEqual(
            second['terminal']['payload']['idempotency_key'],
            terminal['payload']['idempotency_key'],
        )

    def test_worktree_release_refuses_a_dirty_lane(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'dirty-release', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'draft.txt').write_text('keep me\n')

        result = self.run_cli(
            'worktree', 'release', 'dirty-release', '--reason', 'cannot discard', expected=2
        )

        self.assertIn('dirty', result.stderr)
        self.assertTrue(lane_path.is_dir())
        self.assertEqual((lane_path / 'draft.txt').read_text(), 'keep me\n')

    def test_gc_reaps_an_expired_missing_lane_without_active_active_coordination(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'gc-expired', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'saved.txt').write_text('recover this commit\n')
        subprocess.run(['git', 'add', 'saved.txt'], cwd=lane_path, check=True)
        subprocess.run(['git', 'commit', '-qm', 'feat: gc recovery lane'], cwd=lane_path, check=True)
        tip = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=lane_path, text=True, capture_output=True, check=True
        ).stdout.strip()
        self.git('worktree', 'remove', str(lane_path))
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        gc = json.loads(self.run_cli('gc', '--apply', '--no-fetch', '--json').stdout)

        self.assertFalse(gc['enabled'])
        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'], [])
        self.assertEqual(self.git('rev-parse', gc['governed_worktree_reaped'][0]['id'] and next(
            event['payload']['recovery_ref'] for event in self.read_ledger_state()['recent_events']
            if event['type'] == 'governed_worktree_reaped'
        )), tip)

    def test_dirty_lane_is_reported_but_not_reaped(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'dirty', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'draft.txt').write_text('keep me\n')
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        result = self.run_cli('status', '--json')
        status = json.loads(result.stdout)
        reported = status['governed_worktrees']['lanes'][0]

        self.assertEqual(reported['code'], 'dirty')
        self.assertNotIn('\x1b', result.stdout + result.stderr)
        self.assertTrue(lane_path.is_dir())
        self.assertEqual((lane_path / 'draft.txt').read_text(), 'keep me\n')
        strict = json.loads(self.run_cli('check', '--no-fetch', '--strict', '--json', expected=1).stdout)
        self.assertIn('governed_worktree_warnings', strict['readiness']['blockers'])

    def test_expired_existing_lane_outside_the_current_root_is_never_reaped(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'external-live', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'ignored.txt').write_text('must survive\n')
        manifest = self.read_manifest()
        manifest['syncwheel_worktree_root'] = 'var/syncwheel'
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        self.run_cli('worktree', 'open', 'another-lane', '--json')

        self.assertTrue((lane_path / 'ignored.txt').exists())
        status = json.loads(self.run_cli('status', '--json').stdout)
        self.assertEqual(status['governed_worktrees']['lanes'][0]['code'], 'outside_root')

    def test_moved_dirty_lane_is_resolved_by_branch_before_expiry_cleanup(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'moved-dirty', '--json').stdout)
        old_path = Path(opened['lane']['path'])
        moved_path = old_path.with_name('syncwheel-lane-moved-dirty-current')
        self.git('worktree', 'move', str(old_path), str(moved_path))
        (moved_path / 'draft.txt').write_text('must survive\n')
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        branch_tip = module.ref_tip(self.repo, opened['lane']['branch'])

        self.run_cli('worktree', 'lock', 'feature-a')

        registry, _ = module.load_governed_worktree_registry(self.repo)
        lane = next(item for item in registry['lanes'] if item['id'] == 'moved-dirty')
        self.assertEqual(Path(lane['path']).resolve(), moved_path.resolve())
        self.assertEqual(module.ref_tip(self.repo, opened['lane']['branch']), branch_tip)
        self.assertEqual((moved_path / 'draft.txt').read_text(), 'must survive\n')
        status = json.loads(self.run_cli('status', '--json').stdout)
        reported = next(item for item in status['governed_worktrees']['lanes'] if item['id'] == 'moved-dirty')
        self.assertEqual(reported['code'], 'dirty')

    def test_moved_locked_lane_is_resolved_by_branch_before_expiry_cleanup(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'moved-locked', '--json').stdout)
        old_path = Path(opened['lane']['path'])
        moved_path = old_path.with_name('syncwheel-lane-moved-locked-current')
        self.git('worktree', 'move', str(old_path), str(moved_path))
        self.git('worktree', 'lock', '--reason', 'still in use', str(moved_path))
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        branch_tip = module.ref_tip(self.repo, opened['lane']['branch'])

        self.run_cli('worktree', 'lock', 'feature-a')

        registry, _ = module.load_governed_worktree_registry(self.repo)
        lane = next(item for item in registry['lanes'] if item['id'] == 'moved-locked')
        self.assertEqual(Path(lane['path']).resolve(), moved_path.resolve())
        self.assertEqual(module.ref_tip(self.repo, opened['lane']['branch']), branch_tip)
        status = json.loads(self.run_cli('status', '--json').stdout)
        reported = next(item for item in status['governed_worktrees']['lanes'] if item['id'] == 'moved-locked')
        self.assertEqual(reported['code'], 'locked')

    def test_missing_default_owner_with_a_valid_lease_is_not_expired(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'default-owner', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])

        status = json.loads(self.run_cli('status', '--json').stdout)

        self.assertEqual(status['governed_worktrees']['lanes'][0]['code'], 'unregistered_worktree')

    def test_release_anchors_the_base_tip_too(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'base-anchor', '--json').stdout)
        released = json.loads(self.run_cli(
            'worktree', 'release', 'base-anchor', '--reason', 'finished', '--apply', '--json'
        ).stdout)
        self.assertEqual(self.git('rev-parse', released['lane']['recovery_ref']), opened['lane']['base'])

    def test_automatic_reap_removes_the_record_and_writes_the_ledger(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'automatic-ledger', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        self.run_cli('worktree', 'lock', 'feature-a')

        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'], [])
        self.assertEqual(self.read_ledger_state()['recent_events'][-1]['type'], 'governed_worktree_reaped')

    def test_gc_preview_lists_an_eligible_governed_lane(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'gc-preview', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        preview = json.loads(self.run_cli('gc', '--no-fetch', '--json').stdout)

        self.assertFalse(preview['applied'])
        self.assertEqual(preview['governed_worktree_candidates'][0]['id'], 'gc-preview')

    def test_gc_preview_and_apply_share_pending_cleanup_candidates(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'gc-pending', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        lane = registry['lanes'][0]
        tip = module.ref_tip(self.repo, lane['branch'])
        recovery_ref = 'refs/syncwheel/recovery/lanes/gc-pending-fixed'
        self.git('update-ref', recovery_ref, tip)
        lane.update({
            'state': 'captured_pending_cleanup',
            'pending_reason': 'branch_delete_failed',
            'branch_delete_tip': tip,
            'recovery_ref': recovery_ref,
        })
        module.save_governed_worktree_registry(self.repo, registry)

        preview = json.loads(self.run_cli('gc', '--no-fetch', '--json').stdout)
        applied = json.loads(self.run_cli('gc', '--apply', '--no-fetch', '--json').stdout)

        self.assertFalse(preview['applied'])
        self.assertEqual(
            [(item['id'], item['code']) for item in preview['governed_worktree_candidates']],
            [('gc-pending', 'branch_delete_failed')],
        )
        self.assertTrue(applied['applied'])
        self.assertEqual(
            {item['id'] for item in preview['governed_worktree_candidates']},
            {item['id'] for item in applied['governed_worktree_reaped']},
        )
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_huge_owner_pid_is_not_treated_as_dead(self):
        module = self.load_syncwheel_module()
        owner = f'agent@{module.socket.gethostname()}:999999999999999999999999'
        self.assertFalse(module.governed_worktree_owner_is_dead(owner))

    def test_lease_expiry_honours_a_non_utc_offset(self):
        module = self.load_syncwheel_module()
        lane = {'lease_expires_at': '2030-01-01T00:30:00-07:00'}
        now = module.datetime.datetime(2030, 1, 1, 4, 0, tzinfo=module.datetime.timezone.utc)

        self.assertFalse(module.governed_worktree_lane_lease_expired(lane, now))

    def test_gc_apply_never_reaps_a_dirty_lane(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'gc-dirty', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'keep.txt').write_text('keep\n')
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)

        self.run_cli('gc', '--apply', '--no-fetch', '--json')

        self.assertTrue((lane_path / 'keep.txt').exists())
        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'][0]['state'], 'active')

    def test_gc_returns_nonzero_when_governed_cleanup_fails(self):
        module = self.load_syncwheel_module()
        module.governed_worktree_cleanup_candidates = lambda *args, **kwargs: [{'id': 'failed-lane'}]
        module.reconcile_governed_worktrees = lambda *args, **kwargs: {
            'reaped': [],
            'failures': [{'id': 'failed-lane', 'code': 'branch_advanced'}],
        }
        module.run_coordination_gc = lambda *args, **kwargs: {
            'enabled': False,
            'candidates': [],
        }
        module.governed_worktree_diagnostics = lambda *args, **kwargs: {'lanes': []}

        with mock.patch('builtins.print'):
            result = module.command_gc(SimpleNamespace(
                repo=str(self.repo), manifest=None, personal=None,
                apply=True, fetch=False, json=True,
            ))

        self.assertEqual(result, 1)

    def test_gc_apply_reselects_candidates_under_the_registry_lock(self):
        opened = json.loads(self.run_cli(
            'worktree', 'open', 'reused-generation', '--json'
        ).stdout)
        module = self.load_syncwheel_module()
        original_candidates = module.governed_worktree_cleanup_candidates
        calls = []

        def stale_then_current(repo_root, manifest, registry=None):
            calls.append(registry is not None)
            if len(calls) == 1:
                return [{
                    'id': 'reused-generation',
                    'code': 'expired',
                    'path': opened['lane']['path'],
                }]
            return original_candidates(repo_root, manifest, registry)

        module.governed_worktree_cleanup_candidates = stale_then_current
        try:
            with mock.patch('builtins.print'):
                result = module.command_gc(SimpleNamespace(
                    repo=str(self.repo), manifest=None, personal=None,
                    apply=True, fetch=False, json=True,
                ))
        finally:
            module.governed_worktree_cleanup_candidates = original_candidates

        self.assertEqual(calls, [False, True])
        self.assertEqual(result, 0)
        self.assertTrue(Path(opened['lane']['path']).exists())
        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'][0]['generation_token'], opened['lane']['generation_token'])

    def test_missing_expired_directory_prunes_git_metadata_before_followup_rebuild(self):
        self.prepare_replay_stack()
        opened = json.loads(self.run_cli('worktree', 'open', 'missing-directory', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        shutil.rmtree(lane_path)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        self.assertIn(str(lane_path), self.git('worktree', 'list', '--porcelain'))

        self.run_cli('worktree', 'lock', 'replay')

        self.assertNotIn(str(lane_path), self.git('worktree', 'list', '--porcelain'))
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])
        status = json.loads(self.run_cli('status', '--json').stdout)
        self.assertFalse(any(
            item.get('branch') == opened['lane']['branch']
            and item.get('code') == 'unregistered_worktree'
            for item in status['governed_worktrees']['lanes']
        ))
        self.run_cli(
            'stack', 'rebuild', 'replay', '--worktree', str(self.tmp / 'replay-worktree')
        )

    def test_cleanup_takes_the_git_worktree_lock_first_and_treats_failure_as_lane_in_use(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'lock-first', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_git = module.git
        mutations = []

        def refuse_cleanup_lock(repo_root, *args, **kwargs):
            if args[:2] == ('worktree', 'lock'):
                mutations.append(args)
                return module.subprocess.CompletedProcess(args, 1, '', 'already locked')
            if args[:2] == ('update-ref', '--stdin') or args[:1] == ('update-ref',):
                mutations.append(args)
            return original_git(repo_root, *args, **kwargs)

        module.git = refuse_cleanup_lock
        try:
            result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.git = original_git

        self.assertEqual(result['reaped'], [])
        self.assertEqual(result['failures'], [{'id': 'lock-first', 'code': 'lane_in_use'}])
        self.assertEqual(mutations[0][:2], ('worktree', 'lock'))
        self.assertEqual(len(mutations), 1)
        self.assertTrue(lane_path.is_dir())
        self.assertIsNotNone(module.ref_tip(self.repo, opened['lane']['branch']))
        persisted, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(persisted['lanes'][0]['state'], 'active')
        self.assertNotIn('recovery_ref', persisted['lanes'][0])

    def test_reaper_resumes_its_lock_after_a_crash_before_registry_intent(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'lock-crash', '--json').stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        lane = registry['lanes'][0]
        lane['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        reason = module.governed_worktree_cleanup_lock_reason(lane)
        self.git('worktree', 'lock', '--reason', reason, opened['lane']['path'])

        result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())

        self.assertEqual(result['reaped'], [{'id': 'lock-crash'}])
        self.assertEqual(result['failures'], [])
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_sigkill_at_all_nine_cleanup_boundaries_is_resumed_by_plain_gc_retry(self):
        stages = (
            'after_git_worktree_lock',
            'before_cleanup_intent',
            'after_cleanup_intent',
            'after_recovery_anchor',
            'after_ref_transaction',
            'before_worktree_remove',
            'after_worktree_remove',
            'before_terminal_ledger',
            'after_terminal_ledger',
        )
        module = self.load_syncwheel_module()
        lock_path = module.governed_worktree_lock_path(self.repo)
        recovery_log = lock_path.with_name('governed-worktrees-lock-recovery.jsonl')

        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                lane_id = f'sigkill-{index}'
                opened = json.loads(self.run_cli(
                    'worktree', 'open', lane_id, '--json'
                ).stdout)
                registry, _ = module.load_governed_worktree_registry(self.repo)
                lane = next(item for item in registry['lanes'] if item['id'] == lane_id)
                lane['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
                module.save_governed_worktree_registry(self.repo, registry)

                self.run_cli_until_cleanup_sigkill(
                    stage,
                    'gc', '--apply', '--no-fetch', '--json',
                )

                metadata = json.loads(lock_path.read_text())
                self.assertGreater(metadata['pid'], 0)
                self.assertTrue(metadata['process_start_time'])
                self.assertTrue(metadata['token'])
                retry = self.run_cli('gc', '--apply', '--no-fetch', '--json')
                self.assertIn('recovered stale governed worktree registry lock', retry.stderr)
                self.assertFalse(Path(opened['lane']['path']).exists())
                persisted, _ = module.load_governed_worktree_registry(self.repo)
                self.assertFalse(any(item['id'] == lane_id for item in persisted['lanes']))

        self.assertEqual(list(lock_path.parent.glob(f'{lock_path.name}.stale-*')), [])
        recoveries = [json.loads(line) for line in recovery_log.read_text().splitlines()]
        self.assertEqual(len(recoveries), len(stages))
        self.assertTrue(all(item['reason'] == 'pid_not_alive' for item in recoveries))

    def test_registry_lock_recovers_a_reused_pid_with_a_different_start_time(self):
        module = self.load_syncwheel_module()
        lock_path = module.governed_worktree_lock_path(self.repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(json.dumps({
            'pid': os.getpid(),
            'process_start_time': 'definitely-not-this-process',
            'token': 'stale-token',
            'acquired_at': '2000-01-01T00:00:00+00:00',
        }) + '\n')

        with module.governed_worktree_registry_lock(self.repo):
            acquired = json.loads(lock_path.read_text())
            self.assertEqual(acquired['pid'], os.getpid())
            self.assertNotEqual(acquired['token'], 'stale-token')

        recovery_log = lock_path.with_name('governed-worktrees-lock-recovery.jsonl')
        recovery = json.loads(recovery_log.read_text().splitlines()[-1])
        self.assertEqual(recovery['reason'], 'process_start_time_mismatch')

    def test_registry_lock_recovers_empty_and_truncated_files_after_sigkill(self):
        module = self.load_syncwheel_module()
        lock_path = module.governed_worktree_lock_path(self.repo)
        recovery_log = lock_path.with_name('governed-worktrees-lock-recovery.jsonl')

        for index, lock_state in enumerate(('empty', 'truncated')):
            with self.subTest(lock_state=lock_state):
                lane_id = f'incomplete-lock-{index}'
                opened = json.loads(self.run_cli(
                    'worktree', 'open', lane_id, '--json'
                ).stdout)
                registry, _ = module.load_governed_worktree_registry(self.repo)
                lane = next(item for item in registry['lanes'] if item['id'] == lane_id)
                lane['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
                module.save_governed_worktree_registry(self.repo, registry)

                self.run_cli_until_registry_lock_sigkill(
                    lock_state,
                    'gc', '--apply', '--no-fetch', '--json',
                )

                payload = lock_path.read_bytes()
                if lock_state == 'empty':
                    self.assertEqual(payload, b'')
                else:
                    self.assertTrue(payload)
                    with self.assertRaises(json.JSONDecodeError):
                        json.loads(payload)
                retry = self.run_cli('gc', '--apply', '--no-fetch', '--json')
                self.assertIn('incomplete_metadata', retry.stderr)
                self.assertFalse(Path(opened['lane']['path']).exists())
                persisted, _ = module.load_governed_worktree_registry(self.repo)
                self.assertFalse(any(item['id'] == lane_id for item in persisted['lanes']))

        recoveries = [json.loads(line) for line in recovery_log.read_text().splitlines()]
        self.assertEqual(
            [item['reason'] for item in recoveries[-2:]],
            ['incomplete_metadata', 'incomplete_metadata'],
        )

    def test_registry_lock_recovers_an_unreaped_zombie_owner(self):
        opened = json.loads(self.run_cli(
            'worktree', 'open', 'zombie-lock', '--json'
        ).stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        lock_path = module.governed_worktree_lock_path(self.repo)
        ready_path = self.tmp / 'registry-lock-holder.ready'
        holder = self.start_registry_lock_holder(ready_path)
        try:
            os.kill(holder.pid, signal.SIGKILL)
            deadline = time.monotonic() + 10
            observed_state = None
            while time.monotonic() < deadline:
                try:
                    raw_stat = Path(f'/proc/{holder.pid}/stat').read_text(encoding='utf-8')
                except FileNotFoundError:
                    break
                closing_paren = raw_stat.rfind(')')
                fields = raw_stat[closing_paren + 2:].split() if closing_paren >= 0 else []
                observed_state = fields[0] if fields else None
                if observed_state == 'Z':
                    break
                time.sleep(0.01)
            self.assertEqual(observed_state, 'Z')
            self.assertIsNone(holder.returncode)

            retry = self.run_cli('gc', '--apply', '--no-fetch', '--json')

            self.assertIn('process_zombie', retry.stderr)
            self.assertFalse(Path(opened['lane']['path']).exists())
            self.assertFalse(lock_path.exists())
            persisted, _ = module.load_governed_worktree_registry(self.repo)
            self.assertEqual(persisted['lanes'], [])
        finally:
            holder.wait(timeout=10)
            holder.stdout.close()
            holder.stderr.close()

    def test_worktree_release_is_idempotent_after_gc_reaped_the_same_lane(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'reaped-first', '--json').stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        self.run_cli('gc', '--apply', '--no-fetch', '--json')

        released = json.loads(self.run_cli(
            'worktree', 'release', 'reaped-first',
            '--reason', 'operator release', '--apply', '--json',
        ).stdout)

        self.assertTrue(released['applied'])
        self.assertTrue(released['idempotent'])
        self.assertEqual(released['terminal_type'], 'governed_worktree_reaped')
        self.assertEqual(released['terminal_reason'], 'expired')
        self.assertEqual(released['note']['payload']['reason'], 'operator release')
        self.assertEqual(released['note']['payload']['terminal_seq'], released['terminal']['seq'])
        self.assertFalse(Path(opened['lane']['path']).exists())

        repeated = json.loads(self.run_cli(
            'worktree', 'release', 'reaped-first',
            '--reason', 'operator release', '--apply', '--json',
        ).stdout)

        self.assertEqual(repeated['terminal'], released['terminal'])
        notes = [
            event for event in module.load_ledger_events(self.repo)
            if event['type'] == 'governed_worktree_release_noted'
        ]
        self.assertEqual(len(notes), 1)

    def test_worktree_release_preview_never_writes_a_note_for_a_reaped_lane(self):
        self.run_cli('worktree', 'open', 'reaped-preview', '--json')
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        self.run_cli('gc', '--apply', '--no-fetch', '--json')
        before = len(module.load_ledger_events(self.repo))

        preview = json.loads(self.run_cli(
            'worktree', 'release', 'reaped-preview', '--reason', 'operator release', '--json',
        ).stdout)

        self.assertFalse(preview['applied'])
        self.assertTrue(preview['idempotent'])
        self.assertNotIn('note', preview)
        self.assertEqual(len(module.load_ledger_events(self.repo)), before)

    def test_release_racing_gc_never_reports_an_unknown_lane(self):
        module = self.load_syncwheel_module()
        reason = 'operator release'

        for index in range(6):
            with self.subTest(iteration=index):
                lane_id = f'race-gc-{index}'
                self.run_cli('worktree', 'open', lane_id, '--json')
                registry, _ = module.load_governed_worktree_registry(self.repo)
                lane = next(item for item in registry['lanes'] if item['id'] == lane_id)
                lane['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
                module.save_governed_worktree_registry(self.repo, registry)

                release, collector = self.run_cli_pair_concurrently(
                    ['worktree', 'release', lane_id, '--reason', reason, '--apply', '--json'],
                    ['gc', '--apply', '--no-fetch', '--json'],
                )

                self.assertNotIn('unknown governed worktree lane', release.stderr)
                self.assertEqual(release.returncode, 0, release.stderr)
                self.assertEqual(collector.returncode, 0, collector.stderr)
                self.assertTrue(self.lane_release_reason_recorded(module, lane_id, reason))
                persisted, _ = module.load_governed_worktree_registry(self.repo)
                self.assertFalse(any(item['id'] == lane_id for item in persisted['lanes']))

    def test_release_racing_worktree_open_never_reports_an_unknown_lane(self):
        module = self.load_syncwheel_module()
        reason = 'operator release'

        for index in range(6):
            with self.subTest(iteration=index):
                lane_id = f'race-open-{index}'
                self.run_cli('worktree', 'open', lane_id, '--json')
                registry, _ = module.load_governed_worktree_registry(self.repo)
                lane = next(item for item in registry['lanes'] if item['id'] == lane_id)
                lane['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
                module.save_governed_worktree_registry(self.repo, registry)

                release, reopen = self.run_cli_pair_concurrently(
                    ['worktree', 'release', lane_id, '--reason', reason, '--apply', '--json'],
                    ['worktree', 'open', lane_id, '--json'],
                )

                self.assertNotIn('unknown governed worktree lane', release.stderr)
                self.assertEqual(release.returncode, 0, release.stderr)
                self.assertEqual(reopen.returncode, 0, reopen.stderr)
                self.assertTrue(self.lane_release_reason_recorded(module, lane_id, reason))
                self.run_cli(
                    'worktree', 'release', lane_id,
                    '--reason', 'iteration cleanup', '--apply', '--json',
                )

    def test_release_completes_a_reap_interrupted_by_sigkill(self):
        module = self.load_syncwheel_module()
        stages = ('after_cleanup_intent', 'after_ref_transaction', 'before_worktree_remove')

        for stage in stages:
            with self.subTest(stage=stage):
                lane_id = f'crashed-{stage.replace("_", "-")}'
                opened = json.loads(self.run_cli('worktree', 'open', lane_id, '--json').stdout)
                registry, _ = module.load_governed_worktree_registry(self.repo)
                lane = next(item for item in registry['lanes'] if item['id'] == lane_id)
                lane['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
                module.save_governed_worktree_registry(self.repo, registry)

                self.run_cli_until_cleanup_sigkill(stage, 'gc', '--apply', '--no-fetch', '--json')

                pending, _ = module.load_governed_worktree_registry(self.repo)
                record = next(item for item in pending['lanes'] if item['id'] == lane_id)
                self.assertEqual(record['cleanup_event_type'], 'governed_worktree_reaped')
                self.assertEqual(record['pending_reason'], 'reaping')

                released = json.loads(self.run_cli(
                    'worktree', 'release', lane_id,
                    '--reason', 'operator takeover', '--apply', '--json',
                ).stdout)

                self.assertTrue(released['applied'])
                self.assertEqual(released['terminal_type'], 'governed_worktree_reaped')
                self.assertFalse(Path(opened['lane']['path']).exists())
                self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
                persisted, _ = module.load_governed_worktree_registry(self.repo)
                self.assertFalse(any(item['id'] == lane_id for item in persisted['lanes']))
                self.assertTrue(
                    self.lane_release_reason_recorded(module, lane_id, 'operator takeover')
                )
                intents = [
                    event for event in module.load_ledger_events(self.repo)
                    if event['type'] == 'governed_worktree_cleanup_intent'
                    and (event['payload'] or {}).get('lane') == lane_id
                ]
                terminals = [
                    event for event in module.load_ledger_events(self.repo)
                    if event['type'] in {
                        'governed_worktree_reaped', 'governed_worktree_released',
                    }
                    and (event['payload'] or {}).get('lane') == lane_id
                ]
                self.assertEqual(len(intents), 1)
                self.assertEqual(len(terminals), 1)
                self.assertEqual(
                    terminals[0]['payload']['idempotency_key'],
                    intents[0]['payload']['idempotency_key'],
                )

    def test_registry_lock_never_steals_a_lock_younger_than_the_initialization_grace(self):
        module = self.load_syncwheel_module()
        lock_path = module.governed_worktree_lock_path(self.repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))

        started = time.monotonic()
        with module.governed_worktree_registry_lock(self.repo):
            waited = time.monotonic() - started

        self.assertGreaterEqual(waited, 0.2)
        recovery = json.loads(
            lock_path.with_name('governed-worktrees-lock-recovery.jsonl')
            .read_text().splitlines()[-1]
        )
        self.assertEqual(recovery['reason'], 'incomplete_metadata')

    def test_a_stolen_uninitialized_lock_keeps_its_creator_out_of_the_critical_section(self):
        module = self.load_syncwheel_module()
        lock_path = module.governed_worktree_lock_path(self.repo)
        recovery_log = lock_path.with_name('governed-worktrees-lock-recovery.jsonl')
        trace_path = self.tmp / 'registry-lock-race.trace'
        ready_path = self.tmp / 'registry-lock-race.ready'

        creator = self.start_registry_lock_race('A', trace_path, 0.2, ready_path=ready_path)
        contender = None
        try:
            self.await_condition(
                ready_path.exists, 'the lock creator never created the lock file'
            )
            self.await_condition(
                lambda: self.process_state(creator.pid) == 'T',
                'the lock creator never stopped between creation and flock',
            )
            contender = self.start_registry_lock_race('B', trace_path, 1.0)
            self.await_condition(
                lambda: trace_path.exists() and 'B-enter' in trace_path.read_text(),
                'the contender never recovered the uninitialized lock',
            )
            os.kill(creator.pid, signal.SIGCONT)
            contender_stdout, contender_stderr = contender.communicate(timeout=60)
            creator_stdout, creator_stderr = creator.communicate(timeout=60)
        finally:
            for process in (creator, contender):
                if process is not None and process.poll() is None:
                    os.kill(process.pid, signal.SIGCONT)
                    process.kill()
                    process.wait(timeout=10)

        self.assertEqual(contender.returncode, 0, contender_stderr)
        self.assertEqual(creator.returncode, 0, creator_stderr)
        self.assertEqual(
            trace_path.read_text().split(),
            ['B-enter', 'B-exit', 'A-enter', 'A-exit'],
        )
        self.assertIn('incomplete_metadata', contender_stderr)
        self.assertIn('uninitialized governed worktree registry lock', contender_stderr)
        self.assertNotIn('recovered stale governed worktree registry lock', contender_stderr)
        recovery = json.loads(recovery_log.read_text().splitlines()[-1])
        self.assertEqual(recovery['reason'], 'incomplete_metadata')

    def test_retained_stale_registry_locks_are_pruned_by_the_next_cleanup(self):
        module = self.load_syncwheel_module()
        lock_path = module.governed_worktree_lock_path(self.repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path = lock_path.with_name(f'{lock_path.name}.stale-20000101T000000Z-0123456789ab')
        stale_path.write_text('{}\n')

        self.run_cli('gc', '--apply', '--no-fetch', '--json')

        self.assertFalse(stale_path.exists())
        pruned = [
            event for event in module.load_ledger_events(self.repo)
            if event['type'] == 'governed_worktree_stale_locks_pruned'
        ]
        self.assertEqual(len(pruned), 1)
        self.assertEqual(pruned[0]['payload']['files'], [stale_path.name])

    def test_reaper_refuses_when_the_lane_path_reappears(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'path-reappeared', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        shutil.rmtree(lane_path)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        tip = module.ref_tip(self.repo, opened['lane']['branch'])
        original_delete = module.delete_governed_worktree_branch_with_anchor

        def recreate_after_ref_transaction(repo_root, lane, expected_tip):
            deleted, detail = original_delete(repo_root, lane, expected_tip)
            if deleted:
                lane_path.mkdir(parents=True)
                (lane_path / 'late-draft.txt').write_text('must survive\n')
            return deleted, detail

        module.delete_governed_worktree_branch_with_anchor = recreate_after_ref_transaction
        try:
            result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.delete_governed_worktree_branch_with_anchor = original_delete

        self.assertEqual(result['reaped'], [])
        self.assertEqual(result['failures'], [{'id': 'path-reappeared', 'code': 'path_reappeared'}])
        self.assertEqual((lane_path / 'late-draft.txt').read_text(), 'must survive\n')
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
        persisted, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(persisted['lanes'][0]['pending_reason'], 'worktree_remove_failed')
        self.assertEqual(module.ref_tip(self.repo, persisted['lanes'][0]['recovery_ref']), tip)
        self.assertIn(str(lane_path), self.git('worktree', 'list', '--porcelain'))

    def test_cleanup_targets_only_the_registration_whose_gitdir_matches(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'targeted-registration', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        unrelated_path = self.tmp / 'unrelated-prunable-worktree'
        self.git('worktree', 'add', '-q', '-b', 'unrelated-prunable', str(unrelated_path), 'main')
        shutil.rmtree(lane_path)
        shutil.rmtree(unrelated_path)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        before = self.git('worktree', 'list', '--porcelain')
        self.assertIn(str(lane_path), before)
        self.assertIn(str(unrelated_path), before)

        result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())

        self.assertEqual(result['reaped'], [{'id': 'targeted-registration'}])
        after = self.git('worktree', 'list', '--porcelain')
        self.assertNotIn(str(lane_path), after)
        self.assertIn(str(unrelated_path), after)

    def test_cleanup_never_removes_a_worktree_whose_gitdir_does_not_match_the_record(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'gitdir-mismatch', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        module = self.load_syncwheel_module()
        admin_dir = module.governed_worktree_admin_dir_for_path(self.repo, lane_path)
        gitdir_path = admin_dir / 'gitdir'
        original_gitdir = gitdir_path.read_text()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_delete = module.delete_governed_worktree_branch_with_anchor

        def change_gitdir_after_ref_transaction(repo_root, lane, tip):
            deleted, detail = original_delete(repo_root, lane, tip)
            if deleted:
                gitdir_path.write_text(str(self.tmp / 'foreign-worktree' / '.git') + '\n')
            return deleted, detail

        module.delete_governed_worktree_branch_with_anchor = change_gitdir_after_ref_transaction
        try:
            result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.delete_governed_worktree_branch_with_anchor = original_delete
            gitdir_path.write_text(original_gitdir)

        self.assertEqual(result['reaped'], [])
        self.assertEqual(result['failures'], [{'id': 'gitdir-mismatch', 'code': 'registration_mismatch'}])
        self.assertTrue(lane_path.is_dir())
        persisted, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(persisted['lanes'][0]['pending_reason'], 'worktree_remove_failed')

    def test_reaper_reports_when_a_worktree_becomes_dirty_before_remove(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'dirty-race', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_delete = module.delete_governed_worktree_branch_with_anchor

        def dirty_after_ref_transaction(repo_root, lane, tip):
            deleted, detail = original_delete(repo_root, lane, tip)
            if deleted:
                (lane_path / 'late-draft.txt').write_text('keep late change\n')
            return deleted, detail

        module.delete_governed_worktree_branch_with_anchor = dirty_after_ref_transaction
        try:
            completed, detail = module.reap_governed_worktree_lane(
                self.repo,
                self.read_manifest(),
                registry['lanes'][0],
                persist=lambda: module.save_governed_worktree_registry(self.repo, registry),
            )
        finally:
            module.delete_governed_worktree_branch_with_anchor = original_delete

        self.assertFalse(completed)
        self.assertEqual(detail['code'], 'dirty')
        self.assertIn('became dirty before removal', detail['remedy'])
        self.assertEqual((lane_path / 'late-draft.txt').read_text(), 'keep late change\n')
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'][0]['pending_reason'], 'worktree_remove_failed')
        self.assertEqual(module.ref_tip(self.repo, registry['lanes'][0]['recovery_ref']), registry['lanes'][0]['cleanup_tip'])

    def test_registry_records_pending_delete_before_the_ref_transaction_starts(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'delete-retry', '--json').stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_git = module.git
        observed = []

        def inspect_delete_intent(repo_root, *args, **kwargs):
            if args[:2] == ('update-ref', '--stdin'):
                persisted, _ = module.load_governed_worktree_registry(self.repo)
                pending = persisted['lanes'][0]
                self.assertEqual(pending['state'], 'captured_pending_cleanup')
                self.assertEqual(pending['pending_reason'], 'reaping')
                self.assertEqual(pending['cleanup_tip'], module.ref_tip(self.repo, opened['lane']['branch']))
                self.assertTrue(pending['recovery_ref'].startswith('refs/syncwheel/recovery/lanes/'))
                self.assertTrue(pending['cleanup_idempotency_key'].startswith('governed-worktree-cleanup:'))
                self.assertTrue(pending['cleanup_lock_reason'].startswith('syncwheel-cleanup:delete-retry:'))
                observed.append(True)
            return original_git(repo_root, *args, **kwargs)

        module.git = inspect_delete_intent
        try:
            result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.git = original_git

        self.assertEqual(observed, [True])
        self.assertEqual(result['reaped'], [{'id': 'delete-retry'}])
        self.assertEqual(result['failures'], [])
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_cleanup_intent_is_fsynced_before_the_first_ref_effect(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'durable-intent', '--json').stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_fsync = module._LEDGER_FSYNC
        original_git = module.git
        ledger_fsyncs = []
        observed_refs = []

        def record_ledger_fsync(descriptor):
            ledger_fsyncs.append(descriptor)
            return original_fsync(descriptor)

        def inspect_first_ref_effect(repo_root, *args, **kwargs):
            if args[:1] == ('update-ref',):
                intents = [
                    event for event in module.load_ledger_events(self.repo)
                    if event['type'] == 'governed_worktree_cleanup_intent'
                ]
                self.assertTrue(ledger_fsyncs)
                self.assertEqual(len(intents), 1)
                payload = intents[0]['payload']
                self.assertEqual(payload['lane'], 'durable-intent')
                self.assertEqual(payload['cleanup_tip'], module.ref_tip(
                    self.repo, opened['lane']['branch']
                ))
                self.assertTrue(payload['operation_token'])
                self.assertTrue(payload['recovery_ref'].startswith(
                    'refs/syncwheel/recovery/lanes/durable-intent-'
                ))
                self.assertEqual(
                    payload['idempotency_key'],
                    payload['lane_record']['cleanup_idempotency_key'],
                )
                observed_refs.append(args)
            return original_git(repo_root, *args, **kwargs)

        module._LEDGER_FSYNC = record_ledger_fsync
        module.git = inspect_first_ref_effect
        try:
            result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module._LEDGER_FSYNC = original_fsync
            module.git = original_git

        self.assertTrue(observed_refs)
        self.assertEqual(result['reaped'], [{'id': 'durable-intent'}])

    def test_registry_save_fsyncs_temp_and_directory_and_refuses_a_stale_preimage(self):
        self.run_cli('worktree', 'open', 'registry-cas', '--json')
        module = self.load_syncwheel_module()
        registry, registry_path = module.load_governed_worktree_registry(self.repo)
        expected_digest = module.governed_worktree_registry_file_digest(registry_path)
        intended = json.loads(json.dumps(registry))
        intended['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        original_fsync = module.os.fsync
        fsynced_modes = []

        def record_fsync(descriptor):
            fsynced_modes.append(module.os.fstat(descriptor).st_mode)
            return original_fsync(descriptor)

        module.os.fsync = record_fsync
        try:
            module.save_governed_worktree_registry(
                self.repo,
                intended,
                expected_digest=expected_digest,
            )
        finally:
            module.os.fsync = original_fsync

        self.assertTrue(any(module.stat.S_ISREG(mode) for mode in fsynced_modes))
        self.assertTrue(any(module.stat.S_ISDIR(mode) for mode in fsynced_modes))
        stale_digest = expected_digest
        competing = json.loads(registry_path.read_text())
        competing['competing_writer'] = True
        registry_path.write_text(json.dumps(competing, indent=2, sort_keys=True) + '\n')
        before = registry_path.read_bytes()

        with self.assertRaisesRegex(
            module.SyncwheelError,
            'registry changed after its decision snapshot',
        ):
            module.save_governed_worktree_registry(
                self.repo,
                registry,
                expected_digest=stale_digest,
            )

        self.assertEqual(registry_path.read_bytes(), before)

    def test_restart_recovers_cleanup_from_fsynced_intent_after_registry_rollback(self):
        opened = json.loads(self.run_cli(
            'worktree', 'open', 'registry-rollback', '--json'
        ).stdout)
        module = self.load_syncwheel_module()
        registry, registry_path = module.load_governed_worktree_registry(self.repo)
        lane = registry['lanes'][0]
        lane['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        previous_registry = registry_path.read_bytes()
        tip = module.ref_tip(self.repo, lane['branch'])

        self.run_cli_until_cleanup_sigkill(
            'after_ref_transaction',
            'gc', '--apply', '--no-fetch', '--json',
        )
        intent = next(
            event for event in module.load_ledger_events(self.repo)
            if event['type'] == 'governed_worktree_cleanup_intent'
        )
        registry_path.write_bytes(previous_registry)

        retried = json.loads(self.run_cli(
            'gc', '--apply', '--no-fetch', '--json'
        ).stdout)

        self.assertEqual(retried['governed_worktree_failures'], [])
        self.assertFalse(Path(opened['lane']['path']).exists())
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
        terminal = next(
            event for event in module.load_ledger_events(self.repo)
            if event['type'] == 'governed_worktree_reaped'
        )
        self.assertEqual(
            terminal['payload']['idempotency_key'],
            intent['payload']['idempotency_key'],
        )
        self.assertEqual(terminal['payload']['recovery_ref'], intent['payload']['recovery_ref'])
        self.assertEqual(module.ref_tip(self.repo, terminal['payload']['recovery_ref']), tip)
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_recovery_ref_conflict_fails_before_the_worktree_is_touched(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'anchor-moved', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'saved.txt').write_text('unique anchored commit\n')
        subprocess.run(['git', 'add', 'saved.txt'], cwd=lane_path, check=True)
        subprocess.run(['git', 'commit', '-qm', 'feat: unique anchored commit'], cwd=lane_path, check=True)
        tip = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=lane_path, text=True, capture_output=True, check=True
        ).stdout.strip()
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_git = module.git
        injected = {'done': False}

        def move_ref_before_transaction(repo_root, *args, **kwargs):
            if args[:2] == ('update-ref', '--stdin') and not injected['done']:
                injected['done'] = True
                persisted, _ = module.load_governed_worktree_registry(self.repo)
                recovery_ref = persisted['lanes'][0]['recovery_ref']
                original_git(repo_root, 'update-ref', recovery_ref, opened['lane']['base'], tip)
            return original_git(repo_root, *args, **kwargs)

        module.git = move_ref_before_transaction
        try:
            result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.git = original_git

        self.assertEqual(result['reaped'], [])
        self.assertEqual(result['failures'], [{'id': 'anchor-moved', 'code': 'recovery_ref_moved'}])
        self.assertTrue(lane_path.is_dir())
        self.assertEqual((lane_path / 'saved.txt').read_text(), 'unique anchored commit\n')
        self.assertEqual(module.ref_tip(self.repo, opened['lane']['branch']), tip)
        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'][0]['pending_reason'], 'recovery_ref_moved')
        self.assertEqual(module.ref_tip(self.repo, registry['lanes'][0]['recovery_ref']), opened['lane']['base'])
        self.assertEqual(
            [event['type'] for event in self.read_ledger_state()['recent_events']],
            ['governed_worktree_cleanup_intent'],
        )

    def test_automatic_branch_advanced_remedy_reanchors_and_completes_with_gc(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'post-anchor-advance', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        anchored_tip = self.git('rev-parse', opened['lane']['branch'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_ensure = module.ensure_governed_worktree_recovery_ref
        injected = {'done': False}

        def advance_branch_after_anchor(repo_root, recovery_ref, expected_tip):
            original_ensure(repo_root, recovery_ref, expected_tip)
            if not injected['done']:
                injected['done'] = True
                (lane_path / 'late-commit.txt').write_text('advance after anchor\n')
                subprocess.run(['git', 'add', 'late-commit.txt'], cwd=lane_path, check=True)
                subprocess.run(
                    ['git', 'commit', '-qm', 'feat: advance after anchor'],
                    cwd=lane_path,
                    check=True,
                )

        module.ensure_governed_worktree_recovery_ref = advance_branch_after_anchor
        try:
            result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.ensure_governed_worktree_recovery_ref = original_ensure

        advanced_tip = module.ref_tip(self.repo, opened['lane']['branch'])
        self.assertNotEqual(advanced_tip, anchored_tip)
        self.assertEqual(result['reaped'], [])
        self.assertEqual(result['failures'], [{'id': 'post-anchor-advance', 'code': 'branch_advanced'}])
        self.assertTrue(lane_path.is_dir())
        registry, _ = module.load_governed_worktree_registry(self.repo)
        pending = registry['lanes'][0]
        self.assertEqual(pending['pending_reason'], 'branch_advanced')
        old_recovery_ref = pending['recovery_ref']
        old_key = pending['cleanup_idempotency_key']
        self.assertEqual(module.ref_tip(self.repo, old_recovery_ref), anchored_tip)
        remedy = module.governed_worktree_pending_remedy(self.read_manifest(), pending)
        self.assertIn('syncwheel gc --apply', remedy)

        retried = json.loads(self.run_cli(
            'gc', '--apply', '--no-fetch', '--json'
        ).stdout)

        self.assertEqual(retried['governed_worktree_failures'], [])
        self.assertEqual(retried['governed_worktree_reaped'], [{'id': 'post-anchor-advance'}])
        self.assertFalse(lane_path.exists())
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
        self.assertEqual(module.ref_tip(self.repo, old_recovery_ref), anchored_tip)
        events = module.load_ledger_events(self.repo)
        intents = [
            event for event in events
            if event['type'] == 'governed_worktree_cleanup_intent'
        ]
        terminal = next(event for event in events if event['type'] == 'governed_worktree_reaped')
        self.assertEqual(len(intents), 2)
        self.assertEqual(intents[-1]['payload']['supersedes'], old_key)
        self.assertEqual(
            terminal['payload']['idempotency_key'],
            intents[-1]['payload']['idempotency_key'],
        )
        self.assertEqual(module.ref_tip(self.repo, terminal['payload']['recovery_ref']), advanced_tip)
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_automatic_branch_advanced_remedy_reanchors_and_completes_with_release(self):
        opened = json.loads(self.run_cli(
            'worktree', 'open', 'actual-advanced-release', '--json'
        ).stdout)
        lane_path = Path(opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        lane = registry['lanes'][0]
        anchored_tip = module.ref_tip(self.repo, lane['branch'])
        lane['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_ensure = module.ensure_governed_worktree_recovery_ref
        injected = {'done': False}

        def advance_branch_after_anchor(repo_root, recovery_ref, expected_tip):
            original_ensure(repo_root, recovery_ref, expected_tip)
            if not injected['done']:
                injected['done'] = True
                (lane_path / 'advanced.txt').write_text('advanced but clean\n')
                subprocess.run(['git', 'add', 'advanced.txt'], cwd=lane_path, check=True)
                subprocess.run(
                    ['git', 'commit', '-qm', 'feat: advance release lane'],
                    cwd=lane_path,
                    check=True,
                )

        module.ensure_governed_worktree_recovery_ref = advance_branch_after_anchor
        try:
            result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.ensure_governed_worktree_recovery_ref = original_ensure

        advanced_tip = module.ref_tip(self.repo, opened['lane']['branch'])
        self.assertNotEqual(advanced_tip, anchored_tip)
        self.assertEqual(result['reaped'], [])
        self.assertEqual(
            result['failures'],
            [{'id': 'actual-advanced-release', 'code': 'branch_advanced'}],
        )
        registry, _ = module.load_governed_worktree_registry(self.repo)
        pending = registry['lanes'][0]
        self.assertEqual(pending['pending_reason'], 'branch_advanced')
        self.assertEqual(pending['cleanup_event_type'], 'governed_worktree_reaped')
        old_recovery_ref = pending['recovery_ref']
        old_key = pending['cleanup_idempotency_key']
        self.assertEqual(module.ref_tip(self.repo, old_recovery_ref), anchored_tip)

        remedy = module.governed_worktree_pending_remedy(self.read_manifest(), pending)
        self.assertIn('syncwheel gc --apply', remedy)
        released = json.loads(self.run_cli(
            'worktree', 'release', 'actual-advanced-release',
            '--reason', 'operator release', '--apply', '--json',
        ).stdout)

        self.assertEqual(module.ref_tip(self.repo, old_recovery_ref), anchored_tip)
        self.assertEqual(module.ref_tip(self.repo, released['lane']['recovery_ref']), advanced_tip)
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
        self.assertFalse(lane_path.exists())
        events = module.load_ledger_events(self.repo)
        intents = [
            event for event in events
            if event['type'] == 'governed_worktree_cleanup_intent'
        ]
        terminals = [
            event for event in events
            if event['type'] in {'governed_worktree_reaped', 'governed_worktree_released'}
        ]
        self.assertEqual(len(intents), 2)
        self.assertEqual(intents[-1]['payload']['supersedes'], old_key)
        self.assertEqual(intents[-1]['payload']['terminal_type'], 'governed_worktree_released')
        self.assertEqual(intents[-1]['payload']['reason'], 'operator release')
        self.assertEqual([event['type'] for event in terminals], ['governed_worktree_released'])
        self.assertEqual(terminals[0]['payload']['reason'], 'operator release')
        self.assertEqual(
            terminals[0]['payload']['idempotency_key'],
            intents[-1]['payload']['idempotency_key'],
        )

    def test_lane_branch_cannot_be_reattached_while_cleanup_holds_the_lock(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'locked-reattach', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        reattached_path = lane_path.with_name('syncwheel-lane-locked-reattach-second')
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_ensure = module.ensure_governed_worktree_recovery_ref
        attempts = []

        def try_reattach_while_locked(repo_root, recovery_ref, expected_tip):
            worktree = module.governed_worktree_record_for_path(repo_root, lane_path)
            self.assertTrue(str(worktree.get('locked')).startswith('syncwheel-cleanup:locked-reattach:'))
            attempts.append(subprocess.run(
                ['git', 'worktree', 'add', str(reattached_path), opened['lane']['branch']],
                cwd=repo_root,
                text=True,
                capture_output=True,
            ))
            original_ensure(repo_root, recovery_ref, expected_tip)

        module.ensure_governed_worktree_recovery_ref = try_reattach_while_locked
        try:
            result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.ensure_governed_worktree_recovery_ref = original_ensure

        self.assertEqual(len(attempts), 1)
        self.assertNotEqual(attempts[0].returncode, 0)
        self.assertIn('already used by worktree', attempts[0].stderr)
        self.assertEqual(result['reaped'], [{'id': 'locked-reattach'}])
        self.assertEqual(result['failures'], [])
        self.assertFalse(reattached_path.exists())

    def test_release_retry_accepts_branch_delete_failed_and_preserves_reason(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'release-delete-retry', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_git = module.git

        def fail_delete(repo_root, *args, **kwargs):
            if args[:2] == ('update-ref', '--stdin'):
                return module.subprocess.CompletedProcess(args, 1, '', 'injected')
            return original_git(repo_root, *args, **kwargs)

        module.git = fail_delete
        try:
            with self.assertRaises(module.SyncwheelError):
                module.command_worktree_release(SimpleNamespace(
                    repo=str(self.repo), manifest=None, personal=None,
                    lane='release-delete-retry', reason='original release reason',
                    apply=True, json=True,
                ))
        finally:
            module.git = original_git

        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'][0]['pending_reason'], 'branch_delete_failed')
        released = json.loads(self.run_cli(
            'worktree', 'release', 'release-delete-retry',
            '--reason', 'original release reason', '--apply', '--json'
        ).stdout)

        self.assertNotIn('pending_reason', released['lane'])
        events = self.read_ledger_state()['recent_events']
        self.assertEqual(
            [event['type'] for event in events],
            ['governed_worktree_cleanup_intent', 'governed_worktree_released'],
        )
        self.assertEqual(events[-1]['payload']['reason'], 'original release reason')

    def test_reaper_recovers_after_branch_delete_succeeds_before_state_persist(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'delete-crash', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        lane = registry['lanes'][0]
        tip = module.ref_tip(self.repo, lane['branch'])
        recovery_ref = 'refs/syncwheel/recovery/lanes/delete-crash-fixed'
        self.git('update-ref', recovery_ref, tip)
        lane.update({
            'state': 'captured_pending_cleanup',
            'pending_reason': 'branch_delete_failed',
            'branch_delete_tip': tip,
            'cleanup_tip': tip,
            'recovery_ref': recovery_ref,
        })
        module.save_governed_worktree_registry(self.repo, registry)
        self.git('update-ref', '-d', f"refs/heads/{lane['branch']}", tip)

        result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())

        self.assertEqual(result['reaped'], [{'id': 'delete-crash'}])
        self.assertEqual(result['failures'], [])
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])
        events = module.load_ledger_events(self.repo)
        self.assertEqual(
            [event['type'] for event in events],
            ['governed_worktree_cleanup_intent', 'governed_worktree_reaped'],
        )
        self.assertEqual(module.ref_tip(self.repo, recovery_ref), tip)

    def test_reaper_recovers_after_worktree_remove_succeeds_before_state_persist(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'remove-crash', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_run = module.run

        def stop_after_remove(command, *args, **kwargs):
            result = original_run(command, *args, **kwargs)
            if command[:3] == ['git', 'worktree', 'remove'] and Path(command[-1]) == lane_path:
                raise SystemExit('injected crash after worktree removal')
            return result

        module.run = stop_after_remove
        try:
            with self.assertRaisesRegex(SystemExit, 'injected crash'):
                module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.run = original_run

        self.assertFalse(lane_path.exists())
        pending, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(pending['lanes'][0]['state'], 'captured_pending_cleanup')
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))

        result = module.reconcile_governed_worktrees(self.repo, self.read_manifest())

        self.assertEqual(result['reaped'], [{'id': 'remove-crash'}])
        self.assertEqual(result['failures'], [])
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_reaper_retry_reuses_an_existing_matching_recovery_ref(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'remove-retry', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_run = module.run

        def fail_remove(command, *args, **kwargs):
            if command[:3] == ['git', 'worktree', 'remove'] and Path(command[-1]) == lane_path:
                raise module.SyncwheelError('injected worktree remove failure')
            return original_run(command, *args, **kwargs)

        module.run = fail_remove
        try:
            with self.assertRaises(module.SyncwheelError):
                module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.run = original_run

        registry, _ = module.load_governed_worktree_registry(self.repo)
        recovery_ref = registry['lanes'][0]['recovery_ref']
        recovery_tip = module.ref_tip(self.repo, recovery_ref)
        module.reconcile_governed_worktrees(self.repo, self.read_manifest())

        self.assertEqual(module.ref_tip(self.repo, recovery_ref), recovery_tip)
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_reaper_names_a_conflicting_existing_recovery_ref(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'recovery-conflict', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_run = module.run

        def fail_remove(command, *args, **kwargs):
            if command[:3] == ['git', 'worktree', 'remove'] and Path(command[-1]) == lane_path:
                raise module.SyncwheelError('injected worktree remove failure')
            return original_run(command, *args, **kwargs)

        module.run = fail_remove
        try:
            with self.assertRaises(module.SyncwheelError):
                module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.run = original_run

        registry, _ = module.load_governed_worktree_registry(self.repo)
        recovery_ref = registry['lanes'][0]['recovery_ref']
        expected_tip = module.ref_tip(self.repo, recovery_ref)
        conflicting_tip = self.git('rev-parse', 'HEAD~1')
        self.git('update-ref', recovery_ref, conflicting_tip, expected_tip)

        with self.assertRaisesRegex(
            module.SyncwheelError,
            rf'recovery ref {recovery_ref} points to {conflicting_tip} instead of {expected_tip}',
        ):
            module.reconcile_governed_worktrees(self.repo, self.read_manifest())

    def test_cleanup_ref_transaction_is_committed_not_aborted(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'anchor-order', '--json').stdout)
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        tip = module.ref_tip(self.repo, opened['lane']['branch'])
        calls = []
        original_git = module.git

        def record_git(repo_root, *args, **kwargs):
            calls.append((args, kwargs))
            return original_git(repo_root, *args, **kwargs)

        module.git = record_git
        try:
            module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.git = original_git

        recovery_index = next(
            index for index, (args, _) in enumerate(calls)
            if args[0] == 'update-ref' and args[1].startswith('refs/syncwheel/recovery/')
        )
        transaction_index = next(
            index for index, (args, _) in enumerate(calls)
            if args[:2] == ('update-ref', '--stdin')
        )
        self.assertLess(recovery_index, transaction_index)
        transaction = calls[transaction_index][1]['input_text']
        recovery_ref = calls[recovery_index][0][1]
        self.assertIn(f'update {recovery_ref} {tip} {tip}\n', transaction)
        self.assertIn(f'delete refs/heads/{opened["lane"]["branch"]} {tip}\n', transaction)
        self.assertIn('prepare\ncommit\n', transaction)
        self.assertIsNone(module.ref_tip(self.repo, opened['lane']['branch']))
        self.assertEqual(module.ref_tip(self.repo, recovery_ref), tip)
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_reaper_keeps_a_retryable_record_when_ledger_append_fails(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'ledger-retry', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_append = module.append_ledger_event

        def fail_terminal(repo_root, event_type, *args, **kwargs):
            if event_type == 'governed_worktree_reaped':
                raise OSError('injected')
            return original_append(repo_root, event_type, *args, **kwargs)

        module.append_ledger_event = fail_terminal
        try:
            with self.assertRaises(OSError):
                module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.append_ledger_event = original_append

        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'][0]['pending_reason'], 'ledger_pending')
        module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_release_ledger_retry_preserves_original_event_type_and_reason(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'release-ledger-retry', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_append = module.append_ledger_event

        def fail_terminal(repo_root, event_type, *args, **kwargs):
            if event_type == 'governed_worktree_released':
                raise OSError('injected')
            return original_append(repo_root, event_type, *args, **kwargs)

        module.append_ledger_event = fail_terminal
        try:
            with self.assertRaises(OSError):
                module.command_worktree_release(SimpleNamespace(
                    repo=str(self.repo), manifest=None, personal=None,
                    lane='release-ledger-retry', reason='original release reason',
                    apply=True, json=True,
                ))
        finally:
            module.append_ledger_event = original_append

        registry, _ = module.load_governed_worktree_registry(self.repo)
        self.assertEqual(registry['lanes'][0]['state'], 'reaped')
        self.assertEqual(registry['lanes'][0]['pending_reason'], 'ledger_pending')

        module.reconcile_governed_worktrees(self.repo, self.read_manifest())

        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])
        events = module.load_ledger_events(self.repo)
        self.assertEqual(
            [event['type'] for event in events],
            ['governed_worktree_cleanup_intent', 'governed_worktree_released'],
        )
        self.assertEqual(events[-1]['payload']['reason'], 'original release reason')

    def test_release_command_accepts_its_ledger_pending_state(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'release-ledger-command', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_append = module.append_ledger_event

        def fail_terminal(repo_root, event_type, *args, **kwargs):
            if event_type == 'governed_worktree_released':
                raise OSError('injected')
            return original_append(repo_root, event_type, *args, **kwargs)

        module.append_ledger_event = fail_terminal
        try:
            with self.assertRaises(OSError):
                module.command_worktree_release(SimpleNamespace(
                    repo=str(self.repo), manifest=None, personal=None,
                    lane='release-ledger-command', reason='same release reason',
                    apply=True, json=True,
                ))
        finally:
            module.append_ledger_event = original_append

        released = json.loads(self.run_cli(
            'worktree', 'release', 'release-ledger-command',
            '--reason', 'same release reason', '--apply', '--json'
        ).stdout)

        self.assertTrue(released['applied'])
        self.assertNotIn('pending_reason', released['lane'])
        events = self.read_ledger_state()['recent_events']
        self.assertEqual(
            [event['type'] for event in events],
            ['governed_worktree_cleanup_intent', 'governed_worktree_released'],
        )
        self.assertEqual(events[-1]['payload']['reason'], 'same release reason')

    def test_reaper_ledger_append_is_idempotent_after_fsync_failure(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'ledger-fsync-retry', '--json').stdout)
        self.git('worktree', 'remove', opened['lane']['path'])
        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        registry['lanes'][0]['lease_expires_at'] = '2000-01-01T00:00:00+00:00'
        module.save_governed_worktree_registry(self.repo, registry)
        original_checkpoint = module.ledger_io_checkpoint
        original_cleanup_append = module.append_governed_worktree_cleanup_event
        injected = {'raised': False, 'terminal': False}

        def fail_after_fsync(stage):
            if stage == 'event_fsynced' and injected['terminal'] and not injected['raised']:
                injected['raised'] = True
                raise OSError('injected after fsync')
            return original_checkpoint(stage)

        def mark_terminal(*args, **kwargs):
            injected['terminal'] = True
            return original_cleanup_append(*args, **kwargs)

        module.ledger_io_checkpoint = fail_after_fsync
        module.append_governed_worktree_cleanup_event = mark_terminal
        try:
            with self.assertRaises(OSError):
                module.reconcile_governed_worktrees(self.repo, self.read_manifest())
        finally:
            module.ledger_io_checkpoint = original_checkpoint
            module.append_governed_worktree_cleanup_event = original_cleanup_append

        events = [
            event for event in module.load_ledger_events(self.repo)
            if event['type'] == 'governed_worktree_reaped'
        ]
        self.assertEqual(len(events), 1)

        module.reconcile_governed_worktrees(self.repo, self.read_manifest())

        events = [
            event for event in module.load_ledger_events(self.repo)
            if event['type'] == 'governed_worktree_reaped'
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(module.load_governed_worktree_registry(self.repo)[0]['lanes'], [])

    def test_stack_create_captures_a_clean_lane_after_its_commit_is_owned(self):
        opened = json.loads(self.run_cli('worktree', 'open', 'captured', '--json').stdout)
        lane_path = Path(opened['lane']['path'])
        (lane_path / 'lane.txt').write_text('owned lane commit\n')
        subprocess.run(['git', 'add', 'lane.txt'], cwd=lane_path, check=True)
        subprocess.run(['git', 'commit', '-qm', 'feat: lane work'], cwd=lane_path, check=True)
        commit = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=lane_path, text=True, capture_output=True, check=True
        ).stdout.strip()

        self.run_cli('stack', 'create', 'lane-stack', commit, '--branch', 'pr/lane-stack')

        module = self.load_syncwheel_module()
        registry, _ = module.load_governed_worktree_registry(self.repo)
        lane = registry['lanes'][0]
        self.assertEqual(lane['state'], 'reaped')
        self.assertTrue(lane['recovery_ref'].startswith('refs/syncwheel/recovery/lanes/captured-'))
        self.assertEqual(self.git('rev-parse', lane['recovery_ref']), commit)
        self.assertFalse(lane_path.exists())

    def test_stack_create_keeps_external_manifest_lane_cleanup_in_one_ledger(self):
        self.exercise_external_manifest_lane_capture('create')

    def test_stack_add_keeps_external_manifest_lane_cleanup_in_one_ledger(self):
        self.exercise_external_manifest_lane_capture('add')

    def test_stack_capture_keeps_external_manifest_lane_cleanup_in_one_ledger(self):
        self.exercise_external_manifest_lane_capture('capture')

    def test_env_repo_allows_running_outside_target_repo(self):
        result = self.run_cli(
            'ck',
            '--no-fetch',
            '--json',
            expected=0,
            extra_env={'SYNCWHEEL_REPO': str(self.repo)},
            cwd=self.tmp,
        )
        data = json.loads(result.stdout)

        self.assertEqual(data['snapshot']['repo_root'], str(self.repo))

    def test_init_personal_creates_local_manifest_path(self):
        personal_manifest = self.repo / '.syncwheel' / 'manifests' / 'alice.local.json'

        result = self.run_cli('init', '--personal', 'alice', '--force', expected=0)

        self.assertEqual(result.stdout.strip(), str(personal_manifest))
        data = json.loads(personal_manifest.read_text())
        self.assertEqual(data['integration']['branch'], 'integration/alice/main')
        self.assertEqual(data['stacks'], [])

    def test_personal_manifests_have_isolated_ledgers(self):
        module = self.load_syncwheel_module()
        alice = self.repo / '.syncwheel' / 'manifests' / 'alice.local.json'
        bob = self.repo / '.syncwheel' / 'manifests' / 'bob.local.json'

        self.assertEqual(
            module.ledger_root(self.repo, alice),
            self.repo / '.syncwheel' / 'manifests' / 'alice.local-ledger',
        )
        self.assertEqual(
            module.ledger_root(self.repo, bob),
            self.repo / '.syncwheel' / 'manifests' / 'bob.local-ledger',
        )
        self.assertNotEqual(
            module.ledger_root(self.repo, alice),
            module.ledger_root(self.repo, bob),
        )
        self.assertEqual(
            module.ledger_root(self.repo, self.repo / '.syncwheel' / 'manifest.json'),
            self.repo / '.syncwheel' / 'ledger',
        )

    def test_init_defaults_to_main_integration(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        manifest.unlink()

        self.run_cli('init', expected=0)
        data = self.read_manifest()

        self.assertEqual(data['integration']['branch'], 'main-integration')
        self.assertEqual(data['defaults']['integration_membership'], 'required')
        self.assertEqual(self.git('branch', '--show-current'), 'main-integration')

    def test_init_can_persist_syncwheel_tracking_policy(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        manifest.unlink()

        self.run_cli(
            'init',
            '--syncwheel-tracking',
            'git-tracked',
            '--no-coordination',
            '--worktree-root',
            'var/syncwheel',
            expected=0,
        )
        data = self.read_manifest()

        self.assertEqual(data['syncwheel_tracking'], 'git-tracked')
        self.assertEqual(data['version'], 2)
        self.assertEqual(data['coordination']['mode'], 'disabled')
        self.assertEqual(data['syncwheel_worktree_root'], 'var/syncwheel')

    def test_manifest_without_authority_defaults_to_human_gated(self):
        module = self.load_syncwheel_module()

        manifest, _ = module.load_manifest(self.repo)

        self.assertNotIn('authority', manifest)
        self.assertEqual(
            module.manifest_authority(manifest),
            {'mode': 'human-gated', 'allow': [], 'deny': ['destructive_rewrite']},
        )
        self.assertFalse(module.authority_allows(manifest, 'source_change'))
        self.assertNotIn('authority', module.coordination_manifest_snapshot(manifest))

    def test_repo_authority_status_reports_undeclared_policy(self):
        result = self.run_cli('repo', 'authority', 'status', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assertFalse(data['authority_present'])
        self.assertEqual(data['authority']['mode'], 'human-gated')
        self.assertIn('authority is not declared', data['warnings'][0])

    def test_repo_authority_set_dry_run_does_not_write(self):
        before = (self.repo / '.syncwheel' / 'manifest.json').read_text()

        result = self.run_cli('repo', 'authority', 'set', 'ai-managed', '--allow', 'source_change', expected=0)

        self.assertIn('proposed_authority: ai-managed allow=source_change deny=destructive_rewrite', result.stdout)
        self.assertIn('dry_run', result.stdout)
        self.assertEqual((self.repo / '.syncwheel' / 'manifest.json').read_text(), before)

    def test_repo_authority_set_ai_managed_writes_policy_and_stages_tracked_manifest(self):
        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', expected=0)
        self.git('commit', '-qm', 'chore: track manifest')

        result = self.run_cli(
            'repo', 'authority', 'set', 'ai-managed', '--allow', 'source_change', '--apply', '--json',
            expected=0,
        )
        data = json.loads(result.stdout)

        self.assertTrue(data['authority_present'])
        self.assertEqual(
            self.read_manifest()['authority'],
            {'mode': 'ai-managed', 'allow': ['source_change'], 'deny': ['destructive_rewrite']},
        )
        self.assertNotIn('.syncwheel/manifest.json', self.git('status', '--porcelain'))
        self.assertEqual(
            self.git('show', '--format=', '--name-only', 'HEAD'),
            '.syncwheel/manifest.json',
        )
        tracking = json.loads(self.run_cli('repo', 'tracking', 'status', '--json', expected=0).stdout)
        self.assertEqual(tracking['authority']['mode'], 'ai-managed')
        status = json.loads(self.run_cli('status', '--json').stdout)
        self.assertEqual(status['authority']['allow'], ['source_change'])

    def test_repo_authority_set_refuses_destructive_rewrite(self):
        result = self.run_cli(
            'repo', 'authority', 'set', 'ai-managed', '--allow', 'destructive_rewrite', '--apply', expected=2
        )

        self.assertIn('invalid choice', result.stderr)
        self.assertNotIn('authority', self.read_manifest())

    def test_manifest_authority_is_validated_on_load(self):
        data = self.read_manifest()
        cases = [
            ({'mode': 'ai-managed', 'allow': [], 'deny': []}, 'requires at least one allowed class'),
            ({'mode': 'human-gated', 'allow': ['source_change'], 'deny': []}, 'cannot allow any class'),
            ({'mode': 'ai-managed', 'allow': ['destructive_rewrite'], 'deny': []}, 'may never contain'),
            ({'mode': 'ai-managed', 'allow': ['deploy'], 'deny': []}, 'unknown classes'),
            ({'mode': 'autonomous', 'allow': [], 'deny': []}, 'authority.mode must be one of'),
        ]
        for policy, message in cases:
            with self.subTest(policy=policy):
                data['authority'] = policy
                (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
                result = self.run_cli('repo', 'authority', 'status', expected=2)
                self.assertIn(message, result.stderr)

    def test_repo_tracking_status_reports_missing_policy(self):
        result = self.run_cli('repo', 'tracking', 'status', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assertIsNone(data['syncwheel_tracking'])
        self.assertFalse(data['syncwheel_tracking_present'])
        self.assertEqual(data['syncwheel_worktree_root'], '.syncwheel/wt')
        self.assertIn('syncwheel_tracking is not set', data['warnings'][0])

    def test_repo_tracking_set_git_tracked_stages_manifest_and_gitignore(self):
        result = self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', '--json', expected=0)
        data = json.loads(result.stdout)

        manifest = self.read_manifest()
        gitignore = (self.repo / '.gitignore').read_text()
        exclude = self.repo_exclude_path().read_text()
        tracked = self.git('ls-files', '.syncwheel/manifest.json', '.gitignore')

        self.assertEqual(manifest['syncwheel_tracking'], 'git-tracked')
        self.assertEqual(manifest['syncwheel_worktree_root'], '.syncwheel/wt')
        self.assertTrue(data['manifest_tracked'])
        self.assertIn('.syncwheel/manifest.json', tracked)
        self.assertIn('.gitignore', tracked)
        self.assertIn('# syncwheel managed metadata', gitignore)
        self.assertIn('.syncwheel/ledger/', gitignore)
        self.assertIn('.syncwheel/manifests/*.local-ledger/', gitignore)
        self.assertIn('.syncwheel/wt/', gitignore)
        self.assertNotIn('var/syncwheel/', gitignore)
        self.assertNotIn('.syncwheel/', exclude)

    def test_repo_tracking_set_git_tracked_cleans_legacy_worktree_root_from_managed_block(self):
        (self.repo / '.gitignore').write_text(
            '# syncwheel managed metadata\n'
            '.syncwheel/ledger/\n'
            '.syncwheel/profile.local.json\n'
            '.syncwheel/manifests/*.local.json\n'
            'var/syncwheel/\n'
        )

        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', '--json', expected=0)
        gitignore = (self.repo / '.gitignore').read_text()

        self.assertIn('.syncwheel/wt/', gitignore)
        self.assertNotIn('var/syncwheel/', gitignore)
        self.assertIn('# end syncwheel managed metadata', gitignore)

    def test_repo_tracking_set_local_only_uses_info_exclude_without_gitignore(self):
        result = self.run_cli('repo', 'tracking', 'set', 'local-only', '--apply', '--json', expected=0)
        data = json.loads(result.stdout)

        manifest = self.read_manifest()
        exclude = self.repo_exclude_path().read_text()

        self.assertEqual(manifest['syncwheel_tracking'], 'local-only')
        self.assertFalse(data['manifest_tracked'])
        self.assertFalse((self.repo / '.gitignore').exists())
        self.assertIn('# syncwheel local metadata', exclude)
        self.assertIn('.syncwheel/', exclude)
        self.assertNotIn('var/syncwheel/', exclude)

    def test_repo_tracking_migrates_git_tracked_to_local_only(self):
        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', expected=0)
        hook_path = self.tmp / 'hook-path-without-syncwheel'
        hook_path.mkdir()
        for executable in ('git', 'dirname'):
            target = shutil.which(executable)
            self.assertIsNotNone(target)
            (hook_path / executable).symlink_to(target)
        environment = os.environ.copy()
        environment['PATH'] = str(hook_path)
        self.assertIsNone(shutil.which('syncwheel', path=environment['PATH']))
        committed = subprocess.run(
            ['git', 'commit', '-q', '-m', 'test: track syncwheel manifest'],
            cwd=self.repo,
            env=environment,
            text=True,
            capture_output=True,
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)

        result = self.run_cli('repo', 'tracking', 'set', 'local-only', '--apply', '--json', expected=0)
        data = json.loads(result.stdout)
        exclude = self.repo_exclude_path().read_text()
        gitignore = (self.repo / '.gitignore').read_text() if (self.repo / '.gitignore').exists() else ''

        self.assertFalse(data['manifest_tracked'])
        self.assertEqual(self.git('ls-files', '.syncwheel/manifest.json'), '')
        self.assertIn('.syncwheel/', exclude)
        self.assertNotIn('var/syncwheel/', exclude)
        self.assertNotIn('# syncwheel managed metadata', gitignore)

    def test_explicit_legacy_worktree_root_is_preserved(self):
        result = self.run_cli(
            'repo',
            'tracking',
            'set',
            'git-tracked',
            '--worktree-root',
            'var/syncwheel',
            '--apply',
            '--json',
            expected=0,
        )
        data = json.loads(result.stdout)
        manifest = self.read_manifest()
        gitignore = (self.repo / '.gitignore').read_text()

        self.assertEqual(manifest['syncwheel_worktree_root'], 'var/syncwheel')
        self.assertEqual(data['syncwheel_worktree_root'], 'var/syncwheel')
        self.assertTrue(data['effective_worktree_root'].endswith('/var/syncwheel'))
        self.assertIn('var/syncwheel/', gitignore)

    def test_personal_flag_selects_local_manifest_for_commands(self):
        self.run_cli('init', '--personal', 'alice', '--force', expected=0)
        gamma = self.git('rev-parse', 'HEAD')

        self.run_cli(
            'stack',
            'create',
            '-p',
            'alice',
            'feature-c',
            gamma,
            '--branch',
            'pr/alice/feature-c',
            '--include-in-integration',
            expected=0,
        )
        self.run_cli('s', 'set', '-p', 'alice', 'feature-c', 'HEAD~1..HEAD', expected=0)
        result = self.run_cli('st', '-p', 'alice', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assert_path_equal(data['manifest_path'], self.repo / '.syncwheel' / 'manifests' / 'alice.local.json')
        self.assertEqual(data['validation']['details']['stacks'][0]['id'], 'feature-c')

    def test_use_sets_repo_local_default_personal_manifest(self):
        self.run_cli('init', '--personal', 'alice', '--force', expected=0)
        self.run_cli('use', 'alice', expected=0)

        result = self.run_cli('check', '--no-fetch', '--json', expected=1)
        data = json.loads(result.stdout)

        self.assert_path_equal(data['manifest_path'], self.repo / '.syncwheel' / 'manifests' / 'alice.local.json')

    def test_use_shared_clears_repo_local_profile(self):
        self.run_cli('init', '--personal', 'alice', '--force', expected=0)
        self.run_cli('use', 'alice', expected=0)
        self.run_cli('use', '--shared', expected=0)

        result = self.run_cli('check', '--no-fetch', '--json', expected=0)
        data = json.loads(result.stdout)

        self.assert_path_equal(data['manifest_path'], self.repo / '.syncwheel' / 'manifest.json')

    def test_stack_create_adds_stack_without_hand_editing_manifest(self):
        gamma = self.git('rev-parse', 'HEAD')

        result = self.run_cli(
            's',
            'new',
            'feature-c',
            gamma,
            '--branch',
            'pr/alice/feature-c',
            '--purpose',
            'Exercise stack creation',
            '--include-in-integration',
            expected=0,
        )

        self.assertIn('feature-c: created pr/alice/feature-c with 1 commits', result.stdout)
        manifest = self.read_manifest()
        feature_c = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-c')
        self.assertEqual(feature_c['branch'], 'pr/alice/feature-c')
        self.assertEqual(feature_c['commits'], [gamma])
        self.assertEqual(feature_c['meta']['purpose'], 'Exercise stack creation')
        self.assertIn('feature-c', manifest['integration']['stacks'])

        ledger = self.read_ledger_state()
        self.assertEqual(ledger['last_seq'], 1)
        self.assertIn('feature-c', ledger['manifest']['active_stacks'])
        self.assertEqual(ledger['stacks']['feature-c']['branch'], 'pr/alice/feature-c')

    def test_git_tracked_stack_create_commits_only_manifest_and_leaves_worktree_clean(self):
        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', expected=0)
        self.git('commit', '-qm', 'test: finish tracked syncwheel setup')

        before = self.git('rev-parse', 'HEAD')
        self.run_cli('stack', 'create', 'tracked-clean', '--branch', 'pr/tracked-clean')

        self.assertNotEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual(
            self.git('show', '--format=', '--name-only', 'HEAD'),
            '.syncwheel/manifest.json',
        )
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_git_tracked_stack_create_commits_managed_ignore_upgrade_and_stays_clean(self):
        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', expected=0)
        self.git('commit', '-qm', 'test: finish tracked syncwheel setup')
        gitignore_path = self.repo / '.gitignore'
        gitignore_path.write_text(
            gitignore_path.read_text().replace(
                '.syncwheel/manifests/*.local-ledger/\n',
                '',
            )
        )
        self.git('add', '.gitignore')
        self.git('commit', '-qm', 'test: simulate pre-upgrade managed ignore block')

        self.run_cli('stack', 'create', 'tracked-upgrade', '--branch', 'pr/tracked-upgrade')

        self.assertEqual(
            set(self.git('show', '--format=', '--name-only', 'HEAD').splitlines()),
            {'.gitignore', '.syncwheel/manifest.json'},
        )
        self.assertIn(
            '.syncwheel/manifests/*.local-ledger/',
            gitignore_path.read_text(),
        )
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_local_only_stack_create_does_not_commit(self):
        self.run_cli('repo', 'tracking', 'set', 'local-only', '--apply', expected=0)
        before = self.git('rev-parse', 'HEAD')

        self.run_cli('stack', 'create', 'local-only', '--branch', 'pr/local-only')

        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual(self.git('ls-files', '.syncwheel/manifest.json'), '')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_git_tracked_manifest_commit_never_includes_unrelated_dirty_files(self):
        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', expected=0)
        self.git('commit', '-qm', 'test: finish tracked syncwheel setup')
        (self.repo / 'alpha.txt').write_text('other unstaged work\n')
        (self.repo / 'beta.txt').write_text('other staged work\n')
        self.git('add', 'beta.txt')

        module = self.load_syncwheel_module()
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['stacks'].append({
            'id': 'isolated-manifest',
            'branch': 'pr/isolated-manifest',
            'base': 'main',
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'main',
            'commits': [],
        })
        with module.manifest_write_transaction(self.repo, manifest_path):
            module.save_manifest_with_ledger(
                self.repo,
                manifest_path,
                manifest,
                'stack_create',
                {'stack': 'isolated-manifest', 'branch': 'pr/isolated-manifest'},
            )

        self.assertEqual(
            self.git('show', '--format=', '--name-only', 'HEAD'),
            '.syncwheel/manifest.json',
        )
        self.assertEqual(self.git('diff', '--name-only'), 'alpha.txt')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'beta.txt')

    def test_concurrent_git_tracked_stack_creates_converge(self):
        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', expected=0)
        self.git('commit', '-qm', 'test: finish tracked syncwheel setup')

        results = self.run_cli_pair_concurrently(
            ('stack', 'create', 'concurrent-a', '--branch', 'pr/concurrent-a'),
            ('stack', 'create', 'concurrent-b', '--branch', 'pr/concurrent-b'),
        )

        self.assertEqual([result.returncode for result in results], [0, 0])
        self.assertEqual(
            {stack['id'] for stack in self.read_manifest()['stacks']},
            {'feature-a', 'feature-b', 'concurrent-a', 'concurrent-b'},
        )
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_reconcile_preflight_allows_modified_syncwheel_paths(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['syncwheel_tracking'] = 'git-tracked'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-qm', 'test: track syncwheel manifest')
        manifest['meta'] = {'normal_syncwheel_write': True}
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        module = self.load_syncwheel_module()

        module.preflight_reconcile_mutation_targets(
            self.repo,
            manifest,
            [{'type': 'rebuild_integration'}],
            None,
        )

    def test_personal_local_ledger_directories_are_ignored(self):
        self.run_cli('repo', 'tracking', 'set', 'git-tracked', '--apply', expected=0)
        ledger = self.repo / '.syncwheel' / 'manifests' / 'alice.local-ledger' / 'events'
        ledger.mkdir(parents=True)
        event = ledger / '0001.jsonl'
        event.write_text('{}\n')

        self.assertEqual(
            self.git('check-ignore', event.relative_to(self.repo).as_posix()),
            '.syncwheel/manifests/alice.local-ledger/events/0001.jsonl',
        )

    def test_stack_create_includes_stack_by_default_when_membership_is_required(self):
        gamma = self.git('rev-parse', 'HEAD')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        self.run_cli(
            'stack',
            'create',
            'feature-required',
            gamma,
            '--branch',
            'pr/feature-required',
            expected=0,
        )

        updated = self.read_manifest()
        self.assertIn('feature-required', updated['integration']['stacks'])

    def test_stack_create_draft_materializes_its_branch_and_validates_without_warnings(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        self.run_cli('stack', 'create', 'exploration', '--draft')

        updated = self.read_manifest()
        draft = next(stack for stack in updated['stacks'] if stack['id'] == 'exploration')
        self.assertEqual(draft['branch'], 'syncwheel/draft/exploration')
        self.assertEqual(draft['state'], 'draft')
        self.assertEqual(draft['publication'], {'enabled': False})
        self.assertEqual(self.git('rev-parse', 'syncwheel/draft/exploration'), self.git('rev-parse', 'main'))
        self.assertIn('exploration', updated['integration']['stacks'])

        validation = json.loads(self.run_cli('validate', '--json').stdout)
        self.assertEqual(validation['errors'], [])
        self.assertEqual(validation['warnings'], [])

    def test_stack_capture_integration_materializes_a_draft_without_a_worktree(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest['integration'] = {
            'branch': 'integration/capture',
            'base': 'main',
            'stacks': [],
        }
        manifest['stacks'] = []
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('branch', 'integration/capture', 'main')
        self.git('switch', '-q', 'integration/capture')
        self.run_cli('stack', 'create', 'exploration', '--draft')

        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: capture gamma')
        gamma = self.git('rev-parse', 'HEAD')
        worktrees_before_capture = self.git('worktree', 'list', '--porcelain')

        result = self.run_cli('stack', 'capture-integration', 'exploration', 'HEAD')

        self.assertIn('exploration: captured 1 integration commit', result.stdout)
        self.assertEqual(self.git('worktree', 'list', '--porcelain'), worktrees_before_capture)
        updated = self.read_manifest()
        draft = next(stack for stack in updated['stacks'] if stack['id'] == 'exploration')
        self.assertEqual(draft['commits'], [gamma])
        self.assertEqual(self.git('branch', '--contains', gamma, 'syncwheel/draft/exploration'), 'syncwheel/draft/exploration')

        self.git('switch', '-q', 'main')
        integration_worktree = self.tmp / 'wt-capture-integration'
        self.run_cli('int', 'rebuild', '--worktree', str(integration_worktree))
        self.git('worktree', 'remove', '--force', str(integration_worktree))
        self.git('switch', '-q', 'integration/capture')

        validation = json.loads(self.run_cli('validate', '--json').stdout)
        self.assertEqual(validation['details']['integration']['unmapped_commits'], [])
        self.assertTrue(self.git('merge-base', '--is-ancestor', gamma, 'syncwheel/draft/exploration') == '')

    def test_stack_capture_integration_rolls_back_manifest_when_projection_fails(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        alpha = self.git('rev-parse', 'main~1')
        manifest['defaults']['integration_membership'] = 'required'
        manifest['integration'] = {
            'branch': 'integration/capture-conflict',
            'base': alpha,
            'stacks': ['feature-b'],
        }
        feature_b = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-b')
        feature_b['base'] = alpha
        feature_b['integration_branch'] = 'integration/capture-conflict'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('branch', 'integration/capture-conflict', 'main')
        self.git('switch', '-q', 'integration/capture-conflict')
        self.run_cli('stack', 'create', 'exploration', '--draft', '--base', alpha)

        Path(self.repo / 'beta.txt').write_text('beta changed on integration\n')
        self.git('add', 'beta.txt')
        self.git('commit', '-q', '-m', 'feat: integration-only beta follow-up')
        before_capture = manifest_path.read_text()

        failure = self.run_cli('stack', 'capture-integration', 'exploration', 'HEAD', expected=2)

        self.assertIn('projection failed after adding commits', failure.stderr)
        self.assertEqual(manifest_path.read_text(), before_capture)
        updated = self.read_manifest()
        draft = next(stack for stack in updated['stacks'] if stack['id'] == 'exploration')
        self.assertEqual(draft['commits'], [])

    def test_stack_capture_integration_draft_uses_its_branch_for_merge_stacks(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest['integration'] = {
            'branch': 'integration/capture-merge',
            'base': 'main',
            'strategy': 'merge-stacks',
            'stacks': [],
        }
        manifest['stacks'] = []
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        self.git('branch', 'integration/capture-merge', 'main')
        self.git('switch', '-q', 'integration/capture-merge')
        self.run_cli('stack', 'create', 'exploration', '--draft')

        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: merge captured gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.run_cli('stack', 'capture-integration', 'exploration', 'HEAD')

        self.git('switch', '-q', 'main')
        integration_worktree = self.tmp / 'wt-capture-merge'
        self.run_cli('int', 'rebuild', '--worktree', str(integration_worktree))
        self.assertEqual(self.git('merge-base', '--is-ancestor', gamma, 'integration/capture-merge'), '')
        self.git('worktree', 'remove', '--force', str(integration_worktree))

    def test_integration_plan_offers_declarative_classification_or_capture(self):
        self.git('branch', 'integration/capture-diagnostics', 'main')
        self.git('switch', '-q', 'integration/capture-diagnostics')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: diagnose gamma')

        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['integration']['branch'] = 'integration/capture-diagnostics'
        manifest['integration']['base'] = 'main'
        manifest['integration']['stacks'] = []
        manifest['stacks'] = []
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        plan = json.loads(self.run_cli('plan', '--json').stdout)
        action = plan[-1]
        self.assertEqual(action['remedy']['type'], 'declare_integration_ownership')
        self.assertIn('stack classify-integration <stack-id>', action['remedy']['commands'][0])
        self.assertIn('stack capture-integration <stack-id>', action['remedy']['commands'][1])

        check = self.run_cli('check', '--no-fetch')
        self.assertIn('remedy: capture into a new draft stack:', check.stdout)
        self.assertIn('stack capture-integration <new-stack-id>', check.stdout)

    def test_stack_push_rejects_a_draft_by_state(self):
        self.run_cli('stack', 'create', 'exploration', '--draft')

        failure = self.run_cli('stack', 'push', 'exploration', expected=2)

        self.assertIn('state draft', failure.stderr)

    def test_reconcile_push_rejects_a_draft_without_dropping_its_rebuild_action(self):
        self.run_cli('stack', 'create', 'exploration', '--draft')
        self.git('branch', '-D', 'syncwheel/draft/exploration')

        failure = self.run_cli(
            'reconcile',
            '--no-fetch',
            '--push',
            '--stack',
            'exploration',
            '--skip-integration',
            expected=2,
        )

        self.assertIn('rebuild_stack stack=exploration', failure.stdout)
        self.assertIn('push_stack_refused stack=exploration', failure.stdout)
        self.assertIn('state draft', failure.stderr)

    def test_stack_promote_matches_a_directly_created_published_stack(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        original = self.read_manifest()

        self.run_cli('stack', 'create', 'lifecycle')
        direct = next(stack for stack in self.read_manifest()['stacks'] if stack['id'] == 'lifecycle')
        manifest_path.write_text(json.dumps(original, indent=2) + '\n')

        self.run_cli('stack', 'create', 'lifecycle', '--draft')
        self.run_cli('stack', 'promote', 'lifecycle')
        promoted = next(stack for stack in self.read_manifest()['stacks'] if stack['id'] == 'lifecycle')

        self.assertEqual(promoted, direct)
        self.assertEqual(self.git('rev-parse', 'pr/lifecycle'), self.git('rev-parse', 'main'))
        self.assertNotEqual(
            subprocess.run(
                ['git', 'rev-parse', '--verify', '--quiet', 'syncwheel/draft/lifecycle'],
                cwd=self.repo,
                text=True,
                capture_output=True,
            ).returncode,
            0,
        )

    def test_stack_promote_reports_a_retained_reconcile_worktree_path(self):
        self.run_cli('stack', 'create', 'lifecycle', '--draft')
        old_branch = 'syncwheel/draft/lifecycle'
        retained = self.repo / '.syncwheel' / 'wt' / 'syncwheel-draft-lifecycle'
        self.git('worktree', 'add', str(retained), old_branch)

        result = self.run_cli('stack', 'promote', 'lifecycle')

        self.assertTrue(retained.exists())
        self.assertIn(f'worktree path retained (not moved): {retained}', result.stdout)
        branch = subprocess.run(
            ['git', '-C', str(retained), 'branch', '--show-current'],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(branch, 'pr/lifecycle')

    def test_stack_demote_refuses_an_open_github_pull_request(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['stacks'][0]['github'] = {'pr': 42}
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        failure = self.run_cli('stack', 'demote', 'feature-a', expected=2)

        self.assertIn('github.pr', failure.stderr)

    def test_required_membership_rejects_excluded_stack(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['defaults']['integration_membership'] = 'required'
        manifest['integration']['stacks'] = ['feature-a']
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli('validate', expected=1)

        self.assertIn('required integration membership excludes stack(s): feature-b', result.stdout)

    def test_manifest_require_integration_migrates_existing_stacks(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['integration']['stacks'] = ['feature-a']
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli('manifest', 'require-integration', '--json', expected=0)
        proposal = json.loads(result.stdout)

        self.assertEqual(proposal['add_to_integration'], ['feature-b'])
        self.assertFalse(proposal['apply'])

        self.run_cli('manifest', 'require-integration', '--apply', expected=0)
        updated = self.read_manifest()

        self.assertEqual(updated['defaults']['integration_membership'], 'required')
        self.assertEqual(updated['integration']['stacks'], ['feature-a', 'feature-b'])

    def test_spoke_alias_maps_to_stack_commands(self):
        list_result = self.run_cli('spoke', 'list', expected=0)
        self.assertIn('feature-a\tpr/feature-a\tcommits=1', list_result.stdout)

        show_result = self.run_cli('spoke', 'show', 'feature-b', expected=0)
        data = json.loads(show_result.stdout)
        self.assertEqual(data['id'], 'feature-b')
        self.assertEqual(data['branch'], 'pr/feature-b')

    def test_stack_add_accepts_integration_first_commit_on_current_projection(self):
        base = self.git('rev-parse', 'HEAD')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = self.read_manifest()
        data['defaults']['base_ref'] = base
        data['integration']['base'] = base
        for stack in data['stacks']:
            stack['base'] = base
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')

        result = self.run_cli('stack', 'add', 'feature-b', 'HEAD', expected=0)

        self.assertIn('feature-b: now has 3 commits', result.stdout)
        manifest = self.read_manifest()
        feature_b = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-b')
        self.assertEqual(feature_b['commits'][-1], gamma)

    def test_stack_add_rejects_integration_first_commit_on_stale_projection(self):
        base = self.git('rev-parse', 'HEAD')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = self.read_manifest()
        data['defaults']['base_ref'] = base
        data['integration']['base'] = base
        for stack in data['stacks']:
            stack['base'] = base

        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add stack gamma')
        stack_gamma = self.git('rev-parse', 'HEAD')
        feature_b = next(stack for stack in data['stacks'] if stack['id'] == 'feature-b')
        feature_b['commits'].append(stack_gamma)
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        self.git('switch', '-q', 'main')
        Path(self.repo / 'delta.txt').write_text('delta\n')
        self.git('add', 'delta.txt')
        self.git('commit', '-q', '-m', 'feat: add delta from stale integration')
        delta = self.git('rev-parse', 'HEAD')

        result = self.run_cli('stack', 'add', 'feature-b', 'HEAD', expected=2)

        self.assertIn('not created on top of the current manifest projection', result.stderr)
        manifest = self.read_manifest()
        feature_b = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-b')
        self.assertEqual(feature_b['commits'][-1], stack_gamma)
        self.assertNotIn(delta, feature_b['commits'])

    def test_stack_rebuild_worktree_commands_are_emitted(self):
        worktree = self.tmp / 'wt-feature-a'
        result = self.run_cli('stack', 'rebuild', 'feature-a', '--worktree', str(worktree), '--dry-run', expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/pr/feature-a-before-syncwheel-', result.stdout)
        self.assertIn('git update-ref refs/heads/pr/feature-a main', result.stdout)
        self.assertIn(f'git worktree add {worktree} pr/feature-a', result.stdout)
        self.assertIn('git -C', result.stdout)

    def test_stack_rebuild_reuses_existing_stack_worktree(self):
        worktree = self.tmp / 'wt-feature-a'
        self.git('worktree', 'add', str(worktree), 'pr/feature-a')

        result = self.run_cli('stack', 'rebuild', 'feature-a', '--dry-run', expected=0)

        self.assertIn(f'git -C {worktree} reset --hard main', result.stdout)
        self.assertIn(f'git -C {worktree} cherry-pick', result.stdout)
        self.assertNotIn('git worktree add -B pr/feature-a', result.stdout)

    def test_stack_rebuild_in_place_commands_are_emitted(self):
        result = self.run_cli('stack', 'rebuild', 'feature-a', '--in-place', '--dry-run', expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/pr/feature-a-before-syncwheel-', result.stdout)
        self.assertIn('git reset --hard main', result.stdout)
        self.assertIn('git cherry-pick', result.stdout)

    def test_int_rebuild_merge_stack_commands_are_emitted(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        self.git('branch', 'integration/test', 'main')
        data['integration']['branch'] = 'integration/test'
        data['integration']['strategy'] = 'merge-stacks'
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        worktree = self.tmp / 'wt-integration'
        result = self.run_cli('int', 'rebuild', '--worktree', str(worktree), '--dry-run', expected=0)

        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/integration/test-before-syncwheel-', result.stdout)
        self.assertIn('git update-ref refs/heads/integration/test main', result.stdout)
        self.assertIn(f'git worktree add {worktree} integration/test', result.stdout)
        self.assertIn("git -C", result.stdout)
        self.assertIn("merge --no-ff pr/feature-a -m 'Merge stack '", result.stdout)
        self.assertIn("merge --no-ff pr/feature-b -m 'Merge stack '", result.stdout)

    def test_int_rebuild_in_place_commands_are_emitted(self):
        result = self.run_cli('int', 'rebuild', '--in-place', '--dry-run', expected=0)
        self.assertIn('git fetch --all --prune', result.stdout)
        self.assertIn('git branch backup/main-before-syncwheel-', result.stdout)
        self.assertIn('git reset --hard main', result.stdout)
        self.assertIn('git cherry-pick', result.stdout)

    def test_int_rebuild_reuses_existing_integration_worktree(self):
        self.git('branch', 'integration/test', 'main')
        worktree = self.tmp / 'wt-integration'
        self.git('worktree', 'add', str(worktree), 'integration/test')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('int', 'rebuild', '--dry-run', expected=0)

        self.assertIn(f'git -C {worktree} reset --hard main', result.stdout)
        self.assertIn(f'git -C {worktree} cherry-pick', result.stdout)
        self.assertNotIn('git worktree add -B integration/test', result.stdout)

    def test_int_rebuild_skips_empty_cherry_pick(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('int', 'rebuild', '--in-place', '--dry-run', expected=0)

        self.assertIn('git reset --hard main', result.stdout)
        self.assertNotIn('git cherry-pick', result.stdout)

    def test_stack_rebuild_replays_same_commit_to_same_sha(self):
        base, original_commits = self.prepare_replay_stack()
        worktree = self.tmp / 'wt-replay'

        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)

        self.assertEqual(
            self.git('rev-list', '--reverse', f'{base}..pr/replay').splitlines(),
            original_commits,
        )

    def test_stack_rebuild_twice_keeps_identical_tip(self):
        self.prepare_replay_stack()
        worktree = self.tmp / 'wt-replay'

        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)
        first_tip = self.git('rev-parse', 'pr/replay')
        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)

        self.assertEqual(self.git('rev-parse', 'pr/replay'), first_tip)

    def test_stack_rebuild_dry_run_emits_one_pinned_cherry_pick_per_commit(self):
        result = self.run_cli('stack', 'rebuild', 'feature-b', '--in-place', '--dry-run', expected=0)

        cherry_pick_lines = [line for line in result.stdout.splitlines() if ' cherry-pick ' in line]

        self.assertEqual(len(cherry_pick_lines), 2)
        self.assertTrue(all('GIT_COMMITTER_DATE=' in line for line in cherry_pick_lines))

    def test_stack_rebuild_disables_configured_gpg_signing(self):
        _, original_commits = self.prepare_replay_stack()
        worktree = self.tmp / 'wt-replay'
        self.git('config', 'commit.gpgsign', 'true')

        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)
        first_tip = self.git('rev-parse', 'pr/replay')
        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(worktree), expected=0)

        self.assertEqual(first_tip, original_commits[-1])
        self.assertEqual(self.git('rev-parse', 'pr/replay'), first_tip)

    def test_merge_stacks_rebuild_uses_deterministic_stack_tip_metadata(self):
        base, _ = self.prepare_replay_stack()
        stack_worktree = self.tmp / 'wt-replay'
        self.run_cli('stack', 'rebuild', 'replay', '--worktree', str(stack_worktree), expected=0)

        data = self.read_manifest()
        data['integration'] = {
            'branch': 'integration/replay',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['replay'],
        }
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        integration_worktree = self.tmp / 'wt-integration-replay'

        self.run_cli('int', 'rebuild', '--worktree', str(integration_worktree), expected=0)
        first_tip = self.git('rev-parse', 'integration/replay')
        self.run_cli('int', 'rebuild', '--worktree', str(integration_worktree), expected=0)

        self.assertEqual(self.git('rev-parse', 'integration/replay'), first_tip)

    def test_in_place_apply_requires_current_target_branch(self):
        result = self.run_cli('stack', 'rebuild', 'feature-a', '--in-place', expected=2)
        self.assertIn('requires current branch', result.stderr)
        self.assertIn('syncwheel worktree open <lane> --into feature-a', result.stderr)

    def test_int_rebuild_in_place_names_manifest_capture_remedy(self):
        self.git('switch', '-qc', 'feature/wrong-primary')

        result = self.run_cli('int', 'rebuild', '--in-place', expected=2)

        self.assertIn('requires current branch', result.stderr)
        self.assertIn('syncwheel stack capture-integration feature-b HEAD', result.stderr)

    def test_stack_sync_updates_manifest_from_branch(self):
        self.git('switch', '-q', 'pr/feature-a')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')

        result = self.run_cli('stack', 'sync', 'feature-a', expected=0)
        self.assertIn('synced 1 commits', result.stdout)
        manifest = self.read_manifest()
        feature_a = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-a')
        self.assertEqual(len(feature_a['commits']), 1)

    def test_stack_set_and_add_update_manifest(self):
        beta = self.git('rev-parse', 'HEAD')
        self.run_cli('stack', 'set', 'feature-a', beta, expected=0)
        manifest = self.read_manifest()
        feature_a = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-a')
        self.assertEqual(feature_a['commits'], [beta])

        alpha = self.git('rev-parse', 'HEAD~1')
        self.run_cli('stack', 'add', 'feature-a', alpha, expected=0)
        manifest = self.read_manifest()
        feature_a = next(stack for stack in manifest['stacks'] if stack['id'] == 'feature-a')
        self.assertEqual(feature_a['commits'], [beta, alpha])

    def test_stack_resolve_integration_keeps_source_projection_intact(self):
        self.git('switch', '-q', '-c', 'integration/test', 'main')
        Path(self.repo / 'gamma.txt').write_text('resolved integration\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'fix: resolve feature-b integration conflict')
        resolved = self.git('rev-parse', 'HEAD')

        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['integration']['branch'] = 'integration/test'
        manifest['integration']['base'] = 'main'
        manifest['integration']['stacks'] = ['feature-b']
        manifest['stacks'] = [manifest['stacks'][1]]
        manifest['stacks'][0]['integration_branch'] = 'integration/test'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        self.run_cli('stack', 'resolve-integration', 'feature-b', resolved, expected=0)
        updated = self.read_manifest()
        stack = updated['stacks'][0]
        self.assertEqual(stack['integration_commits'], [resolved])
        self.assertEqual(len(stack['commits']), 2)

        validation = json.loads(self.run_cli('validate', '--json', expected=0).stdout)
        self.assertEqual(validation['warnings'], [])
        self.assertEqual(json.loads(self.run_cli('plan', '--json', expected=0).stdout), [])

    def test_stack_classify_integration_is_manifest_only_and_survives_merge_rebuild(self):
        self.git('switch', '-q', '-c', 'integration/test', 'main')
        Path(self.repo / 'gamma.txt').write_text('integration only\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'docs: integration-only classification')
        classified = self.git('rev-parse', 'HEAD')

        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['integration'] = {
            'branch': 'integration/test',
            'base': 'main',
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        manifest['stacks'] = [manifest['stacks'][1]]
        manifest['stacks'][0]['integration_branch'] = 'integration/test'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        refs_before = self.git('show-ref', '--heads')
        stack_before = self.git('rev-parse', 'pr/feature-b')
        preview = json.loads(self.run_cli(
            'stack', 'classify-integration', 'feature-b', classified, expected=0,
        ).stdout)
        self.assertEqual(preview['refUpdates'], [])
        self.assertEqual(preview['worktreeUpdates'], [])
        self.assertEqual(refs_before, self.git('show-ref', '--heads'))

        self.run_cli(
            'stack', 'classify-integration', 'feature-b', classified,
            '--apply', '--plan-digest', preview['planDigest'], expected=0,
        )
        self.assertEqual(refs_before, self.git('show-ref', '--heads'))
        self.assertEqual(stack_before, self.git('rev-parse', 'pr/feature-b'))
        updated = self.read_manifest()
        stack = updated['stacks'][0]
        self.assertEqual(stack['integration_only_commits'], [classified])
        self.assertEqual(len(stack['commits']), 2)
        validation = json.loads(self.run_cli('validate', '--json', expected=0).stdout)
        self.assertEqual(validation['details']['integration']['unmapped_commits'], [])

        self.git('switch', '-q', 'main')
        worktree = self.tmp / 'wt-classified-integration'
        self.run_cli('int', 'rebuild', '--worktree', str(worktree), expected=0)
        self.assertEqual(stack_before, self.git('rev-parse', 'pr/feature-b'))
        rebuilt_validation = json.loads(self.run_cli('validate', '--json', expected=1).stdout)
        self.assertEqual(rebuilt_validation['details']['integration']['unmapped_commits'], [])
        self.assertTrue((worktree / 'gamma.txt').exists())

    def test_stack_push_is_emitted_with_passthrough_args(self):
        result = self.run_cli('stack', 'push', 'feature-a', '--dry-run', '--', '--force-with-lease', expected=0)
        self.assertIn('git push --force-with-lease fork pr/feature-a', result.stdout)

    def test_stack_push_has_explicit_force_with_lease_flag(self):
        result = self.run_cli('stack', 'push', 'feature-a', '--dry-run', '--force-with-lease', expected=0)
        self.assertIn('git push --force-with-lease fork pr/feature-a', result.stdout)

    def test_int_push_is_emitted_with_passthrough_args(self):
        result = self.run_cli('int', 'push', '--dry-run', '--', '--force-with-lease', expected=0)
        self.assertIn('git push --force-with-lease fork main', result.stdout)

    def test_reconcile_push_uses_force_with_lease_by_default(self):
        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'fork', str(origin))
        self.git('branch', 'pr/publish', 'main')
        manifest = self.read_manifest()
        manifest['stacks'].append({
            'id': 'publish',
            'branch': 'pr/publish',
            'base': 'main',
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'main',
            'commits': [],
        })
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli(
            'reconcile',
            '--no-fetch',
            '--apply',
            '--push',
            '--stack',
            'publish',
            '--skip-integration',
            expected=0,
        )

        self.assertIn('git push --force-with-lease fork pr/publish', result.stdout)

    def test_publish_uses_force_with_lease_by_default(self):
        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'fork', str(origin))
        self.git('branch', 'pr/publish', 'main')
        manifest = self.read_manifest()
        manifest['stacks'].append({
            'id': 'publish',
            'branch': 'pr/publish',
            'base': 'main',
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'main',
            'commits': [],
        })
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli(
            'publish',
            '--no-fetch',
            '--stack',
            'publish',
            '--skip-integration',
            expected=0,
        )

        self.assertIn('git push --force-with-lease fork pr/publish', result.stdout)

    def test_sync_never_pushes(self):
        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'fork', str(origin))
        self.git('branch', 'pr/publish', 'main')
        manifest = self.read_manifest()
        manifest['stacks'].append({
            'id': 'publish',
            'branch': 'pr/publish',
            'base': 'main',
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'main',
            'commits': [],
        })
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli(
            'sync',
            '--no-fetch',
            '--stack',
            'publish',
            '--skip-integration',
            expected=0,
        )

        self.assertNotIn('git push', result.stdout)

    def test_reconcile_push_can_disable_default_force_with_lease(self):
        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'fork', str(origin))
        self.git('branch', 'pr/publish', 'main')
        manifest = self.read_manifest()
        manifest['stacks'].append({
            'id': 'publish',
            'branch': 'pr/publish',
            'base': 'main',
            'target_remote': 'origin',
            'target_branch': 'main',
            'integration_branch': 'main',
            'commits': [],
        })
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli(
            'reconcile',
            '--no-fetch',
            '--apply',
            '--push',
            '--no-force-with-lease',
            '--stack',
            'publish',
            '--skip-integration',
            expected=0,
        )

        self.assertIn('git push fork pr/publish', result.stdout)
        self.assertNotIn('--force-with-lease', result.stdout)

    def test_reconcile_reports_stack_and_integration_rebuild_plan(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        self.git('branch', 'integration/reconcile', base)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        self.git('switch', '-q', 'main')

        manifest_path = self.tmp / 'reconcile-manifest.json'
        data = self.read_manifest()
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--json',
            expected=0,
        )
        report = json.loads(result.stdout)

        self.assertEqual(
            [action['type'] for action in report['actions']],
            ['rebuild_stack', 'rebuild_integration'],
        )
        self.assertEqual(report['actions'][0]['reason'], 'local_branch_differs_from_manifest_projection')
        self.assertIn('working_tree_status', report['snapshot'])

    def test_reconcile_accepts_stack_already_absorbed_by_base(self):
        module = self.load_syncwheel_module()
        absorbed = self.git('rev-parse', 'main~1')
        manifest = self.read_manifest()
        stack = next(item for item in manifest['stacks'] if item['id'] == 'feature-a')
        stack['base'] = 'main'
        stack['commits'] = [absorbed]

        report = module.stack_reconcile_report(self.repo, manifest, stack)

        self.assertTrue(report['absorbed'])
        self.assertTrue(report['local_matches_projection'])
        self.assertEqual(report['projected_tree'], module.ref_tree(self.repo, 'main'))

    def test_convergence_accepts_stack_already_absorbed_by_base(self):
        module = self.load_syncwheel_module()
        manifest = self.read_manifest()
        absorbed = self.git('rev-parse', 'main~1')
        stack = next(item for item in manifest['stacks'] if item['id'] == 'feature-a')
        stack['base'] = 'main'
        stack['commits'] = [absorbed]
        manifest['stacks'] = [stack]
        manifest['integration'] = {
            'branch': 'main',
            'base': 'main',
            'strategy': 'merge-stacks',
            'stacks': ['feature-a'],
        }

        self.assertTrue(module.local_manifest_projection_is_convergent(self.repo, manifest))

    def test_integration_projection_accepts_manifest_only_control_tree(self):
        module = self.load_syncwheel_module()
        manifest = self.read_manifest()
        base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', '-c', 'integration/control')
        manifest['integration'] = {
            'branch': 'integration/control',
            'base': base,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        Path(self.repo / '.syncwheel' / 'manifest.json').write_text(
            json.dumps({**manifest, 'control': 'recorded'}, indent=2) + '\n'
        )
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'syncwheel: record control state')

        report = module.integration_sync_report(self.repo, manifest)

        self.assertTrue(report['local_matches_projection'])

    def test_integration_projection_rejects_product_tree_difference(self):
        module = self.load_syncwheel_module()
        manifest = self.read_manifest()
        base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', '-c', 'integration/product')
        manifest['integration'] = {
            'branch': 'integration/product',
            'base': base,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        Path(self.repo / 'product.txt').write_text('drift\n')
        self.git('add', 'product.txt')
        self.git('commit', '-q', '-m', 'feat: unprojected product change')

        report = module.integration_sync_report(self.repo, manifest)

        self.assertFalse(report['local_matches_projection'])

    def test_reconcile_reports_dirty_working_tree_status(self):
        Path(self.repo / 'dirty.txt').write_text('dirty\n')

        result = self.run_cli('reconcile', '--no-fetch', expected=0)

        self.assertIn('working tree:', result.stdout)
        self.assertIn('?? dirty.txt', result.stdout)

        result = self.run_cli('reconcile', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        self.assertTrue(report['snapshot']['working_tree_dirty'])
        self.assertIn('?? dirty.txt', report['snapshot']['working_tree_status'])

    def test_stack_absorb_refuses_a_dirty_primary_before_moving_changes(self):
        Path(self.repo / 'beta.txt').write_text('beta\nabsorbed\n')
        before_stack = self.git('rev-parse', 'pr/feature-b')

        result = self.run_cli('stack', 'absorb', 'feature-b', 'beta.txt', expected=2)

        self.assertIn('primary checkout is dirty', result.stderr)
        self.assertIn('syncwheel stack capture-integration feature-b HEAD', result.stderr)
        self.assertEqual(self.git('rev-parse', 'pr/feature-b'), before_stack)
        self.assertEqual(Path(self.repo / 'beta.txt').read_text(), 'beta\nabsorbed\n')

    def test_stack_absorb_refuses_staged_hunks_in_a_dirty_primary(self):
        original = Path(self.repo / 'beta.txt').read_text()
        Path(self.repo / 'beta.txt').write_text(original + 'staged\n')
        self.git('add', 'beta.txt')
        Path(self.repo / 'alpha.txt').write_text('alpha\nunstaged\n')

        result = self.run_cli('stack', 'absorb', 'feature-b', '--staged', expected=2)

        self.assertIn('primary checkout is dirty', result.stderr)
        self.assertIn('syncwheel worktree open <lane> --into feature-b', result.stderr)
        self.assertEqual(Path(self.repo / 'beta.txt').read_text(), original + 'staged\n')
        self.assertEqual(Path(self.repo / 'alpha.txt').read_text(), 'alpha\nunstaged\n')
        status = self.tracked_status()
        self.assertIn('alpha.txt', status)
        self.assertIn('beta.txt', status)

    def test_reconcile_apply_rebuilds_stack_updates_manifest_and_rebuilds_integration(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        self.git('branch', 'integration/reconcile', base)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        self.git('switch', '-q', 'main')

        manifest_path = self.tmp / 'reconcile-manifest.json'
        data = self.read_manifest()
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--apply',
            '--worktree-root',
            str(self.tmp / 'worktrees'),
            expected=0,
        )
        updated = json.loads(manifest_path.read_text())
        updated_commit = updated['stacks'][0]['commits'][0]

        self.assertEqual(updated_commit, self.git('rev-parse', 'pr/feature-b'))
        self.assertEqual(self.git('rev-list', '--count', f'{base}..pr/feature-b'), '1')
        self.assertEqual(self.git('rev-parse', 'pr/feature-b:beta.txt'), self.git('rev-parse', f'{updated_commit}:beta.txt'))
        self.assertEqual(self.git('rev-list', '--count', f'{base}..integration/reconcile'), '3')
        module = self.load_syncwheel_module()
        committed = json.loads(self.git('show', 'integration/reconcile:.syncwheel/manifest.json'))
        normalized, _ = module.load_manifest(self.repo, manifest_path)
        self.assertEqual(module.manifest_digest(committed), module.manifest_digest(normalized))
        self.assertEqual(
            self.git('show', '-s', '--format=%s', 'integration/reconcile'),
            'chore: restore Syncwheel control manifest',
        )

    def prepare_reconcile_apply_worktree_scenario(self, worktree_root=None):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        self.git('branch', 'integration/reconcile', base)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        self.git('switch', '-q', 'main')

        data = self.read_manifest()
        data['syncwheel_tracking'] = 'local-only'
        if worktree_root is not None:
            data['syncwheel_worktree_root'] = worktree_root
        else:
            data.pop('syncwheel_worktree_root', None)
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        (self.repo / '.syncwheel' / 'manifest.json').write_text(json.dumps(data, indent=2) + '\n')
        self.git('switch', '-q', 'integration/reconcile')

    def test_reconcile_apply_uses_default_worktree_root(self):
        self.prepare_reconcile_apply_worktree_scenario()

        self.run_cli(
            'reconcile', '--no-fetch', '--apply', '--skip-integration', '--replay-mode', 'desk', expected=0
        )

        self.assertTrue((self.repo / '.syncwheel' / 'wt' / 'pr-feature-b').exists())

    def test_reconcile_apply_preserves_explicit_legacy_worktree_root(self):
        self.prepare_reconcile_apply_worktree_scenario('var/syncwheel')

        self.run_cli(
            'reconcile', '--no-fetch', '--apply', '--skip-integration', '--replay-mode', 'desk', expected=0
        )

        self.assertTrue((self.repo / 'var' / 'syncwheel' / 'pr-feature-b').exists())

    def test_stack_rebuild_uses_the_configured_worktree_root(self):
        self.prepare_reconcile_apply_worktree_scenario('var/syncwheel')

        self.run_cli('stack', 'rebuild', 'feature-b', '--replay-mode', 'desk', expected=0)

        self.assertTrue((self.repo / 'var' / 'syncwheel' / 'pr-feature-b').exists())
        self.assertFalse((self.repo.parent / f'{self.repo.name}-wt-pr-feature-b').exists())

    def test_int_rebuild_uses_the_configured_worktree_root(self):
        self.prepare_reconcile_apply_worktree_scenario('var/syncwheel')
        self.git('switch', '-q', 'main')

        self.run_cli('int', 'rebuild', '--replay-mode', 'desk', expected=0)

        self.assertTrue((self.repo / 'var' / 'syncwheel' / 'integration-reconcile').exists())
        self.assertFalse(
            (self.repo.parent / f'{self.repo.name}-wt-integration-reconcile').exists()
        )

    def test_auto_worktree_uses_the_configured_worktree_root(self):
        self.prepare_reconcile_apply_worktree_scenario('var/syncwheel')

        self.run_cli(
            'stack', 'git', 'feature-b', '--auto-worktree', '--', 'status', '--short', expected=0
        )

        self.assertTrue((self.repo / 'var' / 'syncwheel' / 'pr-feature-b').exists())
        self.assertFalse((self.repo.parent / f'{self.repo.name}-wt-pr-feature-b').exists())

    def test_reconcile_apply_leaves_no_worktree_by_default(self):
        self.prepare_reconcile_apply_worktree_scenario()
        before = self.git('worktree', 'list', '--porcelain')

        self.run_cli('reconcile', '--no-fetch', '--apply', '--skip-integration', expected=0)

        self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)
        self.assertFalse((self.repo / '.syncwheel' / 'wt' / 'pr-feature-b').exists())

    def test_reconcile_apply_preflights_a_dirty_primary_before_rebuilding_a_stack(self):
        self.prepare_reconcile_apply_worktree_scenario()
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        before_manifest = manifest.read_text()
        before_stack = self.git('rev-parse', 'pr/feature-b')
        ledger = self.repo / '.syncwheel' / 'ledger'
        self.assertFalse(ledger.exists())
        Path(self.repo / 'alpha.txt').write_text('dirty integration\n')

        result = self.run_cli('reconcile', '--no-fetch', '--apply', expected=2)

        self.assertIn('primary checkout is dirty', result.stderr)
        self.assertIn('syncwheel stack capture-integration feature-b HEAD', result.stderr)
        self.assertEqual(self.git('rev-parse', 'pr/feature-b'), before_stack)
        self.assertEqual(manifest.read_text(), before_manifest)
        self.assertFalse(ledger.exists())

    def test_check_leaves_reconcile_targets_manifest_and_ledger_unchanged(self):
        self.prepare_reconcile_apply_worktree_scenario()
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        before_manifest = manifest.read_text()
        before_stack = self.git('rev-parse', 'pr/feature-b')
        before_integration = self.git('rev-parse', 'integration/reconcile')
        ledger = self.repo / '.syncwheel' / 'ledger'
        self.assertFalse(ledger.exists())

        result = self.run_cli('check', '--no-fetch', '--json', expected=0)

        self.assertIn('plan', json.loads(result.stdout))
        self.assertEqual(self.git('rev-parse', 'pr/feature-b'), before_stack)
        self.assertEqual(self.git('rev-parse', 'integration/reconcile'), before_integration)
        self.assertEqual(manifest.read_text(), before_manifest)
        self.assertFalse(ledger.exists())

    def test_sync_rebuilds_local_projection_without_push(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        self.git('branch', 'integration/reconcile', base)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        self.git('switch', '-q', 'main')

        manifest_path = self.tmp / 'sync-manifest.json'
        data = self.read_manifest()
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli(
            'sync',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--worktree-root',
            str(self.tmp / 'worktrees'),
            expected=0,
        )
        updated = json.loads(manifest_path.read_text())
        module = self.load_syncwheel_module()
        ledger_root = self.expected_external_ledger_root(manifest_path)
        ledger_state = self.run_cli('ledger', 'show', '--manifest', str(manifest_path), '--json', expected=0)
        integration_tip = self.git('rev-parse', 'integration/reconcile')
        committed_manifest = module.manifest_from_tree(
            self.repo, integration_tip, self.repo / '.syncwheel' / 'manifest.json'
        )

        self.assertNotIn('git push', result.stdout)
        self.assertEqual(updated['stacks'][0]['commits'][0], self.git('rev-parse', 'pr/feature-b'))
        # The third commit is the required manifest-only control commit. The old
        # assertion of two commits encoded the persistence bug fixed here.
        self.assertEqual(self.git('rev-list', '--count', f'{base}..integration/reconcile'), '3')
        self.assertEqual(
            self.git('show', '-s', '--format=%s', integration_tip),
            'chore: restore Syncwheel control manifest',
        )
        self.assertEqual(module.manifest_digest(committed_manifest), module.manifest_digest(updated))
        self.assertTrue((ledger_root / 'events').exists())
        self.assertFalse((self.repo / '.syncwheel' / 'ledger').exists())
        self.assertNotIn('.syncwheel/', self.repo_exclude_path().read_text())
        self.assertEqual(self.git('status', '--short', '--untracked-files=all', '--', '.syncwheel/ledger'), '')
        self.assertGreater(json.loads(ledger_state.stdout)['last_seq'], 0)

    def test_reconcile_aligns_local_to_remote_when_remote_matches_projection(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        manifest_path = self.tmp / 'align-manifest.json'
        data = self.read_manifest()
        data['defaults']['publication_remote'] = 'origin'
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        before_manifest = manifest_path.read_text()

        self.git('branch', 'integration/reconcile', base)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")

        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'origin', str(origin))
        self.git('fetch', 'origin', '--prune')

        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: local stale stack commit')
        stale_stack = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', 'integration/reconcile')
        self.git('clean', '-fd')
        Path(self.repo / 'integration-only.txt').write_text('local only\n')
        self.git('add', 'integration-only.txt')
        self.git('commit', '-q', '-m', 'debug: local integration only')

        result = self.run_cli('reconcile', '--manifest', str(manifest_path), '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        self.assertEqual(
            [action['type'] for action in report['actions']],
            ['align_stack_to_remote', 'align_integration_to_remote'],
        )

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--apply',
            '--push',
            expected=0,
        )

        self.assertIn('align_stack_to_remote', result.stdout)
        self.assertIn('align_integration_to_remote', result.stdout)
        self.assertNotIn('git push', result.stdout)
        self.assertNotEqual(self.git('rev-parse', 'pr/feature-b'), stale_stack)
        self.assertEqual(self.git('rev-parse', 'pr/feature-b'), self.git('rev-parse', 'origin/pr/feature-b'))
        self.assertEqual(
            self.git('rev-parse', 'integration/reconcile'),
            self.git('rev-parse', 'origin/integration/reconcile'),
        )
        self.assertEqual(manifest_path.read_text(), before_manifest)

    def test_reconcile_noops_when_rewritten_history_matches_projection(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        manifest_path = self.tmp / 'rewritten-manifest.json'
        data = self.read_manifest()
        data['defaults']['publication_remote'] = 'origin'
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        self.git('switch', '-q', '-c', 'rewritten-feature-b', base)
        self.git('cherry-pick', beta)
        self.git('commit', '--amend', '-m', 'feat: add beta rewritten')
        self.git('branch', '-f', 'pr/feature-b', 'HEAD')
        self.git('switch', '-q', '-c', 'integration/reconcile', base)
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")

        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'origin', str(origin))
        self.git('fetch', 'origin', '--prune')

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--json',
            expected=0,
        )
        report = json.loads(result.stdout)

        self.assertEqual(report['actions'], [])
        self.assertTrue(report['stacks'][0]['local_matches_projection'])
        self.assertTrue(report['stacks'][0]['remote_matches_projection'])
        self.assertEqual(report['stacks'][0]['relation'], 'aligned')
        self.assertEqual(
            self.git('rev-parse', 'pr/feature-b'),
            self.git('rev-parse', 'origin/pr/feature-b'),
        )
        self.assertNotEqual(self.git('rev-parse', 'pr/feature-b'), beta)

    def test_reconcile_can_align_diverged_matching_projection_history(self):
        beta = self.git('rev-parse', 'main')
        base = self.git('rev-parse', 'main~1')
        manifest_path = self.tmp / 'diverged-matching-manifest.json'
        data = self.read_manifest()
        data['defaults']['publication_remote'] = 'origin'
        data['integration'] = {
            'branch': 'integration/reconcile',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': ['feature-b'],
        }
        data['stacks'] = [
            {
                'id': 'feature-b',
                'branch': 'pr/feature-b',
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/reconcile',
                'commits': [beta],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        before_manifest = manifest_path.read_text()

        self.git('switch', '-q', '-c', 'remote-feature-b', base)
        self.git('cherry-pick', beta)
        self.git('commit', '--amend', '-m', 'feat: add beta remote rewrite')
        remote_stack = self.git('rev-parse', 'HEAD')
        self.git('branch', '-f', 'pr/feature-b', remote_stack)
        self.git('switch', '-q', '-c', 'integration/reconcile', base)
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        remote_integration = self.git('rev-parse', 'HEAD')

        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'origin', str(origin))
        self.git('fetch', 'origin', '--prune')

        self.git('switch', '-q', 'remote-feature-b')
        self.git('reset', '--hard', base)
        self.git('cherry-pick', beta)
        self.git('commit', '--amend', '-m', 'feat: add beta local rewrite')
        local_stack = self.git('rev-parse', 'HEAD')
        self.git('branch', '-f', 'pr/feature-b', local_stack)
        self.git('switch', '-q', 'integration/reconcile')
        self.git('reset', '--hard', base)
        self.git('merge', '--no-ff', 'pr/feature-b', '-m', "Merge stack 'feature-b' into integration/reconcile")
        local_integration = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        self.git('clean', '-fd')
        self.git('switch', '-q', 'integration/reconcile')

        self.assertNotEqual(local_stack, remote_stack)
        self.assertNotEqual(local_integration, remote_integration)

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--no-align-local-to-remote',
            '--json',
            expected=0,
        )
        report = json.loads(result.stdout)
        self.assertEqual(report['actions'], [])
        self.assertEqual(report['stacks'][0]['relation'], 'diverged')
        self.assertEqual(report['integration']['relation'], 'diverged')
        self.assertTrue(report['stacks'][0]['local_matches_projection'])
        self.assertTrue(report['stacks'][0]['remote_matches_projection'])
        self.assertTrue(report['integration']['local_matches_projection'])
        self.assertTrue(report['integration']['remote_matches_projection'])

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--json',
            expected=0,
        )
        report = json.loads(result.stdout)
        self.assertEqual(
            [action['type'] for action in report['actions']],
            ['align_stack_to_remote', 'align_integration_to_remote'],
        )
        self.assertEqual(report['actions'][0]['reason'], 'local_and_remote_match_projection')
        self.assertEqual(report['actions'][1]['reason'], 'local_and_remote_match_projection')

        result = self.run_cli(
            'reconcile',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--apply',
            expected=0,
        )
        self.assertIn('align_stack_to_remote', result.stdout)
        self.assertIn('align_integration_to_remote', result.stdout)
        self.assertEqual(self.git('rev-parse', 'pr/feature-b'), remote_stack)
        self.assertEqual(self.git('rev-parse', 'integration/reconcile'), remote_integration)
        self.assertEqual(manifest_path.read_text(), before_manifest)

    def test_reconcile_pushes_manifest_only_control_commit_instead_of_aligning_back(self):
        base = self.git('rev-parse', 'main')
        manifest_path = self.tmp / 'control-ahead-manifest.json'
        data = self.read_manifest()
        data['defaults']['publication_remote'] = 'origin'
        data['integration'] = {
            'branch': 'integration/control-ahead',
            'base': base,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        data['stacks'] = []
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        self.git('branch', 'integration/control-ahead', base)

        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'origin', str(origin))
        self.git('fetch', 'origin', '--prune')
        self.git('switch', '-q', 'integration/control-ahead')
        tracked_manifest = self.repo / '.syncwheel' / 'manifest.json'
        tracked = json.loads(tracked_manifest.read_text())
        tracked['control'] = 'new ownership'
        tracked_manifest.write_text(json.dumps(tracked, indent=2) + '\n')
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'syncwheel: persist control ownership')

        result = self.run_cli(
            'reconcile', '--manifest', str(manifest_path), '--no-fetch', '--push', '--json', expected=0
        )
        report = json.loads(result.stdout)

        self.assertTrue(report['integration']['local_control_only_ahead'])
        self.assertEqual([action['type'] for action in report['actions']], ['push_integration'])

    def test_version_bump_guard_fails_for_cli_change_without_version_files(self):
        base = self.git('rev-parse', 'HEAD')
        script = self.repo / 'scripts' / 'demo.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('print("demo")\n')
        self.git('add', 'scripts/demo.py')
        self.git('commit', '-q', '-m', 'feat: add demo script')

        result = self.run_script(
            CLI.parent / 'check-version-bump.py',
            '--base',
            base,
            expected=1,
        )

        self.assertIn('Release-relevant changes require a version bump', result.stdout)
        self.assertIn('VERSION', result.stdout)
        self.assertIn('CHANGELOG.md', result.stdout)
        self.assertIn('README.md', result.stdout)

    def test_version_bump_guard_passes_with_version_and_changelog(self):
        base = self.git('rev-parse', 'HEAD')
        script = self.repo / 'scripts' / 'demo.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('print("demo")\n')
        (self.repo / 'VERSION').write_text('9.9.9\n')
        (self.repo / 'CHANGELOG.md').write_text('# Changelog\n\n## 9.9.9\n\n- Demo.\n')
        (self.repo / 'README.md').write_text('Current version: `9.9.9`\n')
        (self.repo / 'openpack.json').write_text('{"version": "9.9.9"}\n')
        self.git(
            'add', 'scripts/demo.py', 'VERSION', 'CHANGELOG.md', 'README.md',
            'openpack.json',
        )
        self.git('commit', '-q', '-m', 'feat: add demo script')

        result = self.run_script(
            CLI.parent / 'check-version-bump.py',
            '--base',
            base,
            expected=0,
        )

        self.assertIn('Version bump check passed', result.stdout)

    def test_version_bump_guard_checks_staged_files_for_hooks(self):
        script = self.repo / 'scripts' / 'demo.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('print("demo")\n')
        self.git('add', 'scripts/demo.py')

        result = self.run_script(
            CLI.parent / 'check-version-bump.py',
            '--staged',
            expected=1,
        )

        self.assertIn('Release-relevant changes require a version bump', result.stdout)
        self.assertIn('VERSION', result.stdout)

    def test_pre_commit_hook_runs_version_bump_guard(self):
        hook = REPO_ROOT / 'githooks' / 'pre-commit'
        docs = self.repo / 'docs'
        docs.mkdir(exist_ok=True)
        shutil.copy2(REPO_ROOT / 'docs' / 'sync_version.py', docs / 'sync_version.py')
        version = '1.0.0'
        (self.repo / 'VERSION').write_text(f'{version}\n')
        marker = f'<!-- syncwheel-version:start -->{version}<!-- syncwheel-version:end -->'
        (docs / 'index.html').write_text(f'hero {marker} footer {marker}\n')
        self.git('add', 'VERSION', 'docs/sync_version.py', 'docs/index.html')
        self.git('commit', '-q', '-m', 'test: add generated website')

        script = self.repo / 'scripts' / 'demo.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text('print("demo")\n')
        shutil.copy2(CLI.parent / 'check-version-bump.py', self.repo / 'scripts' / 'check-version-bump.py')
        self.git('add', 'scripts/demo.py')

        result = subprocess.run(
            [str(hook)],
            cwd=self.repo,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 1)
        self.assertIn('Release-relevant changes require a version bump', result.stdout)

    def test_int_sync_status_and_align_remote_with_local_git_remote(self):
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: add manifest')
        self.git('switch', '-q', '-c', 'pr/feature-c', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')

        manifest_path = self.tmp / 'integration-manifest.json'
        data = json.loads((self.repo / '.syncwheel' / 'manifest.json').read_text())
        data['defaults']['publication_remote'] = 'origin'
        data['integration'] = {
            'branch': 'integration/shared',
            'base': 'main',
            'strategy': 'merge-stacks',
            'stacks': ['feature-c'],
        }
        data['stacks'] = [
            {
                'id': 'feature-c',
                'branch': 'pr/feature-c',
                'base': 'main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/shared',
                'commits': [gamma],
            }
        ]
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')

        self.git('switch', '-q', '-c', 'integration/shared', 'main')
        self.git('merge', '--no-ff', 'pr/feature-c', '-m', "Merge stack 'feature-c' into integration/shared")

        origin = self.tmp / 'origin.git'
        subprocess.run(['git', 'clone', '--bare', str(self.repo), str(origin)], check=True)
        self.git('remote', 'add', 'origin', str(origin))
        self.git('push', '-u', 'origin', 'main', 'pr/feature-c', 'integration/shared')

        Path(self.repo / 'local-only.txt').write_text('local only\n')
        self.git('add', 'local-only.txt')
        self.git('commit', '-q', '-m', 'debug: local integration only')

        result = self.run_cli(
            'int',
            'sync-status',
            '--manifest',
            str(manifest_path),
            '--no-fetch',
            '--json',
            expected=0,
        )
        status = json.loads(result.stdout)

        self.assertEqual(status['sync']['relation'], 'local_ahead')
        self.assertEqual(status['sync']['ahead'], 1)
        self.assertTrue(status['sync']['remote_matches_projection'])
        self.assertFalse(status['sync']['local_matches_projection'])

        self.run_cli('int', 'align-remote', '--manifest', str(manifest_path), '--no-fetch', expected=0)

        self.assertFalse((self.repo / 'local-only.txt').exists())
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('rev-parse', 'origin/integration/shared'))
        backups = self.git('branch', '--list', 'backup/integration/shared-before-syncwheel-*')
        self.assertIn('backup/integration/shared-before-syncwheel-', backups)

    def test_manifest_compare_reports_shared_and_divergent_stacks(self):
        self.run_cli('init', '--personal', 'laptop', '--force', expected=0)
        shared_manifest = self.read_manifest()
        personal_path = self.repo / '.syncwheel' / 'manifests' / 'laptop.local.json'
        personal = json.loads(personal_path.read_text())
        personal['integration']['branch'] = 'integration/laptop/main'
        personal['integration']['stacks'] = ['feature-a', 'feature-c']
        personal['stacks'] = [
            dict(shared_manifest['stacks'][0]),
            {
                'id': 'feature-c',
                'branch': 'pr/laptop/feature-c',
                'base': 'main',
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/laptop/main',
                'commits': [self.git('rev-parse', 'HEAD')],
            },
        ]
        personal['stacks'][0]['commits'] = [self.git('rev-parse', 'HEAD')]
        personal_path.write_text(json.dumps(personal, indent=2) + '\n')

        result = self.run_cli('manifest', 'compare', '--other-personal', 'laptop', '--json', expected=0)
        comparison = json.loads(result.stdout)

        self.assertEqual(comparison['left_only'], ['feature-b'])
        self.assertEqual(comparison['right_only'], ['feature-c'])
        self.assertEqual([item['id'] for item in comparison['divergent_shared']], ['feature-a'])
        self.assertEqual(comparison['right_integration']['branch'], 'integration/laptop/main')

    def test_stack_git_runs_in_stack_worktree(self):
        self.git('worktree', 'add', '-q', str(self.tmp / 'wt-feature-a'), 'pr/feature-a')
        result = self.run_cli('stack', 'git', 'feature-a', '--', 'branch', '--show-current', expected=0)
        self.assertEqual(result.stdout.strip(), 'pr/feature-a')

    def test_int_git_runs_in_integration_worktree(self):
        result = self.run_cli('int', 'git', '--', 'branch', '--show-current', expected=0)
        self.assertEqual(result.stdout.strip(), 'main')

    def test_stack_git_can_create_explicit_worktree(self):
        worktree = self.tmp / 'wt-feature-a'
        result = self.run_cli(
            'stack',
            'git',
            'feature-a',
            '--worktree',
            str(worktree),
            '--',
            'branch',
            '--show-current',
            expected=0,
        )

        self.assertEqual(result.stdout.strip(), 'pr/feature-a')
        self.assertTrue(worktree.exists())

    def test_int_git_can_create_explicit_worktree(self):
        self.git('branch', 'integration/test', 'main')
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        worktree = self.tmp / 'wt-integration'

        result = self.run_cli(
            'int',
            'git',
            '--worktree',
            str(worktree),
            '--',
            'branch',
            '--show-current',
            expected=0,
        )

        self.assertEqual(result.stdout.strip(), 'integration/test')
        self.assertTrue(worktree.exists())

    def test_validate_warns_for_unmapped_integration_commits(self):
        self.git('branch', 'integration/test', 'main')
        self.git('switch', '-q', 'integration/test')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = ['feature-b']
        data['stacks'] = [data['stacks'][1]]
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('validate', '--json', expected=0)
        validation = json.loads(result.stdout)

        self.assertIn('not declared in any stack', '\n'.join(validation['warnings']))
        self.assertEqual(len(validation['details']['integration']['unmapped_commits']), 1)

    def test_validate_accepts_manifest_only_integration_control_commit(self):
        self.git('switch', '-q', '-c', 'integration/test', 'main')
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        data = self.read_manifest()
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        manifest_path.write_text(json.dumps(data, indent=2) + '\n')
        self.git('add', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'chore(syncwheel): update integration contract')

        validation = json.loads(self.run_cli('validate', '--json', expected=0).stdout)
        self.assertEqual(validation['warnings'], [])
        self.assertEqual(len(validation['details']['integration']['control_commits']), 1)

    def test_plan_reports_unmapped_integration_commits(self):
        self.git('branch', 'integration/test', 'main')
        self.git('switch', '-q', 'integration/test')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = ['feature-b']
        data['stacks'] = [data['stacks'][1]]
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('plan', '--json', expected=0)
        plan = json.loads(result.stdout)

        self.assertEqual(plan[-1]['type'], 'classify_integration_commits')
        self.assertEqual(len(plan[-1]['commits']), 1)

    def test_check_reports_unmapped_integration_commit_guidance(self):
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('branch', 'integration/test', 'HEAD')
        self.git('switch', '-q', 'integration/test')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('check', '--no-fetch', expected=0)

        self.assertIn('unmapped integration commits:', result.stdout)
        self.assertIn('feat: add gamma', result.stdout)
        self.assertIn('gamma.txt', result.stdout)
        self.assertIn('pr/feature-b', result.stdout)
        self.assertIn('likely stack owners:', result.stdout)
        self.assertIn('feature-b', result.stdout)
        self.assertIn('syncwheel stack add feature-b', result.stdout)

        result = self.run_cli('check', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        diagnostics = report['diagnostics']['unmapped_integration_commits']
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]['commit'], gamma)
        self.assertEqual(diagnostics[0]['likely_stacks'][0]['id'], 'feature-b')

    def test_check_warns_before_adding_same_subject_unmapped_commit(self):
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma remote\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        declared = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', '-c', 'integration/test', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma local\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['stacks'][1]['commits'].append(declared)
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('check', '--no-fetch', expected=0)

        self.assertIn('related declared commits:', result.stdout)
        self.assertIn('same_subject_declared_in_manifest', result.stdout)

    def test_reconcile_resume_mode_registers_single_likely_stack(self):
        self.git('switch', '-q', 'pr/feature-b')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('branch', 'integration/test', 'HEAD')
        self.git('switch', '-q', 'integration/test')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = ['feature-b']
        data['stacks'] = [data['stacks'][1]]
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('reconcile', '--mode', 'resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)

        self.assertEqual(report['mode'], 'resume')
        self.assertEqual(report['actions'][0]['type'], 'resume_add_commit')
        self.assertEqual(report['actions'][0]['stack'], 'feature-b')
        self.assertEqual(report['actions'][0]['commit'], gamma)
        updated = json.loads(manifest.read_text())
        self.assertNotIn(gamma, updated['stacks'][0]['commits'])

        self.run_cli('reconcile', '--mode', 'resume', '--no-fetch', '--apply', expected=0)
        updated = json.loads(manifest.read_text())
        self.assertIn(gamma, updated['stacks'][0]['commits'])

    def test_resume_alias_requires_manual_review_without_detected_owner(self):
        self.git('switch', '-q', '-c', 'integration/test', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat(DIGIT-17765): add gamma')
        gamma = self.git('rev-parse', 'HEAD')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = []
        data['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        self.git('switch', '-q', 'integration/test')

        result = self.run_cli('resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)

        self.assertEqual(report['mode'], 'resume')
        self.assertEqual(report['actions'][0]['type'], 'resume_manual_review')
        self.assertEqual(report['actions'][0]['reason'], 'owner_not_detected')
        self.assertEqual(report['actions'][0]['commit'], gamma)

        self.run_cli('resume', '--no-fetch', '--apply', expected=2)
        updated = json.loads(manifest.read_text())
        self.assertEqual(updated['integration']['stacks'], [])
        self.assertEqual(updated['stacks'], [])

    def test_resume_drops_an_integration_commit_absorbed_by_historical_merged_stack(self):
        self.git('switch', '-q', '-c', 'pr/feature-gamma', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        historical = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        self.run_cli(
            'stack', 'create', 'feature-gamma', historical,
            '--branch', 'pr/feature-gamma', '--include-in-integration', expected=0,
        )
        self.run_cli(
            'stack', 'close', 'feature-gamma', '--force',
            '--reason', 'merged-by-squash-tree-equivalent', expected=0,
        )

        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        Path(self.repo / 'delta.txt').write_text('delivered alongside gamma\n')
        self.git('add', 'gamma.txt', 'delta.txt')
        self.git('commit', '-q', '-m', 'feat: deliver gamma with delta')
        delivered = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', '-c', 'integration/test', f'{delivered}^')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'chore: replay gamma')
        duplicate = self.git('rev-parse', 'HEAD')

        self.git('switch', '-q', 'integration/test')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = []
        data['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        module = self.load_syncwheel_module()
        self.assertEqual(
            module.commit_patch_id(self.repo, historical),
            module.commit_patch_id(self.repo, duplicate),
        )
        self.assertNotIn(
            module.commit_patch_id(self.repo, duplicate),
            module.patch_ids_reachable_from_ref(self.repo, 'main'),
        )
        self.assertEqual(module.rev_list(self.repo, 'main..integration/test'), [duplicate])

        result = self.run_cli('resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        integration = report['validation']['details']['integration']
        self.assertEqual(integration['unmapped_commits'], [duplicate])
        self.assertEqual(integration['absorbed_patch_commits'], [])
        drop = next(action for action in report['actions'] if action['type'] == 'resume_drop_absorbed_commit')
        self.assertEqual(drop['commit'], duplicate)
        self.assertEqual(drop['stack'], 'feature-gamma')
        self.assertEqual(drop['matched_commit'], historical)
        self.assertFalse(any(action['type'] == 'resume_manual_review' for action in report['actions']))

        self.run_cli('resume', '--no-fetch', '--apply', expected=0)
        self.run_cli('reconcile', '--no-fetch', '--apply', expected=0)
        integration_tip = self.git('rev-parse', 'integration/test')
        # Integration now has a manifest-only control commit above main. Exact
        # tip equality was the pre-persistence behavior, not the real invariant.
        self.assertEqual(self.git('rev-parse', f'{integration_tip}^'), self.git('rev-parse', 'main'))
        self.assertEqual(
            self.git('show', '-s', '--format=%s', integration_tip),
            'chore: restore Syncwheel control manifest',
        )
        self.assertEqual(
            self.git('show', '--format=', '--name-only', integration_tip),
            '.syncwheel/manifest.json',
        )
        self.assertNotEqual(integration_tip, duplicate)

    def test_resume_keeps_a_patch_equivalent_closed_stack_for_manual_review(self):
        self.git('switch', '-q', '-c', 'pr/feature-gamma', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        historical = self.git('rev-parse', 'HEAD')
        self.git('switch', '-q', 'main')
        self.run_cli(
            'stack', 'create', 'feature-gamma', historical,
            '--branch', 'pr/feature-gamma', '--include-in-integration', expected=0,
        )
        self.run_cli('stack', 'close', 'feature-gamma', '--force', '--reason', 'abandoned', expected=0)

        self.git('switch', '-q', '-c', 'integration/test', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'chore: replay gamma')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = []
        data['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')

        result = self.run_cli('resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)
        self.assertFalse(any(action['type'] == 'resume_drop_absorbed_commit' for action in report['actions']))
        manual = next(action for action in report['actions'] if action['type'] == 'resume_manual_review')
        self.assertEqual(manual['reason'], 'owner_not_detected')

    def test_resume_restores_historical_stack_from_ledger(self):
        self.git('switch', '-q', '-c', 'pr/feature-c', 'main')
        Path(self.repo / 'gamma.txt').write_text('gamma\n')
        self.git('add', 'gamma.txt')
        self.git('commit', '-q', '-m', 'feat: add gamma')
        gamma = self.git('rev-parse', 'HEAD')
        self.git('branch', 'integration/test', 'HEAD')
        self.git('switch', '-q', 'main')

        self.run_cli(
            'stack',
            'create',
            'feature-c',
            gamma,
            '--branch',
            'pr/feature-c',
            '--include-in-integration',
            expected=0,
        )
        Path(self.repo / 'delta.txt').write_text('delta\n')
        self.git('add', 'delta.txt')
        self.git('commit', '-q', '-m', 'feat: add delta')

        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['branch'] = 'integration/test'
        data['integration']['base'] = 'main'
        data['integration']['stacks'] = []
        data['stacks'] = []
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        self.git('switch', '-q', 'integration/test')
        self.git('add', '-f', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'chore: track recovery manifest')

        check = self.run_cli('check', '--no-fetch', '--json', expected=0)
        check_report = json.loads(check.stdout)
        check_diagnostics = check_report['diagnostics']['unmapped_integration_commits']
        self.assertEqual(check_diagnostics[0]['historical_stacks'][0]['id'], 'feature-c')

        result = self.run_cli('resume', '--no-fetch', '--json', expected=0)
        report = json.loads(result.stdout)

        self.assertEqual(report['mode'], 'resume')
        self.assertEqual(report['actions'][0]['type'], 'resume_restore_stack')
        self.assertEqual(report['actions'][0]['stack'], 'feature-c')
        self.assertEqual(report['actions'][1]['type'], 'resume_add_commit')
        self.assertEqual(report['actions'][1]['stack'], 'feature-c')

        self.run_cli('resume', '--no-fetch', '--apply', expected=0)
        updated = json.loads(manifest.read_text())
        self.assertEqual(updated['integration']['stacks'], ['feature-c'])
        self.assertEqual(updated['stacks'][0]['id'], 'feature-c')
        self.assertEqual(updated['stacks'][0]['branch'], 'pr/feature-c')
        self.assertEqual(updated['stacks'][0]['commits'], [self.git('rev-parse', 'pr/feature-c')])
        self.assertNotEqual(updated['stacks'][0]['commits'], [gamma])

    def test_in_place_replay_accepts_a_tracked_manifest_with_omitted_defaults(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        self.git('add', '-f', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: track manifest')
        module = self.load_syncwheel_module()
        raw_manifest = json.loads(manifest.read_text())
        normalized_manifest, _ = module.load_manifest(self.repo, manifest)

        self.assertNotEqual(
            module.manifest_digest(raw_manifest),
            module.manifest_digest(normalized_manifest),
        )
        with module.manifest_write_transaction(self.repo, manifest):
            module.acknowledge_in_place_manifest_replay(
                self.repo,
                manifest,
                self.git('rev-parse', 'HEAD'),
            )

    def test_int_rebuild_restores_and_commits_the_control_manifest_after_merge_stacks(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        old_manifest = self.read_manifest()
        self.git('add', '-f', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: track stack manifest')
        base = self.git('rev-parse', 'HEAD')
        stack_ids = []
        for name in ('control-a', 'control-b'):
            branch = f'pr/{name}'
            stack_ids.append((name, branch))
            self.git('branch', branch, base)
            self.git('switch', '-q', branch)
            Path(self.repo / f'{name}.txt').write_text(f'{name}\n')
            self.git('add', f'{name}.txt')
            self.git('commit', '-q', '-m', f'feat: add {name}')
        self.git('switch', '-q', 'main')

        control_manifest = json.loads(json.dumps(old_manifest))
        control_manifest['integration'] = {
            'branch': 'integration/control-manifest',
            'base': base,
            'strategy': 'merge-stacks',
            'stacks': [name for name, _branch in stack_ids],
        }
        control_manifest['stacks'] = [
            {
                'id': name,
                'branch': branch,
                'base': base,
                'target_remote': 'origin',
                'target_branch': 'main',
                'integration_branch': 'integration/control-manifest',
                'commits': [self.git('rev-parse', branch)],
            }
            for name, branch in stack_ids
        ]
        manifest_path.write_text(json.dumps(control_manifest, indent=2) + '\n')
        self.git('add', '-f', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: update control manifest')
        self.git('branch', 'integration/control-manifest', 'main')
        self.git('switch', '-q', 'integration/control-manifest')
        module = self.load_syncwheel_module()
        expected_manifest, _ = module.load_manifest(self.repo, manifest_path)
        expected_digest = module.manifest_digest(expected_manifest)

        self.run_cli(
            'int', 'rebuild', '--in-place',
            '--reason', 'first reviewed projection rebuild',
            expected=0,
        )

        restored_manifest, _ = module.load_manifest(self.repo, manifest_path)
        self.assertEqual(module.manifest_digest(restored_manifest), expected_digest)
        self.assertEqual(self.git('show', '-s', '--format=%s', 'HEAD'), 'chore: restore Syncwheel control manifest')
        first_control_commit = self.git('rev-parse', 'HEAD')
        self.assertEqual(
            self.git('show', '--format=', '--name-only', 'HEAD'),
            '.syncwheel/manifest.json',
        )
        self.assertEqual(self.tracked_status(), '')
        events = module.load_ledger_events(self.repo, manifest_path)
        self.assertIn(
            'first reviewed projection rebuild',
            [event['payload'].get('reason') for event in events if event['type'] == 'manifest_saved'],
        )

        self.run_cli(
            'int', 'rebuild', '--in-place',
            '--reason', 'second reviewed projection rebuild',
            expected=0,
        )
        self.assertEqual(self.git('rev-parse', 'HEAD'), first_control_commit)
        repeated_receipts = [
            event for event in module.load_ledger_events(self.repo, manifest_path)
            if event['type'] == 'manifest_saved'
            and event['payload'].get('control_commit') == first_control_commit
        ]
        self.assertEqual(len(repeated_receipts), 2)
        self.assertEqual(
            {event['payload']['reason'] for event in repeated_receipts},
            {
                'first reviewed projection rebuild',
                'second reviewed projection rebuild',
            },
        )
        self.assertEqual(
            len({event['payload']['operation_id'] for event in repeated_receipts}),
            2,
        )

    def test_control_manifest_preflight_rejects_unexplained_divergence(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        module = self.load_syncwheel_module()
        control_manifest, _ = module.load_manifest(self.repo, manifest_path)
        divergent = json.loads(manifest_path.read_text())
        divergent['stacks'] = divergent['stacks'][:1]
        manifest_path.write_text(json.dumps(divergent, indent=2) + '\n')

        with self.assertRaisesRegex(
            module.SyncwheelError,
            'control manifest differs before integration rebuild.*missing stacks.*feature-b.*Restore the control manifest',
        ):
            module.preflight_control_manifest_digest(
                self.repo, manifest_path, control_manifest
            )

    def test_control_manifest_commit_targets_integration_tree_for_external_manifest(self):
        module = self.load_syncwheel_module()
        external = self.tmp / 'external-manifest.json'
        control, _ = module.load_manifest(self.repo, self.repo / '.syncwheel' / 'manifest.json')
        parent = self.git('rev-parse', 'HEAD')
        control['integration'] = {
            'branch': 'integration/external-control',
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        control['stacks'] = []
        external.write_text(json.dumps(control, indent=2) + '\n')
        self.git('branch', 'integration/external-control', parent)

        with module.manifest_write_transaction(self.repo, external):
            changed = module.restore_control_manifest_after_integration_rebuild(
                self.repo, external, control, parent, 'plumbing',
            )

        commit = self.git('rev-parse', 'integration/external-control')
        self.assertTrue(changed)
        self.assertNotEqual(commit, parent)
        committed = json.loads(self.git('show', f'{commit}:.syncwheel/manifest.json'))
        self.assertEqual(module.manifest_digest(committed), module.manifest_digest(control))
        persisted, _ = module.load_manifest(self.repo, external)
        self.assertEqual(module.manifest_digest(persisted), module.manifest_digest(control))

    def test_control_manifest_commit_never_uses_the_shared_index(self):
        module = self.load_syncwheel_module()
        control, _ = module.load_manifest(self.repo, self.repo / '.syncwheel' / 'manifest.json')
        Path(self.repo / 'alpha.txt').write_text('unrelated staged content\n')
        self.git('add', 'alpha.txt')
        parent = self.git('rev-parse', 'HEAD')

        commit = module.materialize_control_manifest_commit(self.repo, control, parent)

        self.assertEqual(self.git('show', '--format=', '--name-only', commit), '.syncwheel/manifest.json')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'alpha.txt')

    def test_control_manifest_commit_is_deterministic_from_its_parent_and_manifest(self):
        module = self.load_syncwheel_module()
        control, _ = module.load_manifest(self.repo, self.repo / '.syncwheel' / 'manifest.json')
        reordered = dict(reversed(list(control.items())))
        parent = self.git('rev-parse', 'HEAD')

        first = module.materialize_control_manifest_commit(self.repo, control, parent)
        self.git('config', 'user.name', 'Different Fixture Identity')
        self.git('config', 'user.email', 'different@example.com')
        second = module.materialize_control_manifest_commit(self.repo, reordered, parent)

        self.assertEqual(module.manifest_digest(control), module.manifest_digest(reordered))
        self.assertEqual(first, second)

    def test_control_manifest_object_is_verified_before_the_ref_cas(self):
        module = self.load_syncwheel_module()
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        control, _ = module.load_manifest(self.repo, manifest_path)
        parent = self.git('rev-parse', 'HEAD')
        original_manifest_from_tree = module.manifest_from_tree
        update_ref_calls = []
        original_git = module.git

        def corrupted_control_object(repo_root, commit, path):
            observed = original_manifest_from_tree(repo_root, commit, path)
            if commit != parent and observed is not None:
                observed['integration']['branch'] = 'corrupted-integration'
            return observed

        def observed_git(repo_root, *args, **kwargs):
            if args and args[0] == 'update-ref':
                update_ref_calls.append(args)
            return original_git(repo_root, *args, **kwargs)

        with mock.patch.object(
            module, 'manifest_from_tree', side_effect=corrupted_control_object,
        ), mock.patch.object(module, 'git', side_effect=observed_git):
            with self.assertRaisesRegex(
                module.SyncwheelError, 'digest differs in the object prepared',
            ):
                module.restore_control_manifest_after_integration_rebuild(
                    self.repo, manifest_path, control, parent, 'plumbing',
                )

        self.assertEqual(update_ref_calls, [])

    def test_control_manifest_retry_finishes_ref_checkout_and_external_file(self):
        module = self.load_syncwheel_module()
        internal = self.repo / '.syncwheel' / 'manifest.json'
        desired, _ = module.load_manifest(self.repo, internal)
        parent = self.git('rev-parse', 'HEAD')
        desired['integration'] = {
            'branch': 'integration/control-retry',
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        desired['stacks'] = []
        self.git('branch', 'integration/control-retry', parent)
        self.git('switch', '-q', 'integration/control-retry')
        stale = json.loads(json.dumps(desired))
        stale['defaults']['base_branch'] = 'stale-main'
        external = self.tmp / 'retry-manifest.json'
        external.write_text(json.dumps(stale, indent=2) + '\n')
        internal.unlink()

        def crash_after_ref(stage):
            if stage == 'ref_updated':
                raise RuntimeError('simulated crash after ref CAS')

        with module.manifest_write_transaction(self.repo, external):
            with mock.patch.object(
                module, 'control_manifest_io_checkpoint', side_effect=crash_after_ref,
            ):
                with self.assertRaisesRegex(RuntimeError, 'after ref CAS'):
                    module.restore_control_manifest_after_integration_rebuild(
                        self.repo, external, desired, parent, 'in-place',
                    )

        control_commit = self.git('rev-parse', 'integration/control-retry')
        self.assertNotEqual(control_commit, parent)
        self.assertNotEqual(self.git('status', '--short'), '')
        observed_stale, _ = module.load_manifest(self.repo, external)
        self.assertNotEqual(module.manifest_digest(observed_stale), module.manifest_digest(desired))

        with module.manifest_write_transaction(self.repo, external):
            recovered = module.recover_incomplete_control_manifest_persistence(
                self.repo, external, observed_stale
            )

        persisted, _ = module.load_manifest(self.repo, external)
        self.assertEqual(module.manifest_digest(recovered), module.manifest_digest(desired))
        self.assertEqual(module.manifest_digest(persisted), module.manifest_digest(desired))
        self.assertEqual(self.git('status', '--short'), '')
        events = [
            event for event in module.load_ledger_events(self.repo, external)
            if event['type'] == 'manifest_saved'
            and (event.get('payload') or {}).get('control_commit') == control_commit
        ]
        self.assertEqual(len(events), 1)

    def test_control_manifest_event_retry_is_idempotent_after_fsync(self):
        module = self.load_syncwheel_module()
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        desired, _ = module.load_manifest(self.repo, manifest_path)
        parent = self.git('rev-parse', 'HEAD')
        event_fsyncs = 0

        def crash_after_fsync(stage):
            nonlocal event_fsyncs
            if stage == 'event_fsynced':
                event_fsyncs += 1
                if event_fsyncs == 2:
                    raise RuntimeError('simulated crash after ledger fsync')

        with module.manifest_write_transaction(self.repo, manifest_path):
            with mock.patch.object(
                module, 'ledger_io_checkpoint', side_effect=crash_after_fsync,
            ):
                with self.assertRaisesRegex(RuntimeError, 'after ledger fsync'):
                    module.restore_control_manifest_after_integration_rebuild(
                        self.repo, manifest_path, desired, parent, 'plumbing',
                    )

        control_commit = self.git('rev-parse', 'main')
        with module.manifest_write_transaction(self.repo, manifest_path):
            recovered = module.recover_incomplete_control_manifest_persistence(
                self.repo, manifest_path, desired
            )
        self.assertEqual(module.manifest_digest(recovered), module.manifest_digest(desired))

        events = [
            event for event in module.load_ledger_events(self.repo, manifest_path)
            if event['type'] == 'manifest_saved'
            and (event.get('payload') or {}).get('control_commit') == control_commit
        ]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]['idempotency_key'].startswith('control-manifest:'))

    def test_control_manifest_retry_after_checkout_alignment_keeps_external_source(self):
        module = self.load_syncwheel_module()
        internal = self.repo / '.syncwheel' / 'manifest.json'
        desired, _ = module.load_manifest(self.repo, internal)
        parent = self.git('rev-parse', 'HEAD')
        desired['integration'] = {
            'branch': 'integration/control-checkout-retry',
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        desired['stacks'] = []
        self.git('branch', 'integration/control-checkout-retry', parent)
        self.git('switch', '-q', 'integration/control-checkout-retry')
        stale = json.loads(json.dumps(desired))
        stale['defaults']['base_branch'] = 'stale-main'
        external = self.tmp / 'checkout-retry-manifest.json'
        external.write_text(json.dumps(stale, indent=2) + '\n')
        internal.unlink()

        def crash_after_checkout(stage):
            if stage == 'checkout_aligned':
                raise RuntimeError('simulated crash after checkout alignment')

        with module.manifest_write_transaction(self.repo, external):
            with mock.patch.object(
                module,
                'control_manifest_io_checkpoint',
                side_effect=crash_after_checkout,
            ):
                with self.assertRaisesRegex(RuntimeError, 'after checkout alignment'):
                    module.restore_control_manifest_after_integration_rebuild(
                        self.repo, external, desired, parent, 'in-place',
                    )

        control_commit = self.git('rev-parse', 'integration/control-checkout-retry')
        persisted, _ = module.load_manifest(self.repo, external)
        self.assertEqual(module.manifest_digest(persisted), module.manifest_digest(stale))
        self.assertEqual(self.git('status', '--short'), '')

        with module.manifest_write_transaction(self.repo, external):
            recovered = module.recover_incomplete_control_manifest_persistence(
                self.repo, external, persisted
            )

        self.assertEqual(module.manifest_digest(recovered), module.manifest_digest(desired))
        persisted, _ = module.load_manifest(self.repo, external)
        self.assertEqual(module.manifest_digest(persisted), module.manifest_digest(desired))
        events = [
            event for event in module.load_ledger_events(self.repo, external)
            if event['type'] == 'manifest_saved'
            and (event.get('payload') or {}).get('control_commit') == control_commit
        ]
        self.assertEqual(len(events), 1)

    def test_control_manifest_alignment_never_rewinds_a_concurrent_ref_advance(self):
        module = self.load_syncwheel_module()
        internal = self.repo / '.syncwheel' / 'manifest.json'
        desired, _ = module.load_manifest(self.repo, internal)
        parent = self.git('rev-parse', 'HEAD')
        branch = 'integration/control-cas-race'
        desired['integration'] = {
            'branch': branch,
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        desired['stacks'] = []
        stale = json.loads(json.dumps(desired))
        stale['defaults']['base_branch'] = 'stale-before-cas-race'
        external = self.tmp / 'cas-race-manifest.json'
        external.write_text(json.dumps(stale, indent=2) + '\n')
        self.git('branch', branch, parent)
        self.git('switch', '-q', branch)
        control_commit = module.materialize_control_manifest_commit(
            self.repo, desired, parent
        )
        control_tree = self.git('rev-parse', f'{control_commit}^{{tree}}')
        concurrent_tip = self.git(
            'commit-tree', control_tree, '-p', control_commit,
            '-m', 'test: concurrent integration advance',
        )
        before = external.read_bytes()

        def advance_before_alignment(stage):
            if stage == 'before_checkout_alignment':
                self.git(
                    'update-ref', f'refs/heads/{branch}', concurrent_tip, control_commit
                )

        with module.manifest_write_transaction(self.repo, external):
            with mock.patch.object(
                module,
                'control_manifest_io_checkpoint',
                side_effect=advance_before_alignment,
            ):
                with self.assertRaisesRegex(
                    module.ControlManifestAlignmentDrift,
                    'advanced beyond.*without moving the ref',
                ):
                    module.restore_control_manifest_after_integration_rebuild(
                        self.repo, external, desired, parent, 'in-place'
                    )

        self.assertEqual(self.git('rev-parse', branch), concurrent_tip)
        self.assertEqual(external.read_bytes(), before)
        abandoned = [
            event for event in module.load_ledger_events(self.repo, external)
            if event['type'] == 'control_manifest_persistence_abandoned'
        ]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(
            abandoned[0]['payload']['outcome'], 'checkout_alignment_ref_drift'
        )

    def test_alignment_keeps_a_foreign_manifest_in_the_integration_checkout(self):
        module = self.load_syncwheel_module()
        internal = self.repo / '.syncwheel' / 'manifest.json'
        desired, _ = module.load_manifest(self.repo, internal)
        parent = self.git('rev-parse', 'HEAD')
        branch = 'integration/foreign-checkout-manifest'
        desired['integration'] = {
            'branch': branch,
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        desired['stacks'] = []
        external = self.tmp / 'foreign-checkout-source.json'
        external.write_text(json.dumps(desired, indent=2) + '\n')
        self.git('branch', branch, parent)
        self.git('switch', '-q', branch)
        foreign = json.loads(json.dumps(desired))
        foreign['defaults']['base_branch'] = 'foreign-local-proposal'
        internal.write_text(json.dumps(foreign, indent=2) + '\n')
        before = internal.read_bytes()
        control_commit = module.materialize_control_manifest_commit(
            self.repo, desired, parent
        )

        stderr = io.StringIO()
        with module.manifest_write_transaction(self.repo, external):
            with contextlib.redirect_stderr(stderr):
                persisted = module.restore_control_manifest_after_integration_rebuild(
                    self.repo, external, desired, parent, 'in-place',
                    reason='keep a foreign checkout manifest',
                    command='syncwheel int rebuild',
                )

        self.assertTrue(persisted)
        self.assertIn('still carries uncommitted changes', stderr.getvalue())
        self.assertEqual(internal.read_bytes(), before)
        self.assertEqual(self.git('rev-parse', branch), control_commit)
        self.assertEqual(
            module.pending_control_manifest_intents(
                module.load_ledger_events(self.repo, external)
            ),
            [],
        )

    def test_control_manifest_recovery_requires_local_intent_for_external_proposal(self):
        module = self.load_syncwheel_module()
        internal = self.repo / '.syncwheel' / 'manifest.json'
        baseline, _ = module.load_manifest(self.repo, internal)
        parent = self.git('rev-parse', 'HEAD')
        branch = 'integration/external-proposal'
        remote_control = json.loads(json.dumps(baseline))
        remote_control['integration'] = {
            'branch': branch,
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        remote_control['stacks'] = []
        remote_control['defaults']['replay_mode'] = 'plumbing'
        local_proposal = json.loads(json.dumps(remote_control))
        local_proposal['defaults']['replay_mode'] = 'ephemeral'
        external = self.tmp / 'proposal-manifest.json'
        external.write_text(json.dumps(local_proposal, indent=2) + '\n')
        self.git('branch', branch, parent)
        control_commit = module.materialize_control_manifest_commit(
            self.repo, remote_control, parent
        )
        self.git('update-ref', f'refs/heads/{branch}', control_commit, parent)
        before = external.read_bytes()

        with module.manifest_write_transaction(self.repo, external):
            with self.assertRaises(module.SyncwheelError) as raised:
                module.recover_incomplete_control_manifest_persistence(
                    self.repo, external, local_proposal
                )

        message = str(raised.exception)
        self.assertIn('no local persistence intent', message)
        self.assertIn('syncwheel int rebuild', message)
        self.assertIn('--reason', message)
        self.assertEqual(external.read_bytes(), before)
        remedy = shlex.split(message.rsplit('run: ', 1)[1])
        self.run_cli(*remedy[1:], expected=0)
        persisted, _ = module.load_manifest(self.repo, external)
        committed = module.manifest_from_tree(
            self.repo,
            self.git('rev-parse', branch),
            module.integration_manifest_path(self.repo),
        )
        self.assertEqual(module.manifest_digest(persisted), module.manifest_digest(local_proposal))
        self.assertEqual(module.manifest_digest(committed), module.manifest_digest(local_proposal))

    def test_historical_manifest_digest_does_not_authorize_control_divergence(self):
        module = self.load_syncwheel_module()
        baseline, _ = module.load_manifest(
            self.repo, self.repo / '.syncwheel' / 'manifest.json'
        )
        parent = self.git('rev-parse', 'HEAD')
        branch = 'integration/historical-proposal'
        local_proposal = json.loads(json.dumps(baseline))
        local_proposal['integration'] = {
            'branch': branch,
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        local_proposal['stacks'] = []
        local_proposal['defaults']['replay_mode'] = 'ephemeral'
        foreign_control = json.loads(json.dumps(local_proposal))
        foreign_control['defaults']['replay_mode'] = 'plumbing'
        external = self.tmp / 'historical-proposal-manifest.json'
        external.write_text(json.dumps(local_proposal, indent=2) + '\n')
        self.git('branch', branch, parent)
        with module.manifest_write_transaction(self.repo, external):
            module.save_manifest_with_ledger(
                self.repo,
                external,
                local_proposal,
                'record historical local proposal',
            )
        control_commit = module.materialize_control_manifest_commit(
            self.repo, foreign_control, parent
        )
        self.git('update-ref', f'refs/heads/{branch}', control_commit, parent)
        before = external.read_bytes()

        with module.manifest_write_transaction(self.repo, external):
            with self.assertRaisesRegex(
                module.SyncwheelError, 'no local persistence intent'
            ):
                module.recover_incomplete_control_manifest_persistence(
                    self.repo, external, local_proposal
                )

        self.assertEqual(external.read_bytes(), before)
        self.assertEqual(self.git('rev-parse', branch), control_commit)

    def test_control_manifest_retry_accepts_control_index_and_replay_worktree(self):
        module = self.load_syncwheel_module()
        internal = self.repo / '.syncwheel' / 'manifest.json'
        replay, _ = module.load_manifest(self.repo, internal)
        replay['defaults']['base_branch'] = 'replay-version'
        internal.write_text(json.dumps(replay, indent=2) + '\n')
        self.git('add', '-f', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: track replay manifest')
        parent = self.git('rev-parse', 'HEAD')
        branch = 'integration/index-control-retry'
        replay['integration'] = {
            'branch': branch,
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        replay['stacks'] = []
        internal.write_text(json.dumps(replay, indent=2) + '\n')
        self.git('add', '-f', '.syncwheel/manifest.json')
        self.git('commit', '-q', '-m', 'test: bind replay integration branch')
        parent = self.git('rev-parse', 'HEAD')
        self.git('branch', branch, parent)
        self.git('switch', '-q', branch)
        desired = json.loads(json.dumps(replay))
        desired['defaults']['base_branch'] = 'control-version'
        external = self.tmp / 'index-control-manifest.json'
        external.write_text(json.dumps(desired, indent=2) + '\n')

        def crash_after_ref(stage):
            if stage == 'ref_updated':
                raise RuntimeError('simulated crash after ref CAS')

        with module.manifest_write_transaction(self.repo, external):
            with mock.patch.object(
                module, 'control_manifest_io_checkpoint', side_effect=crash_after_ref,
            ):
                with self.assertRaisesRegex(RuntimeError, 'after ref CAS'):
                    module.restore_control_manifest_after_integration_rebuild(
                        self.repo, external, desired, parent, 'in-place'
                    )

        control_commit = self.git('rev-parse', branch)
        self.git('read-tree', control_commit)
        self.assertEqual(self.git('write-tree'), module.ref_tree(self.repo, control_commit))
        worktree_manifest = json.loads(internal.read_text())
        self.assertEqual(
            module.manifest_digest(worktree_manifest), module.manifest_digest(replay)
        )

        with module.manifest_write_transaction(self.repo, external):
            recovered = module.recover_incomplete_control_manifest_persistence(
                self.repo, external, desired
            )

        self.assertEqual(module.manifest_digest(recovered), module.manifest_digest(desired))
        self.assertEqual(self.tracked_status(), '')
        aligned = json.loads(internal.read_text())
        self.assertEqual(module.manifest_digest(aligned), module.manifest_digest(desired))

    def test_control_manifest_recovery_repairs_incomplete_ledger_tail_before_read(self):
        module = self.load_syncwheel_module()
        baseline, _ = module.load_manifest(
            self.repo, self.repo / '.syncwheel' / 'manifest.json'
        )
        parent = self.git('rev-parse', 'HEAD')
        branch = 'integration/ledger-tail-retry'
        desired = json.loads(json.dumps(baseline))
        desired['integration'] = {
            'branch': branch,
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        desired['stacks'] = []
        stale = json.loads(json.dumps(desired))
        stale['defaults']['base_branch'] = 'stale-before-retry'
        external = self.tmp / 'ledger-tail-manifest.json'
        external.write_text(json.dumps(stale, indent=2) + '\n')
        self.git('branch', branch, parent)

        def crash_after_ref(stage):
            if stage == 'ref_updated':
                raise RuntimeError('simulated crash after ref CAS')

        with module.manifest_write_transaction(self.repo, external):
            with mock.patch.object(
                module, 'control_manifest_io_checkpoint', side_effect=crash_after_ref,
            ):
                with self.assertRaisesRegex(RuntimeError, 'after ref CAS'):
                    module.restore_control_manifest_after_integration_rebuild(
                        self.repo, external, desired, parent, 'plumbing'
                    )

        segment = sorted(module.ledger_events_dir(self.repo, external).glob('*.jsonl'))[-1]
        with segment.open('ab') as handle:
            handle.write(b'{"type":"manifest_saved"')

        with module.manifest_write_transaction(self.repo, external):
            recovered = module.recover_incomplete_control_manifest_persistence(
                self.repo, external, stale
            )

        self.assertEqual(module.manifest_digest(recovered), module.manifest_digest(desired))
        self.assertTrue(segment.read_bytes().endswith(b'\n'))
        events = module.load_ledger_events(self.repo, external)
        self.assertEqual(
            [event['type'] for event in events],
            ['control_manifest_persistence_intent', 'manifest_saved'],
        )

    def test_control_manifest_receipts_distinguish_identical_rebuild_operations(self):
        module = self.load_syncwheel_module()
        baseline, _ = module.load_manifest(
            self.repo, self.repo / '.syncwheel' / 'manifest.json'
        )
        parent = self.git('rev-parse', 'HEAD')
        branch = 'integration/repeated-control'
        desired = json.loads(json.dumps(baseline))
        desired['integration'] = {
            'branch': branch,
            'base': parent,
            'strategy': 'cherry-pick',
            'stacks': [],
        }
        desired['stacks'] = []
        external = self.tmp / 'repeated-control-manifest.json'
        external.write_text(json.dumps(desired, indent=2) + '\n')
        self.git('branch', branch, parent)

        with module.manifest_write_transaction(self.repo, external):
            module.restore_control_manifest_after_integration_rebuild(
                self.repo, external, desired, parent, 'plumbing',
                reason='first reviewed rebuild', command='syncwheel int rebuild',
            )
        control_commit = self.git('rev-parse', branch)
        self.git('update-ref', f'refs/heads/{branch}', parent, control_commit)
        with module.manifest_write_transaction(self.repo, external):
            module.restore_control_manifest_after_integration_rebuild(
                self.repo, external, desired, parent, 'plumbing',
                reason='second reviewed rebuild', command='syncwheel int rebuild',
            )
        with module.manifest_write_transaction(self.repo, external):
            module.recover_incomplete_control_manifest_persistence(
                self.repo, external, desired
            )

        events = module.load_ledger_events(self.repo, external)
        receipts = [
            event for event in events
            if event['type'] == 'manifest_saved'
            and event['payload'].get('control_commit') == control_commit
        ]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(
            {event['payload']['reason'] for event in receipts},
            {'first reviewed rebuild', 'second reviewed rebuild'},
        )
        self.assertEqual(len({event['payload']['operation_id'] for event in receipts}), 2)
        self.assertEqual(len({event['idempotency_key'] for event in receipts}), 2)

    def test_every_parser_command_declares_its_entrypoint_behavior(self):
        module = self.load_syncwheel_module()
        parser = module.build_parser()
        table = module.entrypoint_behavior_table()
        commands = module.command_behavior_table()
        functions = set()
        for node in module.command_parser_nodes(parser):
            function = node.get_default('func')
            if function is not None:
                functions.add(function)

        self.assertTrue(functions)
        self.assertEqual(functions - set(table), set())
        self.assertEqual(set(commands), functions)
        for function in functions:
            with self.subTest(command=function.__qualname__):
                for rule in ('mutates', 'manifestMutates'):
                    module.mutation_rule_requested(
                        table[function][rule], SimpleNamespace()
                    )

    def test_int_rebuild_is_classified_as_a_manifest_mutation(self):
        module = self.load_syncwheel_module()

        self.assertTrue(module.manifest_mutation_requested(SimpleNamespace(
            func=module.command_int_rebuild, dry_run=False,
        )))

    def test_ai_managed_int_rebuild_requires_an_operator_reason(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        manifest = self.read_manifest()
        manifest['authority'] = {
            'mode': 'ai-managed',
            'allow': ['source_change'],
            'deny': ['destructive_rewrite'],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')

        result = self.run_cli('int', 'rebuild', '--in-place', expected=2)

        self.assertIn('requires --reason for an ai-managed repository', result.stderr)

    def test_control_manifest_event_has_actor_reason_and_command(self):
        module = self.load_syncwheel_module()
        control, _ = module.load_manifest(self.repo, self.repo / '.syncwheel' / 'manifest.json')

        payload = module.control_manifest_event_payload(
            self.repo, self.repo / '.syncwheel' / 'manifest.json', control,
            'abc123', 'in-place', 'operator-request', 'int rebuild', None,
        )

        self.assertEqual(payload['actor'], 'Syncwheel Fixture <syncwheel@example.com>')
        self.assertEqual(payload['reason'], 'operator-request')
        self.assertEqual(payload['command'], 'int rebuild')
        self.assertEqual(payload['control_commit'], 'abc123')

    def test_control_manifest_difference_reports_order_base_commit_and_configuration(self):
        module = self.load_syncwheel_module()
        expected, _ = module.load_manifest(self.repo, self.repo / '.syncwheel' / 'manifest.json')
        observed = json.loads(json.dumps(expected))
        observed['stacks'].reverse()
        observed['stacks'][0]['base'] = 'different-base'
        observed['stacks'][0]['commits'] = ['different-commit']
        observed['stacks'][0]['state'] = 'draft'

        detail = module.control_manifest_difference(expected, observed)

        self.assertIn('stack order differs', detail)
        self.assertIn('base differs', detail)
        self.assertIn('commits differ', detail)
        self.assertIn('configuration differs', detail)

    def test_control_manifest_preflight_names_an_executable_restore_command(self):
        manifest_path = self.repo / '.syncwheel' / 'manifest.json'
        module = self.load_syncwheel_module()
        control, _ = module.load_manifest(self.repo, manifest_path)
        divergent = json.loads(manifest_path.read_text())
        divergent['stacks'].reverse()
        manifest_path.write_text(json.dumps(divergent, indent=2) + '\n')

        with self.assertRaises(module.SyncwheelError) as raised:
            module.preflight_control_manifest_digest(self.repo, manifest_path, control)

        message = str(raised.exception)
        self.assertIn('expected=$(mktemp', message)
        self.assertIn('diff -u', message)
        self.assertIn(str(manifest_path), message)
        self.assertIn(module.manifest_digest(control), message)
        self.assertNotIn('cp ', message)

    def test_validate_fails_for_unknown_integration_strategy(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['integration']['strategy'] = 'octopus'
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        result = self.run_cli('validate', expected=1)
        self.assertIn('integration strategy must be one of', result.stdout + result.stderr)

    def test_validate_fails_when_commit_is_missing(self):
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        data = json.loads(manifest.read_text())
        data['stacks'][0]['commits'].append('deadbeef')
        manifest.write_text(json.dumps(data, indent=2) + '\n')
        result = self.run_cli('validate', expected=1)
        self.assertIn('missing commit', result.stdout + result.stderr)

    def test_repo_alias_can_be_used_with_short_repo_flag(self):
        self.run_cli('repo', 'add', 'fixture', str(self.repo), expected=0)
        result = self.run_cli('status', '-r', 'fixture', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertTrue(data['manifest_present'])

    def test_short_repo_flag_accepts_direct_path(self):
        result = self.run_cli('status', '-r', str(self.repo), '--json', expected=0)
        data = json.loads(result.stdout)
        self.assertTrue(data['manifest_present'])

    def test_common_short_json_flags_are_accepted(self):
        commands = [
            ('status', '-j'),
            ('plan', '-j'),
            ('check', '-F', '-j'),
            ('repo', 'tracking', 'status', '-j'),
        ]

        for command in commands:
            with self.subTest(command=command):
                result = self.run_cli(*command, expected=0)
                json.loads(result.stdout)

    def test_repo_alias_can_store_default_manifest_path(self):
        custom_manifest = self.tmp / 'custom-manifest.json'
        manifest = self.repo / '.syncwheel' / 'manifest.json'
        custom_manifest.write_text(manifest.read_text())

        self.run_cli('repo', 'add', 'fixture2', str(self.repo), '--manifest', str(custom_manifest), expected=0)
        result = self.run_cli('status', '-r', 'fixture2', '--json', expected=0)
        data = json.loads(result.stdout)
        self.assert_path_equal(data['manifest_path'], custom_manifest)

    def test_legacy_script_entrypoint_still_reports_version(self):
        result = self.run_custom_cli(CLI, '--version', expected=0, cwd=REPO_ROOT)

        self.assertIn(f"syncwheel {(REPO_ROOT / 'VERSION').read_text().strip()}", result.stdout)

    def test_install_kind_detection_identifies_git_checkout(self):
        fixture = self.init_syncwheel_install_fixture()
        syncwheel = self.load_syncwheel_module()

        detected = syncwheel.detect_syncwheel_install(root=fixture['install'], source_path=fixture['cli'])

        self.assertEqual(detected['kind'], 'git-clone')
        self.assertTrue(detected['git_repo'])
        self.assertEqual(detected['install_root'], fixture['install'])

    def test_install_kind_detection_identifies_plain_script_without_git(self):
        syncwheel = self.load_syncwheel_module()
        script_root = self.tmp / 'standalone-syncwheel'
        script_path = script_root / 'scripts' / 'syncwheel.py'
        script_path.parent.mkdir(parents=True)
        script_path.write_text('# placeholder\n')

        detected = syncwheel.detect_syncwheel_install(
            source_path=script_path,
            prefix=self.tmp / 'not-a-tool-venv',
            env={},
        )

        self.assertEqual(detected['kind'], 'script')
        self.assertFalse(detected['git_repo'])
        self.assertEqual(detected['install_root'], script_root)

    def test_install_kind_detection_identifies_uv_tool_environment(self):
        syncwheel = self.load_syncwheel_module()
        tool_dir = self.tmp / 'uv-tools'
        prefix = tool_dir / 'syncwheel'
        source_path = prefix / 'lib' / 'python3.12' / 'site-packages' / 'syncwheel.py'
        source_path.parent.mkdir(parents=True)
        source_path.write_text('# placeholder\n')
        (prefix / 'pyvenv.cfg').write_text('home = /usr/bin\n')

        detected = syncwheel.detect_syncwheel_install(
            source_path=source_path,
            prefix=prefix,
            env={'UV_TOOL_DIR': str(tool_dir)},
        )

        self.assertEqual(detected['kind'], 'uv-tool')
        self.assertFalse(detected['git_repo'])
        self.assertEqual(detected['install_root'], prefix)

    def test_self_update_command_selection_per_install_kind(self):
        syncwheel = self.load_syncwheel_module()

        self.assertEqual(
            syncwheel.build_self_update_commands({'install_kind': 'uv-tool'}),
            [['uv', 'tool', 'upgrade', 'syncwheel']],
        )
        self.assertEqual(
            syncwheel.build_self_update_commands({'install_kind': 'git-clone', 'upstream': 'origin/main'}),
            [['git', 'fetch', '--quiet', 'origin', '--tags'], ['git', 'merge', '--ff-only', 'origin/main']],
        )
        self.assertEqual(
            syncwheel.build_self_update_commands(
                {'install_kind': 'git-clone', 'upstream': 'origin/main'},
                fetch=False,
            ),
            [['git', 'merge', '--ff-only', 'origin/main']],
        )

    def test_remote_version_fetch_parses_version_without_network(self):
        syncwheel = self.load_syncwheel_module()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, _size):
                return b'\n  0.18.0\n'

        with mock.patch.object(syncwheel.urllib.request, 'urlopen', return_value=FakeResponse()) as urlopen:
            version = syncwheel.fetch_remote_version('https://example.invalid/VERSION')

        self.assertEqual(version, '0.18.0')
        urlopen.assert_called_once()

    def test_self_status_reports_script_install_kind_for_non_git_copy(self):
        standalone = self.tmp / 'standalone-syncwheel'
        cli = standalone / 'scripts' / 'syncwheel.py'
        cli.parent.mkdir(parents=True)
        shutil.copy2(CLI, cli)
        (standalone / 'VERSION').write_text('0.18.0\n')

        result = self.run_custom_cli(
            cli,
            'self',
            'status',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(self.tmp / 'standalone-update-state.json'),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.tmp / 'standalone-settings.json'),
            },
            cwd=standalone,
        )
        data = json.loads(result.stdout)

        self.assertEqual(data['status']['install_kind'], 'script')
        self.assertFalse(data['status']['can_self_update'])
        self.assertIn('not a git checkout', data['status']['reason'])

    def test_agentwheel_skill_status_skips_when_agentwheel_is_absent(self):
        syncwheel = self.load_syncwheel_module()

        with mock.patch.object(syncwheel.shutil, 'which', return_value=None):
            status = syncwheel.collect_agentwheel_syncwheel_skill_status(target_root=self.repo)

        self.assertFalse(status['available'])
        self.assertFalse(status['checked'])
        self.assertEqual(status['status'], 'unavailable')
        self.assertIsNone(status['installed'])
        self.assertIn('agentwheel not found', status['note'])

    def test_agentwheel_skill_status_treats_doctor_failure_as_nonfatal(self):
        syncwheel = self.load_syncwheel_module()
        result = subprocess.CompletedProcess(
            args=['agentwheel', 'doctor'],
            returncode=2,
            stdout='',
            stderr='unknown option: --json\n',
        )

        with mock.patch.object(syncwheel.shutil, 'which', return_value='/usr/bin/agentwheel'):
            with mock.patch.object(syncwheel.subprocess, 'run', return_value=result) as run_mock:
                status = syncwheel.collect_agentwheel_syncwheel_skill_status(target_root=self.repo)

        self.assertTrue(status['available'])
        self.assertFalse(status['checked'])
        self.assertEqual(status['status'], 'unknown')
        self.assertIn('unknown option', status['note'])
        run_mock.assert_called_once()

    def test_agentwheel_skill_status_reads_agentwheel_doctor_skills_array(self):
        syncwheel = self.load_syncwheel_module()
        result = subprocess.CompletedProcess(
            args=['agentwheel', 'doctor'],
            returncode=0,
            stdout=json.dumps({
                'skills': [
                    {'name': 'agentwheel', 'status': 'managed', 'present': True},
                    {'name': 'syncwheel', 'status': 'missing', 'present': False},
                ],
            }),
            stderr='',
        )

        with mock.patch.object(syncwheel.shutil, 'which', return_value='/usr/bin/agentwheel'):
            with mock.patch.object(syncwheel.subprocess, 'run', return_value=result):
                status = syncwheel.collect_agentwheel_syncwheel_skill_status(target_root=self.repo)

        self.assertTrue(status['available'])
        self.assertTrue(status['checked'])
        self.assertEqual(status['status'], 'missing')
        self.assertFalse(status['installed'])
        self.assertTrue(status['missing'])

    def test_self_status_reports_missing_agentwheel_skill(self):
        standalone = self.tmp / 'standalone-syncwheel'
        cli = standalone / 'scripts' / 'syncwheel.py'
        cli.parent.mkdir(parents=True)
        shutil.copy2(CLI, cli)
        (standalone / 'VERSION').write_text('0.18.0\n')

        fake_bin = self.tmp / 'fake-bin'
        fake_bin.mkdir()
        args_path = self.tmp / 'agentwheel-args.json'
        payload = json.dumps({'skill': {'name': 'syncwheel', 'installed': False}})
        fake_agentwheel = fake_bin / 'agentwheel'
        fake_agentwheel.write_text(
            '#!/usr/bin/env python3\n'
            'import json\n'
            'import sys\n'
            'from pathlib import Path\n'
            f'Path({str(args_path)!r}).write_text(json.dumps(sys.argv[1:]))\n'
            f'print({payload!r})\n'
        )
        fake_agentwheel.chmod(0o755)
        env = {
            'PATH': f'{fake_bin}{os.pathsep}{os.environ["PATH"]}',
            'SYNCWHEEL_UPDATE_STATE_PATH': str(self.tmp / 'standalone-update-state.json'),
            'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(self.tmp / 'standalone-settings.json'),
        }

        result = self.run_custom_cli(cli, 'self', 'status', '--json', expected=0, extra_env=env, cwd=standalone)
        data = json.loads(result.stdout)
        target_root = str(standalone.resolve())

        self.assertEqual(data['agentwheel_skill']['status'], 'missing')
        self.assertTrue(data['agentwheel_skill']['missing'])
        self.assertEqual(data['agentwheel_skill']['target_root'], target_root)
        self.assertIn('agentwheel install github:NestDevLab/syncwheel', data['agentwheel_skill']['install_command'])
        self.assertEqual(
            json.loads(args_path.read_text()),
            [
                'doctor',
                '--adapter',
                'codex',
                '--local',
                '--target-root',
                target_root,
                '--skill',
                'syncwheel',
                '--source',
                'github:NestDevLab/syncwheel',
                '--json',
            ],
        )

        human = self.run_custom_cli(cli, 'self', 'status', expected=0, extra_env=env, cwd=standalone)
        self.assertIn('agentwheel_skill: missing', human.stdout)
        self.assertIn('recommended: agentwheel install github:NestDevLab/syncwheel', human.stdout)

    def test_self_check_update_reports_newer_version_after_fetch(self):
        fixture = self.init_syncwheel_install_fixture()
        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'check-update',
            '--fetch',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        data = json.loads(result.stdout)
        self.assertTrue(data['update_available'])
        self.assertEqual(data['current_version'], '0.6.0')
        self.assertEqual(data['latest_version'], '0.7.0')

    def test_self_check_update_uses_origin_main_when_install_is_detached(self):
        fixture = self.init_syncwheel_install_fixture()
        subprocess.run(['git', 'checkout', '--detach', 'HEAD'], cwd=fixture['install'], check=True)

        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'check-update',
            '--fetch',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        data = json.loads(result.stdout)
        self.assertEqual(data['branch'], 'DETACHED')
        self.assertIsNone(data['upstream'])
        self.assertFalse(data['can_self_update'])
        self.assertTrue(data['update_available'])
        self.assertEqual(data['latest_version'], '0.7.0')
        self.assertIn('checking against origin/main', data['reason'])

    def test_self_check_update_falls_back_to_remote_head_when_origin_main_is_missing(self):
        fixture = self.init_syncwheel_install_fixture()
        subprocess.run(['git', 'checkout', '--detach', 'HEAD'], cwd=fixture['install'], check=True)
        subprocess.run(['git', 'branch', '-m', 'main', 'trunk'], cwd=fixture['seed'], check=True)
        subprocess.run(['git', 'push', 'origin', 'trunk'], cwd=fixture['seed'], check=True)
        subprocess.run(['git', '--git-dir', str(fixture['origin']), 'symbolic-ref', 'HEAD', 'refs/heads/trunk'], check=True)
        subprocess.run(['git', 'push', 'origin', '--delete', 'main'], cwd=fixture['seed'], check=True)
        subprocess.run(['git', 'update-ref', '-d', 'refs/remotes/origin/main'], cwd=fixture['install'], check=True)

        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'check-update',
            '--fetch',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        data = json.loads(result.stdout)
        self.assertEqual(data['branch'], 'DETACHED')
        self.assertTrue(data['update_available'])
        self.assertEqual(data['latest_version'], '0.7.0')
        self.assertIn('checking against origin/trunk', data['reason'])

    def test_self_update_fast_forwards_install(self):
        fixture = self.init_syncwheel_install_fixture()
        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'update',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        self.assertIn('updated syncwheel: 0.6.0 -> 0.7.0', result.stdout)
        self.assertEqual((fixture['install'] / 'VERSION').read_text().strip(), '0.7.0')

    def test_self_install_hooks_sets_core_hooks_path(self):
        fixture = self.init_syncwheel_install_fixture()

        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'install-hooks',
            expected=0,
            cwd=fixture['install'],
        )

        configured = subprocess.run(
            ['git', 'config', '--get', 'core.hooksPath'],
            cwd=fixture['install'],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(configured, 'githooks')
        self.assertIn('pre_commit: active', result.stdout)

    def test_self_status_reports_hook_status(self):
        fixture = self.init_syncwheel_install_fixture()
        subprocess.run(['git', 'config', 'core.hooksPath', 'githooks'], cwd=fixture['install'], check=True)

        result = self.run_custom_cli(
            fixture['cli'],
            'self',
            'status',
            '--json',
            expected=0,
            extra_env={
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
            },
            cwd=fixture['install'],
        )
        data = json.loads(result.stdout)
        self.assertTrue(data['hooks']['active'])
        self.assertEqual(data['hooks']['configured_hooks_path'], 'githooks')

    def test_startup_notify_mode_emits_update_notice(self):
        fixture = self.init_syncwheel_install_fixture()
        result = self.run_custom_cli(
            fixture['cli'],
            'repo',
            'ls',
            expected=0,
            extra_env={
                'SYNCWHEEL_REPO_REGISTRY': str(fixture['registry']),
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
                'SYNCWHEEL_UPDATE_MODE': 'notify',
                'SYNCWHEEL_UPDATE_INTERVAL_SECONDS': '0',
            },
            cwd=fixture['install'],
        )
        self.assertIn('NOTICE: syncwheel update available (0.6.0 -> 0.7.0)', result.stderr)

    def test_startup_auto_mode_updates_before_normal_command(self):
        fixture = self.init_syncwheel_install_fixture()
        result = self.run_custom_cli(
            fixture['cli'],
            'repo',
            'ls',
            expected=0,
            extra_env={
                'SYNCWHEEL_REPO_REGISTRY': str(fixture['registry']),
                'SYNCWHEEL_UPDATE_STATE_PATH': str(fixture['state']),
                'SYNCWHEEL_UPDATE_SETTINGS_PATH': str(fixture['settings']),
                'SYNCWHEEL_UPDATE_MODE': 'auto',
                'SYNCWHEEL_UPDATE_INTERVAL_SECONDS': '0',
            },
            cwd=fixture['install'],
        )
        self.assertIn('syncwheel auto-updated 0.6.0 -> 0.7.0', result.stderr)
        self.assertEqual((fixture['install'] / 'VERSION').read_text().strip(), '0.7.0')


if __name__ == '__main__':
    unittest.main()
