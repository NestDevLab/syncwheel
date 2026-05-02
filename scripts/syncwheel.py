#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import shlex
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


def read_version_file(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


INSTALL_ROOT = Path(__file__).resolve().parents[1]
VERSION = read_version_file(INSTALL_ROOT / 'VERSION') or '0.6.0'


def run(cmd, cwd=None, check=True, input_text=None):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise SyncwheelError(result.stderr.strip() or result.stdout.strip() or f"command failed: {' '.join(cmd)}")
    return result


def git(repo_root, *args, check=True, input_text=None):
    return run(['git', *args], cwd=repo_root, check=check, input_text=input_text)


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


def install_is_clean(root):
    result = git(root, 'status', '--porcelain', check=False)
    return result.returncode == 0 and not result.stdout.strip()


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
    if not upstream:
        status['reason'] = 'syncwheel checkout has no upstream tracking branch'
        return status

    remote = upstream.split('/', 1)[0]
    if fetch:
        git(root, 'fetch', '--quiet', remote, '--tags', check=False)

    if not ref_exists(root, upstream):
        status['reason'] = f'upstream ref not found locally: {upstream}'
        return status

    counts = git(root, 'rev-list', '--left-right', '--count', f'HEAD...{upstream}', check=False)
    parts = counts.stdout.strip().split()
    if len(parts) == 2:
        status['ahead_commits'] = parse_int(parts[0], 0)
        status['behind_commits'] = parse_int(parts[1], 0)

    remote_version = git(root, 'show', f'{upstream}:VERSION', check=False).stdout.strip() or current_version
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


def commit_patch_id(repo_root, commit):
    if commit_parent_count(repo_root, commit) != 1:
        return None
    show = git(repo_root, 'show', '--format=', commit)
    patch_id = run(['git', 'patch-id', '--stable'], input_text=show.stdout)
    line = patch_id.stdout.strip()
    if not line:
        return None
    return line.split()[0]


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


def resolve_stack_rebuild_location(repo_root, stack, args):
    if args.in_place and args.worktree:
        raise SyncwheelError('use either --in-place or --worktree, not both')
    if args.in_place:
        return None, True
    if args.worktree:
        return Path(args.worktree).resolve(), False
    if get_current_branch(repo_root) == stack['branch']:
        return None, True
    return default_worktree_path(repo_root, stack['branch']), False


def resolve_int_rebuild_location(repo_root, manifest, args):
    integration = manifest['integration']
    if args.in_place and args.worktree:
        raise SyncwheelError('use either --in-place or --worktree, not both')
    if args.in_place:
        return None, True
    if args.worktree:
        return Path(args.worktree).resolve(), False
    if get_current_branch(repo_root) == integration['branch']:
        return None, True
    return default_worktree_path(repo_root, integration['branch']), False


def collect_repo_snapshot(repo_root, manifest):
    defaults = manifest['defaults'] if manifest else {}
    canonical_remote = defaults.get('canonical_remote', 'origin')
    base_ref = defaults.get('base_ref') or get_default_remote_head(repo_root, canonical_remote)
    current_branch = get_current_branch(repo_root)
    worktrees = get_worktrees(repo_root)
    stashes = git(repo_root, 'stash', 'list', check=False).stdout.splitlines()
    remotes = git(repo_root, 'remote', '-v', check=False).stdout.splitlines()
    return {
        'repo_root': str(repo_root),
        'current_branch': current_branch,
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
    commands.extend([
        ['git', 'worktree', 'add', '-B', branch, str(worktree), base],
        ['git', '-C', str(worktree), 'cherry-pick', *commit_args],
    ])
    return commands


def integration_stack_commands(manifest, worktree=None):
    integration = manifest['integration']
    stacks_by_id = stack_map(manifest)
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
            commands.append([
                *prefix,
                'merge',
                '--no-ff',
                stack['branch'],
                '-m',
                f"Merge stack '{stack_id}' into {integration['branch']}",
            ])
        return commands
    raise SyncwheelError(f"unsupported integration strategy: {strategy}")


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
    commands.append(['git', 'worktree', 'add', '-B', integration['branch'], str(worktree), integration['base']])
    commands.extend(integration_stack_commands(manifest, worktree))
    return commands


def run_command_list(commands, repo_root, apply):
    if not apply:
        for command in commands:
            print(quoted(command))
        return
    for command in commands:
        run(command, cwd=repo_root)
        print(quoted(command))


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


def command_stack_add(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    commits = list(stack['commits'])
    for spec in args.specs:
        commits.extend(commit_list_for_spec(repo_root, spec))
    stack['commits'] = list(dict.fromkeys(commits))
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
    commands = materialize_pr_commands(repo_root, manifest, stack, worktree, in_place)
    run_command_list(commands, repo_root, not args.dry_run)
    return 0


def command_stack_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    remote = args.remote or stack.get('publication_remote') or manifest['defaults']['publication_remote']
    push_args = passthrough_args(args.git_args)
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


def command_int_rebuild(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    worktree, in_place = resolve_int_rebuild_location(repo_root, manifest, args)
    if not args.dry_run and in_place:
        ensure_in_place_target(repo_root, manifest['integration']['branch'])
    commands = materialize_integration_commands(repo_root, manifest, worktree, in_place)
    run_command_list(commands, repo_root, not args.dry_run)
    return 0


def command_int_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    integration = manifest['integration']
    remote = args.remote or manifest['defaults']['publication_remote']
    push_args = passthrough_args(args.git_args)
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


def add_rebuild_args(parser):
    parser.add_argument('--worktree')
    parser.add_argument('--in-place', action='store_true')
    parser.add_argument('--dry-run', action='store_true')


def add_push_args(parser):
    parser.add_argument('--remote')
    parser.add_argument('--dry-run', action='store_true')


def add_git_args(parser):
    parser.add_argument('--worktree', help='create/use this worktree path when the branch has no worktree')
    parser.add_argument('--auto-worktree', action='store_true', help='create the default worktree when missing')
    return parser


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

    int_p = sub.add_parser('int', aliases=['i'], help='inspect, rebuild, push, or run git for integration')
    int_sub = int_p.add_subparsers(dest='int_command', required=True)

    int_show_p = int_sub.add_parser('show', aliases=['sh'], parents=[common])
    int_show_p.set_defaults(func=command_int_show)

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
    output = {
        'settings': settings,
        'settings_path': settings['path'],
        'state_path': str(state_path),
        'last_checked_at': state.get('last_checked_at'),
        'status': status,
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
