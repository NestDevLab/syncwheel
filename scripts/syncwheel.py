#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import importlib.metadata
import json
import os
import shutil
import shlex
import tempfile
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class SyncwheelError(Exception):
    pass


ENV_REGISTRY_PATH = 'SYNCWHEEL_REPO_REGISTRY'
ENV_REPO = 'SYNCWHEEL_REPO'
ENV_PERSONAL = 'SYNCWHEEL_PERSONAL'
ENV_UPDATE_MODE = 'SYNCWHEEL_UPDATE_MODE'
ENV_UPDATE_INTERVAL_SECONDS = 'SYNCWHEEL_UPDATE_INTERVAL_SECONDS'
ENV_UPDATE_STATE_PATH = 'SYNCWHEEL_UPDATE_STATE_PATH'
ENV_UPDATE_SETTINGS_PATH = 'SYNCWHEEL_UPDATE_SETTINGS_PATH'
ENV_REMOTE_VERSION_URL = 'SYNCWHEEL_REMOTE_VERSION_URL'
ENV_UV_TOOL_SOURCE = 'SYNCWHEEL_UV_TOOL_SOURCE'
PROFILE_FILENAME = 'profile.local.json'
INTEGRATION_STRATEGIES = {'cherry-pick', 'merge-stacks'}
INTEGRATION_MEMBERSHIP_LEGACY = 'legacy'
INTEGRATION_MEMBERSHIP_REQUIRED = 'required'
INTEGRATION_MEMBERSHIP_POLICIES = {
    INTEGRATION_MEMBERSHIP_LEGACY,
    INTEGRATION_MEMBERSHIP_REQUIRED,
}
STACK_STATES = {'draft', 'published'}
MANIFEST_VERSIONS = {1, 2}
MANIFEST_VERSION_LEGACY = 1
MANIFEST_VERSION_COORDINATED = 2
COORDINATION_MODES = {'active-active', 'disabled'}
COORDINATION_STATE_SCHEMA_VERSION = 2
COORDINATION_STATE_FILE = '.syncwheel/coordination-state.json'
COORDINATION_STATE_PREFIX = 'syncwheel/state/'
COORDINATION_REMOTE_ROLE_CANONICAL = 'canonical'
COORDINATION_REMOTE_ROLE_PUBLICATION = 'publication'
COORDINATION_LEASE_SECONDS = 5 * 60
COORDINATION_GIT_IDENTITY_CONFIG = [
    '-c',
    'user.name=Syncwheel Coordination',
    '-c',
    'user.email=coordination@syncwheel.invalid',
]
COORDINATION_GIT_IDENTITY_ENV = {
    'GIT_AUTHOR_NAME': 'Syncwheel Coordination',
    'GIT_AUTHOR_EMAIL': 'coordination@syncwheel.invalid',
    'GIT_COMMITTER_NAME': 'Syncwheel Coordination',
    'GIT_COMMITTER_EMAIL': 'coordination@syncwheel.invalid',
}
DEFAULT_COORDINATION_GC = {
    'worktree_grace_days': 7,
    'backup_retention_days': 30,
    'backup_keep': 2,
}
DEFAULT_INTEGRATION_BRANCH = 'main-integration'
SYNCWHEEL_TRACKING_VALUES = {'git-tracked', 'local-only'}
SYNCWHEEL_TRACKING_GIT_TRACKED = 'git-tracked'
SYNCWHEEL_TRACKING_LOCAL_ONLY = 'local-only'
DEFAULT_SYNCWHEEL_WORKTREE_ROOT = '.syncwheel/wt'
LEGACY_SYNCWHEEL_WORKTREE_ROOTS = ('var/syncwheel',)
UPDATE_MODES = {'off', 'notify', 'auto'}
RECONCILE_MODES = {'standard', 'resume'}
LEDGER_SCHEMA_VERSION = 1
LEDGER_SEGMENT_MAX_EVENTS = 256
SYNCWHEEL_LOCAL_EXCLUDE_PATTERN = '.syncwheel/'
SYNCWHEEL_LOCAL_EXCLUDE_MARKER = '# syncwheel local metadata'
SYNCWHEEL_LOCAL_EXCLUDE_END_MARKER = '# end syncwheel local metadata'
SYNCWHEEL_GITIGNORE_MARKER = '# syncwheel managed metadata'
SYNCWHEEL_GITIGNORE_END_MARKER = '# end syncwheel managed metadata'
DEFAULT_UPDATE_MODE = 'notify'
DEFAULT_UPDATE_INTERVAL_SECONDS = 6 * 60 * 60
UPSTREAM_REPO_URL = 'https://github.com/NestDevLab/syncwheel'
UPSTREAM_DEFAULT_BRANCH = 'main'
UV_TOOL_NAME = 'syncwheel'
AGENTWHEEL_SYNCWHEEL_SKILL_SOURCE = 'github:NestDevLab/syncwheel'
AGENTWHEEL_SYNCWHEEL_SKILL_NAME = 'syncwheel'
AGENTWHEEL_SYNCWHEEL_ADAPTER = 'codex'
AGENTWHEEL_SYNCWHEEL_INSTALLATION_TYPE = 'local'
AGENTWHEEL_DOCTOR_TIMEOUT_SECONDS = 10
SYNCWHEEL_HOOKS_PATH = 'githooks'
FALLBACK_GIT_IDENTITY_CONFIG = [
    '-c',
    'user.name=Syncwheel',
    '-c',
    'user.email=syncwheel@example.com',
]
YELLOW = '\033[33m'
RESET = '\033[0m'
WARNED_GIT_IDENTITY_PATHS = set()
COMMIT_CREATING_GIT_ACTIONS = {'cherry-pick', 'commit', 'commit-tree', 'merge', 'revert'}


def read_version_file(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def source_checkout_root(source_path=None):
    source = Path(source_path or __file__).resolve()
    if source.name == 'syncwheel.py' and source.parent.name == 'scripts':
        return source.parents[1]
    return None


SOURCE_ROOT = source_checkout_root() or Path(__file__).resolve().parent


def package_metadata_version():
    try:
        return importlib.metadata.version(UV_TOOL_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def resolve_runtime_version(root=None):
    if root:
        version = read_version_file(Path(root) / 'VERSION')
        if version:
            return version
    version = read_version_file(SOURCE_ROOT / 'VERSION')
    if version:
        return version
    return package_metadata_version() or '0.6.0'


VERSION = resolve_runtime_version()


def run(cmd, cwd=None, check=True, input_text=None, env=None):
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        env=process_env,
    )
    if check and result.returncode != 0:
        raise SyncwheelError(result.stderr.strip() or result.stdout.strip() or f"command failed: {' '.join(cmd)}")
    return result


def git(repo_root, *args, check=True, input_text=None, env=None):
    return run(['git', *args], cwd=repo_root, check=check, input_text=input_text, env=env)


def git_command_cwd(default_cwd, command):
    if not command or command[0] != 'git':
        return Path(default_cwd)
    index = 1
    while index < len(command):
        if command[index] == '-C' and index + 1 < len(command):
            return Path(command[index + 1])
        index += 1
    return Path(default_cwd)


def git_command_creates_commit(command):
    return bool(command and command[0] == 'git' and any(part in COMMIT_CREATING_GIT_ACTIONS for part in command[1:]))


def git_identity(path):
    name = git(path, 'config', '--get', 'user.name', check=False)
    email = git(path, 'config', '--get', 'user.email', check=False)
    name_value = name.stdout.strip() if name.returncode == 0 else ''
    email_value = email.stdout.strip() if email.returncode == 0 else ''
    return name_value, email_value


def warn_missing_git_identity(path):
    resolved = str(Path(path).resolve())
    if resolved in WARNED_GIT_IDENTITY_PATHS:
        return
    WARNED_GIT_IDENTITY_PATHS.add(resolved)
    print(
        f"{YELLOW}WARN: Git user.name/user.email are not configured for {resolved}; "
        "using Syncwheel fallback identity for generated commits. "
        "Configure Git identity for this repository to avoid this warning."
        f"{RESET}",
        file=sys.stderr,
    )


def with_git_identity(default_cwd, command):
    if not command or command[0] != 'git':
        return command
    if not git_command_creates_commit(command):
        return command
    command_cwd = git_command_cwd(default_cwd, command)
    if not command_cwd.exists():
        command_cwd = Path(default_cwd)
    name, email = git_identity(command_cwd)
    if name and email:
        return command
    warn_missing_git_identity(command_cwd)
    return ['git', *FALLBACK_GIT_IDENTITY_CONFIG, *command[1:]]


def commit_identity_env(repo_root, commit):
    result = git(
        repo_root,
        'show',
        '-s',
        '--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI',
        commit,
    )
    values = result.stdout.rstrip('\n').split('\x00')
    if len(values) != 6:
        raise SyncwheelError(f'could not read commit identity for {commit}')
    author_name, author_email, author_date, committer_name, committer_email, committer_date = values
    return {
        'GIT_AUTHOR_NAME': author_name,
        'GIT_AUTHOR_EMAIL': author_email,
        'GIT_AUTHOR_DATE': author_date,
        'GIT_COMMITTER_NAME': committer_name,
        'GIT_COMMITTER_EMAIL': committer_email,
        'GIT_COMMITTER_DATE': committer_date,
    }


def replay_hygiene_env():
    return {
        'GIT_CONFIG_COUNT': '2',
        'GIT_CONFIG_KEY_0': 'rerere.enabled',
        'GIT_CONFIG_VALUE_0': 'false',
        'GIT_CONFIG_KEY_1': 'commit.gpgsign',
        'GIT_CONFIG_VALUE_1': 'false',
    }


def replay_commit_env(repo_root, commit):
    env = replay_hygiene_env()
    env.update(commit_identity_env(repo_root, commit))
    return env


def get_repo_root(explicit=None):
    cwd = explicit or os.getcwd()
    result = run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd)
    return Path(result.stdout.strip())


def iso_utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def get_settings_path():
    raw = os.environ.get(ENV_UPDATE_SETTINGS_PATH)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / '.config' / 'syncwheel' / 'settings.json'


def get_update_state_path():
    raw = os.environ.get(ENV_UPDATE_STATE_PATH)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / '.config' / 'syncwheel' / 'update-state.json'


