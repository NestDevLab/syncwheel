"""Native adapter for cooperative test-slot qualification and recovery.

This adapter is deliberately separate from publication and managed-worktree
startup. The selected argv is trusted cooperative test code, not a sandbox.
"""

import hashlib
import json
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import time

from syncwheel_errors import SyncwheelError
from syncwheel_test_slot import CooperativeTestSlot, derive_channel_request


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
    git = lambda *args: command(['git', '--no-optional-locks', *args], cwd=root)
    if Path(git('rev-parse', '--show-toplevel')).resolve() != root:
        raise SyncwheelError('test slot must name the exact physical workspace root')
    admin = Path(git('rev-parse', '--absolute-git-dir')).resolve(strict=True)
    physical = root.stat()
    index = Path(git('rev-parse', '--path-format=absolute', '--git-path', 'index'))
    index_digest = hashlib.sha256(index.read_bytes()).hexdigest() if index.exists() else None
    lockfiles = {}
    for relative in git('ls-files', '-z').split('\0'):
        if relative and Path(relative).name in {'package-lock.json', 'npm-shrinkwrap.json', 'yarn.lock', 'pnpm-lock.yaml', 'uv.lock', 'poetry.lock', 'Cargo.lock', 'go.sum'}:
            item = root / relative
            lockfiles[relative] = hashlib.sha256(item.read_bytes()).hexdigest()
    return {'path': str(root), 'admin': str(admin), 'device': physical.st_dev,
            'inode': physical.st_ino, 'admin_device': admin.stat().st_dev,
            'admin_inode': admin.stat().st_ino, 'head': git('rev-parse', 'HEAD'),
            'tree': git('rev-parse', 'HEAD^{tree}'),
            'clean': not bool(git('status', '--porcelain', '--untracked-files=all', '--ignored=matching')),
            'index_sha256': index_digest,
            'lockfiles': lockfiles}


def observe_tools(argv):
    executable = Path(argv[0])
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise SyncwheelError('reviewed test executable must be an available absolute path')
    git_path = shutil.which('git')
    if git_path is None:
        raise SyncwheelError('Git executable is unavailable for test input observation')
    def identity(path):
        target = Path(path).resolve(strict=True)
        return {'path': str(target), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    import syncwheel_test_slot
    return {'command_executable': identity(executable), 'python': identity(sys.executable),
            'python_version': sys.version, 'python_prefix': sys.prefix,
            'git': identity(git_path), 'git_command_path': str(Path(git_path).absolute()),
            'scope': 'controller modules, controller tools and command entrypoint; payload descendants are caller-controlled',
            'git_version': command([git_path, '--version']),
            'adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'domain_sha256': hashlib.sha256(Path(syncwheel_test_slot.__file__).read_bytes()).hexdigest(),
            'controller_modules': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in sorted(Path(__file__).parent.glob('syncwheel*.py'))}}


class NativeJsonStore:
    def __init__(self, path, configuration):
        self.path, self.configuration = path, configuration

    def load(self):
        return self.configuration.load_private_configuration(self.path, {'active': None, 'receipts': {}})

    def save(self, snapshot):
        return self.configuration.save_private_configuration(self.path, snapshot)


