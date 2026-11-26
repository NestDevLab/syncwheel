"""Native adapter for cooperative test-slot qualification and recovery.

This adapter is deliberately separate from publication and managed-worktree
startup. The selected argv is trusted cooperative test code, not a sandbox.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from syncwheel_errors import SyncwheelError
from syncwheel_test_slot import CooperativeTestSlot


def command(argv, *, cwd=None, env=None):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise SyncwheelError(result.stderr.strip() or f'command failed: {argv[0]}')
    return result.stdout.strip()


def workspace_observation(path):
    selectors = {'GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                 'GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS',
                 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_NAMESPACE',
                 'GIT_SHALLOW_FILE', 'GIT_REPLACE_REF_BASE', 'GIT_CEILING_DIRECTORIES',
                 'GIT_DISCOVERY_ACROSS_FILESYSTEM'}
    if selectors.intersection(os.environ) or any(key.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')) for key in os.environ):
        raise SyncwheelError('test-slot Git observation requires an environment without Git selectors')
    root = Path(path).resolve(strict=True)
    git = lambda *args: command(['git', *args], cwd=root)
    if Path(git('rev-parse', '--show-toplevel')).resolve() != root:
        raise SyncwheelError('test slot must name the exact physical workspace root')
    admin = Path(git('rev-parse', '--absolute-git-dir')).resolve(strict=True)
    physical = root.stat()
    lockfiles = {}
    for relative in git('ls-files', '-z').split('\0'):
        if relative and Path(relative).name in {'package-lock.json', 'npm-shrinkwrap.json', 'yarn.lock', 'pnpm-lock.yaml', 'uv.lock', 'poetry.lock', 'Cargo.lock', 'go.sum'}:
            item = root / relative
            lockfiles[relative] = hashlib.sha256(item.read_bytes()).hexdigest()
    return {'path': str(root), 'admin': str(admin), 'device': physical.st_dev,
            'inode': physical.st_ino, 'admin_device': admin.stat().st_dev,
            'admin_inode': admin.stat().st_ino, 'head': git('rev-parse', 'HEAD'),
            'tree': git('rev-parse', 'HEAD^{tree}'),
            'clean': not bool(git('status', '--porcelain', '--untracked-files=all')),
            'lockfiles': lockfiles}


class NativeJsonStore:
    def __init__(self, path, configuration):
        self.path, self.configuration = path, configuration

    def load(self):
        return self.configuration.load_private_configuration(self.path, {'active': None, 'receipts': {}})

    def save(self, snapshot):
        return self.configuration.save_private_configuration(self.path, snapshot)


class NativeCooperativeJob:
    def __init__(self, workspace, configuration):
        self.workspace = Path(workspace).resolve(strict=True)
        self.initial = workspace_observation(self.workspace)
        self.admin = Path(self.initial['admin'])
        # Git's canonical worktree admin directory is invariant under aliases.
        # There is no caller-selected alternate slot or lock name.
        self.store = NativeJsonStore(self.admin / 'syncwheel-test-slot.json', configuration)
        self.slot = CooperativeTestSlot(self.store, lambda: workspace_observation(self.workspace))
        runtime = Path('/run/user') / str(os.geteuid())
        bus = runtime / 'bus'
        info = bus.stat()
        if info.st_uid != os.geteuid() or not stat.S_ISSOCK(info.st_mode):
            raise SyncwheelError('native test-slot user manager identity is unavailable')
        self.manager_env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime),
                                DBUS_SESSION_BUS_ADDRESS='unix:path=' + str(bus))

    def manager(self, *argv):
        return command(list(argv), env=self.manager_env)

    def dbus_property(self, unit, interface, property_name):
        path = json.loads(self.manager('busctl', '--user', '--json=short', 'call',
            'org.freedesktop.systemd1', '/org/freedesktop/systemd1',
            'org.freedesktop.systemd1.Manager', 'GetUnit', 's', unit))['data'][0]
        return json.loads(self.manager('busctl', '--user', '--json=short', 'get-property',
            'org.freedesktop.systemd1', path, interface, property_name))['data']

    def configuration(self, unit):
        values = self.manager('systemctl', '--user', 'show', unit,
            '--property=Type,ExitType,KillMode,RemainAfterExit,WorkingDirectory,Description,Transient')
        config = dict(row.split('=', 1) for row in values.splitlines() if '=' in row)
        starts = self.dbus_property(unit, 'org.freedesktop.systemd1.Service', 'ExecStart')
        if len(starts) != 1:
            raise SyncwheelError('native test unit must contain exactly one reviewed ExecStart')
        config['exec'] = starts[0][:3]
        config['runtime_us'] = self.dbus_property(unit, 'org.freedesktop.systemd1.Service', 'RuntimeMaxUSec')
        return config

    def observe(self, operation):
        unit = operation['unit']
        properties = 'Id,InvocationID,ControlGroup,Job,MainPID,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,CPUUsageNSec,MemoryPeak'
        def values():
            output = self.manager('systemctl', '--user', 'show', unit, '--property=' + properties)
            return dict(row.split('=', 1) for row in output.splitlines() if '=' in row)
        before = values()
        config = self.configuration(unit)
        after = values()
        if before.get('InvocationID') != after.get('InvocationID') or not after.get('InvocationID'):
            raise SyncwheelError('native test invocation is unavailable or changed during observation')
        return {'unit': after['Id'], 'invocation': after['InvocationID'], 'configuration': config,
                'cgroup': after.get('ControlGroup'), 'job': after.get('Job'),
                'main_pid': int(after.get('MainPID', '-1')), 'active': after.get('ActiveState'),
                'substate': after.get('SubState'), 'result': after.get('Result'),
                'main_code': int(after['ExecMainCode']) if 'ExecMainCode' in after else None,
                'main_status': int(after['ExecMainStatus']) if 'ExecMainStatus' in after else None,
                'cpu_ns': after.get('CPUUsageNSec'), 'memory_peak': after.get('MemoryPeak')}

    def paths(self, nonce):
        directory = self.admin / 'syncwheel-test-jobs' / nonce
        return directory, directory / 'output.log'

    def expected_configuration(self, operation, timeout):
        nonce = operation['nonce']
        argv = [str(Path(sys.executable).resolve()), str(Path(__file__).resolve()), '_worker', str(self.store.path), nonce]
        return {'Type': 'exec', 'ExitType': 'cgroup', 'KillMode': 'control-group',
                'RemainAfterExit': 'yes', 'WorkingDirectory': str(self.workspace),
                'Description': 'Syncwheel cooperative test ' + nonce, 'Transient': 'yes',
                'exec': [str(Path(sys.executable).resolve()), argv, False], 'runtime_us': timeout * 1000000}

    def start(self, request, timeout=300):
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise SyncwheelError('cooperative test timeout must be between 1 and 3600 seconds')
        operation = self.slot.reserve(request)
        nonce = operation['nonce']
        directory, log = self.paths(nonce)
        directory.mkdir(parents=True, mode=0o700)
        for name in ('home', 'tmp', 'output'):
            (directory / name).mkdir(mode=0o700)
        log.touch(mode=0o600)
        config = self.expected_configuration(operation, timeout)
        operation = self.slot.starting(nonce, 'syncwheel-test-' + nonce + '.service', config)
        # Starting is durable before the native request. A failed/uncertain call
        # deliberately keeps the reservation; recovery never calls start again.
        argv = ['systemd-run', '--user', '--no-ask-password', '--expand-environment=no',
                '--unit=' + operation['unit'], '--description=' + config['Description'],
                '--service-type=exec', '--remain-after-exit', '--property=ExitType=cgroup',
                '--property=KillMode=control-group', '--property=RuntimeMaxSec=' + str(timeout),
                '--property=StandardOutput=append:' + str(log),
                '--property=StandardError=append:' + str(log), '--working-directory=' + str(self.workspace),
                *config['exec'][1]]
        self.manager(*argv)
        return self.recover(nonce, wait=False)

    def population(self, binding):
        relative = binding['cgroup']
        prefix = '/user.slice/user-' + str(os.geteuid()) + '.slice/'
        if not relative.startswith(prefix) or '..' in Path(relative).parts:
            raise SyncwheelError('bound test cgroup does not belong to this user manager')
        path = Path('/sys/fs/cgroup') / relative.lstrip('/')
        try:
            data = (path / 'cgroup.events').read_text()
        except FileNotFoundError:
            return False, None
        values = dict(row.split() for row in data.splitlines())
        return True, {'0': False, '1': True}.get(values.get('populated'))

    def recover(self, nonce, wait=False):
        while True:
            snapshot = self.store.load()
            receipt = snapshot.get('receipts', {}).get(nonce)
            if receipt is not None:
                return receipt
            operation = snapshot.get('active')
            if not operation or operation.get('nonce') != nonce:
                raise SyncwheelError('native test reservation is absent or belongs to another operation')
            if operation['state'] not in {'starting', 'running'}:
                raise SyncwheelError('reserved test job has not been dispatched')
            observed = self.observe(operation)
            if operation['state'] == 'starting':
                operation = self.slot.bind(nonce, observed)
            binding = operation['binding']
            if any(observed.get(key) != binding[key] for key in ('unit', 'invocation', 'configuration')):
                raise SyncwheelError('native test identity/configuration is unknown; keep reservation')
            exists, populated = self.population(binding)
            from syncwheel_test_slot import terminal_observation
            terminal = terminal_observation(operation['binding'], observed,
                cgroup_exists=exists, cgroup_populated=populated)
            if terminal is not None:
                # Check twice before deciding that the retained invocation is
                # stable; unknown/missing data never becomes an idle witness.
                confirmed = self.observe(operation)
                if confirmed != observed:
                    continue
                _, log = self.paths(nonce)
                with log.open('rb') as handle:
                    os.fsync(handle.fileno())
                return self.slot.finish(nonce, confirmed, cgroup_exists=exists,
                    cgroup_populated=populated, log_digest=hashlib.sha256(log.read_bytes()).hexdigest(),
                    measurements={'cpu_ns': observed['cpu_ns'], 'memory_peak': observed['memory_peak']})
            if (observed.get('active'), observed.get('substate')) in {('active', 'exited'), ('failed', 'failed')}:
                raise SyncwheelError('native test terminal evidence is incomplete; keep reservation')
            if not wait:
                return operation
            time.sleep(0.05)

    def cleanup(self, nonce):
        snapshot = self.store.load()
        receipt = snapshot.get('receipts', {}).get(nonce)
        if receipt is None:
            raise SyncwheelError('test unit cleanup requires a committed terminal receipt')
        operation = {'unit': receipt['binding']['unit']}
        current = self.observe(operation)
        if current['invocation'] != receipt['binding']['invocation'] or current['configuration'] != receipt['binding']['configuration']:
            raise SyncwheelError('test unit changed after receipt; refusing cleanup')
        self.manager('systemctl', '--user', 'stop', operation['unit'])


def worker(path, nonce):
    path = Path(path)
    while True:
        snapshot = json.loads(path.read_text())
        operation = snapshot.get('active')
        if not operation or operation['nonce'] != nonce:
            raise SyncwheelError('test reservation disappeared before worker dispatch')
        if operation['state'] == 'running':
            cgroups = Path('/proc/self/cgroup').read_text().splitlines()
            own_cgroup = next(row[3:] for row in cgroups if row.startswith('0::'))
            if own_cgroup != operation['binding']['cgroup']:
                raise SyncwheelError('worker cgroup does not match the durable native binding')
            break
        if operation['state'] != 'starting':
            raise SyncwheelError('test worker has no valid startup handshake')
        time.sleep(0.02)
    request = operation['request']
    if workspace_observation(request['workspace']['path']) != request['workspace']:
        raise SyncwheelError('test workspace changed before worker execution')
    directory = path.parent / 'syncwheel-test-jobs' / nonce
    environment = dict(request.get('environment', {}))
    if any(key.startswith(('GIT_', 'LD_', 'PYTHON')) or key in {'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'} for key in environment):
        raise SyncwheelError('test environment cannot select Git, loader, Python or manager namespaces')
    environment.update(HOME=str(directory/'home'), TMPDIR=str(directory/'tmp'),
                       TMP=str(directory/'tmp'), TEMP=str(directory/'tmp'),
                       SYNCWHEEL_TEST_OUTPUT=str(directory/'output'))
    return subprocess.run(request['argv'], cwd=request['workspace']['path'], env=environment).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'recover', 'cleanup', '_worker'])
    parser.add_argument('workspace')
    parser.add_argument('input', help='reviewed request JSON for run; recorded nonce for recover/cleanup')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    if args.action == '_worker':
        return worker(args.workspace, args.input)
    import syncwheel as configuration
    job = NativeCooperativeJob(args.workspace, configuration)
    if args.action == 'run':
        request = json.loads(Path(args.input).read_text())
        operation = job.start(request, args.timeout)
        result = job.recover(operation['nonce'], wait=True)
    elif args.action == 'recover':
        result = job.recover(args.input, wait=True)
    else:
        job.cleanup(args.input)
        result = {'nonce': args.input, 'cleanup': 'succeeded'}
    print(json.dumps(result, indent=2))
    return 0 if result.get('qualified', True) else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SyncwheelError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