def load_json_file(path, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SyncwheelError(f'invalid JSON file: {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise SyncwheelError(f'JSON root must be an object: {path}')
    return data


def save_json_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    return path


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_version_tuple(value):
    parts = []
    for raw in str(value or '').strip().split('.'):
        if raw == '':
            continue
        if raw.isdigit():
            parts.append(int(raw))
        else:
            return None
    return tuple(parts) if parts else None


def compare_versions(left, right):
    left_tuple = parse_version_tuple(left)
    right_tuple = parse_version_tuple(right)
    if left_tuple is None or right_tuple is None:
        return (str(left) > str(right)) - (str(left) < str(right))
    width = max(len(left_tuple), len(right_tuple))
    left_tuple = left_tuple + (0,) * (width - len(left_tuple))
    right_tuple = right_tuple + (0,) * (width - len(right_tuple))
    return (left_tuple > right_tuple) - (left_tuple < right_tuple)


def load_update_settings():
    path = get_settings_path()
    data = load_json_file(path, {})
    update = data.get('update', {})
    if update is None:
        update = {}
    if not isinstance(update, dict):
        raise SyncwheelError(f'update settings must be an object: {path}')
    mode = os.environ.get(ENV_UPDATE_MODE) or update.get('mode') or DEFAULT_UPDATE_MODE
    if mode not in UPDATE_MODES:
        raise SyncwheelError(
            f'invalid update mode: {mode!r} (expected one of: {", ".join(sorted(UPDATE_MODES))})'
        )
    interval = parse_int(
        os.environ.get(ENV_UPDATE_INTERVAL_SECONDS) or update.get('check_interval_seconds'),
        DEFAULT_UPDATE_INTERVAL_SECONDS,
    )
    if interval < 0:
        interval = DEFAULT_UPDATE_INTERVAL_SECONDS
    return {
        'path': str(path),
        'mode': mode,
        'check_interval_seconds': interval,
    }


def set_update_mode(mode):
    if mode not in UPDATE_MODES:
        raise SyncwheelError(f'unknown update mode: {mode}')
    path = get_settings_path()
    data = load_json_file(path, {})
    update = data.get('update')
    if update is None or not isinstance(update, dict):
        update = {}
    update['mode'] = mode
    update.setdefault('check_interval_seconds', DEFAULT_UPDATE_INTERVAL_SECONDS)
    data['update'] = update
    save_json_file(path, data)
    return path


def load_update_state():
    path = get_update_state_path()
    data = load_json_file(path, {})
    return data, path


def save_update_state(data, path=None):
    return save_json_file(path or get_update_state_path(), data)


def path_is_relative_to(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def default_remote_version_url(repo_url=None):
    repo = (repo_url or UPSTREAM_REPO_URL).rstrip('/')
    github_prefix = 'https://github.com/'
    if repo.startswith(github_prefix):
        return (
            'https://raw.githubusercontent.com/'
            f'{repo[len(github_prefix):]}/{UPSTREAM_DEFAULT_BRANCH}/VERSION'
        )
    return f'{repo}/raw/{UPSTREAM_DEFAULT_BRANCH}/VERSION'


def remote_version_url():
    return os.environ.get(ENV_REMOTE_VERSION_URL) or default_remote_version_url()


def uv_tool_source():
    return os.environ.get(ENV_UV_TOOL_SOURCE) or f'git+{UPSTREAM_REPO_URL}'


def parse_remote_version_text(text):
    for line in str(text or '').splitlines():
        version = line.strip()
        if version:
            return version
    raise SyncwheelError('remote VERSION file is empty')


def fetch_remote_version(url=None, timeout=10):
    target = url or remote_version_url()
    try:
        with urllib.request.urlopen(target, timeout=timeout) as response:
            body = response.read(4096).decode('utf-8')
    except (OSError, urllib.error.URLError) as exc:
        raise SyncwheelError(f'could not fetch remote syncwheel version from {target}: {exc}') from exc
    return parse_remote_version_text(body)


def detect_uv_tool_prefix(source_path=None, prefix=None, env=None):
    source = Path(source_path or __file__).resolve()
    active_prefix = Path(prefix or sys.prefix).resolve()
    values = env if env is not None else os.environ
    if not path_is_relative_to(source, active_prefix):
        return None
    if not (active_prefix / 'pyvenv.cfg').exists():
        return None

    uv_tool_dir = values.get('UV_TOOL_DIR')
    if uv_tool_dir and path_is_relative_to(active_prefix, Path(uv_tool_dir).expanduser()):
        return active_prefix

    # uv 0.10.x creates one virtualenv per tool under a tools directory. Editable
    # uv installs import from the source checkout, so the source file is not under
    # sys.prefix and this heuristic intentionally does not classify them as uv-tool.
    if active_prefix.name == UV_TOOL_NAME and active_prefix.parent.name == 'tools':
        return active_prefix

    receipt_candidates = (
        active_prefix / 'uv-receipt.toml',
        active_prefix / 'uv-receipt.json',
    )
    if any(path.exists() for path in receipt_candidates):
        return active_prefix
    return None


def detect_syncwheel_install(root=None, source_path=None, prefix=None, env=None):
    source = Path(source_path or __file__).resolve()
    explicit_root = Path(root).resolve() if root else None
    checkout_root = explicit_root or source_checkout_root(source)

    if checkout_root and install_is_git_checkout(checkout_root):
        return {
            'kind': 'git-clone',
            'install_root': checkout_root,
            'source_path': source,
            'git_repo': True,
            'uv_tool_prefix': None,
        }

    uv_prefix = detect_uv_tool_prefix(source_path=source, prefix=prefix, env=env)
    if uv_prefix:
        return {
            'kind': 'uv-tool',
            'install_root': uv_prefix,
            'source_path': source,
            'git_repo': False,
            'uv_tool_prefix': uv_prefix,
        }

    return {
        'kind': 'script',
        'install_root': checkout_root or source.parent,
        'source_path': source,
        'git_repo': False,
        'uv_tool_prefix': None,
    }


def install_root():
    return detect_syncwheel_install()['install_root']


def install_is_git_checkout(root):
    result = run(['git', 'rev-parse', '--show-toplevel'], cwd=root, check=False)
    return result.returncode == 0


def install_git_branch(root):
    result = git(root, 'branch', '--show-current', check=False)
    return result.stdout.strip() or 'DETACHED'


def install_git_upstream(root):
    result = git(root, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}', check=False)
    return result.stdout.strip() or None


def install_git_remotes(root):
    result = git(root, 'remote', check=False)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def remote_branch_exists(repo_root, remote, branch):
    result = git(repo_root, 'ls-remote', '--exit-code', '--heads', remote, branch, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())


def remote_head_branch(repo_root, remote):
    result = git(repo_root, 'ls-remote', '--symref', remote, 'HEAD', check=False)
    for line in result.stdout.splitlines():
        if line.startswith('ref: ') and line.endswith('\tHEAD'):
            ref = line.split()[1]
            if ref.startswith('refs/heads/'):
                return ref.replace('refs/heads/', '', 1)
    return None


def resolve_install_update_ref(root, upstream=None, prefer_network=False):
    upstream = upstream or install_git_upstream(root)
    if upstream and ref_exists(root, upstream):
        return upstream

    remotes = install_git_remotes(root)
    ordered = []
    if 'origin' in remotes:
        ordered.append('origin')
    ordered.extend(remote for remote in remotes if remote != 'origin')

    for remote in ordered:
        preferred = f'{remote}/main'
        if prefer_network:
            if remote_branch_exists(root, remote, 'main'):
                return preferred
        elif ref_exists(root, preferred):
            return preferred

    for remote in ordered:
        if prefer_network:
            branch = remote_head_branch(root, remote)
            if branch:
                return f'{remote}/{branch}'
        fallback = get_default_remote_head(root, remote)
        if fallback:
            return fallback
    return None


def install_is_clean(root):
    result = git(root, 'status', '--porcelain', check=False)
    return result.returncode == 0 and not result.stdout.strip()


def install_hooks_status(root=None):
    root = Path(root or install_root()).resolve()
    hook_path = root / SYNCWHEEL_HOOKS_PATH / 'pre-commit'
    configured = None
    git_repo = install_is_git_checkout(root)
    if git_repo:
        result = git(root, 'config', '--get', 'core.hooksPath', check=False)
        configured = result.stdout.strip() or None
    return {
        'git_repo': git_repo,
        'expected_hooks_path': SYNCWHEEL_HOOKS_PATH,
        'configured_hooks_path': configured,
        'pre_commit_exists': hook_path.exists(),
        'active': git_repo and hook_path.exists() and configured == SYNCWHEEL_HOOKS_PATH,
    }


def install_syncwheel_hooks(root=None, dry_run=False):
    root = Path(root or install_root()).resolve()
    status = install_hooks_status(root)
    if not status['git_repo']:
        raise SyncwheelError('syncwheel install is not a git checkout')
    if not status['pre_commit_exists']:
        raise SyncwheelError(f"missing hook: {SYNCWHEEL_HOOKS_PATH}/pre-commit")
    command = ['git', 'config', 'core.hooksPath', SYNCWHEEL_HOOKS_PATH]
    if dry_run:
        print(quoted(command))
        return status
    run(command, cwd=root)
    return install_hooks_status(root)


def shell_command(command):
    return ' '.join(shlex.quote(str(part)) for part in command)


def repo_root_or_cwd(cwd=None):
    path = Path(cwd or os.getcwd()).resolve()
    result = run(['git', 'rev-parse', '--show-toplevel'], cwd=path, check=False)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return path


def agentwheel_syncwheel_skill_doctor_argv(target_root):
    return [
        'agentwheel',
        'doctor',
        '--adapter',
        AGENTWHEEL_SYNCWHEEL_ADAPTER,
        '--local',
        '--target-root',
        str(target_root),
        '--skill',
        AGENTWHEEL_SYNCWHEEL_SKILL_NAME,
        '--source',
        AGENTWHEEL_SYNCWHEEL_SKILL_SOURCE,
        '--json',
    ]


def agentwheel_syncwheel_skill_install_argv(target_root, dry_run=False):
    command = [
        'agentwheel',
        'install',
        AGENTWHEEL_SYNCWHEEL_SKILL_SOURCE,
        '--adapter',
        AGENTWHEEL_SYNCWHEEL_ADAPTER,
        '--local',
        '--target-root',
        str(target_root),
        '--skill',
        AGENTWHEEL_SYNCWHEEL_SKILL_NAME,
    ]
    if dry_run:
        command.append('--dry-run')
    return command


def bool_from_json_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower().replace('-', '_').replace(' ', '_')
        if normalized in {'true', 'yes', 'installed', 'present', 'ok', 'ready', 'active'}:
            return True
        if normalized in {'false', 'no', 'missing', 'absent', 'not_installed', 'not_found', 'needs_install'}:
            return False
    return None


def iter_agentwheel_skill_status_objects(payload):
    if not isinstance(payload, dict):
        return
    yield payload
    for key in ('skill', 'agent_skill', 'companion_skill', 'requested_skill', 'result', 'status'):
        value = payload.get(key)
        if isinstance(value, dict):
            yield value
    skills = payload.get('skills')
    if isinstance(skills, list):
        for item in skills:
            if not isinstance(item, dict):
                continue
            if item.get('name') == AGENTWHEEL_SYNCWHEEL_SKILL_NAME or item.get('skill') == AGENTWHEEL_SYNCWHEEL_SKILL_NAME:
                yield item
    checks = payload.get('checks')
    if isinstance(checks, list):
        for item in checks:
            if not isinstance(item, dict):
                continue
            label = ' '.join(
                str(item.get(key) or '')
                for key in ('id', 'name', 'kind', 'type', 'skill')
            ).lower()
            if AGENTWHEEL_SYNCWHEEL_SKILL_NAME in label:
                yield item


def parse_agentwheel_skill_installed(payload):
    for item in iter_agentwheel_skill_status_objects(payload):
        for key in ('installed', 'is_installed', 'present', 'exists'):
            parsed = bool_from_json_value(item.get(key))
            if parsed is not None:
                return parsed
        for key in ('missing', 'absent'):
            parsed = bool_from_json_value(item.get(key))
            if parsed is not None:
                return not parsed
    for item in iter_agentwheel_skill_status_objects(payload):
        for key in ('status', 'state', 'result'):
            parsed = bool_from_json_value(item.get(key))
            if parsed is not None:
                return parsed
    for item in iter_agentwheel_skill_status_objects(payload):
        if item is payload:
            continue
        parsed = bool_from_json_value(item.get('ok'))
        if parsed is not None:
            return parsed
    return None


def collect_agentwheel_syncwheel_skill_status(target_root=None):
    root = Path(target_root or repo_root_or_cwd()).resolve()
    doctor_argv = agentwheel_syncwheel_skill_doctor_argv(root)
    install_argv = agentwheel_syncwheel_skill_install_argv(root)
    status = {
        'available': False,
        'path': None,
        'checked': False,
        'status': 'unavailable',
        'installed': None,
        'missing': None,
        'adapter': AGENTWHEEL_SYNCWHEEL_ADAPTER,
        'installation_type': AGENTWHEEL_SYNCWHEEL_INSTALLATION_TYPE,
        'target_root': str(root),
        'skill': AGENTWHEEL_SYNCWHEEL_SKILL_NAME,
        'source': AGENTWHEEL_SYNCWHEEL_SKILL_SOURCE,
        'doctor_command': shell_command(doctor_argv),
        'install_command': shell_command(install_argv),
        'dry_run_command': shell_command(agentwheel_syncwheel_skill_install_argv(root, dry_run=True)),
        'note': None,
    }
    agentwheel_path = shutil.which('agentwheel')
    if not agentwheel_path:
        status['note'] = 'agentwheel not found on PATH'
        return status

    status['available'] = True
    status['path'] = agentwheel_path
    try:
        result = subprocess.run(
            [agentwheel_path, *doctor_argv[1:]],
            text=True,
            capture_output=True,
            timeout=AGENTWHEEL_DOCTOR_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        status['status'] = 'unknown'
        status['note'] = f'agentwheel doctor could not run: {exc}'
        return status

    if result.returncode != 0:
        status['status'] = 'unknown'
        detail = (result.stderr or result.stdout or '').strip().splitlines()
        status['note'] = detail[0] if detail else f'agentwheel doctor exited {result.returncode}'
        return status

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        status['status'] = 'unknown'
        status['note'] = f'agentwheel doctor did not return valid JSON: {exc}'
        return status

    status['checked'] = True
    installed = parse_agentwheel_skill_installed(payload)
    if installed is True:
        status['status'] = 'installed'
        status['installed'] = True
        status['missing'] = False
    elif installed is False:
        status['status'] = 'missing'
        status['installed'] = False
        status['missing'] = True
    else:
        status['status'] = 'unknown'
        status['note'] = 'agentwheel doctor JSON did not include a recognizable skill status'
    return status


def current_install_version(install):
    root = Path(install['install_root'])
    if install['kind'] == 'uv-tool':
        return package_metadata_version() or VERSION
    return read_version_file(root / 'VERSION') or VERSION


def script_self_update_path(install=None):
    detected = install or detect_syncwheel_install()
    root = Path(detected['install_root'])
    legacy_path = root / 'scripts' / 'syncwheel.py'
    if legacy_path.exists():
        return legacy_path
    return Path(detected['source_path'])


def recommended_self_update_command(install=None):
    detected = install or detect_syncwheel_install()
    if detected['kind'] == 'uv-tool':
        return 'syncwheel self update'
    return f'python3 {shlex.quote(str(script_self_update_path(detected)))} self update'


def uv_self_update_command():
    return ['uv', 'tool', 'upgrade', UV_TOOL_NAME]


def build_self_update_commands(status, fetch=True):
    if status.get('install_kind') == 'uv-tool':
        return [uv_self_update_command()]
    upstream = status.get('upstream')
    if not upstream:
        return []
    remote = upstream.split('/', 1)[0]
    commands = []
    if fetch:
        commands.append(['git', 'fetch', '--quiet', remote, '--tags'])
    commands.append(['git', 'merge', '--ff-only', upstream])
    return commands


def collect_self_update_status(root=None, fetch=False):
    install = detect_syncwheel_install(root)
    root = Path(install['install_root']).resolve()
    current_version = current_install_version(install)
    status = {
        'install_root': str(root),
        'install_kind': install['kind'],
        'current_version': current_version,
        'latest_version': current_version,
        'git_repo': False,
        'uv_tool': install['kind'] == 'uv-tool',
        'uv_tool_source': None,
        'remote_version_url': None,
        'recommended_command': recommended_self_update_command(install),
        'branch': None,
        'upstream': None,
        'clean': None,
        'can_self_update': False,
        'update_available': False,
        'ahead_commits': 0,
        'behind_commits': 0,
        'reason': None,
        'checked_at': iso_utc_now(),
    }

    if install['kind'] == 'uv-tool':
        status['can_self_update'] = True
        status['uv_tool_source'] = uv_tool_source()
        status['remote_version_url'] = remote_version_url()
        try:
            remote_version = fetch_remote_version(status['remote_version_url'])
        except SyncwheelError as exc:
            status['reason'] = str(exc)
            return status
        status['latest_version'] = remote_version
        status['update_available'] = compare_versions(remote_version, current_version) > 0
        return status

    if not install['git_repo']:
        status['reason'] = 'syncwheel install is not a git checkout'
        return status

    status['git_repo'] = True
    status['branch'] = install_git_branch(root)
    status['clean'] = install_is_clean(root)
    upstream = install_git_upstream(root)
    status['upstream'] = upstream
    status['can_self_update'] = bool(upstream) and status['branch'] != 'DETACHED'

    remotes = install_git_remotes(root)
    if fetch:
        for remote in remotes:
            git(root, 'fetch', '--quiet', remote, '--tags', check=False)

    update_ref = resolve_install_update_ref(root, upstream=upstream, prefer_network=fetch)
    if not update_ref:
        status['reason'] = 'syncwheel checkout has no upstream tracking branch or remote head to compare against'
        return status

    if not upstream:
        status['reason'] = f'no upstream tracking branch; checking against {update_ref}'

    counts = git(root, 'rev-list', '--left-right', '--count', f'HEAD...{update_ref}', check=False)
    parts = counts.stdout.strip().split()
    if len(parts) == 2:
        status['ahead_commits'] = parse_int(parts[0], 0)
        status['behind_commits'] = parse_int(parts[1], 0)

    remote_version = git(root, 'show', f'{update_ref}:VERSION', check=False).stdout.strip() or current_version
    status['latest_version'] = remote_version
    status['update_available'] = (
        compare_versions(remote_version, current_version) > 0 or status['behind_commits'] > 0
    )
    return status


def refresh_cached_self_update_status(force=False):
    settings = load_update_settings()
    state, state_path = load_update_state()
    now = int(time.time())
    last_checked_epoch = parse_int(state.get('last_checked_epoch'), 0)
    cached = state.get('status') if isinstance(state.get('status'), dict) else None
    current_install = detect_syncwheel_install()
    current_install_root = str(Path(current_install['install_root']).resolve())
    cache_matches_install = (
        cached
        and cached.get('install_root') == current_install_root
        and cached.get('install_kind') == current_install['kind']
    )
    stale = (
        force
        or not cached
        or not cache_matches_install
        or (now - last_checked_epoch) >= settings['check_interval_seconds']
    )
    if stale:
        cached = collect_self_update_status(fetch=True)
        state['status'] = cached
        state['last_checked_at'] = cached.get('checked_at') or iso_utc_now()
        state['last_checked_epoch'] = now
        save_update_state(state, state_path)
    return cached, settings, state, state_path


def perform_self_update(root=None, dry_run=False, fetch=True):
    root = Path(root or install_root()).resolve()
    before = collect_self_update_status(root, fetch=fetch)
    if before['install_kind'] == 'uv-tool':
        commands = build_self_update_commands(before, fetch=fetch)
    else:
        if not before['git_repo']:
            raise SyncwheelError(before['reason'] or 'syncwheel install is not a git checkout')
        if not before['upstream']:
            raise SyncwheelError(before['reason'] or 'syncwheel checkout has no upstream tracking branch')
        if before['branch'] == 'DETACHED':
            raise SyncwheelError('syncwheel checkout is detached; self-update requires a branch checkout')
        if not before['clean']:
            raise SyncwheelError('syncwheel checkout is not clean; commit or stash local changes before self-update')
        commands = build_self_update_commands(before, fetch=fetch)

    if dry_run:
        for command in commands:
            print(quoted(command))
        return before, before, commands

    for command in commands:
        run(command, cwd=root if before['git_repo'] else None)
    after = collect_self_update_status(root, fetch=False)
    state, state_path = load_update_state()
    state['status'] = after
    state['last_checked_at'] = after.get('checked_at') or iso_utc_now()
    state['last_checked_epoch'] = int(time.time())
    save_update_state(state, state_path)
    return before, after, commands


def maybe_handle_startup_update_policy(args):
    if getattr(args, 'command', None) == 'self':
        return
    try:
        status, settings, _, _ = refresh_cached_self_update_status(force=False)
    except SyncwheelError:
        return
    if settings['mode'] == 'off' or not status.get('update_available'):
        return
    current_version = status.get('current_version') or VERSION
    latest_version = status.get('latest_version') or current_version
    if settings['mode'] == 'auto':
        try:
            before, after, _ = perform_self_update(fetch=True)
            print(
                f'syncwheel auto-updated {before["current_version"]} -> {after["current_version"]}',
                file=sys.stderr,
            )
            return
        except SyncwheelError as exc:
            print(
                'NOTICE: syncwheel update available '
                f'({current_version} -> {latest_version}) but auto-update was blocked: {exc}. '
                f'Run: {status.get("recommended_command") or recommended_self_update_command()}',
                file=sys.stderr,
            )
            return
    print(
        f'NOTICE: syncwheel update available ({current_version} -> {latest_version}). '
        f'Run: {status.get("recommended_command") or recommended_self_update_command()}',
        file=sys.stderr,
    )


def get_repo_registry_path():
    raw = os.environ.get(ENV_REGISTRY_PATH)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / '.config' / 'syncwheel' / 'repos.json'


def load_repo_registry(path=None):
    registry_path = path or get_repo_registry_path()
    if not registry_path.exists():
        return {}, registry_path
    try:
        data = json.loads(registry_path.read_text())
    except json.JSONDecodeError as exc:
        raise SyncwheelError(f'invalid repo registry JSON: {registry_path}: {exc}') from exc
    if not isinstance(data, dict):
        raise SyncwheelError(f'repo registry must be an object: {registry_path}')
    registry = {}
    for alias, value in data.items():
        if not isinstance(alias, str) or not alias.strip():
            raise SyncwheelError(f'invalid alias key in registry: {registry_path}')
        if isinstance(value, str):
            if not value.strip():
                raise SyncwheelError(f'invalid alias path for {alias!r} in registry: {registry_path}')
            registry[alias] = {'path': value}
            continue
        if isinstance(value, dict):
            path_value = value.get('path')
            manifest_value = value.get('manifest')
            if not isinstance(path_value, str) or not path_value.strip():
                raise SyncwheelError(f'invalid alias path for {alias!r} in registry: {registry_path}')
            if manifest_value is not None and (not isinstance(manifest_value, str) or not manifest_value.strip()):
                raise SyncwheelError(f'invalid alias manifest for {alias!r} in registry: {registry_path}')
            item = {'path': path_value}
            if manifest_value is not None:
                item['manifest'] = manifest_value
            registry[alias] = item
            continue
        raise SyncwheelError(f'invalid alias entry for {alias!r} in registry: {registry_path}')
    return registry, registry_path


def save_repo_registry(registry, path=None):
    registry_path = path or get_repo_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + '\n')
    return registry_path


def resolve_repo_root(repo_value=None):
    if not repo_value:
        repo_value = os.environ.get(ENV_REPO)
    if not repo_value:
        return get_repo_root()

    candidate_path = Path(repo_value).expanduser()
    if candidate_path.exists():
        return get_repo_root(str(candidate_path.resolve()))

    registry, registry_path = load_repo_registry()
    alias_entry = registry.get(repo_value)
    if alias_entry:
        alias_target = Path(alias_entry['path']).expanduser()
        if not alias_target.exists():
            raise SyncwheelError(
                f"repo alias '{repo_value}' points to a missing path: {alias_target} "
                f"(registry: {registry_path})"
            )
        return get_repo_root(str(alias_target.resolve()))

    raise SyncwheelError(
        f"repo not found: {repo_value} (not a path, not an alias in {registry_path})"
    )


def resolve_personal(repo_root, personal=None):
    personal = personal or os.environ.get(ENV_PERSONAL)
    if personal:
        return personal
    profile = load_repo_profile(repo_root)
    return profile.get('personal')


def resolve_manifest_path(repo_root, repo_value=None, manifest_override=None, personal=None):
    personal = resolve_personal(repo_root, personal)
    if personal:
        if manifest_override:
            raise SyncwheelError('use either --personal or --manifest, not both')
        return personal_manifest_path(repo_root, personal)
    if manifest_override:
        return Path(manifest_override).expanduser()
    if repo_value:
        registry, _ = load_repo_registry()
        alias_entry = registry.get(repo_value)
        if alias_entry and alias_entry.get('manifest'):
            return Path(alias_entry['manifest']).expanduser()
    return repo_root / '.syncwheel' / 'manifest.json'


def branch_exists(repo_root, branch):
    return git(repo_root, 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0


def ref_exists(repo_root, ref):
    return git(repo_root, 'rev-parse', '--verify', '--quiet', ref, check=False).returncode == 0


def commit_exists(repo_root, ref):
    return git(repo_root, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}', check=False).returncode == 0


def branch_contains(repo_root, branch, commit):
    return git(repo_root, 'merge-base', '--is-ancestor', commit, branch, check=False).returncode == 0


def commit_full_sha(repo_root, ref):
    return git(repo_root, 'rev-parse', f'{ref}^{{commit}}').stdout.strip()


def commit_parent_count(repo_root, commit):
    result = git(repo_root, 'rev-list', '--parents', '-n', '1', commit)
    parts = result.stdout.strip().split()
    return max(0, len(parts) - 1)


def commit_first_parent(repo_root, commit):
    result = git(repo_root, 'rev-list', '--parents', '-n', '1', commit)
    parts = result.stdout.strip().split()
    if len(parts) < 2:
        return None
    return parts[1]


def commit_patch_id(repo_root, commit):
    if commit_parent_count(repo_root, commit) != 1:
        return None
    show = git(repo_root, 'show', '--format=', commit)
    patch_id = run(['git', 'patch-id', '--stable'], input_text=show.stdout)
    line = patch_id.stdout.strip()
    if not line:
        return None
    return line.split()[0]


def commit_short_sha(repo_root, commit):
    return git(repo_root, 'rev-parse', '--short', f'{commit}^{{commit}}').stdout.strip()


def commit_subject(repo_root, commit):
    return git(repo_root, 'show', '-s', '--format=%s', commit).stdout.strip()


def commit_changed_files(repo_root, commit, limit=None):
    result = git(repo_root, 'show', '--format=', '--name-only', '--no-renames', commit, check=False)
    if result.returncode != 0:
        return []
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return files[:limit] if limit else files


def is_manifest_only_commit(repo_root, commit):
    """Whether a commit changes only the tracked Syncwheel coordination manifest."""
    files = commit_changed_files(repo_root, commit)
    return bool(files) and set(files) == {'.syncwheel/manifest.json'}


def branches_containing_commit(repo_root, commit, remotes=False):
    args = ['branch', '--format=%(refname:short)', '--contains', commit]
    if remotes:
        args.insert(1, '--remotes')
    result = git(repo_root, *args, check=False)
    if result.returncode != 0:
        return []
    branches = []
    for line in result.stdout.splitlines():
        branch = line.strip()
        if branch and branch != 'HEAD':
            branches.append(branch)
    return branches


def ref_tree(repo_root, ref):
    return git(repo_root, 'rev-parse', f'{ref}^{{tree}}').stdout.strip()


def get_default_remote_head(repo_root, remote):
    symref = git(repo_root, 'symbolic-ref', '--quiet', '--short', f'refs/remotes/{remote}/HEAD', check=False)
    if symref.returncode == 0 and symref.stdout.strip():
        return symref.stdout.strip()
    for candidate in ('main', 'master'):
        ref = f'{remote}/{candidate}'
        if ref_exists(repo_root, ref):
            return ref
    return None


def remote_is_configured(repo_root, remote):
    result = git(repo_root, 'remote', check=False)
    return remote in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def get_current_branch(repo_root):
    result = git(repo_root, 'branch', '--show-current', check=False)
    return result.stdout.strip() or 'DETACHED'


def get_worktrees(repo_root):
    result = git(repo_root, 'worktree', 'list', '--porcelain', check=False)
    blocks = []
    block = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if block:
                blocks.append(block)
                block = {}
            continue
        key, _, value = line.partition(' ')
        if key == 'worktree':
            block['path'] = value
        elif key == 'branch':
            block['branch'] = value.replace('refs/heads/', '')
        else:
            block[key] = value or True
    if block:
        blocks.append(block)
    return blocks


def primary_checkout_state(repo_root, manifest):
    """Return the main Git worktree and its manifest-expected branch."""
    worktrees = get_worktrees(repo_root)
    primary = worktrees[0] if worktrees else {}
    primary_path = Path(primary['path']) if primary.get('path') else None
    shared_manifest = primary_path / '.syncwheel' / 'manifest.json' if primary_path else None
    shared_expected = None
    if shared_manifest and shared_manifest.exists():
        try:
            shared_expected = json.loads(shared_manifest.read_text()).get('integration', {}).get('branch')
        except (OSError, json.JSONDecodeError):
            pass
    active_expected = manifest['integration']['branch'] if manifest else None
    expected = shared_expected or active_expected
    allowed = list(dict.fromkeys(branch for branch in (expected, active_expected) if branch))
    actual = primary.get('branch', 'DETACHED')
    return {
        'path': str(primary_path) if primary_path else None,
        'branch': actual,
        'expected_branch': expected,
        'expected_branches': allowed,
        'compliant': not allowed or actual in allowed,
    }


def ensure_clean_worktree(path, allowed_status_prefixes=None):
    result = run(['git', '-C', str(path), 'status', '--porcelain'], check=False)
    if result.returncode != 0:
        raise SyncwheelError(f'{path} is not a git worktree')
    allowed_status_prefixes = tuple(allowed_status_prefixes or [])
    remaining = []
    for line in result.stdout.splitlines():
        entry = line.strip()
        if not entry:
            continue
        if allowed_status_prefixes and any(entry.startswith(prefix) for prefix in allowed_status_prefixes):
            continue
        remaining.append(entry)
    if remaining:
        raise SyncwheelError(f'{path} is not clean')


def normalize_syncwheel_tracking(value, path='manifest'):
    if value is None:
        return None
    if value not in SYNCWHEEL_TRACKING_VALUES:
        allowed = ', '.join(sorted(SYNCWHEEL_TRACKING_VALUES))
        raise SyncwheelError(f'{path} syncwheel_tracking must be one of: {allowed}')
    return value


def normalize_syncwheel_worktree_root(value, path='manifest'):
    if value is None:
        return DEFAULT_SYNCWHEEL_WORKTREE_ROOT
    if not isinstance(value, str) or not value.strip():
        raise SyncwheelError(f'{path} syncwheel_worktree_root must be a non-empty string')
    return value.strip()


def normalize_coordination_gc(value, path='coordination.gc'):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise SyncwheelError(f'{path} must be an object')
    normalized = dict(DEFAULT_COORDINATION_GC)
    for key in DEFAULT_COORDINATION_GC:
        if key not in value:
            continue
        candidate = value[key]
        if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 1:
            raise SyncwheelError(f'{path}.{key} must be a positive integer')
        normalized[key] = candidate
    unknown = sorted(set(value) - set(DEFAULT_COORDINATION_GC))
    if unknown:
        raise SyncwheelError(f'{path} has unknown keys: {", ".join(unknown)}')
    return normalized


def normalize_coordination_id(value, path='coordination.id'):
    if not isinstance(value, str) or not value.strip():
        raise SyncwheelError(f'{path} must be a non-empty string')
    return safe_ref_segment(value)


def default_coordination_id(manifest_path):
    stem = Path(manifest_path).stem
    if stem == 'manifest':
        return 'default'
    if stem.endswith('.local'):
        stem = stem[:-len('.local')]
    return safe_ref_segment(stem or 'default')


def default_coordination_state_branch(coordination_id):
    return f'{COORDINATION_STATE_PREFIX}{normalize_coordination_id(coordination_id)}'


def normalize_coordination(value, manifest_path='manifest'):
    if not isinstance(value, dict):
        raise SyncwheelError('manifest version 2 requires a coordination object')
    mode = value.get('mode')
    if mode not in COORDINATION_MODES:
        allowed = ', '.join(sorted(COORDINATION_MODES))
        raise SyncwheelError(f'coordination.mode must be one of: {allowed}')
    coordination_id = normalize_coordination_id(value.get('id'))
    remote = value.get('remote')
    if not isinstance(remote, str) or not remote.strip():
        raise SyncwheelError('coordination.remote must be a non-empty string')
    state_branch = value.get('state_branch')
    expected_state_branch = default_coordination_state_branch(coordination_id)
    if state_branch != expected_state_branch:
        raise SyncwheelError(
            f'coordination.state_branch must be {expected_state_branch!r} for coordination id {coordination_id!r}'
        )
    normalized = {
        'mode': mode,
        'id': coordination_id,
        'remote': remote.strip(),
        'state_branch': state_branch,
        'gc': normalize_coordination_gc(value.get('gc')),
    }
    unknown = sorted(set(value) - {'mode', 'id', 'remote', 'state_branch', 'gc'})
    if unknown:
        raise SyncwheelError(f'coordination has unknown keys: {", ".join(unknown)}')
    return normalized


def normalize_integration_membership(value, path='defaults.integration_membership'):
    if value is None:
        return INTEGRATION_MEMBERSHIP_LEGACY
    if value not in INTEGRATION_MEMBERSHIP_POLICIES:
        allowed = ', '.join(sorted(INTEGRATION_MEMBERSHIP_POLICIES))
        raise SyncwheelError(f'{path} must be one of: {allowed}')
    return value


def coordination_config(manifest):
    if manifest.get('version') != MANIFEST_VERSION_COORDINATED:
        return None
    return manifest.get('coordination')


def coordination_is_active(manifest):
    config = coordination_config(manifest)
    return bool(config and config.get('mode') == 'active-active')


def stack_push_remote(manifest, stack, remote_override=None):
    return remote_override or stack.get('publication_remote') or manifest['defaults']['publication_remote']


def draft_push_refusal(manifest, stack, remote):
    """Drafts publish their source ref to the coordination remote only; the forge side stays private."""
    if stack.get('state', 'published') != 'draft':
        return None
    config = coordination_config(manifest)
    if config and config.get('mode') == 'active-active' and remote == config['remote']:
        return None
    return (
        f"{stack['id']}: cannot push stack in state draft to remote {remote!r}; "
        'a draft publishes its source ref only to the coordination remote'
    )


def coordination_state_ref(config):
    return f"refs/heads/{config['state_branch']}"


def active_coordination_config(manifest_path, remote, coordination_id=None):
    coordination_id = normalize_coordination_id(
        coordination_id or default_coordination_id(manifest_path)
    )
    return {
        'mode': 'active-active',
        'id': coordination_id,
        'remote': remote,
        'state_branch': default_coordination_state_branch(coordination_id),
        'gc': dict(DEFAULT_COORDINATION_GC),
    }


def disabled_coordination_config(manifest_path, remote, coordination_id=None):
    coordination_id = normalize_coordination_id(
        coordination_id or default_coordination_id(manifest_path)
    )
    if not isinstance(remote, str) or not remote.strip():
        raise SyncwheelError('disabled coordination requires a non-empty publication remote name')
    return {
        'mode': 'disabled',
        'id': coordination_id,
        'remote': remote.strip(),
        'state_branch': default_coordination_state_branch(coordination_id),
        'gc': dict(DEFAULT_COORDINATION_GC),
    }


def syncwheel_worktree_root(manifest):
    if not manifest:
        return DEFAULT_SYNCWHEEL_WORKTREE_ROOT
    return normalize_syncwheel_worktree_root(manifest.get('syncwheel_worktree_root'))


def resolve_worktree_root_path(repo_root, worktree_root):
    root = Path(worktree_root or DEFAULT_SYNCWHEEL_WORKTREE_ROOT).expanduser()
    if not root.is_absolute():
        root = repo_root / root
    return root.resolve()


def syncwheel_ignore_pattern(value):
    normalized = value.replace('\\', '/').strip()
    if not normalized:
        return DEFAULT_SYNCWHEEL_WORKTREE_ROOT + '/'
    return normalized.rstrip('/') + '/'


def syncwheel_gitignore_patterns(worktree_root):
    return [
        '.syncwheel/ledger/',
        '.syncwheel/profile.local.json',
        '.syncwheel/manifests/*.local.json',
        syncwheel_ignore_pattern(worktree_root),
    ]


def syncwheel_local_exclude_patterns(worktree_root):
    patterns = ['.syncwheel/']
    worktree_pattern = syncwheel_ignore_pattern(worktree_root)
    if not worktree_pattern.startswith('.syncwheel/'):
        patterns.append(worktree_pattern)
    return patterns


def all_syncwheel_managed_patterns(worktree_root):
    patterns = set(syncwheel_gitignore_patterns(worktree_root))
    patterns.update(syncwheel_local_exclude_patterns(worktree_root))
    patterns.add(syncwheel_ignore_pattern(DEFAULT_SYNCWHEEL_WORKTREE_ROOT))
    for legacy_root in LEGACY_SYNCWHEEL_WORKTREE_ROOTS:
        patterns.add(syncwheel_ignore_pattern(legacy_root))
    patterns.add(SYNCWHEEL_LOCAL_EXCLUDE_PATTERN)
    return patterns


def read_text_if_exists(path):
    try:
        return path.read_text()
    except OSError:
        return ''


def replace_managed_block(text, start_marker, end_marker, patterns, legacy_patterns=None):
    lines = text.splitlines()
    output = []
    index = 0
    legacy_patterns = set(legacy_patterns or [])
    found = False
    while index < len(lines):
        if lines[index].strip() != start_marker:
            output.append(lines[index])
            index += 1
            continue
        found = True
        index += 1
        while index < len(lines):
            stripped = lines[index].strip()
            if stripped == end_marker:
                index += 1
                break
            if end_marker not in lines and stripped not in legacy_patterns:
                break
            index += 1
    while output and not output[-1].strip():
        output.pop()
    if patterns:
        if output:
            output.append('')
        output.append(start_marker)
        output.extend(patterns)
        output.append(end_marker)
    updated = '\n'.join(output)
    if updated:
        updated += '\n'
    return updated, found


def write_managed_block(path, start_marker, end_marker, patterns, legacy_patterns=None):
    existing = read_text_if_exists(path)
    updated, _ = replace_managed_block(existing, start_marker, end_marker, patterns, legacy_patterns)
    if updated == existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated)
    return True


def git_info_exclude_path(repo_root):
    result = git(repo_root, 'rev-parse', '--git-path', 'info/exclude', check=False)
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = repo_root / path
    return path


def ensure_syncwheel_metadata_excluded(repo_root, tracking=None, worktree_root=None):
    tracking = normalize_syncwheel_tracking(tracking) or SYNCWHEEL_TRACKING_LOCAL_ONLY
    worktree_root = normalize_syncwheel_worktree_root(worktree_root)
    legacy_patterns = all_syncwheel_managed_patterns(worktree_root)
    if tracking == SYNCWHEEL_TRACKING_GIT_TRACKED:
        write_managed_block(
            repo_root / '.gitignore',
            SYNCWHEEL_GITIGNORE_MARKER,
            SYNCWHEEL_GITIGNORE_END_MARKER,
            syncwheel_gitignore_patterns(worktree_root),
            legacy_patterns,
        )
        path = git_info_exclude_path(repo_root)
        if path:
            write_managed_block(
                path,
                SYNCWHEEL_LOCAL_EXCLUDE_MARKER,
                SYNCWHEEL_LOCAL_EXCLUDE_END_MARKER,
                [],
                legacy_patterns,
            )
        return
    path = git_info_exclude_path(repo_root)
    if not path:
        return
    write_managed_block(
        path,
        SYNCWHEEL_LOCAL_EXCLUDE_MARKER,
        SYNCWHEEL_LOCAL_EXCLUDE_END_MARKER,
        syncwheel_local_exclude_patterns(worktree_root),
        legacy_patterns,
    )


def repo_local_worktree_exclude_pattern(repo_root, worktree_root):
    root = resolve_worktree_root_path(repo_root, worktree_root)
    try:
        relative = root.relative_to(Path(repo_root).resolve())
    except ValueError:
        return None
    return syncwheel_ignore_pattern(relative.as_posix())


def ensure_syncwheel_worktree_root_excluded(repo_root, worktree_root):
    pattern = repo_local_worktree_exclude_pattern(repo_root, worktree_root)
    if not pattern:
        return
    path = git_info_exclude_path(repo_root)
    if not path:
        return
    write_managed_block(
        path,
        SYNCWHEEL_LOCAL_EXCLUDE_MARKER,
        SYNCWHEEL_LOCAL_EXCLUDE_END_MARKER,
        [pattern],
        all_syncwheel_managed_patterns(worktree_root),
    )


def manifest_policy_from_file(manifest_path):
    try:
        data = json.loads(Path(manifest_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None, DEFAULT_SYNCWHEEL_WORKTREE_ROOT
    if not isinstance(data, dict):
        return None, DEFAULT_SYNCWHEEL_WORKTREE_ROOT
    tracking = normalize_syncwheel_tracking(data.get('syncwheel_tracking'), str(manifest_path))
    worktree_root = normalize_syncwheel_worktree_root(data.get('syncwheel_worktree_root'), str(manifest_path))
    return tracking, worktree_root


def syncwheel_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')


def backup_branch_name(branch, timestamp):
    return f'backup/{branch}-before-syncwheel-{timestamp}'


def backup_branch_command(repo_root, branch, timestamp):
    if not branch_exists(repo_root, branch):
        return None
    return ['git', 'branch', backup_branch_name(branch, timestamp), branch]


def ensure_in_place_target(repo_root, target_branch):
    current_branch = get_current_branch(repo_root)
    if current_branch != target_branch:
        raise SyncwheelError(
            f'in-place materialization requires current branch {target_branch!r}; '
            f'current branch is {current_branch!r}'
        )
    ensure_clean_worktree(repo_root)


def load_manifest(repo_root, manifest_path=None):
    path = Path(manifest_path) if manifest_path else repo_root / '.syncwheel' / 'manifest.json'
    if not path.exists():
        return None, path
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SyncwheelError(f'invalid manifest JSON: {exc}') from exc
    if not isinstance(data, dict):
        raise SyncwheelError('manifest root must be an object')
    version = data.get('version')
    if version not in MANIFEST_VERSIONS:
        allowed = ', '.join(str(item) for item in sorted(MANIFEST_VERSIONS))
        raise SyncwheelError(f'manifest version must be one of: {allowed}')
    if 'syncwheel_tracking' in data:
        normalize_syncwheel_tracking(data.get('syncwheel_tracking'))
    data['syncwheel_worktree_root'] = normalize_syncwheel_worktree_root(
        data.get('syncwheel_worktree_root')
    )

    defaults = data.setdefault('defaults', {})
    canonical_remote = defaults.setdefault('canonical_remote', 'origin')
    defaults.setdefault('publication_remote', 'fork')
    defaults.setdefault('base_branch', 'main')
    defaults.setdefault('base_ref', f"{canonical_remote}/{defaults['base_branch']}")
    defaults['integration_membership'] = normalize_integration_membership(
        defaults.get('integration_membership')
    )

    integration = data.setdefault('integration', {})
    integration.setdefault('branch', DEFAULT_INTEGRATION_BRANCH)
    integration.setdefault('base', defaults['base_ref'])
    integration.setdefault('strategy', 'cherry-pick')
    integration.setdefault('stacks', [])

    stacks = data.setdefault('stacks', [])
    if not isinstance(stacks, list):
        raise SyncwheelError('manifest stacks must be an array')

    seen_ids = set()
    seen_branches = set()
    normalized = []
    for raw in stacks:
        if not isinstance(raw, dict):
            raise SyncwheelError('each stack entry must be an object')
        stack = dict(raw)
        stack_id = stack.get('id')
        branch = stack.get('branch')
        commits = stack.get('commits', [])
        if not stack_id or not isinstance(stack_id, str):
            raise SyncwheelError('each stack needs a string id')
        if stack_id in seen_ids:
            raise SyncwheelError(f'duplicate stack id: {stack_id}')
        if not branch or not isinstance(branch, str):
            raise SyncwheelError(f'stack {stack_id} needs a branch')
        if branch in seen_branches:
            raise SyncwheelError(f'duplicate stack branch: {branch}')
        if not isinstance(commits, list) or not all(isinstance(c, str) and c for c in commits):
            raise SyncwheelError(f'stack {stack_id} commits must be a string array')
        integration_commits = stack.get('integration_commits')
        if integration_commits is not None and (
            not isinstance(integration_commits, list)
            or not all(isinstance(c, str) and c for c in integration_commits)
        ):
            raise SyncwheelError(
                f'stack {stack_id} integration_commits must be a string array when present'
            )
        seen_ids.add(stack_id)
        seen_branches.add(branch)
        stack.setdefault('base', defaults['base_ref'])
        stack.setdefault('target_remote', canonical_remote)
        stack.setdefault('target_branch', defaults['base_branch'])
        stack.setdefault('integration_branch', integration['branch'])
        stack.setdefault('state', 'published')
        if stack['state'] not in STACK_STATES:
            raise SyncwheelError(
                f"stack {stack_id} state must be one of: {', '.join(sorted(STACK_STATES))}"
            )
        stack['publication'] = {'enabled': stack['state'] != 'draft'}
        if 'meta' in stack and not isinstance(stack['meta'], dict):
            raise SyncwheelError(f'stack {stack_id} meta must be an object when present')
        stack.setdefault('meta', {})
        normalized.append(stack)
    data['stacks'] = normalized
    if version == MANIFEST_VERSION_COORDINATED:
        coordination = normalize_coordination(data.get('coordination'), path)
        if coordination['remote'] != defaults['publication_remote']:
            raise SyncwheelError(
                'coordination.remote must match defaults.publication_remote'
            )
        data['coordination'] = coordination
    return data, path


def save_manifest(path, manifest):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + '\n')


def stack_integration_commits(stack):
    """Return the commits that materialize a stack on integration.

    Source commits remain authoritative for rebuilding the stack branch. A resolved
    integration projection can be recorded separately after conflict resolution so
    validation never asks Syncwheel to rewrite that source branch with integration
    commits.
    """
    return list(stack.get('integration_commits', stack['commits']))


def ledger_root(repo_root):
    return repo_root / '.syncwheel' / 'ledger'


def external_ledger_root(manifest_path):
    path = Path(manifest_path).expanduser()
    stem = path.stem
    if stem.endswith('-manifest'):
        trimmed = stem[:-len('-manifest')]
        stem = trimmed or stem
    return path.parent / f'{stem}-ledger'


def is_external_manifest_path(repo_root, manifest_path):
    if manifest_path is None:
        return False
    repo_root_path = Path(repo_root).expanduser().resolve(strict=False)
    manifest_root = Path(manifest_path).expanduser().resolve(strict=False)
    try:
        manifest_root.relative_to(repo_root_path)
        return False
    except ValueError:
        return True


def ledger_root(repo_root, manifest_path=None):
    if is_external_manifest_path(repo_root, manifest_path):
        return external_ledger_root(manifest_path)
    return repo_root / '.syncwheel' / 'ledger'


def ledger_events_dir(repo_root, manifest_path=None):
    return ledger_root(repo_root, manifest_path) / 'events'


def ledger_checkpoints_dir(repo_root, manifest_path=None):
    return ledger_root(repo_root, manifest_path) / 'checkpoints'


def ledger_checkpoint_path(repo_root, manifest_path=None):
    return ledger_checkpoints_dir(repo_root, manifest_path) / 'latest.json'


def manifest_stack_history_summary(stack):
    return {
        'id': stack['id'],
        'branch': stack['branch'],
        'base': stack['base'],
        'target_remote': stack['target_remote'],
        'target_branch': stack['target_branch'],
        'integration_branch': stack.get('integration_branch'),
        'state': stack.get('state', 'published'),
        'commits': list(stack['commits']),
        'integration_commits': stack_integration_commits(stack),
        'meta': dict(stack.get('meta', {})),
    }


def manifest_digest(manifest):
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def manifest_event_payload(manifest_path, manifest, reason, context=None):
    return {
        'manifest_path': str(manifest_path),
        'manifest_hash': manifest_digest(manifest),
        'reason': reason,
        'context': context or {},
        'integration': {
            'branch': manifest['integration']['branch'],
            'base': manifest['integration']['base'],
            'strategy': manifest['integration'].get('strategy', 'cherry-pick'),
            'stacks': list(manifest['integration'].get('stacks', [])),
        },
        'stacks': [manifest_stack_history_summary(stack) for stack in manifest['stacks']],
    }


def default_ledger_state():
    return {
        'schema_version': LEDGER_SCHEMA_VERSION,
        'last_seq': 0,
        'event_count': 0,
        'manifest': None,
        'integration': {},
        'stacks': {},
        'recent_events': [],
    }


def load_ledger_events(repo_root, manifest_path=None):
    directory = ledger_events_dir(repo_root, manifest_path)
    if not directory.exists():
        return []
    events = []
    for path in sorted(directory.glob('*.jsonl')):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if not isinstance(data, dict):
                raise SyncwheelError(f'invalid ledger event in {path}')
            events.append(data)
    return events


def branch_ref_matches(candidate, branch):
    return candidate == branch or candidate.endswith(f'/{branch}')


def apply_ledger_event(state, event):
    payload = event.get('payload') or {}
    state['last_seq'] = event['seq']
    state['event_count'] += 1
    state['recent_events'].append(event)
    state['recent_events'] = state['recent_events'][-20:]

    if event['type'] in ('manifest_initialized', 'manifest_saved'):
        active_ids = set()
        for summary in payload.get('stacks') or []:
            stack = state['stacks'].setdefault(summary['id'], {'id': summary['id']})
            stack.update(summary)
            stack['active_in_manifest'] = True
            stack['last_manifest_seq'] = event['seq']
            active_ids.add(summary['id'])
        for stack_id, stack in state['stacks'].items():
            if stack_id not in active_ids:
                stack['active_in_manifest'] = False
        integration = payload.get('integration') or {}
        state['manifest'] = {
            'manifest_path': payload.get('manifest_path'),
            'manifest_hash': payload.get('manifest_hash'),
            'reason': payload.get('reason'),
            'integration': integration,
            'active_stacks': sorted(active_ids),
            'last_seq': event['seq'],
        }
        state['integration'].update({
            'branch': integration.get('branch'),
            'base': integration.get('base'),
            'strategy': integration.get('strategy'),
            'active_stacks': list(integration.get('stacks') or []),
            'last_manifest_seq': event['seq'],
        })
        return state

    if event['type'] == 'stack_rebuilt':
        stack = state['stacks'].setdefault(payload['stack'], {'id': payload['stack']})
        stack.update({
            'branch': payload.get('branch') or stack.get('branch'),
            'base': payload.get('base') or stack.get('base'),
            'integration_branch': payload.get('integration_branch') or stack.get('integration_branch'),
            'last_rebuilt_tip': payload.get('after_tip'),
            'last_rebuild_seq': event['seq'],
            'active_in_manifest': stack.get('active_in_manifest', False),
        })
        return state

    if event['type'] == 'stack_pushed':
        stack = state['stacks'].setdefault(payload['stack'], {'id': payload['stack']})
        stack.update({
            'branch': payload.get('branch') or stack.get('branch'),
            'last_pushed_tip': payload.get('tip'),
            'last_push_remote': payload.get('remote'),
            'last_push_seq': event['seq'],
        })
        return state

    if event['type'] in ('integration_rebuilt', 'integration_aligned_remote', 'integration_pushed'):
        state['integration'].update({
            'branch': payload.get('branch') or state['integration'].get('branch'),
            'last_tip': payload.get('after_tip') or payload.get('tip') or state['integration'].get('last_tip'),
            'last_remote_ref': payload.get('remote_ref') or state['integration'].get('last_remote_ref'),
            'last_push_remote': payload.get('remote') or state['integration'].get('last_push_remote'),
            'last_event_type': event['type'],
            'last_event_seq': event['seq'],
        })
        return state

    if event['type'] == 'stack_closed':
        stack_id = payload.get('stack')
        if stack_id:
            stack = state['stacks'].setdefault(stack_id, {'id': stack_id})
            stack.update({
                'branch': payload.get('branch') or stack.get('branch'),
                'active_in_manifest': False,
                'closed_reason': payload.get('reason', 'merged'),
                'last_closed_seq': event['seq'],
            })
        return state

    return state


def reduce_ledger_state(events):
    state = default_ledger_state()
    for event in events:
        apply_ledger_event(state, event)
    return state


def load_ledger_state(repo_root, manifest_path=None):
    path = ledger_checkpoint_path(repo_root, manifest_path)
    if path.exists():
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    return reduce_ledger_state(load_ledger_events(repo_root, manifest_path))


def write_ledger_checkpoint(repo_root, state, manifest_path=None):
    path = ledger_checkpoint_path(repo_root, manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n')


def next_ledger_segment_path(repo_root, manifest_path=None):
    directory = ledger_events_dir(repo_root, manifest_path)
    directory.mkdir(parents=True, exist_ok=True)
    segments = sorted(directory.glob('*.jsonl'))
    if not segments:
        return directory / '000001.jsonl'
    current = segments[-1]
    line_count = sum(1 for _ in current.open())
    if line_count < LEDGER_SEGMENT_MAX_EVENTS:
        return current
    next_index = int(current.stem) + 1
    return directory / f'{next_index:06d}.jsonl'


def append_ledger_event(repo_root, event_type, payload, manifest_path=None):
    if not is_external_manifest_path(repo_root, manifest_path):
        tracking, worktree_root = manifest_policy_from_file(manifest_path or repo_root / '.syncwheel' / 'manifest.json')
        ensure_syncwheel_metadata_excluded(repo_root, tracking, worktree_root)
    current = load_ledger_state(repo_root, manifest_path)
    event = {
        'schema_version': LEDGER_SCHEMA_VERSION,
        'seq': current['last_seq'] + 1,
        'ts': iso_utc_now(),
        'type': event_type,
        'payload': payload,
    }
    path = next_ledger_segment_path(repo_root, manifest_path)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(event, sort_keys=True) + '\n')
    state = reduce_ledger_state(load_ledger_events(repo_root, manifest_path))
    write_ledger_checkpoint(repo_root, state, manifest_path)
    return event


def save_manifest_with_ledger(repo_root, manifest_path, manifest, reason, context=None, event_type='manifest_saved'):
    save_manifest(manifest_path, manifest)
    append_ledger_event(repo_root, event_type, manifest_event_payload(manifest_path, manifest, reason, context), manifest_path)


def ref_tip(repo_root, ref):
    result = git(repo_root, 'rev-parse', ref, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def stack_map(manifest):
    return {stack['id']: stack for stack in manifest.get('stacks', [])}


def require_manifest(repo_root, repo_value=None, manifest_override=None, personal=None):
    manifest_path = resolve_manifest_path(repo_root, repo_value, manifest_override, personal)
    manifest, manifest_path = load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    return manifest, manifest_path


def require_stack(manifest, stack_id):
    stacks = stack_map(manifest)
    if stack_id not in stacks:
        raise SyncwheelError(f'unknown stack: {stack_id}')
    return stacks[stack_id]


def rev_list(repo_root, rev_range):
    result = git(repo_root, 'rev-list', '--reverse', rev_range)
    return [line for line in result.stdout.splitlines() if line.strip()]


def commit_list_for_spec(repo_root, spec):
    if '..' in spec:
        return rev_list(repo_root, spec)
    if not commit_exists(repo_root, spec):
        raise SyncwheelError(f'commit does not exist: {spec}')
    return [git(repo_root, 'rev-parse', spec).stdout.strip()]


def safe_ref_segment(value):
    cleaned = value.strip().replace('\\', '/').strip('/')
    if not cleaned or cleaned.startswith('.') or '..' in cleaned:
        raise SyncwheelError(f'invalid ref segment: {value!r}')
    disallowed = set(' ~^:?*[')
    if any(char in disallowed for char in cleaned):
        raise SyncwheelError(f'invalid ref segment: {value!r}')
    if cleaned.endswith('.lock') or cleaned.endswith('/'):
        raise SyncwheelError(f'invalid ref segment: {value!r}')
    return cleaned


def personal_manifest_path(repo_root, name):
    segment = safe_ref_segment(name)
    return repo_root / '.syncwheel' / 'manifests' / f'{segment}.local.json'


def personal_integration_branch(name):
    return f'integration/{safe_ref_segment(name)}/main'


def repo_profile_path(repo_root):
    return repo_root / '.syncwheel' / PROFILE_FILENAME


def load_repo_profile(repo_root):
    path = repo_profile_path(repo_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SyncwheelError(f'invalid syncwheel profile JSON: {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise SyncwheelError(f'syncwheel profile must be an object: {path}')
    personal = data.get('personal')
    if personal is not None:
        if not isinstance(personal, str) or not personal.strip():
            raise SyncwheelError(f'invalid syncwheel profile personal value: {path}')
        data['personal'] = safe_ref_segment(personal)
    return data


def save_repo_profile(repo_root, profile):
    tracking, worktree_root = manifest_policy_from_file(repo_root / '.syncwheel' / 'manifest.json')
    ensure_syncwheel_metadata_excluded(repo_root, tracking, worktree_root)
    path = repo_profile_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2, sort_keys=True) + '\n')
    return path


def coordination_profile(repo_root):
    profile = load_repo_profile(repo_root)
    coordination = profile.get('coordination')
    if coordination is None:
        coordination = {}
    if not isinstance(coordination, dict):
        raise SyncwheelError('syncwheel profile coordination state must be an object')
    profile['coordination'] = coordination
    return profile, coordination


def installation_id(create=False):
    path = get_settings_path()
    data = load_json_file(path, {})
    identity = data.get('installation')
    if identity is None:
        identity = {}
    if not isinstance(identity, dict):
        raise SyncwheelError(f'installation settings must be an object: {path}')
    value = identity.get('id')
    if value is not None:
        if not isinstance(value, str) or not value.strip():
            raise SyncwheelError(f'installation id must be a non-empty string: {path}')
        return value.strip()
    if not create:
        return None
    value = str(uuid.uuid4())
    identity['id'] = value
    data['installation'] = identity
    save_json_file(path, data)
    return value


def canonical_json_digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def coordination_manifest_remote_roles(manifest):
    """Map only portable manifest roles to local Git remote aliases."""
    defaults = manifest['defaults']
    canonical_remote = defaults['canonical_remote']
    publication_remote = defaults['publication_remote']
    roles = {canonical_remote: COORDINATION_REMOTE_ROLE_CANONICAL}
    if publication_remote != canonical_remote:
        roles[publication_remote] = COORDINATION_REMOTE_ROLE_PUBLICATION
    return roles


def public_coordination_remote_ref(role, branch, path):
    ref = f'refs/heads/{branch}' if isinstance(branch, str) else None
    if not is_valid_coordination_branch_ref(ref):
        raise SyncwheelError(f'{path} has an invalid remote branch ref')
    return {
        'kind': 'remote-ref',
        'role': role,
        'ref': ref,
    }


def public_coordination_ref(value, remote_roles, repo_root=None, path='manifest ref'):
    """Project a local ref into a portable public ref without collapsing remote identity."""
    if not isinstance(value, str):
        return value
    if value.startswith('refs/remotes/'):
        for remote in sorted(remote_roles, key=len, reverse=True):
            remote_tracking_prefix = f'refs/remotes/{remote}/'
            if value.startswith(remote_tracking_prefix):
                return public_coordination_remote_ref(
                    remote_roles[remote], value[len(remote_tracking_prefix):], path
                )
        raise SyncwheelError(
            f'{path} uses an unrecognized local remote alias: {value!r}; '
            'use the canonical or publication remote role instead'
        )
    if value.startswith('refs/heads/') or value.startswith('refs/tags/'):
        return value
    for remote in sorted(remote_roles, key=len, reverse=True):
        remote_prefix = f'{remote}/'
        if value.startswith(remote_prefix):
            return public_coordination_remote_ref(
                remote_roles[remote], value[len(remote_prefix):], path
            )
    if value.startswith('refs/') or '/' not in value:
        return value
    if repo_root is not None:
        configured_remotes = {
            line.strip()
            for line in git(repo_root, 'remote').stdout.splitlines()
            if line.strip()
        }
        for remote in sorted(configured_remotes - set(remote_roles), key=len, reverse=True):
            if value.startswith(f'{remote}/'):
                raise SyncwheelError(
                    f'{path} uses an unrecognized local remote alias: {value!r}; '
                    'use the canonical or publication remote role instead'
                )
        if branch_exists(repo_root, value):
            return f'refs/heads/{value}'
    raise SyncwheelError(
        f'{path} may contain an unrecognized local remote alias: {value!r}; '
        'use the canonical or publication remote role, or an explicit refs/heads/... local branch'
    )


def is_valid_coordination_branch_ref(value):
    return (
        isinstance(value, str)
        and value.startswith('refs/heads/')
        and run(['git', 'check-ref-format', value], check=False).returncode == 0
    )


def coordination_public_remote_ref_parts(value, path):
    """Validate and unpack a typed public remote ref, if present."""
    if isinstance(value, str):
        if not value:
            raise SyncwheelError(f'{path} must be a non-empty ref string')
        return None
    if not isinstance(value, dict):
        raise SyncwheelError(f'{path} must be a ref string or typed remote ref')
    if set(value) != {'kind', 'role', 'ref'} or value.get('kind') != 'remote-ref':
        raise SyncwheelError(f'{path} contains an invalid typed remote ref')
    role = value.get('role')
    ref = value.get('ref')
    if role not in {
        COORDINATION_REMOTE_ROLE_CANONICAL,
        COORDINATION_REMOTE_ROLE_PUBLICATION,
    } or not is_valid_coordination_branch_ref(ref):
        raise SyncwheelError(f'{path} contains an invalid typed remote ref')
    return role, ref


def local_coordination_ref(value, defaults):
    """Map a portable public ref back to this checkout's remote role."""
    canonical_remote = defaults['canonical_remote']
    publication_remote = defaults['publication_remote']
    remote_ref = coordination_public_remote_ref_parts(value, 'coordination state ref')
    if remote_ref:
        role, ref = remote_ref
        remote = {
            COORDINATION_REMOTE_ROLE_CANONICAL: canonical_remote,
            COORDINATION_REMOTE_ROLE_PUBLICATION: publication_remote,
        }.get(role)
        if not remote:
            raise SyncwheelError('coordination state contains an invalid typed remote ref')
        return f"{remote}/{ref[len('refs/heads/'):]}"
    return value


def coordination_manifest_snapshot(manifest, repo_root=None):
    """Return the public, topology-only projection stored in remote coordination state."""
    defaults = manifest['defaults']
    remote_roles = coordination_manifest_remote_roles(manifest)
    snapshot = {
        'version': manifest['version'],
        'defaults': {
            'base_branch': defaults['base_branch'],
            'base_ref': public_coordination_ref(
                defaults['base_ref'], remote_roles, repo_root, 'defaults.base_ref'
            ),
        },
        'integration': {
            'branch': manifest['integration']['branch'],
            'base': public_coordination_ref(
                manifest['integration']['base'], remote_roles, repo_root, 'integration.base'
            ),
            'strategy': manifest['integration'].get('strategy', 'cherry-pick'),
            'stacks': list(manifest['integration'].get('stacks', [])),
        },
        'stacks': [],
    }
    for stack in manifest['stacks']:
        snapshot_stack = {
            'id': stack['id'],
            'branch': stack['branch'],
            'base': public_coordination_ref(
                stack['base'], remote_roles, repo_root, f"stacks.{stack['id']}.base"
            ),
            'target_branch': stack['target_branch'],
            'integration_branch': stack.get('integration_branch'),
            'commits': list(stack['commits']),
        }
        if stack.get('state', 'published') != 'published':
            snapshot_stack['state'] = stack['state']
        snapshot['stacks'].append(snapshot_stack)
    config = coordination_config(manifest)
    if config:
        snapshot['coordination'] = {
            key: value for key, value in config.items()
            if key in {'mode', 'id', 'state_branch', 'gc'}
        }
    return snapshot


def coordination_manifest_digest(manifest, repo_root=None):
    return canonical_json_digest(coordination_manifest_snapshot(manifest, repo_root))


def managed_ref_names(manifest):
    names = []
    for stack in manifest['stacks']:
        names.append(f"refs/heads/{stack['branch']}")
    names.append(f"refs/heads/{manifest['integration']['branch']}")
    return list(dict.fromkeys(names))


def remote_ref_tips(repo_root, remote, refs):
    refs = list(dict.fromkeys(refs))
    output = {ref: None for ref in refs}
    if not refs:
        return output
    result = git(repo_root, 'ls-remote', '--heads', remote, *refs, check=False)
    if result.returncode != 0:
        raise SyncwheelError(result.stderr.strip() or result.stdout.strip() or f'cannot inspect remote {remote}')
    for line in result.stdout.splitlines():
        sha, separator, ref = line.partition('\t')
        if separator and ref in output:
            output[ref] = sha.strip()
    return output


def validate_coordination_snapshot_refs(snapshot):
    if not isinstance(snapshot, dict):
        raise SyncwheelError('coordination state manifest must be an object')
    defaults = snapshot.get('defaults')
    integration = snapshot.get('integration')
    stacks = snapshot.get('stacks')
    if not isinstance(defaults, dict) or 'base_ref' not in defaults:
        raise SyncwheelError('coordination state manifest is missing defaults.base_ref')
    if not isinstance(integration, dict) or 'base' not in integration:
        raise SyncwheelError('coordination state manifest is missing integration.base')
    if not isinstance(stacks, list):
        raise SyncwheelError('coordination state manifest stacks must be an array')
    coordination_public_remote_ref_parts(
        defaults['base_ref'], 'coordination state defaults.base_ref'
    )
    coordination_public_remote_ref_parts(
        integration['base'], 'coordination state integration.base'
    )
    for index, stack in enumerate(stacks):
        if not isinstance(stack, dict) or 'base' not in stack:
            raise SyncwheelError(f'coordination state manifest stack {index} is missing base')
        coordination_public_remote_ref_parts(
            stack['base'], f'coordination state stacks[{index}].base'
        )


def validate_coordination_state(state, expected_id=None):
    if not isinstance(state, dict):
        raise SyncwheelError('coordination state must be an object')
    if state.get('schema_version') != COORDINATION_STATE_SCHEMA_VERSION:
        raise SyncwheelError(
            f"unsupported coordination state schema: {state.get('schema_version')!r}"
        )
    coordination_id = state.get('coordination_id')
    if not isinstance(coordination_id, str) or not coordination_id:
        raise SyncwheelError('coordination state is missing coordination_id')
    if expected_id and coordination_id != expected_id:
        raise SyncwheelError(
            f'coordination state id mismatch: expected {expected_id!r}, got {coordination_id!r}'
        )
    if not isinstance(state.get('manifest'), dict):
        raise SyncwheelError('coordination state is missing the normalized manifest snapshot')
    validate_coordination_snapshot_refs(state['manifest'])
    if not isinstance(state.get('manifest_digest'), str) or not state['manifest_digest']:
        raise SyncwheelError('coordination state is missing manifest_digest')
    if canonical_json_digest(state['manifest']) != state['manifest_digest']:
        raise SyncwheelError('coordination state manifest_digest does not match its manifest')
    if not isinstance(state.get('managed_refs'), dict):
        raise SyncwheelError('coordination state is missing managed_refs')
    if not isinstance(state.get('tombstones', []), list):
        raise SyncwheelError('coordination state tombstones must be an array')
    return state


def coordination_state_from_commit(repo_root, commit, expected_id=None):
    result = git(repo_root, 'show', f'{commit}:{COORDINATION_STATE_FILE}', check=False)
    if result.returncode != 0:
        raise SyncwheelError(
            result.stderr.strip()
            or f'coordination state commit {commit} does not contain {COORDINATION_STATE_FILE}'
        )
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SyncwheelError(f'invalid coordination state JSON at {commit}: {exc}') from exc
    return validate_coordination_state(state, expected_id)


def read_remote_coordination_state(repo_root, config, fetch=True):
    state_ref = coordination_state_ref(config)
    tip = remote_ref_tips(repo_root, config['remote'], [state_ref])[state_ref]
    if not tip:
        return {'tip': None, 'state': None}
    if fetch:
        result = git(repo_root, 'fetch', '--quiet', config['remote'], state_ref, check=False)
        if result.returncode != 0:
            raise SyncwheelError(
                result.stderr.strip() or result.stdout.strip() or 'failed to fetch coordination state'
            )
        commit = 'FETCH_HEAD'
    else:
        commit = tip
    return {
        'tip': tip,
        'state': coordination_state_from_commit(repo_root, commit, config['id']),
    }


def read_remote_coordination_states(repo_root, config):
    result = git(
        repo_root,
        'ls-remote',
        '--heads',
        config['remote'],
        f"refs/heads/{COORDINATION_STATE_PREFIX}*",
        check=False,
    )
    if result.returncode != 0:
        raise SyncwheelError(result.stderr.strip() or result.stdout.strip() or 'failed to inspect coordination state refs')
    states = []
    for line in result.stdout.splitlines():
        tip, separator, ref = line.partition('\t')
        if not separator:
            continue
        fetch = git(repo_root, 'fetch', '--quiet', config['remote'], ref, check=False)
        if fetch.returncode != 0:
            raise SyncwheelError(fetch.stderr.strip() or f'failed to fetch coordination state {ref}')
        state = coordination_state_from_commit(repo_root, 'FETCH_HEAD')
        expected_ref = f"refs/heads/{default_coordination_state_branch(state['coordination_id'])}"
        if ref != expected_ref:
            raise SyncwheelError(
                f'coordination state branch mismatch: {ref} declares {state["coordination_id"]!r}'
            )
        states.append({'ref': ref, 'tip': tip, 'state': state})
    return states


def coordination_ownership_conflicts(repo_root, config, managed_refs):
    claimed = set(managed_refs)
    conflicts = []
    for item in read_remote_coordination_states(repo_root, config):
        state = item['state']
        if state['coordination_id'] == config['id']:
            continue
        overlap = sorted(claimed.intersection(state['managed_refs']))
        if overlap:
            conflicts.append({
                'coordination_id': state['coordination_id'],
                'state_ref': item['ref'],
                'refs': overlap,
            })
    return conflicts


def require_exclusive_coordination_ownership(repo_root, config, managed_refs):
    conflicts = coordination_ownership_conflicts(repo_root, config, managed_refs)
    if conflicts:
        details = '; '.join(
            f"{item['coordination_id']}: {', '.join(item['refs'])}" for item in conflicts
        )
        raise SyncwheelError(
            'managed refs are already owned by another coordination domain: ' + details
        )
    return conflicts


def coordination_tombstone_ref(tombstone):
    if not isinstance(tombstone, dict):
        return None
    ref = tombstone.get('ref')
    if isinstance(ref, str) and ref:
        return ref
    branch = tombstone.get('branch')
    if isinstance(branch, str) and branch:
        return f'refs/heads/{branch}'
    return None


def coordination_tombstones(previous_state, manifest, additional=None):
    """Keep closures only while their managed ref remains inactive."""
    active_refs = set(managed_ref_names(manifest))
    tombstones = []
    if previous_state:
        tombstones.extend(
            item for item in previous_state.get('tombstones') or []
            if coordination_tombstone_ref(item) not in active_refs
        )
    if additional:
        additional_ref = coordination_tombstone_ref(additional)
        tombstones = [
            item for item in tombstones
            if coordination_tombstone_ref(item) != additional_ref
        ]
        tombstones.append(additional)
    return tombstones


def build_coordination_state(repo_root, manifest, config, previous, observed_refs, changed_refs, scope, projection_status, installation, tombstone=None):
    previous_state = previous.get('state') if previous else None
    managed = {}
    if previous_state:
        managed.update(previous_state.get('managed_refs') or {})
    managed.update(observed_refs)
    managed.update(changed_refs)
    if tombstone:
        closed_ref = tombstone.get('ref') or f"refs/heads/{tombstone['branch']}"
        if closed_ref not in managed:
            managed[closed_ref] = tombstone.get('remote_tip')
    snapshot = coordination_manifest_snapshot(manifest, repo_root)
    return {
        'schema_version': COORDINATION_STATE_SCHEMA_VERSION,
        'coordination_id': config['id'],
        'publication_id': str(uuid.uuid4()),
        'parent_state': previous.get('tip') if previous else None,
        'created_at': iso_utc_now(),
        'syncwheel_version': VERSION,
        'installation_id': installation,
        'manifest': snapshot,
        'manifest_digest': canonical_json_digest(snapshot),
        'managed_refs': dict(sorted(managed.items())),
        'changed_refs': dict(sorted(changed_refs.items())),
        'publication_scope': scope,
        'projection_status': projection_status,
        'tombstones': coordination_tombstones(previous_state, manifest, tombstone),
    }


def create_coordination_state_commit(repo_root, state, parent_tip=None):
    encoded = json.dumps(state, indent=2, sort_keys=True) + '\n'
    blob = run(['git', 'hash-object', '-w', '--stdin'], cwd=repo_root, input_text=encoded).stdout.strip()
    descriptor, index_path = tempfile.mkstemp(prefix='syncwheel-coordination-index-')
    os.close(descriptor)
    os.unlink(index_path)
    environment = {
        'GIT_INDEX_FILE': index_path,
        **COORDINATION_GIT_IDENTITY_ENV,
        'GIT_AUTHOR_DATE': state['created_at'],
        'GIT_COMMITTER_DATE': state['created_at'],
    }
    try:
        if parent_tip:
            git(repo_root, 'read-tree', f'{parent_tip}^{{tree}}', env=environment)
        else:
            git(repo_root, 'read-tree', '--empty', env=environment)
        git(
            repo_root,
            'update-index',
            '--add',
            '--cacheinfo',
            f'100644,{blob},{COORDINATION_STATE_FILE}',
            env=environment,
        )
        tree = git(repo_root, 'write-tree', env=environment).stdout.strip()
        # Coordination state is public transport. Never inherit a maintainer's
        # Git identity into these append-only remote commits.
        command = ['git', *COORDINATION_GIT_IDENTITY_CONFIG, 'commit-tree', tree]
        if parent_tip:
            command.extend(['-p', parent_tip])
        command.extend(['-m', f"syncwheel coordination: {state['publication_scope']}"])
        return run(command, cwd=repo_root).stdout.strip()
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def atomic_push_capability_probe(repo_root, remote):
    probe_tip = ref_tip(repo_root, 'HEAD')
    if not probe_tip:
        probe_tip = git(repo_root, 'rev-list', '--all', '-n', '1', check=False).stdout.strip()
    if not probe_tip:
        raise SyncwheelError('cannot probe atomic push capability without a local HEAD commit')
    probe_ref = f"refs/heads/syncwheel-probe/{uuid.uuid4().hex}"
    result = git(
        repo_root,
        'push',
        '--atomic',
        '--dry-run',
        remote,
        f'{probe_tip}:{probe_ref}',
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or 'no detail returned'
        raise SyncwheelError(
            f'atomic push capability is required for active-active coordination and the preflight failed: {detail}'
        )


def local_lease_is_active(coordination):
    lease = coordination.get('lease') or {}
    if not isinstance(lease, dict):
        return False
    expires_at = lease.get('expires_at')
    if not isinstance(expires_at, (int, float)):
        return False
    return expires_at > time.time()


def acquire_local_coordination_lease(repo_root, config, installation):
    profile, coordination = coordination_profile(repo_root)
    lease = coordination.get('lease') or {}
    if lease and local_lease_is_active(coordination) and lease.get('installation_id') != installation:
        raise SyncwheelError('another local Syncwheel coordinated publication lease is active')
    token = str(uuid.uuid4())
    coordination['lease'] = {
        'coordination_id': config['id'],
        'installation_id': installation,
        'token': token,
        'expires_at': time.time() + COORDINATION_LEASE_SECONDS,
    }
    profile['coordination'] = coordination
    save_repo_profile(repo_root, profile)
    return token


def release_local_coordination_lease(repo_root, token):
    profile, coordination = coordination_profile(repo_root)
    lease = coordination.get('lease') or {}
    if lease.get('token') == token:
        coordination.pop('lease', None)
        profile['coordination'] = coordination
        save_repo_profile(repo_root, profile)


def record_pending_coordination_merge(repo_root, config, expected, latest, manifest):
    profile, coordination = coordination_profile(repo_root)
    coordination['pending_merge'] = {
        'coordination_id': config['id'],
        'base_state': expected.get('tip'),
        'remote_state': latest.get('tip'),
        'local_manifest_digest': coordination_manifest_digest(manifest, repo_root),
        'created_at': iso_utc_now(),
    }
    profile['coordination'] = coordination
    save_repo_profile(repo_root, profile)


def clear_pending_coordination_merge(repo_root, config):
    profile, coordination = coordination_profile(repo_root)
    pending = coordination.get('pending_merge')
    if isinstance(pending, dict) and pending.get('coordination_id') == config['id']:
        coordination.pop('pending_merge', None)
        profile['coordination'] = coordination
        save_repo_profile(repo_root, profile)


def record_coordination_state_seen(repo_root, config, state_tip):
    profile, coordination = coordination_profile(repo_root)
    coordination['last_seen_state'] = {
        'coordination_id': config['id'],
        'state_tip': state_tip,
        'seen_at': iso_utc_now(),
    }
    profile['coordination'] = coordination
    save_repo_profile(repo_root, profile)


def stack_snapshot_map(snapshot):
    return {stack['id']: stack for stack in snapshot.get('stacks') or []}


def changed_stack_ids(base, candidate):
    base_stacks = stack_snapshot_map(base)
    candidate_stacks = stack_snapshot_map(candidate)
    return {
        stack_id
        for stack_id in set(base_stacks) | set(candidate_stacks)
        if base_stacks.get(stack_id) != candidate_stacks.get(stack_id)
    }


def snapshot_globals(snapshot):
    return {
        'version': snapshot.get('version'),
        'defaults': snapshot.get('defaults'),
        'integration': snapshot.get('integration'),
        'coordination': snapshot.get('coordination'),
    }


def merge_coordination_snapshots(base, local, remote):
    """Merge only disjoint stack-record changes with an unchanged shared integration contract."""
    if not base or snapshot_globals(base) != snapshot_globals(local) or snapshot_globals(base) != snapshot_globals(remote):
        return {'status': 'conflict', 'reason': 'shared_integration_or_defaults_changed'}
    local_changes = changed_stack_ids(base, local)
    remote_changes = changed_stack_ids(base, remote)
    overlap = sorted(local_changes.intersection(remote_changes))
    if overlap:
        return {'status': 'conflict', 'reason': 'overlapping_stack_changes', 'stacks': overlap}
    if not local_changes or not remote_changes:
        return {'status': 'conflict', 'reason': 'no_disjoint_changes_to_merge'}
    merged = json.loads(json.dumps(remote))
    base_map = stack_snapshot_map(base)
    local_map = stack_snapshot_map(local)
    merged_map = stack_snapshot_map(merged)
    for stack_id in local_changes:
        if stack_id in local_map:
            merged_map[stack_id] = local_map[stack_id]
        else:
            merged_map.pop(stack_id, None)
    ordered_ids = []
    for source in (base.get('stacks') or [], remote.get('stacks') or [], local.get('stacks') or []):
        for stack in source:
            stack_id = stack['id']
            if stack_id in merged_map and stack_id not in ordered_ids:
                ordered_ids.append(stack_id)
    merged['stacks'] = [merged_map[stack_id] for stack_id in ordered_ids]
    branches = [stack['branch'] for stack in merged['stacks']]
    if len(branches) != len(set(branches)):
        return {'status': 'conflict', 'reason': 'merged_stack_branch_ownership_conflict'}
    return {
        'status': 'mergeable',
        'merged': merged,
        'local_stacks': sorted(local_changes),
        'remote_stacks': sorted(remote_changes),
    }


def apply_coordination_snapshot(manifest, snapshot):
    updated = json.loads(json.dumps(manifest))
    local_stacks = {
        stack['id']: stack
        for stack in updated.get('stacks') or []
        if isinstance(stack, dict) and isinstance(stack.get('id'), str)
    }
    updated['version'] = snapshot['version']
    defaults = dict(updated.get('defaults') or {})
    defaults.update(snapshot['defaults'])
    defaults['base_ref'] = local_coordination_ref(
        snapshot['defaults']['base_ref'],
        defaults,
    )
    updated['defaults'] = defaults
    integration = dict(snapshot['integration'])
    integration['base'] = local_coordination_ref(
        snapshot['integration']['base'],
        defaults,
    )
    updated['integration'] = integration
    updated['stacks'] = []
    for stack in snapshot['stacks']:
        restored = dict(stack)
        local_stack = local_stacks.get(stack['id'], {})
        restored['base'] = local_coordination_ref(stack['base'], defaults)
        restored['target_remote'] = local_stack.get(
            'target_remote', defaults['canonical_remote']
        )
        restored['state'] = stack.get('state', 'published')
        restored['publication'] = {'enabled': restored['state'] != 'draft'}
        if isinstance(local_stack.get('meta'), dict):
            restored['meta'] = local_stack['meta']
        updated['stacks'].append(restored)
    if 'coordination' in snapshot:
        coordination = dict(updated.get('coordination') or {})
        coordination.update(snapshot['coordination'])
        coordination['remote'] = coordination.get('remote') or defaults['publication_remote']
        updated['coordination'] = coordination
    else:
        updated.pop('coordination', None)
    return updated


def coordination_state_matches_remote(repo_root, config, state):
    expected = state.get('managed_refs') or {}
    observed = remote_ref_tips(repo_root, config['remote'], expected)
    return all(observed.get(ref) == tip for ref, tip in expected.items())


def coordination_branch_worktrees(repo_root, branch):
    return [
        Path(item['path'])
        for item in get_worktrees(repo_root)
        if item.get('branch') == branch
    ]


def align_equivalent_coordination_refs(repo_root, config, state, changed_refs):
    """Align safe, tree-equivalent local refs after another device won the lease."""
    aligned = []
    managed = state.get('managed_refs') or {}
    for ref in sorted(changed_refs):
        target_tip = managed.get(ref)
        if not target_tip:
            raise SyncwheelError(
                f'equivalent coordination state does not contain a recoverable remote tip for {ref}'
            )
        if not ref.startswith('refs/heads/'):
            raise SyncwheelError(f'equivalent coordination state has an unsupported managed ref: {ref}')
        branch = ref[len('refs/heads/'):]
        local_tip = ref_tip(repo_root, branch)
        if local_tip == target_tip:
            continue
        if not local_tip:
            raise SyncwheelError(f'cannot align missing local managed branch: {branch}')
        fetched = git(repo_root, 'fetch', '--quiet', config['remote'], ref, check=False)
        if fetched.returncode != 0 or ref_tip(repo_root, 'FETCH_HEAD') != target_tip:
            raise SyncwheelError(f'cannot fetch the expected remote coordination tip for {branch}')
        if ref_tree(repo_root, branch) != ref_tree(repo_root, target_tip):
            raise SyncwheelError(
                f'equivalent coordination state has a different tree for {branch}; manual review is required'
            )
        worktrees = coordination_branch_worktrees(repo_root, branch)
        for worktree in worktrees:
            status = local_worktree_status(worktree)
            if status is None or status:
                raise SyncwheelError(
                    f'cannot align equivalent coordination state because {worktree} is dirty'
                )
        backup = backup_branch_command(repo_root, branch, syncwheel_timestamp())
        if backup:
            run(backup, cwd=repo_root)
        if worktrees:
            for worktree in worktrees:
                run(['git', '-C', str(worktree), 'reset', '--hard', target_tip])
        else:
            git(repo_root, 'update-ref', ref, target_tip, local_tip)
        aligned.append({'branch': branch, 'from': local_tip, 'to': target_tip})
    return aligned


def classify_coordination_race(repo_root, manifest, config, expected, changed_refs, projection_status):
    latest = read_remote_coordination_state(repo_root, config, fetch=True)
    desired_snapshot = coordination_manifest_snapshot(manifest, repo_root)
    if latest['state']:
        state = latest['state']
        if (
            state.get('manifest_digest') == canonical_json_digest(desired_snapshot)
            and state.get('projection_status') == projection_status
            and coordination_state_matches_remote(repo_root, config, state)
        ):
            return {'status': 'equivalent', 'latest': latest}
    base = expected.get('state', {}).get('manifest') if expected.get('state') else None
    if base and latest['state']:
        merged = merge_coordination_snapshots(base, desired_snapshot, latest['state']['manifest'])
        if merged['status'] == 'mergeable':
            return {'status': 'mergeable', 'latest': latest, **merged}
        return {'status': 'conflict', 'latest': latest, **merged}
    return {'status': 'conflict', 'latest': latest, 'reason': 'state_changed_without_a_merge_base'}


def fetch_coordination_ref_tip(repo_root, config, ref, expected_tip):
    fetched = git(repo_root, 'fetch', '--quiet', config['remote'], ref, check=False)
    if fetched.returncode != 0 or ref_tip(repo_root, 'FETCH_HEAD') != expected_tip:
        raise SyncwheelError(f'cannot fetch the expected remote coordination tip for {ref}')
    return expected_tip


def coordination_ref_is_safe_successor(repo_root, config, ref, remote_tip, local_branch):
    local_tip = ref_tip(repo_root, local_branch)
    if local_tip == remote_tip:
        return True
    fetch_coordination_ref_tip(repo_root, config, ref, remote_tip)
    if ref_tree(repo_root, local_branch) == ref_tree(repo_root, remote_tip):
        return True
    return git(repo_root, 'merge-base', '--is-ancestor', remote_tip, local_tip, check=False).returncode == 0


def validate_coordination_publication_base(
    repo_root,
    manifest,
    config,
    expected,
    changed_refs,
    tombstone=None,
    rename=None,
    state_transition=None,
):
    """Fail closed when a stale manifest would erase or overwrite published state."""
    state = expected.get('state') if expected else None
    if not state:
        return
    if not coordination_state_matches_remote(repo_root, config, state):
        raise SyncwheelError(
            'published coordination state no longer matches its managed remote refs; run handoff and resolve manually'
        )
    remote_snapshot = state['manifest']
    local_snapshot = coordination_manifest_snapshot(manifest, repo_root)
    if state['manifest_digest'] == canonical_json_digest(local_snapshot):
        return

    remote_stacks = stack_snapshot_map(remote_snapshot)
    local_stacks = stack_snapshot_map(local_snapshot)
    remote_ids = set(remote_stacks)
    local_ids = set(local_stacks)
    removed = remote_ids - local_ids
    allowed_close = {tombstone['stack']} if tombstone and tombstone.get('stack') else set()
    unexpected_removed = sorted(removed - allowed_close)
    if unexpected_removed:
        raise SyncwheelError(
            'local manifest would drop remote-managed stack(s): '
            + ', '.join(unexpected_removed)
            + '; run handoff and resolve the stale manifest first'
        )

    rename_stack = None
    transition_stack = None
    if rename:
        if not isinstance(rename, dict):
            raise SyncwheelError('coordination branch rename permission must be an object')
        required = {'stack', 'from_branch', 'to_branch', 'from_ref_tip'}
        if set(rename) != required:
            raise SyncwheelError(
                'coordination branch rename permission must contain exactly: '
                + ', '.join(sorted(required))
            )
        rename_stack = rename['stack']
        if rename_stack not in remote_stacks or rename_stack not in local_stacks:
            raise SyncwheelError('coordination branch rename permission requires an existing stack')
        remote_stack = remote_stacks[rename_stack]
        local_stack = local_stacks[rename_stack]
        if (
            remote_stack['branch'] != rename['from_branch']
            or local_stack['branch'] != rename['to_branch']
            or rename['from_branch'] == rename['to_branch']
        ):
            raise SyncwheelError(
                f'{rename_stack}: coordination branch rename permission does not match the manifest transition'
            )
        if remote_stack.get('state', 'published') != 'draft' or local_stack.get('state', 'published') != 'published':
            raise SyncwheelError(
                f'{rename_stack}: coordination branch rename permission is limited to draft-to-published promotion'
            )
        remote_without_transition = dict(remote_stack)
        local_without_transition = dict(local_stack)
        remote_without_transition.pop('branch', None)
        remote_without_transition.pop('state', None)
        local_without_transition.pop('branch', None)
        local_without_transition.pop('state', None)
        if remote_without_transition != local_without_transition:
            raise SyncwheelError(
                f'{rename_stack}: coordination branch rename permission cannot change other stack fields'
            )
        from_ref = f"refs/heads/{rename['from_branch']}"
        remote_tip = state.get('managed_refs', {}).get(from_ref)
        if not remote_tip or rename['from_ref_tip'] != remote_tip:
            raise SyncwheelError(
                f'{rename_stack}: coordination branch rename requires the original published remote tip'
            )
        if (
            not isinstance(tombstone, dict)
            or coordination_tombstone_ref(tombstone) != from_ref
            or tombstone.get('remote_tip') != remote_tip
        ):
            raise SyncwheelError(
                f'{rename_stack}: coordination branch rename requires a tombstone for the original remote ref'
            )

    if state_transition:
        if rename:
            raise SyncwheelError('coordination publication cannot combine branch rename and state-only permissions')
        if not isinstance(state_transition, dict):
            raise SyncwheelError('coordination state transition permission must be an object')
        required = {'stack', 'from_state', 'to_state'}
        if set(state_transition) != required:
            raise SyncwheelError(
                'coordination state transition permission must contain exactly: '
                + ', '.join(sorted(required))
            )
        transition_stack = state_transition['stack']
        if transition_stack not in remote_stacks or transition_stack not in local_stacks:
            raise SyncwheelError('coordination state transition permission requires an existing stack')
        remote_stack = remote_stacks[transition_stack]
        local_stack = local_stacks[transition_stack]
        if (
            remote_stack.get('state', 'published') != state_transition['from_state']
            or local_stack.get('state', 'published') != state_transition['to_state']
            or (state_transition['from_state'], state_transition['to_state'])
            not in {('draft', 'published'), ('published', 'draft')}
        ):
            raise SyncwheelError(
                f'{transition_stack}: coordination state transition permission does not match the manifest transition'
            )
        if remote_stack['branch'] != local_stack['branch']:
            raise SyncwheelError(
                f'{transition_stack}: a state-only coordination transition cannot change branch ownership'
            )
        remote_without_state = dict(remote_stack)
        local_without_state = dict(local_stack)
        remote_without_state.pop('state', None)
        local_without_state.pop('state', None)
        if remote_without_state != local_without_state:
            raise SyncwheelError(
                f'{transition_stack}: a state-only coordination transition cannot change other stack fields'
            )

    changed_stack_refs = {
        stack['id']
        for stack in manifest['stacks']
        if f"refs/heads/{stack['branch']}" in changed_refs
    }
    added = local_ids - remote_ids
    added.update({rename_stack} if rename_stack else set())
    missing_added_refs = sorted(
        stack_id for stack_id in added if stack_id not in changed_stack_refs
    )
    if missing_added_refs:
        raise SyncwheelError(
            'new stack(s) require their managed branch in the coordinated publication: '
            + ', '.join(missing_added_refs)
        )

    for stack_id in sorted(remote_ids & local_ids):
        remote_stack = remote_stacks[stack_id]
        local_stack = local_stacks[stack_id]
        if remote_stack['branch'] != local_stack['branch']:
            if stack_id != rename_stack:
                raise SyncwheelError(
                    f'{stack_id}: changing a managed branch ownership requires manual coordination review'
                )
        if remote_stack == local_stack:
            continue
        if stack_id not in changed_stack_refs:
            if stack_id != transition_stack:
                raise SyncwheelError(
                    f'{stack_id}: local manifest differs from published state without publishing its managed branch'
                )
            continue
        ref = f"refs/heads/{remote_stack['branch']}"
        remote_tip = state.get('managed_refs', {}).get(ref)
        if remote_tip and not coordination_ref_is_safe_successor(
            repo_root,
            config,
            ref,
            remote_tip,
            local_stack['branch'],
        ):
            raise SyncwheelError(
                f'{stack_id}: local branch is not a safe successor of the published managed ref; '
                'run handoff and resolve the overlapping stack change'
            )

    remote_integration = remote_snapshot.get('integration')
    local_integration = local_snapshot.get('integration')
    integration_ref = f"refs/heads/{manifest['integration']['branch']}"
    if remote_integration != local_integration:
        if not tombstone and integration_ref not in changed_refs:
            raise SyncwheelError(
                'local integration configuration differs from published state without publishing integration'
            )
        return

    remote_tip = state.get('managed_refs', {}).get(integration_ref)
    if integration_ref in changed_refs and remote_tip and not coordination_ref_is_safe_successor(
        repo_root,
        config,
        integration_ref,
        remote_tip,
        manifest['integration']['branch'],
    ):
        raise SyncwheelError(
            'local integration branch is not a safe successor of the published integration ref; '
            'run handoff and resolve the overlap'
        )


def coordinated_publish(
    repo_root,
    manifest,
    manifest_path,
    changed_refs,
    scope,
    projection_status,
    dry_run=False,
    tombstone=None,
    rename=None,
    state_transition=None,
):
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        raise SyncwheelError('coordinated publish requires an active-active manifest version 2 coordination block')
    if config['remote'] != manifest['defaults']['publication_remote']:
        raise SyncwheelError('coordination.remote must match defaults.publication_remote')
    changed_refs = dict(changed_refs)
    managed = managed_ref_names(manifest)
    if tombstone:
        managed = list(dict.fromkeys([
            *managed,
            tombstone.get('ref') or f"refs/heads/{tombstone['branch']}",
        ]))
    if rename:
        managed = list(dict.fromkeys([
            *managed,
            f"refs/heads/{rename['from_branch']}",
        ]))
    invalid = sorted(set(changed_refs) - set(managed))
    if invalid:
        raise SyncwheelError('coordinated publish received unmanaged refs: ' + ', '.join(invalid))
    expected = read_remote_coordination_state(repo_root, config, fetch=True)
    require_exclusive_coordination_ownership(repo_root, config, managed)
    observed_refs = remote_ref_tips(repo_root, config['remote'], managed)
    validate_coordination_publication_base(
        repo_root,
        manifest,
        config,
        expected,
        changed_refs,
        tombstone=tombstone,
        rename=rename,
        state_transition=state_transition,
    )
    for ref, sha in changed_refs.items():
        if not sha:
            raise SyncwheelError(f'cannot publish an empty managed ref: {ref}')
    if dry_run:
        payload = {
            'coordination_id': config['id'],
            'state_ref': coordination_state_ref(config),
            'changed_refs': changed_refs,
            'scope': scope,
            'projection_status': projection_status,
            'expected_state': expected['tip'],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return {'status': 'dry_run', 'state_tip': None}
    installation = installation_id(create=True)
    state = build_coordination_state(
        repo_root,
        manifest,
        config,
        expected,
        observed_refs,
        changed_refs,
        scope,
        projection_status,
        installation,
        tombstone=tombstone,
    )
    state_commit = create_coordination_state_commit(repo_root, state, expected['tip'])
    state_ref = coordination_state_ref(config)
    lease_refs = [*changed_refs, state_ref]
    expected_tips = dict(observed_refs)
    expected_tips[state_ref] = expected['tip']
    lease_args = [
        f"--force-with-lease={ref}:{expected_tips.get(ref) or ''}"
        for ref in sorted(lease_refs)
    ]
    refspecs = [f'{changed_refs[ref]}:{ref}' for ref in sorted(changed_refs)]
    refspecs.append(f'{state_commit}:{state_ref}')
    token = acquire_local_coordination_lease(repo_root, config, installation)
    try:
        atomic_push_capability_probe(repo_root, config['remote'])
        command = ['git', 'push', '--atomic', *lease_args, config['remote'], *refspecs]
        result = run(command, cwd=repo_root, check=False)
        if result.returncode != 0:
            race = classify_coordination_race(
                repo_root,
                manifest,
                config,
                expected,
                changed_refs,
                projection_status,
            )
            if race['status'] == 'equivalent':
                aligned = align_equivalent_coordination_refs(
                    repo_root,
                    config,
                    race['latest']['state'],
                    changed_refs,
                )
                clear_pending_coordination_merge(repo_root, config)
                record_coordination_state_seen(repo_root, config, race['latest']['tip'])
                if aligned:
                    append_ledger_event(
                        repo_root,
                        'coordination_equivalent_aligned',
                        {'state_tip': race['latest']['tip'], 'refs': aligned},
                        manifest_path,
                    )
                print('coordinated publish: equivalent state was already published by another device')
                return {
                    'status': 'equivalent',
                    'state_tip': race['latest']['tip'],
                    'aligned_refs': aligned,
                }
            if race['status'] == 'mergeable':
                record_pending_coordination_merge(repo_root, config, expected, race['latest'], manifest)
                raise SyncwheelError(
                    'coordinated publish stopped after a lease loss; disjoint stack changes are mergeable. '
                    'Review handoff, then rerun the full lifecycle with publish --accept-merge.'
                )
            detail = result.stderr.strip() or result.stdout.strip() or 'no detail returned'
            raise SyncwheelError(
                f"coordinated publish stopped after a lease loss or remote rejection ({race.get('reason', 'conflict')}): {detail}"
            )
        clear_pending_coordination_merge(repo_root, config)
        record_coordination_state_seen(repo_root, config, state_commit)
        print(quoted(command))
        return {'status': 'published', 'state_tip': state_commit}
    finally:
        release_local_coordination_lease(repo_root, token)


def coordinated_push_remote(args, config):
    if getattr(args, 'remote', None) and args.remote != config['remote']:
        raise SyncwheelError(
            f"active-active coordination requires remote {config['remote']!r}; remote overrides are not allowed"
        )
    forbidden = [
        value for value in passthrough_args(getattr(args, 'git_args', []))
        if value == '--force' or value.startswith('--force-with-lease') or value == '--atomic'
    ]
    if (
        getattr(args, 'command', None) in {'stack', 'int'}
        and getattr(args, 'force_with_lease', False)
    ):
        forbidden.append('--force-with-lease')
    if forbidden:
        raise SyncwheelError(
            'active-active coordination manages atomic and exact lease flags itself; remove: '
            + ', '.join(forbidden)
        )
    return config['remote']


def local_manifest_projection_is_convergent(repo_root, manifest):
    try:
        for stack in manifest['stacks']:
            if not branch_exists(repo_root, stack['branch']):
                return False
            if ref_tree(repo_root, stack['branch']) != materialize_stack_projection(repo_root, stack):
                return False
        integration = manifest['integration']
        if not branch_exists(repo_root, integration['branch']):
            return False
        return ref_tree(repo_root, integration['branch']) == materialize_integration_projection(repo_root, manifest)
    except SyncwheelError:
        return False


def apply_pending_coordination_merge(repo_root, manifest, manifest_path):
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        raise SyncwheelError('--accept-merge requires an active-active coordination manifest')
    profile, coordination = coordination_profile(repo_root)
    pending = coordination.get('pending_merge')
    if not isinstance(pending, dict) or pending.get('coordination_id') != config['id']:
        raise SyncwheelError('there is no pending mergeable coordinated publication for this manifest')
    if pending.get('local_manifest_digest') != coordination_manifest_digest(manifest, repo_root):
        raise SyncwheelError('the local manifest changed after the mergeable conflict; run handoff and resolve again')
    latest = read_remote_coordination_state(repo_root, config, fetch=True)
    if not latest['state'] or latest['tip'] != pending.get('remote_state'):
        raise SyncwheelError('remote coordination state changed after the mergeable conflict; run handoff and retry')
    base_tip = pending.get('base_state')
    if not base_tip:
        raise SyncwheelError('the mergeable conflict has no shared coordination-state ancestor')
    base = coordination_state_from_commit(repo_root, base_tip, config['id'])
    merged = merge_coordination_snapshots(
        base['manifest'],
        coordination_manifest_snapshot(manifest, repo_root),
        latest['state']['manifest'],
    )
    if merged['status'] != 'mergeable':
        raise SyncwheelError(
            'the previous mergeable coordination conflict is no longer safe to merge: '
            + merged.get('reason', 'unknown')
        )
    updated = apply_coordination_snapshot(manifest, merged['merged'])
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        updated,
        'coordination_accept_merge',
        {
            'coordination_id': config['id'],
            'base_state': base_tip,
            'remote_state': latest['tip'],
            'local_stacks': merged['local_stacks'],
            'remote_stacks': merged['remote_stacks'],
        },
    )
    return updated


def parse_coordination_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def local_worktree_status(path):
    result = run(['git', '-C', str(path), 'status', '--porcelain'], check=False)
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line.strip()]


def local_branch_is_recoverable(repo_root, branch, remote_tip):
    if not remote_tip or not branch_exists(repo_root, branch):
        return False
    local_tip = ref_tip(repo_root, branch)
    if not local_tip:
        return False
    return git(repo_root, 'merge-base', '--is-ancestor', local_tip, remote_tip, check=False).returncode == 0


def backup_branch_records(repo_root):
    result = git(
        repo_root,
        'for-each-ref',
        '--format=%(refname:short)%00%(committerdate:unix)',
        'refs/heads/backup/',
        check=False,
    )
    records = []
    for line in result.stdout.splitlines():
        branch, separator, timestamp = line.partition('\x00')
        if not separator:
            continue
        if '-before-syncwheel-' not in branch:
            continue
        try:
            epoch = int(timestamp)
        except ValueError:
            epoch = 0
        records.append({'branch': branch, 'timestamp': epoch})
    return sorted(records, key=lambda item: item['timestamp'], reverse=True)


def coordination_gc_plan(repo_root, manifest, fetch=True, state_info=None):
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        return {'enabled': False, 'candidates': [], 'skipped': ['coordination is not active-active']}
    state_info = state_info or read_remote_coordination_state(repo_root, config, fetch=fetch)
    if not state_info['state']:
        return {
            'enabled': True,
            'state_tip': None,
            'candidates': [],
            'skipped': ['no remote coordination state has been published'],
        }
    state = state_info['state']
    gc = config['gc']
    grace_seconds = gc['worktree_grace_days'] * 24 * 60 * 60
    backup_seconds = gc['backup_retention_days'] * 24 * 60 * 60
    now = datetime.datetime.now(datetime.timezone.utc)
    profile, local_coordination = coordination_profile(repo_root)
    locks = local_coordination.get('locks') or {}
    lease_active = local_lease_is_active(local_coordination)
    current_branch = get_current_branch(repo_root)
    worktree_root = resolve_worktree_root_path(repo_root, syncwheel_worktree_root(manifest))
    worktrees = {item.get('branch'): Path(item['path']) for item in get_worktrees(repo_root) if item.get('branch')}
    tombstones = state.get('tombstones') or []
    active_refs = set(managed_ref_names(manifest))
    tombstoned_refs = [
        ref for item in tombstones
        if (ref := coordination_tombstone_ref(item)) and ref not in active_refs
    ]
    remote_tips = remote_ref_tips(repo_root, config['remote'], tombstoned_refs)
    candidates = []
    skipped = []
    for tombstone in tombstones:
        stack_id = tombstone.get('stack')
        branch = tombstone.get('branch')
        ref = coordination_tombstone_ref(tombstone)
        closed_at = parse_coordination_timestamp(tombstone.get('closed_at'))
        label = f'{stack_id or branch or "unknown"}'
        if not branch or not ref or not closed_at:
            skipped.append(f'{label}: malformed tombstone')
            continue
        if ref in active_refs:
            skipped.append(f'{label}: tombstoned ref is active in the current manifest')
            continue
        if (now - closed_at).total_seconds() < grace_seconds:
            skipped.append(f'{label}: tombstone grace period has not elapsed')
            continue
        original_tip = tombstone.get('remote_tip')
        if not isinstance(original_tip, str) or not original_tip:
            skipped.append(f'{label}: tombstone is missing its original remote tip')
            continue
        if remote_tips.get(ref) != original_tip:
            skipped.append(f'{label}: remote branch no longer matches the tombstone tip')
            continue
        if lease_active:
            skipped.append(f'{label}: a local coordination lease is active')
            continue
        if stack_id and isinstance(locks, dict) and stack_id in locks:
            skipped.append(f'{label}: local worktree lock is active')
            continue
        worktree = worktrees.get(branch)
        if worktree:
            if worktree.resolve() == Path(repo_root).resolve() or not path_is_relative_to(worktree, worktree_root):
                skipped.append(f'{label}: worktree is outside the managed Syncwheel worktree root')
                continue
            status = local_worktree_status(worktree)
            if status is None or status:
                skipped.append(f'{label}: worktree is dirty or unavailable')
                continue
        if branch == current_branch:
            skipped.append(f'{label}: branch is currently checked out')
            continue
        if not local_branch_is_recoverable(repo_root, branch, original_tip):
            skipped.append(f'{label}: local branch has unique or unpublished commits')
            continue
        if worktree:
            candidates.append({
                'type': 'remove_worktree',
                'stack': stack_id,
                'branch': branch,
                'path': str(worktree),
            })
        candidates.append({
            'type': 'delete_branch',
            'stack': stack_id,
            'branch': branch,
        })

    worktree_branches = set(worktrees)
    for index, backup in enumerate(backup_branch_records(repo_root)):
        if lease_active:
            skipped.append(f"{backup['branch']}: a local coordination lease is active")
            continue
        age_seconds = now.timestamp() - backup['timestamp']
        if index < gc['backup_keep'] or age_seconds < backup_seconds:
            continue
        if backup['branch'] in worktree_branches or backup['branch'] == current_branch:
            skipped.append(f"{backup['branch']}: backup is checked out")
            continue
        candidates.append({'type': 'delete_backup', 'branch': backup['branch']})
    return {
        'enabled': True,
        'state_tip': state_info['tip'],
        'policy': gc,
        'candidates': candidates,
        'skipped': skipped,
    }


def coordination_gc_candidate_key(candidate):
    return (
        candidate.get('type'),
        candidate.get('branch'),
        candidate.get('path'),
    )


def coordination_gc_candidate_is_current(repo_root, manifest, candidate, fetch, state_info):
    refreshed = coordination_gc_plan(
        repo_root,
        manifest,
        fetch=fetch,
        state_info=None if fetch else state_info,
    )
    target = coordination_gc_candidate_key(candidate)
    return any(
        coordination_gc_candidate_key(current) == target
        for current in refreshed['candidates']
    )


def coordination_gc_note_skip(plan, candidate):
    label = candidate.get('stack') or candidate.get('branch') or candidate.get('path') or 'unknown'
    plan['skipped'].append(f'{label}: no longer eligible immediately before deletion')


def run_coordination_gc(repo_root, manifest, apply=False, fetch=True, state_info=None):
    plan = coordination_gc_plan(repo_root, manifest, fetch=fetch, state_info=state_info)
    if not apply or not plan['enabled']:
        return plan
    removed_worktrees = set()
    applied_candidates = []
    for candidate in plan['candidates']:
        if candidate['type'] != 'remove_worktree':
            continue
        if not coordination_gc_candidate_is_current(
            repo_root, manifest, candidate, fetch, state_info
        ):
            coordination_gc_note_skip(plan, candidate)
            continue
        run(['git', 'worktree', 'remove', candidate['path']], cwd=repo_root)
        removed_worktrees.add(candidate['branch'])
        applied_candidates.append(candidate)
    if removed_worktrees:
        git(repo_root, 'worktree', 'prune')
    for candidate in plan['candidates']:
        if candidate['type'] not in {'delete_branch', 'delete_backup'}:
            continue
        if not coordination_gc_candidate_is_current(
            repo_root, manifest, candidate, fetch, state_info
        ):
            coordination_gc_note_skip(plan, candidate)
            continue
        branch = candidate['branch']
        local_tip = ref_tip(repo_root, branch)
        if not local_tip:
            coordination_gc_note_skip(plan, candidate)
            continue
        result = git(
            repo_root,
            'update-ref',
            '-d',
            f'refs/heads/{branch}',
            local_tip,
            check=False,
        )
        if result.returncode != 0:
            coordination_gc_note_skip(plan, candidate)
            continue
        applied_candidates.append(candidate)
    plan['applied'] = True
    plan['applied_candidates'] = applied_candidates
    return plan


def command_gc(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    plan = run_coordination_gc(repo_root, manifest, apply=args.apply, fetch=args.fetch)
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        if not plan['enabled']:
            print('gc: active-active coordination is not enabled')
        elif not plan['candidates']:
            print('gc: no eligible local worktrees, branches, or backups')
        else:
            for candidate in plan['candidates']:
                print(f"gc: {candidate['type']} {candidate.get('branch') or candidate.get('path')}")
            if not args.apply:
                print('gc: dry-run; pass --apply to remove eligible local artifacts')
    return 0


def command_handoff(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    validation = validate_manifest(repo_root, manifest)
    config = coordination_config(manifest)
    output = {
        'manifest_path': str(manifest_path),
        'manifest_version': manifest['version'],
        'validation': validation,
        'coordination': {'mode': 'legacy'},
    }
    if config:
        output['coordination'] = dict(config)
    if config and config.get('mode') == 'active-active':
        state_info = read_remote_coordination_state(repo_root, config, fetch=args.fetch)
        ownership = coordination_ownership_conflicts(repo_root, config, managed_ref_names(manifest))
        profile, local_coordination = coordination_profile(repo_root)
        local_digest = coordination_manifest_digest(manifest, repo_root)
        state = state_info['state']
        output['coordination'].update({
            'state_tip': state_info['tip'],
            'state_status': 'uninitialized' if not state else 'published',
            'manifest_relation': (
                'no_published_state' if not state
                else ('aligned' if state['manifest_digest'] == local_digest else 'local_proposal_differs')
            ),
            'ownership_conflicts': ownership,
            'pending_merge': local_coordination.get('pending_merge'),
            'locks': local_coordination.get('locks') or {},
            'lease_active': local_lease_is_active(local_coordination),
            'gc': coordination_gc_plan(repo_root, manifest, fetch=False, state_info=state_info),
        })
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        coordination = output['coordination']
        print(f"manifest: {output['manifest_path']}")
        print(f"coordination: {coordination.get('mode')}")
        if coordination.get('mode') == 'active-active':
            print(f"state: {coordination['state_status']} ({coordination.get('state_tip') or 'none'})")
            print(f"manifest relation: {coordination['manifest_relation']}")
            print(f"ownership conflicts: {len(coordination['ownership_conflicts'])}")
            print(f"gc candidates: {len(coordination['gc']['candidates'])}")
    has_ownership_conflict = bool(output['coordination'].get('ownership_conflicts'))
    return 1 if validation['errors'] or has_ownership_conflict else 0


def default_worktree_path(repo_root, branch):
    safe = branch.replace('/', '-').replace('\\', '-')
    return repo_root.parent / f'{repo_root.name}-wt-{safe}'


def effective_worktree_root(manifest, override=None):
    return override or syncwheel_worktree_root(manifest)


def find_worktree_for_branch(repo_root, branch):
    for worktree in get_worktrees(repo_root):
        if worktree.get('branch') == branch:
            return Path(worktree['path'])
    return None


def resolve_git_worktree(repo_root, branch, worktree=None, auto_worktree=False):
    found = find_worktree_for_branch(repo_root, branch)
    if found:
        return found
    if worktree:
        path = Path(worktree).expanduser().resolve()
        run(['git', 'worktree', 'add', '-B', branch, str(path), branch], cwd=repo_root)
        return path
    if auto_worktree:
        path = default_worktree_path(repo_root, branch)
        run(['git', 'worktree', 'add', '-B', branch, str(path), branch], cwd=repo_root)
        return path
    raise SyncwheelError(
        f"no worktree found for branch: {branch}; pass --worktree <path> "
        'or --auto-worktree to create one'
    )


def passthrough_args(values):
    return values or []


def push_args_with_options(args):
    push_args = passthrough_args(args.git_args)
    if getattr(args, 'force_with_lease', False) and '--force-with-lease' not in push_args:
        push_args = ['--force-with-lease', *push_args]
    return push_args


def resolve_stack_rebuild_location(repo_root, stack, args):
    if args.in_place and args.worktree:
        raise SyncwheelError('use either --in-place or --worktree, not both')
    if args.in_place:
        return None, True
    existing = find_worktree_for_branch(repo_root, stack['branch'])
    if args.worktree:
        path = Path(args.worktree).resolve()
        if existing and existing != path:
            raise SyncwheelError(
                f"branch {stack['branch']!r} already has a worktree at {existing}; "
                'reuse that worktree or use --in-place from that checkout'
            )
        return path, False
    if get_current_branch(repo_root) == stack['branch']:
        return None, True
    if existing:
        return existing, False
    return default_worktree_path(repo_root, stack['branch']), False


def resolve_int_rebuild_location(repo_root, manifest, args):
    integration = manifest['integration']
    if args.in_place and args.worktree:
        raise SyncwheelError('use either --in-place or --worktree, not both')
    if args.in_place:
        return None, True
    existing = find_worktree_for_branch(repo_root, integration['branch'])
    if args.worktree:
        path = Path(args.worktree).resolve()
        if existing and existing != path:
            raise SyncwheelError(
                f"branch {integration['branch']!r} already has a worktree at {existing}; "
                'reuse that worktree or use --in-place from that checkout'
            )
        return path, False
    if get_current_branch(repo_root) == integration['branch']:
        return None, True
    if existing:
        return existing, False
    return default_worktree_path(repo_root, integration['branch']), False


REPLAY_MODE_CHOICES = ('auto', 'plumbing', 'in-place', 'ephemeral', 'desk')
EPHEMERAL_WORKTREE_PLACEHOLDER = '<syncwheel-ephemeral-worktree>'
EMPTY_REPLAY_MESSAGE = (
    'The previous cherry-pick is now empty, possibly due to conflict resolution.\n'
    'If you wish to commit it anyway, use:\n\n'
    '    git commit --allow-empty\n\n'
    "Otherwise, please use 'git cherry-pick --skip'"
)


def git_supports_write_tree(repo_root):
    """Detect whether this Git can run ``merge-tree --write-tree``."""
    result = git(repo_root, '--version', check=False)
    if result.returncode != 0:
        return False
    parts = result.stdout.strip().split()
    if len(parts) < 3 or parts[0] != 'git' or parts[1] != 'version':
        return False
    version_parts = parts[2].split('.')
    try:
        major, minor = (int(part) for part in version_parts[:2])
    except (TypeError, ValueError):
        return False
    return (major, minor) >= (2, 38)


def resolve_replay_mode(repo_root, location, requested_mode='auto'):
    """Map an already-resolved replay location to an available execution mode."""
    if requested_mode not in REPLAY_MODE_CHOICES:
        raise SyncwheelError(f'unsupported replay mode: {requested_mode}')
    if requested_mode == 'plumbing':
        if git_supports_write_tree(repo_root):
            return 'plumbing', None
        return 'ephemeral', None
    if requested_mode == 'ephemeral':
        return 'ephemeral', None

    worktree, in_place = location
    if requested_mode == 'in-place':
        if not in_place:
            raise SyncwheelError('replay mode in-place requires the target branch to be checked out')
        return 'in-place', None
    if requested_mode == 'desk':
        if in_place:
            raise SyncwheelError('replay mode desk requires a separate worktree')
        return 'desk', worktree
    if in_place:
        return 'in-place', None
    return 'desk', worktree


def requested_replay_mode(args):
    """Read and validate the rebuild-only replay mode selection."""
    mode = getattr(args, 'replay_mode', 'auto')
    if mode in ('ephemeral', 'plumbing'):
        if args.in_place:
            raise SyncwheelError(f'use either --replay-mode {mode} or --in-place, not both')
        if args.worktree:
            raise SyncwheelError(f'use either --replay-mode {mode} or --worktree, not both')
    return mode


def collect_repo_snapshot(repo_root, manifest):
    defaults = manifest['defaults'] if manifest else {}
    canonical_remote = defaults.get('canonical_remote', 'origin')
    base_ref = defaults.get('base_ref') or get_default_remote_head(repo_root, canonical_remote)
    current_branch = get_current_branch(repo_root)
    worktrees = get_worktrees(repo_root)
    stashes = git(repo_root, 'stash', 'list', check=False).stdout.splitlines()
    remotes = git(repo_root, 'remote', '-v', check=False).stdout.splitlines()
    status_short = git(repo_root, 'status', '--short', '--branch', check=False).stdout.splitlines()
    return {
        'repo_root': str(repo_root),
        'current_branch': current_branch,
        'working_tree_status': status_short,
        'working_tree_dirty': any(line and not line.startswith('## ') for line in status_short),
        'canonical_remote_head': get_default_remote_head(repo_root, canonical_remote),
        'base_ref': base_ref,
        'worktrees': worktrees,
        'primary_checkout': primary_checkout_state(repo_root, manifest),
        'stashes': stashes,
        'remotes': remotes,
    }


def validate_manifest(repo_root, manifest):
    warnings = []
    errors = []
    details = {'stacks': [], 'integration': {}, 'coordination': {}}
    coordination = coordination_config(manifest)
    if manifest.get('version') == MANIFEST_VERSION_COORDINATED:
        if not coordination:
            errors.append('manifest version 2 requires coordination')
        else:
            details['coordination'] = {
                'mode': coordination['mode'],
                'id': coordination.get('id'),
                'remote': coordination.get('remote'),
                'state_branch': coordination.get('state_branch'),
                'gc': coordination['gc'],
            }
            if coordination['mode'] == 'active-active':
                if not remote_is_configured(repo_root, coordination['remote']):
                    errors.append(
                        f"coordination remote is not configured locally: {coordination['remote']}"
                    )
                if coordination['remote'] != manifest['defaults']['publication_remote']:
                    errors.append('coordination remote must match defaults.publication_remote')
    else:
        details['coordination'] = {'mode': 'legacy'}
    stacks_by_id = stack_map(manifest)
    integration = manifest['integration']
    integration_branch = integration['branch']
    primary_checkout = primary_checkout_state(repo_root, manifest)
    details['primary_checkout'] = primary_checkout
    if not primary_checkout['compliant']:
        errors.append(
            f"primary worktree branch mismatch at {primary_checkout['path']}: "
            f"expected one of {primary_checkout['expected_branches']!r}, "
            f"found {primary_checkout['branch']!r}"
        )
    integration_strategy = integration.get('strategy')
    declared_commits = []
    declared_commit_shas = set()
    declared_patch_ids = set()
    if integration_strategy not in INTEGRATION_STRATEGIES:
        errors.append(
            'integration strategy must be one of '
            + ', '.join(sorted(INTEGRATION_STRATEGIES))
            + f': {integration_strategy}'
        )
    integration_exists = branch_exists(repo_root, integration_branch)
    if not ref_exists(repo_root, integration['base']):
        errors.append(f"integration base ref does not exist: {integration['base']}")
    if not integration_exists:
        warnings.append(f'integration branch is missing locally: {integration_branch}')
    unknown_stack_refs = [stack_id for stack_id in integration.get('stacks', []) if stack_id not in stacks_by_id]
    if unknown_stack_refs:
        errors.append('integration references unknown stacks: ' + ', '.join(unknown_stack_refs))
    if manifest['defaults']['integration_membership'] == INTEGRATION_MEMBERSHIP_REQUIRED:
        excluded_stack_ids = [
            stack['id'] for stack in manifest['stacks']
            if stack['id'] not in integration.get('stacks', [])
        ]
        if excluded_stack_ids:
            errors.append(
                'required integration membership excludes stack(s): '
                + ', '.join(excluded_stack_ids)
            )

    for stack in manifest['stacks']:
        state = stack.get('state', 'published')
        item = {
            'id': stack['id'],
            'branch': stack['branch'],
            'state': state,
            'meta': stack.get('meta', {}),
            'branch_exists': branch_exists(repo_root, stack['branch']),
            'base_exists': ref_exists(repo_root, stack['base']),
            'target': f"{stack['target_remote']}/{stack['target_branch']}",
            'missing_from_branch': [],
            'missing_from_integration': [],
            'missing_commits': [],
            'integration_commits': stack_integration_commits(stack),
        }
        if state not in STACK_STATES:
            errors.append(
                f"stack {stack['id']} state must be one of: {', '.join(sorted(STACK_STATES))}"
            )
        if not item['base_exists']:
            errors.append(f"stack {stack['id']} base ref does not exist: {stack['base']}")
        if not item['branch_exists']:
            warnings.append(f"stack {stack['id']} branch missing locally: {stack['branch']}")
        for commit in stack['commits']:
            if not commit_exists(repo_root, commit):
                item['missing_commits'].append(commit)
                errors.append(f"stack {stack['id']} references missing commit: {commit}")
                continue
            if item['branch_exists'] and not branch_contains(repo_root, stack['branch'], commit):
                item['missing_from_branch'].append(commit)
        for commit in item['integration_commits']:
            if not commit_exists(repo_root, commit):
                item['missing_commits'].append(commit)
                errors.append(f"stack {stack['id']} references missing integration commit: {commit}")
                continue
            declared_commits.append(commit)
            declared_commit_shas.add(commit_full_sha(repo_root, commit))
            patch_id = commit_patch_id(repo_root, commit)
            if patch_id:
                declared_patch_ids.add(patch_id)
            if integration_exists and not branch_contains(repo_root, integration_branch, commit):
                item['missing_from_integration'].append(commit)
        details['stacks'].append(item)

    integration_commits = []
    unmapped_commits = []
    control_commits = []
    integration_merge_commits = []
    if integration_exists and ref_exists(repo_root, integration['base']):
        integration_commits = rev_list(repo_root, f"{integration['base']}..{integration_branch}")
        for commit in integration_commits:
            full_sha = commit_full_sha(repo_root, commit)
            if commit_parent_count(repo_root, commit) > 1:
                integration_merge_commits.append(full_sha)
                continue
            if is_manifest_only_commit(repo_root, commit):
                control_commits.append(full_sha)
                continue
            patch_id = commit_patch_id(repo_root, commit)
            if full_sha not in declared_commit_shas and (not patch_id or patch_id not in declared_patch_ids):
                unmapped_commits.append(full_sha)
        if unmapped_commits:
            warnings.append(
                f"integration contains {len(unmapped_commits)} non-merge commit(s) "
                'not declared in any stack'
            )

    details['integration'] = {
        'branch': integration_branch,
        'exists': integration_exists,
        'base': integration['base'],
        'strategy': integration_strategy,
        'stacks': integration.get('stacks', []),
        'commits': integration_commits,
        'declared_commits': declared_commits,
        'unmapped_commits': unmapped_commits,
        'control_commits': control_commits,
        'merge_commits': integration_merge_commits,
    }
    return {'errors': errors, 'warnings': warnings, 'details': details}


def build_plan(repo_root, manifest, validation):
    actions = []
    details = validation['details']
    integration = manifest['integration']
    primary_checkout = details.get('primary_checkout') or {}
    if primary_checkout.get('compliant') is False:
        actions.append({
            'type': 'restore_primary_checkout',
            'path': primary_checkout.get('path'),
            'branch': primary_checkout.get('expected_branch'),
            'current_branch': primary_checkout.get('branch'),
        })
    if not details['integration']['exists']:
        actions.append({
            'type': 'create_integration_branch',
            'branch': integration['branch'],
            'base': integration['base'],
        })
    for item in details['stacks']:
        if not item['branch_exists']:
            actions.append({
                'type': 'create_pr_branch',
                'stack': item['id'],
                'branch': item['branch'],
                'meta': item.get('meta', {}),
            })
        if item['missing_from_branch']:
            actions.append({
                'type': 'rebuild_pr_branch',
                'stack': item['id'],
                'branch': item['branch'],
                'missing_commits': item['missing_from_branch'],
                'meta': item.get('meta', {}),
            })
        if item['missing_from_integration']:
            actions.append({
                'type': 'refresh_integration_for_stack',
                'stack': item['id'],
                'branch': integration['branch'],
                'missing_commits': item['missing_from_integration'],
                'meta': item.get('meta', {}),
            })
    if details['integration'].get('unmapped_commits'):
        commits = details['integration']['unmapped_commits']
        actions.append({
            'type': 'classify_integration_commits',
            'branch': integration['branch'],
            'commits': commits,
            'remedy': {
                'type': 'capture_integration_into_new_draft',
                'commands': [
                    'syncwheel stack create --draft <new-stack-id> '
                    '--purpose "Classify integration-first work"',
                    'syncwheel stack capture-integration <new-stack-id> '
                    + ' '.join(commit_short_sha(repo_root, commit) for commit in commits),
                ],
            },
        })
    return actions


def stack_hint_reasons(repo_root, manifest, stack, commit, subject, local_branches, remote_branches):
    reasons = []
    if stack['branch'] in local_branches:
        reasons.append('local_branch_contains_commit')
    remote_ref = stack_remote_ref(manifest, stack)
    if remote_ref in remote_branches:
        reasons.append('remote_branch_contains_commit')
    subject_lower = subject.lower()
    if stack['id'].lower() in subject_lower or stack['branch'].lower() in subject_lower:
        reasons.append('stack_name_matches_subject')
    return reasons


def related_declared_stack_commits(repo_root, manifest, subject):
    related = []
    subject_lower = subject.lower()
    for stack in manifest['stacks']:
        for declared in stack['commits']:
            if not commit_exists(repo_root, declared):
                continue
            declared_subject = commit_subject(repo_root, declared)
            if declared_subject.lower() == subject_lower:
                related.append({
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'commit': commit_full_sha(repo_root, declared),
                    'short': commit_short_sha(repo_root, declared),
                    'subject': declared_subject,
                    'reason': 'same_subject_declared_in_manifest',
                })
    return related


def ledger_stack_candidates_for_commit(ledger_state, manifest, local_branches, remote_branches):
    known = []
    current_ids = set(stack_map(manifest))
    seen = set()
    branch_candidates = [*local_branches, *remote_branches]
    for stack_id, stack in (ledger_state.get('stacks') or {}).items():
        if stack_id in current_ids:
            continue
        branch = stack.get('branch')
        if not branch:
            continue
        reasons = []
        for candidate in branch_candidates:
            if branch_ref_matches(candidate, branch):
                reasons.append('historical_branch_contains_commit')
                break
        if not reasons:
            continue
        dedupe_key = (stack_id, branch)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        known.append({
            'id': stack_id,
            'branch': branch,
            'base': stack.get('base'),
            'target_remote': stack.get('target_remote'),
            'target_branch': stack.get('target_branch'),
            'integration_branch': stack.get('integration_branch'),
            'reasons': reasons,
        })
    return known


def plan_resume_mutations(repo_root, manifest, diagnostics, selected_stack_ids=None):
    manifest_copy = json.loads(json.dumps(manifest))
    actions = []
    stacks = stack_map(manifest_copy)
    selected = set(selected_stack_ids or [])

    for item in diagnostics:
        if item.get('related_declared_commits'):
            actions.append({
                'type': 'resume_manual_review',
                'commit': item['commit'],
                'short': item['short'],
                'subject': item['subject'],
                'reason': 'same_subject_declared_in_manifest',
            })
            continue

        likely_stacks = [
            candidate['id']
            for candidate in item.get('likely_stacks') or []
            if not selected or candidate['id'] in selected
        ]
        historical_stacks = [
            candidate
            for candidate in item.get('historical_stacks') or []
            if not selected or candidate['id'] in selected
        ]

        stack_id = None
        reason = None
        if len(set(likely_stacks)) == 1:
            stack_id = likely_stacks[0]
            reason = 'single_likely_stack'
        elif likely_stacks:
            actions.append({
                'type': 'resume_manual_review',
                'commit': item['commit'],
                'short': item['short'],
                'subject': item['subject'],
                'reason': 'ambiguous_likely_stack',
                'stacks': sorted(set(likely_stacks)),
            })
            continue
        elif len({candidate['id'] for candidate in historical_stacks}) == 1:
            historical = historical_stacks[0]
            stack_id = historical['id']
            reason = 'single_historical_stack'
            if stack_id not in stacks:
                restored_stack = {
                    'id': historical['id'],
                    'branch': historical['branch'],
                    'base': historical.get('base') or manifest_copy['defaults']['base_ref'],
                    'target_remote': historical.get('target_remote') or manifest_copy['defaults']['canonical_remote'],
                    'target_branch': historical.get('target_branch') or manifest_copy['defaults']['base_branch'],
                    'integration_branch': historical.get('integration_branch') or manifest_copy['integration']['branch'],
                    'commits': [],
                    'meta': {},
                }
                if any(stack['branch'] == restored_stack['branch'] for stack in manifest_copy['stacks']):
                    actions.append({
                        'type': 'resume_manual_review',
                        'commit': item['commit'],
                        'short': item['short'],
                        'subject': item['subject'],
                        'reason': 'historical_branch_collision',
                        'branch': restored_stack['branch'],
                    })
                    continue
                manifest_copy['stacks'].append(restored_stack)
                stacks[stack_id] = restored_stack
                if stack_id not in manifest_copy['integration']['stacks']:
                    manifest_copy['integration']['stacks'].append(stack_id)
                actions.append({
                    'type': 'resume_restore_stack',
                    'stack': stack_id,
                    'branch': restored_stack['branch'],
                    'reason': 'single_historical_stack',
                })
        elif historical_stacks:
            actions.append({
                'type': 'resume_manual_review',
                'commit': item['commit'],
                'short': item['short'],
                'subject': item['subject'],
                'reason': 'ambiguous_historical_stack',
                'stacks': sorted({candidate['id'] for candidate in historical_stacks}),
            })
            continue
        else:
            actions.append({
                'type': 'resume_manual_review',
                'commit': item['commit'],
                'short': item['short'],
                'subject': item['subject'],
                'reason': 'owner_not_detected',
            })
            continue

        stack = stacks[stack_id]
        if item['commit'] not in stack['commits']:
            stack['commits'].append(item['commit'])
            actions.append({
                'type': 'resume_add_commit',
                'stack': stack_id,
                'branch': stack['branch'],
                'commit': item['commit'],
                'short': item['short'],
                'subject': item['subject'],
                'reason': reason,
            })

    return actions, manifest_copy


def integration_commit_diagnostics(repo_root, manifest, validation, manifest_path=None):
    ledger_state = load_ledger_state(repo_root, manifest_path)
    unmapped = validation['details']['integration'].get('unmapped_commits') or []
    diagnostics = []
    for commit in unmapped:
        subject = commit_subject(repo_root, commit)
        local_branches = branches_containing_commit(repo_root, commit)
        remote_branches = branches_containing_commit(repo_root, commit, remotes=True)
        related_declared = related_declared_stack_commits(repo_root, manifest, subject)
        likely_stacks = []
        for stack in manifest['stacks']:
            reasons = stack_hint_reasons(
                repo_root,
                manifest,
                stack,
                commit,
                subject,
                local_branches,
                remote_branches,
            )
            if reasons:
                likely_stacks.append({
                    'id': stack['id'],
                    'branch': stack['branch'],
                    'reasons': reasons,
                })
        historical_stacks = ledger_stack_candidates_for_commit(
            ledger_state,
            manifest,
            local_branches,
            remote_branches,
        )
        capture_draft_remedy = {
            'type': 'capture_integration_into_new_draft',
            'commands': [
                'syncwheel stack create --draft <new-stack-id> '
                '--purpose "Classify integration-first work"',
                'syncwheel stack capture-integration <new-stack-id> '
                + commit_short_sha(repo_root, commit),
            ],
        }
        suggested_commands = []
        notes = []
        if related_declared:
            notes.append(
                'A declared stack commit has the same subject; inspect before adding this local-only SHA.'
            )
            suggested_commands.append('syncwheel reconcile')
        elif len(likely_stacks) == 1:
            stack_id = likely_stacks[0]['id']
            suggested_commands.append(f'syncwheel stack add {stack_id} {commit_short_sha(repo_root, commit)}')
            suggested_commands.append('syncwheel reconcile')
        elif len(historical_stacks) == 1:
            suggested_commands.append('syncwheel resume --apply')
        elif likely_stacks:
            for item in likely_stacks:
                suggested_commands.append(
                    f"syncwheel stack add {item['id']} {commit_short_sha(repo_root, commit)}"
                )
            suggested_commands.append('syncwheel reconcile')
        else:
            suggested_commands.append('syncwheel stack add <stack-id> ' + commit_short_sha(repo_root, commit))
            suggested_commands.append('syncwheel reconcile')
        diagnostics.append({
            'commit': commit,
            'short': commit_short_sha(repo_root, commit),
            'subject': subject,
            'files': commit_changed_files(repo_root, commit, limit=8),
            'local_branches': local_branches,
            'remote_branches': remote_branches,
            'likely_stacks': likely_stacks,
            'historical_stacks': historical_stacks,
            'related_declared_commits': related_declared,
            'notes': notes,
            'suggested_commands': suggested_commands,
            'remedy': capture_draft_remedy,
        })
    return diagnostics


def print_integration_commit_diagnostics(diagnostics):
    if not diagnostics:
        return
    print('\nunmapped integration commits:')
    for item in diagnostics:
        print(f"  - {item['short']} {item['subject']}")
        if item['files']:
            print('    files:')
            for path in item['files']:
                print(f'      - {path}')
        if item['local_branches']:
            print('    local branches containing commit:')
            for branch in item['local_branches']:
                print(f'      - {branch}')
        if item['remote_branches']:
            print('    remote branches containing commit:')
            for branch in item['remote_branches']:
                print(f'      - {branch}')
        if item['likely_stacks']:
            print('    likely stack owners:')
            for stack in item['likely_stacks']:
                reasons = ', '.join(stack['reasons'])
                print(f"      - {stack['id']} ({reasons})")
        else:
            print('    likely stack owners: none detected')
        if item.get('historical_stacks'):
            print('    historical stack owners:')
            for stack in item['historical_stacks']:
                reasons = ', '.join(stack['reasons'])
                print(f"      - {stack['id']} ({reasons})")
        if item.get('related_declared_commits'):
            print('    related declared commits:')
            for related in item['related_declared_commits']:
                print(
                    f"      - {related['short']} stack={related['stack']} "
                    f"reason={related['reason']}"
                )
        if item.get('notes'):
            print('    notes:')
            for note in item['notes']:
                print(f'      - {note}')
        if item.get('remedy'):
            print('    remedy: capture into a new draft stack:')
            for command in item['remedy']['commands']:
                print(f'      - {command}')
        print('    suggested commands:')
        for command in item['suggested_commands']:
            print(f'      - {command}')


def quoted(parts):
    return ' '.join(shlex.quote(part) for part in parts)


def quoted_with_env(env, argv):
    assignments = [] if not env else [f'{key}={shlex.quote(value)}' for key, value in env.items()]
    return ' '.join([*assignments, quoted(argv)])


def command_argv_env(command):
    if isinstance(command, tuple):
        return command
    return command, None


def worktree_matches_branch(repo_root, branch, worktree):
    if worktree is None:
        return False
    found = find_worktree_for_branch(repo_root, branch)
    if not found:
        return False
    return found.resolve() == Path(worktree).resolve()


def replay_step(kind, argv=None, env=None, render=None):
    return {
        'kind': kind,
        'argv': argv,
        'env': env,
        'render': render,
    }


def replay_exec_step(argv, env=None):
    return replay_step('exec', argv=argv, env=env, render=quoted(argv))


def replay_shell_step(render, **details):
    step = replay_step('shell', render=render)
    step.update(details)
    return step


def replay_step_render(step):
    render = step.get('render')
    if step['kind'] == 'exec':
        render = render if render is not None else quoted(step['argv'])
        env = step.get('env')
        if not env:
            return render
        assignments = [f'{key}={shlex.quote(value)}' for key, value in env.items()]
        return ' '.join([*assignments, render])
    return render or ''


def replay_commit_message(repo_root, commit):
    return git(repo_root, 'show', '-s', '--format=%B', commit).stdout.rstrip('\n')


def shell_ref(reference):
    if reference.startswith('$'):
        return f'"{reference}"'
    return shlex.quote(reference)


def empty_replay_shell_error():
    lines = []
    for line in EMPTY_REPLAY_MESSAGE.splitlines():
        lines.append(f"  printf '%s\\n' {shlex.quote(line)} >&2")
    return lines


def plumbing_replay_script(repo_root, branch, base, commits):
    """Render an object-only replay as one POSIX shell transaction."""
    lines = ['set -e']
    head = base
    for declared_commit in commits:
        commit = commit_full_sha(repo_root, declared_commit)
        parent = shell_ref(head)
        merge_tree = ' '.join([
            quoted(['git', 'merge-tree', '--write-tree', f'--merge-base={commit}^']),
            parent,
            shlex.quote(commit),
        ])
        lines.append(f'T=$({merge_tree}) || {{')
        lines.append('  status=$?')
        lines.append('  printf \'%s\\n\' "$T" >&2')
        lines.append('  exit "$status"')
        lines.append('}')
        lines.append(f'if test "$T" = "$(git rev-parse {parent}^{{tree}})"; then')
        lines.extend(empty_replay_shell_error())
        lines.append('  exit 1')
        lines.append('fi')
        commit_tree = quoted_with_env(replay_commit_env(repo_root, commit), ['git', 'commit-tree'])
        lines.append(
            f'N=$({commit_tree} "$T" -p {parent} -m {shlex.quote(replay_commit_message(repo_root, commit))})'
        )
        head = '$N'
    lines.append(f'git update-ref refs/heads/{branch} {shell_ref(head)}')
    return '\n'.join(lines)


def merge_tree_conflict_paths(output):
    """Extract conflict paths from the documented human-readable merge-tree output."""
    paths = []
    for line in output.splitlines():
        metadata, separator, path = line.partition('\t')
        fields = metadata.split()
        if separator and len(fields) == 3 and fields[-1] in {'1', '2', '3'}:
            paths.append(path)
        marker = 'Merge conflict in '
        if marker in line:
            paths.append(line.split(marker, 1)[1].rstrip('.'))
    return list(dict.fromkeys(paths))


def replay_conflict_retry_command(target):
    if target['kind'] == 'stack':
        return f"syncwheel stack rebuild {target['stack_id']} --replay-mode desk"
    return 'syncwheel int rebuild --replay-mode desk'


def require_replay_success(result):
    """Turn a structured plumbing conflict into the explicit CLI escalation."""
    if result['status'] != 'conflict':
        return
    paths = result['conflict']['paths']
    print(f"replay mode: {result['mode']}", file=sys.stderr)
    print('replay conflict paths:', file=sys.stderr)
    for path in paths:
        print(f'  {path}', file=sys.stderr)
    print('retry with a desk worktree:', file=sys.stderr)
    print(f"  {replay_conflict_retry_command(result['target'])}", file=sys.stderr)
    raise SyncwheelError('plumbing replay stopped before updating the target ref')


def ensure_plumbing_target_is_unchecked_out(repo_root, branch):
    worktree = find_worktree_for_branch(repo_root, branch)
    if worktree:
        raise SyncwheelError(
            f"replay mode plumbing requires target branch {branch!r} to be unchecked out; "
            'use --replay-mode ephemeral or desk'
        )


def replay_target(
    stack=None,
    integration=None,
    worktree=None,
    return_tree=False,
    skip_contained=False,
    stack_ref_overrides=None,
):
    # Projections skip contained commits; rebuilds replay their declared history.
    if stack is not None:
        return {
            'kind': 'stack',
            'stack_id': stack['id'],
            'stack': stack,
            'worktree': worktree,
            'return_tree': return_tree,
            'skip_contained': skip_contained,
        }
    return {
        'kind': 'integration',
        'integration': integration,
        'worktree': worktree,
        'return_tree': return_tree,
        'skip_contained': skip_contained,
        'stack_ref_overrides': stack_ref_overrides or {},
    }


def replay_plan(repo_root, manifest, target, mode):
    """Build one replay plan without executing it."""
    if mode not in ('desk', 'in-place', 'ephemeral', 'plumbing'):
        raise SyncwheelError(f'unsupported replay mode: {mode}')

    projection = target.get('return_tree', False)
    worktree = target.get('worktree')
    if mode == 'ephemeral':
        worktree = EPHEMERAL_WORKTREE_PLACEHOLDER
    if target['kind'] == 'stack':
        stack = target['stack']
        branch = stack['branch']
        base = stack['base']
        replay_kind = 'stack'
        commits = stack['commits']
        integration = None
    elif target['kind'] == 'integration':
        integration = target['integration']
        branch = integration['branch']
        base = integration['base']
        replay_kind = 'integration'
        commits = None
    else:
        raise SyncwheelError(f"unsupported replay target: {target['kind']}")

    plan_target = {
        'kind': replay_kind,
        'stack_id': target.get('stack_id'),
        'branch': branch,
        'base': base,
        'worktree': str(worktree) if worktree is not None else None,
        'return_tree': projection,
        'skip_contained': target.get('skip_contained', False),
        'emit_output': not projection,
    }
    steps = []
    if projection:
        if mode != 'desk' or worktree is None:
            raise SyncwheelError('tree projection requires a desk worktree')
        steps.append(replay_exec_step([
            'git', 'worktree', 'add', '--detach', '--quiet', str(worktree), base,
        ]))
    else:
        timestamp = syncwheel_timestamp()
        steps.append(replay_exec_step(['git', 'fetch', '--all', '--prune']))
        backup = backup_branch_command(repo_root, branch, timestamp)
        if backup:
            steps.append(replay_exec_step(backup))
        if mode == 'plumbing':
            pass
        elif mode == 'in-place':
            steps.append(replay_exec_step(['git', 'reset', '--hard', base]))
        elif mode == 'ephemeral':
            steps.append(replay_exec_step([
                'git', 'worktree', 'add', '--detach', '--quiet', str(worktree), base,
            ]))
        elif worktree_matches_branch(repo_root, branch, worktree):
            steps.append(replay_exec_step(['git', '-C', str(worktree), 'reset', '--hard', base]))
        elif worktree is not None:
            steps.append(replay_exec_step(['git', 'worktree', 'add', '-B', branch, str(worktree), base]))
        else:
            raise SyncwheelError('desk replay requires a worktree path')

    if mode == 'plumbing':
        if replay_kind == 'stack':
            plumbing_commits = commits
        elif integration.get('strategy', 'cherry-pick') == 'cherry-pick':
            stacks_by_id = stack_map(manifest)
            plumbing_commits = [
                commit
                for stack_id in integration['stacks']
                for commit in stack_integration_commits(stacks_by_id[stack_id])
            ]
        else:
            raise SyncwheelError('replay mode plumbing supports cherry-pick integration only')
        steps.append(replay_shell_step(
            plumbing_replay_script(repo_root, branch, base, plumbing_commits),
            plumbing=True,
        ))
        return {
            'mode': mode,
            'target': plan_target,
            'steps': steps,
            'fallback_from': None,
        }

    prefix = ['git'] if mode == 'in-place' else ['git', '-C', str(worktree)]
    if replay_kind == 'stack':
        for commit in commits:
            steps.append(replay_exec_step(
                [*prefix, 'cherry-pick', commit],
                replay_commit_env(repo_root, commit),
            ))
    elif integration.get('strategy', 'cherry-pick') == 'cherry-pick':
        stacks_by_id = stack_map(manifest)
        for stack_id in integration['stacks']:
            for commit in stack_integration_commits(stacks_by_id[stack_id]):
                steps.append(replay_exec_step(
                    [*prefix, 'cherry-pick', commit],
                    replay_commit_env(repo_root, commit),
                ))
    elif integration.get('strategy') == 'merge-stacks':
        stacks_by_id = stack_map(manifest)
        stack_ref_overrides = target.get('stack_ref_overrides') or {}
        for stack_id in integration['stacks']:
            stack = stacks_by_id[stack_id]
            stack_ref = stack_ref_overrides.get(stack_id, stack['branch'])
            steps.append(replay_exec_step(
                [
                    *prefix,
                    'merge',
                    '--no-ff',
                    stack_ref,
                    '-m',
                    f"Merge stack '{stack_id}' into {branch}",
                ],
                replay_commit_env(repo_root, stack_ref),
            ))
    else:
        raise SyncwheelError(f"unsupported integration strategy: {integration.get('strategy')}")

    if mode == 'ephemeral':
        steps.append(replay_exec_step([
            'git', '-C', str(worktree), 'update-ref', f'refs/heads/{branch}', 'HEAD',
        ]))

    return {
        'mode': mode,
        'target': plan_target,
        'steps': steps,
        'fallback_from': None,
    }


def bind_ephemeral_worktree(plan, worktree):
    """Replace the dry-run-only ephemeral path with one temporary worktree."""
    worktree = str(worktree)
    target = dict(plan['target'])
    target['worktree'] = worktree
    steps = []
    for step in plan['steps']:
        bound = dict(step)
        if step['kind'] == 'exec':
            bound['argv'] = [
                worktree if value == EPHEMERAL_WORKTREE_PLACEHOLDER else value
                for value in step['argv']
            ]
            bound['render'] = quoted(bound['argv'])
        steps.append(bound)
    return {**plan, 'target': target, 'steps': steps}


def execute_replay_steps(repo_root, plan):
    """Execute an already-materialized replay plan."""
    target = plan['target']
    branch = target['branch']
    before_tip = ref_tip(repo_root, branch)
    result = {
        'status': 'applied',
        'mode': plan['mode'],
        'branch': branch,
        'before_tip': before_tip,
        'after_tip': before_tip,
        'conflict': None,
    }
    cleanup_worktree = target['worktree'] if target.get('return_tree') else None
    try:
        for step in plan['steps']:
            if step['kind'] == 'exec':
                argv = step['argv']
                env = step['env']
                if target.get('skip_contained') and 'cherry-pick' in argv:
                    command_cwd = git_command_cwd(repo_root, argv)
                    if branch_contains(command_cwd, 'HEAD', argv[-1]):
                        continue
                effective_argv = argv if env is not None else with_git_identity(repo_root, argv)
                run(effective_argv, cwd=repo_root, env=env)
            elif step['kind'] == 'shell':
                process_env = os.environ.copy()
                if step['env']:
                    process_env.update(step['env'])
                result_shell = subprocess.run(
                    step['render'],
                    cwd=repo_root,
                    shell=True,
                    text=True,
                    capture_output=True,
                    env=process_env,
                )
                if result_shell.returncode != 0:
                    output = result_shell.stdout + result_shell.stderr
                    if step.get('plumbing'):
                        paths = merge_tree_conflict_paths(output)
                        if paths:
                            result['status'] = 'conflict'
                            result['conflict'] = {'paths': paths}
                            result['target'] = target
                            return result
                    raise SyncwheelError(result_shell.stderr.strip() or result_shell.stdout.strip())
            elif step['kind'] != 'note':
                raise SyncwheelError(f"unsupported replay step: {step['kind']}")
            if target['emit_output']:
                render = replay_step_render(step)
                if render:
                    print(render)
        if plan['mode'] == 'ephemeral':
            target_worktree = find_worktree_for_branch(repo_root, branch)
            if target_worktree:
                run(['git', '-C', str(target_worktree), 'reset', '--hard', branch], cwd=repo_root)
        if target.get('return_tree'):
            result['tree'] = ref_tree(target['worktree'], 'HEAD')
        result['after_tip'] = ref_tip(repo_root, branch)
        return result
    finally:
        if cleanup_worktree:
            git(repo_root, 'worktree', 'remove', '--force', cleanup_worktree, check=False)


def execute_replay(repo_root, plan, apply):
    """Apply a replay plan or render its dry-run transcript.

    Dry-run output is an executable POSIX shell transcript. Non-plumbing
    ``exec`` steps retain ``quoted(argv)`` exactly; plumbing uses a shell step
    because its object IDs flow through command substitutions.
    """
    if not apply:
        target = plan['target']
        branch = target['branch']
        before_tip = ref_tip(repo_root, branch)
        result = {
            'status': 'planned',
            'mode': plan['mode'],
            'branch': branch,
            'before_tip': before_tip,
            'after_tip': before_tip,
            'conflict': None,
        }
        for step in plan['steps']:
            render = replay_step_render(step)
            if render:
                print(render)
        return result

    if plan['mode'] == 'plumbing':
        ensure_plumbing_target_is_unchecked_out(repo_root, plan['target']['branch'])

    if plan['mode'] == 'ephemeral':
        with tempfile.TemporaryDirectory(prefix='syncwheel-replay-') as tmp:
            worktree = Path(tmp)
            bound_plan = bind_ephemeral_worktree(plan, worktree)
            try:
                return execute_replay_steps(repo_root, bound_plan)
            finally:
                git(repo_root, 'worktree', 'remove', '--force', worktree, check=False)
    return execute_replay_steps(repo_root, plan)


def materialize_stack_projection(repo_root, stack):
    with tempfile.TemporaryDirectory(prefix='syncwheel-stack-projection-') as tmp:
        plan = replay_plan(
            repo_root,
            None,
            replay_target(
                stack=stack,
                worktree=Path(tmp),
                return_tree=True,
                skip_contained=True,
            ),
            'desk',
        )
        return execute_replay(repo_root, plan, True)['tree']


def materialize_integration_projection(repo_root, manifest, stack_ref_overrides=None):
    with tempfile.TemporaryDirectory(prefix='syncwheel-projection-') as tmp:
        plan = replay_plan(
            repo_root,
            manifest,
            replay_target(
                integration=manifest['integration'],
                worktree=Path(tmp),
                return_tree=True,
                skip_contained=True,
                stack_ref_overrides=stack_ref_overrides,
            ),
            'desk',
        )
        return execute_replay(repo_root, plan, True)['tree']


def materialize_remote_align_commands(repo_root, branch, remote_ref, worktree=None, timestamp=None):
    timestamp = timestamp or syncwheel_timestamp()
    commands = [['git', 'fetch', '--all', '--prune']]
    backup = backup_branch_command(repo_root, branch, timestamp)
    if backup:
        commands.append(backup)
    if worktree is None:
        commands.append(['git', 'reset', '--hard', remote_ref])
        return commands
    if worktree_matches_branch(repo_root, branch, worktree):
        commands.append(['git', '-C', str(worktree), 'reset', '--hard', remote_ref])
        return commands
    commands.append(['git', 'worktree', 'add', '-B', branch, str(worktree), remote_ref])
    return commands


def run_command_list(commands, repo_root, apply):
    if not apply:
        for entry in commands:
            command, env = command_argv_env(entry)
            effective_command = command if env is not None else with_git_identity(repo_root, command)
            print(quoted_with_env(env, effective_command))
        return
    for entry in commands:
        command, env = command_argv_env(entry)
        effective_command = command if env is not None else with_git_identity(repo_root, command)
        run(effective_command, cwd=repo_root, env=env)
        print(quoted_with_env(env, effective_command))


def ensure_non_in_place_target_clean(repo_root, branch, worktree):
    if worktree is None:
        return
    path = Path(worktree).resolve()
    if worktree_matches_branch(repo_root, branch, path):
        ensure_clean_worktree(path)
        current_branch = get_current_branch(path)
        if current_branch != branch:
            raise SyncwheelError(
                f'{path} is expected to be on {branch!r} but is on {current_branch!r}'
            )


def command_init(args):
    repo_root = resolve_repo_root(args.repo)
    canonical_remote = args.canonical_remote
    base_branch = args.base_branch
    if args.personal:
        if args.manifest:
            raise SyncwheelError('use either --personal or --manifest, not both')
        manifest_path = personal_manifest_path(repo_root, args.personal)
        integration_branch = args.integration_branch or personal_integration_branch(args.personal)
    else:
        manifest_path = Path(args.manifest).expanduser() if args.manifest else repo_root / '.syncwheel' / 'manifest.json'
        integration_branch = args.integration_branch or DEFAULT_INTEGRATION_BRANCH
    tracking = normalize_syncwheel_tracking(args.syncwheel_tracking) if args.syncwheel_tracking else None
    publication_remote = args.publication_remote or (
        canonical_remote if tracking == SYNCWHEEL_TRACKING_GIT_TRACKED else 'fork'
    )
    manifest = {
        'version': MANIFEST_VERSION_LEGACY,
        'defaults': {
            'canonical_remote': canonical_remote,
            'publication_remote': publication_remote,
            'base_branch': base_branch,
            'base_ref': f'{canonical_remote}/{base_branch}',
            'integration_membership': INTEGRATION_MEMBERSHIP_REQUIRED,
        },
        'integration': {
            'branch': integration_branch,
            'base': f'{canonical_remote}/{base_branch}',
            'strategy': 'cherry-pick',
            'stacks': [],
        },
        'stacks': [],
    }
    if tracking:
        manifest['syncwheel_tracking'] = tracking
    if args.worktree_root:
        manifest['syncwheel_worktree_root'] = normalize_syncwheel_worktree_root(args.worktree_root)
    if tracking:
        manifest['version'] = MANIFEST_VERSION_COORDINATED
        if tracking == SYNCWHEEL_TRACKING_GIT_TRACKED and not args.no_coordination:
            if not remote_is_configured(repo_root, publication_remote):
                raise SyncwheelError(
                    f"git-tracked initialization requires a configured publication remote: {publication_remote!r}. "
                    'Pass --publication-remote <remote>, configure that remote, or use --no-coordination.'
                )
            manifest['coordination'] = active_coordination_config(
                manifest_path,
                publication_remote,
                args.coordination_id,
            )
        else:
            manifest['coordination'] = disabled_coordination_config(
                manifest_path,
                publication_remote,
                args.coordination_id,
            )
    output = json.dumps(manifest, indent=2) + '\n'
    if args.stdout:
        print(output, end='')
        return 0
    if manifest_path.exists() and not args.force:
        raise SyncwheelError(f'manifest already exists: {manifest_path}')
    shared_manifest = not args.personal and not args.manifest
    if shared_manifest:
        primary = get_worktrees(repo_root)[0]
        primary_path = Path(primary['path'])
        current_branch = primary.get('branch', 'DETACHED')
        if current_branch != integration_branch:
            ensure_clean_worktree(primary_path)
            if branch_exists(repo_root, integration_branch):
                run(['git', '-C', str(primary_path), 'switch', integration_branch])
            else:
                start_point = manifest['integration']['base'] if ref_exists(repo_root, manifest['integration']['base']) else 'HEAD'
                run(['git', '-C', str(primary_path), 'switch', '-c', integration_branch, start_point])
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(output)
    append_ledger_event(repo_root, 'manifest_initialized', manifest_event_payload(manifest_path, manifest, 'init'), manifest_path)
    print(manifest_path)
    return 0


def command_coordination_init(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    tracking = manifest.get('syncwheel_tracking')
    if tracking not in SYNCWHEEL_TRACKING_VALUES:
        raise SyncwheelError(
            'coordination init requires an explicit syncwheel_tracking policy before migration'
        )
    existing = coordination_config(manifest) or {}
    if existing.get('mode') != 'active-active' and not args.remote:
        raise SyncwheelError(
            'active-active coordination is opt-in for legacy, local-only, or disabled manifests; '
            'pass --remote <configured-remote> explicitly'
        )
    remote = args.remote or existing.get('remote') or manifest['defaults']['publication_remote']
    if not remote_is_configured(repo_root, remote):
        raise SyncwheelError(f'coordination remote is not configured locally: {remote}')
    coordination_id = args.coordination_id or existing.get('id') or default_coordination_id(manifest_path)
    proposed = json.loads(json.dumps(manifest))
    proposed['version'] = MANIFEST_VERSION_COORDINATED
    proposed['defaults']['publication_remote'] = remote
    proposed['coordination'] = active_coordination_config(manifest_path, remote, coordination_id)
    if existing.get('gc'):
        proposed['coordination']['gc'] = normalize_coordination_gc(existing['gc'])
    if not args.apply:
        print(json.dumps({
            'manifest_path': str(manifest_path),
            'migration': 'active-active',
            'coordination': proposed['coordination'],
            'remote_state_created': False,
            'dry_run': True,
        }, indent=2))
        return 0
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        proposed,
        'coordination_init',
        {'coordination_id': proposed['coordination']['id'], 'remote': remote},
    )
    print(f"coordination enabled: {proposed['coordination']['id']}")
    print('remote state will be created by the first successful coordinated publish')
    return 0


def command_coordination_disable(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    proposed = json.loads(json.dumps(manifest))
    existing = coordination_config(proposed) or {}
    disabled = disabled_coordination_config(
        manifest_path,
        proposed['defaults']['publication_remote'],
        existing.get('id'),
    )
    if existing.get('gc'):
        disabled['gc'] = normalize_coordination_gc(existing['gc'])
    proposed['version'] = MANIFEST_VERSION_COORDINATED
    proposed['coordination'] = disabled
    if not args.apply:
        print(json.dumps({
            'manifest_path': str(manifest_path),
            'coordination': disabled,
            'remote_state_deleted': False,
            'dry_run': True,
        }, indent=2))
        return 0
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        proposed,
        'coordination_disabled',
        {'previous_mode': existing.get('mode', 'legacy')},
    )
    print('coordination disabled; no remote state branch was deleted')
    return 0


def command_worktree_lock(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    profile, coordination = coordination_profile(repo_root)
    locks = coordination.get('locks') or {}
    if not isinstance(locks, dict):
        raise SyncwheelError('syncwheel profile worktree locks must be an object')
    locks[stack['id']] = {'branch': stack['branch'], 'created_at': iso_utc_now()}
    coordination['locks'] = locks
    profile['coordination'] = coordination
    save_repo_profile(repo_root, profile)
    print(f"worktree lock created for {stack['id']}")
    return 0


def command_worktree_unlock(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    profile, coordination = coordination_profile(repo_root)
    locks = coordination.get('locks') or {}
    if not isinstance(locks, dict):
        raise SyncwheelError('syncwheel profile worktree locks must be an object')
    if args.stack not in locks:
        require_stack(manifest, args.stack)
    locks.pop(args.stack, None)
    coordination['locks'] = locks
    profile['coordination'] = coordination
    save_repo_profile(repo_root, profile)
    print(f"worktree lock removed for {args.stack}")
    return 0


def command_use(args):
    repo_root = resolve_repo_root(args.repo)
    if args.shared:
        path = repo_profile_path(repo_root)
        if path.exists():
            path.unlink()
        print('using shared manifest')
        return 0
    if not args.personal:
        profile = load_repo_profile(repo_root)
        personal = profile.get('personal')
        if personal:
            print(f'using personal manifest: {personal}')
            print(personal_manifest_path(repo_root, personal))
        else:
            print('using shared manifest')
            print(repo_root / '.syncwheel' / 'manifest.json')
        return 0
    personal = safe_ref_segment(args.personal)
    path = save_repo_profile(repo_root, {'personal': personal})
    print(f'using personal manifest: {personal}')
    print(path)
    return 0


def command_status(args):
    repo_root = resolve_repo_root(args.repo)
    if args.fetch:
        git(repo_root, 'fetch', '--all', '--prune', '--quiet', check=False)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    manifest, manifest_path = load_manifest(repo_root, manifest_path)
    snapshot = collect_repo_snapshot(repo_root, manifest)
    output = {'snapshot': snapshot, 'manifest_path': str(manifest_path), 'manifest_present': manifest is not None}
    if manifest:
        validation = validate_manifest(repo_root, manifest)
        output['validation'] = validation
        output['plan'] = build_plan(repo_root, manifest, validation)
    if args.json:
        print(json.dumps(output, indent=2))
        return 1 if manifest and not output['validation']['details']['primary_checkout']['compliant'] else 0
    print(f"repo: {snapshot['repo_root']}")
    print(f"current_branch: {snapshot['current_branch']}")
    print(f"canonical_remote_head: {snapshot['canonical_remote_head'] or 'unknown'}")
    print(f"manifest: {manifest_path if manifest else 'missing'}")
    print('\nremotes:')
    for line in snapshot['remotes']:
        print(f'  - {line}')
    print('\nworktrees:')
    for worktree in snapshot['worktrees']:
        branch = worktree.get('branch', 'DETACHED')
        print(f"  - {worktree.get('path')} ({branch})")
    print('\nstashes:')
    if snapshot['stashes']:
        for line in snapshot['stashes']:
            print(f'  - {line}')
    else:
        print('  - none')
    if manifest:
        validation = output['validation']
        print('\nmanifest validation:')
        if validation['errors']:
            for line in validation['errors']:
                print(f'  - ERROR: {line}')
        if validation['warnings']:
            for line in validation['warnings']:
                print(f'  - WARN: {line}')
        if not validation['errors'] and not validation['warnings']:
            print('  - OK')
        print('\nstack state:')
        for item in validation['details']['stacks']:
            summary = []
            summary.append('branch=present' if item['branch_exists'] else 'branch=missing')
            if item['missing_from_branch']:
                summary.append(f"missing_from_branch={len(item['missing_from_branch'])}")
            if item['missing_from_integration']:
                summary.append(f"missing_from_integration={len(item['missing_from_integration'])}")
            if item['missing_commits']:
                summary.append(f"missing_commits={len(item['missing_commits'])}")
            print(f"  - {item['id']}: {', '.join(summary)}")
        print('\nplan:')
        if output['plan']:
            for action in output['plan']:
                line = action['type']
                if 'stack' in action:
                    line += f" stack={action['stack']}"
                if 'branch' in action:
                    line += f" branch={action['branch']}"
                print(f'  - {line}')
        else:
            print('  - no actions needed')
    return 1 if manifest and not output['validation']['details']['primary_checkout']['compliant'] else 0


def command_validate(args):
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    manifest, manifest_path = load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    validation = validate_manifest(repo_root, manifest)
    if args.json:
        print(json.dumps(validation, indent=2))
    else:
        for line in validation['errors']:
            print(f'ERROR: {line}')
        for line in validation['warnings']:
            print(f'WARN: {line}')
        if not validation['errors'] and not validation['warnings']:
            print('OK')
    return 1 if validation['errors'] else 0


def command_plan(args):
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    manifest, manifest_path = load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    validation = validate_manifest(repo_root, manifest)
    plan = build_plan(repo_root, manifest, validation)
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        if not plan:
            print('no actions needed')
        for action in plan:
            print(json.dumps(action, sort_keys=True))
    return 1 if validation['errors'] else 0


def command_check(args):
    repo_root = resolve_repo_root(args.repo)
    if args.fetch:
        git(repo_root, 'fetch', '--all', '--prune', '--quiet', check=False)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    manifest, manifest_path = load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    snapshot = collect_repo_snapshot(repo_root, manifest)
    validation = validate_manifest(repo_root, manifest)
    plan = build_plan(repo_root, manifest, validation)
    diagnostics = integration_commit_diagnostics(repo_root, manifest, validation)
    output = {
        'snapshot': snapshot,
        'manifest_path': str(manifest_path),
        'validation': validation,
        'plan': plan,
        'diagnostics': {
            'unmapped_integration_commits': diagnostics,
        },
    }
    if args.json:
        print(json.dumps(output, indent=2))
        return 1 if validation['errors'] else 0
    print(f"repo: {snapshot['repo_root']}")
    print(f"branch: {snapshot['current_branch']}")
    print(f"manifest: {manifest_path}")
    if validation['errors']:
        print('\nvalidation:')
        for line in validation['errors']:
            print(f'  - ERROR: {line}')
    if validation['warnings']:
        if not validation['errors']:
            print('\nvalidation:')
        for line in validation['warnings']:
            print(f'  - WARN: {line}')
    if not validation['errors'] and not validation['warnings']:
        print('\nvalidation: OK')
    print_integration_commit_diagnostics(diagnostics)
    print('\nplan:')
    if not plan:
        print('  - no actions needed')
    for action in plan:
        line = action['type']
        if 'stack' in action:
            line += f" stack={action['stack']}"
        if 'branch' in action:
            line += f" branch={action['branch']}"
        print(f'  - {line}')
    return 1 if validation['errors'] else 0


def command_stack_list(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    for stack in manifest['stacks']:
        print(
            f"{stack['id']}\t{stack['branch']}\tcommits={len(stack['commits'])}"
            f"\tstate={stack['state']}"
        )
    return 0


def command_stack_show(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    print(json.dumps(stack, indent=2))
    return 0


def command_stack_close(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    branch = stack['branch']
    base_ref = stack.get('base') or manifest['defaults']['base_ref']

    # Check whether every commit in the stack is already reachable from base_ref.
    unmerged = []
    for sha in stack.get('commits') or []:
        result = git(repo_root, 'merge-base', '--is-ancestor', sha, base_ref, check=False)
        if result.returncode != 0:
            unmerged.append(sha)

    if unmerged and not args.force:
        short = [commit_short_sha(repo_root, sha) for sha in unmerged[:5]]
        extra = f' (and {len(unmerged) - 5} more)' if len(unmerged) > 5 else ''
        raise SyncwheelError(
            f"{args.stack}: {len(unmerged)} commit(s) are NOT yet reachable from {base_ref}: "
            f"{', '.join(short)}{extra}\n"
            f"Pass --force to close the stack anyway."
        )

    merged_note = '' if unmerged else f' (all commits confirmed in {base_ref})'

    # Remove from stacks list.
    manifest['stacks'] = [s for s in manifest['stacks'] if s['id'] != args.stack]
    # Remove from integration stacks list.
    if args.stack in manifest['integration'].get('stacks', []):
        manifest['integration']['stacks'] = [
            s for s in manifest['integration']['stacks'] if s != args.stack
        ]

    reason = args.reason or ('merged' if not unmerged else 'closed')
    coordination_result = None
    if coordination_is_active(manifest):
        config = coordination_config(manifest)
        closed_ref = f'refs/heads/{branch}'
        remote_tip = remote_ref_tips(repo_root, config['remote'], [closed_ref])[closed_ref]
        coordination_result = coordinated_publish(
            repo_root,
            manifest,
            manifest_path,
            {},
            f'close:{args.stack}',
            'partial',
            tombstone={
                'stack': args.stack,
                'branch': branch,
                'ref': closed_ref,
                'reason': reason,
                'closed_at': iso_utc_now(),
                'remote_tip': remote_tip,
            },
        )
    save_manifest(manifest_path, manifest)
    append_ledger_event(
        repo_root,
        'stack_closed',
        {
            'stack': args.stack,
            'branch': branch,
            'reason': reason,
            'coordination_state': coordination_result.get('state_tip') if coordination_result else None,
        },
        manifest_path,
    )

    print(f"{args.stack}: closed{merged_note}")
    print(f"  branch : {branch}")
    print(f"  reason : {reason}")
    print(f"  removed from integration: {manifest['integration']['branch']}")
    if coordination_result:
        print(f"  coordination state: {coordination_result['status']}")

    if args.delete_branch:
        if branch_exists(repo_root, branch):
            git(repo_root, 'branch', '-d', branch, check=False)
            print(f"  local branch deleted: {branch}")
        else:
            print(f"  local branch already absent: {branch}")
    else:
        print(f"  tip: run 'git branch -d {branch}' to delete the local branch when ready")

    return 0


def command_stack_create(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stacks = stack_map(manifest)
    if args.stack in stacks:
        raise SyncwheelError(f"stack already exists: {args.stack}")
    if args.draft and args.branch:
        raise SyncwheelError('--draft chooses the reserved syncwheel/draft branch name; omit --branch')
    branch = (
        f'syncwheel/draft/{safe_ref_segment(args.stack)}'
        if args.draft
        else args.branch or f'pr/{safe_ref_segment(args.stack)}'
    )
    if any(stack['branch'] == branch for stack in manifest['stacks']):
        raise SyncwheelError(f'stack branch already exists in manifest: {branch}')
    if args.draft and branch_exists(repo_root, branch):
        raise SyncwheelError(f'draft stack branch already exists locally: {branch}')
    commits = []
    for spec in args.specs:
        commits.extend(commit_list_for_spec(repo_root, spec))
    stack = {
        'id': args.stack,
        'branch': branch,
        'base': args.base or manifest['defaults']['base_ref'],
        'target_remote': args.target_remote or manifest['defaults']['canonical_remote'],
        'target_branch': args.target_branch or manifest['defaults']['base_branch'],
        'integration_branch': args.integration_branch or manifest['integration']['branch'],
        'commits': list(dict.fromkeys(commits)),
        'state': 'draft' if args.draft else 'published',
        'publication': {'enabled': not args.draft},
        'meta': {},
    }
    if args.purpose:
        stack['meta'] = {'purpose': args.purpose}
    if args.draft:
        materialize_new_stack_branch(repo_root, stack)
    manifest['stacks'].append(stack)
    integration_membership = manifest['defaults']['integration_membership']
    if (
        integration_membership == INTEGRATION_MEMBERSHIP_REQUIRED
        or args.include_in_integration
    ) and args.stack not in manifest['integration']['stacks']:
        manifest['integration']['stacks'].append(args.stack)
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        manifest,
        'stack_create',
        {'stack': args.stack, 'branch': branch},
    )
    print(f"{args.stack}: created {branch} with {len(stack['commits'])} commits (state={stack['state']})")
    return 0


def materialize_new_stack_branch(repo_root, stack):
    """Create a new stack branch without leaving a persistent worktree behind."""
    with tempfile.TemporaryDirectory(prefix='syncwheel-stack-create-') as tmp:
        worktree = Path(tmp)
        git(repo_root, 'worktree', 'add', '-B', stack['branch'], str(worktree), stack['base'])
        try:
            for commit in stack['commits']:
                if branch_contains(worktree, 'HEAD', commit):
                    continue
                run(
                    ['git', '-C', str(worktree), 'cherry-pick', commit],
                    cwd=repo_root,
                    env=replay_commit_env(repo_root, commit),
                )
        finally:
            git(repo_root, 'worktree', 'remove', '--force', str(worktree), check=False)


def command_stack_promote(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    if stack.get('state', 'published') != 'draft':
        raise SyncwheelError(f"{args.stack}: promote requires state draft (found {stack.get('state', 'published')})")
    from_branch = stack['branch']
    if not branch_exists(repo_root, from_branch):
        raise SyncwheelError(f"{args.stack}: cannot promote draft without a materialized branch: {from_branch}")
    to_branch = args.branch or f'pr/{safe_ref_segment(args.stack)}'
    if to_branch != from_branch:
        if any(
            other['id'] != args.stack and other['branch'] == to_branch
            for other in manifest['stacks']
        ):
            raise SyncwheelError(f'stack branch already exists in manifest: {to_branch}')
        if branch_exists(repo_root, to_branch):
            raise SyncwheelError(f'cannot promote onto an existing local branch: {to_branch}')

    previous_worktree_path = None
    worktree_root = effective_worktree_root(manifest)
    if worktree_root:
        safe = from_branch.replace('/', '-').replace('\\', '-')
        previous_worktree_path = resolve_worktree_root_path(repo_root, worktree_root) / safe

    stack['branch'] = to_branch
    stack['state'] = 'published'
    stack['publication'] = {'enabled': True}
    renamed = False
    coordination_result = None
    try:
        if to_branch != from_branch:
            run(['git', 'branch', '-m', from_branch, to_branch], cwd=repo_root)
            renamed = True

        if coordination_is_active(manifest):
            config = coordination_config(manifest)
            if to_branch != from_branch:
                from_ref = f'refs/heads/{from_branch}'
                from_ref_tip = remote_ref_tips(repo_root, config['remote'], [from_ref])[from_ref]
                rename = {
                    'stack': args.stack,
                    'from_branch': from_branch,
                    'to_branch': to_branch,
                    'from_ref_tip': from_ref_tip,
                }
                tombstone = None
                if from_ref_tip:
                    tombstone = {
                        'stack': args.stack,
                        'branch': from_branch,
                        'ref': from_ref,
                        'reason': 'promoted',
                        'closed_at': iso_utc_now(),
                        'remote_tip': from_ref_tip,
                    }
                coordination_result = coordinated_publish(
                    repo_root,
                    manifest,
                    manifest_path,
                    {f'refs/heads/{to_branch}': ref_tip(repo_root, to_branch)},
                    f'promote:{args.stack}',
                    'partial',
                    tombstone=tombstone,
                    rename=rename,
                )
            else:
                coordination_result = coordinated_publish(
                    repo_root,
                    manifest,
                    manifest_path,
                    {},
                    f'promote:{args.stack}',
                    'partial',
                    state_transition={
                        'stack': args.stack,
                        'from_state': 'draft',
                        'to_state': 'published',
                    },
                )
    except Exception:
        if renamed and branch_exists(repo_root, to_branch) and not branch_exists(repo_root, from_branch):
            git(repo_root, 'branch', '-m', to_branch, from_branch, check=False)
        raise

    save_manifest(manifest_path, manifest)
    append_ledger_event(
        repo_root,
        'stack_promoted',
        {
            'stack': args.stack,
            'from_branch': from_branch,
            'branch': to_branch,
            'coordination_state': coordination_result.get('state_tip') if coordination_result else None,
        },
        manifest_path,
    )
    print(f'{args.stack}: promoted draft -> published')
    if to_branch != from_branch:
        print(f'  branch: {from_branch} -> {to_branch}')
        if previous_worktree_path and previous_worktree_path.exists():
            print(f'  worktree path retained (not moved): {previous_worktree_path}')
    else:
        print(f'  branch: {to_branch} (unchanged)')
    if coordination_result:
        print(f"  coordination state: {coordination_result['status']}")
    return 0


def command_stack_demote(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    if stack.get('state', 'published') != 'published':
        raise SyncwheelError(f"{args.stack}: demote requires state published (found {stack.get('state', 'published')})")
    github = stack.get('github')
    if isinstance(github, dict) and github.get('pr') not in (None, ''):
        raise SyncwheelError(f"{args.stack}: cannot demote while github.pr is set")

    stack['state'] = 'draft'
    stack['publication'] = {'enabled': False}
    coordination_result = None
    if coordination_is_active(manifest):
        coordination_result = coordinated_publish(
            repo_root,
            manifest,
            manifest_path,
            {},
            f'demote:{args.stack}',
            'partial',
            state_transition={
                'stack': args.stack,
                'from_state': 'published',
                'to_state': 'draft',
            },
        )
    save_manifest(manifest_path, manifest)
    append_ledger_event(
        repo_root,
        'stack_demoted',
        {
            'stack': args.stack,
            'branch': stack['branch'],
            'coordination_state': coordination_result.get('state_tip') if coordination_result else None,
        },
        manifest_path,
    )
    print(f'{args.stack}: demoted published -> draft')
    print(f"  branch: {stack['branch']} (unchanged)")
    if coordination_result:
        print(f"  coordination state: {coordination_result['status']}")
    return 0


def command_stack_sync(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    commits = rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
    stack['commits'] = commits
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        manifest,
        'stack_sync',
        {'stack': args.stack, 'branch': stack['branch']},
    )
    print(f"{args.stack}: synced {len(commits)} commits from {stack['branch']}")
    return 0


def command_stack_absorb(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    integration_branch = stack.get('integration_branch') or manifest['integration']['branch']
    current_branch = get_current_branch(repo_root)
    if current_branch != integration_branch and not args.force:
        raise SyncwheelError(
            f"stack absorb expects the integration branch {integration_branch!r}; "
            f"current branch is {current_branch!r}. Pass --force to override."
        )

    pathspec = args.paths or []
    separator = ['--', *pathspec] if pathspec else []
    if not args.staged:
        staged = git(repo_root, 'diff', '--cached', '--quiet', '--', *pathspec, check=False)
        if staged.returncode == 1:
            raise SyncwheelError('staged changes exist; pass --staged or unstage them before absorbing unstaged changes')

    diff_args = ['diff', '--binary']
    if args.staged:
        diff_args.append('--cached')
    diff_args.extend(separator)
    patch = git(repo_root, *diff_args).stdout
    if not patch.strip():
        source = 'staged changes' if args.staged else 'working tree changes'
        raise SyncwheelError(f'no {source} to absorb')

    stack_worktree = resolve_stack_absorb_location(repo_root, manifest_path, manifest, stack, args)
    ensure_clean_worktree(stack_worktree)
    apply_patch = run(['git', '-C', str(stack_worktree), 'apply', '--index'], input_text=patch, check=False)
    if apply_patch.returncode != 0:
        raise SyncwheelError(apply_patch.stderr.strip() or apply_patch.stdout.strip() or 'failed to apply patch to stack worktree')

    if args.amend:
        run(with_git_identity(stack_worktree, ['git', 'commit', '--amend', '--no-edit']), cwd=stack_worktree)
    else:
        message = args.message or f"chore: absorb integration changes into {args.stack}"
        run(with_git_identity(stack_worktree, ['git', 'commit', '-m', message]), cwd=stack_worktree)

    reverse_args = ['apply', '--reverse']
    if args.staged:
        git(repo_root, *reverse_args, '--cached', input_text=patch)
    git(repo_root, *reverse_args, input_text=patch)

    stack['commits'] = rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        manifest,
        'stack_absorb',
        {'stack': args.stack, 'branch': stack['branch']},
    )
    print(f"{args.stack}: absorbed changes into {stack['branch']} and synced {len(stack['commits'])} commits")
    return 0


def resolve_stack_absorb_location(repo_root, manifest_path, manifest, stack, args):
    branch = stack['branch']
    existing = find_worktree_for_branch(repo_root, branch)
    if args.worktree:
        path = Path(args.worktree).expanduser().resolve()
        if existing and existing != path:
            raise SyncwheelError(
                f"branch {branch!r} already has a worktree at {existing}; "
                'reuse that worktree or pass its path with --worktree'
            )
        if not existing:
            run(['git', 'worktree', 'add', '-B', branch, str(path), branch], cwd=repo_root)
        return path
    if existing:
        return existing
    path = reconcile_worktree_path(repo_root, branch, effective_worktree_root(manifest, args.worktree_root))
    worktree_root = effective_worktree_root(manifest, args.worktree_root)
    if is_external_manifest_path(repo_root, manifest_path):
        ensure_syncwheel_worktree_root_excluded(repo_root, worktree_root)
    else:
        ensure_syncwheel_metadata_excluded(repo_root, manifest.get('syncwheel_tracking'), worktree_root)
    run(['git', 'worktree', 'add', '-B', branch, str(path), branch], cwd=repo_root)
    return path


def command_stack_set(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    commits = []
    for spec in args.specs:
        commits.extend(commit_list_for_spec(repo_root, spec))
    stack['commits'] = list(dict.fromkeys(commits))
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        manifest,
        'stack_set',
        {'stack': args.stack, 'branch': stack['branch']},
    )
    print(f"{args.stack}: set {len(stack['commits'])} commits")
    return 0


def command_stack_resolve_integration(args):
    """Record conflict-resolved commits that already materialize on integration."""
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    integration_branch = manifest['integration']['branch']
    if args.empty and args.specs:
        raise SyncwheelError('use either --empty or resolved integration commit specs, not both')
    if not args.empty and not args.specs:
        raise SyncwheelError('provide resolved integration commit specs or pass --empty explicitly')
    commits = []
    for spec in args.specs:
        commits.extend(commit_list_for_spec(repo_root, spec))
    commits = list(dict.fromkeys(commits))
    for commit in commits:
        if not branch_contains(repo_root, integration_branch, commit):
            raise SyncwheelError(
                f"resolved integration commit is not on {integration_branch}: {commit}"
            )
    stack['integration_commits'] = commits
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        manifest,
        'stack_resolve_integration',
        {'stack': args.stack, 'integration_branch': integration_branch, 'commits': commits},
    )
    print(f"{args.stack}: recorded {len(commits)} resolved integration commits")
    return 0


def validate_stack_update(repo_root, manifest, stack, previous_commits):
    report = stack_reconcile_report(repo_root, manifest, stack)
    if report.get('projection_error'):
        stack['commits'] = previous_commits
        detail = report['projection_error']
        raise SyncwheelError(
            f"stack {stack['id']} projection failed after adding commits; "
            f"the stack branch cannot be rebuilt cleanly from the manifest:\n{detail}"
        )


def validate_integration_first_base(repo_root, manifest, added_commits):
    if not added_commits:
        return
    integration = manifest['integration']
    integration_branch = integration['branch']
    if get_current_branch(repo_root) != integration_branch:
        return
    first_added = added_commits[0]
    if not branch_contains(repo_root, integration_branch, first_added):
        return
    parent = commit_first_parent(repo_root, first_added)
    if not parent:
        return
    expected_tree = materialize_integration_projection(repo_root, manifest)
    parent_tree = ref_tree(repo_root, parent)
    if parent_tree != expected_tree:
        raise SyncwheelError(
            f"cannot add {first_added} from integration branch {integration_branch!r}: "
            "the commit was not created on top of the current manifest projection. "
            "Run `syncwheel reconcile` and apply the required integration rebuild before "
            "creating or adding more integration-first commits."
        )


def command_stack_add(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    previous_commits = list(stack['commits'])
    commits = list(previous_commits)
    previous_full_shas = {commit_full_sha(repo_root, commit) for commit in previous_commits if commit_exists(repo_root, commit)}
    added_commits = []
    for spec in args.specs:
        for commit in commit_list_for_spec(repo_root, spec):
            commits.append(commit)
            if commit_full_sha(repo_root, commit) not in previous_full_shas:
                added_commits.append(commit)
    validate_integration_first_base(repo_root, manifest, added_commits)
    stack['commits'] = list(dict.fromkeys(commits))
    validate_stack_update(repo_root, manifest, stack, previous_commits)
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        manifest,
        'stack_add',
        {'stack': args.stack, 'branch': stack['branch'], 'added_commits': added_commits},
    )
    print(f"{args.stack}: now has {len(stack['commits'])} commits")
    return 0


def rebuild_stack_from_manifest(
    repo_root,
    manifest,
    manifest_path,
    stack,
    *,
    dry_run,
    mode,
    worktree,
):
    """Rebuild one stack through the shared replay executor and ledger path."""
    if not dry_run and mode == 'in-place':
        ensure_in_place_target(repo_root, stack['branch'])
    if not dry_run and mode in ('ephemeral', 'plumbing'):
        ensure_non_in_place_target_clean(
            repo_root,
            stack['branch'],
            find_worktree_for_branch(repo_root, stack['branch']),
        )
    if not dry_run and mode == 'desk':
        ensure_non_in_place_target_clean(repo_root, stack['branch'], worktree)
    result = execute_replay(
        repo_root,
        replay_plan(repo_root, manifest, replay_target(stack=stack, worktree=worktree), mode),
        not dry_run,
    )
    require_replay_success(result)
    if not dry_run:
        append_ledger_event(
            repo_root,
            'stack_rebuilt',
            {
                'stack': stack['id'],
                'branch': stack['branch'],
                'base': stack['base'],
                'integration_branch': stack.get('integration_branch'),
                'before_tip': result['before_tip'],
                'after_tip': result['after_tip'],
            },
            manifest_path,
        )
    return result


def command_stack_capture_integration(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)

    # Expand every requested spec before changing stack ownership or refs.
    captured_commits = []
    for spec in args.specs:
        captured_commits.extend(commit_list_for_spec(repo_root, spec))

    previous_commits = list(stack['commits'])
    previous_full_shas = {
        commit_full_sha(repo_root, commit)
        for commit in previous_commits
        if commit_exists(repo_root, commit)
    }
    added_commits = []
    for commit in captured_commits:
        full_sha = commit_full_sha(repo_root, commit)
        if full_sha not in previous_full_shas:
            added_commits.append(full_sha)
            previous_full_shas.add(full_sha)

    # This guard must remain before stack mutation: integration-first commits
    # can only be captured from the current manifest projection.
    validate_integration_first_base(repo_root, manifest, added_commits)

    commits = []
    seen_commits = set()
    for commit in [*previous_commits, *captured_commits]:
        full_sha = commit_full_sha(repo_root, commit)
        if full_sha not in seen_commits:
            seen_commits.add(full_sha)
            commits.append(full_sha)
    stack['commits'] = commits
    validate_stack_update(repo_root, manifest, stack, previous_commits)

    # Capture owns an integration SHA, so it must materialize the same SHA on
    # the stack ref before the manifest can record that ownership. The shared
    # executor creates the usual backup ref and ledger event; R4's ephemeral
    # mode removes its detached worktree before returning.
    rebuild_stack_from_manifest(
        repo_root,
        manifest,
        manifest_path,
        stack,
        dry_run=False,
        mode='ephemeral',
        worktree=None,
    )
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        manifest,
        'stack_capture_integration',
        {'stack': args.stack, 'branch': stack['branch'], 'added_commits': added_commits},
    )
    print(f"{args.stack}: captured {len(added_commits)} integration commit(s)")
    return 0


def command_stack_rebuild(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    mode, worktree = resolve_replay_mode(
        repo_root,
        resolve_stack_rebuild_location(repo_root, stack, args),
        requested_replay_mode(args),
    )
    rebuild_stack_from_manifest(
        repo_root,
        manifest,
        manifest_path,
        stack,
        dry_run=args.dry_run,
        mode=mode,
        worktree=worktree,
    )
    return 0


def command_stack_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    refusal = draft_push_refusal(manifest, stack, stack_push_remote(manifest, stack, args.remote))
    if refusal:
        raise SyncwheelError(refusal)
    if coordination_is_active(manifest):
        config = coordination_config(manifest)
        coordinated_push_remote(args, config)
        result = coordinated_publish(
            repo_root,
            manifest,
            manifest_path,
            {f"refs/heads/{stack['branch']}": ref_tip(repo_root, stack['branch'])},
            f"stack:{stack['id']}",
            'partial',
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            append_ledger_event(
                repo_root,
                'stack_pushed',
                {
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'remote': config['remote'],
                    'tip': ref_tip(repo_root, stack['branch']),
                    'coordination_state': result.get('state_tip'),
                    'coordination_status': result['status'],
                },
                manifest_path,
            )
        return 0
    remote = stack_push_remote(manifest, stack, args.remote)
    push_args = push_args_with_options(args)
    command = ['git', 'push', *push_args, remote, stack['branch']]
    if args.dry_run:
        print(quoted(command))
        return 0
    run(command, cwd=repo_root)
    print(quoted(command))
    append_ledger_event(
        repo_root,
        'stack_pushed',
        {
            'stack': stack['id'],
            'branch': stack['branch'],
            'remote': remote,
            'tip': ref_tip(repo_root, stack['branch']),
        },
        manifest_path,
    )
    return 0


def command_stack_git(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    worktree = resolve_git_worktree(repo_root, stack['branch'], args.worktree, args.auto_worktree)
    git_args = passthrough_args(args.git_args)
    if not git_args:
        raise SyncwheelError('stack git requires git arguments after --')
    result = run(['git', *git_args], cwd=worktree, check=False)
    if result.stdout:
        print(result.stdout, end='')
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    return result.returncode


def command_int_show(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    print(json.dumps(manifest['integration'], indent=2))
    return 0


def remote_integration_ref(manifest, remote=None):
    integration = manifest['integration']
    remote = remote or manifest['defaults']['publication_remote']
    return f"{remote}/{integration['branch']}"


def rev_left_right_count(repo_root, left, right):
    result = git(repo_root, 'rev-list', '--left-right', '--count', f'{left}...{right}')
    left_count, right_count = result.stdout.strip().split()
    return int(left_count), int(right_count)


def integration_sync_report(repo_root, manifest, remote=None, stack_ref_overrides=None):
    integration = manifest['integration']
    branch = integration['branch']
    remote_ref = remote_integration_ref(manifest, remote)
    local_exists = branch_exists(repo_root, branch)
    remote_exists = ref_exists(repo_root, remote_ref)
    report = {
        'branch': branch,
        'remote_ref': remote_ref,
        'local_exists': local_exists,
        'remote_exists': remote_exists,
        'relation': 'missing',
        'ahead': None,
        'behind': None,
        'local_tree': None,
        'remote_tree': None,
        'projected_tree': None,
        'remote_matches_projection': None,
        'local_matches_projection': None,
    }
    if local_exists:
        report['local_tree'] = ref_tree(repo_root, branch)
    if remote_exists:
        report['remote_tree'] = ref_tree(repo_root, remote_ref)
    if local_exists and remote_exists:
        ahead, behind = rev_left_right_count(repo_root, branch, remote_ref)
        report['ahead'] = ahead
        report['behind'] = behind
        if ahead == 0 and behind == 0:
            report['relation'] = 'aligned'
        elif ahead == 0:
            report['relation'] = 'local_behind'
        elif behind == 0:
            report['relation'] = 'local_ahead'
        else:
            report['relation'] = 'diverged'
    elif local_exists:
        report['relation'] = 'local_only'
    elif remote_exists:
        report['relation'] = 'remote_only'

    try:
        projected_tree = materialize_integration_projection(repo_root, manifest, stack_ref_overrides)
        report['projected_tree'] = projected_tree
        if report['remote_tree']:
            report['remote_matches_projection'] = report['remote_tree'] == projected_tree
        if report['local_tree']:
            report['local_matches_projection'] = report['local_tree'] == projected_tree
    except SyncwheelError as exc:
        report['projection_error'] = str(exc)
    return report


def stack_remote_ref(manifest, stack, remote=None):
    remote = remote or stack.get('publication_remote') or manifest['defaults']['publication_remote']
    return f"{remote}/{stack['branch']}"


def stack_reconcile_report(repo_root, manifest, stack, remote=None):
    branch = stack['branch']
    remote_ref = stack_remote_ref(manifest, stack, remote)
    local_exists = branch_exists(repo_root, branch)
    remote_exists = ref_exists(repo_root, remote_ref)
    report = {
        'id': stack['id'],
        'branch': branch,
        'remote_ref': remote_ref,
        'local_exists': local_exists,
        'remote_exists': remote_exists,
        'relation': 'missing',
        'ahead': None,
        'behind': None,
        'local_tree': None,
        'remote_tree': None,
        'projected_tree': None,
        'local_matches_projection': None,
        'remote_matches_projection': None,
    }
    if local_exists:
        report['local_tree'] = ref_tree(repo_root, branch)
    if remote_exists:
        report['remote_tree'] = ref_tree(repo_root, remote_ref)
    if local_exists and remote_exists:
        ahead, behind = rev_left_right_count(repo_root, branch, remote_ref)
        report['ahead'] = ahead
        report['behind'] = behind
        if ahead == 0 and behind == 0:
            report['relation'] = 'aligned'
        elif ahead == 0:
            report['relation'] = 'local_behind'
        elif behind == 0:
            report['relation'] = 'local_ahead'
        else:
            report['relation'] = 'diverged'
    elif local_exists:
        report['relation'] = 'local_only'
    elif remote_exists:
        report['relation'] = 'remote_only'

    try:
        projected_tree = materialize_stack_projection(repo_root, stack)
        report['projected_tree'] = projected_tree
        if report['local_tree']:
            report['local_matches_projection'] = report['local_tree'] == projected_tree
        if report['remote_tree']:
            report['remote_matches_projection'] = report['remote_tree'] == projected_tree
    except SyncwheelError as exc:
        report['projection_error'] = str(exc)
    return report


def reconcile_worktree_path(repo_root, branch, worktree_root):
    existing = find_worktree_for_branch(repo_root, branch)
    if existing:
        existing_path = Path(existing).resolve()
        if existing_path != Path(repo_root).resolve():
            return existing_path
    if worktree_root:
        safe = branch.replace('/', '-').replace('\\', '-')
        return resolve_worktree_root_path(repo_root, worktree_root) / safe
    return default_worktree_path(repo_root, branch)


def reconcile_actions(repo_root, manifest, validation, stack_reports, integration_report, args):
    stack_ids = set(args.stack or [stack['id'] for stack in manifest['stacks']])
    actions = []
    validation_action_types = {action['type'] for action in build_plan(repo_root, manifest, validation)}
    stack_rebuild_planned = False
    for stack in manifest['stacks']:
        if stack['id'] not in stack_ids:
            continue
        report = stack_reports[stack['id']]
        if report.get('projection_error'):
            actions.append({
                'type': 'manual_review',
                'scope': 'stack',
                'stack': stack['id'],
                'branch': stack['branch'],
                'reason': 'projection_failed',
                'detail': report['projection_error'],
            })
            continue
        align_from_remote = (
            args.rebuild != 'all'
            and report['remote_exists']
            and report.get('remote_matches_projection') is True
            and report.get('local_matches_projection') is not True
        )
        if align_from_remote:
            actions.append({
                'type': 'align_stack_to_remote',
                'stack': stack['id'],
                'branch': stack['branch'],
                'remote_ref': report['remote_ref'],
                'reason': 'remote_matches_manifest_projection',
            })
            continue
        normalize_history_from_remote = (
            args.align_local_to_remote
            and args.rebuild != 'all'
            and report['local_exists']
            and report['remote_exists']
            and report.get('local_matches_projection') is True
            and report.get('remote_matches_projection') is True
            and report['relation'] != 'aligned'
        )
        if normalize_history_from_remote:
            actions.append({
                'type': 'align_stack_to_remote',
                'stack': stack['id'],
                'branch': stack['branch'],
                'remote_ref': report['remote_ref'],
                'reason': 'local_and_remote_match_projection',
            })
            continue
        rebuild_needed = (
            args.rebuild == 'all'
            or not report['local_exists']
            or report.get('local_matches_projection') is False
            or (
                report.get('local_matches_projection') is not True
                and any(
                    item['id'] == stack['id'] and item['missing_from_branch']
                    for item in validation['details']['stacks']
                )
            )
        )
        if args.rebuild != 'none' and rebuild_needed:
            stack_rebuild_planned = True
            actions.append({
                'type': 'rebuild_stack',
                'stack': stack['id'],
                'branch': stack['branch'],
                'reason': classify_stack_reconcile(report),
            })
        push_needed = args.push and (
            rebuild_needed
            or not report['remote_exists']
            or report.get('remote_matches_projection') is False
        )
        if push_needed:
            if draft_push_refusal(manifest, stack, stack_push_remote(manifest, stack, args.remote)):
                actions.append({
                    'type': 'push_stack_refused',
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'state': 'draft',
                    'reason': 'draft_stacks_publish_only_to_the_coordination_remote',
                })
            else:
                actions.append({
                    'type': 'push_stack',
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'remote_ref': report['remote_ref'],
                })

    integration_rebuild_needed = (
        not args.skip_integration
        and not integration_report.get('projection_error')
        and (
            args.rebuild == 'all'
            or stack_rebuild_planned
            or not integration_report['local_exists']
            or integration_report.get('local_matches_projection') is False
            or (
                integration_report.get('local_matches_projection') is not True
                and (
                    'refresh_integration_for_stack' in validation_action_types
                    or 'classify_integration_commits' in validation_action_types
                )
            )
        )
    )
    if not args.skip_integration and integration_report.get('projection_error'):
        actions.append({
            'type': 'manual_review',
            'scope': 'integration',
            'branch': manifest['integration']['branch'],
            'reason': 'projection_failed',
            'detail': integration_report['projection_error'],
        })
    integration_align_from_remote = (
        not args.skip_integration
        and args.rebuild != 'all'
        and not integration_report.get('projection_error')
        and integration_report['remote_exists']
        and integration_report.get('remote_matches_projection') is True
        and integration_report.get('local_matches_projection') is not True
    )
    if integration_align_from_remote:
        actions.append({
            'type': 'align_integration_to_remote',
            'branch': manifest['integration']['branch'],
            'remote_ref': integration_report['remote_ref'],
            'reason': 'remote_matches_manifest_projection',
        })
        integration_rebuild_needed = False
    integration_normalize_history_from_remote = (
        not args.skip_integration
        and args.align_local_to_remote
        and args.rebuild != 'all'
        and not integration_report.get('projection_error')
        and integration_report['local_exists']
        and integration_report['remote_exists']
        and integration_report.get('local_matches_projection') is True
        and integration_report.get('remote_matches_projection') is True
        and integration_report['relation'] != 'aligned'
    )
    if integration_normalize_history_from_remote:
        actions.append({
            'type': 'align_integration_to_remote',
            'branch': manifest['integration']['branch'],
            'remote_ref': integration_report['remote_ref'],
            'reason': 'local_and_remote_match_projection',
        })
        integration_rebuild_needed = False
    if integration_rebuild_needed and args.rebuild != 'none':
        actions.append({
            'type': 'rebuild_integration',
            'branch': manifest['integration']['branch'],
            'reason': classify_integration_reconcile(integration_report, validation_action_types),
        })
    if args.push and not args.skip_integration and (
        integration_rebuild_needed
        or not integration_report['remote_exists']
        or integration_report.get('remote_matches_projection') is False
    ):
        actions.append({
            'type': 'push_integration',
            'branch': manifest['integration']['branch'],
            'remote_ref': integration_report['remote_ref'],
        })
    return actions


def classify_stack_reconcile(report):
    if not report['local_exists']:
        return 'local_branch_missing'
    if report.get('projection_error'):
        return 'projection_failed'
    if report.get('local_matches_projection') is False:
        return 'local_branch_differs_from_manifest_projection'
    if report['relation'] in ('local_behind', 'diverged', 'remote_only'):
        return f"remote_relation_{report['relation']}"
    return 'requested'


def classify_integration_reconcile(report, validation_action_types):
    if not report['local_exists']:
        return 'local_branch_missing'
    if 'classify_integration_commits' in validation_action_types:
        return 'integration_contains_unmapped_commits'
    if 'refresh_integration_for_stack' in validation_action_types:
        return 'integration_missing_declared_stack_commits'
    if report.get('projection_error'):
        return 'projection_failed'
    if report.get('local_matches_projection') is False:
        return 'local_branch_differs_from_manifest_projection'
    if report['relation'] in ('local_behind', 'diverged', 'remote_only'):
        return f"remote_relation_{report['relation']}"
    return 'requested'


def print_reconcile_report(output):
    print(f"repo: {output['snapshot']['repo_root']}")
    print(f"manifest: {output['manifest_path']}")
    print('\nworking tree:')
    status_lines = output['snapshot'].get('working_tree_status') or []
    if status_lines:
        for line in status_lines:
            print(f'  {line}')
    else:
        print('  clean')
    print('\nvalidation:')
    validation = output['validation']
    if validation['errors']:
        for line in validation['errors']:
            print(f'  - ERROR: {line}')
    if validation['warnings']:
        for line in validation['warnings']:
            print(f'  - WARN: {line}')
    if not validation['errors'] and not validation['warnings']:
        print('  - OK')
    print('\nstack drift:')
    for report in output['stacks']:
        parts = [f"relation={report['relation']}"]
        if report['ahead'] is not None:
            parts.append(f"ahead={report['ahead']}")
            parts.append(f"behind={report['behind']}")
        if report.get('projection_error'):
            parts.append(f"projection_error={report['projection_error']}")
        else:
            parts.append(f"local_matches_projection={report['local_matches_projection']}")
            parts.append(f"remote_matches_projection={report['remote_matches_projection']}")
        print(f"  - {report['id']}: " + ', '.join(parts))
    integration = output['integration']
    print('\nintegration drift:')
    parts = [f"relation={integration['relation']}"]
    if integration['ahead'] is not None:
        parts.append(f"ahead={integration['ahead']}")
        parts.append(f"behind={integration['behind']}")
    if integration.get('projection_error'):
        parts.append(f"projection_error={integration['projection_error']}")
    else:
        parts.append(f"local_matches_projection={integration['local_matches_projection']}")
        parts.append(f"remote_matches_projection={integration['remote_matches_projection']}")
    print('  - ' + ', '.join(parts))
    print_integration_commit_diagnostics(
        output.get('diagnostics', {}).get('unmapped_integration_commits') or []
    )
    print('\nreconcile plan:')
    if output['actions']:
        for action in output['actions']:
            print(f'  - {format_reconcile_action(action)}')
    else:
        print('  - no actions needed')
    if not output['applied']:
        print('\nmode: dry-run; pass --apply to execute branch rebuilds')


def format_reconcile_action(action):
    line = action['type']
    if 'stack' in action:
        line += f" stack={action['stack']}"
    if 'branch' in action:
        line += f" branch={action['branch']}"
    if 'short' in action:
        line += f" commit={action['short']}"
    if 'reason' in action:
        line += f" reason={action['reason']}"
    if action['type'] in ('align_stack_to_remote', 'align_integration_to_remote'):
        line += ' detail=remote already has the manifest projection; aligning local history'
    elif action['type'] in ('push_stack', 'push_integration'):
        line += ' detail=local projection needs publishing'
    elif action['type'] == 'push_stack_refused':
        line += f" detail=stack state={action['state']} cannot be published"
    elif action['type'] == 'manual_review':
        line += ' detail=manual review required before applying'
    elif action['type'] == 'resume_add_commit':
        line += ' detail=register integration commit on detected owning stack'
    elif action['type'] == 'resume_restore_stack':
        line += ' detail=restore a previously known stack from the ledger before registering commits'
    elif action['type'] == 'resume_manual_review':
        line += ' detail=resume mode could not classify this integration commit safely'
    elif action['type'] == 'rebuild_integration' and action.get('reason') == 'integration_contains_unmapped_commits':
        line += ' detail=integration contains unassigned commits'
    return line


def command_ledger_show(args):
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    state = load_ledger_state(repo_root, manifest_path)
    if args.json:
        print(json.dumps(state, indent=2))
        return 0
    print(f"last_seq: {state['last_seq']}")
    print(f"event_count: {state['event_count']}")
    manifest = state.get('manifest') or {}
    if manifest:
        print(f"manifest_hash: {manifest.get('manifest_hash')}")
        print(f"manifest_reason: {manifest.get('reason')}")
        print(f"active_stacks: {', '.join(manifest.get('active_stacks') or []) or 'none'}")
    integration = state.get('integration') or {}
    if integration.get('branch'):
        print(f"integration_branch: {integration.get('branch')}")
        print(f"integration_last_tip: {integration.get('last_tip') or 'unknown'}")
    print('stacks:')
    if state.get('stacks'):
        for stack_id in sorted(state['stacks']):
            stack = state['stacks'][stack_id]
            active = 'active' if stack.get('active_in_manifest') else 'historical'
            print(f"  - {stack_id}: branch={stack.get('branch')} state={active}")
    else:
        print('  - none')
    print('recent_events:')
    if state.get('recent_events'):
        for event in state['recent_events'][-10:]:
            print(f"  - {event['seq']} {event['type']}")
    else:
        print('  - none')
    return 0


def command_reconcile(args):
    repo_root = resolve_repo_root(args.repo)
    if getattr(args, 'accept_merge', False) and args.command != 'publish':
        raise SyncwheelError('--accept-merge is only available through publish --accept-merge')
    if args.fetch:
        git(repo_root, 'fetch', '--all', '--prune', '--quiet', check=False)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    if getattr(args, 'accept_merge', False):
        manifest = apply_pending_coordination_merge(repo_root, manifest, manifest_path)
    resume_actions = []
    resume_manifest_changed = False
    if args.mode == 'resume':
        original_manifest = json.loads(json.dumps(manifest))
        initial_validation = validate_manifest(repo_root, manifest)
        initial_diagnostics = integration_commit_diagnostics(repo_root, manifest, initial_validation, manifest_path)
        selected_stack_ids = args.stack or None
        resume_actions, effective_manifest = plan_resume_mutations(
            repo_root,
            manifest,
            initial_diagnostics,
            selected_stack_ids=selected_stack_ids,
        )
        manifest = effective_manifest
        resume_manifest_changed = manifest != original_manifest
    if args.stack:
        known = stack_map(manifest)
        for stack_id in args.stack:
            if stack_id not in known:
                raise SyncwheelError(f'unknown stack: {stack_id}')
    validation = validate_manifest(repo_root, manifest)
    worktree_root = effective_worktree_root(manifest, args.worktree_root)
    stack_ids = set(args.stack or [stack['id'] for stack in manifest['stacks']])
    stack_reports = {
        stack['id']: stack_reconcile_report(repo_root, manifest, stack, args.remote)
        for stack in manifest['stacks']
        if stack['id'] in stack_ids
    }
    stack_ref_overrides = {
        stack_id: report['remote_ref']
        for stack_id, report in stack_reports.items()
        if report['remote_exists'] and report.get('remote_matches_projection') is True
    }
    integration_report = integration_sync_report(repo_root, manifest, args.remote, stack_ref_overrides)
    actions = reconcile_actions(repo_root, manifest, validation, stack_reports, integration_report, args)
    diagnostics = integration_commit_diagnostics(repo_root, manifest, validation, manifest_path)
    output = {
        'snapshot': collect_repo_snapshot(repo_root, manifest),
        'manifest_path': str(manifest_path),
        'validation': validation,
        'stacks': list(stack_reports.values()),
        'integration': integration_report,
        'actions': [*resume_actions, *actions],
        'diagnostics': {
            'unmapped_integration_commits': diagnostics,
        },
        'applied': args.apply,
        'push': args.push,
        'mode': args.mode,
    }
    if args.json and not args.apply:
        print(json.dumps(output, indent=2))
        return 2 if any(
            action['type'] == 'push_stack_refused' for action in output['actions']
        ) else (1 if validation['errors'] else 0)
    print_reconcile_report(output)
    refused_pushes = [
        action for action in output['actions']
        if action['type'] == 'push_stack_refused'
    ]
    if refused_pushes:
        stack_ids = ', '.join(action['stack'] for action in refused_pushes)
        raise SyncwheelError(
            'reconcile cannot push stack(s) in state draft to this remote: '
            + stack_ids
            + '; a draft publishes its source ref only to the coordination remote'
        )
    if validation['errors']:
        return 1
    if not args.apply:
        return 0
    manual_actions = [
        action for action in output['actions']
        if action['type'] in ('manual_review', 'resume_manual_review')
    ]
    if manual_actions:
        raise SyncwheelError('reconcile requires manual review before --apply can continue')

    if is_external_manifest_path(repo_root, manifest_path):
        ensure_syncwheel_worktree_root_excluded(repo_root, worktree_root)
    else:
        ensure_syncwheel_metadata_excluded(repo_root, manifest.get('syncwheel_tracking'), worktree_root)

    push_args = push_args_with_options(args)
    coordinated_push = args.push and coordination_is_active(manifest)
    if coordinated_push:
        coordinated_push_remote(args, coordination_config(manifest))
    coordinated_refs = {}
    coordinated_events = []
    for action in actions:
        if action['type'] == 'rebuild_stack':
            stack = require_stack(manifest, action['stack'])
            worktree = reconcile_worktree_path(repo_root, stack['branch'], worktree_root)
            ensure_non_in_place_target_clean(repo_root, stack['branch'], worktree)
            mode, worktree = resolve_replay_mode(repo_root, (worktree, False))
            result = execute_replay(
                repo_root,
                replay_plan(repo_root, manifest, replay_target(stack=stack, worktree=worktree), mode),
                True,
            )
            require_replay_success(result)
            append_ledger_event(
                repo_root,
                'stack_rebuilt',
                {
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'base': stack['base'],
                    'integration_branch': stack.get('integration_branch'),
                    'before_tip': result['before_tip'],
                    'after_tip': result['after_tip'],
                },
                manifest_path,
            )
            if args.update_manifest:
                stack['commits'] = rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
                save_manifest_with_ledger(
                    repo_root,
                    manifest_path,
                    manifest,
                    'reconcile_update_manifest',
                    {'stack': stack['id'], 'branch': stack['branch']},
                )
                print(f"{stack['id']}: manifest updated from rebuilt branch")
        elif action['type'] == 'align_stack_to_remote':
            stack = require_stack(manifest, action['stack'])
            before_tip = ref_tip(repo_root, stack['branch'])
            worktree = reconcile_worktree_path(repo_root, stack['branch'], worktree_root)
            ensure_non_in_place_target_clean(repo_root, stack['branch'], worktree)
            commands = materialize_remote_align_commands(
                repo_root,
                stack['branch'],
                action['remote_ref'],
                worktree,
            )
            run_command_list(commands, repo_root, True)
            append_ledger_event(
                repo_root,
                'stack_rebuilt',
                {
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'base': stack['base'],
                    'integration_branch': stack.get('integration_branch'),
                    'before_tip': before_tip,
                    'after_tip': ref_tip(repo_root, stack['branch']),
                },
                manifest_path,
            )
        elif action['type'] == 'push_stack':
            stack = require_stack(manifest, action['stack'])
            refusal = draft_push_refusal(manifest, stack, stack_push_remote(manifest, stack, args.remote))
            if refusal:
                raise SyncwheelError(refusal)
            if coordinated_push:
                ref = f"refs/heads/{stack['branch']}"
                coordinated_refs[ref] = ref_tip(repo_root, stack['branch'])
                coordinated_events.append({
                    'type': 'stack_pushed',
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'tip': coordinated_refs[ref],
                })
                continue
            remote = stack_push_remote(manifest, stack, args.remote)
            command = ['git', 'push', *push_args, remote, stack['branch']]
            run(command, cwd=repo_root)
            print(quoted(command))
            append_ledger_event(
                repo_root,
                'stack_pushed',
                {
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'remote': remote,
                    'tip': ref_tip(repo_root, stack['branch']),
                },
                manifest_path,
            )
        elif action['type'] == 'rebuild_integration':
            integration = manifest['integration']
            use_primary_checkout = get_current_branch(repo_root) == integration['branch']
            if args.in_place_integration or use_primary_checkout:
                if get_current_branch(repo_root) != integration['branch']:
                    raise SyncwheelError(
                        f"in-place materialization requires current branch {integration['branch']!r}; "
                        f"current branch is {get_current_branch(repo_root)!r}"
                    )
                ensure_clean_worktree(repo_root, allowed_status_prefixes=['?? .syncwheel/'])
                worktree = None
                in_place = True
            else:
                worktree = reconcile_worktree_path(repo_root, integration['branch'], worktree_root)
                ensure_non_in_place_target_clean(repo_root, integration['branch'], worktree)
                in_place = False
            mode, worktree = resolve_replay_mode(repo_root, (worktree, in_place))
            result = execute_replay(
                repo_root,
                replay_plan(
                    repo_root,
                    manifest,
                    replay_target(integration=integration, worktree=worktree),
                    mode,
                ),
                True,
            )
            require_replay_success(result)
            append_ledger_event(
                repo_root,
                'integration_rebuilt',
                {
                    'branch': integration['branch'],
                    'before_tip': result['before_tip'],
                    'after_tip': result['after_tip'],
                    'stacks': list(integration.get('stacks', [])),
                },
                manifest_path,
            )
        elif action['type'] == 'align_integration_to_remote':
            integration = manifest['integration']
            before_tip = ref_tip(repo_root, integration['branch'])
            use_primary_checkout = get_current_branch(repo_root) == integration['branch']
            if use_primary_checkout:
                ensure_clean_worktree(repo_root, allowed_status_prefixes=['?? .syncwheel/'])
                worktree = None
            else:
                worktree = reconcile_worktree_path(repo_root, integration['branch'], worktree_root)
                ensure_non_in_place_target_clean(repo_root, integration['branch'], worktree)
            commands = materialize_remote_align_commands(
                repo_root,
                integration['branch'],
                action['remote_ref'],
                worktree,
            )
            run_command_list(commands, repo_root, True)
            append_ledger_event(
                repo_root,
                'integration_aligned_remote',
                {
                    'branch': integration['branch'],
                    'remote_ref': action['remote_ref'],
                    'before_tip': before_tip,
                    'after_tip': ref_tip(repo_root, integration['branch']),
                },
                manifest_path,
            )
        elif action['type'] == 'push_integration':
            if coordinated_push:
                branch = manifest['integration']['branch']
                ref = f'refs/heads/{branch}'
                coordinated_refs[ref] = ref_tip(repo_root, branch)
                coordinated_events.append({
                    'type': 'integration_pushed',
                    'branch': branch,
                    'tip': coordinated_refs[ref],
                })
                continue
            remote = args.remote or manifest['defaults']['publication_remote']
            command = ['git', 'push', *push_args, remote, manifest['integration']['branch']]
            run(command, cwd=repo_root)
            print(quoted(command))
            append_ledger_event(
                repo_root,
                'integration_pushed',
                {
                    'branch': manifest['integration']['branch'],
                    'remote': remote,
                    'tip': ref_tip(repo_root, manifest['integration']['branch']),
                },
                manifest_path,
            )
    if coordinated_push:
        full_scope = not args.stack and not args.skip_integration
        if full_scope and not local_manifest_projection_is_convergent(repo_root, manifest):
            raise SyncwheelError(
                'full coordinated publish requires every managed local ref to match the manifest projection'
            )
        coordination_result = coordinated_publish(
            repo_root,
            manifest,
            manifest_path,
            coordinated_refs,
            'full' if full_scope else 'partial',
            'convergent' if full_scope else 'partial',
        )
        config = coordination_config(manifest)
        for event in coordinated_events:
            payload = {
                **event,
                'remote': config['remote'],
                'coordination_state': coordination_result.get('state_tip'),
                'coordination_status': coordination_result['status'],
            }
            append_ledger_event(repo_root, event['type'], payload, manifest_path)
        if not coordinated_events:
            append_ledger_event(
                repo_root,
                'coordination_published',
                {
                    'remote': config['remote'],
                    'coordination_state': coordination_result.get('state_tip'),
                    'coordination_status': coordination_result['status'],
                    'scope': 'full' if full_scope else 'partial',
                },
                manifest_path,
            )
    if args.apply and resume_manifest_changed:
        save_manifest_with_ledger(repo_root, manifest_path, manifest, 'resume_manifest_update')
    if args.apply and getattr(args, 'auto_gc', False) and coordination_is_active(manifest):
        gc_plan = run_coordination_gc(repo_root, manifest, apply=True, fetch=True)
        if gc_plan.get('applied_candidates'):
            print(f"automatic gc: processed {len(gc_plan['applied_candidates'])} eligible local artifact(s)")
    return 0


def command_sync(args):
    args.apply = True
    args.push = False
    args.auto_gc = True
    return command_reconcile(args)


def command_publish(args):
    args.apply = True
    args.push = True
    args.auto_gc = True
    return command_reconcile(args)


def command_resume(args):
    args.mode = 'resume'
    return command_reconcile(args)


def command_int_sync_status(args):
    repo_root = resolve_repo_root(args.repo)
    if args.fetch:
        git(repo_root, 'fetch', '--all', '--prune', '--quiet', check=False)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    validation = validate_manifest(repo_root, manifest)
    report = integration_sync_report(repo_root, manifest, args.remote)
    output = {
        'manifest_path': str(manifest_path),
        'validation': validation,
        'sync': report,
    }
    if args.json:
        print(json.dumps(output, indent=2))
        return 1 if validation['errors'] else 0
    print(f"branch: {report['branch']}")
    print(f"remote_ref: {report['remote_ref']}")
    print(f"relation: {report['relation']}")
    if report['ahead'] is not None:
        print(f"ahead: {report['ahead']}")
        print(f"behind: {report['behind']}")
    if report.get('projection_error'):
        print(f"projection_error: {report['projection_error']}")
    else:
        print(f"remote_matches_projection: {report['remote_matches_projection']}")
        print(f"local_matches_projection: {report['local_matches_projection']}")
    return 1 if validation['errors'] else 0


def command_int_align_remote(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    integration = manifest['integration']
    if args.fetch:
        git(repo_root, 'fetch', '--all', '--prune', '--quiet', check=False)
    ensure_in_place_target(repo_root, integration['branch'])
    report = integration_sync_report(repo_root, manifest, args.remote)
    if not report['remote_exists']:
        raise SyncwheelError(f"remote integration ref does not exist: {report['remote_ref']}")
    if report.get('projection_error'):
        raise SyncwheelError(f"cannot project integration from manifest: {report['projection_error']}")
    if not args.force and not report['remote_matches_projection']:
        raise SyncwheelError(
            f"remote integration ref {report['remote_ref']} does not match manifest projection; "
            'use int rebuild or pass --force after manual review'
        )
    if report['relation'] == 'aligned':
        print(f"{integration['branch']}: already aligned with {report['remote_ref']}")
        return 0
    timestamp = syncwheel_timestamp()
    before_tip = ref_tip(repo_root, integration['branch'])
    commands = []
    backup = backup_branch_command(repo_root, integration['branch'], timestamp)
    if backup:
        commands.append(backup)
    commands.append(['git', 'reset', '--hard', report['remote_ref']])
    run_command_list(commands, repo_root, not args.dry_run)
    if not args.dry_run:
        append_ledger_event(
            repo_root,
            'integration_aligned_remote',
            {
                'branch': integration['branch'],
                'remote_ref': report['remote_ref'],
                'before_tip': before_tip,
                'after_tip': ref_tip(repo_root, integration['branch']),
            },
            manifest_path,
        )
    return 0


def command_int_rebuild(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    integration = manifest['integration']
    mode, worktree = resolve_replay_mode(
        repo_root,
        resolve_int_rebuild_location(repo_root, manifest, args),
        requested_replay_mode(args),
    )
    if not args.dry_run and mode == 'in-place':
        ensure_in_place_target(repo_root, manifest['integration']['branch'])
    if not args.dry_run and mode in ('ephemeral', 'plumbing'):
        ensure_non_in_place_target_clean(
            repo_root,
            manifest['integration']['branch'],
            find_worktree_for_branch(repo_root, manifest['integration']['branch']),
        )
    if not args.dry_run and mode == 'desk':
        ensure_non_in_place_target_clean(repo_root, manifest['integration']['branch'], worktree)
    result = execute_replay(
        repo_root,
        replay_plan(
            repo_root,
            manifest,
            replay_target(integration=integration, worktree=worktree),
            mode,
        ),
        not args.dry_run,
    )
    require_replay_success(result)
    if not args.dry_run:
        append_ledger_event(
            repo_root,
            'integration_rebuilt',
            {
                'branch': manifest['integration']['branch'],
                'before_tip': result['before_tip'],
                'after_tip': result['after_tip'],
                'stacks': list(manifest['integration'].get('stacks', [])),
            },
            manifest_path,
        )
    return 0


def command_int_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    integration = manifest['integration']
    if coordination_is_active(manifest):
        config = coordination_config(manifest)
        coordinated_push_remote(args, config)
        result = coordinated_publish(
            repo_root,
            manifest,
            manifest_path,
            {f"refs/heads/{integration['branch']}": ref_tip(repo_root, integration['branch'])},
            'integration',
            'partial',
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            append_ledger_event(
                repo_root,
                'integration_pushed',
                {
                    'branch': integration['branch'],
                    'remote': config['remote'],
                    'tip': ref_tip(repo_root, integration['branch']),
                    'coordination_state': result.get('state_tip'),
                    'coordination_status': result['status'],
                },
                manifest_path,
            )
        return 0
    remote = args.remote or manifest['defaults']['publication_remote']
    push_args = push_args_with_options(args)
    command = ['git', 'push', *push_args, remote, integration['branch']]
    if args.dry_run:
        print(quoted(command))
        return 0
    run(command, cwd=repo_root)
    print(quoted(command))
    append_ledger_event(
        repo_root,
        'integration_pushed',
        {
            'branch': integration['branch'],
            'remote': remote,
            'tip': ref_tip(repo_root, integration['branch']),
        },
        manifest_path,
    )
    return 0


def command_int_git(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    branch = manifest['integration']['branch']
    worktree = resolve_git_worktree(repo_root, branch, args.worktree, args.auto_worktree)
    git_args = passthrough_args(args.git_args)
    if not git_args:
        raise SyncwheelError('int git requires git arguments after --')
    result = run(['git', *git_args], cwd=worktree, check=False)
    if result.stdout:
        print(result.stdout, end='')
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    return result.returncode


def manifest_stack_summary(stack):
    return {
        'id': stack['id'],
        'branch': stack['branch'],
        'base': stack['base'],
        'commits': stack['commits'],
        'integration_branch': stack.get('integration_branch'),
    }


def load_other_manifest(repo_root, args):
    if args.other_personal and args.other_manifest:
        raise SyncwheelError('use either --other-personal or --other-manifest, not both')
    if args.other_personal:
        path = personal_manifest_path(repo_root, args.other_personal)
    elif args.other_manifest:
        path = Path(args.other_manifest).expanduser()
    else:
        raise SyncwheelError('manifest compare requires --other-manifest or --other-personal')
    manifest, path = load_manifest(repo_root, path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {path}')
    return manifest, path


def compare_manifests(left, right):
    left_stacks = stack_map(left)
    right_stacks = stack_map(right)
    left_ids = set(left_stacks)
    right_ids = set(right_stacks)
    shared = []
    divergent = []
    for stack_id in sorted(left_ids & right_ids):
        left_stack = manifest_stack_summary(left_stacks[stack_id])
        right_stack = manifest_stack_summary(right_stacks[stack_id])
        same = (
            left_stack['branch'] == right_stack['branch']
            and left_stack['base'] == right_stack['base']
            and left_stack['commits'] == right_stack['commits']
        )
        item = {
            'id': stack_id,
            'same': same,
            'left': left_stack,
            'right': right_stack,
        }
        shared.append(item)
        if not same:
            divergent.append(item)
    return {
        'left_integration': left['integration'],
        'right_integration': right['integration'],
        'shared': shared,
        'divergent_shared': divergent,
        'left_only': sorted(left_ids - right_ids),
        'right_only': sorted(right_ids - left_ids),
    }


def command_manifest_compare(args):
    repo_root = resolve_repo_root(args.repo)
    left, left_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    right, right_path = load_other_manifest(repo_root, args)
    comparison = compare_manifests(left, right)
    output = {
        'left_manifest': str(left_path),
        'right_manifest': str(right_path),
        **comparison,
    }
    if args.json:
        print(json.dumps(output, indent=2))
        return 0
    print(f"left_manifest: {left_path}")
    print(f"right_manifest: {right_path}")
    print(f"left_integration: {left['integration']['branch']}")
    print(f"right_integration: {right['integration']['branch']}")
    print(f"shared_stacks: {len(comparison['shared'])}")
    print(f"divergent_shared_stacks: {len(comparison['divergent_shared'])}")
    if comparison['left_only']:
        print('left_only: ' + ', '.join(comparison['left_only']))
    if comparison['right_only']:
        print('right_only: ' + ', '.join(comparison['right_only']))
    for item in comparison['divergent_shared']:
        print(f"divergent: {item['id']}")
    return 0


def command_manifest_require_integration(args):
    """Migrate a manifest so every declared stack participates in integration."""
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    integration_stacks = manifest['integration']['stacks']
    excluded_stack_ids = [
        stack['id'] for stack in manifest['stacks']
        if stack['id'] not in integration_stacks
    ]
    output = {
        'manifest_path': str(manifest_path),
        'integration_membership': INTEGRATION_MEMBERSHIP_REQUIRED,
        'add_to_integration': excluded_stack_ids,
        'apply': bool(args.apply),
    }
    if args.apply:
        manifest['defaults']['integration_membership'] = INTEGRATION_MEMBERSHIP_REQUIRED
        manifest['integration']['stacks'].extend(excluded_stack_ids)
        save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'manifest_require_integration',
            {'added_stacks': excluded_stack_ids},
        )
    if args.json:
        print(json.dumps(output, indent=2))
    else:
        print('add_to_integration: ' + (', '.join(excluded_stack_ids) or 'none'))
        print('integration_membership: required')
        print('applied: ' + ('yes' if args.apply else 'no'))
    return 0


def repo_relative_path(repo_root, path):
    try:
        return Path(path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return None


def git_path_is_tracked(repo_root, path):
    relative = repo_relative_path(repo_root, path)
    if not relative:
        return False
    return git(repo_root, 'ls-files', '--error-unmatch', '--', relative, check=False).returncode == 0


def gitignore_manual_syncwheel_conflicts(repo_root):
    path = repo_root / '.gitignore'
    text = read_text_if_exists(path)
    if not text:
        return []
    scrubbed, _ = replace_managed_block(
        text,
        SYNCWHEEL_GITIGNORE_MARKER,
        SYNCWHEEL_GITIGNORE_END_MARKER,
        [],
        all_syncwheel_managed_patterns(DEFAULT_SYNCWHEEL_WORKTREE_ROOT),
    )
    conflicts = []
    for line in scrubbed.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped == '.syncwheel/' or stripped == '.syncwheel':
            conflicts.append(stripped)
    return conflicts


def managed_block_exists(path, marker):
    if not path:
        return False
    return any(line.strip() == marker for line in read_text_if_exists(path).splitlines())


def syncwheel_tracking_report(repo_root, manifest_path):
    manifest, manifest_path = load_manifest(repo_root, manifest_path)
    manifest_present = manifest is not None
    tracking = manifest.get('syncwheel_tracking') if manifest else None
    worktree_root = syncwheel_worktree_root(manifest)
    effective_root = resolve_worktree_root_path(repo_root, worktree_root)
    info_exclude = git_info_exclude_path(repo_root)
    gitignore = repo_root / '.gitignore'
    manifest_relative = repo_relative_path(repo_root, manifest_path)
    manifest_tracked = git_path_is_tracked(repo_root, manifest_path)
    warnings = []
    actions = []

    if not manifest_present:
        actions.append('create a manifest with syncwheel init, then set syncwheel_tracking')
    elif tracking is None:
        warnings.append('syncwheel_tracking is not set; choose git-tracked or local-only before branch/push/PR work')
        actions.append('run syncwheel repo tracking set git-tracked|local-only --apply')
    elif tracking == SYNCWHEEL_TRACKING_GIT_TRACKED:
        conflicts = gitignore_manual_syncwheel_conflicts(repo_root)
        if conflicts:
            warnings.append('.gitignore contains manual .syncwheel ignore entries outside the Syncwheel managed block')
        if not manifest_tracked and manifest_relative:
            actions.append('git add -f .syncwheel/manifest.json')
        if not managed_block_exists(gitignore, SYNCWHEEL_GITIGNORE_MARKER):
            actions.append('add Syncwheel managed .gitignore block')
        if managed_block_exists(info_exclude, SYNCWHEEL_LOCAL_EXCLUDE_MARKER):
            actions.append('remove Syncwheel managed .git/info/exclude block')
    elif tracking == SYNCWHEEL_TRACKING_LOCAL_ONLY:
        if manifest_tracked and manifest_relative:
            actions.append('git rm --cached .syncwheel/manifest.json')
        if not managed_block_exists(info_exclude, SYNCWHEEL_LOCAL_EXCLUDE_MARKER):
            actions.append('add Syncwheel managed .git/info/exclude block')
        if managed_block_exists(gitignore, SYNCWHEEL_GITIGNORE_MARKER):
            actions.append('remove Syncwheel managed .gitignore block')

    return {
        'repo_root': str(repo_root),
        'manifest_path': str(manifest_path),
        'manifest_present': manifest_present,
        'manifest_in_repo': manifest_relative is not None,
        'manifest_tracked': manifest_tracked,
        'syncwheel_tracking': tracking,
        'syncwheel_tracking_present': tracking is not None,
        'syncwheel_worktree_root': worktree_root,
        'effective_worktree_root': str(effective_root),
        'gitignore_path': str(gitignore),
        'gitignore_managed': managed_block_exists(gitignore, SYNCWHEEL_GITIGNORE_MARKER),
        'info_exclude_path': str(info_exclude) if info_exclude else None,
        'info_exclude_managed': managed_block_exists(info_exclude, SYNCWHEEL_LOCAL_EXCLUDE_MARKER),
        'warnings': warnings,
        'actions': actions,
    }


def print_syncwheel_tracking_report(report):
    print(f"repo: {report['repo_root']}")
    print(f"manifest: {report['manifest_path'] if report['manifest_present'] else 'missing'}")
    print(f"syncwheel_tracking: {report['syncwheel_tracking'] or 'missing'}")
    print(f"syncwheel_worktree_root: {report['syncwheel_worktree_root']}")
    print(f"effective_worktree_root: {report['effective_worktree_root']}")
    print(f"manifest_tracked: {'yes' if report['manifest_tracked'] else 'no'}")
    if report['warnings']:
        print('warnings:')
        for warning in report['warnings']:
            print(f'  - {warning}')
    if report['actions']:
        print('actions:')
        for action in report['actions']:
            print(f'  - {action}')
    else:
        print('actions: none')


def command_repo_tracking_status(args):
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    report = syncwheel_tracking_report(repo_root, manifest_path)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print_syncwheel_tracking_report(report)
    return 0


def ensure_manifest_in_repo(repo_root, manifest_path):
    relative = repo_relative_path(repo_root, manifest_path)
    if not relative:
        raise SyncwheelError('syncwheel_tracking setup requires a manifest path inside the target repo')
    return relative


def ensure_manifests_readme(repo_root):
    path = repo_root / '.syncwheel' / 'manifests' / 'README.md'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '# Syncwheel Manifests\n\n'
            'Personal `*.local.json` manifests are per-clone overlays and should not be committed.\n'
        )
    return path


def git_add_paths(repo_root, paths, force_paths=None):
    normal = []
    force = []
    force_paths = {Path(path).resolve() for path in (force_paths or [])}
    for path in paths:
        resolved = Path(path).resolve()
        relative = repo_relative_path(repo_root, resolved)
        if not relative or not resolved.exists():
            continue
        if resolved in force_paths:
            force.append(relative)
        else:
            normal.append(relative)
    if normal:
        git(repo_root, 'add', '--', *normal)
    if force:
        git(repo_root, 'add', '-f', '--', *force)


def git_rm_cached_paths(repo_root, paths):
    relatives = []
    for path in paths:
        relative = repo_relative_path(repo_root, path)
        if relative:
            relatives.append(relative)
    if relatives:
        git(repo_root, 'rm', '--cached', '--ignore-unmatch', '--', *relatives)


def command_repo_tracking_set(args):
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    manifest, manifest_path = load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    ensure_manifest_in_repo(repo_root, manifest_path)
    tracking = normalize_syncwheel_tracking(args.tracking)
    worktree_root = normalize_syncwheel_worktree_root(args.worktree_root or syncwheel_worktree_root(manifest))
    manifest['syncwheel_tracking'] = tracking
    manifest['syncwheel_worktree_root'] = worktree_root

    warnings = []
    if tracking == SYNCWHEEL_TRACKING_GIT_TRACKED:
        conflicts = gitignore_manual_syncwheel_conflicts(repo_root)
        if conflicts:
            warnings.append(
                '.gitignore contains manual .syncwheel ignore entries outside the Syncwheel managed block; '
                'remove or audit them before applying git-tracked setup'
            )
    if warnings and args.apply:
        raise SyncwheelError('; '.join(warnings))

    if not args.apply:
        report = syncwheel_tracking_report(repo_root, manifest_path)
        report['syncwheel_tracking'] = tracking
        report['syncwheel_tracking_present'] = True
        report['syncwheel_worktree_root'] = worktree_root
        report['effective_worktree_root'] = str(resolve_worktree_root_path(repo_root, worktree_root))
        report['warnings'].extend(warnings)
        report['actions'].append(f'apply syncwheel_tracking={tracking}')
        print_syncwheel_tracking_report(report)
        print('dry_run: pass --apply to write this setup')
        return 0

    readme_path = repo_root / '.syncwheel' / 'manifests' / 'README.md'
    if tracking == SYNCWHEEL_TRACKING_GIT_TRACKED:
        readme_path = ensure_manifests_readme(repo_root)
        save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'repo_tracking_set',
            {'syncwheel_tracking': tracking, 'syncwheel_worktree_root': worktree_root},
        )
        git_add_paths(
            repo_root,
            [manifest_path, repo_root / '.gitignore', readme_path],
            force_paths=[manifest_path],
        )
    else:
        save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'repo_tracking_set',
            {'syncwheel_tracking': tracking, 'syncwheel_worktree_root': worktree_root},
        )
        write_managed_block(
            repo_root / '.gitignore',
            SYNCWHEEL_GITIGNORE_MARKER,
            SYNCWHEEL_GITIGNORE_END_MARKER,
            [],
            all_syncwheel_managed_patterns(worktree_root),
        )
        git_add_paths(repo_root, [repo_root / '.gitignore'])
        git_rm_cached_paths(repo_root, [manifest_path, readme_path])

    report = syncwheel_tracking_report(repo_root, manifest_path)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_syncwheel_tracking_report(report)
    return 0


def add_rebuild_args(parser):
    parser.add_argument('-w', '--worktree')
    parser.add_argument('-i', '--in-place', action='store_true')
    parser.add_argument(
        '--replay-mode',
        choices=REPLAY_MODE_CHOICES,
        default='auto',
        help='replay execution mode (plumbing requires Git 2.38+; auto keeps the desk default)',
    )
    parser.add_argument('-n', '--dry-run', action='store_true')


def add_push_args(parser):
    parser.add_argument('-R', '--remote')
    parser.add_argument('-n', '--dry-run', action='store_true')
    parser.add_argument(
        '-l',
        '--force-with-lease',
        action='store_true',
        help='pass --force-with-lease to git push',
    )


def add_git_args(parser):
    parser.add_argument('-w', '--worktree', help='create/use this worktree path when the branch has no worktree')
    parser.add_argument('-a', '--auto-worktree', action='store_true', help='create the default worktree when missing')
    return parser


def add_reconcile_args(parser, include_apply_push=True, include_push_options=True):
    parser.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    parser.add_argument('-j', '--json', action='store_true')
    if include_apply_push:
        parser.add_argument('-a', '--apply', action='store_true', help='execute the reported rebuild/push plan')
        parser.add_argument('-P', '--push', action='store_true', help='push rebuilt or drifted managed branches')
    if include_push_options:
        parser.add_argument(
            '-l',
            '--force-with-lease',
            action='store_true',
            default=True,
            help='pass --force-with-lease to reconcile-managed git pushes (default)',
        )
        parser.add_argument(
            '-L',
            '--no-force-with-lease',
            dest='force_with_lease',
            action='store_false',
            help='use normal git push for reconcile-managed pushes',
        )
    parser.add_argument('-R', '--remote', help='remote override for managed branch comparisons and publication')
    parser.add_argument(
        '--accept-merge',
        action='store_true',
        help='explicitly apply a previously reported disjoint-stack coordination merge before publishing',
    )
    parser.add_argument(
        '-m',
        '--mode',
        choices=sorted(RECONCILE_MODES),
        default='standard',
        help='reconcile mode: standard or resume',
    )
    parser.add_argument('-s', '--stack', action='append', help='limit reconciliation to one stack; may be repeated')
    parser.add_argument('-I', '--skip-integration', action='store_true')
    parser.add_argument(
        '-A',
        '--align-local-to-remote',
        dest='align_local_to_remote',
        action='store_true',
        default=True,
        help='align local branch tips to remote refs when both match the manifest projection (default)',
    )
    parser.add_argument(
        '--no-align-local-to-remote',
        dest='align_local_to_remote',
        action='store_false',
        help='do not normalize local history to remote even when both match the manifest projection',
    )
    parser.add_argument(
        '-b',
        '--rebuild',
        choices=['needed', 'all', 'none'],
        default='needed',
        help='which managed branches to rebuild before optional push',
    )
    parser.add_argument(
        '-W',
        '--worktree-root',
        help='directory where reconcile creates branch worktrees when no worktree already exists',
    )
    parser.add_argument(
        '-i',
        '--in-place-integration',
        action='store_true',
        help='allow integration rebuild in the current clean integration checkout',
    )
    parser.add_argument(
        '-U',
        '--no-update-manifest',
        dest='update_manifest',
        action='store_false',
        help='do not refresh stack commit SHAs after stack rebuilds',
    )


def build_parser():
    parser = argparse.ArgumentParser(description='Deterministic syncwheel helper for fork/upstream/integration repos.')
    parser.add_argument('--version', action='version', version=f'syncwheel {VERSION}')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('-r', '--repo', help='target repo path or registered alias')
    common.add_argument('-M', '--manifest', help='path to a syncwheel manifest JSON file')
    common.add_argument('-p', '--personal', help='use .syncwheel/manifests/<name>.local.json')
    sub = parser.add_subparsers(dest='command', required=True)

    repo_p = sub.add_parser('repo', aliases=['r'], help='manage repo aliases')
    repo_sub = repo_p.add_subparsers(dest='repo_command', required=True)

    repo_add_p = repo_sub.add_parser('add', help='add/update one repo alias')
    repo_add_p.add_argument('alias')
    repo_add_p.add_argument('path')
    repo_add_p.add_argument('-M', '--manifest', help='optional default manifest path for this alias')
    repo_add_p.set_defaults(func=command_repo_add)

    repo_manifest_p = repo_sub.add_parser('set-manifest', help='set/clear default manifest path for one alias')
    repo_manifest_p.add_argument('alias')
    repo_manifest_p.add_argument('manifest', nargs='?', help='manifest path; omit with --clear to remove')
    repo_manifest_p.add_argument('-c', '--clear', action='store_true')
    repo_manifest_p.set_defaults(func=command_repo_set_manifest)

    repo_rm_p = repo_sub.add_parser('rm', help='remove one repo alias')
    repo_rm_p.add_argument('alias')
    repo_rm_p.set_defaults(func=command_repo_rm)

    repo_ls_p = repo_sub.add_parser('ls', help='list repo aliases')
    repo_ls_p.add_argument('-j', '--json', action='store_true')
    repo_ls_p.set_defaults(func=command_repo_ls)

    repo_tracking_p = repo_sub.add_parser('tracking', help='inspect or set repo-local syncwheel tracking policy')
    repo_tracking_sub = repo_tracking_p.add_subparsers(dest='tracking_command', required=True)

    repo_tracking_status_p = repo_tracking_sub.add_parser('status', parents=[common])
    repo_tracking_status_p.add_argument('-j', '--json', action='store_true')
    repo_tracking_status_p.set_defaults(func=command_repo_tracking_status)

    repo_tracking_set_p = repo_tracking_sub.add_parser('set', parents=[common])
    repo_tracking_set_p.add_argument('tracking', choices=sorted(SYNCWHEEL_TRACKING_VALUES))
    repo_tracking_set_p.add_argument('-W', '--worktree-root', help='default repo-relative syncwheel worktree/cache root')
    repo_tracking_set_p.add_argument('-a', '--apply', action='store_true')
    repo_tracking_set_p.add_argument('-j', '--json', action='store_true')
    repo_tracking_set_p.set_defaults(func=command_repo_tracking_set)

    self_p = sub.add_parser('self', help='inspect or update the syncwheel installation itself')
    self_sub = self_p.add_subparsers(dest='self_command', required=True)

    self_status_p = self_sub.add_parser('status', help='show syncwheel install/update status')
    self_status_p.add_argument('-f', '--fetch', action='store_true', help='refresh remote tracking info before reporting')
    self_status_p.add_argument('-j', '--json', action='store_true')
    self_status_p.set_defaults(func=command_self_status)

    self_check_p = self_sub.add_parser('check-update', help='check whether a newer syncwheel version exists')
    self_check_p.add_argument('-f', '--fetch', action='store_true', help='refresh remote tracking info before checking')
    self_check_p.add_argument('-j', '--json', action='store_true')
    self_check_p.set_defaults(func=command_self_check_update)

    self_update_p = self_sub.add_parser('update', help='update this syncwheel install')
    self_update_p.add_argument('-n', '--dry-run', action='store_true')
    self_update_p.add_argument('-F', '--no-fetch', action='store_true')
    self_update_p.set_defaults(func=command_self_update)

    self_hooks_p = self_sub.add_parser('install-hooks', help='install syncwheel Git hooks in this syncwheel checkout')
    self_hooks_p.add_argument('-n', '--dry-run', action='store_true')
    self_hooks_p.set_defaults(func=command_self_install_hooks)

    self_mode_p = self_sub.add_parser('mode', help='show or set automatic update policy: off, notify, auto')
    self_mode_p.add_argument('mode', nargs='?', choices=sorted(UPDATE_MODES))
    self_mode_p.set_defaults(func=command_self_mode)

    use_p = sub.add_parser('use', help='show or set the repo-local default syncwheel profile', parents=[common])
    use_p.add_argument('personal', nargs='?', help='personal profile name to use by default')
    use_p.add_argument('-s', '--shared', action='store_true', help='clear the local profile and use the shared manifest')
    use_p.set_defaults(func=command_use)

    init_p = sub.add_parser('init', aliases=['in'], help='create a starter manifest', parents=[common])
    init_p.add_argument('-C', '--canonical-remote', default='origin')
    init_p.add_argument('-P', '--publication-remote')
    init_p.add_argument('-B', '--base-branch', default='main')
    init_p.add_argument('-I', '--integration-branch')
    init_p.add_argument('-T', '--syncwheel-tracking', choices=sorted(SYNCWHEEL_TRACKING_VALUES))
    init_p.add_argument('-W', '--worktree-root', help='default repo-relative syncwheel worktree/cache root')
    init_p.add_argument('--no-coordination', action='store_true', help='persist manifest v2 coordination mode=disabled')
    init_p.add_argument('--coordination-id', help='public coordination-domain id for a new git-tracked manifest')
    init_p.add_argument('-f', '--force', action='store_true')
    init_p.add_argument('-o', '--stdout', action='store_true')
    init_p.set_defaults(func=command_init)

    coordination_p = sub.add_parser(
        'coordination',
        aliases=['coord'],
        help='enable, inspect, or disable active-active remote coordination',
    )
    coordination_sub = coordination_p.add_subparsers(dest='coordination_command', required=True)

    coordination_init_p = coordination_sub.add_parser('init', parents=[common])
    coordination_init_p.add_argument('-R', '--remote', help='configured publication remote for active-active coordination')
    coordination_init_p.add_argument('--coordination-id', help='public coordination-domain id')
    coordination_init_p.add_argument('-a', '--apply', action='store_true')
    coordination_init_p.set_defaults(func=command_coordination_init)

    coordination_disable_p = coordination_sub.add_parser('disable', parents=[common])
    coordination_disable_p.add_argument('-a', '--apply', action='store_true')
    coordination_disable_p.set_defaults(func=command_coordination_disable)

    handoff_p = sub.add_parser(
        'handoff',
        help='read-only active-active coordination diagnostic for a multi-device handoff',
        parents=[common],
    )
    handoff_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    handoff_p.add_argument('-j', '--json', action='store_true')
    handoff_p.set_defaults(func=command_handoff, fetch=True)

    gc_p = sub.add_parser(
        'gc',
        help='report or reap eligible local tombstoned worktrees, branches, and backups',
        parents=[common],
    )
    gc_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    gc_p.add_argument('-a', '--apply', action='store_true')
    gc_p.add_argument('-j', '--json', action='store_true')
    gc_p.set_defaults(func=command_gc, fetch=True)

    worktree_p = sub.add_parser('worktree', aliases=['wt'], help='manage local Syncwheel worktree safety locks')
    worktree_sub = worktree_p.add_subparsers(dest='worktree_command', required=True)

    worktree_lock_p = worktree_sub.add_parser('lock', parents=[common])
    worktree_lock_p.add_argument('stack')
    worktree_lock_p.set_defaults(func=command_worktree_lock)

    worktree_unlock_p = worktree_sub.add_parser('unlock', parents=[common])
    worktree_unlock_p.add_argument('stack')
    worktree_unlock_p.set_defaults(func=command_worktree_unlock)

    status_p = sub.add_parser('status', aliases=['st'], help='show repo and manifest state', parents=[common])
    status_p.add_argument('-f', '--fetch', action='store_true')
    status_p.add_argument('-j', '--json', action='store_true')
    status_p.set_defaults(func=command_status)

    validate_p = sub.add_parser('validate', aliases=['v'], help='validate the manifest against local git state', parents=[common])
    validate_p.add_argument('-j', '--json', action='store_true')
    validate_p.set_defaults(func=command_validate)

    plan_p = sub.add_parser('plan', aliases=['pl'], help='emit a deterministic action plan from the manifest', parents=[common])
    plan_p.add_argument('-j', '--json', action='store_true')
    plan_p.set_defaults(func=command_plan)

    check_p = sub.add_parser('check', aliases=['ck'], help='fetch, validate, and print the current action plan', parents=[common])
    check_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    check_p.add_argument('-j', '--json', action='store_true')
    check_p.set_defaults(func=command_check, fetch=True)

    ledger_p = sub.add_parser('ledger', help='inspect the append-only syncwheel ledger', parents=[common])
    ledger_sub = ledger_p.add_subparsers(dest='ledger_command', required=True)

    ledger_show_p = ledger_sub.add_parser('show', aliases=['sh'], parents=[common])
    ledger_show_p.add_argument('-j', '--json', action='store_true')
    ledger_show_p.set_defaults(func=command_ledger_show)

    reconcile_p = sub.add_parser(
        'reconcile',
        aliases=['rec'],
        help='reconcile manifest, stack branches, integration, and remote tips',
        parents=[common],
    )
    add_reconcile_args(reconcile_p, include_apply_push=True)
    reconcile_p.set_defaults(func=command_reconcile, fetch=True, update_manifest=True, mode='standard')

    resume_p = sub.add_parser(
        'resume',
        help='resume reconcile from a shared remote state on a new machine',
        parents=[common],
    )
    add_reconcile_args(resume_p, include_apply_push=True)
    resume_p.set_defaults(func=command_resume, fetch=True, update_manifest=True, mode='resume')

    sync_p = sub.add_parser(
        'sync',
        help='apply the safe local reconcile lifecycle without pushing remotes',
        parents=[common],
    )
    add_reconcile_args(sync_p, include_apply_push=False, include_push_options=False)
    sync_p.set_defaults(
        func=command_sync,
        fetch=True,
        update_manifest=True,
        apply=True,
        push=False,
        force_with_lease=True,
        mode='standard',
    )

    publish_p = sub.add_parser(
        'publish',
        help='apply the reconcile lifecycle and push managed branches',
        parents=[common],
    )
    add_reconcile_args(publish_p, include_apply_push=False)
    publish_p.set_defaults(func=command_publish, fetch=True, update_manifest=True, apply=True, push=True, mode='standard')

    manifest_p = sub.add_parser('manifest', aliases=['m'], help='inspect, compare, and migrate syncwheel manifests')
    manifest_sub = manifest_p.add_subparsers(dest='manifest_command', required=True)

    manifest_compare_p = manifest_sub.add_parser('compare', parents=[common])
    manifest_compare_p.add_argument('-O', '--other-manifest')
    manifest_compare_p.add_argument('-P', '--other-personal')
    manifest_compare_p.add_argument('-j', '--json', action='store_true')
    manifest_compare_p.set_defaults(func=command_manifest_compare)

    manifest_require_integration_p = manifest_sub.add_parser(
        'require-integration',
        help='require every declared stack to participate in integration',
        parents=[common],
    )
    manifest_require_integration_p.add_argument('-a', '--apply', action='store_true')
    manifest_require_integration_p.add_argument('-j', '--json', action='store_true')
    manifest_require_integration_p.set_defaults(func=command_manifest_require_integration)

    stack_p = sub.add_parser(
        'stack',
        aliases=['s', 'spoke'],
        help='inspect, create, edit, rebuild, push, or run git for one stack/spoke',
    )
    stack_sub = stack_p.add_subparsers(dest='stack_command', required=True)

    stack_list_p = stack_sub.add_parser('list', aliases=['ls'], parents=[common])
    stack_list_p.set_defaults(func=command_stack_list)

    stack_show_p = stack_sub.add_parser('show', aliases=['sh'], parents=[common])
    stack_show_p.add_argument('stack')
    stack_show_p.set_defaults(func=command_stack_show)

    stack_create_p = stack_sub.add_parser('create', aliases=['new'], parents=[common])
    stack_create_p.add_argument('stack')
    stack_create_p.add_argument('specs', nargs='*', help='optional commit refs or ranges to seed the stack')
    stack_create_p.add_argument('-b', '--branch')
    stack_create_p.add_argument('-B', '--base')
    stack_create_p.add_argument('-R', '--target-remote')
    stack_create_p.add_argument('-T', '--target-branch')
    stack_create_p.add_argument('-I', '--integration-branch')
    stack_create_p.add_argument('-P', '--purpose')
    stack_create_p.add_argument(
        '--draft',
        action='store_true',
        help='create a materialized draft stack on syncwheel/draft/<stack>',
    )
    stack_create_p.add_argument(
        '-u',
        '--include-in-integration',
        action='store_true',
        help='compatibility flag; required-membership manifests include every stack by default',
    )
    stack_create_p.set_defaults(func=command_stack_create)

    stack_promote_p = stack_sub.add_parser(
        'promote',
        aliases=['pro'],
        parents=[common],
        help='promote a draft stack and rename its branch for PR publication',
    )
    stack_promote_p.add_argument('stack')
    stack_promote_p.add_argument('-b', '--branch', help='published PR branch name (default: pr/<stack>)')
    stack_promote_p.set_defaults(func=command_stack_promote)

    stack_demote_p = stack_sub.add_parser(
        'demote',
        aliases=['dem'],
        parents=[common],
        help='demote a published stack to draft without renaming its branch',
    )
    stack_demote_p.add_argument('stack')
    stack_demote_p.set_defaults(func=command_stack_demote)

    stack_sync_p = stack_sub.add_parser('sync', parents=[common])
    stack_sync_p.add_argument('stack')
    stack_sync_p.set_defaults(func=command_stack_sync)

    stack_absorb_p = stack_sub.add_parser('absorb', parents=[common])
    stack_absorb_p.add_argument('stack')
    stack_absorb_p.add_argument('paths', nargs='*', help='optional pathspecs to absorb from the integration worktree')
    stack_absorb_p.add_argument('-s', '--staged', action='store_true', help='absorb staged changes instead of unstaged working tree changes')
    stack_absorb_p.add_argument('-N', '--no-amend', dest='amend', action='store_false', help='create a new stack commit instead of amending the stack tip')
    stack_absorb_p.add_argument('-m', '--message', help='commit message used with --no-amend')
    stack_absorb_p.add_argument('-w', '--worktree', help='stack branch worktree to reuse or create')
    stack_absorb_p.add_argument('-W', '--worktree-root', help='directory where stack absorb creates a worktree when needed')
    stack_absorb_p.add_argument('-f', '--force', action='store_true', help='allow absorbing when the current checkout is not the integration branch')
    stack_absorb_p.set_defaults(func=command_stack_absorb, amend=True)

    stack_set_p = stack_sub.add_parser('set', parents=[common])
    stack_set_p.add_argument('stack')
    stack_set_p.add_argument('specs', nargs='+')
    stack_set_p.set_defaults(func=command_stack_set)

    stack_resolve_p = stack_sub.add_parser(
        'resolve-integration',
        aliases=['resolve'],
        parents=[common],
        help='record conflict-resolved commits that materialize this stack on integration',
    )
    stack_resolve_p.add_argument('stack')
    stack_resolve_p.add_argument('specs', nargs='*')
    stack_resolve_p.add_argument(
        '--empty',
        action='store_true',
        help='record that this stack was absorbed by the integration base or another resolved stack',
    )
    stack_resolve_p.set_defaults(func=command_stack_resolve_integration)

    stack_add_p = stack_sub.add_parser('add', parents=[common])
    stack_add_p.add_argument('stack')
    stack_add_p.add_argument('specs', nargs='+')
    stack_add_p.set_defaults(func=command_stack_add)

    stack_capture_p = stack_sub.add_parser(
        'capture-integration',
        aliases=['capture'],
        parents=[common],
        help='assign integration-first commits to a stack and materialize its branch',
    )
    stack_capture_p.add_argument('stack')
    stack_capture_p.add_argument('specs', nargs='+')
    stack_capture_p.set_defaults(func=command_stack_capture_integration)

    stack_rebuild_p = stack_sub.add_parser('rebuild', aliases=['rb'], parents=[common])
    stack_rebuild_p.add_argument('stack')
    add_rebuild_args(stack_rebuild_p)
    stack_rebuild_p.set_defaults(func=command_stack_rebuild)

    stack_push_p = stack_sub.add_parser('push', parents=[common])
    stack_push_p.add_argument('stack')
    add_push_args(stack_push_p)
    stack_push_p.set_defaults(func=command_stack_push)

    stack_close_p = stack_sub.add_parser(
        'close',
        aliases=['cl'],
        parents=[common],
        help='remove a merged (or abandoned) stack from the manifest and integration',
    )
    stack_close_p.add_argument('stack', help='stack id to close')
    stack_close_p.add_argument(
        '-R',
        '--reason',
        default=None,
        help='reason for closing: merged (default when all commits are in base), abandoned, or custom string',
    )
    stack_close_p.add_argument(
        '-d', '--delete-branch',
        dest='delete_branch',
        action='store_true',
        help='also delete the local branch after closing',
    )
    stack_close_p.add_argument(
        '-f',
        '--force',
        action='store_true',
        help='close even if not all commits are reachable from the base ref',
    )
    stack_close_p.set_defaults(func=command_stack_close, delete_branch=False)

    stack_git_p = stack_sub.add_parser('git', aliases=['g'], parents=[common])
    stack_git_p.add_argument('stack')
    add_git_args(stack_git_p)
    stack_git_p.set_defaults(func=command_stack_git)

    int_p = sub.add_parser('int', aliases=['i'], help='inspect, align, rebuild, push, or run git for integration')
    int_sub = int_p.add_subparsers(dest='int_command', required=True)

    int_show_p = int_sub.add_parser('show', aliases=['sh'], parents=[common])
    int_show_p.set_defaults(func=command_int_show)

    int_sync_status_p = int_sub.add_parser('sync-status', parents=[common])
    int_sync_status_p.add_argument('-R', '--remote')
    int_sync_status_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    int_sync_status_p.add_argument('-j', '--json', action='store_true')
    int_sync_status_p.set_defaults(func=command_int_sync_status, fetch=True)

    int_align_remote_p = int_sub.add_parser('align-remote', parents=[common])
    int_align_remote_p.add_argument('-R', '--remote')
    int_align_remote_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    int_align_remote_p.add_argument('-n', '--dry-run', action='store_true')
    int_align_remote_p.add_argument('-f', '--force', action='store_true')
    int_align_remote_p.set_defaults(func=command_int_align_remote, fetch=True)

    int_rebuild_p = int_sub.add_parser('rebuild', aliases=['rb'], parents=[common])
    add_rebuild_args(int_rebuild_p)
    int_rebuild_p.set_defaults(func=command_int_rebuild)

    int_push_p = int_sub.add_parser('push', parents=[common])
    add_push_args(int_push_p)
    int_push_p.set_defaults(func=command_int_push)

    int_git_p = int_sub.add_parser('git', aliases=['g'], parents=[common])
    add_git_args(int_git_p)
    int_git_p.set_defaults(func=command_int_git)

    return parser


def command_repo_add(args):
    alias = args.alias.strip()
    if not alias:
        raise SyncwheelError('alias must be non-empty')
    path = Path(args.path).expanduser().resolve()
    if not path.exists():
        raise SyncwheelError(f'path does not exist: {path}')
    repo_root = get_repo_root(str(path))
    registry, registry_path = load_repo_registry()
    item = {'path': str(repo_root)}
    if args.manifest:
        item['manifest'] = str(Path(args.manifest).expanduser())
    registry[alias] = item
    save_repo_registry(registry, registry_path)
    print(f'{alias} -> {repo_root}')
    if args.manifest:
        print(f"manifest -> {item['manifest']}")
    return 0


def command_repo_set_manifest(args):
    alias = args.alias
    registry, registry_path = load_repo_registry()
    if alias not in registry:
        raise SyncwheelError(f"alias not found: {alias} (registry: {registry_path})")
    if args.clear:
        registry[alias].pop('manifest', None)
        save_repo_registry(registry, registry_path)
        print(f'cleared manifest for: {alias}')
        return 0
    if not args.manifest:
        raise SyncwheelError('manifest path is required unless --clear is used')
    registry[alias]['manifest'] = str(Path(args.manifest).expanduser())
    save_repo_registry(registry, registry_path)
    print(f"{alias} manifest -> {registry[alias]['manifest']}")
    return 0


def command_repo_rm(args):
    alias = args.alias
    registry, registry_path = load_repo_registry()
    if alias not in registry:
        raise SyncwheelError(f"alias not found: {alias} (registry: {registry_path})")
    del registry[alias]
    save_repo_registry(registry, registry_path)
    print(f'removed: {alias}')
    return 0


def command_repo_ls(args):
    registry, registry_path = load_repo_registry()
    rows = []
    for alias in sorted(registry.keys()):
        entry = registry[alias]
        raw_path = entry['path']
        resolved = str(Path(raw_path).expanduser())
        manifest = entry.get('manifest')
        rows.append({
            'alias': alias,
            'path': raw_path,
            'manifest': manifest,
            'exists': Path(resolved).exists(),
        })
    if args.json:
        print(json.dumps({'registry': str(registry_path), 'repos': rows}, indent=2))
        return 0
    print(f'registry: {registry_path}')
    if not rows:
        print('no aliases configured')
        return 0
    for item in rows:
        suffix = '' if item['exists'] else ' (missing)'
        manifest_part = f" | manifest={item['manifest']}" if item.get('manifest') else ''
        print(f"{item['alias']}\t{item['path']}{suffix}{manifest_part}")
    return 0


def command_self_status(args):
    status, settings, state, state_path = refresh_cached_self_update_status(force=args.fetch)
    hooks = install_hooks_status()
    agentwheel_skill = collect_agentwheel_syncwheel_skill_status()
    output = {
        'settings': settings,
        'settings_path': settings['path'],
        'state_path': str(state_path),
        'last_checked_at': state.get('last_checked_at'),
        'status': status,
        'hooks': hooks,
        'agentwheel_skill': agentwheel_skill,
    }
    if args.json:
        print(json.dumps(output, indent=2))
        return 0
    print(f"install_root: {status['install_root']}")
    print(f"install_kind: {status['install_kind']}")
    print(f"current_version: {status['current_version']}")
    print(f"update_mode: {settings['mode']}")
    print(f"check_interval_seconds: {settings['check_interval_seconds']}")
    if status['git_repo']:
        print(f"branch: {status['branch']}")
        print(f"upstream: {status['upstream'] or 'none'}")
        print(f"clean: {'yes' if status['clean'] else 'no'}")
        print(f"ahead_commits: {status['ahead_commits']}")
        print(f"behind_commits: {status['behind_commits']}")
    else:
        print('git_repo: no')
    if status.get('uv_tool'):
        print('uv_tool: yes')
        print(f"uv_tool_source: {status['uv_tool_source']}")
        print(f"remote_version_url: {status['remote_version_url']}")
    if status.get('reason'):
        print(f"note: {status['reason']}")
    if status['update_available']:
        print(f"update: available ({status['current_version']} -> {status['latest_version']})")
        print(f"recommended: {status['recommended_command']}")
    else:
        print('update: none')
    print(f"hooks_active: {'yes' if hooks['active'] else 'no'}")
    print(f"hooks_path: {hooks['configured_hooks_path'] or 'none'}")
    if agentwheel_skill.get('missing') is True:
        print(
            'agentwheel_skill: missing '
            f"({agentwheel_skill['adapter']}/{agentwheel_skill['installation_type']} "
            f"at {agentwheel_skill['target_root']})"
        )
        print(f"recommended: {agentwheel_skill['install_command']}")
    if output['last_checked_at']:
        print(f"last_checked_at: {output['last_checked_at']}")
    return 0


def command_self_check_update(args):
    status, _, _, _ = refresh_cached_self_update_status(force=args.fetch)
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    if status['update_available']:
        print(f"update available: {status['current_version']} -> {status['latest_version']}")
        print(status['recommended_command'])
    else:
        print(f"up to date: {status['current_version']}")
    if status.get('reason'):
        print(f"note: {status['reason']}")
    return 0


def command_self_update(args):
    before, after, _ = perform_self_update(dry_run=args.dry_run, fetch=not args.no_fetch)
    if args.dry_run:
        return 0
    if before['current_version'] == after['current_version'] and not before['update_available']:
        print(f"already up to date: {after['current_version']}")
        return 0
    print(f"updated syncwheel: {before['current_version']} -> {after['current_version']}")
    return 0


def command_self_install_hooks(args):
    status = install_syncwheel_hooks(dry_run=args.dry_run)
    if args.dry_run:
        return 0
    print(f"hooks_path: {status['configured_hooks_path']}")
    print(f"pre_commit: {'active' if status['active'] else 'inactive'}")
    return 0


def command_self_mode(args):
    if not args.mode:
        settings = load_update_settings()
        print(settings['mode'])
        return 0
    path = set_update_mode(args.mode)
    print(f'{args.mode}\n{path}')
    return 0


def main():
    parser = build_parser()
    raw_args = sys.argv[1:]
    passthrough = []
    if '--' in raw_args:
        marker = raw_args.index('--')
        passthrough = raw_args[marker + 1:]
        raw_args = raw_args[:marker]
    args = parser.parse_args(raw_args)
    args.git_args = passthrough
    try:
        maybe_handle_startup_update_policy(args)
        return args.func(args)
    except SyncwheelError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
