#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import shlex
import tempfile
import subprocess
import sys
import time
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
PROFILE_FILENAME = 'profile.local.json'
INTEGRATION_STRATEGIES = {'cherry-pick', 'merge-stacks'}
DEFAULT_INTEGRATION_BRANCH = 'main-integration'
UPDATE_MODES = {'off', 'notify', 'auto'}
DEFAULT_UPDATE_MODE = 'notify'
DEFAULT_UPDATE_INTERVAL_SECONDS = 6 * 60 * 60
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
COMMIT_CREATING_GIT_ACTIONS = {'cherry-pick', 'commit', 'merge', 'revert'}


def read_version_file(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


INSTALL_ROOT = Path(__file__).resolve().parents[1]
VERSION = read_version_file(INSTALL_ROOT / 'VERSION') or '0.6.0'


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


def install_root():
    return INSTALL_ROOT


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


def collect_self_update_status(root=None, fetch=False):
    root = Path(root or install_root()).resolve()
    current_version = read_version_file(root / 'VERSION') or VERSION
    status = {
        'install_root': str(root),
        'current_version': current_version,
        'latest_version': current_version,
        'git_repo': False,
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
    if not install_is_git_checkout(root):
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


def recommended_self_update_command():
    return f'python3 {shlex.quote(str(Path(__file__).resolve()))} self update'


def refresh_cached_self_update_status(force=False):
    settings = load_update_settings()
    state, state_path = load_update_state()
    now = int(time.time())
    last_checked_epoch = parse_int(state.get('last_checked_epoch'), 0)
    cached = state.get('status') if isinstance(state.get('status'), dict) else None
    stale = force or not cached or (now - last_checked_epoch) >= settings['check_interval_seconds']
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
    if not before['git_repo']:
        raise SyncwheelError(before['reason'] or 'syncwheel install is not a git checkout')
    if not before['upstream']:
        raise SyncwheelError(before['reason'] or 'syncwheel checkout has no upstream tracking branch')
    if before['branch'] == 'DETACHED':
        raise SyncwheelError('syncwheel checkout is detached; self-update requires a branch checkout')
    if not before['clean']:
        raise SyncwheelError('syncwheel checkout is not clean; commit or stash local changes before self-update')

    remote = before['upstream'].split('/', 1)[0]
    commands = []
    if fetch:
        commands.append(['git', 'fetch', '--quiet', remote, '--tags'])
    commands.append(['git', 'merge', '--ff-only', before['upstream']])
    if dry_run:
        for command in commands:
            print(quoted(command))
        return before, before, commands

    for command in commands:
        run(command, cwd=root)
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
                f'Run: {recommended_self_update_command()}',
                file=sys.stderr,
            )
            return
    print(
        f'NOTICE: syncwheel update available ({current_version} -> {latest_version}). '
        f'Run: {recommended_self_update_command()}',
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


def ensure_clean_worktree(path):
    result = run(['git', '-C', str(path), 'status', '--porcelain'], check=False)
    if result.returncode != 0:
        raise SyncwheelError(f'{path} is not a git worktree')
    if result.stdout.strip():
        raise SyncwheelError(f'{path} is not clean')


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
    if data.get('version') != 1:
        raise SyncwheelError('manifest version must be 1')

    defaults = data.setdefault('defaults', {})
    canonical_remote = defaults.setdefault('canonical_remote', 'origin')
    defaults.setdefault('publication_remote', 'fork')
    defaults.setdefault('base_branch', 'main')
    defaults.setdefault('base_ref', f"{canonical_remote}/{defaults['base_branch']}")

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
        seen_ids.add(stack_id)
        seen_branches.add(branch)
        stack.setdefault('base', defaults['base_ref'])
        stack.setdefault('target_remote', canonical_remote)
        stack.setdefault('target_branch', defaults['base_branch'])
        stack.setdefault('integration_branch', integration['branch'])
        if 'meta' in stack and not isinstance(stack['meta'], dict):
            raise SyncwheelError(f'stack {stack_id} meta must be an object when present')
        stack.setdefault('meta', {})
        normalized.append(stack)
    data['stacks'] = normalized
    return data, path


def save_manifest(path, manifest):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + '\n')


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
    path = repo_profile_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2, sort_keys=True) + '\n')
    return path


def default_worktree_path(repo_root, branch):
    safe = branch.replace('/', '-').replace('\\', '-')
    return repo_root.parent / f'{repo_root.name}-wt-{safe}'


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
        'stashes': stashes,
        'remotes': remotes,
    }