class NativeCooperativeJob:
    def __init__(self, workspace, configuration):
        self.api = configuration
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
            '--property=Type,ExitType,KillMode,RemainAfterExit,WorkingDirectory,Description,Transient,CPUAccounting,MemoryAccounting,Restart')
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
        argv = self.api.test_slot_worker_argv(self.store.path, nonce)
        return {'Type': 'exec', 'ExitType': 'cgroup', 'KillMode': 'control-group',
                'RemainAfterExit': 'yes', 'Restart': 'no', 'CPUAccounting': 'yes', 'MemoryAccounting': 'yes',
                'WorkingDirectory': str(self.workspace),
                'Description': 'Syncwheel cooperative test ' + nonce, 'Transient': 'yes',
                'exec': [sys.executable, argv, False], 'runtime_us': timeout * 1000000}

    def read_manifest_blob(self, object_id):
        result = subprocess.run(['git', 'cat-file', 'blob', object_id], cwd=self.workspace,
                                capture_output=True)
        if result.returncode:
            raise SyncwheelError('immutable test control manifest blob is unavailable')
        check = subprocess.run(['git', 'hash-object', '--stdin'], cwd=self.workspace,
                               input=result.stdout, capture_output=True, check=True)
        if check.stdout.decode().strip() != object_id:
            raise SyncwheelError('immutable test control manifest blob failed object verification')
        return result.stdout

    def derive_request(self, inputs):
        workspace = workspace_observation(self.workspace)
        def validate(manifest, channel):
            self.api.require_channel_materialization_valid(self.workspace, manifest, channel)
            if self.api.ref_tip(self.workspace, channel['branch']) != workspace['head']:
                raise SyncwheelError('test workspace HEAD does not match the selected local channel ref')
        return derive_channel_request(inputs, workspace, read_manifest_blob=self.read_manifest_blob,
                                      validate_channel=validate, observe_tools=observe_tools)

    def start(self, inputs, timeout=300):
        if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            raise SyncwheelError('cooperative test timeout must be between 1 and 3600 seconds')
        request = self.derive_request(inputs)
        operation = self.slot.reserve(request)
        nonce = operation['nonce']
        directory, log = self.paths(nonce)
        directory.mkdir(parents=True, mode=0o700)
        for name in ('home', 'tmp', 'output'):
            (directory / name).mkdir(mode=0o700)
        log.touch(mode=0o600)
        verification = {'channel_projection_pending': True, 'request_digest': operation['request_digest']}
        config = self.expected_configuration(operation, timeout)
        operation = self.slot.starting(nonce, 'syncwheel-test-' + nonce + '.service', config,
                                       verification=verification)
        # Starting is durable before the native request. A failed/uncertain call
        # deliberately keeps the reservation; recovery never calls start again.
        argv = ['systemd-run', '--user', '--no-ask-password', '--expand-environment=no',
                '--unit=' + operation['unit'], '--description=' + config['Description'],
                '--service-type=exec', '--remain-after-exit', '--property=ExitType=cgroup', '--property=Restart=no',
                '--property=KillMode=control-group', '--property=CPUAccounting=yes',
                '--property=MemoryAccounting=yes', '--property=RuntimeMaxSec=' + str(timeout),
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
                preparation_path = self.paths(nonce)[0] / 'preparation.json'
                try:
                    preparation = json.loads(preparation_path.read_text())
                except (OSError, ValueError):
                    preparation = {'available': False}
                valid_preparation = (
                    preparation.get('nonce') == nonce
                    and preparation.get('invocation') == operation['binding']['invocation']
                    and preparation.get('request_digest') == operation['request_digest']
                    and preparation.get('channel_projection_tree') == operation['request']['tree']
                    and preparation.get('passed') is True
                )
                try:
                    tools_unchanged = observe_tools(operation['request']['argv']) == operation['request']['tools']
                except (OSError, SyncwheelError):
                    tools_unchanged = False
                return self.slot.finish(nonce, confirmed, cgroup_exists=exists,
                    cgroup_populated=populated, log_digest=hashlib.sha256(log.read_bytes()).hexdigest(),
                    measurements={'cpu_ns': observed['cpu_ns'], 'memory_peak': observed['memory_peak']},
                    inputs_unchanged=valid_preparation and tools_unchanged, verification=preparation)
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


def worker(path, nonce, configuration):
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
            if os.environ.get('INVOCATION_ID') != operation['binding']['invocation']:
                raise SyncwheelError('worker invocation does not match the durable native binding')
            break
        if operation['state'] != 'starting':
            raise SyncwheelError('test worker has no valid startup handshake')
        time.sleep(0.02)
    request = operation['request']
    directory = path.parent / 'syncwheel-test-jobs' / nonce
    preparation = {'nonce': nonce, 'invocation': operation['binding']['invocation'],
                   'request_digest': operation['request_digest'], 'passed': False,
                   'method': 'existing ReplayEngine channel projection'}
    started = time.monotonic()
    try:
        environment = dict(request.get('environment', {}))
        environment.update(HOME=str(directory/'home'), TMPDIR=str(directory/'tmp'),
                           TMP=str(directory/'tmp'), TEMP=str(directory/'tmp'),
                           XDG_CONFIG_HOME=str(directory/'home/config'),
                           XDG_STATE_HOME=str(directory/'home/state'),
                           XDG_CACHE_HOME=str(directory/'home/cache'),
                           SYNCWHEEL_UPDATE_MODE='off', PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0',
                           SYNCWHEEL_UPDATE_SETTINGS_PATH=str(directory/'home/settings.json'),
                           SYNCWHEEL_REPO_REGISTRY=str(directory/'home/registry.json'),
                           SYNCWHEEL_TEST_OUTPUT=str(directory/'output'))
        # Preparation also runs in the job's private HOME/temp namespaces, not
        # the manager's ambient application settings or cache directories.
        preparation_environment = dict(environment)
        preparation_environment['PATH'] = str(Path(request['tools']['git_command_path']).parent) + os.pathsep + os.defpath
        os.environ.clear()
        os.environ.update(preparation_environment)
        if observe_tools(request['argv']) != request['tools']:
            raise SyncwheelError('test executable or worker toolchain changed before execution')
        if workspace_observation(request['workspace']['path']) != request['workspace']:
            raise SyncwheelError('test workspace changed before worker execution')
        projected = configuration.materialize_channel_tree(
            Path(request['workspace']['path']), request['verified_provenance']['channel'])
        preparation.update(channel_projection_tree=projected,
                           preparation_seconds=time.monotonic()-started)
        if projected != request['tree']:
            raise SyncwheelError('test candidate tree differs from the selected channel projection')
        if workspace_observation(request['workspace']['path']) != request['workspace']:
            raise SyncwheelError('test workspace changed during input preparation')
        preparation['passed'] = True
        configuration.atomic_write_private_json(directory/'preparation.json', preparation, indent=2)
        test_started = time.monotonic()
        result = subprocess.run(request['argv'], cwd=request['workspace']['path'], env=environment)
        preparation.update(test_seconds=time.monotonic()-test_started, command_exit=result.returncode)
        configuration.atomic_write_private_json(directory/'preparation.json', preparation, indent=2)
        return result.returncode
    except Exception as error:
        preparation.update(passed=False, detail=str(error), preparation_seconds=time.monotonic()-started)
        configuration.atomic_write_private_json(directory/'preparation.json', preparation, indent=2)
        raise


def execute_test_slot(args, configuration):
    if args.action == '_worker':
        return worker(args.workspace, args.input, configuration)
    job = NativeCooperativeJob(args.workspace, configuration)
    if args.action == 'status':
        result = dict(job.store.load())
    elif args.action == 'inspect':
        result = job.derive_request(json.loads(Path(args.input).read_text()))
    elif args.action == 'run':
        request = json.loads(Path(args.input).read_text())
        operation = job.start(request, args.timeout)
        result = job.recover(operation['nonce'], wait=True)
    elif args.action == 'recover':
        result = job.recover(args.input, wait=True)
    else:
        job.cleanup(args.input)
        result = {'nonce': args.input, 'cleanup': 'succeeded'}
    return result