def validate_manifest(repo_root, manifest):
    warnings = []
    errors = []
    details = {'stacks': [], 'integration': {}}
    stacks_by_id = stack_map(manifest)
    integration = manifest['integration']
    integration_branch = integration['branch']
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

    for stack in manifest['stacks']:
        item = {
            'id': stack['id'],
            'branch': stack['branch'],
            'meta': stack.get('meta', {}),
            'branch_exists': branch_exists(repo_root, stack['branch']),
            'base_exists': ref_exists(repo_root, stack['base']),
            'target': f"{stack['target_remote']}/{stack['target_branch']}",
            'missing_from_branch': [],
            'missing_from_integration': [],
            'missing_commits': [],
        }
        if not item['base_exists']:
            errors.append(f"stack {stack['id']} base ref does not exist: {stack['base']}")
        if not item['branch_exists']:
            warnings.append(f"stack {stack['id']} branch missing locally: {stack['branch']}")
        for commit in stack['commits']:
            if not commit_exists(repo_root, commit):
                item['missing_commits'].append(commit)
                errors.append(f"stack {stack['id']} references missing commit: {commit}")
                continue
            declared_commits.append(commit)
            declared_commit_shas.add(commit_full_sha(repo_root, commit))
            patch_id = commit_patch_id(repo_root, commit)
            if patch_id:
                declared_patch_ids.add(patch_id)
            if item['branch_exists'] and not branch_contains(repo_root, stack['branch'], commit):
                item['missing_from_branch'].append(commit)
            if integration_exists and not branch_contains(repo_root, integration_branch, commit):
                item['missing_from_integration'].append(commit)
        details['stacks'].append(item)

    integration_commits = []
    unmapped_commits = []
    integration_merge_commits = []
    if integration_exists and ref_exists(repo_root, integration['base']):
        integration_commits = rev_list(repo_root, f"{integration['base']}..{integration_branch}")
        for commit in integration_commits:
            full_sha = commit_full_sha(repo_root, commit)
            if commit_parent_count(repo_root, commit) > 1:
                integration_merge_commits.append(full_sha)
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
        'merge_commits': integration_merge_commits,
    }
    return {'errors': errors, 'warnings': warnings, 'details': details}


def build_plan(repo_root, manifest, validation):
    actions = []
    details = validation['details']
    integration = manifest['integration']
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
        actions.append({
            'type': 'classify_integration_commits',
            'branch': integration['branch'],
            'commits': details['integration']['unmapped_commits'],
        })
    return actions


def quoted(parts):
    return ' '.join(shlex.quote(part) for part in parts)


def worktree_matches_branch(repo_root, branch, worktree):
    if worktree is None:
        return False
    found = find_worktree_for_branch(repo_root, branch)
    if not found:
        return False
    return found.resolve() == Path(worktree).resolve()


def materialize_pr_commands(repo_root, manifest, stack, worktree=None, in_place=False, timestamp=None):
    branch = stack['branch']
    base = stack['base']
    commit_args = stack['commits']
    timestamp = timestamp or syncwheel_timestamp()
    commands = [['git', 'fetch', '--all', '--prune']]
    backup = backup_branch_command(repo_root, branch, timestamp)
    if backup:
        commands.append(backup)
    if in_place:
        commands.extend([
            ['git', 'reset', '--hard', base],
            ['git', 'cherry-pick', *commit_args],
        ])
        return commands
    if worktree_matches_branch(repo_root, branch, worktree):
        commands.extend([
            ['git', '-C', str(worktree), 'reset', '--hard', base],
            ['git', '-C', str(worktree), 'cherry-pick', *commit_args],
        ])
        return commands
    commands.extend([
        ['git', 'worktree', 'add', '-B', branch, str(worktree), base],
        ['git', '-C', str(worktree), 'cherry-pick', *commit_args],
    ])
    return commands


def materialize_stack_projection(repo_root, stack):
    with tempfile.TemporaryDirectory(prefix='syncwheel-stack-projection-') as tmp:
        worktree = Path(tmp)
        git(repo_root, 'worktree', 'add', '--detach', '--quiet', str(worktree), stack['base'])
        try:
            for commit in stack['commits']:
                if branch_contains(worktree, 'HEAD', commit):
                    continue
                command = ['git', '-C', str(worktree), 'cherry-pick', commit]
                run(with_git_identity(repo_root, command), cwd=repo_root)
            return ref_tree(worktree, 'HEAD')
        finally:
            git(repo_root, 'worktree', 'remove', '--force', str(worktree), check=False)


def integration_stack_commands(manifest, worktree=None, stack_ref_overrides=None):
    integration = manifest['integration']
    stacks_by_id = stack_map(manifest)
    stack_ref_overrides = stack_ref_overrides or {}
    prefix = ['git'] if worktree is None else ['git', '-C', str(worktree)]
    strategy = integration.get('strategy', 'cherry-pick')
    if strategy == 'cherry-pick':
        commits = []
        for stack_id in integration['stacks']:
            commits.extend(stacks_by_id[stack_id]['commits'])
        if not commits:
            return []
        return [[*prefix, 'cherry-pick', *commits]]
    if strategy == 'merge-stacks':
        commands = []
        for stack_id in integration['stacks']:
            stack = stacks_by_id[stack_id]
            stack_ref = stack_ref_overrides.get(stack_id, stack['branch'])
            commands.append([
                *prefix,
                'merge',
                '--no-ff',
                stack_ref,
                '-m',
                f"Merge stack '{stack_id}' into {integration['branch']}",
            ])
        return commands
    raise SyncwheelError(f"unsupported integration strategy: {strategy}")


def materialize_integration_projection(repo_root, manifest, stack_ref_overrides=None):
    integration = manifest['integration']
    with tempfile.TemporaryDirectory(prefix='syncwheel-projection-') as tmp:
        worktree = Path(tmp)
        git(repo_root, 'worktree', 'add', '--detach', '--quiet', str(worktree), integration['base'])
        try:
            if integration.get('strategy', 'cherry-pick') == 'cherry-pick':
                stacks_by_id = stack_map(manifest)
                for stack_id in integration['stacks']:
                    for commit in stacks_by_id[stack_id]['commits']:
                        if branch_contains(worktree, 'HEAD', commit):
                            continue
                        command = ['git', '-C', str(worktree), 'cherry-pick', commit]
                        run(with_git_identity(repo_root, command), cwd=repo_root)
            else:
                for command in integration_stack_commands(manifest, worktree, stack_ref_overrides):
                    run(with_git_identity(repo_root, command), cwd=repo_root)
            return ref_tree(worktree, 'HEAD')
        finally:
            git(repo_root, 'worktree', 'remove', '--force', str(worktree), check=False)


def materialize_integration_commands(repo_root, manifest, worktree=None, in_place=False, timestamp=None):
    integration = manifest['integration']
    timestamp = timestamp or syncwheel_timestamp()
    commands = [['git', 'fetch', '--all', '--prune']]
    backup = backup_branch_command(repo_root, integration['branch'], timestamp)
    if backup:
        commands.append(backup)
    if in_place:
        commands.append(['git', 'reset', '--hard', integration['base']])
        commands.extend(integration_stack_commands(manifest))
        return commands
    if worktree_matches_branch(repo_root, integration['branch'], worktree):
        commands.append(['git', '-C', str(worktree), 'reset', '--hard', integration['base']])
        commands.extend(integration_stack_commands(manifest, worktree))
        return commands
    commands.append(['git', 'worktree', 'add', '-B', integration['branch'], str(worktree), integration['base']])
    commands.extend(integration_stack_commands(manifest, worktree))
    return commands


def materialize_remote_align_commands(repo_root, branch, remote_ref, worktree=None, timestamp=None):
    timestamp = timestamp or syncwheel_timestamp()
    commands = [['git', 'fetch', '--all', '--prune']]
    backup = backup_branch_command(repo_root, branch, timestamp)
    if backup:
        commands.append(backup)
    if worktree_matches_branch(repo_root, branch, worktree):
        commands.append(['git', '-C', str(worktree), 'reset', '--hard', remote_ref])
        return commands
    commands.append(['git', 'worktree', 'add', '-B', branch, str(worktree), remote_ref])
    return commands


def run_command_list(commands, repo_root, apply):
    if not apply:
        for command in commands:
            print(quoted(with_git_identity(repo_root, command)))
        return
    for command in commands:
        effective_command = with_git_identity(repo_root, command)
        run(effective_command, cwd=repo_root)
        print(quoted(effective_command))


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
    publication_remote = args.publication_remote
    if args.personal:
        if args.manifest:
            raise SyncwheelError('use either --personal or --manifest, not both')
        manifest_path = personal_manifest_path(repo_root, args.personal)
        integration_branch = args.integration_branch or personal_integration_branch(args.personal)
    else:
        manifest_path = Path(args.manifest).expanduser() if args.manifest else repo_root / '.syncwheel' / 'manifest.json'
        integration_branch = args.integration_branch or DEFAULT_INTEGRATION_BRANCH
    manifest = {
        'version': 1,
        'defaults': {
            'canonical_remote': canonical_remote,
            'publication_remote': publication_remote,
            'base_branch': base_branch,
            'base_ref': f'{canonical_remote}/{base_branch}',
        },
        'integration': {
            'branch': integration_branch,
            'base': f'{canonical_remote}/{base_branch}',
            'strategy': 'cherry-pick',
            'stacks': [],
        },
        'stacks': [],
    }
    output = json.dumps(manifest, indent=2) + '\n'
    if args.stdout:
        print(output, end='')
        return 0
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists() and not args.force:
        raise SyncwheelError(f'manifest already exists: {manifest_path}')
    manifest_path.write_text(output)
    print(manifest_path)
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
        return 0
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
    return 0


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
    output = {
        'snapshot': snapshot,
        'manifest_path': str(manifest_path),
        'validation': validation,
        'plan': plan,
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
        print(f"{stack['id']}\t{stack['branch']}\tcommits={len(stack['commits'])}")
    return 0


def command_stack_show(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    print(json.dumps(stack, indent=2))
    return 0


def command_stack_create(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stacks = stack_map(manifest)
    if args.stack in stacks:
        raise SyncwheelError(f"stack already exists: {args.stack}")
    branch = args.branch or f'pr/{safe_ref_segment(args.stack)}'
    if any(stack['branch'] == branch for stack in manifest['stacks']):
        raise SyncwheelError(f'stack branch already exists in manifest: {branch}')
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
    }
    if args.purpose:
        stack['meta'] = {'purpose': args.purpose}
    manifest['stacks'].append(stack)
    if args.include_in_integration and args.stack not in manifest['integration']['stacks']:
        manifest['integration']['stacks'].append(args.stack)
    save_manifest(manifest_path, manifest)
    print(f"{args.stack}: created {branch} with {len(stack['commits'])} commits")
    return 0


def command_stack_sync(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    commits = rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
    stack['commits'] = commits
    save_manifest(manifest_path, manifest)
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

    stack_worktree = resolve_stack_absorb_location(repo_root, stack, args)
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
    save_manifest(manifest_path, manifest)
    print(f"{args.stack}: absorbed changes into {stack['branch']} and synced {len(stack['commits'])} commits")
    return 0


def resolve_stack_absorb_location(repo_root, stack, args):
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
    if args.worktree_root:
        path = reconcile_worktree_path(repo_root, branch, args.worktree_root)
    else:
        path = default_worktree_path(repo_root, branch)
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
    save_manifest(manifest_path, manifest)
    print(f"{args.stack}: set {len(stack['commits'])} commits")
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
    save_manifest(manifest_path, manifest)
    print(f"{args.stack}: now has {len(stack['commits'])} commits")
    return 0


def command_stack_rebuild(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    worktree, in_place = resolve_stack_rebuild_location(repo_root, stack, args)
    if not args.dry_run and in_place:
        ensure_in_place_target(repo_root, stack['branch'])
    if not args.dry_run and not in_place:
        ensure_non_in_place_target_clean(repo_root, stack['branch'], worktree)
    commands = materialize_pr_commands(repo_root, manifest, stack, worktree, in_place)
    run_command_list(commands, repo_root, not args.dry_run)
    return 0


def command_stack_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    remote = args.remote or stack.get('publication_remote') or manifest['defaults']['publication_remote']
    push_args = push_args_with_options(args)
    command = ['git', 'push', *push_args, remote, stack['branch']]
    if args.dry_run:
        print(quoted(command))
        return 0
    run(command, cwd=repo_root)
    print(quoted(command))
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
        return existing
    if worktree_root:
        safe = branch.replace('/', '-').replace('\\', '-')
        return Path(worktree_root).expanduser().resolve() / safe
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
    if 'reason' in action:
        line += f" reason={action['reason']}"
    if action['type'] in ('align_stack_to_remote', 'align_integration_to_remote'):
        line += ' detail=remote already has the manifest projection; aligning local history'
    elif action['type'] in ('push_stack', 'push_integration'):
        line += ' detail=local projection needs publishing'
    elif action['type'] == 'manual_review':
        line += ' detail=manual review required before applying'
    elif action['type'] == 'rebuild_integration' and action.get('reason') == 'integration_contains_unmapped_commits':
        line += ' detail=integration contains unassigned commits'
    return line


def command_reconcile(args):
    repo_root = resolve_repo_root(args.repo)
    if args.fetch:
        git(repo_root, 'fetch', '--all', '--prune', '--quiet', check=False)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    if args.stack:
        known = stack_map(manifest)
        for stack_id in args.stack:
            if stack_id not in known:
                raise SyncwheelError(f'unknown stack: {stack_id}')
    validation = validate_manifest(repo_root, manifest)
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
    output = {
        'snapshot': collect_repo_snapshot(repo_root, manifest),
        'manifest_path': str(manifest_path),
        'validation': validation,
        'stacks': list(stack_reports.values()),
        'integration': integration_report,
        'actions': actions,
        'applied': args.apply,
        'push': args.push,
    }
    if args.json and not args.apply:
        print(json.dumps(output, indent=2))
        return 1 if validation['errors'] else 0
    print_reconcile_report(output)
    if validation['errors']:
        return 1
    if not args.apply:
        return 0
    manual_actions = [action for action in actions if action['type'] == 'manual_review']
    if manual_actions:
        raise SyncwheelError('reconcile requires manual review before --apply can continue')

    push_args = push_args_with_options(args)
    for action in actions:
        if action['type'] == 'rebuild_stack':
            stack = require_stack(manifest, action['stack'])
            worktree = reconcile_worktree_path(repo_root, stack['branch'], args.worktree_root)
            ensure_non_in_place_target_clean(repo_root, stack['branch'], worktree)
            commands = materialize_pr_commands(repo_root, manifest, stack, worktree, False)
            run_command_list(commands, repo_root, True)
            if args.update_manifest:
                stack['commits'] = rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
                save_manifest(manifest_path, manifest)
                print(f"{stack['id']}: manifest updated from rebuilt branch")
        elif action['type'] == 'align_stack_to_remote':
            stack = require_stack(manifest, action['stack'])
            worktree = reconcile_worktree_path(repo_root, stack['branch'], args.worktree_root)
            ensure_non_in_place_target_clean(repo_root, stack['branch'], worktree)
            commands = materialize_remote_align_commands(
                repo_root,
                stack['branch'],
                action['remote_ref'],
                worktree,
            )
            run_command_list(commands, repo_root, True)
        elif action['type'] == 'push_stack':
            stack = require_stack(manifest, action['stack'])
            remote = args.remote or stack.get('publication_remote') or manifest['defaults']['publication_remote']
            command = ['git', 'push', *push_args, remote, stack['branch']]
            run(command, cwd=repo_root)
            print(quoted(command))
        elif action['type'] == 'rebuild_integration':
            integration = manifest['integration']
            if args.in_place_integration:
                ensure_in_place_target(repo_root, integration['branch'])
                worktree = None
                in_place = True
            else:
                worktree = reconcile_worktree_path(repo_root, integration['branch'], args.worktree_root)
                ensure_non_in_place_target_clean(repo_root, integration['branch'], worktree)
                in_place = False
            commands = materialize_integration_commands(repo_root, manifest, worktree, in_place)
            run_command_list(commands, repo_root, True)
        elif action['type'] == 'align_integration_to_remote':
            integration = manifest['integration']
            worktree = reconcile_worktree_path(repo_root, integration['branch'], args.worktree_root)
            ensure_non_in_place_target_clean(repo_root, integration['branch'], worktree)
            commands = materialize_remote_align_commands(
                repo_root,
                integration['branch'],
                action['remote_ref'],
                worktree,
            )
            run_command_list(commands, repo_root, True)
        elif action['type'] == 'push_integration':
            remote = args.remote or manifest['defaults']['publication_remote']
            command = ['git', 'push', *push_args, remote, manifest['integration']['branch']]
            run(command, cwd=repo_root)
            print(quoted(command))
    return 0


def command_sync(args):
    args.apply = True
    args.push = False
    return command_reconcile(args)


def command_publish(args):
    args.apply = True
    args.push = True
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
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
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
    commands = []
    backup = backup_branch_command(repo_root, integration['branch'], timestamp)
    if backup:
        commands.append(backup)
    commands.append(['git', 'reset', '--hard', report['remote_ref']])
    run_command_list(commands, repo_root, not args.dry_run)
    return 0


def command_int_rebuild(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    worktree, in_place = resolve_int_rebuild_location(repo_root, manifest, args)
    if not args.dry_run and in_place:
        ensure_in_place_target(repo_root, manifest['integration']['branch'])
    if not args.dry_run and not in_place:
        ensure_non_in_place_target_clean(repo_root, manifest['integration']['branch'], worktree)
    commands = materialize_integration_commands(repo_root, manifest, worktree, in_place)
    run_command_list(commands, repo_root, not args.dry_run)
    return 0


def command_int_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    integration = manifest['integration']
    remote = args.remote or manifest['defaults']['publication_remote']
    push_args = push_args_with_options(args)
    command = ['git', 'push', *push_args, remote, integration['branch']]
    if args.dry_run:
        print(quoted(command))
        return 0
    run(command, cwd=repo_root)
    print(quoted(command))
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


def add_rebuild_args(parser):
    parser.add_argument('--worktree')
    parser.add_argument('--in-place', action='store_true')
    parser.add_argument('--dry-run', action='store_true')


def add_push_args(parser):
    parser.add_argument('--remote')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument(
        '--force-with-lease',
        action='store_true',
        help='pass --force-with-lease to git push',
    )


def add_git_args(parser):
    parser.add_argument('--worktree', help='create/use this worktree path when the branch has no worktree')
    parser.add_argument('--auto-worktree', action='store_true', help='create the default worktree when missing')
    return parser


def add_reconcile_args(parser, include_apply_push=True, include_push_options=True):
    parser.add_argument('--no-fetch', dest='fetch', action='store_false')
    parser.add_argument('--json', action='store_true')
    if include_apply_push:
        parser.add_argument('--apply', action='store_true', help='execute the reported rebuild/push plan')
        parser.add_argument('--push', action='store_true', help='push rebuilt or drifted managed branches')
    if include_push_options:
        parser.add_argument(
            '--force-with-lease',
            action='store_true',
            default=True,
            help='pass --force-with-lease to reconcile-managed git pushes (default)',
        )
        parser.add_argument(
            '--no-force-with-lease',
            dest='force_with_lease',
            action='store_false',
            help='use normal git push for reconcile-managed pushes',
        )
    parser.add_argument('--remote', help='remote override for managed branch comparisons and publication')
    parser.add_argument('--stack', action='append', help='limit reconciliation to one stack; may be repeated')
    parser.add_argument('--skip-integration', action='store_true')
    parser.add_argument(
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
        '--rebuild',
        choices=['needed', 'all', 'none'],
        default='needed',
        help='which managed branches to rebuild before optional push',
    )
    parser.add_argument(
        '--worktree-root',
        help='directory where reconcile creates branch worktrees when no worktree already exists',
    )
    parser.add_argument(
        '--in-place-integration',
        action='store_true',
        help='allow integration rebuild in the current clean integration checkout',
    )
    parser.add_argument(
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
    common.add_argument('--manifest', help='path to a syncwheel manifest JSON file')
    common.add_argument('-p', '--personal', help='use .syncwheel/manifests/<name>.local.json')
    sub = parser.add_subparsers(dest='command', required=True)

    repo_p = sub.add_parser('repo', aliases=['r'], help='manage repo aliases')
    repo_sub = repo_p.add_subparsers(dest='repo_command', required=True)

    repo_add_p = repo_sub.add_parser('add', help='add/update one repo alias')
    repo_add_p.add_argument('alias')
    repo_add_p.add_argument('path')
    repo_add_p.add_argument('--manifest', help='optional default manifest path for this alias')
    repo_add_p.set_defaults(func=command_repo_add)

    repo_manifest_p = repo_sub.add_parser('set-manifest', help='set/clear default manifest path for one alias')
    repo_manifest_p.add_argument('alias')
    repo_manifest_p.add_argument('manifest', nargs='?', help='manifest path; omit with --clear to remove')
    repo_manifest_p.add_argument('--clear', action='store_true')
    repo_manifest_p.set_defaults(func=command_repo_set_manifest)

    repo_rm_p = repo_sub.add_parser('rm', help='remove one repo alias')
    repo_rm_p.add_argument('alias')
    repo_rm_p.set_defaults(func=command_repo_rm)

    repo_ls_p = repo_sub.add_parser('ls', help='list repo aliases')
    repo_ls_p.add_argument('--json', action='store_true')
    repo_ls_p.set_defaults(func=command_repo_ls)

    self_p = sub.add_parser('self', help='inspect or update the syncwheel installation itself')
    self_sub = self_p.add_subparsers(dest='self_command', required=True)

    self_status_p = self_sub.add_parser('status', help='show syncwheel install/update status')
    self_status_p.add_argument('--fetch', action='store_true', help='refresh remote tracking info before reporting')
    self_status_p.add_argument('--json', action='store_true')
    self_status_p.set_defaults(func=command_self_status)

    self_check_p = self_sub.add_parser('check-update', help='check whether a newer syncwheel version exists')
    self_check_p.add_argument('--fetch', action='store_true', help='refresh remote tracking info before checking')
    self_check_p.add_argument('--json', action='store_true')
    self_check_p.set_defaults(func=command_self_check_update)

    self_update_p = self_sub.add_parser('update', help='fast-forward this syncwheel checkout to its upstream branch')
    self_update_p.add_argument('--dry-run', action='store_true')
    self_update_p.add_argument('--no-fetch', action='store_true')
    self_update_p.set_defaults(func=command_self_update)

    self_hooks_p = self_sub.add_parser('install-hooks', help='install syncwheel Git hooks in this syncwheel checkout')
    self_hooks_p.add_argument('--dry-run', action='store_true')
    self_hooks_p.set_defaults(func=command_self_install_hooks)

    self_mode_p = self_sub.add_parser('mode', help='show or set automatic update policy: off, notify, auto')
    self_mode_p.add_argument('mode', nargs='?', choices=sorted(UPDATE_MODES))
    self_mode_p.set_defaults(func=command_self_mode)

    use_p = sub.add_parser('use', help='show or set the repo-local default syncwheel profile', parents=[common])
    use_p.add_argument('personal', nargs='?', help='personal profile name to use by default')
    use_p.add_argument('--shared', action='store_true', help='clear the local profile and use the shared manifest')
    use_p.set_defaults(func=command_use)

    init_p = sub.add_parser('init', aliases=['in'], help='create a starter manifest', parents=[common])
    init_p.add_argument('--canonical-remote', default='origin')
    init_p.add_argument('--publication-remote', default='fork')
    init_p.add_argument('--base-branch', default='main')
    init_p.add_argument('--integration-branch')
    init_p.add_argument('--force', action='store_true')
    init_p.add_argument('--stdout', action='store_true')
    init_p.set_defaults(func=command_init)

    status_p = sub.add_parser('status', aliases=['st'], help='show repo and manifest state', parents=[common])
    status_p.add_argument('--fetch', action='store_true')
    status_p.add_argument('--json', action='store_true')
    status_p.set_defaults(func=command_status)

    validate_p = sub.add_parser('validate', aliases=['v'], help='validate the manifest against local git state', parents=[common])
    validate_p.add_argument('--json', action='store_true')
    validate_p.set_defaults(func=command_validate)

    plan_p = sub.add_parser('plan', aliases=['pl'], help='emit a deterministic action plan from the manifest', parents=[common])
    plan_p.add_argument('--json', action='store_true')
    plan_p.set_defaults(func=command_plan)

    check_p = sub.add_parser('check', aliases=['ck'], help='fetch, validate, and print the current action plan', parents=[common])
    check_p.add_argument('--no-fetch', dest='fetch', action='store_false')
    check_p.add_argument('--json', action='store_true')
    check_p.set_defaults(func=command_check, fetch=True)

    reconcile_p = sub.add_parser(
        'reconcile',
        aliases=['rec'],
        help='reconcile manifest, stack branches, integration, and remote tips',
        parents=[common],
    )
    add_reconcile_args(reconcile_p, include_apply_push=True)
    reconcile_p.set_defaults(func=command_reconcile, fetch=True, update_manifest=True)

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
    )

    publish_p = sub.add_parser(
        'publish',
        help='apply the reconcile lifecycle and push managed branches',
        parents=[common],
    )
    add_reconcile_args(publish_p, include_apply_push=False)
    publish_p.set_defaults(func=command_publish, fetch=True, update_manifest=True, apply=True, push=True)

    manifest_p = sub.add_parser('manifest', aliases=['m'], help='inspect and compare syncwheel manifests')
    manifest_sub = manifest_p.add_subparsers(dest='manifest_command', required=True)

    manifest_compare_p = manifest_sub.add_parser('compare', parents=[common])
    manifest_compare_p.add_argument('--other-manifest')
    manifest_compare_p.add_argument('--other-personal')
    manifest_compare_p.add_argument('--json', action='store_true')
    manifest_compare_p.set_defaults(func=command_manifest_compare)

    stack_p = sub.add_parser('stack', aliases=['s'], help='inspect, create, edit, rebuild, push, or run git for one stack')
    stack_sub = stack_p.add_subparsers(dest='stack_command', required=True)

    stack_list_p = stack_sub.add_parser('list', aliases=['ls'], parents=[common])
    stack_list_p.set_defaults(func=command_stack_list)

    stack_show_p = stack_sub.add_parser('show', aliases=['sh'], parents=[common])
    stack_show_p.add_argument('stack')
    stack_show_p.set_defaults(func=command_stack_show)

    stack_create_p = stack_sub.add_parser('create', aliases=['new'], parents=[common])
    stack_create_p.add_argument('stack')
    stack_create_p.add_argument('specs', nargs='*', help='optional commit refs or ranges to seed the stack')
    stack_create_p.add_argument('--branch')
    stack_create_p.add_argument('--base')
    stack_create_p.add_argument('--target-remote')
    stack_create_p.add_argument('--target-branch')
    stack_create_p.add_argument('--integration-branch')
    stack_create_p.add_argument('--purpose')
    stack_create_p.add_argument('--include-in-integration', action='store_true')
    stack_create_p.set_defaults(func=command_stack_create)

    stack_sync_p = stack_sub.add_parser('sync', parents=[common])
    stack_sync_p.add_argument('stack')
    stack_sync_p.set_defaults(func=command_stack_sync)

    stack_absorb_p = stack_sub.add_parser('absorb', parents=[common])
    stack_absorb_p.add_argument('stack')
    stack_absorb_p.add_argument('paths', nargs='*', help='optional pathspecs to absorb from the integration worktree')
    stack_absorb_p.add_argument('--staged', action='store_true', help='absorb staged changes instead of unstaged working tree changes')
    stack_absorb_p.add_argument('--no-amend', dest='amend', action='store_false', help='create a new stack commit instead of amending the stack tip')
    stack_absorb_p.add_argument('-m', '--message', help='commit message used with --no-amend')
    stack_absorb_p.add_argument('--worktree', help='stack branch worktree to reuse or create')
    stack_absorb_p.add_argument('--worktree-root', help='directory where stack absorb creates a worktree when needed')
    stack_absorb_p.add_argument('--force', action='store_true', help='allow absorbing when the current checkout is not the integration branch')
    stack_absorb_p.set_defaults(func=command_stack_absorb, amend=True)

    stack_set_p = stack_sub.add_parser('set', parents=[common])
    stack_set_p.add_argument('stack')
    stack_set_p.add_argument('specs', nargs='+')
    stack_set_p.set_defaults(func=command_stack_set)

    stack_add_p = stack_sub.add_parser('add', parents=[common])
    stack_add_p.add_argument('stack')
    stack_add_p.add_argument('specs', nargs='+')
    stack_add_p.set_defaults(func=command_stack_add)

    stack_rebuild_p = stack_sub.add_parser('rebuild', aliases=['rb'], parents=[common])
    stack_rebuild_p.add_argument('stack')
    add_rebuild_args(stack_rebuild_p)
    stack_rebuild_p.set_defaults(func=command_stack_rebuild)

    stack_push_p = stack_sub.add_parser('push', parents=[common])
    stack_push_p.add_argument('stack')
    add_push_args(stack_push_p)
    stack_push_p.set_defaults(func=command_stack_push)

    stack_git_p = stack_sub.add_parser('git', aliases=['g'], parents=[common])
    stack_git_p.add_argument('stack')
    add_git_args(stack_git_p)
    stack_git_p.set_defaults(func=command_stack_git)

    int_p = sub.add_parser('int', aliases=['i'], help='inspect, align, rebuild, push, or run git for integration')
    int_sub = int_p.add_subparsers(dest='int_command', required=True)

    int_show_p = int_sub.add_parser('show', aliases=['sh'], parents=[common])
    int_show_p.set_defaults(func=command_int_show)

    int_sync_status_p = int_sub.add_parser('sync-status', parents=[common])
    int_sync_status_p.add_argument('--remote')
    int_sync_status_p.add_argument('--no-fetch', dest='fetch', action='store_false')
    int_sync_status_p.add_argument('--json', action='store_true')
    int_sync_status_p.set_defaults(func=command_int_sync_status, fetch=True)

    int_align_remote_p = int_sub.add_parser('align-remote', parents=[common])
    int_align_remote_p.add_argument('--remote')
    int_align_remote_p.add_argument('--no-fetch', dest='fetch', action='store_false')
    int_align_remote_p.add_argument('--dry-run', action='store_true')
    int_align_remote_p.add_argument('--force', action='store_true')
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
    output = {
        'settings': settings,
        'settings_path': settings['path'],
        'state_path': str(state_path),
        'last_checked_at': state.get('last_checked_at'),
        'status': status,
        'hooks': hooks,
    }
    if args.json:
        print(json.dumps(output, indent=2))
        return 0
    print(f"install_root: {status['install_root']}")
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
    if status.get('reason'):
        print(f"note: {status['reason']}")
    if status['update_available']:
        print(f"update: available ({status['current_version']} -> {status['latest_version']})")
        print(f"recommended: {recommended_self_update_command()}")
    else:
        print('update: none')
    print(f"hooks_active: {'yes' if hooks['active'] else 'no'}")
    print(f"hooks_path: {hooks['configured_hooks_path'] or 'none'}")
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
        print(recommended_self_update_command())
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
