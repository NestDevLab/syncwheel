#!/usr/bin/env python3
import argparse
import copy
import contextlib
import datetime
import errno
import fnmatch
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import shlex
import socket
import stat
import tempfile
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX snapshotting reports unsupported at runtime
    fcntl = None


class SyncwheelError(Exception):
    pass


class ManifestDurabilityError(SyncwheelError):
    """The manifest was replaced, but durable directory sync could not be proven."""


class DerivedProvenanceDurabilityError(SyncwheelError):
    """The provenance store was replaced, but directory durability is unknown."""


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
MANIFEST_VERSIONS = {1, 2, 3}
MANIFEST_VERSION_LEGACY = 1
MANIFEST_VERSION_COORDINATED = 2
MANIFEST_VERSION_CHANNELS = 3
COORDINATION_MODES = {'active-active', 'disabled'}
COORDINATION_STATE_SCHEMA_VERSION = 2
COORDINATION_STATE_SCHEMA_VERSION_CHANNELS = 3
COORDINATION_STATE_FILE = '.syncwheel/coordination-state.json'
COORDINATION_STATE_PREFIX = 'syncwheel/state/'
COORDINATION_CLAIM_FILE = 'claim.json'
COORDINATION_CLAIM_PREFIX = 'syncwheel/claim/'
COORDINATION_CLAIM_MODES = {'advisory', 'required'}
EXPECTED_COORDINATION_STATE_UNSET = object()
COORDINATION_REMOTE_ROLE_CANONICAL = 'canonical'
COORDINATION_REMOTE_ROLE_PUBLICATION = 'publication'
COORDINATION_LEASE_SECONDS = 5 * 60
COORDINATION_REPAIR_PLAN_SCHEMA_VERSION = 1
COORDINATION_COMPOSE_PLAN_SCHEMA_VERSION = 1
COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND = 'tree-equivalent-state-cas'
COORDINATION_REPAIR_TREE_EQUIVALENT_PROOF = 'exact-tree-equality'
COORDINATION_REPAIR_FAST_FORWARD_BACKEND = 'fast-forward-state-cas'
COORDINATION_REPAIR_FAST_FORWARD_PROOF = 'exact-fast-forward-ancestry'
COORDINATION_REPAIR_MAX_ADVANCE_COMMITS = 1024
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
AUTHORITY_MODE_HUMAN_GATED = 'human-gated'
AUTHORITY_MODE_AI_MANAGED = 'ai-managed'
AUTHORITY_MODES = {AUTHORITY_MODE_HUMAN_GATED, AUTHORITY_MODE_AI_MANAGED}
AUTHORITY_CLASS_SOURCE_CHANGE = 'source_change'
AUTHORITY_CLASS_RUNTIME_CHANGE = 'runtime_change'
AUTHORITY_CLASS_DESTRUCTIVE_REWRITE = 'destructive_rewrite'
AUTHORITY_CLASSES = (
    AUTHORITY_CLASS_SOURCE_CHANGE,
    AUTHORITY_CLASS_RUNTIME_CHANGE,
    AUTHORITY_CLASS_DESTRUCTIVE_REWRITE,
)
AUTHORITY_GRANTABLE_CLASSES = (AUTHORITY_CLASS_SOURCE_CHANGE, AUTHORITY_CLASS_RUNTIME_CHANGE)
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
MANAGED_PUSH_HOOK_MARKER = '# syncwheel-managed-ref-guard v1'
MANAGED_PRIMARY_PRE_COMMIT_MARKER = '# syncwheel-primary-checkout-guard pre-commit v1'
MANAGED_PRIMARY_POST_CHECKOUT_MARKER = '# syncwheel-primary-checkout-guard post-checkout v1'
MANAGED_REF_MOVE_MARKER = '# syncwheel-ref-move-guard v1'
MANAGED_REPOSITORY_HOOKS = ('pre-push', 'pre-commit', 'post-checkout', 'reference-transaction')
MANAGED_PUSH_AUTH_ENV = 'SYNCWHEEL_PUSH_AUTH_FILE'
MANAGED_PUSH_SECRET_ENV = 'SYNCWHEEL_PUSH_AUTH_SECRET'
MANAGED_PUSH_AUTH_TTL_SECONDS = 60
MANAGED_REF_MOVE_AUTH_ENV = 'SYNCWHEEL_REF_MOVE_AUTH'
SYNCWHEEL_OWNS_REF_MOVES = False
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
REPOSITORY_MODES = {'delivery', 'journal'}
DEFAULT_JOURNAL_INTERVAL = '30m'
CHANNEL_LIFECYCLES = {'shared', 'ephemeral'}
CHANNEL_PLAN_SCHEMA_VERSION = 1
STACK_LAND_PLAN_SCHEMA_VERSION = 1
GOVERNED_WORKTREE_REGISTRY_VERSION = 1
GOVERNED_WORKTREE_DEFAULT_CAPACITY = 4
GOVERNED_WORKTREE_DEFAULT_LEASE_SECONDS = 120 * 60
GOVERNED_WORKTREE_LOCK_TIMEOUT_SECONDS = 5
GOVERNED_WORKTREE_LOCK_STALE_SECONDS = 300
GOVERNED_WORKTREE_LOCK_INCOMPLETE_GRACE_SECONDS = 0.25
GOVERNED_WORKTREE_TERMINAL_EVENT_TYPES = (
    'governed_worktree_released',
    'governed_worktree_reaped',
)
GOVERNED_WORKTREE_REAP_PENDING_REASONS = frozenset({
    'reaping',
    'worktree_remove_failed',
    'branch_delete_failed',
    'recovery_ref_moved',
    'registration_mismatch',
    'ledger_pending',
})
ZERO_OBJECT_ID = '0' * 40
_REGISTRY_EXPECTED_DIGEST_UNSET = object()
DERIVED_PROJECTION_TRAILER = 'syncwheel-derived-projection'
DERIVED_PATHS_TRAILER = 'syncwheel-derived-paths'
DERIVED_OPERATION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}')
DERIVED_PROVENANCE_FIELDS = {
    'operation_id',
    'commit',
    'paths',
    'paths_digest',
    'composition_digest',
}
DERIVED_PROVENANCE_STORE_VERSION = 1
DERIVED_PROVENANCE_OVERRIDE_FIELDS = {'paths', 'base_commit', 'record'}
DERIVED_PATHS_REBUILD_REASON = 'reconcile narrowed derived paths'
DERIVED_PROVENANCE_DISCARD_REASON = (
    'discard clone-local derived provenance superseded by the coordination snapshot'
)
DERIVED_PROVENANCE_RESET_REASON = 'discard an unreadable clone-local derived provenance store'
JOURNAL_SENSITIVE_PARTS = {
    '.env', '.ssh', '.gnupg', '.aws', '.kube', '.docker',
    'id_rsa', 'id_ed25519', 'credentials', 'credentials.json',
}
JOURNAL_SECRET_PATTERNS = (
    ('private key', re.compile(br'-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----')),
    ('OpenSSH private key', re.compile(br'-----BEGIN OPENSSH PRIVATE KEY-----')),
    ('AWS access key', re.compile(br'(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])')),
    ('GitHub token', re.compile(br'(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{36}(?![A-Za-z0-9])')),
    ('GitHub token', re.compile(br'(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{60,}(?![A-Za-z0-9_])')),
    ('Slack token', re.compile(br'(?<![A-Za-z0-9])xoxb-[0-9]{10,}-[A-Za-z0-9-]{20,}(?![A-Za-z0-9])')),
)
JOURNAL_FORBIDDEN_COMMANDS = {
    'stack', 's', 'spoke', 'int', 'i', 'publish', 'sync', 'reconcile', 'rec',
    'resume', 'coordination', 'coord', 'handoff', 'gc', 'worktree', 'wt',
    'plan', 'pl', 'check', 'ck', 'manifest', 'm', 'channel', 'ch',
}


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


def managed_process_env(extra=None):
    process_env = os.environ.copy()
    if SYNCWHEEL_OWNS_REF_MOVES:
        process_env[MANAGED_REF_MOVE_AUTH_ENV] = '1'
    if extra:
        process_env.update(extra)
    return process_env


def run(cmd, cwd=None, check=True, input_text=None, env=None):
    process_env = managed_process_env(env)
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


def git_common_dir(repo_root):
    result = git(repo_root, 'rev-parse', '--git-common-dir')
    path = Path(result.stdout.strip())
    return (repo_root / path).resolve() if not path.is_absolute() else path.resolve()


def active_hooks_dir(repo_root):
    configured = git(repo_root, 'config', '--get', 'core.hooksPath', check=False).stdout.strip()
    if configured:
        path = Path(configured)
        return ((repo_root / path).resolve() if not path.is_absolute() else path.resolve()), configured
    return git_common_dir(repo_root) / 'hooks', None


def managed_push_hook_paths(repo_root):
    hooks_dir, configured = active_hooks_dir(repo_root)
    hook = hooks_dir / 'pre-push'
    backup = hooks_dir / 'pre-push.syncwheel-chain'
    return hooks_dir, hook, backup, configured


def managed_hook_paths(repo_root, hook_name):
    hooks_dir, configured = active_hooks_dir(repo_root)
    hook = hooks_dir / hook_name
    backup = hooks_dir / f'{hook_name}.syncwheel-chain'
    metadata = hooks_dir / f'{hook_name}.syncwheel-meta.json'
    return hooks_dir, hook, backup, metadata, configured


def managed_hook_syncwheel_command():
    if not sys.executable or not __file__:
        raise SyncwheelError('cannot resolve the current Syncwheel invocation for repository hooks')
    executable = os.path.abspath(sys.executable)
    source = os.path.abspath(__file__)
    return f'{shlex.quote(executable)} {shlex.quote(source)}'


def managed_push_hook_content(backup_exists):
    syncwheel_command = managed_hook_syncwheel_command()
    chain = (
        'if [ -x "$hook_dir/pre-push.syncwheel-chain" ]; then\n'
        '  "$hook_dir/pre-push.syncwheel-chain" "$@" <"$input"\n'
        'fi\n'
        if backup_exists else ''
    )
    return (
        '#!/bin/sh\n'
        f'{MANAGED_PUSH_HOOK_MARKER}\n'
        'set -eu\n'
        'hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'input=$(mktemp "${TMPDIR:-/tmp}/syncwheel-pre-push.XXXXXX")\n'
        'trap \'rm -f "$input"\' EXIT HUP INT TERM\n'
        'cat >"$input"\n'
        + chain +
        f'{syncwheel_command} hooks guard '
        '--remote-name "${1:-}" --remote-url "${2:-}" <"$input"\n'
    )


def managed_worktree_hook_content(hook_name, backup_exists):
    if hook_name not in {'pre-commit', 'post-checkout'}:
        raise SyncwheelError(f'unsupported primary-checkout hook: {hook_name}')
    marker = (
        MANAGED_PRIMARY_PRE_COMMIT_MARKER
        if hook_name == 'pre-commit'
        else MANAGED_PRIMARY_POST_CHECKOUT_MARKER
    )
    chain = (
        f'if [ -x "$hook_dir/{hook_name}.syncwheel-chain" ]; then\n'
        f'  "$hook_dir/{hook_name}.syncwheel-chain" "$@"\n'
        'fi\n'
        if backup_exists else ''
    )
    syncwheel_command = managed_hook_syncwheel_command()
    return (
        '#!/bin/sh\n'
        f'{marker}\n'
        'set -eu\n'
        'hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        + chain
        + f'{syncwheel_command} hooks worktree-guard --event {hook_name}\n'
    )


def managed_ref_move_hook_content(backup_exists):
    chain = (
        'if [ -x "$hook_dir/reference-transaction.syncwheel-chain" ]; then\n'
        '  printf \'%s\\n\' "$input" | '
        '"$hook_dir/reference-transaction.syncwheel-chain" "$@" || exit 0\n'
        'fi\n'
        if backup_exists else ''
    )
    syncwheel_command = managed_hook_syncwheel_command()
    # Git runs this for every ref transaction, so it stays cheap and it fails
    # open: only an explicit refusal (exit 2) aborts the update. A missing
    # interpreter or PATH must never be able to block an ordinary commit, and
    # the payload is a few ref lines, so no temporary file is needed.
    return (
        '#!/bin/sh\n'
        f'{MANAGED_REF_MOVE_MARKER}\n'
        '[ "${1:-}" = "prepared" ] || exit 0\n'
        'hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 0\n'
        'input=$(cat) || exit 0\n'
        '[ -n "$input" ] || exit 0\n'
        + chain +
        f'printf \'%s\\n\' "$input" | {syncwheel_command} '
        'hooks ref-guard --phase "$1"\n'
        'status=$?\n'
        '[ "$status" -eq 2 ] && exit 1\n'
        'exit 0\n'
    )


def managed_hook_content(hook_name, backup_exists):
    if hook_name == 'pre-push':
        return managed_push_hook_content(backup_exists)
    if hook_name == 'reference-transaction':
        return managed_ref_move_hook_content(backup_exists)
    return managed_worktree_hook_content(hook_name, backup_exists)


def managed_hook_marker(hook_name):
    return {
        'pre-push': MANAGED_PUSH_HOOK_MARKER,
        'pre-commit': MANAGED_PRIMARY_PRE_COMMIT_MARKER,
        'post-checkout': MANAGED_PRIMARY_POST_CHECKOUT_MARKER,
        'reference-transaction': MANAGED_REF_MOVE_MARKER,
    }[hook_name]


def managed_hook_status(repo_root, hook_name):
    hooks_dir, hook, backup, metadata_path, configured = managed_hook_paths(
        repo_root, hook_name
    )
    existing = hook.read_text() if hook.is_file() else None
    marker = bool(
        existing
        and existing.startswith('#!/bin/sh\n' + managed_hook_marker(hook_name))
    )
    digest = hashlib.sha256(existing.encode()).hexdigest() if existing is not None else None
    try:
        metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        metadata = None
    chained_digest = hashlib.sha256(backup.read_bytes()).hexdigest() if backup.is_file() else None
    chain_matches = bool(
        metadata
        and metadata.get('chainedDigest') == chained_digest
        and (backup.exists() == (metadata.get('chainedDigest') is not None))
    )
    owned = bool(marker and metadata and metadata.get('digest') == digest and chain_matches)
    expected = hashlib.sha256(managed_hook_content(hook_name, backup.exists()).encode()).hexdigest()
    ready = owned and digest == expected
    return {
        'name': hook_name,
        'hooksPath': configured,
        'hooksDir': str(hooks_dir),
        'hook': str(hook),
        'exists': hook.exists(),
        'owned': owned,
        'ready': ready,
        'marker': marker,
        'digest': digest,
        'expectedDigest': expected,
        'metadata': str(metadata_path),
        'chained': backup.exists(),
        'chainedDigest': chained_digest,
        'chainMatches': chain_matches,
        'status': (
            'installed' if ready else
            ('stale' if owned else ('conflict' if hook.exists() or metadata_path.exists() else 'absent'))
        ),
    }
def managed_push_hook_status(repo_root):
    return managed_hook_status(repo_root, 'pre-push')


def managed_hook_bundle_status(repo_root):
    hooks = {name: managed_hook_status(repo_root, name) for name in MANAGED_REPOSITORY_HOOKS}
    expected = {
        name: hook['expectedDigest']
        for name, hook in hooks.items()
    }
    return {
        'ready': all(hook['ready'] for hook in hooks.values()),
        'expectedDigest': hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest(),
        'hooks': hooks,
    }


def managed_push_guard_policy(repo_root, manifest):
    profile = load_repo_profile(repo_root)
    hooks = profile.get('hooks') or {}
    if not isinstance(hooks, dict):
        raise SyncwheelError('syncwheel profile hooks state must be an object')
    disabled = hooks.get('mode') == 'disabled'
    enforced = hooks.get('mode') == 'required'
    reason = hooks.get('reason')
    if disabled and (not isinstance(reason, str) or not reason.strip()):
        raise SyncwheelError('hooks.mode=disabled requires a non-empty persisted reason')
    tracking = manifest.get('syncwheel_tracking')
    required = tracking == SYNCWHEEL_TRACKING_GIT_TRACKED and bool(
        managed_ref_names(manifest)
        or coordination_is_active(manifest)
        or manifest.get('repository_mode') == 'journal'
    )
    bundle = managed_hook_bundle_status(repo_root)
    hook = bundle['hooks']['pre-push']
    ready = bundle['ready']
    return {
        'required': required,
        'disabled': disabled,
        'enforced': enforced,
        'migrationPending': required and not disabled and not enforced,
        'disabledReason': reason if disabled else None,
        'ready': ready,
        'expectedDigest': bundle['expectedDigest'],
        'hook': hook,
        'hooks': bundle['hooks'],
        'mode': (
            'disabled' if disabled else
            ('required' if enforced else ('required-pending-migration' if required else 'optional'))
        ),
    }


def require_managed_push_guard(repo_root, manifest):
    policy = managed_push_guard_policy(repo_root, manifest)
    if policy['required'] and policy['enforced'] and not policy['ready']:
        raise SyncwheelError(
            'managed-ref guard is required but missing, stale, or tampered; '
            'review `syncwheel hooks install`, then run `syncwheel hooks install --apply`'
        )
    return policy


def ensure_managed_repository_hooks(repo_root, manifest):
    policy = managed_push_guard_policy(repo_root, manifest)
    if not policy['required'] or policy['disabled']:
        return policy
    if policy['ready'] and policy['enforced']:
        return policy
    install_managed_push_hook(repo_root, apply=True)
    policy = managed_push_guard_policy(repo_root, manifest)
    if not policy['ready']:
        raise SyncwheelError('managed repository hook bootstrap did not converge')
    return policy


def install_one_managed_hook(repo_root, hook_name, apply=False):
    status = managed_hook_status(repo_root, hook_name)
    if status['ready']:
        return {'action': 'none', **status}
    if status['status'] == 'conflict' and (status['marker'] or Path(status['metadata']).exists()):
        raise SyncwheelError(
            f'managed hook is stale or tampered; refusing automatic replacement: {status["hook"]}'
        )
    if status['status'] == 'conflict' and not status['exists']:
        raise SyncwheelError(f"hook metadata conflict: {status['metadata']}")
    hooks_dir, hook, backup, metadata_path, _ = managed_hook_paths(repo_root, hook_name)
    if backup.exists() and not status['owned']:
        raise SyncwheelError(f'hook chaining conflict: retained backup already exists: {backup}')
    chain_existing = status['exists'] and not status['owned']
    content = managed_hook_content(hook_name, backup.exists() or chain_existing)
    action = 'upgrade' if status['owned'] else 'install'
    plan = {
        'action': action,
        'name': hook_name,
        'hook': str(hook),
        'chainExisting': chain_existing,
        'digest': hashlib.sha256(content.encode()).hexdigest(),
        'apply': apply,
    }
    if not apply:
        return plan
    hooks_dir.mkdir(parents=True, exist_ok=True)
    if chain_existing:
        os.replace(hook, backup)
    try:
        hook.write_text(content)
        hook.chmod(0o755)
        metadata_path.write_text(json.dumps({
            'version': 2,
            'owner': 'syncwheel-managed-repository-hook',
            'hook': hook_name,
            'digest': hashlib.sha256(content.encode()).hexdigest(),
            'chainedDigest': (
                hashlib.sha256(backup.read_bytes()).hexdigest() if backup.exists() else None
            ),
        }, indent=2, sort_keys=True) + '\n')
    except BaseException:
        metadata_path.unlink(missing_ok=True)
        hook.unlink(missing_ok=True)
        if chain_existing and backup.exists():
            os.replace(backup, hook)
        raise
    return {'action': 'upgraded' if action == 'upgrade' else 'installed', **managed_hook_status(repo_root, hook_name)}


def install_managed_push_hook(repo_root, apply=False):
    plans = {
        name: install_one_managed_hook(repo_root, name, apply=False)
        for name in MANAGED_REPOSITORY_HOOKS
    }
    if apply:
        results = {
            name: install_one_managed_hook(repo_root, name, apply=True)
            for name in MANAGED_REPOSITORY_HOOKS
        }
        profile = load_repo_profile(repo_root)
        profile['hooks'] = {'mode': 'required'}
        save_repo_profile(repo_root, profile)
    else:
        results = plans
    bundle = managed_hook_bundle_status(repo_root)
    pre_push = bundle['hooks']['pre-push']
    changed = [item['action'] for item in results.values() if item['action'] != 'none']
    return {
        **pre_push,
        'action': ('installed' if apply else 'install') if changed else 'none',
        'chainExisting': results['pre-push'].get('chainExisting', pre_push['chained']),
        'ready': bundle['ready'],
        'expectedDigest': bundle['expectedDigest'],
        'hooks': bundle['hooks'] if apply else results,
    }


def remove_managed_push_hook(repo_root, apply=False, disable=False, reason=None):
    if disable and (not isinstance(reason, str) or not reason.strip()):
        raise SyncwheelError('--disable requires --reason')
    statuses = {name: managed_hook_status(repo_root, name) for name in MANAGED_REPOSITORY_HOOKS}
    conflicts = [name for name, status in statuses.items() if status['exists'] and not status['owned']]
    if conflicts:
        raise SyncwheelError('hook is not owned by Syncwheel; refusing removal: ' + ', '.join(conflicts))
    plan = {
        'action': 'remove' if any(status['owned'] for status in statuses.values()) else 'none',
        'hooks': {
            name: {'hook': status['hook'], 'restoreChained': status['chained']}
            for name, status in statuses.items() if status['owned']
        },
        'disable': disable, 'reason': reason if disable else None, 'apply': apply,
    }
    if not apply:
        return plan
    for name, status in statuses.items():
        if not status['owned']:
            continue
        _, hook, backup, metadata_path, _ = managed_hook_paths(repo_root, name)
        hook.unlink()
        metadata_path.unlink()
        if backup.exists():
            os.replace(backup, hook)
    if disable:
        profile = load_repo_profile(repo_root)
        profile['hooks'] = {'mode': 'disabled', 'reason': reason.strip()}
        save_repo_profile(repo_root, profile)
    return {
        'action': 'disable' if disable else 'removed',
        'reason': reason.strip() if disable else None,
        'hooks': managed_hook_bundle_status(repo_root)['hooks'],
    }


def managed_push_refs(repo_root, manifest):
    refs = set(managed_ref_names(manifest))
    refs.update(coordination_claim_ref(ref) for ref in list(refs))
    refs.update(delivery_ref_names(manifest))
    config = coordination_config(manifest)
    if config and config.get('mode') == 'active-active':
        refs.add(coordination_state_ref(config))
        previous = read_remote_coordination_state(
            repo_root, config, fetch=True, local_manifest_version=manifest['version']
        )
        if previous.get('state'):
            refs.update(previous['state'].get('managed_refs') or {})
            refs.update(
                coordination_claim_ref(ref)
                for ref in previous['state'].get('managed_refs') or {}
            )
    if manifest.get('repository_mode') == 'journal':
        refs.add(f"refs/heads/{manifest['journal']['branch']}")
    return refs


def parse_pre_push_updates(stream):
    updates = []
    for number, raw in enumerate(stream, 1):
        fields = raw.rstrip('\n').split()
        if len(fields) != 4:
            raise SyncwheelError(f'invalid pre-push input at line {number}')
        updates.append({'localRef': fields[0], 'localSha': fields[1], 'remoteRef': fields[2], 'remoteSha': fields[3]})
    return updates


def push_auth_digest(payload, secret):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(secret.encode() + b'\0' + encoded).hexdigest()


def authorize_syncwheel_push(repo_root, remote, refs):
    auth_dir = git_common_dir(repo_root) / 'syncwheel-push-auth'
    auth_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    payload = {
        'version': 1, 'remote': remote, 'refs': sorted(set(refs)),
        'expiresAt': time.time() + MANAGED_PUSH_AUTH_TTL_SECONDS, 'nonce': uuid.uuid4().hex,
    }
    record = {'payload': payload, 'digest': push_auth_digest(payload, secret)}
    path = auth_dir / f"{payload['nonce']}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(record, handle, sort_keys=True)
    return path, secret


def run_authorized_push(repo_root, command, remote, refs, check=True):
    path, secret = authorize_syncwheel_push(repo_root, remote, refs)
    env = {MANAGED_PUSH_AUTH_ENV: str(path), MANAGED_PUSH_SECRET_ENV: secret}
    try:
        if command[:2] == ['git', 'push']:
            return git(repo_root, *command[1:], env=env, check=check)
        return run(command, cwd=repo_root, env=env, check=check)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def verify_managed_push_authorization(repo_root, remote, refs):
    path_value = os.environ.get(MANAGED_PUSH_AUTH_ENV)
    secret = os.environ.get(MANAGED_PUSH_SECRET_ENV)
    if not path_value or not secret:
        return False
    path = Path(path_value).resolve()
    auth_dir = (git_common_dir(repo_root) / 'syncwheel-push-auth').resolve()
    if path.parent != auth_dir or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        return False
    try:
        record = json.loads(path.read_text())
        payload = record['payload']
        valid = (
            record.get('digest') == push_auth_digest(payload, secret)
            and payload.get('version') == 1
            and payload.get('remote') == remote
            and set(refs).issubset(set(payload.get('refs') or []))
            and bool(refs)
            and float(payload.get('expiresAt', 0)) >= time.time()
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        valid = False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return valid


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


def patch_ids_reachable_from_ref(repo_root, ref):
    return {
        patch_id
        for commit in rev_list(repo_root, ref)
        if (patch_id := commit_patch_id(repo_root, commit))
    }


def fetch_observed_delivery_tip(repo_root, remote, branch):
    """Fetch one delivery ref and bind the proof to its observed remote SHA."""
    delivery_ref = f'refs/heads/{branch}'
    observed = remote_ref_tips(repo_root, remote, [delivery_ref])[delivery_ref]
    if not observed:
        raise SyncwheelError(
            f'cannot prove absorbed content: delivery ref {remote}/{branch} is absent'
        )
    fetched = git(repo_root, 'fetch', '--quiet', remote, delivery_ref, check=False)
    fetched_tip = ref_tip(repo_root, 'FETCH_HEAD') if fetched.returncode == 0 else None
    if fetched.returncode != 0 or fetched_tip != observed:
        raise SyncwheelError(
            f'cannot prove absorbed content: delivery ref {remote}/{branch} changed or '
            'could not be fetched; fetch the delivery ref successfully, then retry'
        )
    return observed


def stack_content_is_present_at_delivery_tip(
    repo_root, stack, delivery_tip, projected_tip=None
):
    """Compare the composed stack result with delivery for every touched path."""
    projected_tip = projected_tip or deterministic_stack_replay_tip(
        repo_root, stack['base'], stack.get('commits') or []
    )
    if not projected_tip:
        return False
    paths = list(dict.fromkeys(
        path
        for commit in stack.get('commits') or []
        for path in commit_changed_files(repo_root, commit)
    ))
    if not paths:
        return False
    comparison = git(
        repo_root,
        'diff',
        '--quiet',
        projected_tip,
        delivery_tip,
        '--',
        *paths,
        check=False,
    )
    return comparison.returncode == 0


def composed_stack_projection_tip(repo_root, stack):
    """Replay the declared chain from the materialized branch's pinned base."""
    materialized_tip = ref_tip(repo_root, stack['branch'])
    if not materialized_tip:
        return None
    projection_base = materialized_tip
    for _commit in stack.get('commits') or []:
        projection_base = commit_first_parent(repo_root, projection_base)
        if not projection_base:
            return None
    projected_tip = deterministic_stack_replay_tip(
        repo_root, projection_base, stack.get('commits') or []
    )
    if not projected_tip or ref_tree(repo_root, projected_tip) != ref_tree(repo_root, materialized_tip):
        return None
    return projected_tip


def commit_short_sha(repo_root, commit):
    return git(repo_root, 'rev-parse', '--short', f'{commit}^{{commit}}').stdout.strip()


def commit_subject(repo_root, commit):
    return git(repo_root, 'show', '-s', '--format=%s', commit).stdout.strip()


def commit_changed_files(repo_root, commit, limit=None):
    result = git(
        repo_root,
        'show',
        '--format=',
        '--name-only',
        '--no-renames',
        '-z',
        commit,
        check=False,
    )
    if result.returncode != 0:
        return []
    files = [path for path in result.stdout.split('\0') if path]
    return files[:limit] if limit else files


def is_manifest_only_commit(repo_root, commit):
    """Whether a commit changes only the tracked Syncwheel coordination manifest."""
    files = commit_changed_files(repo_root, commit)
    return bool(files) and set(files) == {'.syncwheel/manifest.json'}


def integration_composition_digest(manifest):
    """Digest only declared integration composition, not unrelated manifest metadata."""
    integration = manifest['integration']
    stacks = stack_map(manifest)
    return canonical_json_digest({
        'base': integration['base'],
        'strategy': integration['strategy'],
        'stacks': [
            {
                'id': stack_id,
                'commits': list(stacks[stack_id].get('commits') or []),
                'integration_commits': list(stacks[stack_id].get('integration_commits') or []),
                'integration_only_commits': list(stacks[stack_id].get('integration_only_commits') or []),
            }
            for stack_id in integration.get('stacks') or []
            if stack_id in stacks
        ],
    })


def parsed_commit_trailers(repo_root, commit):
    """Return Git's parsed trailer block without accepting trailer-like body text."""
    message = git(repo_root, 'show', '-s', '--format=%B', commit).stdout
    parsed = git(
        repo_root,
        'interpret-trailers',
        '--parse',
        input_text=message,
    ).stdout
    trailers = []
    for line in parsed.splitlines():
        key, separator, value = line.partition(':')
        if separator:
            trailers.append((key.strip(), value.strip()))
    return trailers


def commit_path_blob(repo_root, commit, path):
    """Return the exact blob at path in commit, or None when the path is absent."""
    listing = git(repo_root, 'ls-tree', '-z', commit, '--', path, check=False)
    if listing.returncode != 0:
        return None
    entries = [entry for entry in listing.stdout.split('\0') if entry]
    if not entries:
        return None
    if len(entries) != 1:
        raise SyncwheelError(f'ambiguous tree entry for derived path: {path}')
    metadata, separator, listed_path = entries[0].partition('\t')
    if not separator or listed_path != path:
        raise SyncwheelError(f'ambiguous tree entry for derived path: {path}')
    _mode, object_type, object_id = metadata.split(' ', 2)
    if object_type != 'blob' or not re.fullmatch(r'[0-9a-f]{40,64}', object_id):
        raise SyncwheelError(f'derived path is not a blob: {path}')
    return object_id


def derived_projection_paths_digest(path_blobs):
    """Hash sorted ``path NUL resulting-blob NUL`` records; absence is an empty blob id."""
    digest = hashlib.sha256()
    for path in sorted(path_blobs):
        blob = path_blobs[path]
        if not isinstance(path, str) or not path or '\0' in path:
            raise SyncwheelError('derived projection digest requires non-empty NUL-free paths')
        if blob is not None and (
            not isinstance(blob, str) or not re.fullmatch(r'[0-9a-f]{40,64}', blob)
        ):
            raise SyncwheelError(f'derived projection digest has an invalid blob for {path}')
        digest.update(path.encode('utf-8'))
        digest.update(b'\0')
        digest.update((blob or '').encode('ascii'))
        digest.update(b'\0')
    return digest.hexdigest()


def derived_projection_commit_paths_digest(repo_root, commit, paths):
    return derived_projection_paths_digest({
        path: commit_path_blob(repo_root, commit, path)
        for path in paths
    })


def normalize_derived_provenance(records, prefixes=None, label='derived provenance'):
    if records is None:
        records = []
    if not isinstance(records, list):
        raise SyncwheelError(f'{label} must be an array')
    normalized = []
    operation_ids = set()
    path_sets = set()
    for index, raw in enumerate(records):
        if not isinstance(raw, dict) or set(raw) != DERIVED_PROVENANCE_FIELDS:
            raise SyncwheelError(
                f'{label}[{index}] must contain exactly: '
                + ', '.join(sorted(DERIVED_PROVENANCE_FIELDS))
            )
        operation_id = raw.get('operation_id')
        commit = raw.get('commit')
        paths = raw.get('paths')
        paths_digest = raw.get('paths_digest')
        composition_digest = raw.get('composition_digest')
        if not isinstance(operation_id, str) or not DERIVED_OPERATION_ID.fullmatch(operation_id):
            raise SyncwheelError(f'{label}[{index}].operation_id is invalid')
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise SyncwheelError(f'{label}[{index}].commit must be a full SHA-1')
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) and path and '\0' not in path for path in paths)
            or paths != sorted(paths)
            or len(paths) != len(set(paths))
        ):
            raise SyncwheelError(
                f'{label}[{index}].paths must be a non-empty sorted unique NUL-free string array'
            )
        if prefixes is not None and not all(
            any(path.startswith(prefix) for prefix in prefixes) for path in paths
        ):
            raise SyncwheelError(f'{label}[{index}] contains a path outside integration.derived_paths')
        if not isinstance(paths_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', paths_digest):
            raise SyncwheelError(f'{label}[{index}].paths_digest must be a SHA-256')
        if not isinstance(composition_digest, str) or not re.fullmatch(
            r'[0-9a-f]{64}', composition_digest
        ):
            raise SyncwheelError(f'{label}[{index}].composition_digest must be a SHA-256')
        path_key = tuple(paths)
        if operation_id in operation_ids:
            raise SyncwheelError(f'{label} contains duplicate operation_id {operation_id!r}')
        if path_key in path_sets:
            raise SyncwheelError(f'{label} contains duplicate declared path sets')
        operation_ids.add(operation_id)
        path_sets.add(path_key)
        normalized.append({
            'operation_id': operation_id,
            'commit': commit,
            'paths': list(paths),
            'paths_digest': paths_digest,
            'composition_digest': composition_digest,
        })
    return sorted(normalized, key=lambda item: (item['paths'], item['operation_id']))


def derived_provenance_store_path(repo_root):
    return git_common_dir(repo_root) / 'syncwheel' / 'derived-provenance.json'


def derived_provenance_store_lock_path(repo_root):
    return git_common_dir(repo_root) / 'syncwheel' / 'derived-provenance.lock'


def default_derived_provenance_store():
    return {
        'version': DERIVED_PROVENANCE_STORE_VERSION,
        'overrides': [],
    }


def normalize_derived_provenance_store(data, label='derived provenance store'):
    if not isinstance(data, dict) or set(data) != {'version', 'overrides'}:
        raise SyncwheelError(f'{label} must contain exactly: overrides, version')
    if data.get('version') != DERIVED_PROVENANCE_STORE_VERSION:
        raise SyncwheelError(f'{label} has an unsupported version')
    overrides = data.get('overrides')
    if not isinstance(overrides, list):
        raise SyncwheelError(f'{label}.overrides must be an array')
    normalized = []
    path_sets = set()
    for index, raw in enumerate(overrides):
        item_label = f'{label}.overrides[{index}]'
        if not isinstance(raw, dict) or set(raw) != DERIVED_PROVENANCE_OVERRIDE_FIELDS:
            raise SyncwheelError(
                f'{item_label} must contain exactly: '
                + ', '.join(sorted(DERIVED_PROVENANCE_OVERRIDE_FIELDS))
            )
        paths = raw.get('paths')
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) and path and '\0' not in path for path in paths)
            or paths != sorted(paths)
            or len(paths) != len(set(paths))
        ):
            raise SyncwheelError(
                f'{item_label}.paths must be a non-empty sorted unique NUL-free string array'
            )
        path_key = tuple(paths)
        if path_key in path_sets:
            raise SyncwheelError(f'{label} contains duplicate declared path sets')
        path_sets.add(path_key)
        base_commit = raw.get('base_commit')
        if base_commit is not None and (
            not isinstance(base_commit, str)
            or not re.fullmatch(r'[0-9a-f]{40}', base_commit)
        ):
            raise SyncwheelError(f'{item_label}.base_commit must be null or a full SHA-1')
        record = raw.get('record')
        if record is not None:
            record = normalize_derived_provenance(
                [record], label=f'{item_label}.record'
            )[0]
            if record['paths'] != paths:
                raise SyncwheelError(f'{item_label}.record paths do not match its override key')
        normalized.append({
            'paths': list(paths),
            'base_commit': base_commit,
            'record': record,
        })
    return {
        'version': DERIVED_PROVENANCE_STORE_VERSION,
        'overrides': sorted(normalized, key=lambda item: item['paths']),
    }


def load_derived_provenance_store(repo_root):
    path = derived_provenance_store_path(repo_root)
    if not path.exists():
        return default_derived_provenance_store()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncwheelError(
            f'invalid derived provenance store {path}: {exc}; discard it with '
            + derived_provenance_reset_remedy(whole_store=True)
        ) from exc
    try:
        return normalize_derived_provenance_store(data, str(path))
    except SyncwheelError as exc:
        raise SyncwheelError(
            f'{exc}; discard the derived provenance store with '
            + derived_provenance_reset_remedy(whole_store=True)
        ) from exc


def save_derived_provenance_store(repo_root, store):
    store = normalize_derived_provenance_store(store)
    path = derived_provenance_store_path(repo_root)
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        fsync_directory_path(path.parent.parent)
    payload = (json.dumps(store, indent=2, sort_keys=True) + '\n').encode('utf-8')
    temporary_prefix = f'.{path.name}.tmp-'
    for orphan in path.parent.glob(f'{temporary_prefix}*'):
        orphan.unlink(missing_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=temporary_prefix, dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        try:
            fsync_directory_path(path.parent)
        except OSError as exc:
            raise DerivedProvenanceDurabilityError(
                'derived provenance replace completed but parent directory '
                f'durability check failed; outcome is unknown: {path}'
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            temporary.unlink(missing_ok=True)
    return path


@contextlib.contextmanager
def derived_provenance_store_lock(repo_root):
    if fcntl is None:
        raise SyncwheelError('derived provenance writes require POSIX file locking support')
    lock_path = derived_provenance_store_lock_path(repo_root)
    parent_existed = lock_path.parent.exists()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        fsync_directory_path(lock_path.parent.parent)
    existed = lock_path.exists()
    with lock_path.open('a+b') as handle:
        if not existed:
            os.fchmod(handle.fileno(), 0o600)
            handle.flush()
            os.fsync(handle.fileno())
            fsync_directory_path(lock_path.parent)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def local_coordination_provenance_state(repo_root, manifest):
    """Return the newest locally available published coordination state."""
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        return None
    candidates = []
    remote_state_ref = f"refs/remotes/{config['remote']}/{config['state_branch']}"
    remote_tip = ref_tip(repo_root, remote_state_ref)
    if remote_tip:
        candidates.append(remote_tip)
    _profile, local_coordination = coordination_profile(repo_root)
    seen = local_coordination.get('last_seen_state') or {}
    if seen.get('coordination_id') == config['id'] and isinstance(seen.get('state_tip'), str):
        candidates.append(seen['state_tip'])
    candidates = list(dict.fromkeys(candidates))
    available = [tip for tip in candidates if commit_exists(repo_root, tip)]
    if not available:
        return None
    newest = available[0]
    for candidate in available[1:]:
        if git(
            repo_root,
            'merge-base',
            '--is-ancestor',
            newest,
            candidate,
            check=False,
        ).returncode == 0:
            newest = candidate
    return coordination_state_from_commit(repo_root, newest, config['id'])


def shared_derived_provenance_records(repo_root, manifest, coordination_state=None):
    """Return the active-active snapshot provenance, or an empty clone-local base."""
    active = coordination_is_active(manifest)
    state = coordination_state if active else None
    if active and state is None:
        state = local_coordination_provenance_state(repo_root, manifest)
    if state:
        shared = (state.get('manifest', {}).get('integration', {}).get(
            'derived_provenance'
        ) or [])
    elif active:
        shared = manifest.get('integration', {}).get('derived_provenance') or []
    else:
        shared = []
    return normalize_derived_provenance(
        shared, label='integration.derived_provenance'
    )


def resolve_derived_provenance_overrides(shared, store, *, coordinated=True):
    """Return the effective records plus the clone-local entries the snapshot supersedes.

    Under coordination the snapshot wins: a superseded cache entry is dropped, never raised.
    """
    records = normalize_derived_provenance(
        shared, label='shared derived provenance'
    )
    by_paths = {tuple(item['paths']): item for item in records}
    store = normalize_derived_provenance_store(store)
    diverged = []
    for override in store['overrides']:
        key = tuple(override['paths'])
        current = by_paths.get(key)
        desired = override['record']
        if current == desired:
            continue
        current_commit = current['commit'] if current else None
        if coordinated and current_commit != override['base_commit']:
            diverged.append({
                'paths': list(override['paths']),
                'base_commit': override['base_commit'],
                'local_commit': desired['commit'] if desired else None,
                'snapshot_commit': current_commit,
            })
            continue
        if desired is None:
            by_paths.pop(key, None)
        else:
            by_paths[key] = desired
    return (
        normalize_derived_provenance(
            list(by_paths.values()), label='effective derived provenance'
        ),
        sorted(diverged, key=lambda item: item['paths']),
    )


def apply_derived_provenance_overrides(shared, store, *, coordinated=True):
    return resolve_derived_provenance_overrides(
        shared, store, coordinated=coordinated
    )[0]


def derived_provenance_snapshot(repo_root, manifest, coordination_state=None):
    """Resolve provenance from the shared snapshot plus Git-common-dir state."""
    shared = shared_derived_provenance_records(
        repo_root, manifest, coordination_state
    )
    return resolve_derived_provenance_overrides(
        shared,
        load_derived_provenance_store(repo_root),
        coordinated=coordination_is_active(manifest),
    )


def derived_provenance_records(repo_root, manifest, coordination_state=None):
    return derived_provenance_snapshot(repo_root, manifest, coordination_state)[0]


def update_common_derived_provenance(
    repo_root,
    manifest,
    paths,
    record,
    *,
    expected_commit=None,
    coordination_state=None,
):
    """Atomically set or resolve one complete provenance path set."""
    paths = list(paths)
    if (
        not paths
        or paths != sorted(paths)
        or len(paths) != len(set(paths))
        or not all(isinstance(path, str) and path and '\0' not in path for path in paths)
    ):
        raise SyncwheelError(
            'derived provenance update paths must be a non-empty sorted unique '
            'NUL-free string array'
        )
    if record is not None:
        record = normalize_derived_provenance(
            [record], label='derived provenance update'
        )[0]
        if record['paths'] != paths:
            raise SyncwheelError('derived provenance update record paths do not match')
    with derived_provenance_store_lock(repo_root):
        shared = shared_derived_provenance_records(
            repo_root, manifest, coordination_state
        )
        original_store = load_derived_provenance_store(repo_root)
        shared_by_paths = {
            tuple(item['paths']): item for item in shared
        }
        store = {
            'version': DERIVED_PROVENANCE_STORE_VERSION,
            'overrides': [
                item for item in original_store['overrides']
                if item['record'] != shared_by_paths.get(tuple(item['paths']))
            ],
        }
        store = normalize_derived_provenance_store(store)
        effective = apply_derived_provenance_overrides(
            shared, store, coordinated=coordination_is_active(manifest)
        )
        key = tuple(paths)
        current = {tuple(item['paths']): item for item in effective}.get(key)
        if expected_commit is not None and current is not None and (
            current['commit'] != expected_commit
        ):
            raise SyncwheelError(
                'derived provenance changed before reconciliation for '
                + json.dumps(paths, ensure_ascii=True)
            )
        if expected_commit is not None and current is None:
            return False
        overrides = {
            tuple(item['paths']): item for item in store['overrides']
        }
        shared_record = {
            tuple(item['paths']): item for item in shared
        }.get(key)
        base_commit = shared_record['commit'] if shared_record else None
        if record == shared_record:
            overrides.pop(key, None)
        else:
            overrides[key] = {
                'paths': paths,
                'base_commit': base_commit,
                'record': record,
            }
        updated = {
            'version': DERIVED_PROVENANCE_STORE_VERSION,
            'overrides': list(overrides.values()),
        }
        if normalize_derived_provenance_store(updated) == original_store:
            return False
        save_derived_provenance_store(repo_root, updated)
        return True


def record_common_derived_provenance(
    repo_root, manifest, record, coordination_state=None
):
    normalized = normalize_derived_provenance(
        [record], label='derived provenance record'
    )[0]
    return update_common_derived_provenance(
        repo_root,
        manifest,
        normalized['paths'],
        normalized,
        coordination_state=coordination_state,
    )


def resolve_common_derived_provenance(
    repo_root,
    manifest,
    paths,
    *,
    expected_commit=None,
    coordination_state=None,
):
    return update_common_derived_provenance(
        repo_root,
        manifest,
        sorted(paths),
        None,
        expected_commit=expected_commit,
        coordination_state=coordination_state,
    )


def is_provenance_bound_derived_projection_commit(repo_root, commit, provenance):
    """Verify trailers, content, and provenance independently of the current path policy."""
    if commit_parent_count(repo_root, commit) != 1:
        return False
    full_commit = commit_full_sha(repo_root, commit)
    files = commit_changed_files(repo_root, full_commit)
    if not files:
        return False
    trailers = parsed_commit_trailers(repo_root, full_commit)
    operation_values = [
        value for key, value in trailers
        if key.casefold() == DERIVED_PROJECTION_TRAILER
    ]
    digest_values = [
        value for key, value in trailers
        if key.casefold() == DERIVED_PATHS_TRAILER
    ]
    if (
        len(operation_values) != 1
        or not DERIVED_OPERATION_ID.fullmatch(operation_values[0])
        or len(digest_values) != 1
        or not re.fullmatch(r'[0-9a-f]{64}', digest_values[0])
    ):
        return False
    paths = sorted(files)
    content_digest = derived_projection_commit_paths_digest(
        repo_root, full_commit, paths
    )
    if digest_values[0] != content_digest:
        return False
    records = normalize_derived_provenance(provenance)
    return any(
        record['operation_id'] == operation_values[0]
        and record['commit'] == full_commit
        and record['paths'] == paths
        and record['paths_digest'] == content_digest
        for record in records
    )


def is_derived_projection_commit(repo_root, manifest, commit, provenance=None):
    """Recognize only a currently allowed, provenance-bound provider projection."""
    full_commit = commit_full_sha(repo_root, commit)
    prefixes = manifest.get('integration', {}).get('derived_paths') or []
    files = commit_changed_files(repo_root, full_commit)
    if not files or not prefixes or not all(
        any(path.startswith(prefix) for prefix in prefixes) for path in files
    ):
        return False
    records = (
        normalize_derived_provenance(provenance)
        if provenance is not None
        else derived_provenance_records(repo_root, manifest)
    )
    return is_provenance_bound_derived_projection_commit(
        repo_root, full_commit, records
    )


def narrowed_derived_provenance_records(repo_root, manifest, provenance=None):
    """Return provenance paths excluded by the current derived-path policy."""
    prefixes = manifest.get('integration', {}).get('derived_paths') or []
    records = (
        normalize_derived_provenance(provenance)
        if provenance is not None
        else derived_provenance_records(repo_root, manifest)
    )
    narrowed = []
    for record in records:
        outside = [
            path for path in record['paths']
            if not any(path.startswith(prefix) for prefix in prefixes)
        ]
        for path in outside:
            narrowed.append({**record, 'path': path})
    return sorted(
        narrowed,
        key=lambda item: (item['path'], item['commit'], item['operation_id']),
    )


def derived_provenance_reset_remedy(*, whole_store=False):
    return (
        'syncwheel coordination provenance reset'
        + (' --all' if whole_store else '')
        + ' --reason '
        + shlex.quote(
            DERIVED_PROVENANCE_RESET_REASON
            if whole_store
            else DERIVED_PROVENANCE_DISCARD_REASON
        )
    )


def derived_paths_rebuild_remedy():
    return (
        'syncwheel int rebuild --reason '
        + shlex.quote(DERIVED_PATHS_REBUILD_REASON)
    )


def stale_derived_projection_records(
    repo_root, manifest, integration_branch, provenance=None
):
    """Return shared/local provider provenance no longer reachable from integration."""
    stale = []
    records = (
        normalize_derived_provenance(provenance)
        if provenance is not None
        else derived_provenance_records(repo_root, manifest)
    )
    for record in records:
        if branch_contains(repo_root, integration_branch, record['commit']):
            continue
        for path in record['paths']:
            stale.append({**record, 'path': path})
    return sorted(stale, key=lambda item: (item['path'], item['operation_id']))


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
            shared_data = json.loads(shared_manifest.read_text())
            if shared_data.get('repository_mode') == 'journal':
                shared_expected = shared_data.get('journal', {}).get('branch')
            else:
                shared_expected = shared_data.get('integration', {}).get('branch')
        except (OSError, json.JSONDecodeError):
            pass
    if manifest and manifest.get('repository_mode') == 'journal':
        active_expected = manifest['journal']['branch']
    else:
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


def format_remedy_suffix(commands):
    commands = list(dict.fromkeys(command for command in commands if command))
    return f'. Use: {"; ".join(commands)}' if commands else ''


def ensure_clean_worktree(path, allowed_status_prefixes=None, remedy_commands=None):
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
        raise SyncwheelError(f'{path} is not clean' + format_remedy_suffix(remedy_commands or []))


def normalize_syncwheel_tracking(value, path='manifest'):
    if value is None:
        return None
    if value not in SYNCWHEEL_TRACKING_VALUES:
        allowed = ', '.join(sorted(SYNCWHEEL_TRACKING_VALUES))
        raise SyncwheelError(f'{path} syncwheel_tracking must be one of: {allowed}')
    return value


def default_authority_policy():
    return {
        'mode': AUTHORITY_MODE_HUMAN_GATED,
        'allow': [],
        'deny': [AUTHORITY_CLASS_DESTRUCTIVE_REWRITE],
    }


def normalize_authority_policy(value, path='manifest'):
    if value is None:
        return default_authority_policy()
    if not isinstance(value, dict):
        raise SyncwheelError(f'{path} authority must be an object')
    mode = value.get('mode')
    if mode not in AUTHORITY_MODES:
        allowed = ', '.join(sorted(AUTHORITY_MODES))
        raise SyncwheelError(f'{path} authority.mode must be one of: {allowed}')
    classes = {}
    for field in ('allow', 'deny'):
        raw = value.get(field, [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise SyncwheelError(f'{path} authority.{field} must be a string array')
        unknown = sorted(set(raw) - set(AUTHORITY_CLASSES))
        if unknown:
            raise SyncwheelError(
                f'{path} authority.{field} contains unknown classes: ' + ', '.join(unknown)
                + '; allowed: ' + ', '.join(AUTHORITY_CLASSES)
            )
        classes[field] = set(raw)
    if AUTHORITY_CLASS_DESTRUCTIVE_REWRITE in classes['allow']:
        raise SyncwheelError(f'{path} authority.allow may never contain {AUTHORITY_CLASS_DESTRUCTIVE_REWRITE}')
    classes['deny'].add(AUTHORITY_CLASS_DESTRUCTIVE_REWRITE)
    overlap = sorted(classes['allow'] & classes['deny'])
    if overlap:
        raise SyncwheelError(f'{path} authority classes cannot be both allowed and denied: ' + ', '.join(overlap))
    if mode == AUTHORITY_MODE_HUMAN_GATED and classes['allow']:
        raise SyncwheelError(f'{path} authority.mode={AUTHORITY_MODE_HUMAN_GATED} cannot allow any class')
    if mode == AUTHORITY_MODE_AI_MANAGED and not classes['allow']:
        raise SyncwheelError(f'{path} authority.mode={AUTHORITY_MODE_AI_MANAGED} requires at least one allowed class')
    return {
        'mode': mode,
        'allow': [item for item in AUTHORITY_CLASSES if item in classes['allow']],
        'deny': [item for item in AUTHORITY_CLASSES if item in classes['deny']],
    }


def manifest_authority(manifest):
    if not manifest:
        return default_authority_policy()
    return normalize_authority_policy(manifest.get('authority'))


def authority_allows(manifest, authority_class):
    return authority_class in manifest_authority(manifest)['allow']


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
    claims = value.get('claims')
    if claims is not None:
        if claims not in COORDINATION_CLAIM_MODES:
            raise SyncwheelError('coordination.claims must be one of: advisory, required')
        normalized['claims'] = claims
    unknown = sorted(set(value) - {'mode', 'id', 'remote', 'state_branch', 'gc', 'claims'})
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
    if manifest.get('version') not in {MANIFEST_VERSION_COORDINATED, MANIFEST_VERSION_CHANNELS}:
        return None
    config = manifest.get('coordination')
    if not isinstance(config, dict):
        return config
    return {**config, 'claims': config.get('claims', 'advisory')}


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


def coordination_claim_ref(source_ref):
    if not isinstance(source_ref, str) or not source_ref.startswith('refs/heads/'):
        raise SyncwheelError(
            f'coordination claim requires a full refs/heads source ref: {source_ref!r}'
        )
    return f"refs/heads/{COORDINATION_CLAIM_PREFIX}{source_ref[len('refs/'):]}"


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
        'claims': 'advisory',
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
        'claims': 'advisory',
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


def ensure_in_place_target(repo_root, target_branch, manifest, stack_id=None):
    remedies = primary_checkout_remedy_commands(manifest, stack_id=stack_id)
    current_branch = get_current_branch(repo_root)
    if current_branch != target_branch:
        raise SyncwheelError(
            f'in-place materialization requires current branch {target_branch!r}; '
            f'current branch is {current_branch!r}' + format_remedy_suffix(remedies)
        )
    ensure_clean_worktree(repo_root, remedy_commands=remedies)


def normalize_channel_timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SyncwheelError(f'{field} must be a non-empty ISO-8601 timestamp')
    candidate = value.strip()
    try:
        datetime.datetime.fromisoformat(candidate.replace('Z', '+00:00'))
    except ValueError as exc:
        raise SyncwheelError(f'{field} must be an ISO-8601 timestamp') from exc
    return candidate


def normalize_landing_requirement(value, path='landing.checks', seen_ids=None):
    """Normalize the deliberately small all/any requirement grammar for stack land."""
    if not isinstance(value, dict):
        raise SyncwheelError(f'{path} must be an object')
    requirement_id = value.get('id')
    if not isinstance(requirement_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', requirement_id):
        raise SyncwheelError(f'{path}.id must be a 1-128 character requirement id')
    seen_ids = seen_ids if seen_ids is not None else set()
    if requirement_id in seen_ids:
        raise SyncwheelError(f'duplicate landing requirement id: {requirement_id}')
    seen_ids.add(requirement_id)
    kinds = [kind for kind in ('all', 'any', 'local', 'attestation', 'pr') if kind in value]
    if len(kinds) != 1:
        raise SyncwheelError(f'{path} must contain exactly one of all, any, local, attestation, or pr')
    kind = kinds[0]
    if kind in {'all', 'any'}:
        children = value[kind]
        if not isinstance(children, list) or not children:
            raise SyncwheelError(f'{path}.{kind} must be a non-empty array')
        if set(value) != {'id', kind}:
            raise SyncwheelError(f'{path} {kind} groups may contain only id and {kind}')
        return {'id': requirement_id, kind: [
            normalize_landing_requirement(item, f'{path}.{kind}[{index}]', seen_ids)
            for index, item in enumerate(children)
        ]}
    if kind == 'local':
        local = value['local']
        if not isinstance(local, dict) or set(local) - {'scope', 'argv', 'timeoutSeconds'}:
            raise SyncwheelError(f'{path}.local must contain scope, argv, and optional timeoutSeconds')
        scope = local.get('scope')
        argv = local.get('argv')
        timeout = local.get('timeoutSeconds', 1800)
        if scope not in {'stack', 'integration'}:
            raise SyncwheelError(f'{path}.local.scope must be stack or integration')
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise SyncwheelError(f'{path}.local.argv must be a non-empty string array')
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise SyncwheelError(f'{path}.local.timeoutSeconds must be a positive integer')
        return {'id': requirement_id, 'local': {'scope': scope, 'argv': list(argv), 'timeoutSeconds': timeout}}
    if kind == 'attestation':
        attestation = value['attestation']
        if not isinstance(attestation, dict) or set(attestation) != {'scope', 'verifierArgv'}:
            raise SyncwheelError(f'{path}.attestation must contain exactly scope and verifierArgv')
        if attestation.get('scope') not in {'stack', 'integration'}:
            raise SyncwheelError(f'{path}.attestation.scope must be stack or integration')
        argv = attestation.get('verifierArgv')
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise SyncwheelError(f'{path}.attestation.verifierArgv must be a non-empty string array')
        return {'id': requirement_id, 'attestation': {'scope': attestation['scope'], 'verifierArgv': list(argv)}}
    pr = value['pr']
    if not isinstance(pr, dict) or set(pr) != {'checks'}:
        raise SyncwheelError(f'{path}.pr must contain exactly checks')
    checks = pr.get('checks')
    if not isinstance(checks, list) or not checks or not all(isinstance(item, str) and item for item in checks):
        raise SyncwheelError(f'{path}.pr.checks must be a non-empty string array')
    return {'id': requirement_id, 'pr': {'checks': list(checks)}}


def normalize_landing_policy(value):
    if value is None:
        return {'mode': 'disabled', 'strategy': 'merge', 'checks': None}
    if not isinstance(value, dict):
        raise SyncwheelError('landing must be an object')
    unknown = sorted(set(value) - {'mode', 'strategy', 'checks'})
    if unknown:
        raise SyncwheelError('landing has unknown keys: ' + ', '.join(unknown))
    mode = value.get('mode', 'disabled')
    if mode not in {'disabled', 'direct'}:
        raise SyncwheelError('landing.mode must be disabled or direct')
    strategy = value.get('strategy', 'merge')
    if strategy not in {'merge', 'ff-only'}:
        raise SyncwheelError('landing.strategy must be merge or ff-only')
    checks = value.get('checks')
    return {
        'mode': mode,
        'strategy': strategy,
        'checks': normalize_landing_requirement(checks) if checks is not None else None,
    }


def normalize_channel_entry(raw, channel_id):
    if not isinstance(raw, dict):
        raise SyncwheelError(f'channel {channel_id} composition entries must be objects')
    entry = dict(raw)
    stack_id = entry.get('stack')
    branch = entry.get('branch')
    stack_base = entry.get('stackBase')
    stack_base_revision = entry.get('stackBaseRevision')
    branch_revision = entry.get('branchRevision')
    commits = entry.get('commits')
    if not isinstance(stack_id, str) or not stack_id:
        raise SyncwheelError(f'channel {channel_id} composition entry needs a stack id')
    if not isinstance(branch, str) or not branch:
        raise SyncwheelError(f'channel {channel_id} stack {stack_id} needs a branch')
    if not isinstance(stack_base, str) or not stack_base:
        raise SyncwheelError(f'channel {channel_id} stack {stack_id} needs stackBase')
    if not isinstance(stack_base_revision, str) or not re.fullmatch(r'[0-9a-f]{40}', stack_base_revision):
        raise SyncwheelError(
            f'channel {channel_id} stack {stack_id} stackBaseRevision must be a full commit SHA'
        )
    if not isinstance(branch_revision, str) or not re.fullmatch(r'[0-9a-f]{40}', branch_revision):
        raise SyncwheelError(
            f'channel {channel_id} stack {stack_id} branchRevision must be a full commit SHA'
        )
    if (
        not isinstance(commits, list)
        or not all(isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit) for commit in commits)
    ):
        raise SyncwheelError(
            f'channel {channel_id} stack {stack_id} commits must be ordered full commit SHAs'
        )
    dependencies = entry.get('dependsOn', [])
    if (
        not isinstance(dependencies, list)
        or not all(isinstance(item, str) and item for item in dependencies)
        or len(dependencies) != len(set(dependencies))
        or stack_id in dependencies
    ):
        raise SyncwheelError(
            f'channel {channel_id} stack {stack_id} dependsOn must be a unique string array '
            'that excludes itself'
        )
    normalized = {
        'stack': stack_id,
        'branch': branch,
        'stackBase': stack_base,
        'stackBaseRevision': stack_base_revision,
        'branchRevision': branch_revision,
        'commits': list(commits),
        'dependsOn': list(dependencies),
    }
    return normalized


def normalize_channel_resolution(raw, channel_id, pin_digest):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SyncwheelError(f'channel {channel_id} resolution must be an object')
    required = ('forPinDigest', 'revision', 'tree', 'parentRevision')
    for field in required:
        value = raw.get(field)
        expected_length = 64 if field == 'forPinDigest' else 40
        if not isinstance(value, str) or not re.fullmatch(
            rf'[0-9a-f]{{{expected_length}}}', value
        ):
            raise SyncwheelError(
                f'channel {channel_id} resolution.{field} must be a full object id or digest'
            )
    if raw['forPinDigest'] != pin_digest:
        raise SyncwheelError(
            f'channel {channel_id} resolution does not bind the current pin digest'
        )
    return {field: raw[field] for field in required}


def validate_channel_dependency_order(channel):
    seen = set()
    for entry in channel.get('composition', []):
        missing = [dependency for dependency in entry.get('dependsOn', []) if dependency not in seen]
        if missing:
            raise SyncwheelError(
                f"channel {channel['id']} stack {entry['stack']} requires earlier dependency stack(s): "
                + ', '.join(missing)
            )
        seen.add(entry['stack'])


def normalize_manifest_channels(data, defaults, repository_mode):
    raw_channels = data.get('channels', [])
    if raw_channels and repository_mode != 'delivery':
        raise SyncwheelError('manifest channels are supported only in repository_mode="delivery"')
    if not isinstance(raw_channels, list):
        raise SyncwheelError('manifest channels must be an array')
    seen_ids = set()
    seen_branches = set()
    channels = []
    for raw in raw_channels:
        if not isinstance(raw, dict):
            raise SyncwheelError('each channel entry must be an object')
        channel = dict(raw)
        channel_id = channel.get('id')
        branch = channel.get('branch')
        if not isinstance(channel_id, str) or not channel_id:
            raise SyncwheelError('each channel needs a string id')
        if channel_id in seen_ids:
            raise SyncwheelError(f'duplicate channel id: {channel_id}')
        if not isinstance(branch, str) or not branch:
            raise SyncwheelError(f'channel {channel_id} needs a branch')
        if branch in seen_branches:
            raise SyncwheelError(f'duplicate channel branch: {branch}')
        lifecycle = channel.get('lifecycle', 'shared')
        if lifecycle not in CHANNEL_LIFECYCLES:
            raise SyncwheelError(
                f'channel {channel_id} lifecycle must be one of: '
                + ', '.join(sorted(CHANNEL_LIFECYCLES))
            )
        composition = channel.get('composition', [])
        if not isinstance(composition, list):
            raise SyncwheelError(f'channel {channel_id} composition must be an array')
        normalized_composition = []
        composition_ids = set()
        for entry in composition:
            normalized_entry = normalize_channel_entry(entry, channel_id)
            if normalized_entry['stack'] in composition_ids:
                raise SyncwheelError(
                    f"channel {channel_id} contains stack {normalized_entry['stack']} more than once"
                )
            composition_ids.add(normalized_entry['stack'])
            normalized_composition.append(normalized_entry)
        normalized = {
            'id': channel_id,
            'branch': branch,
            'lifecycle': lifecycle,
            'base': channel.get('base') or defaults['base_ref'],
            'baseRevision': channel.get('baseRevision'),
            'remote': channel.get('remote') or defaults['publication_remote'],
            'composition': normalized_composition,
        }
        if (
            not isinstance(normalized['baseRevision'], str)
            or not re.fullmatch(r'[0-9a-f]{40}', normalized['baseRevision'])
        ):
            raise SyncwheelError(
                f'channel {channel_id} baseRevision must be a full commit SHA'
            )
        if lifecycle == 'ephemeral':
            expiry = channel.get('expiry')
            if not isinstance(expiry, dict):
                raise SyncwheelError(f'ephemeral channel {channel_id} requires expiry metadata')
            normalized['expiry'] = {
                'createdAt': normalize_channel_timestamp(
                    expiry.get('createdAt'), f'channel {channel_id} expiry.createdAt'
                ),
                'expiresAt': normalize_channel_timestamp(
                    expiry.get('expiresAt'), f'channel {channel_id} expiry.expiresAt'
                ),
            }
        elif 'expiry' in channel:
            raise SyncwheelError(f'shared channel {channel_id} must not define expiry metadata')
        validate_channel_dependency_order(normalized)
        pin_digest = channel_pin_digest(normalized)
        resolution = normalize_channel_resolution(
            channel.get('resolution'), channel_id, pin_digest
        )
        if resolution:
            normalized['resolution'] = resolution
        seen_ids.add(channel_id)
        seen_branches.add(branch)
        channels.append(normalized)
    return channels


def validate_channel_stack_authority(channels, stacks):
    """Bind every persisted channel entry to its authoritative stack declaration."""
    by_id = {stack['id']: stack for stack in stacks}
    for channel in channels:
        for entry in channel.get('composition', []):
            stack = by_id.get(entry['stack'])
            if stack is None:
                raise SyncwheelError(
                    f"channel {channel['id']} references unknown stack: {entry['stack']}"
                )
            if entry['branch'] != stack['branch']:
                raise SyncwheelError(
                    f"channel {channel['id']} stack {entry['stack']} pinned branch "
                    f"{entry['branch']!r} does not match stack branch {stack['branch']!r}"
                )
            if entry['stackBase'] != stack['base']:
                raise SyncwheelError(
                    f"channel {channel['id']} stack {entry['stack']} pinned stackBase "
                    f"{entry['stackBase']!r} does not match stack base {stack['base']!r}"
                )
            if list(entry.get('dependsOn', [])) != list(stack.get('depends_on', [])):
                raise SyncwheelError(
                    f"channel {channel['id']} stack {entry['stack']} pinned dependencies "
                    'do not match the authoritative stack declaration'
                )


def symbolic_base_branch_for_remote(value, remote):
    if not isinstance(value, str) or not value or re.fullmatch(r'[0-9a-f]{40}', value):
        return None
    remote_ref_prefix = f'refs/remotes/{remote}/'
    if value.startswith(remote_ref_prefix):
        return value[len(remote_ref_prefix):]
    short_remote_prefix = f'{remote}/'
    if value.startswith(short_remote_prefix):
        return value[len(short_remote_prefix):]
    if value.startswith('refs/remotes/'):
        return None
    if value.startswith('refs/heads/'):
        return value[len('refs/heads/'):]
    if value.startswith('refs/') or value in {'HEAD', '@'}:
        return None
    return value


def validate_channel_branch_ownership(manifest):
    """Keep channel refs disjoint from every authoritative repository ref role."""
    defaults = manifest['defaults']
    integration_branch = manifest['integration']['branch']
    coordination = manifest.get('coordination') or {}
    stacks = manifest.get('stacks', [])
    for channel in manifest.get('channels', []):
        branch = channel['branch']
        remote = channel['remote']
        if branch == defaults['base_branch']:
            raise SyncwheelError(
                f"channel {channel['id']} branch {branch!r} overlaps protected "
                f"defaults.base_branch on remote {remote!r}"
            )
        base_authorities = [
            ('channel.base', channel.get('base')),
            ('defaults.base_ref', defaults.get('base_ref')),
            ('integration.base', manifest['integration'].get('base')),
            *(
                (f"stack {stack['id']} base", stack.get('base'))
                for stack in stacks
            ),
        ]
        conflicting_bases = [
            label for label, value in base_authorities
            if symbolic_base_branch_for_remote(value, remote) == branch
        ]
        if conflicting_bases:
            raise SyncwheelError(
                f"channel {channel['id']} branch {branch!r} on remote {remote!r} "
                'overlaps canonical symbolic base(s): ' + ', '.join(conflicting_bases)
            )
        source_owners = sorted(
            stack['id'] for stack in stacks if stack['branch'] == branch
        )
        if branch == integration_branch or source_owners:
            raise SyncwheelError(
                f"channel {channel['id']} branch overlaps a stack or integration branch: "
                f'{branch}'
            )
        target_owners = sorted(
            stack['id'] for stack in stacks
            if stack.get('target_remote') == remote
            and stack.get('target_branch') == branch
        )
        if target_owners:
            raise SyncwheelError(
                f"channel {channel['id']} branch {branch!r} on remote {remote!r} "
                'overlaps target branch owned by stack(s): ' + ', '.join(target_owners)
            )
        if branch == coordination.get('state_branch'):
            raise SyncwheelError(
                f"channel {channel['id']} branch must not overlap coordination.state_branch"
            )


def stack_base_owner_id(stack, stacks):
    base = stack.get('base')
    if not isinstance(base, str):
        # Public coordination snapshots encode remote refs as typed objects.
        # Their explicit depends_on graph is still validated below.
        return None
    candidates = [
        (len(candidate['branch']), candidate['id'])
        for candidate in stacks
        if candidate['id'] != stack['id']
        and (
            base == candidate['branch']
            or base.endswith(f"/{candidate['branch']}")
        )
    ]
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def derive_stack_dependencies(stacks):
    for stack in stacks:
        owner = stack_base_owner_id(stack, stacks)
        if owner and owner not in stack.get('depends_on', []):
            stack['depends_on'] = [*stack.get('depends_on', []), owner]
    return stacks


def validate_stack_dependency_graph(stacks, require_declared_dependencies=True):
    by_id = {stack['id']: stack for stack in stacks}
    for stack in stacks:
        dependencies = stack.get('depends_on', [])
        unknown = sorted(set(dependencies) - set(by_id))
        if unknown:
            raise SyncwheelError(
                f"stack {stack['id']} depends_on unknown stack(s): " + ', '.join(unknown)
            )
        if stack['id'] in dependencies:
            raise SyncwheelError(f"stack {stack['id']} must not depend on itself")
        base_owner = stack_base_owner_id(stack, stacks)
        if (
            require_declared_dependencies
            and base_owner
            and base_owner not in dependencies
        ):
            raise SyncwheelError(
                f"stack {stack['id']} base {stack['base']!r} belongs to stack {base_owner}; "
                f'declare depends_on: ["{base_owner}"]'
            )

    visiting = set()
    visited = set()

    def visit(stack_id, path):
        if stack_id in visiting:
            cycle = path[path.index(stack_id):] + [stack_id]
            raise SyncwheelError('stack dependency cycle: ' + ' -> '.join(cycle))
        if stack_id in visited:
            return
        visiting.add(stack_id)
        for dependency in by_id[stack_id].get('depends_on', []):
            visit(dependency, [*path, dependency])
        visiting.remove(stack_id)
        visited.add(stack_id)

    for stack_id in by_id:
        visit(stack_id, [stack_id])


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
    if 'authority' in data:
        data['authority'] = normalize_authority_policy(data.get('authority'))
    repository_mode = data.get('repository_mode', 'delivery')
    if repository_mode not in REPOSITORY_MODES:
        raise SyncwheelError(
            'repository_mode must be one of: ' + ', '.join(sorted(REPOSITORY_MODES))
        )
    data['repository_mode'] = repository_mode
    if repository_mode == 'journal':
        raw_journal = data.get('journal')
        if not isinstance(raw_journal, dict):
            raise SyncwheelError('journal mode requires a journal object')
        journal = dict(raw_journal)
        for field in ('branch', 'remote'):
            if not isinstance(journal.get(field), str) or not journal[field].strip():
                raise SyncwheelError(f'journal.{field} must be a non-empty string')
        for field in ('include', 'exclude'):
            value = journal.get(field)
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise SyncwheelError(f'journal.{field} must be a string array')
            if field == 'include' and not value:
                raise SyncwheelError('journal.include must be a non-empty explicit allowlist')
            journal[field] = value
        max_bytes = journal.get('max_file_bytes')
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise SyncwheelError('journal.max_file_bytes must be a positive integer')
        journal['max_file_bytes'] = max_bytes
        interval = journal.get('interval', DEFAULT_JOURNAL_INTERVAL)
        if not isinstance(interval, str) or not re.fullmatch(r'[1-9][0-9]*(?:s|min|m|h|d|w)', interval):
            raise SyncwheelError('journal.interval must be a positive systemd duration such as 30m')
        journal['interval'] = interval
        data['journal'] = journal
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
    if defaults.get('replay_mode') is not None:
        normalize_replay_mode(defaults['replay_mode'], 'manifest defaults.replay_mode')
    if 'landing' in data:
        data['landing'] = normalize_landing_policy(data['landing'])

    integration = data.setdefault('integration', {})
    integration.setdefault('branch', DEFAULT_INTEGRATION_BRANCH)
    integration.setdefault('base', defaults['base_ref'])
    integration.setdefault('strategy', 'cherry-pick')
    integration.setdefault('stacks', [])
    if version == MANIFEST_VERSION_CHANNELS:
        derived_paths = integration.setdefault('derived_paths', [])
        if (
            not isinstance(derived_paths, list)
            or not all(isinstance(item, str) and item.endswith('/') and item for item in derived_paths)
            or len(derived_paths) != len(set(derived_paths))
        ):
            raise SyncwheelError('integration.derived_paths must be a unique string array of path prefixes')
        if 'derived_provenance' in integration:
            integration['derived_provenance'] = normalize_derived_provenance(
                integration['derived_provenance'],
                label='integration.derived_provenance',
            )
    elif 'derived_paths' in integration:
        raise SyncwheelError('integration.derived_paths requires manifest version 3')
    elif 'derived_provenance' in integration:
        raise SyncwheelError('integration.derived_provenance requires manifest version 3')

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
        integration_only_commits = stack.get('integration_only_commits')
        if integration_only_commits is not None and (
            not isinstance(integration_only_commits, list)
            or not all(isinstance(c, str) and c for c in integration_only_commits)
            or len(integration_only_commits) != len(set(integration_only_commits))
        ):
            raise SyncwheelError(
                f'stack {stack_id} integration_only_commits must be a unique string array when present'
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
        dependencies = stack.get('depends_on', [])
        if (
            not isinstance(dependencies, list)
            or not all(isinstance(item, str) and item for item in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            raise SyncwheelError(f'stack {stack_id} depends_on must be a unique string array')
        if dependencies and version != MANIFEST_VERSION_CHANNELS:
            raise SyncwheelError(
                f'stack {stack_id} depends_on requires manifest version 3; '
                'migrate explicitly with channel create --apply before declaring dependencies'
            )
        if dependencies:
            stack['depends_on'] = list(dependencies)
        else:
            stack.pop('depends_on', None)
        stack.setdefault('meta', {})
        normalized.append(stack)
    data['stacks'] = normalized
    validate_stack_dependency_graph(
        data['stacks'], require_declared_dependencies=version == MANIFEST_VERSION_CHANNELS
    )
    data['channels'] = normalize_manifest_channels(data, defaults, repository_mode)
    validate_channel_stack_authority(data['channels'], data['stacks'])
    if version == MANIFEST_VERSION_CHANNELS and repository_mode != 'delivery':
        raise SyncwheelError('manifest version 3 is supported only for repository_mode="delivery"')
    if data.get('channels') and version != MANIFEST_VERSION_CHANNELS:
        raise SyncwheelError('manifest channels require version 3; migrate explicitly with channel create --apply')
    if version in {MANIFEST_VERSION_COORDINATED, MANIFEST_VERSION_CHANNELS} and repository_mode != 'journal':
        coordination = normalize_coordination(data.get('coordination'), path)
        if coordination['remote'] != defaults['publication_remote']:
            raise SyncwheelError(
                'coordination.remote must match defaults.publication_remote'
            )
        data['coordination'] = coordination
        if version == MANIFEST_VERSION_CHANNELS and coordination['mode'] == 'active-active':
            mismatched = [
                channel['id'] for channel in data['channels']
                if channel['remote'] != coordination['remote']
            ]
            if mismatched:
                raise SyncwheelError(
                    'active-active channel remote must match coordination.remote for: '
                    + ', '.join(mismatched)
                )
    validate_channel_branch_ownership(data)
    return data, path


def save_manifest(path, manifest):
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = None
    if path.exists() or path.is_symlink():
        existing = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(existing.st_mode):
            raise SyncwheelError(f'manifest path is not a regular file: {path}')
        existing_mode = stat.S_IMODE(existing.st_mode)
    payload = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.tmp-', dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    replaced = False
    try:
        if existing_mode is not None:
            os.fchmod(descriptor, existing_mode)
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        require_manifest_transaction_current(path)
        os.replace(temporary_path, path)
        replaced = True
        transaction = active_manifest_write_transaction(path)
        if transaction is not None:
            transaction['expectedDigest'] = manifest_digest(manifest)
        directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        try:
            directory_fd = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ManifestDurabilityError(
                f'manifest replace completed but parent directory durability check failed; '
                f'outcome is unknown: {path}'
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            temporary_path.unlink(missing_ok=True)


def stack_integration_base_commits(stack):
    """Return the source or resolved commits that materialize a stack on integration.

    Source commits remain authoritative for rebuilding the stack branch. A resolved
    integration projection can be recorded separately after conflict resolution so
    validation never asks Syncwheel to rewrite that source branch with integration
    commits.
    """
    return list(stack.get('integration_commits', stack['commits']))


def stack_integration_only_commits(stack):
    """Return commits owned by a stack only in its integration projection."""
    return list(stack.get('integration_only_commits') or [])


def stack_integration_commits(stack):
    """Return every commit owned by a stack in the integration projection."""
    return list(dict.fromkeys([
        *stack_integration_base_commits(stack),
        *stack_integration_only_commits(stack),
    ]))


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


def is_personal_manifest_path(repo_root, manifest_path):
    if manifest_path is None:
        return False
    repo_root_path = Path(repo_root).expanduser().resolve(strict=False)
    manifest_root = Path(manifest_path).expanduser().resolve(strict=False)
    try:
        relative = manifest_root.relative_to(repo_root_path)
    except ValueError:
        return False
    return (
        relative.parent == Path('.syncwheel/manifests')
        and relative.name.endswith('.local.json')
    )


def ledger_root(repo_root, manifest_path=None):
    if manifest_path is None:
        return repo_root / '.syncwheel' / 'ledger'
    repo_root_path = Path(repo_root).expanduser().resolve(strict=False)
    manifest_root = Path(manifest_path).expanduser().resolve(strict=False)
    if is_external_manifest_path(repo_root_path, manifest_root) or is_personal_manifest_path(
        repo_root_path, manifest_root
    ):
        return external_ledger_root(manifest_path)
    return repo_root / '.syncwheel' / 'ledger'


def ledger_events_dir(repo_root, manifest_path=None):
    return ledger_root(repo_root, manifest_path) / 'events'


def ledger_checkpoints_dir(repo_root, manifest_path=None):
    return ledger_root(repo_root, manifest_path) / 'checkpoints'


_CHANNEL_MUTATION_LOCK_STATE = threading.local()
_MANIFEST_WRITE_TRANSACTION_STATE = threading.local()


@contextlib.contextmanager
def channel_mutation_lock(repo_root, manifest_path, channel_id):
    if fcntl is None:
        raise SyncwheelError('channel mutations require POSIX file locking support')
    if is_external_manifest_path(repo_root, manifest_path):
        lock_dir = ledger_root(repo_root, manifest_path) / 'locks'
    else:
        common_dir = git(repo_root, 'rev-parse', '--git-common-dir').stdout.strip()
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = repo_root / common_path
        lock_dir = common_path.resolve(strict=False) / 'syncwheel-locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_key = hashlib.sha256(
        str(Path(manifest_path).resolve(strict=False)).encode('utf-8')
    ).hexdigest()[:32]
    lock_path = lock_dir / f'manifest-{lock_key}.lock'
    held = getattr(_CHANNEL_MUTATION_LOCK_STATE, 'held', None)
    if held is None:
        held = {}
        _CHANNEL_MUTATION_LOCK_STATE.held = held
    lock_identity = str(lock_path.resolve(strict=False))
    if lock_identity in held:
        held[lock_identity] += 1
        try:
            yield lock_path
        finally:
            held[lock_identity] -= 1
        return
    with lock_path.open('a+') as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        held[lock_identity] = 1
        try:
            yield lock_path
        finally:
            held.pop(lock_identity, None)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def active_manifest_write_transaction(manifest_path):
    transactions = getattr(_MANIFEST_WRITE_TRANSACTION_STATE, 'transactions', {})
    return transactions.get(str(Path(manifest_path).resolve(strict=False)))


def require_manifest_transaction_current(manifest_path):
    transaction = active_manifest_write_transaction(manifest_path)
    if transaction is None:
        return
    current, _ = load_manifest(transaction['repoRoot'], manifest_path)
    current_digest = manifest_digest(current) if current is not None else None
    if current_digest != transaction['expectedDigest']:
        raise SyncwheelError(
            'manifest changed outside the active transaction; refusing stale write or side effect'
        )


@contextlib.contextmanager
def manifest_write_transaction(repo_root, manifest_path, owner='manifest-command'):
    identity = str(Path(manifest_path).resolve(strict=False))
    transactions = getattr(_MANIFEST_WRITE_TRANSACTION_STATE, 'transactions', None)
    if transactions is None:
        transactions = {}
        _MANIFEST_WRITE_TRANSACTION_STATE.transactions = transactions
    existing = transactions.get(identity)
    if existing is not None:
        existing['depth'] += 1
        try:
            yield
        finally:
            existing['depth'] -= 1
        return
    with channel_mutation_lock(repo_root, manifest_path, owner):
        observed, _ = load_manifest(repo_root, manifest_path)
        transaction = {
            'repoRoot': repo_root,
            'expectedDigest': manifest_digest(observed) if observed is not None else None,
            'depth': 1,
        }
        transactions[identity] = transaction
        try:
            yield
        finally:
            transactions.pop(identity, None)


def require_locked_manifest_observation(repo_root, manifest_path, plan):
    current, _ = load_manifest(repo_root, manifest_path)
    if not current or manifest_digest(current) != plan.get('manifestDigestBefore'):
        raise SyncwheelError(
            'channel plan is stale under mutation lock; manifest changed before apply'
        )
    return current


def channel_mutation_checkpoint():
    """Fault-injection seam immediately before an authoritative channel mutation."""
    return None


def ledger_checkpoint_path(repo_root, manifest_path=None):
    return ledger_checkpoints_dir(repo_root, manifest_path) / 'latest.json'


def ledger_io_checkpoint(stage):
    """Fault-injection seam around physically durable ledger writes."""
    return None


def fsync_directory_path(directory):
    descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_LEDGER_FSYNC = os.fsync


def ledger_fsync_directory_path(directory):
    descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        _LEDGER_FSYNC(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory_durable(directory):
    directory = Path(directory)
    missing = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        ledger_fsync_directory_path(created.parent)
        ledger_fsync_directory_path(created)


@contextlib.contextmanager
def ledger_write_lock(repo_root, manifest_path=None):
    if fcntl is None:
        raise SyncwheelError('ledger writes require POSIX file locking support')
    root = ledger_root(repo_root, manifest_path)
    ensure_directory_durable(root)
    lock_path = root / 'ledger.lock'
    existed = lock_path.exists()
    with lock_path.open('a+b') as handle:
        if not existed:
            handle.flush()
            _LEDGER_FSYNC(handle.fileno())
            ledger_fsync_directory_path(root)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _repair_incomplete_ledger_tail(path):
    """Repair only the final unterminated JSONL frame.

    A complete JSON value without its delimiter is finished by appending the
    delimiter. An invalid unterminated suffix is the only data we truncate;
    newline-terminated corruption is never rewritten or hidden.
    """
    path = Path(path)
    if not path.exists():
        return 'unchanged'
    payload = path.read_bytes()
    if not payload or payload.endswith(b'\n'):
        return 'unchanged'
    boundary = payload.rfind(b'\n') + 1
    tail = payload[boundary:]
    try:
        parsed = json.loads(tail.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        with path.open('r+b') as handle:
            handle.truncate(boundary)
            handle.flush()
            _LEDGER_FSYNC(handle.fileno())
        ledger_fsync_directory_path(path.parent)
        return 'truncated-incomplete-tail'
    if not isinstance(parsed, dict):
        raise SyncwheelError(
            f'invalid unterminated ledger event in {path}: expected a JSON object'
        )
    with path.open('ab') as handle:
        handle.write(b'\n')
        handle.flush()
        _LEDGER_FSYNC(handle.fileno())
    ledger_fsync_directory_path(path.parent)
    return 'completed-delimiter'


def _recover_ledger_tail_unlocked(repo_root, manifest_path=None):
    directory = ledger_events_dir(repo_root, manifest_path)
    if not directory.exists():
        return 'unchanged'
    segments = sorted(directory.glob('*.jsonl'))
    if not segments:
        return 'unchanged'
    return _repair_incomplete_ledger_tail(segments[-1])


def recover_ledger_tail(repo_root, manifest_path=None):
    with ledger_write_lock(repo_root, manifest_path):
        return _recover_ledger_tail_unlocked(repo_root, manifest_path)


def manifest_stack_history_summary(stack):
    summary = {
        'id': stack['id'],
        'branch': stack['branch'],
        'base': stack['base'],
        'target_remote': stack['target_remote'],
        'target_branch': stack['target_branch'],
        'integration_branch': stack.get('integration_branch'),
        'state': stack.get('state', 'published'),
        'commits': list(stack['commits']),
        'integration_commits': stack_integration_commits(stack),
        'integration_only_commits': stack_integration_only_commits(stack),
        'meta': dict(stack.get('meta', {})),
    }
    if stack.get('depends_on'):
        summary['depends_on'] = list(stack['depends_on'])
    return summary


def manifest_channel_history_summary(channel):
    summary = {
        'id': channel['id'],
        'branch': channel['branch'],
        'base': channel['base'],
        'baseRevision': channel['baseRevision'],
        'remote': channel['remote'],
        'lifecycle': channel['lifecycle'],
        'composition': [
            {
                'stack': entry['stack'],
                'branch': entry['branch'],
                'stackBase': entry['stackBase'],
                'stackBaseRevision': entry['stackBaseRevision'],
                'branchRevision': entry['branchRevision'],
                'commits': list(entry['commits']),
                'dependsOn': list(entry.get('dependsOn', [])),
            }
            for entry in channel.get('composition', [])
        ],
    }
    if channel.get('expiry'):
        summary['expiry'] = dict(channel['expiry'])
    if channel.get('resolution'):
        summary['resolution'] = dict(channel['resolution'])
    return summary


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
        'channels': [
            manifest_channel_history_summary(channel)
            for channel in manifest.get('channels', [])
        ],
    }


def default_ledger_state():
    return {
        'schema_version': LEDGER_SCHEMA_VERSION,
        'last_seq': 0,
        'event_count': 0,
        'manifest': None,
        'integration': {},
        'stacks': {},
        'channels': {},
        'recent_events': [],
    }


def load_ledger_events(repo_root, manifest_path=None):
    directory = ledger_events_dir(repo_root, manifest_path)
    if not directory.exists():
        return []
    events = []
    for path in sorted(directory.glob('*.jsonl')):
        payload = path.read_bytes()
        if payload and not payload.endswith(b'\n'):
            raise SyncwheelError(
                f'incomplete ledger tail in {path}; recover it before reading'
            )
        for line_number, raw_line in enumerate(payload.split(b'\n')[:-1], start=1):
            if not raw_line.strip():
                continue
            try:
                data = json.loads(raw_line.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SyncwheelError(
                    f'invalid ledger event in {path}:{line_number}: {exc}'
                ) from exc
            if not isinstance(data, dict):
                raise SyncwheelError(
                    f'invalid ledger event in {path}:{line_number}: expected object'
                )
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
        active_channel_ids = set()
        channels = state.setdefault('channels', {})
        for summary in payload.get('channels') or []:
            channel = channels.setdefault(summary['id'], {'id': summary['id']})
            channel.update(summary)
            channel['active_in_manifest'] = True
            channel['last_manifest_seq'] = event['seq']
            active_channel_ids.add(summary['id'])
        for channel_id, channel in channels.items():
            if channel_id not in active_channel_ids:
                channel['active_in_manifest'] = False
        integration = payload.get('integration') or {}
        state['manifest'] = {
            'manifest_path': payload.get('manifest_path'),
            'manifest_hash': payload.get('manifest_hash'),
            'reason': payload.get('reason'),
            'integration': integration,
            'active_stacks': sorted(active_ids),
            'active_channels': sorted(active_channel_ids),
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

    if event['type'] in (
        'channel_operation_started', 'channel_operation_prepared',
        'channel_operation_receipt',
    ):
        channel_id = payload.get('channel')
        if channel_id:
            channels = state.setdefault('channels', {})
            channel = channels.setdefault(channel_id, {'id': channel_id})
            channel['last_operation_id'] = payload.get('operationId')
            channel['last_operation'] = payload.get('operation')
            channel['last_operation_seq'] = event['seq']
            channel['last_operation_status'] = (
                payload.get('status')
                if event['type'] == 'channel_operation_receipt' else 'pending'
            )
        return state

    if event['type'] in ('channel_applied', 'channel_published', 'channel_closed'):
        channel_id = payload.get('channel')
        if channel_id:
            channels = state.setdefault('channels', {})
            channel = channels.setdefault(channel_id, {'id': channel_id})
            channel.update({
                'branch': payload.get('branch') or channel.get('branch'),
                'last_event_type': event['type'],
                'last_event_seq': event['seq'],
                'last_plan_digest': payload.get('planDigest'),
                'last_tip': payload.get('tip') or channel.get('last_tip'),
                'last_remote_tip': payload.get('publishedRevision') or channel.get('last_remote_tip'),
            })
            if event['type'] == 'channel_closed':
                channel['active_in_manifest'] = False
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
    reduced = reduce_ledger_state(load_ledger_events(repo_root, manifest_path))
    path = ledger_checkpoint_path(repo_root, manifest_path)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data == reduced:
            return data
    return reduced


def _write_all(descriptor, payload):
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError('short ledger write')
        offset += written


def _write_ledger_checkpoint_unlocked(repo_root, state, manifest_path=None):
    path = ledger_checkpoint_path(repo_root, manifest_path)
    ensure_directory_durable(path.parent)
    encoded = (json.dumps(state, indent=2, sort_keys=True) + '\n').encode('utf-8')
    descriptor, temporary_name = tempfile.mkstemp(
        prefix='.latest.', suffix='.tmp', dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        midpoint = max(1, len(encoded) // 2)
        _write_all(descriptor, encoded[:midpoint])
        ledger_io_checkpoint('checkpoint_payload_half_written')
        _write_all(descriptor, encoded[midpoint:])
        ledger_io_checkpoint('checkpoint_payload_written')
        _LEDGER_FSYNC(descriptor)
        ledger_io_checkpoint('checkpoint_temp_fsynced')
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        ledger_fsync_directory_path(path.parent)
        ledger_io_checkpoint('checkpoint_replaced')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def write_ledger_checkpoint(repo_root, state, manifest_path=None):
    with ledger_write_lock(repo_root, manifest_path):
        _recover_ledger_tail_unlocked(repo_root, manifest_path)
        authoritative = reduce_ledger_state(
            load_ledger_events(repo_root, manifest_path)
        )
        # The append-only event log is authoritative if the caller raced and
        # supplied an older derived checkpoint state.
        _write_ledger_checkpoint_unlocked(repo_root, authoritative, manifest_path)


def next_ledger_segment_path(repo_root, manifest_path=None):
    directory = ledger_events_dir(repo_root, manifest_path)
    ensure_directory_durable(directory)
    segments = sorted(directory.glob('*.jsonl'))
    if not segments:
        return directory / '000001.jsonl'
    current = segments[-1]
    with current.open() as handle:
        line_count = sum(1 for _ in handle)
    if line_count < LEDGER_SEGMENT_MAX_EVENTS:
        return current
    next_index = int(current.stem) + 1
    return directory / f'{next_index:06d}.jsonl'


def append_ledger_event(repo_root, event_type, payload, manifest_path=None, idempotency_key=None):
    if not is_external_manifest_path(repo_root, manifest_path):
        tracking, worktree_root = manifest_policy_from_file(manifest_path or repo_root / '.syncwheel' / 'manifest.json')
        ensure_syncwheel_metadata_excluded(repo_root, tracking, worktree_root)
    with ledger_write_lock(repo_root, manifest_path):
        _recover_ledger_tail_unlocked(repo_root, manifest_path)
        events = load_ledger_events(repo_root, manifest_path)
        if idempotency_key is not None:
            payload = dict(payload)
            payload['idempotency_key'] = idempotency_key
            for existing in events:
                if (
                    existing.get('type') == event_type
                    and (existing.get('payload') or {}).get('idempotency_key') == idempotency_key
                ):
                    return existing
        current = reduce_ledger_state(events)
        event = {
            'schema_version': LEDGER_SCHEMA_VERSION,
            'seq': current['last_seq'] + 1,
            'ts': iso_utc_now(),
            'type': event_type,
            'payload': payload,
        }
        path = next_ledger_segment_path(repo_root, manifest_path)
        existed = path.exists()
        encoded = json.dumps(event, sort_keys=True).encode('utf-8')
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            midpoint = max(1, len(encoded) // 2)
            _write_all(descriptor, encoded[:midpoint])
            ledger_io_checkpoint('event_payload_half_written')
            _write_all(descriptor, encoded[midpoint:])
            ledger_io_checkpoint('event_payload_written')
            _write_all(descriptor, b'\n')
            ledger_io_checkpoint('event_record_written')
            _LEDGER_FSYNC(descriptor)
            if not existed:
                ledger_fsync_directory_path(path.parent)
            ledger_io_checkpoint('event_fsynced')
        finally:
            os.close(descriptor)
        state = reduce_ledger_state(load_ledger_events(repo_root, manifest_path))
        _write_ledger_checkpoint_unlocked(repo_root, state, manifest_path)
        return event


def save_manifest_with_ledger(repo_root, manifest_path, manifest, reason, context=None, event_type='manifest_saved'):
    if reason == 'stack_create' and context and context.get('operation_token'):
        stack = stack_map(manifest).get(context.get('stack'))
        if stack:
            require_current_stack_create_operation(
                repo_root,
                manifest_path,
                stack['id'],
                stack['branch'],
                ref_tip(repo_root, stack['branch']),
                context['operation_token'],
            )
    save_manifest(manifest_path, manifest)
    append_ledger_event(repo_root, event_type, manifest_event_payload(manifest_path, manifest, reason, context), manifest_path)


def acknowledge_in_place_manifest_replay(repo_root, manifest_path, replay_tip):
    """Accept only the manifest state written by a completed in-place replay."""
    transaction = active_manifest_write_transaction(manifest_path)
    if transaction is None or is_external_manifest_path(repo_root, manifest_path):
        return
    try:
        relative_path = Path(manifest_path).resolve(strict=False).relative_to(
            Path(repo_root).resolve()
        ).as_posix()
    except ValueError as exc:
        raise SyncwheelError(
            f'in-place replay cannot verify manifest outside repository: {manifest_path}'
        ) from exc
    expected_result = git(repo_root, 'show', f'{replay_tip}:{relative_path}', check=False)
    if expected_result.returncode == 0:
        try:
            expected_manifest = json.loads(expected_result.stdout)
        except json.JSONDecodeError as exc:
            raise SyncwheelError(
                f'in-place replay produced an invalid manifest at {replay_tip}:{relative_path}'
            ) from exc
        expected_digest = manifest_digest(expected_manifest)
    else:
        expected_digest = None
    observed_manifest, _ = load_manifest(repo_root, manifest_path)
    observed_normalized_digest = (
        manifest_digest(observed_manifest) if observed_manifest is not None else None
    )
    if observed_manifest is None:
        observed_digest = None
    else:
        try:
            observed_digest = manifest_digest(json.loads(Path(manifest_path).read_text()))
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncwheelError(
                f'in-place replay left an unreadable manifest: {manifest_path}'
            ) from exc
    preserved_untracked_digest = None
    if expected_digest is None and observed_normalized_digest == transaction['expectedDigest']:
        preserved_untracked_digest = observed_digest
    if observed_digest not in {expected_digest, preserved_untracked_digest}:
        raise SyncwheelError(
            'manifest changed unexpectedly during in-place replay; refusing stale write or side effect'
        )
    transaction['expectedDigest'] = observed_normalized_digest


def ref_tip(repo_root, ref):
    result = git(repo_root, 'rev-parse', ref, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def stack_map(manifest):
    return {stack['id']: stack for stack in manifest.get('stacks', [])}


def channel_ids_referencing_stack(manifest, stack_id):
    return sorted(
        channel['id'] for channel in manifest.get('channels', [])
        if any(
            entry['stack'] == stack_id
            for entry in channel.get('composition', [])
        )
    )


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


def channel_map(manifest):
    return {channel['id']: channel for channel in manifest.get('channels', [])}


def require_delivery_manifest(manifest):
    if manifest.get('repository_mode') != 'delivery':
        raise SyncwheelError('channel commands require repository_mode="delivery"')


def require_channel(manifest, channel_id):
    channels = channel_map(manifest)
    if channel_id not in channels:
        raise SyncwheelError(f'unknown channel: {channel_id}')
    return channels[channel_id]


def channel_pin_digest(channel):
    return canonical_json_digest({
        'base': channel['base'],
        'baseRevision': channel['baseRevision'],
        'composition': channel.get('composition', []),
    })


def channel_composition_digest(channel):
    return canonical_json_digest({
        'pinDigest': channel_pin_digest(channel),
        'resolution': channel.get('resolution'),
    })


def pin_stack_for_channel(repo_root, manifest, stack_id):
    stack = require_stack(manifest, stack_id)
    if not branch_exists(repo_root, stack['branch']):
        raise SyncwheelError(
            f"cannot pin stack {stack_id}: local branch {stack['branch']!r} is missing"
        )
    branch_revision = commit_full_sha(repo_root, stack['branch'])
    if not ref_exists(repo_root, stack['base']):
        raise SyncwheelError(f"cannot pin stack {stack_id}: base ref {stack['base']!r} is missing")
    stack_base_revision = commit_full_sha(repo_root, stack['base'])
    commits = [commit_full_sha(repo_root, commit) for commit in stack.get('commits', [])]
    observed_commits = [
        commit_full_sha(repo_root, commit)
        for commit in rev_list(repo_root, f'{stack_base_revision}..{branch_revision}')
    ]
    if observed_commits != commits:
        undeclared = [commit for commit in observed_commits if commit not in commits]
        missing = [commit for commit in commits if commit not in observed_commits]
        details = []
        if undeclared:
            details.append('undeclared=' + ','.join(undeclared))
        if missing:
            details.append('missing=' + ','.join(missing))
        raise SyncwheelError(
            f'cannot pin stack {stack_id}: branch range must exactly match declared ordered commits'
            + (f" ({'; '.join(details)})" if details else '')
        )
    return {
        'stack': stack_id,
        'branch': stack['branch'],
        'stackBase': stack['base'],
        'stackBaseRevision': stack_base_revision,
        'branchRevision': branch_revision,
        'commits': commits,
        'dependsOn': list(stack.get('depends_on', [])),
    }


def channel_entry_projection_errors(repo_root, manifest, entry):
    errors = []
    stack_id = entry['stack']
    if not commit_exists(repo_root, entry['stackBaseRevision']):
        errors.append(f'pinned stack base revision is missing: {entry["stackBaseRevision"]}')
        return errors
    if not commit_exists(repo_root, entry['branchRevision']):
        errors.append(f'pinned branch revision is missing: {entry["branchRevision"]}')
        return errors
    if not branch_contains(repo_root, entry['branchRevision'], entry['stackBaseRevision']):
        errors.append('pinned branch revision does not descend from pinned stack base revision')
        return errors
    for commit in entry['commits']:
        if not commit_exists(repo_root, commit):
            errors.append(f'pinned commit is missing: {commit}')
    if errors:
        return errors
    observed = [
        commit_full_sha(repo_root, commit)
        for commit in rev_list(
            repo_root, f"{entry['stackBaseRevision']}..{entry['branchRevision']}"
        )
    ]
    if observed != entry['commits']:
        errors.append('pinned branch range does not equal pinned ordered commits')
    stack = stack_map(manifest).get(stack_id)
    if stack:
        if entry['branch'] != stack['branch']:
            errors.append(
                f"pinned branch {entry['branch']!r} does not match stack branch {stack['branch']!r}"
            )
        if entry['stackBase'] != stack['base']:
            errors.append(
                f"pinned stackBase {entry['stackBase']!r} does not match stack base {stack['base']!r}"
            )
        if list(stack.get('depends_on', [])) != list(entry.get('dependsOn', [])):
            errors.append('current stack dependency declaration does not equal the pinned dependencies')
        current_branch_revision = ref_tip(repo_root, stack['branch'])
        if current_branch_revision == entry['branchRevision']:
            declared = [commit_full_sha(repo_root, commit) for commit in stack.get('commits', [])]
            if declared != entry['commits']:
                errors.append('current stack declaration does not equal the pinned ordered commits')
        elif current_branch_revision and not branch_contains(
            repo_root, current_branch_revision, entry['branchRevision']
        ):
            errors.append('pinned branch revision is not historical ancestry of the current stack branch')
    return errors


def require_channel_materialization_valid(repo_root, manifest, channel):
    validate_channel_dependency_order(channel)
    failures = []
    for entry in channel.get('composition', []):
        for error in channel_entry_projection_errors(repo_root, manifest, entry):
            failures.append(f"{entry['stack']}: {error}")
    resolution = channel.get('resolution')
    if resolution:
        revision = resolution['revision']
        if not commit_exists(repo_root, revision):
            failures.append(f'resolution revision is missing: {revision}')
        else:
            parents = git(repo_root, 'rev-list', '--parents', '-n', '1', revision).stdout.split()
            if len(parents) != 2 or parents[1] != channel['baseRevision']:
                failures.append('resolution revision must be single-parent directly on baseRevision')
            if ref_tree(repo_root, revision) != resolution['tree']:
                failures.append('resolution tree does not match its recorded revision')
        if resolution['parentRevision'] != channel['baseRevision']:
            failures.append('resolution parentRevision does not match channel baseRevision')
        if resolution['forPinDigest'] != channel_pin_digest(channel):
            failures.append('resolution is stale for the current pins')
    if failures:
        raise SyncwheelError(
            f"channel {channel['id']} has invalid pinned projection: " + '; '.join(failures)
        )


def channel_remote_observation(repo_root, channel):
    remote = channel['remote']
    ref = f"refs/heads/{channel['branch']}"
    if not remote_is_configured(repo_root, remote):
        return {'known': False, 'revision': None, 'error': f'remote not configured: {remote}'}
    result = git(repo_root, 'ls-remote', '--heads', remote, ref, check=False)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or 'remote observation failed'
        return {'known': False, 'revision': None, 'error': message[:400]}
    revision = None
    for line in result.stdout.splitlines():
        sha, separator, observed_ref = line.partition('\t')
        if separator and observed_ref == ref:
            revision = sha.strip()
            break
    return {'known': True, 'revision': revision, 'error': None}


def observe_new_channel_ref(repo_root, channel):
    local_revision = ref_tip(repo_root, f"refs/heads/{channel['branch']}")
    remote = channel_remote_observation(repo_root, channel)
    return {
        'localRevision': local_revision,
        'remoteKnown': remote['known'],
        'remoteRevision': remote['revision'],
    }


def require_new_channel_ref_unowned(repo_root, channel, phase='create'):
    observation = observe_new_channel_ref(repo_root, channel)
    if observation['localRevision']:
        raise SyncwheelError(
            f"channel {phase} refuses existing unowned local branch: {channel['branch']}"
        )
    if not observation['remoteKnown']:
        raise SyncwheelError(
            f"channel {phase} cannot prove remote branch absence on {channel['remote']!r}; "
            'remote observation is unknown'
        )
    if observation['remoteRevision']:
        raise SyncwheelError(
            f"channel {phase} refuses existing unowned remote branch: "
            f"{channel['remote']}/{channel['branch']}"
        )
    return observation


def channel_observation(repo_root, manifest, channel, include_remote=True):
    current_base_revision = ref_tip(repo_root, channel['base'])
    current_revision = ref_tip(repo_root, channel['branch'])
    remote = (
        channel_remote_observation(repo_root, channel)
        if include_remote else {'known': None, 'revision': None, 'error': None}
    )
    stack_revisions = {}
    for entry in channel.get('composition', []):
        stack_revisions[entry['stack']] = ref_tip(repo_root, entry['branch'])
    body = {
        'manifestDigest': manifest_digest(manifest),
        'pinDigest': channel_pin_digest(channel),
        'channelDigest': channel_composition_digest(channel),
        'baseRevision': channel['baseRevision'],
        'currentBaseRevision': current_base_revision,
        'currentRevision': current_revision,
        'remoteKnown': remote['known'],
        'remoteRevision': remote['revision'],
        'stackRevisions': stack_revisions,
    }
    return {
        **body,
        'remoteError': remote['error'],
        'observationRevision': canonical_json_digest(body),
    }


def normalize_channel_operation_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value):
        raise SyncwheelError(
            '--operation-id must be 1-128 ref-safe letters, digits, dot, underscore, colon, or dash'
        )
    return value


def channel_operation_id_from_args(args):
    value = getattr(args, 'operation_id', None)
    return value if isinstance(value, str) else None


def return_existing_channel_operation(args):
    operation_id = channel_operation_id_from_args(args)
    plan_digest = getattr(args, 'plan_digest', None)
    if not getattr(args, 'apply', False) or not operation_id or not plan_digest:
        return False
    repo_root = resolve_repo_root(args.repo)
    _, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    events = channel_operation_events(repo_root, manifest_path, operation_id)
    receipts = [
        event.get('payload') or {} for event in events
        if event.get('type') == 'channel_operation_receipt'
    ]
    if not receipts:
        return False
    receipt = receipts[-1]
    if receipt.get('planDigest') != plan_digest:
        raise SyncwheelError(
            f'channel operation id collision: {operation_id} is bound to another plan'
        )
    if receipt.get('status') == 'unknown':
        raise SyncwheelError(
            f'channel operation {operation_id} has unknown outcome; use channel operation reconcile'
        )
    print(json.dumps(receipt, indent=2))
    return True


def finalize_channel_plan(plan, operation_id=None):
    requested = normalize_channel_operation_id(operation_id)
    plan['planDigest'] = canonical_json_digest(plan)
    if requested is None:
        requested = 'chop-' + hashlib.sha256(
            f"channel-operation:{plan['planDigest']}".encode('utf-8')
        ).hexdigest()[:24]
    plan['operationId'] = requested
    return plan


def build_channel_plan(repo_root, manifest, channel, operation='apply', operation_id=None):
    if operation not in {'apply', 'publish'}:
        raise SyncwheelError(f'unsupported channel plan operation: {operation}')
    require_channel_materialization_valid(repo_root, manifest, channel)
    observation = channel_observation(repo_root, manifest, channel, include_remote=True)
    if not observation['baseRevision']:
        raise SyncwheelError(f"channel {channel['id']} pinned base revision is missing")
    if not commit_exists(repo_root, observation['baseRevision']):
        raise SyncwheelError(
            f"channel {channel['id']} pinned base revision is unavailable: "
            f"{observation['baseRevision']}"
        )
    if operation == 'publish' and channel['lifecycle'] == 'shared':
        stacks = stack_map(manifest)
        drafts = [
            entry['stack'] for entry in channel.get('composition', [])
            if entry['stack'] in stacks and stacks[entry['stack']].get('state') == 'draft'
        ]
        if drafts:
            raise SyncwheelError(
                'shared channel publication refuses draft stack(s): ' + ', '.join(drafts)
            )
    coordination_active = operation == 'publish' and coordination_is_active(manifest)
    coordination_state_revision = None
    coordination_manifest_revision = None
    coordination_target = None
    if coordination_active:
        config = coordination_config(manifest)
        coordination_target = coordination_state_ref(config)
        coordination_state_revision = remote_ref_tips(
            repo_root, config['remote'], [coordination_target]
        )[coordination_target]
        coordination_manifest_revision = coordination_manifest_digest(manifest, repo_root)
    actions = []
    if operation == 'apply':
        actions.append({
            'id': 'materialize-local-channel-ref',
            'type': 'materialize-local-channel',
            'target': f"refs/heads/{channel['branch']}",
            'before': observation['currentRevision'],
            'intendedAfter': {
                'pinDigest': observation['pinDigest'],
                'compositionDigest': observation['channelDigest'],
            },
        })
    else:
        actions.append({
            'id': 'publish-channel-ref',
            'type': 'publish-channel-ref',
            'target': f"{channel['remote']}:refs/heads/{channel['branch']}",
            'before': observation['remoteRevision'],
            'intendedAfter': observation['currentRevision'],
            **({'atomicGroup': 'coordinated-publication'} if coordination_active else {}),
        })
        if coordination_active:
            actions.append({
                'id': 'publish-coordination-state',
                'type': 'publish-coordination-state',
                'target': f"{config['remote']}:{coordination_target}",
                'before': coordination_state_revision,
                'intendedAfter': {
                    'manifestDigest': coordination_manifest_revision,
                },
                'atomicGroup': 'coordinated-publication',
            })
    plan = {
        'kind': 'channelPlan',
        'schemaVersion': CHANNEL_PLAN_SCHEMA_VERSION,
        'operation': operation,
        'request': {
            'operation': operation,
            'channel': channel['id'],
        },
        'channel': channel['id'],
        'branch': channel['branch'],
        'lifecycle': channel['lifecycle'],
        'expiry': channel.get('expiry'),
        'observationRevision': observation['observationRevision'],
        'manifestDigest': observation['manifestDigest'],
        'manifestDigestBefore': observation['manifestDigest'],
        'compositionDigest': observation['channelDigest'],
        'pinDigest': observation['pinDigest'],
        'base': channel['base'],
        'baseRevision': observation['baseRevision'],
        'currentBaseRevision': observation['currentBaseRevision'],
        'baseDrifted': observation['currentBaseRevision'] != observation['baseRevision'],
        'currentRevision': observation['currentRevision'],
        'remote': channel['remote'],
        'remoteObservationKnown': observation['remoteKnown'],
        'remoteRevision': observation['remoteRevision'],
        'coordinationStateRevision': coordination_state_revision,
        'coordinationManifestDigest': coordination_manifest_revision,
        'composition': json.loads(json.dumps(channel.get('composition', []))),
        'actions': actions,
        'before': {
            'manifestDigest': observation['manifestDigest'],
            'channel': manifest_channel_history_summary(channel),
            'localRevision': observation['currentRevision'],
            'remoteRevision': observation['remoteRevision'],
        },
        'after': {
            'manifestDigest': observation['manifestDigest'],
            'channel': manifest_channel_history_summary(channel),
            'localRevision': (
                '<materialized-from-composition>'
                if operation == 'apply' else observation['currentRevision']
            ),
            'remoteRevision': (
                observation['currentRevision']
                if operation == 'publish' else observation['remoteRevision']
            ),
        },
        'deployment': {
            'asserted': False,
            'note': 'A published channel branch is not proof of an external deployment.',
        },
    }
    return finalize_channel_plan(plan, operation_id)


def verify_channel_plan_digest(
    repo_root, manifest, channel, operation, expected_digest, operation_id=None
):
    if not isinstance(expected_digest, str) or not expected_digest:
        raise SyncwheelError('--plan-digest is required with --apply')
    plan = build_channel_plan(repo_root, manifest, channel, operation, operation_id)
    if plan['planDigest'] != expected_digest:
        raise SyncwheelError(
            'channel plan is stale: manifest, base, composition, local branch, stack branch, '
            'or remote observation changed; generate a new plan'
        )
    return plan


def latest_channel_event(repo_root, manifest_path, channel_id, event_type):
    for event in reversed(load_ledger_events(repo_root, manifest_path)):
        if event.get('type') != event_type:
            continue
        payload = event.get('payload') or {}
        if payload.get('channel') == channel_id:
            return payload
    return None


def materialize_channel_tip(repo_root, channel, plan):
    if find_worktree_for_branch(repo_root, channel['branch']):
        raise SyncwheelError(
            f"channel branch {channel['branch']!r} is checked out; close its worktree before apply"
        )
    resolution = channel.get('resolution')
    if resolution:
        return resolution['revision']
    temporary_branch = f"syncwheel/channel-build/{plan['planDigest'][:24]}"
    temporary_ref = f'refs/heads/{temporary_branch}'
    if ref_tip(repo_root, temporary_ref):
        raise SyncwheelError(f'temporary channel build ref already exists: {temporary_ref}')
    commits = [
        commit
        for entry in channel.get('composition', [])
        for commit in entry.get('commits', [])
    ]
    synthetic = {
        'id': f"channel-{channel['id']}",
        'branch': temporary_branch,
        'base': channel['baseRevision'],
        'commits': commits,
    }
    mode = 'plumbing' if git_supports_write_tree(repo_root) else 'ephemeral'
    replay = replay_plan(repo_root, None, replay_target(stack=synthetic), mode)
    try:
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink):
            result = execute_replay(repo_root, replay, True)
        require_replay_success(result)
        tip = result['after_tip']
        if not tip:
            raise SyncwheelError('channel materialization did not produce a commit')
        return tip
    finally:
        git(repo_root, 'update-ref', '-d', temporary_ref, check=False)


def channel_receipt(channel, plan, status, tip=None, published_revision=None, coordination_state=None):
    return {
        'schemaVersion': CHANNEL_PLAN_SCHEMA_VERSION,
        'channel': channel['id'],
        'branch': channel['branch'],
        'operation': plan['operation'],
        'status': status,
        'operationId': plan['operationId'],
        'planDigest': plan['planDigest'],
        'observationRevision': plan['observationRevision'],
        'compositionDigest': plan['compositionDigest'],
        'tip': tip,
        'publishedRevision': published_revision,
        'coordinationState': coordination_state,
        'deploymentAsserted': False,
        'recordedAt': iso_utc_now(),
    }


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


def manifest_remedy_stack_ids(manifest, stack_id=None):
    """Return deterministic, manifest-declared stack destinations for recovery guidance."""
    stacks = {
        stack['id']: stack for stack in manifest.get('stacks', [])
        if isinstance(stack, dict) and isinstance(stack.get('id'), str)
    }
    if stack_id:
        return [stack_id] if stack_id in stacks else []
    ordered = [stack_id for stack_id in manifest.get('integration', {}).get('stacks', []) if stack_id in stacks]
    return list(dict.fromkeys([*ordered, *sorted(stacks)]))


def primary_checkout_remedy_commands(manifest, stack_id=None):
    """Name capture and queue commands without guessing ownership of primary changes."""
    stack_ids = manifest_remedy_stack_ids(manifest, stack_id)
    if not stack_ids:
        return [
            'syncwheel stack create --draft <stack-id> --purpose "Capture primary checkout work"',
            'syncwheel stack capture-integration <stack-id> HEAD',
        ]
    commands = []
    for identifier in stack_ids:
        quoted_stack = shlex.quote(identifier)
        commands.extend([
            f'syncwheel stack capture-integration {quoted_stack} HEAD',
            f'syncwheel worktree open <lane> --into {quoted_stack}',
        ])
    return commands


def governed_lane_queue_commands(manifest, lanes):
    """Return manifest-backed queue commands for retained governed lane commits."""
    commands = []
    for lane in lanes:
        branch = lane.get('branch')
        base = lane.get('base')
        if not isinstance(branch, str) or not branch or not isinstance(base, str) or not base:
            continue
        targets = manifest_remedy_stack_ids(manifest, lane.get('target'))
        for target in targets:
            commands.append(
                f'syncwheel stack add {shlex.quote(target)} '
                f'{shlex.quote(base)}..{shlex.quote(branch)}'
            )
    return list(dict.fromkeys(commands))


def governed_lane_remedy(manifest, lane, *, commit_first=False):
    commands = governed_lane_queue_commands(manifest, [lane])
    if not commands:
        commands = primary_checkout_remedy_commands(manifest)
    prefix = 'commit the lane changes, then ' if commit_first else ''
    return prefix + 'Use: ' + '; '.join(commands)


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


def governed_worktree_registry_path(repo_root):
    return git_common_dir(repo_root) / 'syncwheel' / 'governed-worktrees.json'


def governed_worktree_lock_path(repo_root):
    return git_common_dir(repo_root) / 'syncwheel' / 'governed-worktrees.lock'


def governed_worktree_owner():
    explicit_owner = os.environ.get('SYNCWHEEL_LANE_OWNER')
    if explicit_owner:
        return explicit_owner
    owner_pid = os.getppid()
    if owner_pid <= 1:
        return f'unknown@{socket.gethostname()}:0'
    return (
        f'{os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"}'
        f'@{socket.gethostname()}:{owner_pid}'
    )


def governed_worktree_process_start_time(pid):
    """Return a stable process-start identity when the host exposes one."""
    try:
        raw = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
    except (OSError, ValueError):
        raw = None
    if raw is not None:
        closing_paren = raw.rfind(')')
        fields = raw[closing_paren + 2:].split() if closing_paren >= 0 else []
        # The suffix starts at proc(5) field 3; starttime is field 22.
        if len(fields) > 19:
            return f'proc:{fields[19]}'
    try:
        observed = subprocess.run(
            ['ps', '-o', 'lstart=', '-p', str(pid)],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    started = observed.stdout.strip()
    return f'ps:{started}' if observed.returncode == 0 and started else None


def governed_worktree_process_state(pid):
    """Return the kernel process state, including Z for an unreaped zombie."""
    try:
        raw = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
    except (OSError, ValueError):
        raw = None
    if raw is not None:
        closing_paren = raw.rfind(')')
        fields = raw[closing_paren + 2:].split() if closing_paren >= 0 else []
        if fields:
            return fields[0]
    try:
        observed = subprocess.run(
            ['ps', '-o', 'stat=', '-p', str(pid)],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    state = observed.stdout.strip()
    return state[0] if observed.returncode == 0 and state else None


def governed_worktree_lock_metadata(pid=None, token=None):
    pid = os.getpid() if pid is None else pid
    return {
        'pid': pid,
        'process_start_time': governed_worktree_process_start_time(pid),
        'token': token or uuid.uuid4().hex,
        'acquired_at': iso_utc_now(),
    }


def parse_governed_worktree_lock_metadata(payload):
    try:
        metadata = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        metadata = None
    if isinstance(metadata, dict):
        return metadata
    # Read the pre-0.40.2 lock format conservatively so a dead legacy owner can
    # still be recovered without stealing a live process's lock.
    try:
        raw_pid, acquired_at = payload.decode('utf-8').strip().split(maxsplit=1)
        return {
            'pid': int(raw_pid),
            'process_start_time': None,
            'token': None,
            'acquired_at': acquired_at,
        }
    except (UnicodeDecodeError, ValueError):
        return {}


def governed_worktree_stale_lock_reason(metadata, *, lock_available=False, lock_age=None):
    try:
        pid = int(metadata.get('pid'))
    except (AttributeError, TypeError, ValueError):
        if (
            lock_available
            and lock_age is not None
            and lock_age >= GOVERNED_WORKTREE_LOCK_INCOMPLETE_GRACE_SECONDS
        ):
            return 'incomplete_metadata'
        return None
    if pid <= 0:
        if (
            lock_available
            and lock_age is not None
            and lock_age >= GOVERNED_WORKTREE_LOCK_INCOMPLETE_GRACE_SECONDS
        ):
            return 'incomplete_metadata'
        return None
    recorded_start = metadata.get('process_start_time')
    current_start = governed_worktree_process_start_time(pid)
    if recorded_start is not None and current_start is not None and str(recorded_start) != current_start:
        return 'process_start_time_mismatch'
    if governed_worktree_process_state(pid) == 'Z':
        return 'process_zombie'
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 'pid_not_alive'
    except (PermissionError, OSError, OverflowError):
        return None
    return None


def log_governed_worktree_lock_recovery(lock_path, stale_path, metadata, reason, token):
    log_path = lock_path.with_name('governed-worktrees-lock-recovery.jsonl')
    existed = log_path.exists()
    entry = {
        'recovered_at': iso_utc_now(),
        'reason': reason,
        'recovery_token': token,
        'stale_lock': str(stale_path),
        'stale_owner': metadata,
    }
    descriptor = os.open(str(log_path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        _write_all(descriptor, json.dumps(entry, sort_keys=True).encode('utf-8') + b'\n')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if not existed:
        fsync_directory_path(log_path.parent)
    if reason == 'incomplete_metadata':
        # Nothing here proves the creator died; it may be alive and descheduled.
        message = (
            'recovered an uninitialized governed worktree registry lock '
            f'({reason}); its creator, if still running, retries'
        )
    else:
        message = (
            'recovered stale governed worktree registry lock from pid '
            f"{metadata.get('pid', 'unknown')} ({reason})"
        )
    print(f'WARNING: {message}; retained {stale_path.name}', file=sys.stderr)


def governed_worktree_lock_descriptor_matches_path(lock_path, descriptor):
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = lock_path.stat()
    except (FileNotFoundError, OSError):
        return False
    return (
        descriptor_stat.st_ino == path_stat.st_ino
        and descriptor_stat.st_dev == path_stat.st_dev
    )


@contextlib.contextmanager
def governed_worktree_registry_lock(repo_root):
    """Serialize clone-local lane state and recover a provably dead owner."""
    if fcntl is None:
        raise SyncwheelError(
            'crash-safe governed worktree registry locking requires POSIX file locking support'
        )
    lock_path = governed_worktree_lock_path(repo_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + GOVERNED_WORKTREE_LOCK_TIMEOUT_SECONDS
    descriptor = None
    metadata = governed_worktree_lock_metadata()
    encoded_metadata = (json.dumps(metadata, sort_keys=True) + '\n').encode('utf-8')
    while descriptor is None:
        try:
            candidate = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            try:
                fcntl.flock(candidate, fcntl.LOCK_EX)
                _write_all(candidate, encoded_metadata)
                os.fsync(candidate)
                fsync_directory_path(lock_path.parent)
                # A contender may recover an empty file if this creator was
                # descheduled beyond the initialization grace before flock.
                # Never enter the critical section through the renamed inode.
                if not governed_worktree_lock_descriptor_matches_path(lock_path, candidate):
                    os.close(candidate)
                    continue
            except BaseException:
                owns_path = governed_worktree_lock_descriptor_matches_path(lock_path, candidate)
                os.close(candidate)
                if owns_path:
                    try:
                        lock_path.unlink()
                        fsync_directory_path(lock_path.parent)
                    except FileNotFoundError:
                        pass
                raise
            descriptor = candidate
        except FileExistsError:
            try:
                candidate = os.open(str(lock_path), os.O_RDWR)
            except FileNotFoundError:
                continue
            acquired_candidate = False
            try:
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired_candidate = True
                except (BlockingIOError, OSError) as exc:
                    if not isinstance(exc, BlockingIOError) and exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                if acquired_candidate:
                    candidate_stat = os.fstat(candidate)
                    try:
                        path_stat = lock_path.stat()
                    except FileNotFoundError:
                        continue
                    if candidate_stat.st_ino != path_stat.st_ino or candidate_stat.st_dev != path_stat.st_dev:
                        continue
                    os.lseek(candidate, 0, os.SEEK_SET)
                    stale_metadata = parse_governed_worktree_lock_metadata(os.read(candidate, 65536))
                    lock_age = max(0.0, time.time() - candidate_stat.st_mtime)
                    stale_reason = governed_worktree_stale_lock_reason(
                        stale_metadata,
                        lock_available=True,
                        lock_age=lock_age,
                    )
                    if stale_reason:
                        stale_token = uuid.uuid4().hex
                        stale_path = lock_path.with_name(
                            f'{lock_path.name}.stale-{syncwheel_timestamp()}-{stale_token[:12]}'
                        )
                        # The flock serializes conforming contenders on this inode;
                        # a waiter that opened it before this rename verifies the
                        # path inode again and retries rather than moving a new lock.
                        os.replace(lock_path, stale_path)
                        fsync_directory_path(lock_path.parent)
                        log_governed_worktree_lock_recovery(
                            lock_path,
                            stale_path,
                            stale_metadata,
                            stale_reason,
                            stale_token,
                        )
                        continue
            finally:
                if acquired_candidate:
                    fcntl.flock(candidate, fcntl.LOCK_UN)
                os.close(candidate)
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                continue
            if time.monotonic() >= deadline:
                stale = (
                    ' (the lock metadata could not prove that its owner died; inspect it manually)'
                    if age > GOVERNED_WORKTREE_LOCK_STALE_SECONDS else ''
                )
                raise SyncwheelError(
                    'governed worktree registry is busy; retry after the other local operation finishes' + stale
                )
            time.sleep(0.05)
    try:
        yield lock_path
    finally:
        try:
            if governed_worktree_lock_descriptor_matches_path(lock_path, descriptor):
                lock_path.unlink()
                fsync_directory_path(lock_path.parent)
        except FileNotFoundError:
            pass
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def prune_governed_worktree_stale_locks(repo_root, manifest_path=None):
    """Drop retained stale lock inodes nobody holds; the recovery log keeps the evidence."""
    if fcntl is None:
        return []
    lock_path = governed_worktree_lock_path(repo_root)
    directory = lock_path.parent
    pruned = []
    for candidate in sorted(directory.glob(f'{lock_path.name}.stale-*')):
        try:
            descriptor = os.open(str(candidate), os.O_RDWR)
        except OSError:
            continue
        held = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            held = True
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            pruned.append(candidate.name)
        finally:
            if held:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
    if pruned:
        fsync_directory_path(directory)
        append_ledger_event(
            repo_root,
            'governed_worktree_stale_locks_pruned',
            {'files': pruned, 'count': len(pruned)},
            manifest_path,
        )
    return pruned


def load_governed_worktree_registry(repo_root):
    path = governed_worktree_registry_path(repo_root)
    data = load_json_file(path, {'version': GOVERNED_WORKTREE_REGISTRY_VERSION, 'lanes': []})
    if data.get('version') != GOVERNED_WORKTREE_REGISTRY_VERSION:
        raise SyncwheelError(f'unsupported governed worktree registry version: {path}')
    lanes = data.get('lanes')
    if not isinstance(lanes, list):
        raise SyncwheelError(f'governed worktree registry lanes must be a list: {path}')
    identifiers = set()
    for lane in lanes:
        if not isinstance(lane, dict):
            raise SyncwheelError(f'governed worktree registry lane must be an object: {path}')
        lane_id = lane.get('id')
        if not isinstance(lane_id, str) or not lane_id or lane_id in identifiers:
            raise SyncwheelError(f'governed worktree registry has an invalid or duplicate lane id: {path}')
        identifiers.add(lane_id)
        for key in ('owner', 'path', 'base', 'branch', 'state', 'created_at', 'lease_expires_at'):
            if not isinstance(lane.get(key), str) or not lane[key]:
                raise SyncwheelError(f'governed worktree lane {lane_id!r} is missing {key!r}: {path}')
        if not isinstance(lane.get('full'), bool):
            raise SyncwheelError(f'governed worktree lane {lane_id!r} has invalid full mode: {path}')
    return data, path


def governed_worktree_registry_payload(registry):
    return (json.dumps(registry, indent=2, sort_keys=True) + '\n').encode('utf-8')


def governed_worktree_registry_file_digest(path):
    path = Path(path)
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(payload).hexdigest()


def governed_worktree_registry_io_checkpoint(stage):
    """Fault-injection seam around the registry's durable CAS write."""
    return None


def save_governed_worktree_registry(
    repo_root,
    registry,
    expected_digest=_REGISTRY_EXPECTED_DIGEST_UNSET,
):
    path = governed_worktree_registry_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = governed_worktree_registry_payload(registry)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    replaced = False
    try:
        _write_all(descriptor, payload)
        governed_worktree_registry_io_checkpoint('registry_temp_written')
        os.fsync(descriptor)
        governed_worktree_registry_io_checkpoint('registry_temp_fsynced')
        os.close(descriptor)
        descriptor = None
        if expected_digest is not _REGISTRY_EXPECTED_DIGEST_UNSET:
            current_digest = governed_worktree_registry_file_digest(path)
            if current_digest != expected_digest:
                raise SyncwheelError(
                    'governed worktree registry changed after its decision snapshot; '
                    'refusing a stale local-state write'
                )
        governed_worktree_registry_io_checkpoint('registry_preimage_verified')
        os.replace(temporary, path)
        replaced = True
        fsync_directory_path(path.parent)
        governed_worktree_registry_io_checkpoint('registry_directory_fsynced')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not replaced:
            temporary.unlink(missing_ok=True)
    return path


def governed_worktree_registry_cas_persister(repo_root, registry):
    path = governed_worktree_registry_path(repo_root)
    state = {'expected_digest': governed_worktree_registry_file_digest(path)}

    def persist():
        save_governed_worktree_registry(
            repo_root,
            registry,
            expected_digest=state['expected_digest'],
        )
        state['expected_digest'] = hashlib.sha256(
            governed_worktree_registry_payload(registry)
        ).hexdigest()

    return persist


def governed_worktree_root(repo_root, manifest):
    worktrees = get_worktrees(repo_root)
    primary = Path(worktrees[0]['path']).resolve() if worktrees else Path(repo_root).resolve()
    return resolve_worktree_root_path(primary, effective_worktree_root(manifest)).resolve()


def governed_worktree_owner_is_dead(owner):
    """Return true only for a dead PID that belongs to this host.

    Owners are advisory identifiers, so an unparseable or remote owner must never
    make a lane eligible for deletion.  A missing local process is enough to reap
    a missing lane because there is no worktree left to protect.
    """
    if not isinstance(owner, str):
        return False
    owner_and_host, separator, raw_pid = owner.rpartition(':')
    user, at, hostname = owner_and_host.rpartition('@')
    if not separator or not at or not user or hostname != socket.gethostname():
        return False
    try:
        pid = int(raw_pid)
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError, OverflowError):
        return False
    return False


def governed_worktree_lane_lease_expired(lane, now):
    expires = parse_coordination_timestamp(lane['lease_expires_at'])
    return bool(expires and expires <= now)


def governed_worktree_pending_remedy(manifest, lane):
    if lane.get('cleanup_event_type') == 'governed_worktree_released':
        reason = shlex.quote(lane.get('cleanup_event_reason') or 'retry requested release')
        return (
            f"retry with: syncwheel worktree release {shlex.quote(lane['id'])} "
            f'--reason {reason} --apply'
        )
    return 'retry with: syncwheel gc --apply; ' + governed_lane_remedy(manifest, lane)


def governed_worktree_lane_status(repo_root, manifest, lane, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    root = governed_worktree_root(repo_root, manifest)
    registered_path = Path(lane['path']).resolve(strict=False)
    worktree = find_worktree_record_for_branch(repo_root, lane['branch'])
    worktree_path = (
        Path(worktree['path']).resolve(strict=False)
        if worktree and worktree.get('path') else None
    )
    path_moved = bool(
        not registered_path.exists()
        and worktree_path is not None
        and worktree_path != registered_path
    )
    path = worktree_path if path_moved else registered_path
    status = {
        'id': lane['id'], 'state': lane['state'], 'path': str(path),
        'branch': lane['branch'], 'owner': lane['owner'], 'full': lane['full'],
        'target': lane.get('target'), 'lease_expires_at': lane['lease_expires_at'],
        'code': None, 'remedy': None,
    }
    if path_moved:
        status['registered_path'] = str(registered_path)
        status['path_moved'] = True
    if lane['state'] == 'reaped':
        if lane.get('pending_reason') == 'ledger_pending':
            status.update(
                code='ledger_pending',
                remedy=governed_worktree_pending_remedy(manifest, lane),
            )
        return status
    if lane['state'] == 'captured_pending_cleanup':
        status.update(
            code=lane.get('pending_reason') or 'captured_pending_cleanup',
            remedy=governed_worktree_pending_remedy(manifest, lane),
        )
        return status
    if (
        worktree
        and worktree.get('locked')
        and worktree.get('locked') != governed_worktree_cleanup_lock_reason(lane)
    ):
        status.update(
            code='locked',
            remedy='unlock the Git worktree before retrying cleanup',
        )
        return status
    if path.exists():
        if path_moved:
            dirty = local_worktree_status(path)
            if dirty is None:
                status.update(code='unavailable', remedy='restore the worktree before attempting cleanup')
                return status
            if dirty:
                status.update(code='dirty', remedy=governed_lane_remedy(manifest, lane, commit_first=True))
                return status
        if not path_is_relative_to(path, root):
            status.update(code='outside_root', remedy='inspect and recover this lane; Syncwheel will not move or remove it')
            return status
        if worktree_path is None or worktree_path != path:
            status.update(code='unregistered_worktree', remedy='inspect the path and branch; Syncwheel will not remove an unregistered worktree')
            return status
        dirty = local_worktree_status(path)
        if dirty is None:
            status.update(code='unavailable', remedy='restore the worktree before attempting cleanup')
            return status
        if dirty:
            status.update(code='dirty', remedy=governed_lane_remedy(manifest, lane, commit_first=True))
            return status
        if governed_worktree_lane_lease_expired(lane, now):
            status.update(code='expired', remedy=governed_lane_remedy(manifest, lane))
            return status
    elif governed_worktree_lane_lease_expired(lane, now) or governed_worktree_owner_is_dead(lane['owner']):
        # An expired or dead owner cannot keep a missing path alive merely
        # because an old root configuration no longer contains that path.
        status.update(code='expired', remedy=governed_lane_remedy(manifest, lane))
        return status
    elif not path_is_relative_to(path, root):
        status.update(code='outside_root', remedy='inspect and recover this lane; Syncwheel will not move or remove it')
        return status
    elif worktree_path is None or worktree_path != path:
        status.update(code='unregistered_worktree', remedy='inspect the path and branch; Syncwheel will not remove an unregistered worktree')
        return status
    expires = parse_coordination_timestamp(lane['lease_expires_at'])
    if expires is None:
        status.update(code='invalid_lease', remedy='repair the local registry before any cleanup')
    elif governed_worktree_lane_lease_expired(lane, now):
        status.update(code='expired', remedy=governed_lane_remedy(manifest, lane))
    return status


def governed_worktree_diagnostics(repo_root, manifest):
    registry, path = load_governed_worktree_registry(repo_root)
    root = governed_worktree_root(repo_root, manifest)
    diagnostics = [governed_worktree_lane_status(repo_root, manifest, lane) for lane in registry['lanes']]
    known_paths = {item['path'] for item in diagnostics if item.get('id')}
    for worktree in get_worktrees(repo_root):
        worktree_path = Path(worktree['path']).resolve(strict=False)
        if worktree_path == Path(repo_root).resolve(strict=False) or not path_is_relative_to(worktree_path, root):
            continue
        if worktree.get('branch', '').startswith('syncwheel/lane/') and str(worktree_path) not in known_paths:
            diagnostics.append({
                'id': None, 'state': 'unknown', 'path': str(worktree_path),
                'branch': worktree.get('branch'), 'owner': None, 'full': None,
                'target': None, 'lease_expires_at': None,
                'code': 'unregistered_worktree',
                'remedy': 'inspect it manually; Syncwheel will not remove an unregistered worktree',
            })
    return {'registry_path': str(path), 'lanes': diagnostics}


def governed_worktree_warning_lines(repo_root, manifest):
    lines = []
    for lane in governed_worktree_diagnostics(repo_root, manifest)['lanes']:
        if not lane['code']:
            continue
        label = lane['id'] or lane['branch'] or lane['path']
        lines.append(
            f"governed worktree {label}: {lane['code']}; {lane['remedy']}"
        )
    return lines


def emit_governed_worktree_warnings(repo_root, manifest, json_mode=False):
    lines = governed_worktree_warning_lines(repo_root, manifest)
    if not lines or json_mode or not sys.stderr.isatty():
        return lines
    color = '' if os.environ.get('NO_COLOR') else YELLOW
    reset = '' if not color else RESET
    for line in lines:
        print(f'{color}WARNING: {line}{reset}', file=sys.stderr)
    return lines


def governed_worktree_recovery_ref(lane):
    return f'refs/syncwheel/recovery/lanes/{safe_ref_segment(lane["id"])}-{syncwheel_timestamp()}'


def governed_worktree_cleanup_lock_reason(lane):
    material = f"{lane['id']}\0{lane['created_at']}".encode('utf-8')
    token = hashlib.sha256(material).hexdigest()[:24]
    return f"syncwheel-cleanup:{safe_ref_segment(lane['id'])}:{token}"


def governed_worktree_generation_token(lane):
    token = lane.get('generation_token')
    if isinstance(token, str) and token:
        return token
    material = f"{lane['id']}\0{lane['created_at']}\0{lane['branch']}".encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def governed_worktree_cleanup_tip(repo_root, lane):
    return (
        lane.get('cleanup_tip')
        or lane.get('branch_delete_tip')
        or ref_tip(repo_root, lane['branch'])
        or (ref_tip(repo_root, lane['recovery_ref']) if lane.get('recovery_ref') else None)
    )


def governed_worktree_cleanup_key(lane):
    material = {
        'lane': lane['id'],
        'generation_token': governed_worktree_generation_token(lane),
        'operation_token': lane.get('cleanup_operation_token'),
        'tip': lane.get('cleanup_tip'),
        'recovery_ref': lane.get('recovery_ref'),
        'event_type': lane.get('cleanup_event_type'),
        'reason': lane.get('cleanup_event_reason'),
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return f'governed-worktree-cleanup:{digest}'


def prepare_governed_worktree_cleanup(lane, event_type, reason, tip=None):
    existing_type = lane.get('cleanup_event_type')
    existing_reason = lane.get('cleanup_event_reason')
    if existing_type is None:
        lane['cleanup_event_type'] = event_type
        lane['cleanup_event_reason'] = reason
    elif event_type == 'governed_worktree_released' and existing_type != event_type:
        raise SyncwheelError(
            f"governed worktree lane {lane['id']!r} is already pending as {existing_type}"
        )
    elif event_type == 'governed_worktree_released' and existing_reason != reason:
        raise SyncwheelError(
            f"governed worktree lane {lane['id']!r} must retry its original --reason "
            f'{existing_reason!r}'
        )
    if tip and not lane.get('cleanup_tip'):
        lane['cleanup_tip'] = tip
    lane.setdefault('cleanup_operation_token', uuid.uuid4().hex)
    lane.setdefault('cleanup_idempotency_key', governed_worktree_cleanup_key(lane))


def governed_worktree_cleanup_checkpoint(stage):
    """Fault-injection seam at process-death boundaries in lane cleanup."""
    return None


def governed_worktree_cleanup_intent_payload(lane):
    return {
        'operation_token': lane['cleanup_operation_token'],
        'lane': lane['id'],
        'generation_token': governed_worktree_generation_token(lane),
        'cleanup_tip': lane.get('cleanup_tip'),
        'recovery_ref': lane.get('recovery_ref'),
        'terminal_type': lane.get('cleanup_event_type') or 'governed_worktree_reaped',
        'reason': lane.get('cleanup_event_reason') or 'expired',
        'supersedes': lane.get('cleanup_supersedes_key'),
        'lane_record': copy.deepcopy(lane),
    }


def append_governed_worktree_cleanup_intent(repo_root, lane, manifest_path=None):
    return append_ledger_event(
        repo_root,
        'governed_worktree_cleanup_intent',
        governed_worktree_cleanup_intent_payload(lane),
        manifest_path,
        idempotency_key=lane['cleanup_idempotency_key'],
    )


def governed_worktree_cleanup_ledger(repo_root, manifest_path=None):
    if not ledger_events_dir(repo_root, manifest_path).exists():
        return {
            'events': [],
            'intents': {},
            'pending': {},
            'successful': {},
            'superseded': set(),
        }
    recover_ledger_tail(repo_root, manifest_path)
    events = load_ledger_events(repo_root, manifest_path)
    intents = {}
    superseded = set()
    successful = {}
    for event in events:
        payload = event.get('payload') or {}
        key = payload.get('idempotency_key')
        if not isinstance(key, str) or not key:
            continue
        if event.get('type') == 'governed_worktree_cleanup_intent':
            intents[key] = event
            prior = payload.get('supersedes')
            if isinstance(prior, str) and prior:
                superseded.add(prior)
        elif event.get('type') in {'governed_worktree_reaped', 'governed_worktree_released'}:
            successful[key] = event
    pending = {
        key: event for key, event in intents.items()
        if key not in successful and key not in superseded
    }
    return {
        'events': events,
        'intents': intents,
        'pending': pending,
        'successful': successful,
        'superseded': superseded,
    }


def recover_governed_worktree_registry_from_ledger(
    repo_root,
    registry,
    persist,
    manifest_path=None,
):
    ledger = governed_worktree_cleanup_ledger(repo_root, manifest_path)
    changed = False
    by_id = {lane['id']: lane for lane in registry['lanes']}

    for key, terminal in ledger['successful'].items():
        payload = terminal.get('payload') or {}
        lane_id = payload.get('lane')
        generation = payload.get('generation_token')
        lane = by_id.get(lane_id)
        if (
            lane is not None
            and generation
            and governed_worktree_generation_token(lane) == generation
        ):
            registry['lanes'].remove(lane)
            by_id.pop(lane_id, None)
            changed = True

    pending_by_lane = {}
    for key, intent in ledger['pending'].items():
        payload = intent.get('payload') or {}
        lane_id = payload.get('lane')
        if isinstance(lane_id, str):
            previous = pending_by_lane.get(lane_id)
            if previous is None or intent.get('seq', 0) > previous[1].get('seq', 0):
                pending_by_lane[lane_id] = (key, intent)

    for lane_id, (key, intent) in pending_by_lane.items():
        payload = intent.get('payload') or {}
        recorded = payload.get('lane_record')
        generation = payload.get('generation_token')
        if not isinstance(recorded, dict) or not generation:
            continue
        lane = by_id.get(lane_id)
        if lane is None:
            restored = copy.deepcopy(recorded)
            registry['lanes'].append(restored)
            by_id[lane_id] = restored
            changed = True
            continue
        if governed_worktree_generation_token(lane) != generation:
            raise SyncwheelError(
                f'governed worktree lane {lane_id!r} has a different generation than its '
                'durable cleanup intent; inspect the lane before retrying cleanup'
            )
        current_key = lane.get('cleanup_idempotency_key')
        if current_key == key:
            continue
        if current_key is not None:
            if payload.get('supersedes') != current_key:
                raise SyncwheelError(
                    f'governed worktree lane {lane_id!r} has a cleanup operation that conflicts '
                    'with its durable ledger intent'
                )
        lane.clear()
        lane.update(copy.deepcopy(recorded))
        changed = True

    if changed:
        persist()
    return ledger


def governed_worktree_release_terminal(repo_root, lane_id, manifest_path=None):
    """Latest terminal any Syncwheel command wrote for this lane, release or reap."""
    ledger = governed_worktree_cleanup_ledger(repo_root, manifest_path)
    intent_keys = {
        key for key, event in ledger['intents'].items()
        if (event.get('payload') or {}).get('lane') == lane_id
    }
    for event in reversed(ledger['events']):
        if event.get('type') not in GOVERNED_WORKTREE_TERMINAL_EVENT_TYPES:
            continue
        payload = event.get('payload') or {}
        if (
            payload.get('lane') == lane_id
            and payload.get('idempotency_key') in intent_keys
        ):
            return event
    return None


def governed_worktree_release_note_key(lane_id, terminal, reason):
    payload = terminal.get('payload') or {}
    material = {
        'lane': lane_id,
        'generation_token': payload.get('generation_token'),
        'terminal_type': terminal.get('type'),
        'terminal_key': payload.get('idempotency_key'),
        'reason': reason,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return f'governed-worktree-release-note:{digest}'


def append_governed_worktree_release_note(repo_root, lane_id, reason, terminal, manifest_path=None):
    payload = terminal.get('payload') or {}
    return append_ledger_event(
        repo_root,
        'governed_worktree_release_noted',
        {
            'lane': lane_id,
            'reason': reason,
            'terminal_type': terminal.get('type'),
            'terminal_seq': terminal.get('seq'),
            'terminal_reason': payload.get('reason'),
            'generation_token': payload.get('generation_token'),
            'branch': payload.get('branch'),
            'path': payload.get('path'),
            'recovery_ref': payload.get('recovery_ref'),
        },
        manifest_path,
        idempotency_key=governed_worktree_release_note_key(lane_id, terminal, reason),
    )


def ensure_governed_worktree_recovery_ref(repo_root, recovery_ref, tip):
    current = ref_tip(repo_root, recovery_ref)
    if current == tip:
        return
    if current is not None:
        raise SyncwheelError(
            f'governed worktree recovery_ref_moved: recovery ref {recovery_ref} '
            f'points to {current} instead of {tip}'
        )
    result = git(repo_root, 'update-ref', recovery_ref, tip, ZERO_OBJECT_ID, check=False)
    if result.returncode == 0:
        return
    current = ref_tip(repo_root, recovery_ref)
    if current == tip:
        return
    if current is not None:
        raise SyncwheelError(
            f'governed worktree recovery_ref_moved: recovery ref {recovery_ref} '
            f'points to {current} instead of {tip}'
        )
    raise SyncwheelError(
        result.stderr.strip() or result.stdout.strip()
        or f'could not create governed worktree recovery ref {recovery_ref}'
    )


def verify_governed_worktree_recovery_ref(repo_root, recovery_ref, tip):
    current = ref_tip(repo_root, recovery_ref)
    if current != tip:
        actual = current or 'missing'
        raise SyncwheelError(
            f'governed worktree recovery_ref_moved: recovery ref {recovery_ref} '
            f'points to {actual} instead of {tip}; restore the anchored tip before retrying cleanup'
        )


def governed_worktree_record_for_path(repo_root, path):
    expected = Path(path).resolve(strict=False)
    for worktree in get_worktrees(repo_root):
        if worktree.get('path') and Path(worktree['path']).resolve(strict=False) == expected:
            return worktree
    return None


def governed_worktree_admin_dir_for_path(repo_root, path):
    expected_gitdir = (Path(path).resolve(strict=False) / '.git').resolve(strict=False)
    worktrees_dir = git_common_dir(repo_root) / 'worktrees'
    if not worktrees_dir.is_dir():
        return None
    matches = []
    for admin_dir in sorted(item for item in worktrees_dir.iterdir() if item.is_dir()):
        gitdir_path = admin_dir / 'gitdir'
        try:
            raw_gitdir = gitdir_path.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        candidate = Path(raw_gitdir)
        if not candidate.is_absolute():
            candidate = admin_dir / candidate
        if candidate.resolve(strict=False) == expected_gitdir:
            matches.append(admin_dir.resolve(strict=False))
    return matches[0] if len(matches) == 1 else None


def governed_worktree_registration_matches(repo_root, lane, path, admin_dir, lock_reason):
    path = Path(path).resolve(strict=False)
    admin_dir = Path(admin_dir).resolve(strict=False)
    try:
        raw_gitdir = (admin_dir / 'gitdir').read_text(encoding='utf-8').strip()
        head = (admin_dir / 'HEAD').read_text(encoding='utf-8').strip()
    except OSError:
        return False
    registered_gitdir = Path(raw_gitdir)
    if not registered_gitdir.is_absolute():
        registered_gitdir = admin_dir / registered_gitdir
    if registered_gitdir.resolve(strict=False) != (path / '.git').resolve(strict=False):
        return False
    if head != f"ref: refs/heads/{lane['branch']}":
        return False
    worktree = governed_worktree_record_for_path(repo_root, path)
    return bool(worktree and worktree.get('locked') == lock_reason)


def lock_governed_worktree_for_cleanup(repo_root, lane):
    stored_admin = lane.get('cleanup_admin_dir')
    if stored_admin:
        path = Path(lane['path']).resolve(strict=False)
        worktree = governed_worktree_record_for_path(repo_root, path)
        admin_dir = Path(stored_admin).resolve(strict=False)
        if not worktree and not admin_dir.exists() and not path.exists():
            return {
                'path': path,
                'admin_dir': admin_dir,
                'lock_reason': governed_worktree_cleanup_lock_reason(lane),
                'registration_removed': True,
            }, None
    else:
        worktree = find_worktree_record_for_branch(repo_root, lane['branch'])
        if not worktree or not worktree.get('path'):
            return None, None
        path = Path(worktree['path']).resolve(strict=False)
        admin_dir = governed_worktree_admin_dir_for_path(repo_root, path)
    if not worktree:
        return None, {
            'code': 'registration_mismatch',
            'remedy': 'inspect the recorded Git worktree registration before retrying cleanup',
        }
    lock_reason = governed_worktree_cleanup_lock_reason(lane)
    locked = git(
        repo_root,
        'worktree',
        'lock',
        '--reason',
        lock_reason,
        str(path),
        check=False,
    )
    refreshed = governed_worktree_record_for_path(repo_root, path)
    if locked.returncode != 0 and not (refreshed and refreshed.get('locked') == lock_reason):
        return None, {
            'code': 'lane_in_use',
            'remedy': 'retry after the process using the governed lane releases its Git worktree lock',
        }
    if admin_dir is None or not governed_worktree_registration_matches(
        repo_root,
        lane,
        path,
        admin_dir,
        lock_reason,
    ):
        if refreshed and refreshed.get('locked') == lock_reason:
            git(repo_root, 'worktree', 'unlock', str(path), check=False)
        return None, {
            'code': 'registration_mismatch',
            'remedy': 'inspect the recorded Git worktree registration before retrying cleanup',
        }
    return {
        'path': path,
        'admin_dir': admin_dir,
        'lock_reason': lock_reason,
    }, None


def unlock_governed_worktree_cleanup(repo_root, lock):
    if not lock:
        return
    worktree = governed_worktree_record_for_path(repo_root, lock['path'])
    if worktree and worktree.get('locked') == lock['lock_reason']:
        git(repo_root, 'worktree', 'unlock', str(lock['path']), check=False)


def governed_worktree_final_probe(path, anchored_tip, missing_at_classification):
    path = Path(path).resolve(strict=False)
    if missing_at_classification and path.exists():
        return {
            'code': 'path_reappeared',
            'remedy': 'inspect the reappeared lane path, then retry its recorded worktree release',
        }
    if not path.exists():
        return None
    untracked = run(
        ['git', '-C', str(path), 'ls-files', '--others', '--exclude-standard', '-z'],
        check=False,
    )
    if untracked.returncode != 0:
        return {
            'code': 'unavailable',
            'remedy': 'restore the worktree before retrying cleanup',
        }
    tracked = run(
        ['git', '-C', str(path), 'diff-index', '--quiet', anchored_tip, '--'],
        check=False,
    )
    if untracked.stdout or tracked.returncode != 0:
        return {
            'code': 'dirty',
            'remedy': 'the worktree became dirty before removal; retain or commit its changes, then retry release',
        }
    return None


def delete_governed_worktree_branch_with_anchor(repo_root, lane, tip):
    recovery_ref = lane['recovery_ref']
    branch_ref = f'refs/heads/{lane["branch"]}'
    transaction = '\n'.join([
        'start',
        f'update {recovery_ref} {tip} {tip}',
        f'delete {branch_ref} {tip}',
        'prepare',
        'commit',
        '',
    ])
    deletion = git(
        repo_root,
        'update-ref',
        '--stdin',
        check=False,
        input_text=transaction,
    )
    committed = 'commit: ok' in {line.strip() for line in deletion.stdout.splitlines()}
    if (
        deletion.returncode == 0
        and committed
        and ref_tip(repo_root, recovery_ref) == tip
        and ref_tip(repo_root, lane['branch']) is None
    ):
        return True, None
    recovery_tip = ref_tip(repo_root, recovery_ref)
    branch_tip = ref_tip(repo_root, lane['branch'])
    if recovery_tip != tip:
        actual = recovery_tip or 'missing'
        return False, {
            'code': 'recovery_ref_moved',
            'remedy': (
                f'restore recovery ref {recovery_ref} from {actual} to anchored tip {tip}, '
                'then retry cleanup'
            ),
        }
    if branch_tip != tip:
        return False, {
            'code': 'branch_advanced',
            'remedy': 'inspect the retained lane branch; its recovery ref is immutable',
        }
    detail = deletion.stderr.strip() or deletion.stdout.strip()
    if deletion.returncode == 0 and not committed:
        detail = 'ref transaction did not report commit: ok'
    return False, {
        'code': 'branch_delete_failed',
        'remedy': detail or 'retry cleanup',
    }


def governed_worktree_cleanup_candidates(repo_root, manifest, registry=None):
    if registry is None:
        registry = load_governed_worktree_registry(repo_root)[0]
    candidates = []
    for lane in registry['lanes']:
        status = governed_worktree_lane_status(repo_root, manifest, lane)
        if lane['state'] == 'active' and status['code'] != 'expired':
            continue
        if lane['state'] == 'reaped' and lane.get('pending_reason') != 'ledger_pending':
            continue
        if lane['state'] not in {'active', 'captured_pending_cleanup', 'reaped'}:
            continue
        candidates.append(status)
    return candidates


def reap_governed_worktree_lane(
    repo_root,
    manifest,
    lane,
    persist=None,
    manifest_path=None,
    event_type='governed_worktree_reaped',
    event_reason='expired',
):
    if lane['state'] == 'reaped' and lane.get('pending_reason') == 'ledger_pending':
        return True, governed_worktree_lane_status(repo_root, manifest, lane)

    status = governed_worktree_lane_status(repo_root, manifest, lane)
    lock, lock_failure = lock_governed_worktree_for_cleanup(repo_root, lane)
    if lock_failure:
        return False, {**status, **lock_failure}
    governed_worktree_cleanup_checkpoint('after_git_worktree_lock')
    path = lock['path'] if lock else Path(lane['path']).resolve(strict=False)
    if lock and Path(lane['path']).resolve(strict=False) != path:
        lane['path'] = str(path)

    def fail_before_ref_change(code, remedy):
        unlock_governed_worktree_cleanup(repo_root, lock)
        return False, {**status, 'code': code, 'remedy': remedy}

    path_exists = path.exists()
    if path_exists and not path_is_relative_to(path, governed_worktree_root(repo_root, manifest)):
        return fail_before_ref_change(
            'outside_root',
            'inspect and recover this lane; Syncwheel will not move or remove it',
        )
    if path_exists and not lock:
        return fail_before_ref_change(
            'unregistered_worktree',
            'inspect the path and branch; Syncwheel will not remove an unregistered worktree',
        )
    if lane['state'] == 'active' and event_type != 'governed_worktree_released':
        expired = governed_worktree_lane_lease_expired(
            lane,
            datetime.datetime.now(datetime.timezone.utc),
        )
        abandoned_missing = not path_exists and governed_worktree_owner_is_dead(lane['owner'])
        if not expired and not abandoned_missing:
            return fail_before_ref_change(status['code'], status['remedy'])
    current_tip = ref_tip(repo_root, lane['branch'])
    post_ref_cleanup = bool(
        current_tip is None
        and lane.get('cleanup_tip')
        and lane.get('recovery_ref')
    )
    if path_exists and post_ref_cleanup:
        retry_probe = governed_worktree_final_probe(path, lane['cleanup_tip'], False)
        if retry_probe:
            lane['state'] = 'captured_pending_cleanup'
            lane['pending_reason'] = 'worktree_remove_failed'
            lane['cleanup_failure'] = retry_probe['code']
            if persist:
                persist()
            return False, {**status, **retry_probe}
    elif path_exists:
        dirty = local_worktree_status(path)
        if dirty is None:
            return fail_before_ref_change(
                'unavailable',
                'restore the worktree before attempting cleanup',
            )
        if dirty:
            return fail_before_ref_change(
                'dirty',
                governed_lane_remedy(manifest, lane, commit_first=True),
            )
    current = run(['git', 'rev-parse', '--show-toplevel'], check=False)
    current_path = Path(current.stdout.strip()).resolve(strict=False) if current.returncode == 0 else None
    if current_path == path:
        return fail_before_ref_change(
            'current_directory',
            'leave the lane directory, then run a Syncwheel mutation again',
        )

    branch_advanced_pending = lane.get('pending_reason') == 'branch_advanced'
    retrying_advanced = bool(
        branch_advanced_pending
        and current_tip
        and lane.get('cleanup_tip')
        and lane.get('recovery_ref')
        and lane.get('cleanup_event_type')
    )
    if branch_advanced_pending and not retrying_advanced:
        return False, {
            **status,
            'code': 'branch_advanced',
            'remedy': governed_worktree_pending_remedy(manifest, lane),
        }
    if retrying_advanced and current_tip:
        superseded_key = lane.get('cleanup_idempotency_key')
        superseded_event_type = lane.get('cleanup_event_type')
        lane.pop('cleanup_tip', None)
        lane.pop('branch_delete_tip', None)
        lane.pop('recovery_ref', None)
        lane.pop('cleanup_idempotency_key', None)
        lane.pop('cleanup_operation_token', None)
        if (
            event_type == 'governed_worktree_released'
            and superseded_event_type != event_type
        ):
            lane.pop('cleanup_event_type', None)
            lane.pop('cleanup_event_reason', None)
        if superseded_key:
            lane['cleanup_supersedes_key'] = superseded_key

    anchored_tip = governed_worktree_cleanup_tip(repo_root, lane)
    if lane.get('cleanup_tip') and current_tip not in {None, lane['cleanup_tip']}:
        lane['state'] = 'captured_pending_cleanup'
        lane['pending_reason'] = 'branch_advanced'
        lane['cleanup_admin_dir'] = str(lock['admin_dir']) if lock else None
        lane['cleanup_lock_reason'] = lock['lock_reason'] if lock else None
        if persist:
            persist()
        return False, {
            **status,
            'code': 'branch_advanced',
            'remedy': governed_worktree_pending_remedy(manifest, lane),
        }

    lane['state'] = 'captured_pending_cleanup'
    lane['pending_reason'] = 'reaping'
    if lock:
        lane['cleanup_admin_dir'] = str(lock['admin_dir'])
        lane['cleanup_lock_reason'] = lock['lock_reason']
    if anchored_tip:
        lane['cleanup_tip'] = anchored_tip
        lane['recovery_ref'] = lane.get('recovery_ref') or governed_worktree_recovery_ref(lane)
    prepare_governed_worktree_cleanup(lane, event_type, event_reason, anchored_tip)
    governed_worktree_cleanup_checkpoint('before_cleanup_intent')
    append_governed_worktree_cleanup_intent(repo_root, lane, manifest_path)
    if persist:
        persist()
    governed_worktree_cleanup_checkpoint('after_cleanup_intent')

    if anchored_tip:
        try:
            ensure_governed_worktree_recovery_ref(repo_root, lane['recovery_ref'], anchored_tip)
            verify_governed_worktree_recovery_ref(repo_root, lane['recovery_ref'], anchored_tip)
        except SyncwheelError:
            lane['pending_reason'] = 'recovery_ref_moved'
            if persist:
                persist()
            raise
        governed_worktree_cleanup_checkpoint('after_recovery_anchor')
        current_tip = ref_tip(repo_root, lane['branch'])
        if current_tip is not None:
            if current_tip != anchored_tip:
                lane['pending_reason'] = 'branch_advanced'
                if persist:
                    persist()
                return False, {
                    **status,
                    'code': 'branch_advanced',
                    'remedy': governed_worktree_pending_remedy(manifest, lane),
                }
            deleted, delete_detail = delete_governed_worktree_branch_with_anchor(
                repo_root,
                lane,
                anchored_tip,
            )
            if not deleted:
                lane['pending_reason'] = delete_detail['code']
                lane['branch_delete_tip'] = anchored_tip
                if persist:
                    persist()
                if delete_detail['code'] == 'branch_advanced':
                    delete_detail['remedy'] = governed_worktree_pending_remedy(manifest, lane)
                return False, {**status, **delete_detail}
            governed_worktree_cleanup_checkpoint('after_ref_transaction')
        else:
            try:
                verify_governed_worktree_recovery_ref(repo_root, lane['recovery_ref'], anchored_tip)
            except SyncwheelError:
                lane['pending_reason'] = 'recovery_ref_moved'
                if persist:
                    persist()
                raise

    if lock and not lock.get('registration_removed') and not governed_worktree_registration_matches(
        repo_root,
        lane,
        path,
        lock['admin_dir'],
        lock['lock_reason'],
    ):
        lane['pending_reason'] = 'worktree_remove_failed'
        lane['cleanup_failure'] = 'registration_mismatch'
        if persist:
            persist()
        return False, {
            **status,
            'code': 'registration_mismatch',
            'remedy': 'the locked Git worktree registration changed; inspect it before retrying release',
        }

    final_probe = governed_worktree_final_probe(path, anchored_tip, not path_exists)
    if final_probe:
        lane['pending_reason'] = 'worktree_remove_failed'
        lane['cleanup_failure'] = final_probe['code']
        if persist:
            persist()
        return False, {**status, **final_probe}

    if lock and not lock.get('registration_removed'):
        governed_worktree_cleanup_checkpoint('before_worktree_remove')
        try:
            removal = run(
                ['git', 'worktree', 'remove', '--force', '--force', str(path)],
                cwd=repo_root,
                check=False,
            )
        except SyncwheelError:
            lane['pending_reason'] = 'worktree_remove_failed'
            lane['cleanup_failure'] = 'worktree_remove_failed'
            if persist:
                persist()
            raise
        if removal.returncode != 0:
            lane['pending_reason'] = 'worktree_remove_failed'
            lane['cleanup_failure'] = 'worktree_remove_failed'
            if persist:
                persist()
            raise SyncwheelError(
                removal.stderr.strip() or removal.stdout.strip()
                or f'could not remove governed worktree {path}'
            )
        governed_worktree_cleanup_checkpoint('after_worktree_remove')
        if Path(lock['admin_dir']).exists():
            lane['pending_reason'] = 'worktree_remove_failed'
            lane['cleanup_failure'] = 'registration_mismatch'
            if persist:
                persist()
            return False, {
                **status,
                'code': 'registration_mismatch',
                'remedy': 'Git retained the targeted worktree registration; inspect it before retrying release',
            }

    lane['state'] = 'reaped'
    lane['reaped_at'] = iso_utc_now()
    lane['pending_reason'] = 'ledger_pending'
    if persist:
        persist()
    return True, governed_worktree_lane_status(repo_root, manifest, lane)


def governed_worktree_reaped_payload(lane, reason='expired'):
    return {
        'lane': lane['id'], 'branch': lane['branch'], 'reason': reason,
        'recovery_ref': lane.get('recovery_ref'), 'target': lane.get('target'),
        'full': lane['full'],
        'generation_token': governed_worktree_generation_token(lane),
        'operation_token': lane.get('cleanup_operation_token'),
        'cleanup_tip': lane.get('cleanup_tip'),
        'path': lane.get('path'),
    }


def append_governed_worktree_cleanup_event(repo_root, lane, manifest_path=None):
    event_type = lane.get('cleanup_event_type') or 'governed_worktree_reaped'
    reason = lane.get('cleanup_event_reason') or 'expired'
    key = lane.get('cleanup_idempotency_key') or governed_worktree_cleanup_key(lane)
    lane['cleanup_idempotency_key'] = key
    return append_ledger_event(
        repo_root,
        event_type,
        governed_worktree_reaped_payload(lane, reason),
        manifest_path,
        idempotency_key=key,
    )


def completed_governed_worktree_lane(lane):
    completed = dict(lane)
    completed.pop('pending_reason', None)
    completed.pop('branch_delete_tip', None)
    completed.pop('cleanup_event_type', None)
    completed.pop('cleanup_event_reason', None)
    completed.pop('cleanup_idempotency_key', None)
    completed.pop('cleanup_operation_token', None)
    completed.pop('cleanup_supersedes_key', None)
    completed.pop('cleanup_tip', None)
    completed.pop('cleanup_admin_dir', None)
    completed.pop('cleanup_lock_reason', None)
    completed.pop('cleanup_failure', None)
    return completed


def reconcile_governed_worktrees(repo_root, manifest, manifest_path=None, candidate_ids=None):
    with governed_worktree_registry_lock(repo_root):
        prune_governed_worktree_stale_locks(repo_root, manifest_path)
        registry, _ = load_governed_worktree_registry(repo_root)
        persist = governed_worktree_registry_cas_persister(repo_root, registry)
        recover_governed_worktree_registry_from_ledger(
            repo_root,
            registry,
            persist,
            manifest_path,
        )
        paths_updated = False
        for lane in registry['lanes']:
            status = governed_worktree_lane_status(repo_root, manifest, lane)
            if (
                status.get('path_moved')
                and (candidate_ids is None or lane['id'] in candidate_ids)
            ):
                lane['path'] = status['path']
                paths_updated = True
        if paths_updated:
            persist()
        if candidate_ids is None:
            candidates = governed_worktree_cleanup_candidates(repo_root, manifest, registry)
            selected = {item['id'] for item in candidates}
        else:
            selected = set(candidate_ids)
        reaped = []
        failures = []
        for lane in list(registry['lanes']):
            if lane['id'] not in selected:
                continue
            completed, detail = reap_governed_worktree_lane(
                repo_root,
                manifest,
                lane,
                persist=persist,
                manifest_path=manifest_path,
            )
            if not completed:
                failures.append({
                    'id': lane['id'],
                    'code': detail.get('code') or 'lane_generation_changed',
                })
                continue
            try:
                governed_worktree_cleanup_checkpoint('before_terminal_ledger')
                append_governed_worktree_cleanup_event(repo_root, lane, manifest_path)
                governed_worktree_cleanup_checkpoint('after_terminal_ledger')
            except Exception:
                lane['state'] = 'reaped'
                lane['pending_reason'] = 'ledger_pending'
                persist()
                raise
            registry['lanes'].remove(lane)
            persist()
            governed_worktree_cleanup_checkpoint('after_cleanup_record_removed')
            reaped.append({'id': lane['id']})
        return {'reaped': reaped, 'failures': failures}


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


def coordination_manifest_snapshot(manifest, repo_root=None, coordination_state=None):
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
    if manifest.get('version') == MANIFEST_VERSION_CHANNELS:
        snapshot['integration']['derived_paths'] = list(
            manifest['integration'].get('derived_paths') or []
        )
        snapshot['integration']['derived_provenance'] = (
            derived_provenance_records(repo_root, manifest, coordination_state)
            if repo_root is not None
            else normalize_derived_provenance(
                manifest['integration'].get('derived_provenance') or [],
                label='integration.derived_provenance',
            )
        )
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
        if stack.get('depends_on'):
            snapshot_stack['depends_on'] = list(stack['depends_on'])
        snapshot['stacks'].append(snapshot_stack)
    if manifest.get('version') == MANIFEST_VERSION_CHANNELS:
        snapshot['channels'] = []
        for channel in manifest.get('channels', []):
            snapshot_channel = manifest_channel_history_summary(channel)
            snapshot_channel.pop('remote', None)
            snapshot_channel['base'] = public_coordination_ref(
                channel['base'], remote_roles, repo_root, f"channels.{channel['id']}.base"
            )
            snapshot['channels'].append(snapshot_channel)
    config = coordination_config(manifest)
    if config:
        snapshot['coordination'] = {
            key: value for key, value in config.items()
            if key in {'mode', 'id', 'state_branch', 'gc'}
        }
    if 'landing' in manifest:
        snapshot['landing'] = json.loads(json.dumps(manifest['landing']))
    return snapshot


def coordination_manifest_digest(manifest, repo_root=None):
    return canonical_json_digest(coordination_manifest_snapshot(manifest, repo_root))


def managed_ref_names(manifest):
    names = []
    for stack in manifest['stacks']:
        names.append(f"refs/heads/{stack['branch']}")
    names.append(f"refs/heads/{manifest['integration']['branch']}")
    for channel in manifest.get('channels', []):
        names.append(f"refs/heads/{channel['branch']}")
    return list(dict.fromkeys(names))


def delivery_ref_names(manifest):
    names = []
    base_branch = (manifest.get('defaults') or {}).get('base_branch')
    if base_branch:
        names.append(f'refs/heads/{base_branch}')
    for stack in manifest.get('stacks', []):
        if stack.get('target_branch'):
            names.append(f"refs/heads/{stack['target_branch']}")
    return list(dict.fromkeys(names))


def coordination_snapshot_managed_ref_names(snapshot):
    refs = []
    integration = snapshot.get('integration') or {}
    if isinstance(integration.get('branch'), str) and integration['branch']:
        refs.append(f"refs/heads/{integration['branch']}")
    for stack in snapshot.get('stacks') or []:
        if isinstance(stack, dict) and isinstance(stack.get('branch'), str) and stack['branch']:
            refs.append(f"refs/heads/{stack['branch']}")
    for channel in snapshot.get('channels') or []:
        if isinstance(channel, dict) and isinstance(channel.get('branch'), str) and channel['branch']:
            refs.append(f"refs/heads/{channel['branch']}")
    return list(dict.fromkeys(refs))


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
    channels = snapshot.get('channels', [])
    if not isinstance(defaults, dict) or 'base_ref' not in defaults:
        raise SyncwheelError('coordination state manifest is missing defaults.base_ref')
    if not isinstance(integration, dict) or 'base' not in integration:
        raise SyncwheelError('coordination state manifest is missing integration.base')
    derived_paths = integration.get('derived_paths')
    if snapshot.get('version') == MANIFEST_VERSION_CHANNELS:
        if (
            not isinstance(derived_paths, list)
            or not all(
                isinstance(item, str) and item and item.endswith('/')
                for item in derived_paths
            )
            or len(derived_paths) != len(set(derived_paths))
        ):
            raise SyncwheelError(
                'coordination state manifest integration.derived_paths must be '
                'a unique string array of path prefixes'
            )
        normalize_derived_provenance(
            integration.get('derived_provenance') or [],
            label='coordination state manifest integration.derived_provenance',
        )
    else:
        if derived_paths is not None:
            raise SyncwheelError(
                'coordination state manifest integration.derived_paths requires version 3'
            )
        if 'derived_provenance' in integration:
            raise SyncwheelError(
                'coordination state manifest integration.derived_provenance requires version 3'
            )
    if not isinstance(stacks, list):
        raise SyncwheelError('coordination state manifest stacks must be an array')
    if not isinstance(channels, list):
        raise SyncwheelError('coordination state manifest channels must be an array')
    if 'landing' in snapshot:
        normalize_landing_policy(snapshot['landing'])
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
        dependencies = stack.get('depends_on', [])
        if (
            not isinstance(dependencies, list)
            or not all(isinstance(item, str) and item for item in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            raise SyncwheelError(
                f'coordination state manifest stack {index} has invalid depends_on'
            )
    validate_stack_dependency_graph(
        stacks,
        require_declared_dependencies=snapshot.get('version') == MANIFEST_VERSION_CHANNELS,
    )
    for index, channel in enumerate(channels):
        if not isinstance(channel, dict) or 'base' not in channel:
            raise SyncwheelError(f'coordination state manifest channel {index} is missing base')
        coordination_public_remote_ref_parts(
            channel['base'], f'coordination state channels[{index}].base'
        )
        channel_id = channel.get('id')
        if not isinstance(channel_id, str) or not channel_id:
            raise SyncwheelError(f'coordination state manifest channel {index} is missing id')
        if not isinstance(channel.get('branch'), str) or not channel['branch']:
            raise SyncwheelError(f'coordination state manifest channel {channel_id} is missing branch')
        if not isinstance(channel.get('baseRevision'), str) or not re.fullmatch(
            r'[0-9a-f]{40}', channel['baseRevision']
        ):
            raise SyncwheelError(
                f'coordination state manifest channel {channel_id} has invalid baseRevision'
            )
        if channel.get('lifecycle') not in CHANNEL_LIFECYCLES:
            raise SyncwheelError(
                f'coordination state manifest channel {channel_id} has invalid lifecycle'
            )
        composition = channel.get('composition')
        if not isinstance(composition, list):
            raise SyncwheelError(
                f'coordination state manifest channel {channel_id} has invalid composition'
            )
        for entry in composition:
            normalize_channel_entry(entry, channel_id)
        normalize_channel_resolution(
            channel.get('resolution'), channel_id, channel_pin_digest(channel)
        )
        if channel['lifecycle'] == 'ephemeral':
            expiry = channel.get('expiry')
            if not isinstance(expiry, dict):
                raise SyncwheelError(
                    f'coordination state ephemeral channel {channel_id} is missing expiry'
                )
            normalize_channel_timestamp(expiry.get('createdAt'), f'channel {channel_id} expiry.createdAt')
            normalize_channel_timestamp(expiry.get('expiresAt'), f'channel {channel_id} expiry.expiresAt')


def validate_coordination_state(state, expected_id=None, claims_mode='advisory'):
    if not isinstance(state, dict):
        raise SyncwheelError('coordination state must be an object')
    if state.get('schema_version') not in {
        COORDINATION_STATE_SCHEMA_VERSION,
        COORDINATION_STATE_SCHEMA_VERSION_CHANNELS,
    }:
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
    manifest_version = state['manifest'].get('version')
    expected_schema = (
        COORDINATION_STATE_SCHEMA_VERSION_CHANNELS
        if manifest_version == MANIFEST_VERSION_CHANNELS
        else COORDINATION_STATE_SCHEMA_VERSION
    )
    if state['schema_version'] != expected_schema:
        raise SyncwheelError(
            f'coordination state schema {state["schema_version"]} is incompatible with '
            f'manifest version {manifest_version}'
        )
    dependency_stacks = [
        stack.get('id') for stack in state['manifest'].get('stacks', [])
        if isinstance(stack, dict) and stack.get('depends_on')
    ]
    if dependency_stacks and manifest_version != MANIFEST_VERSION_CHANNELS:
        raise SyncwheelError(
            'coordination state stack depends_on requires manifest version 3: '
            + ', '.join(str(stack_id) for stack_id in dependency_stacks)
        )
    validate_coordination_snapshot_refs(state['manifest'])
    if not isinstance(state.get('manifest_digest'), str) or not state['manifest_digest']:
        raise SyncwheelError('coordination state is missing manifest_digest')
    if canonical_json_digest(state['manifest']) != state['manifest_digest']:
        raise SyncwheelError('coordination state manifest_digest does not match its manifest')
    if not isinstance(state.get('managed_refs'), dict):
        raise SyncwheelError('coordination state is missing managed_refs')
    claims = state.get('claims', {})
    if not isinstance(claims, dict):
        raise SyncwheelError('coordination state claims must be an object')
    invalid_claims = sorted(set(claims) - set(state['managed_refs']))
    if invalid_claims:
        raise SyncwheelError(
            'coordination state claims contain unmanaged refs: ' + ', '.join(invalid_claims)
        )
    if claims_mode == 'required':
        unclaimed = sorted(set(state['managed_refs']) - set(claims))
        if unclaimed:
            raise SyncwheelError(
                'coordination claims required mode refuses unclaimed managed refs: '
                + ', '.join(unclaimed)
            )
    operation_token = state.get('operation_token')
    if operation_token is not None and (
        not isinstance(operation_token, str) or not operation_token
    ):
        raise SyncwheelError('coordination state operation_token must be a non-empty string')
    if not isinstance(state.get('tombstones', []), list):
        raise SyncwheelError('coordination state tombstones must be an array')
    return state


def coordination_state_from_commit(
    repo_root, commit, expected_id=None, claims_mode='advisory'
):
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
    return validate_coordination_state(state, expected_id, claims_mode)


def read_remote_coordination_state(repo_root, config, fetch=True, local_manifest_version=None):
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
    state = coordination_state_from_commit(
        repo_root, commit, config['id'], config.get('claims', 'advisory')
    )
    remote_manifest_version = state['manifest'].get('version')
    compatible_upgrade = (
        local_manifest_version == MANIFEST_VERSION_CHANNELS
        and remote_manifest_version == MANIFEST_VERSION_COORDINATED
    )
    if (
        local_manifest_version is not None
        and remote_manifest_version != local_manifest_version
        and not compatible_upgrade
    ):
        raise SyncwheelError(
            f"remote coordination manifest version {remote_manifest_version} is incompatible "
            f'with local manifest version {local_manifest_version}; migrate explicitly'
        )
    return {
        'tip': tip,
        'state': state,
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


def coordination_ownership_conflicts(repo_root, config, managed_refs, states=None):
    claimed = set(managed_refs)
    conflicts = []
    for item in (states if states is not None else read_remote_coordination_states(repo_root, config)):
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


def require_exclusive_coordination_ownership(
    repo_root,
    config,
    managed_refs,
    expected_state_refs=None,
):
    states = read_remote_coordination_states(repo_root, config)
    observed_state_refs = {item['ref']: item['tip'] for item in states}
    if expected_state_refs is not None and observed_state_refs != expected_state_refs:
        raise SyncwheelError(
            'coordinated publish STOP: coordination state refs changed after the reviewed plan'
        )
    conflicts = coordination_ownership_conflicts(
        repo_root, config, managed_refs, states=states
    )
    if conflicts:
        details = '; '.join(
            f"{item['coordination_id']}: {', '.join(item['refs'])}" for item in conflicts
        )
        raise SyncwheelError(
            'managed refs are already owned by another coordination domain: ' + details
        )
    return observed_state_refs


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


def build_coordination_state(
    repo_root, manifest, config, previous, observed_refs, changed_refs, scope,
    projection_status, installation, tombstone=None, claim_commits=None,
    operation_token=None,
):
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
    snapshot = coordination_manifest_snapshot(
        manifest,
        repo_root,
        previous_state,
    )
    return {
        'schema_version': (
            COORDINATION_STATE_SCHEMA_VERSION_CHANNELS
            if manifest.get('version') == MANIFEST_VERSION_CHANNELS
            else COORDINATION_STATE_SCHEMA_VERSION
        ),
        'coordination_id': config['id'],
        'publication_id': str(uuid.uuid4()),
        'parent_state': previous.get('tip') if previous else None,
        'created_at': iso_utc_now(),
        'syncwheel_version': VERSION,
        'installation_id': installation,
        'manifest': snapshot,
        'manifest_digest': canonical_json_digest(snapshot),
        'managed_refs': dict(sorted(managed.items())),
        'claims': dict(sorted({
            **((previous_state or {}).get('claims', {})),
            **(claim_commits or {}),
        }.items())),
        'changed_refs': dict(sorted(changed_refs.items())),
        'publication_scope': scope,
        'operation_token': operation_token,
        'projection_status': projection_status,
        'tombstones': coordination_tombstones(previous_state, manifest, tombstone),
    }


def build_coordination_repair_state(previous_state, previous_tip, repaired_ref, repaired_tip, installation):
    """Build an append-only repair child without re-projecting prior public state.

    A repair corrects transport evidence, not topology.  Deep-copying the
    validated parent is deliberate: the manifest snapshot and digest,
    tombstones, and every unmentioned (including no-longer-adopted) managed ref
    remain byte-for-byte equal when encoded as their JSON values.
    """
    validate_coordination_state(previous_state)
    if not isinstance(previous_tip, str) or not re.fullmatch(r'[0-9a-f]{40}', previous_tip):
        raise SyncwheelError('coordination repair requires an exact 40-hex parent state tip')
    if not is_valid_coordination_branch_ref(repaired_ref):
        raise SyncwheelError(f'coordination repair ref must be a full branch ref: {repaired_ref!r}')
    if not isinstance(repaired_tip, str) or not re.fullmatch(r'[0-9a-f]{40}', repaired_tip):
        raise SyncwheelError('coordination repair requires an exact 40-hex managed ref tip')
    if repaired_ref not in previous_state['managed_refs']:
        raise SyncwheelError(
            f'coordination repair refuses unadopted ref {repaired_ref}; publish ownership first'
        )
    child = copy.deepcopy(previous_state)
    child['publication_id'] = str(uuid.uuid4())
    child['parent_state'] = previous_tip
    child['created_at'] = iso_utc_now()
    child['syncwheel_version'] = VERSION
    child['installation_id'] = installation
    child['managed_refs'][repaired_ref] = repaired_tip
    child['changed_refs'] = {repaired_ref: repaired_tip}
    child['publication_scope'] = f'repair:{repaired_ref}'
    child['projection_status'] = previous_state.get('projection_status')
    return validate_coordination_state(child, previous_state['coordination_id'])


def build_tree_equivalent_coordination_repair_state(previous_state, previous_tip, plan, installation):
    """Append an evidence-only child for an exact tree-equivalent ref replacement."""
    child = build_coordination_repair_state(
        previous_state,
        previous_tip,
        plan['repairedRef'],
        plan['expectedRemoteTip'],
        installation,
    )
    child['changed_refs'] = {}
    child['publication_scope'] = f"repair-evidence:{plan['repairedRef']}"
    child['repair_evidence'] = {
        'schemaVersion': 1,
        'planDigest': plan['planDigest'],
        'proof': COORDINATION_REPAIR_TREE_EQUIVALENT_PROOF,
        'ref': plan['repairedRef'],
        'recordedTip': plan['expectedRecordedTip'],
        'observedTip': plan['expectedRemoteTip'],
        'tree': plan['expectedRemoteTree'],
    }
    return validate_coordination_state(child, previous_state['coordination_id'])


def build_fast_forward_coordination_repair_state(previous_state, previous_tip, plan, installation):
    """Append reviewed evidence for one exact fast-forward managed-ref advance."""
    child = build_coordination_repair_state(
        previous_state,
        previous_tip,
        plan['repairedRef'],
        plan['expectedRemoteTip'],
        installation,
    )
    child['changed_refs'] = {}
    child['publication_scope'] = f"repair-evidence:{plan['repairedRef']}"
    child['repair_evidence'] = {
        'schemaVersion': 1,
        'planDigest': plan['planDigest'],
        'proof': COORDINATION_REPAIR_FAST_FORWARD_PROOF,
        'ref': plan['repairedRef'],
        'recordedTip': plan['expectedRecordedTip'],
        'observedTip': plan['expectedRemoteTip'],
        'recordedTree': plan['expectedRecordedTree'],
        'observedTree': plan['expectedRemoteTree'],
        'advanceCommitCount': plan['expectedAdvanceCommitCount'],
        'advanceCommitsDigest': plan['expectedAdvanceCommitsDigest'],
    }
    return validate_coordination_state(child, previous_state['coordination_id'])


def coordination_repair_plan(repo_root, manifest, repaired_ref, freeze_backend='github-lock'):
    require_sha1_repository(repo_root, 'coordination repair')
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        raise SyncwheelError('coordination repair requires active-active coordination')
    profile, local_coordination = coordination_profile(repo_root)
    pending = local_coordination.get('pending_merge')
    if isinstance(pending, dict) and pending.get('coordination_id') == config['id']:
        raise SyncwheelError('coordination repair STOP: a pending coordination merge must be resolved first')
    previous = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    if not previous['state'] or not previous['tip']:
        raise SyncwheelError('coordination repair requires an existing remote coordination state')
    for owned_ref, owned_tip in previous['state']['managed_refs'].items():
        if not is_valid_coordination_branch_ref(owned_ref):
            raise SyncwheelError(f'coordination repair state has an invalid managed ref: {owned_ref!r}')
        if not isinstance(owned_tip, str) or not re.fullmatch(r'[0-9a-f]{40}', owned_tip):
            raise SyncwheelError(
                f'coordination repair state lacks an exact tip for managed ref: {owned_ref}'
            )
    require_exclusive_coordination_ownership(
        repo_root, config, previous['state']['managed_refs']
    )
    if repaired_ref not in previous['state']['managed_refs']:
        raise SyncwheelError(f'coordination repair refuses unowned ref: {repaired_ref}')
    observed_refs = remote_ref_tips(
        repo_root, config['remote'], list(previous['state']['managed_refs'])
    )
    observed = observed_refs[repaired_ref]
    if not observed:
        raise SyncwheelError(f'coordination repair managed ref is absent: {repaired_ref}')
    expected_recorded = previous['state']['managed_refs'][repaired_ref]
    status = 'noop' if expected_recorded == observed else 'repair-required'
    payload = {
        'schemaVersion': COORDINATION_REPAIR_PLAN_SCHEMA_VERSION,
        'operation': 'coordination-repair',
        'coordinationId': config['id'],
        'remote': config['remote'],
        'stateRef': coordination_state_ref(config),
        'expectedStateTip': previous['tip'],
        'repairedRef': repaired_ref,
        'expectedRecordedTip': expected_recorded,
        'expectedRemoteTip': observed,
        'guardedRefs': dict(sorted(observed_refs.items())),
        'localManifestDigest': coordination_manifest_digest(manifest, repo_root),
        'status': status,
        'precondition': 'externally-verified-write-freeze-or-server-transaction',
        'freezeBackend': freeze_backend,
    }
    if freeze_backend == COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND and status != 'noop':
        active_refs = coordination_snapshot_managed_ref_names(previous['state']['manifest'])
        if repaired_ref not in active_refs:
            raise SyncwheelError(
                'coordination repair tree-equivalence proof requires an active managed ref'
            )
        fetch_coordination_ref_tip(repo_root, config, repaired_ref, observed)
        if not commit_exists(repo_root, expected_recorded):
            raise SyncwheelError(
                'coordination repair tree-equivalence proof requires the recorded commit object'
            )
        recorded_tree = ref_tree(repo_root, expected_recorded)
        observed_tree = ref_tree(repo_root, observed)
        if recorded_tree != observed_tree:
            raise SyncwheelError(
                'coordination repair tree-equivalence proof failed: managed ref trees differ'
            )
        payload.update({
            'repairClass': 'tree-equivalent-state-evidence',
            'expectedRecordedTree': recorded_tree,
            'expectedRemoteTree': observed_tree,
            'proof': COORDINATION_REPAIR_TREE_EQUIVALENT_PROOF,
            'precondition': 'exact-tree-equivalence-and-state-only-cas',
        })
    if freeze_backend == COORDINATION_REPAIR_FAST_FORWARD_BACKEND and status != 'noop':
        active_refs = coordination_snapshot_managed_ref_names(previous['state']['manifest'])
        if repaired_ref not in active_refs:
            raise SyncwheelError(
                'coordination repair fast-forward proof requires an active managed ref'
            )
        fetch_coordination_ref_tip(repo_root, config, repaired_ref, observed)
        if not commit_exists(repo_root, expected_recorded):
            raise SyncwheelError(
                'coordination repair fast-forward proof requires the recorded commit object'
            )
        ancestry = git(
            repo_root,
            'merge-base',
            '--is-ancestor',
            expected_recorded,
            observed,
            check=False,
        )
        if ancestry.returncode != 0:
            raise SyncwheelError(
                'coordination repair fast-forward proof failed: observed tip is not a descendant'
            )
        commits = [
            item for item in git(
                repo_root,
                'rev-list',
                '--reverse',
                f'{expected_recorded}..{observed}',
            ).stdout.splitlines()
            if item
        ]
        if not commits or len(commits) > COORDINATION_REPAIR_MAX_ADVANCE_COMMITS:
            raise SyncwheelError(
                'coordination repair fast-forward proof exceeds the bounded commit interval'
            )
        recorded_tree = ref_tree(repo_root, expected_recorded)
        observed_tree = ref_tree(repo_root, observed)
        payload.update({
            'repairClass': 'fast-forward-state-evidence',
            'expectedRecordedTree': recorded_tree,
            'expectedRemoteTree': observed_tree,
            'expectedAdvanceCommits': commits,
            'expectedAdvanceCommitCount': len(commits),
            'expectedAdvanceCommitsDigest': canonical_json_digest(commits),
            'proof': COORDINATION_REPAIR_FAST_FORWARD_PROOF,
            'precondition': 'exact-fast-forward-ancestry-and-state-only-cas',
        })
    payload['planDigest'] = canonical_json_digest(payload)
    return payload, previous


class CoordinationRepairBackend:
    """Server-side serialization seam; ordinary Git push is intentionally absent."""

    name = 'unsupported'

    def preflight(self, **_kwargs):
        raise SyncwheelError(
            'coordination repair STOP unsupported: no verified write-freeze backend is selected'
        )

    def apply(self, **_kwargs):
        raise SyncwheelError('coordination repair backend cannot apply state CAS')

    def postflight(self, **kwargs):
        return self.preflight(**kwargs)

    def observe(self, repo_root, remote, refs):
        return remote_ref_tips(repo_root, remote, refs)


class GitHubLockCoordinationRepairBackend(CoordinationRepairBackend):
    name = 'github-lock'

    def preflight(self, **_kwargs):
        raise SyncwheelError(
            'coordination repair STOP unsupported: GitHub branch locks can be bypassed or '
            'changed concurrently and do not provide a continuous server-side transaction'
        )


class TreeEquivalentStateCasCoordinationRepairBackend(CoordinationRepairBackend):
    """CAS only the state ref after exact proof; never claim a lease on code refs."""

    name = COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND
    proof = COORDINATION_REPAIR_TREE_EQUIVALENT_PROOF

    def _verify_observations(
        self, repo_root, coordination, remote, state_ref, expected_state_tip, guarded_refs
    ):
        require_exclusive_coordination_ownership(repo_root, coordination, guarded_refs)
        observed = remote_ref_tips(repo_root, remote, [state_ref, *guarded_refs])
        if observed.get(state_ref) != expected_state_tip:
            raise SyncwheelError('coordination repair STOP: state lease was lost before state CAS')
        drifted = {
            ref: (tip, observed.get(ref)) for ref, tip in guarded_refs.items()
            if observed.get(ref) != tip
        }
        if drifted:
            raise SyncwheelError('coordination repair STOP: guarded refs drifted before state CAS')
        return observed

    def preflight(self, **kwargs):
        self._verify_observations(
            kwargs['repo_root'],
            kwargs['coordination'],
            kwargs['remote'],
            kwargs['state_ref'],
            kwargs['expected_state_tip'],
            kwargs['guarded_refs'],
        )
        return {'proof': self.proof}

    def apply(self, **kwargs):
        self._verify_observations(
            kwargs['repo_root'],
            kwargs['coordination'],
            kwargs['remote'],
            kwargs['state_ref'],
            kwargs['expected_state_tip'],
            kwargs['guarded_refs'],
        )
        command = [
            'git',
            'push',
            f"--force-with-lease={kwargs['state_ref']}:{kwargs['expected_state_tip']}",
            kwargs['remote'],
            f"{kwargs['new_state_tip']}:{kwargs['state_ref']}",
        ]
        result = run_authorized_push(
            kwargs['repo_root'],
            command,
            kwargs['remote'],
            [kwargs['state_ref']],
            check=False,
        )
        if result.returncode != 0:
            observed = remote_ref_tips(
                kwargs['repo_root'], kwargs['remote'], [kwargs['state_ref']]
            )[kwargs['state_ref']]
            if observed == kwargs['expected_state_tip']:
                raise SyncwheelError('coordination repair state CAS was rejected without mutation')
            if observed != kwargs['new_state_tip']:
                raise SyncwheelError('coordination repair outcome is unknown after state CAS rejection')
        return {'proof': self.proof, 'stateOnly': True}

    def postflight(self, **kwargs):
        self._verify_observations(
            kwargs['repo_root'],
            kwargs['coordination'],
            kwargs['remote'],
            kwargs['state_ref'],
            kwargs['expected_state_tip'],
            kwargs['guarded_refs'],
        )
        return {'proof': self.proof}


class FastForwardStateCasCoordinationRepairBackend(
    TreeEquivalentStateCasCoordinationRepairBackend
):
    """Adopt one exact reviewed fast-forward by CASing only append-only state."""

    name = COORDINATION_REPAIR_FAST_FORWARD_BACKEND
    proof = COORDINATION_REPAIR_FAST_FORWARD_PROOF


def apply_coordination_repair_plan(repo_root, manifest, plan, backend=None):
    if not isinstance(plan, dict):
        raise SyncwheelError('coordination repair plan must be a JSON object')
    required_plan_keys = {
        'schemaVersion', 'operation', 'coordinationId', 'remote', 'stateRef',
        'expectedStateTip', 'repairedRef', 'expectedRecordedTip',
        'expectedRemoteTip', 'guardedRefs', 'localManifestDigest', 'status',
        'precondition', 'freezeBackend', 'planDigest',
    }
    missing = sorted(required_plan_keys - set(plan))
    if missing:
        raise SyncwheelError('coordination repair plan is missing: ' + ', '.join(missing))
    if backend is None:
        if plan.get('freezeBackend') == 'github-lock':
            backend = GitHubLockCoordinationRepairBackend()
        elif plan.get('freezeBackend') == COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND:
            backend = TreeEquivalentStateCasCoordinationRepairBackend()
        elif plan.get('freezeBackend') == COORDINATION_REPAIR_FAST_FORWARD_BACKEND:
            backend = FastForwardStateCasCoordinationRepairBackend()
        else:
            backend = CoordinationRepairBackend()
    supplied_digest = plan.get('planDigest')
    unsigned = {key: value for key, value in plan.items() if key != 'planDigest'}
    if supplied_digest != canonical_json_digest(unsigned):
        raise SyncwheelError('coordination repair plan digest is invalid')
    if plan.get('schemaVersion') != COORDINATION_REPAIR_PLAN_SCHEMA_VERSION:
        raise SyncwheelError('unsupported coordination repair plan schema')
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        raise SyncwheelError('coordination repair requires active-active coordination')
    if plan.get('coordinationId') != config['id'] or plan.get('remote') != config['remote']:
        raise SyncwheelError('coordination repair plan does not match the active coordination domain')
    if plan.get('freezeBackend') != backend.name:
        raise SyncwheelError('coordination repair backend does not match the reviewed plan')
    tree_equivalent_repair = backend.name == COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND
    fast_forward_repair = backend.name == COORDINATION_REPAIR_FAST_FORWARD_BACKEND
    state_only_repair = tree_equivalent_repair or fast_forward_repair
    if tree_equivalent_repair and plan.get('status') != 'noop':
        required_proof = {
            'repairClass', 'expectedRecordedTree', 'expectedRemoteTree', 'proof',
        }
        missing_proof = sorted(required_proof - set(plan))
        if missing_proof:
            raise SyncwheelError(
                'coordination repair tree-equivalence plan is missing: '
                + ', '.join(missing_proof)
            )
        if (
            plan.get('repairClass') != 'tree-equivalent-state-evidence'
            or plan.get('proof') != COORDINATION_REPAIR_TREE_EQUIVALENT_PROOF
            or plan.get('expectedRecordedTree') != plan.get('expectedRemoteTree')
            or plan.get('precondition') != 'exact-tree-equivalence-and-state-only-cas'
        ):
            raise SyncwheelError('coordination repair tree-equivalence proof is invalid')
    if fast_forward_repair and plan.get('status') != 'noop':
        required_proof = {
            'repairClass', 'expectedRecordedTree', 'expectedRemoteTree',
            'expectedAdvanceCommits', 'expectedAdvanceCommitCount',
            'expectedAdvanceCommitsDigest', 'proof',
        }
        missing_proof = sorted(required_proof - set(plan))
        if missing_proof:
            raise SyncwheelError(
                'coordination repair fast-forward plan is missing: '
                + ', '.join(missing_proof)
            )
        commits = plan.get('expectedAdvanceCommits')
        valid_commits = (
            isinstance(commits, list)
            and 1 <= len(commits) <= COORDINATION_REPAIR_MAX_ADVANCE_COMMITS
            and len(set(commits)) == len(commits)
            and all(
                isinstance(item, str) and re.fullmatch(r'[0-9a-f]{40}', item)
                for item in commits
            )
            and commits[-1] == plan.get('expectedRemoteTip')
        )
        if (
            plan.get('repairClass') != 'fast-forward-state-evidence'
            or plan.get('proof') != COORDINATION_REPAIR_FAST_FORWARD_PROOF
            or plan.get('precondition') != 'exact-fast-forward-ancestry-and-state-only-cas'
            or not isinstance(plan.get('expectedRecordedTree'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', plan['expectedRecordedTree'])
            or not isinstance(plan.get('expectedRemoteTree'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', plan['expectedRemoteTree'])
            or not valid_commits
            or plan.get('expectedAdvanceCommitCount') != len(commits)
            or plan.get('expectedAdvanceCommitsDigest') != canonical_json_digest(commits)
        ):
            raise SyncwheelError('coordination repair fast-forward proof is invalid')
    if plan.get('localManifestDigest') != coordination_manifest_digest(manifest, repo_root):
        raise SyncwheelError('coordination repair STOP: local manifest changed after review')
    current_plan, previous = coordination_repair_plan(
        repo_root, manifest, plan['repairedRef'], plan['freezeBackend']
    )
    comparison_keys = [
        'stateRef', 'expectedStateTip', 'expectedRecordedTip', 'expectedRemoteTip',
        'guardedRefs', 'localManifestDigest', 'precondition',
    ]
    if tree_equivalent_repair and plan.get('status') != 'noop':
        comparison_keys.extend([
            'repairClass', 'expectedRecordedTree', 'expectedRemoteTree', 'proof',
        ])
    if fast_forward_repair and plan.get('status') != 'noop':
        comparison_keys.extend([
            'repairClass', 'expectedRecordedTree', 'expectedRemoteTree',
            'expectedAdvanceCommits', 'expectedAdvanceCommitCount',
            'expectedAdvanceCommitsDigest', 'proof',
        ])
    for key in comparison_keys:
        if current_plan.get(key) != plan.get(key):
            raise SyncwheelError(f'coordination repair STOP: reviewed plan drifted at {key}')
    if plan.get('status') == 'noop':
        return {'status': 'noop', 'state_tip': previous['tip'], 'plan_digest': supplied_digest}
    backend.preflight(
        repo_root=repo_root,
        coordination=config,
        remote=config['remote'],
        state_ref=plan['stateRef'],
        expected_state_tip=plan['expectedStateTip'],
        guarded_refs=plan['guardedRefs'],
    )
    installation = installation_id(create=True)
    if tree_equivalent_repair:
        child = build_tree_equivalent_coordination_repair_state(
            previous['state'], previous['tip'], plan, installation
        )
    elif fast_forward_repair:
        child = build_fast_forward_coordination_repair_state(
            previous['state'], previous['tip'], plan, installation
        )
    else:
        child = build_coordination_repair_state(
            previous['state'], previous['tip'], plan['repairedRef'], plan['expectedRemoteTip'], installation
        )
    child_tip = create_coordination_state_commit(repo_root, child, previous['tip'])
    result = backend.apply(
        repo_root=repo_root,
        coordination=config,
        remote=config['remote'],
        state_ref=plan['stateRef'],
        expected_state_tip=plan['expectedStateTip'],
        new_state_tip=child_tip,
        guarded_refs=plan['guardedRefs'],
    )
    observed = backend.observe(
        repo_root, config['remote'], [plan['stateRef'], *plan['guardedRefs']]
    )
    if observed.get(plan['stateRef']) != child_tip:
        raise SyncwheelError('coordination repair outcome is unknown: state CAS was not observed')
    drifted = {
        ref: (tip, observed.get(ref)) for ref, tip in plan['guardedRefs'].items()
        if observed.get(ref) != tip
    }
    if drifted:
        raise SyncwheelError('coordination repair post-verification failed: guarded refs drifted')
    backend.postflight(
        repo_root=repo_root,
        coordination=config,
        remote=config['remote'],
        state_ref=plan['stateRef'],
        expected_state_tip=child_tip,
        guarded_refs=plan['guardedRefs'],
    )
    verified = coordination_state_from_commit(repo_root, child_tip, config['id'])
    git_parent = git(repo_root, 'rev-parse', f'{child_tip}^').stdout.strip()
    if (
        git_parent != previous['tip']
        or verified['parent_state'] != previous['tip']
        or verified['managed_refs'][plan['repairedRef']] != plan['expectedRemoteTip']
    ):
        raise SyncwheelError('coordination repair post-verification failed: invalid child state')
    if state_only_repair:
        evidence = verified.get('repair_evidence')
        if (
            verified.get('changed_refs') != {}
            or not isinstance(evidence, dict)
            or evidence.get('planDigest') != supplied_digest
            or evidence.get('proof') != plan.get('proof')
        ):
            raise SyncwheelError(
                'coordination repair post-verification failed: invalid state-only evidence'
            )
        if tree_equivalent_repair and evidence.get('tree') != plan['expectedRemoteTree']:
            raise SyncwheelError(
                'coordination repair post-verification failed: invalid tree-equivalent evidence'
            )
        if fast_forward_repair and (
            evidence.get('recordedTree') != plan['expectedRecordedTree']
            or evidence.get('observedTree') != plan['expectedRemoteTree']
            or evidence.get('advanceCommitCount') != plan['expectedAdvanceCommitCount']
            or evidence.get('advanceCommitsDigest') != plan['expectedAdvanceCommitsDigest']
        ):
            raise SyncwheelError(
                'coordination repair post-verification failed: invalid fast-forward evidence'
            )
    return {
        'status': 'repaired',
        'state_tip': child_tip,
        'parent_state': previous['tip'],
        'plan_digest': supplied_digest,
        'backend': backend.name,
        'backend_result': result,
        'proof': plan.get('proof'),
    }


def coordination_compose_stack_plan(
    repo_root, manifest, stack_id, known_base_state_tip, known_base_snapshot_digest
):
    require_sha1_repository(repo_root, 'coordination compose')
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        raise SyncwheelError('coordination compose requires active-active coordination')
    profile, local_coordination = coordination_profile(repo_root)
    pending = local_coordination.get('pending_merge')
    if isinstance(pending, dict) and pending.get('coordination_id') == config['id']:
        raise SyncwheelError('coordination compose STOP: resolve the pending coordination merge first')
    if not isinstance(known_base_state_tip, str) or not re.fullmatch(r'[0-9a-f]{40}', known_base_state_tip):
        raise SyncwheelError('coordination compose requires an exact SHA-1 known base state tip')
    latest = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    if not latest['tip'] or not latest['state']:
        raise SyncwheelError('coordination compose requires existing remote coordination state')
    if not commit_exists(repo_root, known_base_state_tip):
        raise SyncwheelError('coordination compose known base state object is unavailable')
    coordination_state_chain_contains(
        repo_root, config['id'], known_base_state_tip, latest['tip']
    )
    base_state = coordination_state_from_commit(
        repo_root, known_base_state_tip, config['id']
    )
    if base_state['manifest_digest'] != known_base_snapshot_digest:
        raise SyncwheelError('coordination compose known base snapshot digest does not match state')
    local_snapshot = coordination_manifest_snapshot(manifest, repo_root)
    composition = compose_additive_coordination_snapshots(
        base_state['manifest'], local_snapshot, latest['state']['manifest'], stack_id
    )
    merged_snapshot = composition['merged']
    merged_snapshot_digest = canonical_json_digest(merged_snapshot)
    stack = require_stack(manifest, stack_id)
    source_ref = f"refs/heads/{stack['branch']}"
    source_tip = ref_tip(repo_root, stack['branch'])
    if not source_tip or not re.fullmatch(r'[0-9a-f]{40}', source_tip):
        raise SyncwheelError('coordination compose requested stack has no exact local source tip')
    claimed_refs = list(dict.fromkeys([*latest['state']['managed_refs'], source_ref]))
    require_exclusive_coordination_ownership(repo_root, config, claimed_refs)
    observed_refs = remote_ref_tips(repo_root, config['remote'], claimed_refs)
    drifted = {
        ref: (tip, observed_refs.get(ref))
        for ref, tip in latest['state']['managed_refs'].items()
        if observed_refs.get(ref) != tip
    }
    if drifted:
        raise SyncwheelError('coordination compose STOP: remote state does not match managed refs')
    remote_has_composed_snapshot = latest['state']['manifest_digest'] == merged_snapshot_digest
    remote_source_tip = observed_refs.get(source_ref)
    if remote_has_composed_snapshot:
        if latest['state']['managed_refs'].get(source_ref) != source_tip or remote_source_tip != source_tip:
            raise SyncwheelError('coordination compose remote composed state has a different source tip')
        status = 'adopt-only'
    else:
        if remote_source_tip is not None:
            raise SyncwheelError('coordination compose refuses to adopt an existing unowned source ref')
        status = 'publish-required'
    integration_ref = f"refs/heads/{manifest['integration']['branch']}"
    integration_tip = latest['state']['managed_refs'].get(integration_ref)
    if not integration_tip or observed_refs.get(integration_ref) != integration_tip:
        raise SyncwheelError('coordination compose requires an exact unchanged integration tip')
    if ref_tip(repo_root, manifest['integration']['branch']) != integration_tip:
        raise SyncwheelError('coordination compose local integration must match the remote state tip exactly')
    proposed_manifest = apply_coordination_snapshot(manifest, merged_snapshot)
    validation = validate_manifest(repo_root, proposed_manifest)
    if validation['errors']:
        raise SyncwheelError(
            'coordination compose proposed manifest is invalid: ' + '; '.join(validation['errors'])
        )
    unmapped = list(validation['details']['integration'].get('unmapped_commits') or [])
    payload = {
        'schemaVersion': COORDINATION_COMPOSE_PLAN_SCHEMA_VERSION,
        'operation': 'coordination-compose-stack',
        'coordinationId': config['id'],
        'remote': config['remote'],
        'stateRef': coordination_state_ref(config),
        'knownBaseStateTip': known_base_state_tip,
        'knownBaseSnapshotDigest': known_base_snapshot_digest,
        'expectedRemoteStateTip': latest['tip'],
        'remoteSnapshotDigest': latest['state']['manifest_digest'],
        'localProposalDigest': manifest_digest(manifest),
        'localSnapshotDigest': canonical_json_digest(local_snapshot),
        'composedSnapshot': merged_snapshot,
        'composedSnapshotDigest': merged_snapshot_digest,
        'proposedManifestDigest': manifest_digest(proposed_manifest),
        'stack': stack_id,
        'sourceRef': source_ref,
        'sourceTip': source_tip,
        'expectedRemoteSourceTip': remote_source_tip,
        'guardedRefs': dict(sorted(observed_refs.items())),
        'integrationRef': integration_ref,
        'expectedIntegrationTip': integration_tip,
        'expectedIntegrationTree': ref_tree(repo_root, integration_tip),
        'unmappedIntegrationCommits': unmapped,
        'projectionStatus': 'partial',
        'integrationMutation': False,
        'localAddedStacks': composition['localAddedStacks'],
        'remoteAddedStacks': composition['remoteAddedStacks'],
        'status': status,
    }
    payload['planDigest'] = canonical_json_digest(payload)
    return payload, proposed_manifest, latest


def apply_coordination_compose_stack_plan(repo_root, manifest, manifest_path, plan):
    if not isinstance(plan, dict):
        raise SyncwheelError('coordination compose plan must be a JSON object')
    supplied_digest = plan.get('planDigest')
    unsigned = {key: value for key, value in plan.items() if key != 'planDigest'}
    if supplied_digest != canonical_json_digest(unsigned):
        raise SyncwheelError('coordination compose plan digest does not match its payload')
    if plan.get('schemaVersion') != COORDINATION_COMPOSE_PLAN_SCHEMA_VERSION:
        raise SyncwheelError('unsupported coordination compose plan schema')
    if plan.get('operation') != 'coordination-compose-stack':
        raise SyncwheelError('coordination compose plan has the wrong operation')
    proposed_manifest = apply_coordination_snapshot(
        manifest, plan.get('composedSnapshot') or {}
    )
    _identity, retry_fingerprint = coordination_publication_identity(
        repo_root,
        proposed_manifest,
        {plan['sourceRef']: plan['sourceTip']},
        f"compose-stack:{plan['stack']}",
        plan['projectionStatus'],
    )
    publication_operation = pending_coordination_publication_after_resolution(
        repo_root, proposed_manifest, manifest_path, retry_fingerprint
    )
    if publication_operation:
        current = plan
        latest = {
            'tip': plan['expectedRemoteStateTip'],
            'state': coordination_state_from_commit(
                repo_root,
                plan['expectedRemoteStateTip'],
                plan['coordinationId'],
            ),
        }
    else:
        current, proposed_manifest, latest = coordination_compose_stack_plan(
            repo_root,
            manifest,
            plan.get('stack'),
            plan.get('knownBaseStateTip'),
            plan.get('knownBaseSnapshotDigest'),
        )
    if current != plan:
        raise SyncwheelError('coordination compose STOP: reviewed plan drifted')
    if publication_operation and plan['status'] == 'adopt-only':
        result = coordinated_publish(
            repo_root,
            proposed_manifest,
            manifest_path,
            {plan['sourceRef']: plan['sourceTip']},
            f"compose-stack:{plan['stack']}",
            plan['projectionStatus'],
            expected_coordination_state_tip=publication_operation[
                'expected_coordination_state_tip'
            ],
            operation_token=publication_operation['operation_token'],
        )
    elif plan['status'] == 'publish-required':
        if publication_operation is None:
            publication_operation = begin_coordination_publication(
                repo_root,
                proposed_manifest,
                manifest_path,
                {plan['sourceRef']: plan['sourceTip']},
                f"compose-stack:{plan['stack']}",
                plan['projectionStatus'],
                expected_state_tip=plan['expectedRemoteStateTip'],
            )
        result = coordinated_publish(
            repo_root,
            proposed_manifest,
            manifest_path,
            {plan['sourceRef']: plan['sourceTip']},
            f"compose-stack:{plan['stack']}",
            plan['projectionStatus'],
            expected_coordination_state_tip=plan['expectedRemoteStateTip'],
            expected_observed_refs=plan['guardedRefs'],
            operation_token=publication_operation['operation_token'],
        )
        if result.get('status') not in {'published', 'recovered'}:
            raise SyncwheelError('coordination compose publication outcome requires a fresh plan')
        config = coordination_config(proposed_manifest)
        accepted = read_remote_coordination_state(
            repo_root, config, fetch=True, local_manifest_version=proposed_manifest['version']
        )
        state = accepted['state']
        if (
            state.get('parent_state') != plan['expectedRemoteStateTip']
            or state.get('manifest_digest') != plan['composedSnapshotDigest']
            or state.get('manifest') != plan['composedSnapshot']
            or state.get('changed_refs') != {plan['sourceRef']: plan['sourceTip']}
            or state.get('managed_refs', {}).get(plan['integrationRef']) != plan['expectedIntegrationTip']
            or state.get('projection_status') != 'partial'
            or state.get('tombstones') != latest['state'].get('tombstones')
        ):
            raise SyncwheelError('coordination compose post-verification failed: invalid state child')
        require_exclusive_coordination_ownership(
            repo_root, config, state['managed_refs']
        )
        observed = remote_ref_tips(repo_root, config['remote'], state['managed_refs'])
        if observed != state['managed_refs']:
            raise SyncwheelError('coordination compose post-verification failed: managed refs drifted')
    observed_manifest, _ = load_manifest(repo_root, manifest_path)
    if not observed_manifest or manifest_digest(observed_manifest) != plan['localProposalDigest']:
        raise SyncwheelError(
            'coordination compose local adoption pending: manifest drifted after remote publication'
        )
    try:
        save_manifest_with_ledger(
            repo_root,
            manifest_path,
            proposed_manifest,
            'coordination_compose_stack',
            {
                'plan_digest': supplied_digest,
                'known_base_state': plan['knownBaseStateTip'],
                'remote_state': plan['expectedRemoteStateTip'],
                'stack': plan['stack'],
            },
        )
    except (OSError, SyncwheelError) as exc:
        raise SyncwheelError(
            'coordination compose local adoption pending; do not retry publication, replan: '
            + str(exc)
        ) from exc
    verified_manifest, _ = load_manifest(repo_root, manifest_path)
    if not verified_manifest or manifest_digest(verified_manifest) != plan['proposedManifestDigest']:
        raise SyncwheelError('coordination compose local adoption verification failed')
    if publication_operation:
        complete_coordination_publication(
            repo_root, manifest_path, publication_operation, result
        )
    return {
        'status': 'composed' if plan['status'] == 'publish-required' else 'adopted',
        'plan_digest': supplied_digest,
        'remote_state_tip': (
            read_remote_coordination_state(
                repo_root,
                coordination_config(proposed_manifest),
                fetch=False,
                local_manifest_version=proposed_manifest['version'],
            )['tip']
        ),
        'manifest_digest': plan['proposedManifestDigest'],
        'integration_mutated': False,
        'unmapped_integration_commits': plan['unmappedIntegrationCommits'],
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


def coordination_claim_from_commit(repo_root, commit):
    result = git(repo_root, 'show', f'{commit}:{COORDINATION_CLAIM_FILE}', check=False)
    if result.returncode != 0:
        raise SyncwheelError(
            result.stderr.strip()
            or f'coordination claim {commit} does not contain {COORDINATION_CLAIM_FILE}'
        )
    try:
        claim = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SyncwheelError(f'invalid coordination claim JSON at {commit}: {exc}') from exc
    required = ('coordination_id', 'source_ref', 'operation_token', 'claimed_at', 'syncwheel_version')
    if not isinstance(claim, dict) or any(
        not isinstance(claim.get(key), str) or not claim[key] for key in required
    ):
        raise SyncwheelError(f'invalid coordination claim payload at {commit}')
    coordination_claim_ref(claim['source_ref'])
    if 'closed' in claim and not isinstance(claim['closed'], bool):
        raise SyncwheelError(f'invalid coordination claim closed flag at {commit}')
    return claim


def create_coordination_claim_commit(
    repo_root,
    source_ref,
    coordination_id,
    operation_token,
    parent_tip=None,
    *,
    closed=False,
    reason=None,
):
    claim = {
        'coordination_id': normalize_coordination_id(coordination_id),
        'source_ref': source_ref,
        'operation_token': operation_token,
        'claimed_at': iso_utc_now(),
        'syncwheel_version': VERSION,
    }
    if closed:
        claim['closed'] = True
        claim['reason'] = reason or 'closed'
    encoded = json.dumps(claim, indent=2, sort_keys=True) + '\n'
    blob = run(
        ['git', 'hash-object', '-w', '--stdin'], cwd=repo_root, input_text=encoded
    ).stdout.strip()
    descriptor, index_path = tempfile.mkstemp(prefix='syncwheel-claim-index-')
    os.close(descriptor)
    os.unlink(index_path)
    environment = {
        'GIT_INDEX_FILE': index_path,
        **COORDINATION_GIT_IDENTITY_ENV,
        'GIT_AUTHOR_DATE': claim['claimed_at'],
        'GIT_COMMITTER_DATE': claim['claimed_at'],
    }
    try:
        if parent_tip:
            git(repo_root, 'read-tree', f'{parent_tip}^{{tree}}', env=environment)
        else:
            git(repo_root, 'read-tree', '--empty', env=environment)
        git(
            repo_root, 'update-index', '--add', '--cacheinfo',
            f'100644,{blob},{COORDINATION_CLAIM_FILE}', env=environment,
        )
        tree = git(repo_root, 'write-tree', env=environment).stdout.strip()
        command = ['git', *COORDINATION_GIT_IDENTITY_CONFIG, 'commit-tree', tree]
        if parent_tip:
            command.extend(['-p', parent_tip])
        command.extend(['-m', f'syncwheel claim: {source_ref}'])
        return run(command, cwd=repo_root).stdout.strip()
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def fetch_coordination_claim(repo_root, remote, claim_ref, tip):
    if not tip:
        return None
    result = git(repo_root, 'fetch', '--quiet', remote, claim_ref, check=False)
    if result.returncode != 0 or ref_tip(repo_root, 'FETCH_HEAD') != tip:
        raise SyncwheelError(f'coordination claim changed while fetching: {claim_ref}')
    return coordination_claim_from_commit(repo_root, 'FETCH_HEAD')


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


def channel_snapshot_map(snapshot):
    return {channel['id']: channel for channel in snapshot.get('channels') or []}


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
        'channels': snapshot.get('channels', []),
        'coordination': snapshot.get('coordination'),
        'landing': snapshot.get('landing'),
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


def require_sha1_repository(repo_root, operation):
    object_format = git(repo_root, 'rev-parse', '--show-object-format').stdout.strip()
    if object_format != 'sha1':
        raise SyncwheelError(f'{operation} supports only SHA-1 repositories; found {object_format}')


def coordination_state_chain_contains(repo_root, coordination_id, base_tip, latest_tip):
    """Prove both Git ancestry and every declared append-only state link."""
    if git(repo_root, 'merge-base', '--is-ancestor', base_tip, latest_tip, check=False).returncode != 0:
        raise SyncwheelError('coordination compose known base state is not an ancestor of remote state')
    current = latest_tip
    while current != base_tip:
        state = coordination_state_from_commit(repo_root, current, coordination_id)
        declared_parent = state.get('parent_state')
        git_parent_result = git(repo_root, 'rev-parse', f'{current}^', check=False)
        git_parent = git_parent_result.stdout.strip() if git_parent_result.returncode == 0 else None
        if not declared_parent or declared_parent != git_parent:
            raise SyncwheelError('coordination compose state chain is not append-only')
        current = declared_parent
    coordination_state_from_commit(repo_root, base_tip, coordination_id)
    return True


def additive_coordination_snapshot_delta(base, candidate, side):
    """Accept only stack additions and their ordered integration membership."""
    base_contract = json.loads(json.dumps(base))
    candidate_contract = json.loads(json.dumps(candidate))
    base_contract.pop('stacks', None)
    candidate_contract.pop('stacks', None)
    base_contract.get('integration', {}).pop('stacks', None)
    candidate_contract.get('integration', {}).pop('stacks', None)
    if base_contract != candidate_contract:
        raise SyncwheelError(
            f'coordination compose {side} proposal changes shared defaults or integration contract'
        )
    base_stacks = stack_snapshot_map(base)
    candidate_stacks = stack_snapshot_map(candidate)
    removed = sorted(set(base_stacks) - set(candidate_stacks))
    changed = sorted(
        stack_id for stack_id in set(base_stacks) & set(candidate_stacks)
        if base_stacks[stack_id] != candidate_stacks[stack_id]
    )
    if removed or changed:
        raise SyncwheelError(
            f'coordination compose {side} proposal is not additive; '
            f'removed={removed}, changed={changed}'
        )
    base_order = [stack['id'] for stack in base.get('stacks') or []]
    candidate_order = [stack['id'] for stack in candidate.get('stacks') or []]
    if candidate_order[:len(base_order)] != base_order:
        raise SyncwheelError(f'coordination compose {side} proposal reorders existing stacks')
    added = candidate_order[len(base_order):]
    if set(added) != set(candidate_stacks) - set(base_stacks):
        raise SyncwheelError(f'coordination compose {side} proposal has ambiguous stack ordering')
    base_members = list(base.get('integration', {}).get('stacks') or [])
    candidate_members = list(candidate.get('integration', {}).get('stacks') or [])
    if candidate_members != [*base_members, *added]:
        raise SyncwheelError(
            f'coordination compose {side} integration membership must append exactly its new stacks'
        )
    return {'added': added}


def compose_additive_coordination_snapshots(base, local, remote, requested_stack):
    local_delta = additive_coordination_snapshot_delta(base, local, 'local')
    remote_delta = additive_coordination_snapshot_delta(base, remote, 'remote')
    if local_delta['added'] != [requested_stack]:
        raise SyncwheelError(
            'coordination compose local proposal must add exactly the requested stack'
        )
    local_map = stack_snapshot_map(local)
    remote_map = stack_snapshot_map(remote)
    overlap = set(local_delta['added']).intersection(remote_delta['added'])
    conflicting = sorted(
        stack_id for stack_id in overlap if local_map[stack_id] != remote_map[stack_id]
    )
    if conflicting:
        raise SyncwheelError(
            'coordination compose has conflicting additions: ' + ', '.join(conflicting)
        )
    merged = json.loads(json.dumps(base))
    merged_map = stack_snapshot_map(merged)
    ordered_additions = []
    for source, added in ((remote_map, remote_delta['added']), (local_map, local_delta['added'])):
        for stack_id in added:
            if stack_id not in merged_map:
                merged_map[stack_id] = json.loads(json.dumps(source[stack_id]))
                ordered_additions.append(stack_id)
    merged['stacks'] = [
        *list(base.get('stacks') or []),
        *(merged_map[stack_id] for stack_id in ordered_additions),
    ]
    merged['integration']['stacks'] = [
        *list(base.get('integration', {}).get('stacks') or []),
        *ordered_additions,
    ]
    branches = [stack['branch'] for stack in merged['stacks']]
    if len(branches) != len(set(branches)):
        raise SyncwheelError('coordination compose stack branch ownership is ambiguous')
    validate_coordination_snapshot_refs(merged)
    return {
        'merged': merged,
        'localAddedStacks': local_delta['added'],
        'remoteAddedStacks': remote_delta['added'],
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
        restored['meta'] = (
            local_stack['meta'] if isinstance(local_stack.get('meta'), dict) else {}
        )
        updated['stacks'].append(restored)
    updated['channels'] = []
    for channel in snapshot.get('channels', []):
        restored_channel = dict(channel)
        restored_channel['base'] = local_coordination_ref(channel['base'], defaults)
        restored_channel['remote'] = defaults['publication_remote']
        updated['channels'].append(restored_channel)
    if 'coordination' in snapshot:
        coordination = dict(updated.get('coordination') or {})
        coordination.update(snapshot['coordination'])
        coordination['remote'] = coordination.get('remote') or defaults['publication_remote']
        updated['coordination'] = coordination
    else:
        updated.pop('coordination', None)
    if 'landing' in snapshot:
        updated['landing'] = normalize_landing_policy(snapshot['landing'])
    return updated


def coordination_state_matches_remote(repo_root, config, state):
    expected = state.get('managed_refs') or {}
    observed = remote_ref_tips(repo_root, config['remote'], expected)
    return all(observed.get(ref) == tip for ref, tip in expected.items())


def coordination_unclaimed_owned_refs(config, state):
    if config.get('claims', 'advisory') != 'advisory':
        return []
    return sorted(
        set(state.get('managed_refs') or {}) - set(state.get('claims') or {})
    )


def report_advisory_unclaimed_owned_refs(config, state):
    unclaimed = coordination_unclaimed_owned_refs(config, state)
    if unclaimed:
        print(
            'coordination claims advisory: unclaimed owned refs: '
            + ', '.join(unclaimed)
            + '; run syncwheel coordination claims backfill'
        )


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
    latest = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
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


def deterministic_stack_replay_tip(repo_root, base, commits):
    """Return the exact tip produced by replaying commits onto base, or None."""
    projection = deterministic_stack_projection(repo_root, base, commits)
    return projection.get('tip') if projection['status'] == 'projected' else None


def deterministic_stack_projection(repo_root, base, commits):
    """Construct a stack projection entirely in the object database.

    The detailed status lets callers distinguish an empty projection from a
    conflicting one without falling back to a temporary worktree.
    """
    head = base
    for declared_commit in commits:
        commit = commit_full_sha(repo_root, declared_commit)
        merge = git(
            repo_root,
            'merge-tree',
            '--write-tree',
            f'--merge-base={commit}^',
            head,
            commit,
            check=False,
        )
        if merge.returncode != 0:
            detail = (merge.stderr.strip() or merge.stdout.strip())[:2000]
            named = git(
                repo_root,
                'merge-tree',
                '--write-tree',
                '--name-only',
                '-z',
                '--no-messages',
                f'--merge-base={commit}^',
                head,
                commit,
                check=False,
            )
            paths = [
                path for path in named.stdout.split('\0')
                if path and not re.fullmatch(r'[0-9a-f]{40,64}', path)
            ]
            return {
                'status': 'conflict',
                'commit': commit,
                'base': head,
                'paths': paths,
                'detail': detail,
            }
        tree = merge.stdout.strip()
        if not tree or tree == ref_tree(repo_root, head):
            return {'status': 'empty', 'commit': commit, 'base': head}
        head = git(
            repo_root,
            'commit-tree',
            tree,
            '-p',
            head,
            '-m',
            replay_commit_message(repo_root, commit),
            env=replay_commit_env(repo_root, commit),
        ).stdout.strip()
    return {'status': 'projected', 'tip': head, 'tree': ref_tree(repo_root, head)}


def coordination_stack_ref_is_exact_rebase(
    repo_root,
    manifest,
    remote_snapshot,
    stack_id,
    remote_tip,
):
    """Accept only a replay-proven rebase of an already published stack ref."""
    previous_manifest = apply_coordination_snapshot(manifest, remote_snapshot)
    previous_stack = stack_map(previous_manifest).get(stack_id)
    local_stack = stack_map(manifest).get(stack_id)
    if not previous_stack or not local_stack:
        return False
    for key in (
        'branch',
        'base',
        'target_remote',
        'target_branch',
        'integration_branch',
        'state',
        'depends_on',
    ):
        if previous_stack.get(key) != local_stack.get(key):
            return False
    previous_commits = list(previous_stack.get('commits') or [])
    local_commits = list(local_stack.get('commits') or [])
    if not previous_commits or not local_commits:
        return False
    if commit_full_sha(repo_root, previous_commits[-1]) != remote_tip:
        return False
    local_tip = ref_tip(repo_root, local_stack['branch'])
    if not local_tip or commit_full_sha(repo_root, local_commits[-1]) != local_tip:
        return False
    expected_tip = deterministic_stack_replay_tip(
        repo_root,
        local_stack['base'],
        previous_commits,
    )
    return expected_tip == local_tip


def integration_partial_stack_adoption_allowed(
    remote_integration, local_integration, added, changed_stack_refs, tombstone=None
):
    remote_shape = dict(remote_integration or {})
    local_shape = dict(local_integration or {})
    remote_members = remote_shape.pop('stacks', None)
    local_members = local_shape.pop('stacks', None)
    return (
        not tombstone
        and remote_shape == local_shape
        and isinstance(remote_members, list)
        and isinstance(local_members, list)
        and [item for item in local_members if item not in added] == remote_members
        and all(stack_id in local_members for stack_id in added)
        and all(stack_id in changed_stack_refs for stack_id in added)
    )


def validate_coordination_publication_base(
    repo_root,
    manifest,
    config,
    expected,
    changed_refs,
    tombstone=None,
    rename=None,
    state_transition=None,
    remedy_stack=None,
    creation_remedy=False,
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

    remote_channels = channel_snapshot_map(remote_snapshot)
    local_channels = channel_snapshot_map(local_snapshot)
    remote_channel_ids = set(remote_channels)
    local_channel_ids = set(local_channels)
    removed_channels = remote_channel_ids - local_channel_ids
    allowed_channel_close = {
        tombstone['channel']
        if tombstone and tombstone.get('channel') else None
    } - {None}
    unexpected_removed_channels = sorted(removed_channels - allowed_channel_close)
    if unexpected_removed_channels:
        raise SyncwheelError(
            'local manifest would drop remote-managed channel(s): '
            + ', '.join(unexpected_removed_channels)
            + '; run handoff and resolve the stale manifest first'
        )
    changed_channel_refs = {
        channel['id']
        for channel in manifest.get('channels', [])
        if f"refs/heads/{channel['branch']}" in changed_refs
    }
    added_channels = local_channel_ids - remote_channel_ids
    missing_channel_refs = sorted(added_channels - changed_channel_refs)
    if missing_channel_refs:
        raise SyncwheelError(
            'new channel(s) require their managed branch in the coordinated publication: '
            + ', '.join(missing_channel_refs)
        )
    for channel_id in sorted(remote_channel_ids & local_channel_ids):
        remote_channel = remote_channels[channel_id]
        local_channel = local_channels[channel_id]
        if remote_channel == local_channel:
            continue
        if remote_channel['branch'] != local_channel['branch']:
            raise SyncwheelError(
                f'{channel_id}: changing channel branch ownership requires manual coordination review'
            )
        if channel_id not in changed_channel_refs:
            raise SyncwheelError(
                f'{channel_id}: local channel differs from published state without publishing its managed branch'
            )
        # Channel branches intentionally replace pinned compositions. The exact
        # coordinated lease, rather than ancestry, is the concurrency boundary.

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
        remedy = ''
        if creation_remedy:
            first_missing = missing_added_refs[0]
            remedy = (
                f'; close or publish {", ".join(missing_added_refs)} first. '
                'For an unpublished local draft, run:\n  '
                f'syncwheel stack close {first_missing} --force\n'
                'Then retry the stack create command.'
            )
        elif remedy_stack and expected.get('tip') and state.get('manifest_digest'):
            remedy = (
                f'; publish or close {", ".join(missing_added_refs)} first. '
                'Then coordinate the remaining local proposal with:\n  '
                f'syncwheel coordination compose --stack {remedy_stack} '
                f'--known-base-state {expected["tip"]} '
                f'--known-base-snapshot-digest {state["manifest_digest"]}'
            )
        raise SyncwheelError(
            'new stack(s) require their managed branch in the coordinated publication: '
            + ', '.join(missing_added_refs)
            + remedy
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
        safe_successor = not remote_tip or coordination_ref_is_safe_successor(
            repo_root,
            config,
            ref,
            remote_tip,
            local_stack['branch'],
        )
        exact_rebase = remote_tip and coordination_stack_ref_is_exact_rebase(
            repo_root,
            manifest,
            remote_snapshot,
            stack_id,
            remote_tip,
        )
        if not safe_successor and not exact_rebase:
            raise SyncwheelError(
                f'{stack_id}: local branch is not a safe successor of the published managed ref; '
                'run handoff and resolve the overlapping stack change'
            )

    remote_integration = remote_snapshot.get('integration')
    local_integration = local_snapshot.get('integration')
    integration_ref = f"refs/heads/{manifest['integration']['branch']}"
    if remote_integration != local_integration:
        partial_stack_adoption = integration_partial_stack_adoption_allowed(
            remote_integration, local_integration, added, changed_stack_refs, tombstone
        )
        if partial_stack_adoption:
            return
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


def coordination_publication_identity(
    repo_root,
    manifest,
    changed_refs,
    scope,
    projection_status,
    *,
    tombstone=None,
    rename=None,
    state_transition=None,
    publication_manifest=None,
):
    published_manifest = publication_manifest or manifest
    stable_tombstone = copy.deepcopy(tombstone) if tombstone else None
    if stable_tombstone:
        stable_tombstone.pop('closed_at', None)
    identity = {
        'coordination_id': coordination_config(manifest)['id'],
        'scope': scope,
        'projection_status': projection_status,
        'manifest_digest': canonical_json_digest(
            coordination_manifest_snapshot(published_manifest, repo_root)
        ),
        'changed_refs': dict(sorted(changed_refs.items())),
        'tombstone': stable_tombstone,
        'rename': rename,
        'state_transition': state_transition,
    }
    return identity, canonical_json_digest(identity)


def pending_coordination_publications(repo_root, manifest_path):
    events = load_ledger_events(repo_root, manifest_path)
    completed = {
        (event.get('payload') or {}).get('operation_token')
        for event in events
        if event.get('type') in {
            'coordination_publish_completed',
            'coordination_publish_abandoned',
        }
    }
    pending = []
    for event in events:
        if event.get('type') != 'coordination_publish_intent':
            continue
        payload = event.get('payload') or {}
        if payload.get('operation_token') not in completed:
            pending.append(payload)
    return pending


def pending_coordination_publication(repo_root, manifest_path, fingerprint):
    pending = pending_coordination_publications(repo_root, manifest_path)
    matching = [item for item in pending if item.get('fingerprint') == fingerprint]
    if matching:
        return matching[-1]
    if pending:
        scopes = sorted({str(item.get('scope')) for item in pending})
        raise SyncwheelError(
            'another coordinated publication intent is pending: '
            + ', '.join(scopes)
            + '; retry or reconcile that exact command first'
        )
    return None


def pending_coordination_publication_for_scope(
    repo_root, manifest_path, scope
):
    matching = [
        item for item in pending_coordination_publications(repo_root, manifest_path)
        if item.get('scope') == scope
    ]
    if len(matching) > 1:
        raise SyncwheelError(
            f'multiple coordinated publication intents are pending for {scope}; '
            'inspect the ledger before retrying'
        )
    return matching[-1] if matching else None


def coordination_remote_is_reachable(repo_root, remote):
    return git(
        repo_root, 'ls-remote', '--quiet', remote, 'HEAD', check=False
    ).returncode == 0


def coordinated_publish_remote_failure(remedy):
    return SyncwheelError(
        'coordinated publish could not inspect the coordination remote; '
        f'restore remote access, then retry:\n  syncwheel {remedy}'
    )


def coordination_intent_touched_refs(intent):
    refs = list(intent.get('changed_refs') or {})
    closed_ref = coordination_tombstone_ref(intent.get('tombstone'))
    if closed_ref:
        refs.append(closed_ref)
    return list(dict.fromkeys(refs))


def coordinated_operation_landed(
    repo_root, config, state, changed_refs, touched_refs, operation_token
):
    if not operation_token or not touched_refs:
        return False
    observed = remote_ref_tips(repo_root, config['remote'], list(changed_refs))
    for ref, sha in changed_refs.items():
        if not sha or observed.get(ref) != sha:
            return False
    claims = (state or {}).get('claims') or {}
    claim_refs = {ref: coordination_claim_ref(ref) for ref in touched_refs}
    claim_tips = remote_ref_tips(repo_root, config['remote'], claim_refs.values())
    for source_ref, claim_ref in claim_refs.items():
        claim_tip = claim_tips[claim_ref]
        if not claim_tip or claims.get(source_ref) != claim_tip:
            return False
        claim = fetch_coordination_claim(
            repo_root, config['remote'], claim_ref, claim_tip
        ) or {}
        if (
            claim.get('coordination_id') != config['id']
            or claim.get('source_ref') != source_ref
            or claim.get('operation_token') != operation_token
        ):
            return False
    return True


def publication_intent_owner_recovers(intent):
    """Promote finishes its own landed intent: it still owes a manifest save."""
    return str(intent.get('scope') or '').startswith('promote:')


def restore_abandoned_publication_rename(repo_root, intent):
    rename = intent.get('rename') or {}
    from_branch = rename.get('from_branch')
    to_branch = rename.get('to_branch')
    if not from_branch or not to_branch or from_branch == to_branch:
        return
    if branch_exists(repo_root, from_branch) or not branch_exists(repo_root, to_branch):
        return
    git(repo_root, 'branch', '-m', to_branch, from_branch, check=False)


def resolve_pending_coordination_publications(
    repo_root, manifest, manifest_path, *, adopt_tokens=()
):
    """Terminalize publish intents that can no longer complete as recorded.

    An intent whose own refs and claims are already on the remote is completed
    as ``already_published``; one whose reviewed state tip has been overtaken
    can never satisfy its own leases again and is abandoned. Tokens in
    ``adopt_tokens`` are left pending when they landed, so the command that
    owns them can finish from its own recovery path.
    """
    config = coordination_config(manifest)
    if not config:
        return None
    pending = pending_coordination_publications(repo_root, manifest_path)
    if not pending:
        return None
    observed = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    for intent in pending:
        token = intent.get('operation_token')
        if coordinated_operation_landed(
            repo_root,
            config,
            observed.get('state'),
            intent.get('changed_refs') or {},
            coordination_intent_touched_refs(intent),
            token,
        ):
            if token in adopt_tokens or publication_intent_owner_recovers(intent):
                continue
            append_ledger_event(
                repo_root,
                'coordination_publish_completed',
                {
                    'operation_token': token,
                    'fingerprint': intent.get('fingerprint'),
                    'scope': intent.get('scope'),
                    'coordination_state': observed['tip'],
                    'coordination_status': 'already_published',
                    'recovered': True,
                },
                manifest_path,
            )
            continue
        if observed['tip'] != intent.get('expected_coordination_state_tip'):
            restore_abandoned_publication_rename(repo_root, intent)
            append_ledger_event(
                repo_root,
                'coordination_publish_abandoned',
                {
                    'operation_token': token,
                    'fingerprint': intent.get('fingerprint'),
                    'scope': intent.get('scope'),
                    'reason': 'superseded',
                    'status': 'superseded',
                    'coordination_state': observed['tip'],
                },
                manifest_path,
            )
    return observed


def renew_coordination_publication(repo_root, manifest, manifest_path, pending):
    """Give a pending intent a reviewable plan again.

    A recorded state tip that has been overtaken can never satisfy its own
    leases, so the intent is abandoned and replaced by an equivalent one
    observed against the current state. An intent whose result already landed
    keeps its token so the publish recovers instead of republishing.
    """
    config = coordination_config(manifest)
    observed = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    if observed['tip'] == pending.get('expected_coordination_state_tip'):
        return pending
    if coordinated_operation_landed(
        repo_root,
        config,
        observed.get('state'),
        pending.get('changed_refs') or {},
        coordination_intent_touched_refs(pending),
        pending.get('operation_token'),
    ):
        return pending
    append_ledger_event(
        repo_root,
        'coordination_publish_abandoned',
        {
            'operation_token': pending.get('operation_token'),
            'fingerprint': pending.get('fingerprint'),
            'scope': pending.get('scope'),
            'reason': 'superseded',
            'status': 'superseded',
            'coordination_state': observed['tip'],
        },
        manifest_path,
    )
    payload = {
        **{key: value for key, value in pending.items() if key != 'retry'},
        'operation_token': str(uuid.uuid4()),
        'expected_coordination_state_tip': observed['tip'],
    }
    append_ledger_event(
        repo_root, 'coordination_publish_intent', payload, manifest_path
    )
    return payload


def pending_coordination_publication_after_resolution(
    repo_root, manifest, manifest_path, fingerprint
):
    adopt = {
        intent.get('operation_token')
        for intent in pending_coordination_publications(repo_root, manifest_path)
        if intent.get('fingerprint') == fingerprint
    }
    resolve_pending_coordination_publications(
        repo_root, manifest, manifest_path, adopt_tokens=adopt
    )
    return pending_coordination_publication(repo_root, manifest_path, fingerprint)


def begin_coordination_publication(
    repo_root,
    manifest,
    manifest_path,
    changed_refs,
    scope,
    projection_status,
    *,
    tombstone=None,
    rename=None,
    state_transition=None,
    publication_manifest=None,
    expected_state_tip=EXPECTED_COORDINATION_STATE_UNSET,
):
    config = coordination_config(manifest)
    identity, fingerprint = coordination_publication_identity(
        repo_root,
        manifest,
        changed_refs,
        scope,
        projection_status,
        tombstone=tombstone,
        rename=rename,
        state_transition=state_transition,
        publication_manifest=publication_manifest,
    )
    existing = pending_coordination_publication_after_resolution(
        repo_root, manifest, manifest_path, fingerprint
    )
    if existing:
        return {**existing, 'retry': True}
    observed = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    if (
        expected_state_tip is not EXPECTED_COORDINATION_STATE_UNSET
        and observed['tip'] != expected_state_tip
    ):
        raise SyncwheelError(
            'coordinated publication state changed before its intent could be recorded'
        )
    payload = {
        **identity,
        'fingerprint': fingerprint,
        'operation_token': str(uuid.uuid4()),
        'expected_coordination_state_tip': observed['tip'],
    }
    append_ledger_event(
        repo_root, 'coordination_publish_intent', payload, manifest_path
    )
    return {**payload, 'retry': False}


def complete_coordination_publication(
    repo_root, manifest_path, operation, result
):
    append_ledger_event(
        repo_root,
        'coordination_publish_completed',
        {
            'operation_token': operation['operation_token'],
            'fingerprint': operation['fingerprint'],
            'scope': operation['scope'],
            'coordination_state': result.get('state_tip'),
            'coordination_status': result.get('status'),
            'recovered': bool(result.get('recovered')),
        },
        manifest_path,
    )


def coordinated_publication_matches_remote(
    repo_root,
    config,
    state,
    changed_refs,
    touched_refs,
    scope,
    projection_status,
    manifest_digest_value,
    operation_token,
):
    if (
        state.get('operation_token') != operation_token
        or state.get('publication_scope') != scope
        or state.get('projection_status') != projection_status
        or state.get('manifest_digest') != manifest_digest_value
        or state.get('changed_refs') != dict(sorted(changed_refs.items()))
        or not coordination_state_matches_remote(repo_root, config, state)
    ):
        return False
    claim_refs = {source_ref: coordination_claim_ref(source_ref) for source_ref in touched_refs}
    observed = remote_ref_tips(repo_root, config['remote'], claim_refs.values())
    for source_ref, claim_ref in claim_refs.items():
        claim_tip = observed[claim_ref]
        if not claim_tip or state.get('claims', {}).get(source_ref) != claim_tip:
            return False
        claim = fetch_coordination_claim(
            repo_root, config['remote'], claim_ref, claim_tip
        )
        if (
            claim.get('coordination_id') != config['id']
            or claim.get('source_ref') != source_ref
            or claim.get('operation_token') != operation_token
        ):
            return False
    return True


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
    expected_coordination_state_tip=EXPECTED_COORDINATION_STATE_UNSET,
    expected_observed_refs=None,
    expected_coordination_state_refs=None,
    preflight_complete=False,
    remedy_stack=None,
    creation_remedy=False,
    operation_token=None,
    publication_manifest=None,
):
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        raise SyncwheelError('coordinated publish requires an active-active manifest version 2 coordination block')
    if config['remote'] != manifest['defaults']['publication_remote']:
        raise SyncwheelError('coordination.remote must match defaults.publication_remote')
    changed_refs = dict(changed_refs)
    publication_manifest = publication_manifest or manifest
    touched_refs = list(changed_refs)
    managed = managed_ref_names(publication_manifest)
    if tombstone:
        closed_ref = tombstone.get('ref') or f"refs/heads/{tombstone['branch']}"
        touched_refs = list(dict.fromkeys([*touched_refs, closed_ref]))
        managed = list(dict.fromkeys([
            *managed,
            closed_ref,
        ]))
    if rename:
        managed = list(dict.fromkeys([
            *managed,
            f"refs/heads/{rename['from_branch']}",
        ]))
    if not dry_run and not operation_token:
        raise SyncwheelError(
            'coordinated publish requires a durable caller operation token'
        )
    invalid = sorted(set(changed_refs) - set(managed))
    if invalid:
        raise SyncwheelError('coordinated publish received unmanaged refs: ' + ', '.join(invalid))
    expected = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    desired_manifest_digest = canonical_json_digest(
        coordination_manifest_snapshot(publication_manifest, repo_root)
    )
    if (
        operation_token
        and expected.get('state')
        and coordinated_publication_matches_remote(
            repo_root,
            config,
            expected['state'],
            changed_refs,
            touched_refs,
            scope,
            projection_status,
            desired_manifest_digest,
            operation_token,
        )
    ):
        report_advisory_unclaimed_owned_refs(config, expected['state'])
        print('coordinated publish: recovered the exact published operation intent')
        return {
            'status': 'recovered',
            'state_tip': expected['tip'],
            'recovered': True,
        }
    if operation_token and coordinated_operation_landed(
        repo_root,
        config,
        expected.get('state'),
        changed_refs,
        touched_refs,
        operation_token,
    ):
        report_advisory_unclaimed_owned_refs(config, expected['state'])
        print(
            'coordinated publish: this operation is already published; '
            'completing without republishing'
        )
        return {
            'status': 'already_published',
            'state_tip': expected['tip'],
            'recovered': True,
        }
    if (
        expected_coordination_state_tip is not EXPECTED_COORDINATION_STATE_UNSET
        and expected['tip'] != expected_coordination_state_tip
    ):
        raise SyncwheelError(
            'coordinated publish STOP: remote state changed after the reviewed plan'
        )
    require_exclusive_coordination_ownership(
        repo_root,
        config,
        managed,
        expected_state_refs=expected_coordination_state_refs,
    )
    claim_refs = {ref: coordination_claim_ref(ref) for ref in touched_refs}
    observation_refs = list(dict.fromkeys([*managed, *claim_refs.values()]))
    full_observation = remote_ref_tips(repo_root, config['remote'], observation_refs)
    observed_refs = {ref: full_observation.get(ref) for ref in managed}
    observed_claims = {
        source_ref: full_observation.get(claim_ref)
        for source_ref, claim_ref in claim_refs.items()
    }
    if expected_observed_refs is not None:
        planned_observations = {
            ref: expected_observed_refs.get(ref) for ref in observation_refs
        }
        if full_observation != planned_observations:
            raise SyncwheelError(
                'coordinated publish STOP: managed refs changed after the reviewed plan'
            )
        # The lease belongs to the reviewed observation, not to the later
        # verification read. Equality above proves the latter did not replace
        # the former as the publication authority.
        observed_refs = {ref: planned_observations.get(ref) for ref in managed}
        observed_claims = {
            source_ref: planned_observations.get(claim_ref)
            for source_ref, claim_ref in claim_refs.items()
        }
    for source_ref, claim_tip in observed_claims.items():
        if not claim_tip:
            continue
        claim = fetch_coordination_claim(
            repo_root, config['remote'], claim_refs[source_ref], claim_tip
        )
        if claim['source_ref'] != source_ref:
            raise SyncwheelError(
                f'coordination claim {claim_refs[source_ref]} names a different source ref'
            )
        if claim['coordination_id'] != config['id']:
            raise SyncwheelError(
                f'{source_ref} is claimed by coordination domain '
                f'{claim["coordination_id"]}; refusing publication'
            )
    validate_coordination_publication_base(
        repo_root,
        publication_manifest,
        config,
        expected,
        changed_refs,
        tombstone=tombstone,
        rename=rename,
        state_transition=state_transition,
        remedy_stack=remedy_stack,
        creation_remedy=creation_remedy,
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
            'claim_refs': claim_refs,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return {'status': 'dry_run', 'state_tip': None}
    installation = installation_id(create=True)
    claim_commits = {}
    for source_ref, claim_ref in claim_refs.items():
        claim_commits[source_ref] = create_coordination_claim_commit(
            repo_root,
            source_ref,
            config['id'],
            operation_token,
            observed_claims[source_ref],
            closed=bool(tombstone and source_ref == coordination_tombstone_ref(tombstone)),
            reason=(tombstone or {}).get('reason'),
        )
    state = build_coordination_state(
        repo_root,
        publication_manifest,
        config,
        expected,
        observed_refs,
        changed_refs,
        scope,
        projection_status,
        installation,
        tombstone=tombstone,
        claim_commits=claim_commits,
        operation_token=operation_token,
    )
    validate_coordination_state(state, config['id'], config.get('claims', 'advisory'))
    state_commit = create_coordination_state_commit(repo_root, state, expected['tip'])
    state_ref = coordination_state_ref(config)
    lease_refs = [*changed_refs, *claim_refs.values(), state_ref]
    expected_tips = dict(observed_refs)
    expected_tips.update({
        claim_refs[source_ref]: tip for source_ref, tip in observed_claims.items()
    })
    expected_tips[state_ref] = expected['tip']
    lease_args = [
        f"--force-with-lease={ref}:{expected_tips.get(ref) or ''}"
        for ref in sorted(lease_refs)
    ]
    refspecs = [f'{changed_refs[ref]}:{ref}' for ref in sorted(changed_refs)]
    refspecs.extend(
        f'{claim_commits[source_ref]}:{claim_refs[source_ref]}'
        for source_ref in sorted(claim_refs)
    )
    refspecs.append(f'{state_commit}:{state_ref}')
    token = acquire_local_coordination_lease(repo_root, config, installation)
    try:
        if not preflight_complete:
            atomic_push_capability_probe(repo_root, config['remote'])
        command = ['git', 'push', '--atomic', *lease_args, config['remote'], *refspecs]
        result = run_authorized_push(
            repo_root, command, config['remote'],
            [*changed_refs, *claim_refs.values(), state_ref], check=False,
        )
        if result.returncode != 0:
            latest_claims = remote_ref_tips(
                repo_root, config['remote'], claim_refs.values()
            )
            expected_claim_refs = {
                claim_refs[source_ref]: tip
                for source_ref, tip in observed_claims.items()
            }
            if latest_claims != expected_claim_refs:
                raise SyncwheelError(
                    'coordinated publish stopped after a claim lease loss; '
                    'run syncwheel handoff, inspect the named source ref claim, then retry'
                )
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
                report_advisory_unclaimed_owned_refs(config, race['latest']['state'])
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
        report_advisory_unclaimed_owned_refs(config, state)
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


def local_manifest_projection_is_convergent(repo_root, manifest, manifest_path=None):
    try:
        for stack in manifest['stacks']:
            if not branch_exists(repo_root, stack['branch']):
                return False
            if stack_reconcile_report(repo_root, manifest, stack).get('local_matches_projection') is not True:
                return False
        integration = manifest['integration']
        if not branch_exists(repo_root, integration['branch']):
            return False
        if integration_sync_report(repo_root, manifest).get('local_matches_projection') is not True:
            return False
        for channel in manifest.get('channels', []):
            tip = ref_tip(repo_root, channel['branch'])
            applied = latest_channel_event(
                repo_root, manifest_path, channel['id'], 'channel_applied'
            )
            if (
                not tip
                or not applied
                or applied.get('tip') != tip
                or applied.get('compositionDigest') != channel_composition_digest(channel)
            ):
                return False
        return True
    except SyncwheelError:
        return False


def apply_pending_coordination_merge(
    repo_root, manifest, manifest_path, *, persist=True, expected_digest=None
):
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        raise SyncwheelError('--accept-merge requires an active-active coordination manifest')
    profile, coordination = coordination_profile(repo_root)
    pending = coordination.get('pending_merge')
    if not isinstance(pending, dict) or pending.get('coordination_id') != config['id']:
        raise SyncwheelError('there is no pending mergeable coordinated publication for this manifest')
    if pending.get('local_manifest_digest') != coordination_manifest_digest(manifest, repo_root):
        raise SyncwheelError('the local manifest changed after the mergeable conflict; run handoff and resolve again')
    latest = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
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
    if expected_digest is not None and manifest_digest(updated) != expected_digest:
        raise SyncwheelError(
            'the accepted coordination merge changed after reconciliation planning; '
            'run handoff and retry'
        )
    if persist:
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
    state_info = state_info or read_remote_coordination_state(
        repo_root, config, fetch=fetch, local_manifest_version=manifest['version']
    )
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
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    governed_candidates = governed_worktree_cleanup_candidates(repo_root, manifest)
    governed_result = (
        reconcile_governed_worktrees(
            repo_root,
            manifest,
            manifest_path,
        )
        if args.apply else {'reaped': [], 'failures': []}
    )
    plan = run_coordination_gc(repo_root, manifest, apply=args.apply, fetch=args.fetch)
    plan['applied'] = bool(args.apply)
    plan['governed_worktrees'] = governed_worktree_diagnostics(repo_root, manifest)
    plan['governed_worktree_candidates'] = governed_candidates
    plan['governed_worktree_reaped'] = governed_result['reaped']
    plan['governed_worktree_failures'] = governed_result['failures']
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        if not plan['enabled']:
            print('gc: active-active coordination is not enabled')
        if governed_result['reaped']:
            print(f"gc: reaped {len(governed_result['reaped'])} governed lane(s)")
        elif governed_candidates:
            for lane in governed_candidates:
                print(f"gc: governed lane {lane['id']} [{lane['code']}] ({lane['path']})")
        elif not plan['candidates']:
            print('gc: no eligible local worktrees, branches, or backups')
        else:
            for candidate in plan['candidates']:
                print(f"gc: {candidate['type']} {candidate.get('branch') or candidate.get('path')}")
            if not args.apply:
                print('gc: dry-run; pass --apply to remove eligible local artifacts')
    return 1 if governed_result['failures'] else 0


def command_handoff(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    validation = validate_manifest(repo_root, manifest)
    config = coordination_config(manifest)
    output = {
        'manifest_path': str(manifest_path),
        'manifest_version': manifest['version'],
        'validation': validation,
        'governed_worktrees': governed_worktree_diagnostics(repo_root, manifest),
        'coordination': {'mode': 'legacy'},
    }
    if config:
        output['coordination'] = dict(config)
    if config and config.get('mode') == 'active-active':
        state_info = read_remote_coordination_state(
            repo_root, config, fetch=args.fetch, local_manifest_version=manifest['version']
        )
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


def configured_worktree_path(repo_root, branch, worktree_root):
    if worktree_root:
        safe = branch.replace('/', '-').replace('\\', '-')
        return resolve_worktree_root_path(repo_root, worktree_root) / safe
    return default_worktree_path(repo_root, branch)


def find_worktree_record_for_branch(repo_root, branch):
    for worktree in get_worktrees(repo_root):
        if worktree.get('branch') == branch:
            return worktree
    return None


def find_worktree_for_branch(repo_root, branch):
    worktree = find_worktree_record_for_branch(repo_root, branch)
    return Path(worktree['path']) if worktree else None


def resolve_git_worktree(repo_root, branch, manifest, worktree=None, auto_worktree=False):
    found = find_worktree_for_branch(repo_root, branch)
    if found:
        return found
    if worktree:
        path = Path(worktree).expanduser().resolve()
        run(['git', 'worktree', 'add', str(path), branch], cwd=repo_root)
        return path
    if auto_worktree:
        path = configured_worktree_path(repo_root, branch, effective_worktree_root(manifest))
        run(['git', 'worktree', 'add', str(path), branch], cwd=repo_root)
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


def resolve_stack_rebuild_location(repo_root, manifest, stack, args):
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
    return configured_worktree_path(
        repo_root, stack['branch'], effective_worktree_root(manifest)
    ), False


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
    return configured_worktree_path(
        repo_root, integration['branch'], effective_worktree_root(manifest)
    ), False


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


def normalize_replay_mode(value, source):
    if value not in REPLAY_MODE_CHOICES:
        allowed = ', '.join(REPLAY_MODE_CHOICES)
        raise SyncwheelError(f'invalid {source}: {value!r}; expected one of: {allowed}')
    return value


def configured_replay_mode(repo_root, manifest):
    """Return the configured default and where it came from, most specific first."""
    profile_mode = load_repo_profile(repo_root).get('replay_mode')
    if profile_mode is not None:
        return normalize_replay_mode(profile_mode, f'{PROFILE_FILENAME} replay_mode'), 'profile'
    manifest_mode = ((manifest or {}).get('defaults') or {}).get('replay_mode')
    if manifest_mode is not None:
        return normalize_replay_mode(manifest_mode, 'manifest defaults.replay_mode'), 'manifest'
    return 'auto', 'builtin'


def auto_replay_mode(
    repo_root,
    branch,
    location,
    *,
    explicit_worktree=False,
    plumbing_supported=True,
):
    """Pick the cheapest mode that applies. Unavailability falls back, never fails."""
    _worktree, in_place = location
    if in_place:
        return 'in-place'
    if explicit_worktree or find_worktree_for_branch(repo_root, branch):
        return 'desk'
    if plumbing_supported and git_supports_write_tree(repo_root):
        return 'plumbing'
    return 'ephemeral'


def select_replay_mode(
    repo_root,
    manifest,
    args,
    branch,
    location,
    *,
    plumbing_supported=True,
):
    """Four-tier selection: command flags, repo profile, manifest default, auto."""
    mode = requested_replay_mode(args)
    explicit_worktree = bool(getattr(args, 'worktree', None))
    if mode is None and (explicit_worktree or getattr(args, 'in_place', False)):
        # --worktree and --in-place are command flags too, so they outrank a
        # configured default and let auto read the location they asked for.
        mode = 'auto'
    if mode is None:
        mode = configured_replay_mode(repo_root, manifest)[0]
    if mode == 'auto':
        mode = auto_replay_mode(
            repo_root,
            branch,
            location,
            explicit_worktree=explicit_worktree,
            plumbing_supported=plumbing_supported,
        )
    return resolve_replay_mode(repo_root, location, mode)


def integration_supports_plumbing(manifest):
    return manifest['integration'].get('strategy', 'cherry-pick') == 'cherry-pick'


def resolve_replay_mode(repo_root, location, requested_mode):
    """Map an already-resolved replay location to an available execution mode."""
    if requested_mode not in REPLAY_MODE_CHOICES:
        raise SyncwheelError(f'unsupported replay mode: {requested_mode}')
    if requested_mode == 'auto':
        raise SyncwheelError('replay mode auto must be resolved before this point')
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
    """Read and validate the explicit --replay-mode flag; None when unset."""
    mode = getattr(args, 'replay_mode', None)
    if mode is None:
        return None
    normalize_replay_mode(mode, '--replay-mode')
    if mode in ('ephemeral', 'plumbing'):
        if getattr(args, 'in_place', False):
            raise SyncwheelError(f'use either --replay-mode {mode} or --in-place, not both')
        if getattr(args, 'worktree', None):
            raise SyncwheelError(f'use either --replay-mode {mode} or --worktree, not both')
    return mode


def require_nonempty_desk_stack_rebuild(stack, mode):
    """Reject desk rebuilds before an empty stack can materialize a branch."""
    if mode != 'desk' or stack['commits']:
        return
    raise SyncwheelError(
        f"stack {stack['id']!r} has no declared commits; cannot rebuild it with replay mode desk. "
        'Author on the integration branch, then capture the integration commit(s) into the stack.'
    )


def require_journal_manifest(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    if manifest.get('repository_mode') != 'journal':
        raise SyncwheelError('journal commands require manifest repository_mode="journal"')
    return repo_root, manifest, manifest_path


def journal_path_matches(path, patterns):
    path = path.replace(os.sep, '/')
    return any(
        pattern == '**'
        or fnmatch.fnmatchcase(path, pattern)
        or (pattern.startswith('**/') and fnmatch.fnmatchcase(path, pattern[3:]))
        for pattern in patterns
    )


def journal_path_sensitive(path):
    parts = Path(path).parts
    return any(part.lower() in JOURNAL_SENSITIVE_PARTS for part in parts)


def journal_secret_reason(content):
    for label, pattern in JOURNAL_SECRET_PATTERNS:
        if pattern.search(content):
            return label
    return None


def journal_status_entries(repo_root):
    result = git(
        repo_root, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
        '--ignore-submodules=all', '--no-renames',
    )
    tokens = result.stdout.split('\0')
    entries = []
    index = 0
    while index < len(tokens):
        record = tokens[index]
        index += 1
        if not record:
            continue
        status = record[:2]
        path = record[3:]
        if status[0] in {'R', 'C'}:
            if index >= len(tokens):
                raise SyncwheelError('could not parse git status rename record')
            path = tokens[index]
            index += 1
        entries.append({'status': status, 'path': path})
    return entries


def journal_real_index_clean(repo_root):
    return git(repo_root, 'diff', '--cached', '--quiet', 'HEAD', '--', check=False).returncode == 0


def journal_file_fingerprint(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SyncwheelError('unsupported file type')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            content = handle.read()
    finally:
        os.close(descriptor)
    return (
        file_stat.st_mode, file_stat.st_size, file_stat.st_mtime_ns,
        file_stat.st_dev, file_stat.st_ino, hashlib.sha256(content).hexdigest(),
    )


def journal_read_admitted_file(path, max_file_bytes):
    path_stat = os.lstat(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise SyncwheelError('unsupported file type')
        if (path_stat.st_dev, path_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
            raise SyncwheelError('content changed before reading')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            before = os.fstat(handle.fileno())
            content = handle.read(max_file_bytes + 1)
            after = os.fstat(handle.fileno())
    finally:
        os.close(descriptor)
    if len(content) > max_file_bytes or after.st_size > max_file_bytes:
        raise SyncwheelError(f'oversize ({after.st_size} bytes)')
    stable_fields = ('st_mode', 'st_size', 'st_mtime_ns', 'st_ino', 'st_dev')
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise SyncwheelError('content changed while reading')
    mode = '100755' if before.st_mode & 0o111 else '100644'
    fingerprint = (
        before.st_mode, before.st_size, before.st_mtime_ns,
        before.st_dev, before.st_ino, hashlib.sha256(content).hexdigest(),
    )
    return content, fingerprint, mode


def journal_admission(repo_root, journal):
    admitted = []
    excluded = []
    rejected = []
    fingerprints = {}
    blobs = {}
    for entry in journal_status_entries(repo_root):
        path = entry['path']
        if not journal_path_matches(path, journal['include']) or journal_path_matches(path, journal['exclude']):
            excluded.append(entry)
            continue
        if journal_path_sensitive(path):
            rejected.append({**entry, 'reason': 'sensitive path'})
            continue
        target = repo_root / path
        deleted = entry['status'][1] == 'D' or (entry['status'] == ' D') or not target.exists()
        if deleted:
            fingerprints[path] = None
        else:
            try:
                content, fingerprint, mode = journal_read_admitted_file(
                    target, journal['max_file_bytes']
                )
            except (OSError, SyncwheelError) as exc:
                rejected.append({**entry, 'reason': str(exc)})
                continue
            secret = journal_secret_reason(content)
            if secret:
                rejected.append({**entry, 'reason': f'high-confidence secret: {secret}'})
                continue
            fingerprints[path] = fingerprint
            blobs[path] = {'content': content, 'mode': mode}
        admitted.append(entry)
    return admitted, excluded, rejected, fingerprints, blobs


@contextlib.contextmanager
def journal_lock(repo_root):
    if fcntl is None:
        raise SyncwheelError(f'journal snapshot locking is unsupported on {sys.platform}')
    git_dir = Path(git(repo_root, 'rev-parse', '--absolute-git-dir').stdout.strip())
    lock_path = git_dir / 'syncwheel-journal.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SyncwheelError(f'journal snapshot already running: {lock_path}') from exc
        yield lock_path


@contextlib.contextmanager
def journal_index_lock(repo_root):
    git_dir = Path(git(repo_root, 'rev-parse', '--absolute-git-dir').stdout.strip())
    index_path = git_dir / 'index'
    lock_path = git_dir / 'index.lock'
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SyncwheelError(f'journal could not lock real index: {lock_path}') from exc
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            if index_path.exists():
                handle.write(index_path.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        yield index_path, lock_path
    finally:
        lock_path.unlink(missing_ok=True)


def journal_snapshot(repo_root, manifest, apply=False):
    journal = manifest['journal']
    branch = journal['branch']
    if get_current_branch(repo_root) != branch:
        raise SyncwheelError(f'journal branch must be checked out: expected {branch!r}')
    if not journal_real_index_clean(repo_root):
        raise SyncwheelError('journal snapshot requires a clean real index')
    parent = git(repo_root, 'rev-parse', 'HEAD').stdout.strip()
    admitted, excluded, rejected, fingerprints, blobs = journal_admission(repo_root, journal)
    result = {
        'mode': 'apply' if apply else 'plan',
        'branch': branch,
        'parent': parent,
        'admitted': admitted,
        'excluded': excluded,
        'rejected': rejected,
        'commit': None,
        'changed': False,
    }
    if rejected:
        reasons = ', '.join(f"{item['path']}: {item['reason']}" for item in rejected)
        raise SyncwheelError(f'journal snapshot rejected content: {reasons}')
    if not apply or not admitted:
        return result
    with journal_lock(repo_root), journal_index_lock(repo_root) as (real_index_path, real_index_lock):
        if git(repo_root, 'rev-parse', 'HEAD').stdout.strip() != parent:
            raise SyncwheelError('journal HEAD changed during snapshot; retry')
        if not journal_real_index_clean(repo_root):
            raise SyncwheelError('journal real index changed during snapshot; retry')
        with tempfile.NamedTemporaryFile(prefix='syncwheel-journal-index-', delete=False) as handle:
            index_path = Path(handle.name)
        index_path.unlink(missing_ok=True)
        env = {'GIT_INDEX_FILE': str(index_path)}
        try:
            git(repo_root, 'read-tree', parent, env=env)
            for entry in admitted:
                path = entry['path']
                blob = blobs.get(path)
                if blob is None:
                    git(repo_root, 'update-index', '--force-remove', '--', path, env=env)
                    continue
                with tempfile.NamedTemporaryFile(prefix='syncwheel-journal-blob-', delete=False) as blob_file:
                    blob_path = Path(blob_file.name)
                    blob_file.write(blob['content'])
                try:
                    object_id = git(repo_root, 'hash-object', '-w', str(blob_path)).stdout.strip()
                finally:
                    blob_path.unlink(missing_ok=True)
                git(
                    repo_root, 'update-index', '--add', '--cacheinfo',
                    f"{blob['mode']},{object_id},{path}", env=env,
                )
            tree = git(repo_root, 'write-tree', env=env).stdout.strip()
            parent_tree = git(repo_root, 'rev-parse', f'{parent}^{{tree}}').stdout.strip()
            if tree == parent_tree:
                return result
            for path, before in fingerprints.items():
                target = repo_root / path
                if before is None:
                    stable = not target.exists()
                else:
                    try:
                        stable = target.exists() and journal_file_fingerprint(target) == before
                    except (OSError, SyncwheelError):
                        stable = False
                if not stable:
                    raise SyncwheelError(f'journal content changed during snapshot: {path}; retry')
            if git(repo_root, 'rev-parse', 'HEAD').stdout.strip() != parent:
                raise SyncwheelError('journal HEAD changed during snapshot; retry')
            timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
            message = f'journal: snapshot {timestamp}'
            commit_cmd = with_git_identity(
                repo_root, ['git', 'commit-tree', tree, '-p', parent, '-m', message]
            )
            commit = run(commit_cmd, cwd=repo_root).stdout.strip()
            update = git(repo_root, 'update-ref', f'refs/heads/{branch}', commit, parent, check=False)
            if update.returncode != 0:
                raise SyncwheelError('journal HEAD changed before commit publication; retry')
            try:
                git(repo_root, 'read-tree', commit, env={'GIT_INDEX_FILE': str(real_index_lock)})
                os.replace(real_index_lock, real_index_path)
            except Exception as exc:
                rollback = git(
                    repo_root, 'update-ref', f'refs/heads/{branch}', parent, commit, check=False
                )
                if rollback.returncode != 0:
                    raise SyncwheelError(
                        'journal index realignment failed and branch rollback also failed; STOP'
                    ) from exc
                raise SyncwheelError('journal index realignment failed; branch update rolled back') from exc
            result.update({'commit': commit, 'changed': True})
            return result
        finally:
            index_path.unlink(missing_ok=True)


def journal_remote_tip(repo_root, remote, branch):
    result = git(repo_root, 'ls-remote', '--heads', remote, f'refs/heads/{branch}', check=False)
    if result.returncode != 0:
        raise SyncwheelError(result.stderr.strip() or f'could not observe journal remote {remote}')
    line = result.stdout.strip()
    return line.split()[0] if line else None


def command_journal_status(args):
    repo_root, manifest, _ = require_journal_manifest(args)
    journal = manifest['journal']
    admitted, excluded, rejected, _, _ = journal_admission(repo_root, journal)
    payload = {
        'repository_mode': 'journal',
        'branch': journal['branch'],
        'remote': journal['remote'],
        'interval': journal['interval'],
        'max_file_bytes': journal['max_file_bytes'],
        'current_branch': get_current_branch(repo_root),
        'index_clean': journal_real_index_clean(repo_root),
        'head': git(repo_root, 'rev-parse', 'HEAD').stdout.strip(),
        'remote_tip': journal_remote_tip(repo_root, journal['remote'], journal['branch']),
        'admitted': admitted,
        'excluded': excluded,
        'rejected': rejected,
    }
    print(json.dumps(payload, indent=2) if args.json else '\n'.join(
        f'{key}: {value}' for key, value in payload.items()
    ))
    return 0


def command_journal_snapshot(args):
    repo_root, manifest, _ = require_journal_manifest(args)
    payload = journal_snapshot(repo_root, manifest, apply=args.apply)
    print(json.dumps(payload, indent=2))
    return 0


def command_journal_publish(args):
    repo_root, manifest, _ = require_journal_manifest(args)
    journal = manifest['journal']
    observed = journal_remote_tip(repo_root, journal['remote'], journal['branch'])
    tracking_ref = f"refs/remotes/{journal['remote']}/{journal['branch']}"
    expected_parent = ref_tip(repo_root, tracking_ref) or observed
    if observed != expected_parent:
        raise SyncwheelError(
            f'journal remote tip mismatch; expected parent {expected_parent}, observed {observed or "missing"}; STOP'
        )
    if not args.apply:
        payload = journal_snapshot(repo_root, manifest, apply=False)
        payload.update({'remote': journal['remote'], 'expected_remote_tip': expected_parent})
        print(json.dumps(payload, indent=2))
        return 0
    local_head = git(repo_root, 'rev-parse', 'HEAD').stdout.strip()
    if expected_parent and git(
        repo_root, 'merge-base', '--is-ancestor', expected_parent, local_head, check=False
    ).returncode != 0:
        raise SyncwheelError('journal local branch diverged from the expected remote parent; STOP')
    snapshot = journal_snapshot(repo_root, manifest, apply=True)
    tip = snapshot['commit'] or local_head
    if tip != observed:
        refspec = f'{tip}:refs/heads/{journal["branch"]}'
        expected_ref = observed or ''
        lease = f'--force-with-lease=refs/heads/{journal["branch"]}:{expected_ref}'
        pushed = run_authorized_push(
            repo_root,
            ['git', 'push', '--porcelain', lease, journal['remote'], refspec],
            journal['remote'],
            [f'refs/heads/{journal["branch"]}'],
            check=False,
        )
        if pushed.returncode != 0:
            raise SyncwheelError('journal publish lease lost; STOP without merge, reset, rebase, or force')
    snapshot.update({'remote': journal['remote'], 'expected_remote_tip': expected_parent, 'published_tip': tip})
    print(json.dumps(snapshot, indent=2))
    return 0


def journal_unit_id(repo_root):
    safe = ''.join(char.lower() if char.isalnum() else '-' for char in repo_root.name).strip('-') or 'repo'
    digest = hashlib.sha256(str(repo_root.resolve()).encode()).hexdigest()[:12]
    return f'syncwheel-journal-{safe[:32]}-{digest}'


def journal_systemd_paths(repo_root):
    root = Path(os.environ.get('SYNCWHEEL_SYSTEMD_USER_DIR', Path.home() / '.config/systemd/user'))
    unit_id = journal_unit_id(repo_root)
    return unit_id, root / f'{unit_id}.service', root / f'{unit_id}.timer'


def journal_executable():
    configured = os.environ.get('SYNCWHEEL_EXECUTABLE')
    candidate = configured or shutil.which('syncwheel') or sys.argv[0]
    return str(Path(candidate).expanduser().resolve())


def journal_systemctl(*args, check=True):
    executable = os.environ.get('SYNCWHEEL_SYSTEMCTL', 'systemctl')
    return run([executable, '--user', *args], check=check)


def journal_unit_contents(repo_root, manifest):
    executable = journal_executable()
    repo = str(repo_root.resolve())
    interval = manifest['journal']['interval']
    def systemd_quote(value):
        return '"' + value.replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"') + '"'

    service = '\n'.join([
        '[Unit]', 'Description=Syncwheel journal snapshot and publish', '',
        '[Service]', 'Type=oneshot',
        f'ExecStart={systemd_quote(executable)} journal publish --repo {systemd_quote(repo)} --apply', '',
    ])
    timer = '\n'.join([
        '[Unit]', 'Description=Run Syncwheel journal periodically', '',
        '[Timer]', f'OnUnitInactiveSec={interval}', 'Persistent=true', '',
        '[Install]', 'WantedBy=timers.target', '',
    ])
    return service, timer


def require_linux_scheduler():
    if not sys.platform.startswith('linux'):
        raise SyncwheelError(f'journal scheduler is unsupported on {sys.platform}')


def command_journal_schedule(args):
    require_linux_scheduler()
    repo_root, manifest, _ = require_journal_manifest(args)
    unit_id, service_path, timer_path = journal_systemd_paths(repo_root)
    service, timer = journal_unit_contents(repo_root, manifest)
    managed_files = ((service_path, service), (timer_path, timer))
    payload = {
        'mode': 'apply' if args.apply else 'plan', 'unit_id': unit_id,
        'service_path': str(service_path), 'timer_path': str(timer_path),
        'command': args.schedule_command,
    }
    if args.schedule_command == 'install':
        payload['managed_ref_guard'] = install_managed_push_hook(repo_root, apply=False)
    if args.schedule_command == 'status':
        payload['installed'] = service_path.read_text() == service if service_path.exists() else False
        payload['timer_installed'] = timer_path.read_text() == timer if timer_path.exists() else False
        enabled = journal_systemctl('is-enabled', f'{unit_id}.timer', check=False)
        payload['enabled'] = enabled.returncode == 0
    elif args.schedule_command == 'install' and args.apply:
        conflicts = [str(path) for path, content in managed_files if path.exists() and path.read_text() != content]
        if conflicts:
            raise SyncwheelError('journal scheduler unit collision; refusing overwrite: ' + ', '.join(conflicts))
        payload['managed_ref_guard'] = install_managed_push_hook(repo_root, apply=True)
        service_path.parent.mkdir(parents=True, exist_ok=True)
        service_path.write_text(service)
        timer_path.write_text(timer)
        journal_systemctl('daemon-reload')
        journal_systemctl('enable', '--now', f'{unit_id}.timer')
    elif args.schedule_command == 'remove' and args.apply:
        conflicts = [str(path) for path, content in managed_files if path.exists() and path.read_text() != content]
        if conflicts:
            raise SyncwheelError('journal scheduler unit collision; refusing removal: ' + ', '.join(conflicts))
        if not service_path.exists() and not timer_path.exists():
            print(json.dumps(payload, indent=2))
            return 0
        journal_systemctl('disable', '--now', f'{unit_id}.timer')
        service_path.unlink(missing_ok=True)
        timer_path.unlink(missing_ok=True)
        journal_systemctl('daemon-reload')
    print(json.dumps(payload, indent=2))
    return 0


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
    details = {'stacks': [], 'channels': [], 'integration': {}, 'coordination': {}}
    hooks = managed_push_guard_policy(repo_root, manifest)
    details['hooks'] = hooks
    if hooks['disabled']:
        warnings.append(f"managed repository guards explicitly disabled: {hooks['disabledReason']}")
    elif hooks['migrationPending']:
        warnings.append(
            'managed repository guards required; this clone is pending migration. '
            'The next mutating Syncwheel command will install them automatically; '
            'use `syncwheel hooks install --apply` to install now'
        )
    elif hooks['required'] and not hooks['ready']:
        warnings.append(
            'managed repository guards are missing, stale, or tampered. '
            'The next mutating Syncwheel command will repair them or stop on a chaining conflict'
        )
    if manifest.get('repository_mode') == 'journal':
        journal = manifest['journal']
        details['journal'] = dict(journal)
        if not remote_is_configured(repo_root, journal['remote']):
            errors.append(f"journal remote is not configured locally: {journal['remote']}")
        if get_current_branch(repo_root) != journal['branch']:
            warnings.append(
                f"journal branch is not checked out: expected {journal['branch']!r}"
            )
        return {'warnings': warnings, 'errors': errors, 'details': details}
    coordination = coordination_config(manifest)
    if manifest.get('version') in {MANIFEST_VERSION_COORDINATED, MANIFEST_VERSION_CHANNELS}:
        if not coordination:
            errors.append(f"manifest version {manifest.get('version')} requires coordination")
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
    integration_projection_patch_ids = set()
    if not ref_exists(repo_root, integration['base']):
        errors.append(f"integration base ref does not exist: {integration['base']}")
    if not integration_exists:
        warnings.append(f'integration branch is missing locally: {integration_branch}')
    elif ref_exists(repo_root, integration['base']):
        integration_projection_patch_ids = {
            patch_id
            for commit in rev_list(repo_root, f"{integration['base']}..{integration_branch}")
            if (patch_id := commit_patch_id(repo_root, commit))
        }
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
        publication_remote = stack.get('publication_remote') or manifest['defaults']['publication_remote']
        remote_ref = f"{publication_remote}/{stack['branch']}"
        item = {
            'id': stack['id'],
            'branch': stack['branch'],
            'state': state,
            'meta': stack.get('meta', {}),
            'branch_exists': branch_exists(repo_root, stack['branch']),
            'base_exists': ref_exists(repo_root, stack['base']),
            'target': f"{stack['target_remote']}/{stack['target_branch']}",
            'remote_ref': remote_ref,
            'remote_exists': ref_exists(repo_root, remote_ref),
            'remote_relation': None,
            'missing_from_branch': [],
            'branch_commits': [],
            'undeclared_branch_commits': [],
            'remote_commits': [],
            'undeclared_remote_commits': [],
            'missing_from_integration': [],
            'missing_commits': [],
            'integration_commits': stack_integration_commits(stack),
        }
        stack_declared_shas = set()
        stack_declared_patch_ids = set()
        branch_patch_ids = set()
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
            stack_declared_shas.add(commit_full_sha(repo_root, commit))
            patch_id = commit_patch_id(repo_root, commit)
            if patch_id:
                stack_declared_patch_ids.add(patch_id)
        if item['branch_exists'] and item['base_exists']:
            item['branch_commits'] = [
                commit_full_sha(repo_root, commit)
                for commit in rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
            ]
            for commit in item['branch_commits']:
                patch_id = commit_patch_id(repo_root, commit)
                if patch_id:
                    branch_patch_ids.add(patch_id)
                if commit not in stack_declared_shas and (
                    not patch_id or patch_id not in stack_declared_patch_ids
                ):
                    item['undeclared_branch_commits'].append(commit)
            if item['undeclared_branch_commits']:
                warnings.append(
                    f"stack {stack['id']} branch contains "
                    f"{len(item['undeclared_branch_commits'])} undeclared commit(s)"
                )
        if item['branch_exists']:
            for commit in stack['commits']:
                if not commit_exists(repo_root, commit):
                    continue
                if branch_contains(repo_root, stack['branch'], commit):
                    continue
                patch_id = commit_patch_id(repo_root, commit)
                if not patch_id or patch_id not in branch_patch_ids:
                    item['missing_from_branch'].append(commit)
        if item['remote_exists'] and item['base_exists']:
            item['remote_commits'] = [
                commit_full_sha(repo_root, commit)
                for commit in rev_list(repo_root, f"{stack['base']}..{remote_ref}")
            ]
            for commit in item['remote_commits']:
                patch_id = commit_patch_id(repo_root, commit)
                if commit not in stack_declared_shas and (
                    not patch_id or patch_id not in stack_declared_patch_ids
                ):
                    item['undeclared_remote_commits'].append(commit)
            if item['undeclared_remote_commits']:
                warnings.append(
                    f"stack {stack['id']} remote branch contains "
                    f"{len(item['undeclared_remote_commits'])} undeclared commit(s)"
                )
        if item['branch_exists'] and item['remote_exists']:
            ahead, behind = rev_left_right_count(repo_root, stack['branch'], remote_ref)
            if ahead == 0 and behind == 0:
                item['remote_relation'] = 'aligned'
            elif ahead == 0:
                item['remote_relation'] = 'local_behind'
            elif behind == 0:
                item['remote_relation'] = 'local_ahead'
            else:
                item['remote_relation'] = 'diverged'
        elif item['branch_exists']:
            item['remote_relation'] = 'local_only'
        elif item['remote_exists']:
            item['remote_relation'] = 'remote_only'
        else:
            item['remote_relation'] = 'missing'
        if (
            state == 'published'
            and remote_is_configured(repo_root, publication_remote)
            and item['remote_relation'] != 'aligned'
        ):
            warnings.append(
                f"published stack {stack['id']} branch is not aligned with {remote_ref}: "
                f"{item['remote_relation']}"
            )
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
                patch_id = commit_patch_id(repo_root, commit)
                if not patch_id or patch_id not in integration_projection_patch_ids:
                    item['missing_from_integration'].append(commit)
        details['stacks'].append(item)

    for channel in manifest.get('channels', []):
        item = {
            'id': channel['id'],
            'branch': channel['branch'],
            'lifecycle': channel['lifecycle'],
            'branch_exists': branch_exists(repo_root, channel['branch']),
            'base_exists': ref_exists(repo_root, channel['base']),
            'base_revision_exists': commit_exists(repo_root, channel['baseRevision']),
            'base_drifted': ref_tip(repo_root, channel['base']) != channel['baseRevision'],
            'remote_configured': remote_is_configured(repo_root, channel['remote']),
            'composition_digest': channel_composition_digest(channel),
            'drifted_stacks': [],
            'missing_commits': [],
            'expired': False,
        }
        if not item['base_exists']:
            errors.append(f"channel {channel['id']} base ref does not exist: {channel['base']}")
        if not item['base_revision_exists']:
            errors.append(
                f"channel {channel['id']} pinned base revision does not exist: "
                f"{channel['baseRevision']}"
            )
        if coordination and channel['branch'] == coordination['state_branch']:
            errors.append(
                f"channel {channel['id']} branch overlaps coordination.state_branch"
            )
        if item['base_drifted']:
            warnings.append(
                f"channel {channel['id']} symbolic base moved; use channel refresh to repin deliberately"
            )
        if not item['remote_configured']:
            errors.append(f"channel {channel['id']} remote is not configured: {channel['remote']}")
        if not item['branch_exists']:
            warnings.append(f"channel {channel['id']} branch missing locally: {channel['branch']}")
        for entry in channel.get('composition', []):
            for projection_error in channel_entry_projection_errors(repo_root, manifest, entry):
                errors.append(
                    f"channel {channel['id']} stack {entry['stack']}: {projection_error}"
                )
            if not commit_exists(repo_root, entry['branchRevision']):
                item['missing_commits'].append(entry['branchRevision'])
                errors.append(
                    f"channel {channel['id']} references missing branch revision: "
                    f"{entry['branchRevision']}"
                )
            for commit in entry['commits']:
                if not commit_exists(repo_root, commit):
                    item['missing_commits'].append(commit)
                    errors.append(f"channel {channel['id']} references missing commit: {commit}")
            current = ref_tip(repo_root, entry['branch'])
            if current != entry['branchRevision']:
                item['drifted_stacks'].append({
                    'stack': entry['stack'],
                    'pinned': entry['branchRevision'],
                    'current': current,
                })
        if channel['lifecycle'] == 'ephemeral':
            expires_at = datetime.datetime.fromisoformat(
                channel['expiry']['expiresAt'].replace('Z', '+00:00')
            )
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
            item['expired'] = expires_at <= datetime.datetime.now(datetime.timezone.utc)
            if item['expired']:
                warnings.append(
                    f"ephemeral channel {channel['id']} expired at {channel['expiry']['expiresAt']}; "
                    'cleanup remains explicit with channel close'
                )
        details['channels'].append(item)

    integration_commits = []
    unmapped_commits = []
    absorbed_patch_commits = []
    control_commits = []
    derived_commits = []
    narrowed_derived_commits = []
    integration_merge_commits = []
    provenance, diverged_provenance = (
        derived_provenance_snapshot(repo_root, manifest)
        if manifest.get('version') == MANIFEST_VERSION_CHANNELS
        else ([], [])
    )
    if diverged_provenance:
        diverged_detail = '; '.join(
            json.dumps(item['paths'], ensure_ascii=True)
            + f": clone-local {item['local_commit'] or 'none'}"
            + f" vs snapshot {item['snapshot_commit'] or 'none'}"
            for item in diverged_provenance
        )
        warnings.append(
            'derived-provenance-diverged: the published coordination snapshot supersedes '
            f'clone-local derived provenance and is used instead: {diverged_detail}; '
            + derived_provenance_reset_remedy()
        )
    narrowed_derived = narrowed_derived_provenance_records(
        repo_root, manifest, provenance
    )
    narrowed_record_keys = {
        (item['operation_id'], item['commit']) for item in narrowed_derived
    }
    if integration_exists and ref_exists(repo_root, integration['base']):
        integration_commits = rev_list(repo_root, f"{integration['base']}..{integration_branch}")
        base_patch_ids = patch_ids_reachable_from_ref(repo_root, integration['base'])
        for commit in integration_commits:
            full_sha = commit_full_sha(repo_root, commit)
            if commit_parent_count(repo_root, commit) > 1:
                integration_merge_commits.append(full_sha)
                continue
            if is_manifest_only_commit(repo_root, commit):
                control_commits.append(full_sha)
                continue
            if is_derived_projection_commit(
                repo_root, manifest, commit, provenance=provenance
            ):
                derived_commits.append(full_sha)
                continue
            if (
                any(full_sha == item['commit'] for item in narrowed_derived)
                and is_provenance_bound_derived_projection_commit(
                    repo_root, full_sha, provenance
                )
            ):
                narrowed_derived_commits.append(full_sha)
                continue
            patch_id = commit_patch_id(repo_root, commit)
            if patch_id and patch_id in base_patch_ids:
                absorbed_patch_commits.append(full_sha)
                continue
            if full_sha not in declared_commit_shas and (not patch_id or patch_id not in declared_patch_ids):
                unmapped_commits.append(full_sha)
        if unmapped_commits:
            warnings.append(
                f"integration contains {len(unmapped_commits)} non-merge commit(s) "
                'not declared in any stack'
            )

    if narrowed_derived:
        narrowed_by_commit = {}
        for item in narrowed_derived:
            narrowed_by_commit.setdefault(item['commit'], []).append(item['path'])
        narrowed_detail = '; '.join(
            f'{commit}: '
            + json.dumps(sorted(set(paths)), ensure_ascii=True)
            for commit, paths in sorted(narrowed_by_commit.items())
        )
        errors.append(
            'derived-paths-narrowed: retained derived provenance is outside '
            f'integration.derived_paths: {narrowed_detail}; '
            + derived_paths_rebuild_remedy()
        )

    retained_provenance = [
        record for record in provenance
        if (record['operation_id'], record['commit']) not in narrowed_record_keys
    ]
    stale_derived = stale_derived_projection_records(
        repo_root, manifest, integration_branch, retained_provenance
    )
    if stale_derived:
        stale_paths = ', '.join(item['path'] for item in stale_derived)
        errors.append(
            'derived-projection-stale: derived projection path(s) are no longer '
            f'present on {integration_branch}: {stale_paths}; '
            'run a new Agentwheel update'
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
        'absorbed_patch_commits': absorbed_patch_commits,
        'control_commits': control_commits,
        'derived_commits': derived_commits,
        'narrowed_derived_commits': narrowed_derived_commits,
        'derived_paths_narrowed': narrowed_derived,
        'derived_provenance_diverged': diverged_provenance,
        'derived_projection_stale': stale_derived,
        'merge_commits': integration_merge_commits,
    }
    return {'errors': errors, 'warnings': warnings, 'details': details}


def planned_replay_mode(repo_root, manifest, branch, plumbing_supported=True):
    """Name the mode a rebuild of ``branch`` would take, for plan output."""
    mode = configured_replay_mode(repo_root, manifest)[0]
    if mode != 'auto':
        return mode
    return auto_replay_mode(
        repo_root,
        branch,
        (None, get_current_branch(repo_root) == branch),
        plumbing_supported=plumbing_supported,
    )


def build_plan(repo_root, manifest, validation):
    actions = []
    details = validation['details']
    integration = manifest['integration']
    integration_replay_mode = planned_replay_mode(
        repo_root,
        manifest,
        integration['branch'],
        integration_supports_plumbing(manifest),
    )
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
                'replay_mode': planned_replay_mode(repo_root, manifest, item['branch']),
                'meta': item.get('meta', {}),
            })
        if item['missing_from_integration']:
            actions.append({
                'type': 'refresh_integration_for_stack',
                'stack': item['id'],
                'branch': integration['branch'],
                'missing_commits': item['missing_from_integration'],
                'replay_mode': integration_replay_mode,
                'meta': item.get('meta', {}),
            })
    if details['integration'].get('derived_paths_narrowed'):
        narrowed = details['integration']['derived_paths_narrowed']
        actions.append({
            'type': 'derived-paths-narrowed',
            'branch': integration['branch'],
            'commits': sorted({item['commit'] for item in narrowed}),
            'paths': sorted({item['path'] for item in narrowed}),
            'remedy': derived_paths_rebuild_remedy(),
        })
    if details['integration'].get('derived_provenance_diverged'):
        diverged = details['integration']['derived_provenance_diverged']
        actions.append({
            'type': 'derived-provenance-diverged',
            'branch': integration['branch'],
            'paths': sorted({path for item in diverged for path in item['paths']}),
            'local_commits': sorted(
                {item['local_commit'] for item in diverged if item['local_commit']}
            ),
            'snapshot_commits': sorted(
                {item['snapshot_commit'] for item in diverged if item['snapshot_commit']}
            ),
            'remedy': derived_provenance_reset_remedy(),
        })
    if details['integration'].get('unmapped_commits'):
        commits = details['integration']['unmapped_commits']
        actions.append({
            'type': 'classify_integration_commits',
            'branch': integration['branch'],
            'commits': commits,
            'remedy': {
                'type': 'declare_integration_ownership',
                'commands': [
                    'syncwheel stack classify-integration <stack-id> '
                    + ' '.join(commit_short_sha(repo_root, commit) for commit in commits),
                    'syncwheel stack capture-integration <stack-id> '
                    + ' '.join(commit_short_sha(repo_root, commit) for commit in commits),
                ],
            },
        })
    if details['integration'].get('derived_projection_stale'):
        actions.append({
            'type': 'derived-projection-stale',
            'branch': integration['branch'],
            'paths': [
                item['path']
                for item in details['integration']['derived_projection_stale']
            ],
            'remedy': 'run a new Agentwheel update',
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


def ledger_stack_candidates_for_commit(
    repo_root,
    ledger_state,
    manifest,
    commit,
    local_branches,
    remote_branches,
):
    known = []
    current_ids = set(stack_map(manifest))
    seen = set()
    branch_candidates = [*local_branches, *remote_branches]
    commit_patch = commit_patch_id(repo_root, commit)
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
        dedupe_key = (stack_id, branch)
        if reasons:
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

        # A closed stack that was recorded as merged can explain a distinct SHA
        # left on an old integration projection.  The main delivery commit may
        # include additional changes, so its patch-id is not a sufficient
        # comparison; use the historical source/integration commit recorded by
        # the ledger instead.  Do not infer this for a merely closed stack.
        if not commit_patch or not str(stack.get('closed_reason') or '').startswith('merged'):
            continue
        historical_commits = [
            *(stack.get('integration_commits') or []),
            *(stack.get('commits') or []),
        ]
        for historical_commit in dict.fromkeys(historical_commits):
            if not commit_exists(repo_root, historical_commit):
                continue
            if commit_patch_id(repo_root, historical_commit) != commit_patch:
                continue
            dedupe_key = (stack_id, branch)
            existing = next(
                (candidate for candidate in known if (candidate['id'], candidate['branch']) == dedupe_key),
                None,
            )
            if existing is None:
                existing = {
                    'id': stack_id,
                    'branch': branch,
                    'base': stack.get('base'),
                    'target_remote': stack.get('target_remote'),
                    'target_branch': stack.get('target_branch'),
                    'integration_branch': stack.get('integration_branch'),
                    'reasons': [],
                }
                known.append(existing)
                seen.add(dedupe_key)
            existing['reasons'].append('patch_equivalent_historical_merged_stack')
            existing['absorbed'] = True
            existing['matched_commit'] = commit_full_sha(repo_root, historical_commit)
            existing['closed_reason'] = stack.get('closed_reason')
            break
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

        absorbed_stacks = [candidate for candidate in historical_stacks if candidate.get('absorbed')]
        if absorbed_stacks:
            if len(historical_stacks) == 1 and len({candidate['id'] for candidate in absorbed_stacks}) == 1:
                historical = absorbed_stacks[0]
                actions.append({
                    'type': 'resume_drop_absorbed_commit',
                    'commit': item['commit'],
                    'short': item['short'],
                    'subject': item['subject'],
                    'stack': historical['id'],
                    'branch': historical['branch'],
                    'matched_commit': historical['matched_commit'],
                    'reason': 'patch_equivalent_historical_merged_stack',
                })
                continue
            actions.append({
                'type': 'resume_manual_review',
                'commit': item['commit'],
                'short': item['short'],
                'subject': item['subject'],
                'reason': 'ambiguous_historical_patch_equivalence',
                'stacks': sorted({candidate['id'] for candidate in historical_stacks}),
            })
            continue

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
            repo_root,
            ledger_state,
            manifest,
            commit,
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
            expected_tip = ref_tip(repo_root, branch) or ZERO_OBJECT_ID
            steps.append(replay_exec_step([
                'git', 'update-ref', f'refs/heads/{branch}', base, expected_tip,
            ]))
            steps.append(replay_exec_step(['git', 'worktree', 'add', str(worktree), branch]))
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
                for commit in stack_integration_base_commits(stacks_by_id[stack_id])
            ] + [
                commit
                for stack_id in integration['stacks']
                for commit in stack_integration_only_commits(stacks_by_id[stack_id])
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
            for commit in stack_integration_base_commits(stacks_by_id[stack_id]):
                steps.append(replay_exec_step(
                    [*prefix, 'cherry-pick', commit],
                    replay_commit_env(repo_root, commit),
                ))
        for stack_id in integration['stacks']:
            for commit in stack_integration_only_commits(stacks_by_id[stack_id]):
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
        for stack_id in integration['stacks']:
            for commit in stack_integration_only_commits(stacks_by_id[stack_id]):
                steps.append(replay_exec_step(
                    [*prefix, 'cherry-pick', commit],
                    replay_commit_env(repo_root, commit),
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
                process_env = managed_process_env(step['env'])
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
    expected_tip = ref_tip(repo_root, branch) or ZERO_OBJECT_ID
    commands.append(['git', 'update-ref', f'refs/heads/{branch}', remote_ref, expected_tip])
    commands.append(['git', 'worktree', 'add', str(worktree), branch])
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
    if manifest_path.exists():
        existing_manifest, _ = load_manifest(repo_root, manifest_path)
        if existing_manifest and existing_manifest.get('version') == MANIFEST_VERSION_CHANNELS:
            raise SyncwheelError(
                'init refuses to replace an existing manifest version 3; '
                'close or migrate channel state through its governed commands'
            )
        if not args.force:
            raise SyncwheelError(f'manifest already exists: {manifest_path}')
    shared_manifest = not args.personal and not args.manifest
    if shared_manifest:
        require_manifest_transaction_current(manifest_path)
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
    save_manifest(manifest_path, manifest)
    append_ledger_event(repo_root, 'manifest_initialized', manifest_event_payload(manifest_path, manifest, 'init'), manifest_path)
    print(manifest_path)
    if tracking == SYNCWHEEL_TRACKING_GIT_TRACKED:
        hook_result = install_managed_push_hook(repo_root, apply=True)
        print('managed-ref guard: ' + json.dumps(hook_result, sort_keys=True))
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
    proposed['version'] = max(manifest['version'], MANIFEST_VERSION_COORDINATED)
    proposed['defaults']['publication_remote'] = remote
    proposed['coordination'] = active_coordination_config(manifest_path, remote, coordination_id)
    if existing.get('gc'):
        proposed['coordination']['gc'] = normalize_coordination_gc(existing['gc'])
    if existing.get('claims'):
        proposed['coordination']['claims'] = existing['claims']
    if not args.apply:
        print(json.dumps({
            'manifest_path': str(manifest_path),
            'migration': 'active-active',
            'coordination': proposed['coordination'],
            'remote_state_created': False,
            'managed_ref_guard': install_managed_push_hook(repo_root, apply=False),
            'dry_run': True,
        }, indent=2))
        return 0
    hook_result = None
    if tracking == SYNCWHEEL_TRACKING_GIT_TRACKED:
        hook_result = install_managed_push_hook(repo_root, apply=True)
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        proposed,
        'coordination_init',
        {'coordination_id': proposed['coordination']['id'], 'remote': remote},
    )
    print(f"coordination enabled: {proposed['coordination']['id']}")
    if hook_result:
        print('managed-ref guard: ' + json.dumps(hook_result, sort_keys=True))
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
    if existing.get('claims'):
        disabled['claims'] = existing['claims']
    proposed['version'] = max(manifest['version'], MANIFEST_VERSION_COORDINATED)
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


def command_coordination_provenance_reset(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    reason = (getattr(args, 'reason', None) or '').strip()
    if not reason:
        raise SyncwheelError(
            'coordination provenance reset requires --reason; use: '
            + derived_provenance_reset_remedy(whole_store=args.all)
        )
    with derived_provenance_store_lock(repo_root):
        if args.all:
            discarded = None
            store = default_derived_provenance_store()
        else:
            store = load_derived_provenance_store(repo_root)
            _effective, diverged = resolve_derived_provenance_overrides(
                shared_derived_provenance_records(repo_root, manifest),
                store,
                coordinated=coordination_is_active(manifest),
            )
            keys = {tuple(item['paths']) for item in diverged}
            if not keys:
                print(
                    'coordination provenance reset: no clone-local record is superseded '
                    'by the coordination snapshot'
                )
                return 0
            discarded = diverged
            store = {
                'version': DERIVED_PROVENANCE_STORE_VERSION,
                'overrides': [
                    item for item in store['overrides']
                    if tuple(item['paths']) not in keys
                ],
            }
        save_derived_provenance_store(repo_root, store)
        append_ledger_event(
            repo_root,
            'derived_provenance_reset',
            {
                'reason': reason,
                'scope': 'store' if args.all else 'diverged',
                'discarded': discarded,
            },
            manifest_path,
        )
    if args.all:
        print('coordination provenance reset: clone-local derived provenance store cleared')
    else:
        for item in discarded:
            print(
                'coordination provenance reset: discarded '
                + json.dumps(item['paths'], ensure_ascii=True)
            )
    return 0


def command_coordination_claims_backfill(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        raise SyncwheelError('coordination claims backfill requires active-active coordination')
    if args.apply and (not isinstance(args.reason, str) or not args.reason.strip()):
        raise SyncwheelError('coordination claims backfill --apply requires --reason')
    published = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    if not published.get('state'):
        raise SyncwheelError('coordination claims backfill requires a published state')
    state = published['state']
    source_refs = sorted(state['managed_refs'])
    claim_refs = {source_ref: coordination_claim_ref(source_ref) for source_ref in source_refs}
    observations = remote_ref_tips(
        repo_root, config['remote'], [*source_refs, *claim_refs.values()]
    )
    claims = {}
    create = {}
    foreign = []
    for source_ref in source_refs:
        if observations[source_ref] != state['managed_refs'][source_ref]:
            raise SyncwheelError(
                f'claims backfill refuses drifted source ref {source_ref}; '
                'run syncwheel handoff and repair transport evidence first'
            )
        claim_ref = claim_refs[source_ref]
        claim_tip = observations[claim_ref]
        if claim_tip:
            claim = fetch_coordination_claim(
                repo_root, config['remote'], claim_ref, claim_tip
            )
            if claim['coordination_id'] != config['id']:
                foreign.append(f'{source_ref} ({claim["coordination_id"]})')
                continue
            claims[source_ref] = claim_tip
            continue
        if args.apply:
            create[source_ref] = create_coordination_claim_commit(
                repo_root, source_ref, config['id'], str(uuid.uuid4()), None
            )
            claims[source_ref] = create[source_ref]
    if foreign:
        raise SyncwheelError(
            'claims backfill found foreign claim(s), none were overwritten: '
            + ', '.join(foreign)
        )
    missing = sorted(set(source_refs) - set(claims))
    if not args.apply:
        print(json.dumps({
            'coordination_id': config['id'],
            'unclaimed_owned_refs': missing,
            'apply': False,
        }, indent=2, sort_keys=True))
        return 0
    if dict(sorted(claims.items())) == dict(sorted((state.get('claims') or {}).items())):
        print(json.dumps({
            'coordination_id': config['id'], 'state_tip': published['tip'],
            'created_claims': [], 'unclaimed_owned_refs': [],
        }, indent=2, sort_keys=True))
        return 0
    child = copy.deepcopy(state)
    child['publication_id'] = str(uuid.uuid4())
    child['parent_state'] = published['tip']
    child['created_at'] = iso_utc_now()
    child['syncwheel_version'] = VERSION
    child['installation_id'] = installation_id(create=True)
    child['claims'] = dict(sorted(claims.items()))
    child['changed_refs'] = {}
    child['publication_scope'] = 'claims-backfill'
    child['projection_status'] = state.get('projection_status')
    state_commit = create_coordination_state_commit(repo_root, child, published['tip'])
    state_ref = coordination_state_ref(config)
    lease_args = [f'--force-with-lease={state_ref}:{published["tip"]}']
    refspecs = []
    for source_ref, claim_commit in sorted(create.items()):
        claim_ref = claim_refs[source_ref]
        lease_args.append(f'--force-with-lease={claim_ref}:')
        refspecs.append(f'{claim_commit}:{claim_ref}')
    refspecs.append(f'{state_commit}:{state_ref}')
    command = ['git', 'push', '--atomic', *lease_args, config['remote'], *refspecs]
    result = run_authorized_push(
        repo_root, command, config['remote'],
        [*(claim_refs[source_ref] for source_ref in create), state_ref], check=False,
    )
    if result.returncode != 0:
        raise SyncwheelError(
            'claims backfill lost its create-only CAS; run syncwheel handoff and retry'
        )
    append_ledger_event(repo_root, 'coordination_claims_backfilled', {
        'coordination_id': config['id'], 'state_tip': state_commit,
        'refs': sorted(create), 'reason': args.reason.strip(),
    }, manifest_path)
    print(json.dumps({
        'coordination_id': config['id'], 'state_tip': state_commit,
        'created_claims': sorted(create), 'unclaimed_owned_refs': [],
    }, indent=2, sort_keys=True))
    return 0


def command_coordination_repair(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    if not args.apply:
        if not args.ref:
            raise SyncwheelError('coordination repair planning requires --ref')
        if args.plan_file:
            raise SyncwheelError('--plan-file is only valid with --apply')
        plan, _ = coordination_repair_plan(repo_root, manifest, args.ref, args.freeze_backend)
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if not args.plan_file:
        raise SyncwheelError(
            'coordination repair --apply requires --plan-file with the exact reviewed plan'
        )
    try:
        plan = json.loads(Path(args.plan_file).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncwheelError(f'cannot read coordination repair plan: {exc}') from exc
    if not isinstance(plan, dict):
        raise SyncwheelError('coordination repair plan must be a JSON object')
    if args.ref and args.ref != plan.get('repairedRef'):
        raise SyncwheelError('--ref does not match the reviewed repair plan')
    if args.freeze_backend != plan.get('freezeBackend'):
        raise SyncwheelError('--freeze-backend does not match the reviewed repair plan')
    result = apply_coordination_repair_plan(repo_root, manifest, plan)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_coordination_compose(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    if not args.apply:
        if args.plan_file:
            raise SyncwheelError('--plan-file is only valid with --apply')
        if not args.stack or not args.known_base_state or not args.known_base_snapshot_digest:
            raise SyncwheelError(
                'coordination compose planning requires --stack, --known-base-state, '
                'and --known-base-snapshot-digest'
            )
        plan, _, _ = coordination_compose_stack_plan(
            repo_root,
            manifest,
            args.stack,
            args.known_base_state,
            args.known_base_snapshot_digest,
        )
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if not args.plan_file:
        raise SyncwheelError(
            'coordination compose --apply requires --plan-file with the exact reviewed plan'
        )
    try:
        plan = json.loads(Path(args.plan_file).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncwheelError(f'cannot read coordination compose plan: {exc}') from exc
    for option, key in (
        (args.stack, 'stack'),
        (args.known_base_state, 'knownBaseStateTip'),
        (args.known_base_snapshot_digest, 'knownBaseSnapshotDigest'),
    ):
        if option and option != plan.get(key):
            raise SyncwheelError('coordination compose arguments do not match the reviewed plan')
    result = apply_coordination_compose_stack_plan(
        repo_root, manifest, manifest_path, plan
    )
    print(json.dumps(result, indent=2, sort_keys=True))
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


def command_worktree_open(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    lane_id = safe_ref_segment(args.lane)
    if args.into:
        require_stack(manifest, args.into)
    branch = f'syncwheel/lane/{lane_id}'
    root = governed_worktree_root(repo_root, manifest)
    path = root / branch.replace('/', '-').replace('\\', '-')
    if not path_is_relative_to(path, root):
        raise SyncwheelError('configured worktree path escapes syncwheel_worktree_root')
    with governed_worktree_registry_lock(repo_root):
        registry, registry_path = load_governed_worktree_registry(repo_root)
        persist = governed_worktree_registry_cas_persister(repo_root, registry)
        recover_governed_worktree_registry_from_ledger(
            repo_root,
            registry,
            persist,
            manifest_path,
        )
        active = [
            item for item in registry['lanes']
            if item['state'] in {'active', 'captured_pending_cleanup'}
        ]
        if len(active) >= GOVERNED_WORKTREE_DEFAULT_CAPACITY:
            raise SyncwheelError(
                f'governed worktree capacity reached ({GOVERNED_WORKTREE_DEFAULT_CAPACITY}); '
                'capture or queue an existing lane before opening another'
                + format_remedy_suffix(governed_lane_queue_commands(manifest, active))
            )
        if any(item['id'] == lane_id for item in registry['lanes']):
            raise SyncwheelError(
                f'governed worktree lane id was already used: {lane_id}; choose a new lane id'
            )
        if find_worktree_for_branch(repo_root, branch):
            raise SyncwheelError(f'governed worktree branch is already checked out: {branch}')
        if path.exists():
            raise SyncwheelError(
                f'governed worktree path already exists and is not registered: {path}'
            )
        base = ref_tip(repo_root, 'HEAD')
        if not base:
            raise SyncwheelError('cannot open a governed worktree without a current commit')
        now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        lane = {
            'id': lane_id,
            'owner': governed_worktree_owner(),
            'path': str(path),
            'base': base,
            'branch': branch,
            'target': args.into,
            'state': 'active',
            'full': bool(args.full),
            'generation_token': uuid.uuid4().hex,
            'created_at': now.isoformat(),
            'lease_expires_at': (now + datetime.timedelta(
                seconds=GOVERNED_WORKTREE_DEFAULT_LEASE_SECONDS
            )).isoformat(),
        }
        run(['git', 'worktree', 'add', '-b', branch, str(path), base], cwd=repo_root)
        try:
            registry['lanes'].append(lane)
            persist()
        except BaseException:
            run(['git', 'worktree', 'remove', '--force', str(path)], cwd=repo_root, check=False)
            git(repo_root, 'branch', '-D', branch, check=False)
            raise
    output = {'lane': lane, 'registry_path': str(registry_path)}
    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        mode = 'full' if lane['full'] else 'light'
        print(f"opened {mode} governed worktree {lane['id']}: {lane['path']}")
        print(f"  branch: {lane['branch']}")
        print(f"  lease: {lane['lease_expires_at']}")
        if lane['target']:
            print(f"  target stack: {lane['target']}")
    return 0


def command_worktree_release(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    lane_id = safe_ref_segment(args.lane)
    reason = args.reason.strip() if isinstance(args.reason, str) else ''
    if not reason:
        raise SyncwheelError('worktree release requires a non-empty --reason')
    with governed_worktree_registry_lock(repo_root):
        registry, registry_path = load_governed_worktree_registry(repo_root)
        persist = governed_worktree_registry_cas_persister(repo_root, registry)
        recover_governed_worktree_registry_from_ledger(
            repo_root,
            registry,
            persist,
            manifest_path,
        )
        lane = next((item for item in registry['lanes'] if item['id'] == lane_id), None)
        if lane is None:
            terminal = governed_worktree_release_terminal(repo_root, lane_id, manifest_path)
            if terminal is None:
                raise SyncwheelError(f'unknown governed worktree lane: {lane_id}')
            payload = terminal.get('payload') or {}
            terminal_type = terminal.get('type')
            terminal_reason = payload.get('reason')
            note = None
            already_recorded = (
                terminal_type == 'governed_worktree_released'
                and terminal_reason == reason
            )
            if args.apply and not already_recorded:
                note = append_governed_worktree_release_note(
                    repo_root,
                    lane_id,
                    reason,
                    terminal,
                    manifest_path,
                )
            terminal_lane = {
                'id': lane_id,
                'branch': payload.get('branch'),
                'path': payload.get('path'),
                'recovery_ref': payload.get('recovery_ref'),
                'generation_token': payload.get('generation_token'),
            }
            output = {
                'lane': terminal_lane,
                'reason': reason,
                'registry_path': str(registry_path),
                'applied': bool(args.apply),
                'idempotent': True,
                'terminal': terminal,
                'terminal_type': terminal_type,
                'terminal_reason': terminal_reason,
            }
            if note is not None:
                output['note'] = note
            if args.json:
                print(json.dumps(output, indent=2, sort_keys=True))
            elif already_recorded:
                print(
                    f'already released governed worktree {lane_id}; '
                    f"terminal ledger event {terminal.get('seq')}"
                )
            else:
                print(
                    f'governed worktree {lane_id} was already cleaned up as '
                    f'{terminal_type} ({terminal_reason}); '
                    f"terminal ledger event {terminal.get('seq')}"
                )
                if note is not None:
                    print(f'  recorded release reason: {reason}')
            return 0
        if lane['state'] not in {'active', 'captured_pending_cleanup', 'reaped'}:
            raise SyncwheelError(
                f"governed worktree lane {lane_id!r} is already {lane['state']}; it cannot be released"
            )
        pending_event_type = lane.get('cleanup_event_type')
        pending_event_reason = lane.get('cleanup_event_reason')
        pending_reap = pending_event_type == 'governed_worktree_reaped'
        converting_advanced_reap = bool(
            pending_reap and lane.get('pending_reason') == 'branch_advanced'
        )
        completing_pending_reap = bool(
            pending_reap
            and not converting_advanced_reap
            and lane.get('pending_reason') in GOVERNED_WORKTREE_REAP_PENDING_REASONS
        )
        if (
            pending_event_type
            and pending_event_type != 'governed_worktree_released'
            and not converting_advanced_reap
            and not completing_pending_reap
        ):
            raise SyncwheelError(
                f"governed worktree lane {lane_id!r} is already pending as {pending_event_type}; "
                'retry with syncwheel gc --apply'
            )
        if pending_event_type == 'governed_worktree_released' and pending_event_reason != reason:
            raise SyncwheelError(
                f"governed worktree lane {lane_id!r} must retry its original --reason "
                f'{pending_event_reason!r}'
            )
        status = governed_worktree_lane_status(repo_root, manifest, lane)
        if args.apply and status.get('path_moved'):
            lane['path'] = status['path']
            persist()
            status = governed_worktree_lane_status(repo_root, manifest, lane)
        missing_abandoned_path = (
            status['code'] == 'unregistered_worktree'
            and not Path(lane['path']).resolve(strict=False).exists()
            and find_worktree_record_for_branch(repo_root, lane['branch']) is None
        )
        if status['code'] not in {
            None,
            'expired',
            'captured_pending_cleanup',
            'reaping',
            'worktree_remove_failed',
            'branch_delete_failed',
            'branch_advanced',
            'ledger_pending',
            'recovery_ref_moved',
        } and not missing_abandoned_path:
            raise SyncwheelError(
                f"cannot release governed worktree lane {lane_id!r}: {status['code']}; {status['remedy']}"
            )
        if not args.apply:
            output = {
                'lane': lane,
                'reason': reason,
                'registry_path': str(registry_path),
                'applied': False,
            }
            if args.json:
                print(json.dumps(output, indent=2, sort_keys=True))
            else:
                print(f"would release governed worktree {lane_id}: {lane['path']}")
                print('  rerun with --apply to create any recovery ref and remove the lane record')
            return 0
        released, detail = reap_governed_worktree_lane(
            repo_root,
            manifest,
            lane,
            persist=persist,
            manifest_path=manifest_path,
            event_type=(
                'governed_worktree_reaped' if completing_pending_reap
                else 'governed_worktree_released'
            ),
            event_reason=(
                (pending_event_reason or 'expired') if completing_pending_reap else reason
            ),
        )
        if not released:
            raise SyncwheelError(
                f"cannot release governed worktree lane {lane_id!r}: {detail['code']}; {detail['remedy']}"
            )
        terminal_type = lane.get('cleanup_event_type') or 'governed_worktree_reaped'
        terminal_reason = lane.get('cleanup_event_reason') or 'expired'
        try:
            governed_worktree_cleanup_checkpoint('before_terminal_ledger')
            terminal = append_governed_worktree_cleanup_event(repo_root, lane, manifest_path)
            if terminal_type != 'governed_worktree_released' or terminal_reason != reason:
                append_governed_worktree_release_note(
                    repo_root,
                    lane_id,
                    reason,
                    terminal,
                    manifest_path,
                )
            governed_worktree_cleanup_checkpoint('after_terminal_ledger')
        except Exception:
            lane['state'] = 'reaped'
            lane['pending_reason'] = 'ledger_pending'
            persist()
            raise
        registry['lanes'] = [item for item in registry['lanes'] if item['id'] != lane_id]
        persist()
        governed_worktree_cleanup_checkpoint('after_cleanup_record_removed')
    output = {
        'lane': completed_governed_worktree_lane(lane),
        'reason': reason,
        'registry_path': str(registry_path),
        'applied': True,
        'terminal_type': terminal_type,
        'terminal_reason': terminal_reason,
    }
    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print(f"released governed worktree {lane_id}: {lane['path']}")
        if terminal_type != 'governed_worktree_released':
            print(f'  completed the pending {terminal_type} ({terminal_reason})')
        if lane.get('recovery_ref'):
            print(f"  recovery ref: {lane['recovery_ref']}")
    return 0


def governed_worktree_commit_set(repo_root, lane):
    if not branch_exists(repo_root, lane['branch']):
        return set()
    return set(rev_list(repo_root, f"{lane['base']}..{lane['branch']}"))


def capture_governed_worktrees_for_stack(repo_root, manifest, stack_id, manifest_path=None):
    stack = require_stack(manifest, stack_id)
    owned = {commit_full_sha(repo_root, commit) for commit in stack['commits']}
    with governed_worktree_registry_lock(repo_root):
        registry, _ = load_governed_worktree_registry(repo_root)
        persist = governed_worktree_registry_cas_persister(repo_root, registry)
        recover_governed_worktree_registry_from_ledger(
            repo_root,
            registry,
            persist,
            manifest_path,
        )
        captured = []
        for lane in registry['lanes']:
            if lane['state'] != 'active' or lane.get('target') not in {None, stack_id}:
                continue
            commits = governed_worktree_commit_set(repo_root, lane)
            if not commits or not commits.issubset(owned):
                continue
            lane['state'] = 'captured_pending_cleanup'
            lane['captured_at'] = iso_utc_now()
            reaped, detail = reap_governed_worktree_lane(
                repo_root,
                manifest,
                lane,
                persist=persist,
                manifest_path=manifest_path,
            )
            captured.append({
                'id': lane['id'], 'state': lane['state'], 'detail': detail['code'], 'reaped': reaped,
            })
        if captured:
            persist()
        return captured


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
    profile = load_repo_profile(repo_root)
    profile['personal'] = personal
    path = save_repo_profile(repo_root, profile)
    print(f'using personal manifest: {personal}')
    print(path)
    return 0


def command_replay_mode(args):
    repo_root = resolve_repo_root(args.repo)
    profile = load_repo_profile(repo_root)
    if args.clear and args.mode:
        raise SyncwheelError('use either a mode or --clear, not both')
    if args.clear or args.mode:
        if args.clear:
            profile.pop('replay_mode', None)
        else:
            profile['replay_mode'] = normalize_replay_mode(args.mode, '--replay-mode')
        save_repo_profile(repo_root, profile)
        profile = load_repo_profile(repo_root)
    manifest, _manifest_path = load_manifest(
        repo_root, resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    )
    mode, source = configured_replay_mode(repo_root, manifest)
    report = {
        'replay_mode': mode,
        'source': source,
        'profile': profile.get('replay_mode'),
        'manifest': ((manifest or {}).get('defaults') or {}).get('replay_mode'),
        'profile_path': str(repo_profile_path(repo_root)),
    }
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"replay_mode: {report['replay_mode']} (from {report['source']})")
    print(f"profile: {report['profile'] or 'unset'} ({report['profile_path']})")
    print(f"manifest defaults.replay_mode: {report['manifest'] or 'unset'}")
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
        output['governed_worktrees'] = governed_worktree_diagnostics(repo_root, manifest)
        output['authority'] = manifest_authority(manifest)
    if args.json:
        print(json.dumps(output, indent=2))
        return 1 if manifest and not output['validation']['details']['primary_checkout']['compliant'] else 0
    print(f"repo: {snapshot['repo_root']}")
    print(f"current_branch: {snapshot['current_branch']}")
    print(f"canonical_remote_head: {snapshot['canonical_remote_head'] or 'unknown'}")
    print(f"manifest: {manifest_path if manifest else 'missing'}")
    if manifest:
        print(f"authority: {format_authority_policy(output['authority'])}")
    print('\nremotes:')
    for line in snapshot['remotes']:
        print(f'  - {line}')
    print('\nworktrees:')
    for worktree in snapshot['worktrees']:
        branch = worktree.get('branch', 'DETACHED')
        print(f"  - {worktree.get('path')} ({branch})")
    if manifest:
        governed = output['governed_worktrees']['lanes']
        print('\ngoverned worktrees:')
        if not governed:
            print('  - none')
        for lane in governed:
            detail = f" ({lane['code']})" if lane['code'] else ''
            print(f"  - {lane['id'] or lane['branch']}: state={lane['state']}{detail}")
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
        print('\nchannel state:')
        if not validation['details']['channels']:
            print('  - none')
        for item in validation['details']['channels']:
            summary = [
                'branch=present' if item['branch_exists'] else 'branch=missing',
                f"lifecycle={item['lifecycle']}",
                f"drifted_stacks={len(item['drifted_stacks'])}",
            ]
            if item['base_drifted']:
                summary.append('base_drifted=yes')
            if item['expired']:
                summary.append('expired=yes')
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
    governed = governed_worktree_diagnostics(repo_root, manifest)
    readiness_blockers = []
    if validation['errors']:
        readiness_blockers.append('validation_errors')
    if validation['warnings']:
        readiness_blockers.append('validation_warnings')
    if plan:
        readiness_blockers.append('planned_actions')
    if any(item['code'] for item in governed['lanes']):
        readiness_blockers.append('governed_worktree_warnings')
    readiness = {
        'ready': not readiness_blockers,
        'blockers': readiness_blockers,
    }
    output = {
        'snapshot': snapshot,
        'manifest_path': str(manifest_path),
        'validation': validation,
        'plan': plan,
        'governed_worktrees': governed,
        'readiness': readiness,
        'diagnostics': {
            'unmapped_integration_commits': diagnostics,
        },
    }
    if args.json:
        print(json.dumps(output, indent=2))
        return 1 if validation['errors'] or (args.strict and not readiness['ready']) else 0
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
    if args.strict:
        print(f"\nreadiness: {'READY' if readiness['ready'] else 'BLOCKED'}")
    return 1 if validation['errors'] or (args.strict and not readiness['ready']) else 0


def channel_manifest_change_output(operation, manifest_path, channel, migration=False):
    return {
        'schemaVersion': CHANNEL_PLAN_SCHEMA_VERSION,
        'operation': operation,
        'manifestPath': str(manifest_path),
        'manifestMigration': '2-to-3' if migration else None,
        'channel': manifest_channel_history_summary(channel),
        'applyRequired': True,
        'deploymentAsserted': False,
    }


def channel_summary_for_mutation(channel):
    if channel is None:
        return None
    return manifest_channel_history_summary(channel)


def build_channel_mutation_plan(
    operation,
    manifest_path,
    observed_manifest_digest,
    proposed_manifest,
    channel,
    context=None,
    migration=False,
    observation=None,
    before_channel=None,
    operation_id=None,
):
    proposed_channel = channel_summary_for_mutation(channel)
    digest_channel = channel or before_channel
    request_channel = (
        proposed_channel.get('id') if proposed_channel
        else (channel_summary_for_mutation(before_channel) or {}).get('id')
    )
    normalized_context = json.loads(json.dumps(context or {}))
    proposal = {
        'operation': operation,
        'manifestDigest': observed_manifest_digest,
        'proposedManifestDigest': manifest_digest(proposed_manifest),
        'proposedChannel': proposed_channel,
        'context': normalized_context,
    }
    observation_body = {
        'manifestDigest': observed_manifest_digest,
        **(observation or {}),
    }
    action_context = context or {}
    if operation == 'close':
        actions = []
        if action_context.get('coordinationActive'):
            actions.append({
                'id': 'publish-coordination-state',
                'type': 'publish-coordination-state',
                'target': action_context.get('coordinationTarget'),
                'before': (observation or {}).get('coordinationStateRevision'),
                'intendedAfter': {
                    'manifestDigest': action_context.get('coordinationManifestDigest'),
                },
            })
        actions.append({
            'id': 'update-channel-manifest',
            'type': 'update-channel-manifest',
            'target': str(manifest_path),
            'before': observed_manifest_digest,
            'intendedAfter': manifest_digest(proposed_manifest),
        })
        if action_context.get('deleteLocal'):
            actions.append({
                'id': 'delete-local-channel-ref',
                'type': 'delete-local-channel-ref',
                'target': action_context.get('localRef'),
                'before': (observation or {}).get('localRevision'),
                'intendedAfter': None,
            })
    else:
        actions = [{
            'id': 'update-channel-manifest',
            'type': 'update-channel-manifest',
            'target': str(manifest_path),
            'before': observed_manifest_digest,
            'intendedAfter': manifest_digest(proposed_manifest),
        }]
    plan = {
        'kind': 'channelPlan',
        'schemaVersion': CHANNEL_PLAN_SCHEMA_VERSION,
        'operation': operation,
        'request': {
            'operation': operation,
            'channel': request_channel,
            'parameters': normalized_context,
        },
        'manifestPath': str(manifest_path),
        'manifestMigration': '2-to-3' if migration else None,
        'channel': proposed_channel,
        'observationRevision': canonical_json_digest(observation_body),
        'manifestDigest': observed_manifest_digest,
        'manifestDigestBefore': observed_manifest_digest,
        'proposedManifestDigest': manifest_digest(proposed_manifest),
        'proposedStateDigest': canonical_json_digest(proposal),
        'context': normalized_context,
        'observation': observation or {},
        'actions': actions,
        'before': {
            'manifestDigest': observed_manifest_digest,
            'channel': channel_summary_for_mutation(before_channel),
        },
        'after': {
            'manifestDigest': manifest_digest(proposed_manifest),
            'channel': proposed_channel,
        },
        'pinDigest': channel_pin_digest(digest_channel) if digest_channel else None,
        'compositionDigest': (
            channel_composition_digest(digest_channel) if digest_channel else None
        ),
        'applyRequired': True,
        'deploymentAsserted': False,
    }
    return finalize_channel_plan(plan, operation_id)


def verify_channel_mutation_plan(args, plan):
    expected = getattr(args, 'plan_digest', None)
    if not isinstance(expected, str) or not expected:
        raise SyncwheelError('--plan-digest is required with --apply')
    if expected != plan['planDigest']:
        raise SyncwheelError(
            'channel mutation plan is stale; generate a new preview and use its exact planDigest'
        )


def channel_operation_payload(plan, channel=None, mutation=None):
    plan_channel = plan.get('channel')
    plan_channel_id = (
        plan_channel.get('id') if isinstance(plan_channel, dict) else plan_channel
    )
    payload = {
        'operationId': plan['operationId'],
        'planDigest': plan['planDigest'],
        'operation': plan['operation'],
        'channel': channel['id'] if channel else plan_channel_id,
        'manifestDigest': plan.get('manifestDigest'),
        'manifestDigestBefore': plan.get('manifestDigestBefore'),
        'observationRevision': plan.get('observationRevision'),
        'compositionDigest': plan.get('compositionDigest'),
        'pinDigest': plan.get('pinDigest'),
        'request': json.loads(json.dumps(plan.get('request') or {})),
        'before': plan.get('before') or {},
        'after': plan.get('after') or {},
        'context': plan.get('context') or {},
        'actions': list(plan.get('actions') or []),
        'mutation': mutation or {},
        'deploymentAsserted': False,
    }
    return payload


def record_channel_operation_prepared(repo_root, manifest_path, plan, channel=None, mutation=None):
    payload = channel_operation_payload(plan, channel, mutation)
    existing = [
        event for event in load_ledger_events(repo_root, manifest_path)
        if event.get('type') in {
            'channel_operation_started', 'channel_operation_prepared',
            'channel_operation_receipt',
        }
        and (event.get('payload') or {}).get('operationId') == plan['operationId']
    ]
    mismatched = [
        event for event in existing
        if (event.get('payload') or {}).get('planDigest') != plan['planDigest']
    ]
    if mismatched:
        raise SyncwheelError(
            f'channel operation id collision: {plan["operationId"]} is bound to another plan'
        )
    terminal = next(
        (event for event in reversed(existing)
         if event.get('type') == 'channel_operation_receipt'),
        None,
    )
    if terminal:
        terminal_payload = terminal.get('payload') or payload
        raise SyncwheelError(
            f'channel operation {plan["operationId"]} is already terminal: '
            f'{terminal_payload.get("status")}'
        )
    existing_types = {event.get('type') for event in existing}
    if existing_types & {'channel_operation_started', 'channel_operation_prepared'}:
        state = 'prepared' if 'channel_operation_prepared' in existing_types else 'started'
        raise SyncwheelError(
            f'channel operation {plan["operationId"]} is {state} without a terminal receipt; '
            'use channel operation reconcile'
        )
    if 'channel_operation_started' not in existing_types:
        started = dict(payload)
        started.update({'status': 'started', 'startedAt': iso_utc_now()})
        append_ledger_event(repo_root, 'channel_operation_started', started, manifest_path)
    else:
        started = next(
            (event.get('payload') or {} for event in existing
             if event.get('type') == 'channel_operation_started'),
            {},
        )
    if 'channel_operation_prepared' not in existing_types:
        prepared = dict(payload)
        prepared.update({
            'status': 'prepared',
            'startedAt': started.get('startedAt'),
            'preparedAt': iso_utc_now(),
            'expectedAfter': mutation or {},
        })
        append_ledger_event(repo_root, 'channel_operation_prepared', prepared, manifest_path)
    return payload


def default_channel_action_outcomes(plan, status, evidence=None, detail=None):
    actions = plan.get('actions', [])
    outcomes = []
    for index, action in enumerate(actions):
        if status == 'succeeded':
            action_status = 'succeeded'
        elif status == 'partial':
            action_status = 'succeeded' if index == 0 else (
                'failed' if index == 1 else 'not-attempted'
            )
        elif index == 0:
            action_status = status if status in {
                'failed', 'unknown', 'cancelled'
            } else 'unknown'
        else:
            action_status = 'not-attempted'
        outcomes.append({
            'id': action['id'],
            'type': action['type'],
            'target': action.get('target'),
            'before': action.get('before'),
            'intendedAfter': action.get('intendedAfter'),
            'status': action_status,
            'observedAfter': evidence or {},
            'detail': detail,
        })
    return outcomes


def reduce_channel_action_outcomes(action_outcomes, fallback='unknown'):
    statuses = [outcome.get('status') for outcome in action_outcomes]
    if not statuses:
        return fallback
    if all(status == 'succeeded' for status in statuses):
        return 'succeeded'
    if any(status == 'partial' for status in statuses):
        return 'partial'
    if any(status == 'unknown' for status in statuses):
        return 'unknown'
    if any(status == 'succeeded' for status in statuses):
        return 'partial'
    attempted = [status for status in statuses if status != 'not-attempted']
    if attempted and all(status == 'cancelled' for status in attempted):
        return 'cancelled'
    if attempted and all(status == 'failed' for status in attempted):
        return 'failed'
    return fallback


def validate_channel_action_outcomes(plan, action_outcomes):
    actions = plan.get('actions') or []
    expected_ids = [action.get('id') for action in actions]
    observed_ids = [outcome.get('id') for outcome in action_outcomes]
    if observed_ids != expected_ids or len(observed_ids) != len(set(observed_ids)):
        raise SyncwheelError(
            'channel operation receipt must contain exactly one ordered outcome per plan action'
        )
    allowed_statuses = {
        'succeeded', 'failed', 'partial', 'unknown', 'cancelled', 'not-attempted',
    }
    for action, outcome in zip(actions, action_outcomes):
        for field in ('id', 'type', 'target', 'before', 'intendedAfter'):
            if outcome.get(field) != action.get(field):
                raise SyncwheelError(
                    f'channel operation outcome {action.get("id")} does not preserve action.{field}'
                )
        if outcome.get('status') not in allowed_statuses:
            raise SyncwheelError(
                f'channel operation outcome {action.get("id")} has invalid status'
            )


def record_channel_operation_receipt(
    repo_root, manifest_path, plan, status, channel=None, detail=None, reconciled=False,
    evidence=None, action_outcomes=None,
):
    if status not in {'succeeded', 'failed', 'partial', 'unknown', 'cancelled'}:
        raise SyncwheelError(f'invalid channel operation terminal status: {status}')
    payload = channel_operation_payload(plan, channel)
    prepared = next(
        (
            event.get('payload') or {}
            for event in reversed(load_ledger_events(repo_root, manifest_path))
            if event.get('type') == 'channel_operation_prepared'
            and (event.get('payload') or {}).get('operationId') == plan['operationId']
        ),
        {},
    )
    started = next(
        (
            event.get('payload') or {}
            for event in load_ledger_events(repo_root, manifest_path)
            if event.get('type') == 'channel_operation_started'
            and (event.get('payload') or {}).get('operationId') == plan['operationId']
        ),
        {},
    )
    completed_at = iso_utc_now()
    normalized_action_outcomes = (
        action_outcomes
        if action_outcomes is not None
        else default_channel_action_outcomes(plan, status, evidence, detail)
    )
    validate_channel_action_outcomes(plan, normalized_action_outcomes)
    terminal_status = reduce_channel_action_outcomes(normalized_action_outcomes, status)
    payload.update({
        'status': terminal_status,
        'detail': detail,
        'reconciled': bool(reconciled),
        'evidence': evidence or {},
        'before': prepared.get('before') or payload.get('before') or {},
        'expectedAfter': (
            prepared.get('expectedAfter') or prepared.get('mutation')
            or plan.get('mutation') or {}
        ),
        'observedAfter': evidence or {},
        'actionOutcomes': normalized_action_outcomes,
        'startedAt': started.get('startedAt') or prepared.get('startedAt'),
        'preparedAt': prepared.get('preparedAt'),
        'completedAt': completed_at,
        'recordedAt': completed_at,
    })
    if reconciled:
        payload['reconciledAt'] = completed_at
    append_ledger_event(repo_root, 'channel_operation_receipt', payload, manifest_path)
    return payload


def finish_channel_manifest_mutation(
    repo_root, manifest_path, manifest, plan, channel, reason, context=None,
    locked_check=None,
):
    mutation = {
        'kind': 'manifest',
        'proposedManifestDigest': manifest_digest(manifest),
        'proposedChannelDigest': canonical_json_digest(
            channel_summary_for_mutation(channel)
        ) if channel else None,
    }
    channel_id = channel['id'] if channel else (plan.get('context') or {}).get('channel')
    with manifest_write_transaction(repo_root, manifest_path, channel_id or 'manifest'):
        require_locked_manifest_observation(repo_root, manifest_path, plan)
        if locked_check:
            locked_check()
        record_channel_operation_prepared(repo_root, manifest_path, plan, channel, mutation)
        try:
            channel_mutation_checkpoint()
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'cancelled', channel,
                'cancelled before authoritative manifest mutation',
                evidence={'manifestDigest': plan['manifestDigestBefore']},
            )
            raise SyncwheelError('channel mutation cancelled before authoritative change') from exc
        require_manifest_transaction_current(manifest_path)
        try:
            save_manifest(manifest_path, manifest)
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'unknown', channel,
                'interrupted after authoritative manifest mutation began',
                evidence={'manifestDigest': manifest_digest(load_manifest(repo_root, manifest_path)[0])},
            )
            raise SyncwheelError(
                f'channel mutation outcome is unknown; operation {plan["operationId"]} '
                'requires operation reconcile'
            ) from exc
        except Exception as exc:
            if isinstance(exc, ManifestDurabilityError):
                observed_manifest, _ = load_manifest(repo_root, manifest_path)
                record_channel_operation_receipt(
                    repo_root, manifest_path, plan, 'unknown', channel,
                    str(exc)[:400],
                    evidence={
                        'manifestDigest': (
                            manifest_digest(observed_manifest)
                            if observed_manifest else None
                        ),
                    },
                )
                raise SyncwheelError(
                    f'manifest durability is unknown; operation {plan["operationId"]} '
                    'requires operation reconcile'
                ) from exc
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'failed', channel, str(exc)[:400]
            )
            raise
        try:
            append_ledger_event(
                repo_root, 'manifest_saved',
                manifest_event_payload(manifest_path, manifest, reason, context), manifest_path,
            )
            receipt = record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'succeeded', channel,
                evidence={'manifestDigest': manifest_digest(manifest)},
            )
        except (KeyboardInterrupt, SystemExit) as exc:
            try:
                record_channel_operation_receipt(
                    repo_root, manifest_path, plan, 'unknown', channel,
                    'interrupted after manifest mutation completed',
                    evidence={'manifestDigest': manifest_digest(manifest)},
                )
            except Exception:
                pass
            raise SyncwheelError(
                f'channel mutation outcome is unknown; operation {plan["operationId"]} '
                'requires operation reconcile'
            ) from exc
        except Exception as exc:
            try:
                record_channel_operation_receipt(
                    repo_root, manifest_path, plan, 'unknown', channel,
                    f'manifest changed; receipt recording failed: {str(exc)[:300]}',
                    evidence={'manifestDigest': manifest_digest(manifest)},
                )
            except Exception:
                pass
            raise SyncwheelError(
                f'manifest mutation may have succeeded but receipt recording failed; '
                f'operation {plan["operationId"]} requires reconcile-outcome'
            ) from exc
    return receipt


def command_channel_list(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_delivery_manifest(manifest)
    rows = []
    for channel in manifest.get('channels', []):
        observation = channel_observation(repo_root, manifest, channel, include_remote=False)
        rows.append({
            **manifest_channel_history_summary(channel),
            'currentRevision': observation['currentRevision'],
        })
    if args.json:
        print(json.dumps({'channels': rows}, indent=2))
    else:
        for channel in rows:
            expiry = channel.get('expiry') or {}
            suffix = f"\texpires={expiry.get('expiresAt')}" if expiry else ''
            print(
                f"{channel['id']}\t{channel['branch']}\t{channel['lifecycle']}"
                f"\tstacks={len(channel['composition'])}{suffix}"
            )
    return 0


def command_channel_show(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_delivery_manifest(manifest)
    channel = require_channel(manifest, args.channel)
    output = manifest_channel_history_summary(channel)
    output['observation'] = channel_observation(repo_root, manifest, channel)
    output['deployment'] = {'asserted': False}
    print(json.dumps(output, indent=2))
    return 0


def command_channel_create(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_delivery_manifest(manifest)
    if manifest['version'] == MANIFEST_VERSION_LEGACY:
        raise SyncwheelError(
            'channel create requires a coordinated version 2 manifest; run coordination init first'
        )
    if manifest['version'] not in {MANIFEST_VERSION_COORDINATED, MANIFEST_VERSION_CHANNELS}:
        raise SyncwheelError('channel create requires manifest version 2 or 3')
    if args.channel in channel_map(manifest):
        raise SyncwheelError(f'channel already exists: {args.channel}')
    branch = args.branch or f'channel/{safe_ref_segment(args.channel)}'
    if any(channel['branch'] == branch for channel in manifest.get('channels', [])):
        raise SyncwheelError(f'channel branch already exists in manifest: {branch}')
    lifecycle = args.lifecycle
    if lifecycle == 'ephemeral' and not args.expires_at:
        raise SyncwheelError('ephemeral channel create requires --expires-at')
    if lifecycle == 'shared' and args.expires_at:
        raise SyncwheelError('--expires-at is valid only for ephemeral channels')
    duplicate_stacks = sorted({
        stack_id for stack_id in args.stack if args.stack.count(stack_id) > 1
    })
    if duplicate_stacks:
        raise SyncwheelError(
            'channel composition contains duplicate stack id(s): '
            + ', '.join(duplicate_stacks)
        )
    migration = manifest['version'] == MANIFEST_VERSION_COORDINATED
    channel_manifest = json.loads(json.dumps(manifest))
    if migration:
        channel_manifest['version'] = MANIFEST_VERSION_CHANNELS
        derive_stack_dependencies(channel_manifest['stacks'])
        validate_stack_dependency_graph(channel_manifest['stacks'])
    channel = {
        'id': args.channel,
        'branch': branch,
        'lifecycle': lifecycle,
        'base': args.base or channel_manifest['defaults']['base_ref'],
        'remote': args.remote or channel_manifest['defaults']['publication_remote'],
        'composition': [
            pin_stack_for_channel(repo_root, channel_manifest, item) for item in args.stack
        ],
    }
    if (
        coordination_is_active(channel_manifest)
        and channel['remote'] != coordination_config(channel_manifest)['remote']
    ):
        raise SyncwheelError('active-active channel remote must match coordination.remote')
    ownership_candidate = dict(channel_manifest)
    ownership_candidate['channels'] = [*channel_manifest.get('channels', []), channel]
    validate_channel_branch_ownership(ownership_candidate)
    create_ref_observation = require_new_channel_ref_unowned(repo_root, channel)
    if not ref_exists(repo_root, channel['base']):
        raise SyncwheelError(f"channel base ref does not exist: {channel['base']}")
    channel['baseRevision'] = commit_full_sha(repo_root, channel['base'])
    if lifecycle == 'ephemeral':
        channel['expiry'] = {
            'createdAt': normalize_channel_timestamp(
                git(repo_root, 'show', '-s', '--format=%cI', channel['baseRevision']).stdout.strip(),
                f'channel {args.channel} expiry.createdAt',
            ),
            'expiresAt': normalize_channel_timestamp(
                args.expires_at, f'channel {args.channel} expiry.expiresAt'
            ),
        }
    proposed = channel_manifest
    proposed.setdefault('channels', []).append(channel)
    validate_channel_dependency_order(channel)
    observed_digest = manifest_digest(manifest)
    output = build_channel_mutation_plan(
        'create', manifest_path, observed_digest, proposed, channel,
        {'branch': branch}, migration=migration, before_channel=None,
        observation={'newChannelRef': create_ref_observation},
        operation_id=channel_operation_id_from_args(args),
    )
    if not args.apply:
        print(json.dumps(output, indent=2))
        return 0
    verify_channel_mutation_plan(args, output)
    receipt = finish_channel_manifest_mutation(
        repo_root, manifest_path, proposed, output, channel, 'channel_create',
        {'channel': args.channel, 'branch': branch, 'manifest_migration': output['manifestMigration']},
        locked_check=lambda: require_new_channel_ref_unowned(
            repo_root, channel, phase='create plan is stale;'
        ),
    )
    output['applyRequired'] = False
    output['applied'] = True
    output['status'] = 'succeeded'
    output['receipt'] = receipt
    print(json.dumps(output, indent=2))
    return 0


def channel_edit_context(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_delivery_manifest(manifest)
    if manifest['version'] != MANIFEST_VERSION_CHANNELS:
        raise SyncwheelError('channel edits require manifest version 3')
    channel = require_channel(manifest, args.channel)
    return (
        repo_root, manifest, manifest_path, channel, manifest_digest(manifest),
        json.loads(json.dumps(channel)),
    )


def save_or_plan_channel_edit(
    args, repo_root, manifest, manifest_path, channel, operation, context=None,
    observed_manifest_digest=None, observation=None, before_channel=None,
):
    validate_channel_dependency_order(channel)
    require_channel_materialization_valid(repo_root, manifest, channel)
    output = build_channel_mutation_plan(
        operation, manifest_path, observed_manifest_digest, manifest, channel,
        context, observation=observation, before_channel=before_channel,
        operation_id=channel_operation_id_from_args(args),
    )
    if not args.apply:
        print(json.dumps(output, indent=2))
        return 0
    verify_channel_mutation_plan(args, output)
    receipt = finish_channel_manifest_mutation(
        repo_root, manifest_path, manifest, output, channel, f'channel_{operation}',
        {'channel': channel['id'], **(context or {})},
    )
    output['applyRequired'] = False
    output['applied'] = True
    output['status'] = 'succeeded'
    output['receipt'] = receipt
    print(json.dumps(output, indent=2))
    return 0


def command_channel_add(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root, manifest, manifest_path, channel, observed_digest, before = channel_edit_context(args)
    if any(entry['stack'] == args.stack for entry in channel['composition']):
        raise SyncwheelError(f"channel {args.channel} already contains stack {args.stack}")
    entry = pin_stack_for_channel(repo_root, manifest, args.stack)
    position = len(channel['composition']) if args.position is None else args.position
    if position < 0 or position > len(channel['composition']):
        raise SyncwheelError('--position is outside the channel composition')
    channel['composition'].insert(position, entry)
    channel.pop('resolution', None)
    return save_or_plan_channel_edit(
        args, repo_root, manifest, manifest_path, channel, 'add',
        {'stack': args.stack, 'position': position},
        observed_manifest_digest=observed_digest, before_channel=before,
    )


def command_channel_remove(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root, manifest, manifest_path, channel, observed_digest, before = channel_edit_context(args)
    before_count = len(channel['composition'])
    channel['composition'] = [
        entry for entry in channel['composition'] if entry['stack'] != args.stack
    ]
    if len(channel['composition']) == before_count:
        raise SyncwheelError(f"channel {args.channel} does not contain stack {args.stack}")
    channel.pop('resolution', None)
    return save_or_plan_channel_edit(
        args, repo_root, manifest, manifest_path, channel, 'remove', {'stack': args.stack},
        observed_manifest_digest=observed_digest, before_channel=before,
    )


def command_channel_replace(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root, manifest, manifest_path, channel, observed_digest, before = channel_edit_context(args)
    if args.old_stack != args.new_stack and any(
        entry['stack'] == args.new_stack for entry in channel['composition']
    ):
        raise SyncwheelError(f"channel {args.channel} already contains stack {args.new_stack}")
    for index, entry in enumerate(channel['composition']):
        if entry['stack'] == args.old_stack:
            channel['composition'][index] = pin_stack_for_channel(repo_root, manifest, args.new_stack)
            break
    else:
        raise SyncwheelError(f"channel {args.channel} does not contain stack {args.old_stack}")
    channel.pop('resolution', None)
    return save_or_plan_channel_edit(
        args, repo_root, manifest, manifest_path, channel, 'replace',
        {'old_stack': args.old_stack, 'new_stack': args.new_stack},
        observed_manifest_digest=observed_digest, before_channel=before,
    )


def command_channel_refresh(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root, manifest, manifest_path, channel, observed_digest, before = channel_edit_context(args)
    selected = set(args.stack or [entry['stack'] for entry in channel['composition']])
    present = {entry['stack'] for entry in channel['composition']}
    unknown = sorted(selected - present)
    if unknown:
        raise SyncwheelError('channel refresh references absent stack(s): ' + ', '.join(unknown))
    channel['composition'] = [
        pin_stack_for_channel(repo_root, manifest, entry['stack'])
        if entry['stack'] in selected else entry
        for entry in channel['composition']
    ]
    if not ref_exists(repo_root, channel['base']):
        raise SyncwheelError(f"channel base ref does not exist: {channel['base']}")
    channel['baseRevision'] = commit_full_sha(repo_root, channel['base'])
    channel.pop('resolution', None)
    return save_or_plan_channel_edit(
        args, repo_root, manifest, manifest_path, channel, 'refresh',
        {'stacks': sorted(selected)},
        observed_manifest_digest=observed_digest, before_channel=before,
    )


def command_channel_promote(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_delivery_manifest(manifest)
    if manifest['version'] != MANIFEST_VERSION_CHANNELS:
        raise SyncwheelError('channel promote requires manifest version 3')
    source = require_channel(manifest, args.source)
    target = require_channel(manifest, args.target)
    observed_digest = manifest_digest(manifest)
    before = json.loads(json.dumps(target))
    target['base'] = source['base']
    target['baseRevision'] = source['baseRevision']
    target['composition'] = json.loads(json.dumps(source['composition']))
    if source.get('resolution'):
        target['resolution'] = json.loads(json.dumps(source['resolution']))
    else:
        target.pop('resolution', None)
    return save_or_plan_channel_edit(
        args, repo_root, manifest, manifest_path, target, 'promote',
        {'source': source['id'], 'target': target['id'], 'copiedCompositionDigest': channel_composition_digest(source)},
        observed_manifest_digest=observed_digest, before_channel=before,
    )


def command_channel_resolve(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root, manifest, manifest_path, channel, observed_digest, before = channel_edit_context(args)
    if args.clear:
        if 'resolution' not in channel:
            raise SyncwheelError(f'channel {args.channel} has no resolution to clear')
        cleared = channel.pop('resolution')
        return save_or_plan_channel_edit(
            args, repo_root, manifest, manifest_path, channel, 'resolve-clear',
            {'clearedRevision': cleared['revision']},
            observed_manifest_digest=observed_digest, before_channel=before,
        )
    revision = args.revision
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise SyncwheelError('--revision must be a full commit SHA')
    if not commit_exists(repo_root, revision):
        raise SyncwheelError(f'channel resolution revision does not exist: {revision}')
    parents = git(repo_root, 'rev-list', '--parents', '-n', '1', revision).stdout.split()
    if len(parents) != 2 or parents[1] != channel['baseRevision']:
        raise SyncwheelError(
            'channel resolution revision must be single-parent directly on channel baseRevision'
        )
    channel['resolution'] = {
        'forPinDigest': channel_pin_digest(channel),
        'revision': revision,
        'tree': ref_tree(repo_root, revision),
        'parentRevision': channel['baseRevision'],
    }
    return save_or_plan_channel_edit(
        args, repo_root, manifest, manifest_path, channel, 'resolve',
        {'revision': revision, 'tree': channel['resolution']['tree']},
        observed_manifest_digest=observed_digest, before_channel=before,
    )


def channel_operation_events(repo_root, manifest_path, operation_id=None):
    allowed = {
        'channel_operation_started', 'channel_operation_prepared',
        'channel_operation_receipt',
    }
    return [
        event for event in load_ledger_events(repo_root, manifest_path)
        if event.get('type') in allowed
        and (
            operation_id is None
            or (event.get('payload') or {}).get('operationId') == operation_id
        )
    ]


def command_channel_operation_show(args):
    repo_root = resolve_repo_root(args.repo)
    _, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    events = channel_operation_events(repo_root, manifest_path, args.operation_id)
    operations = {}
    for event in events:
        payload = event.get('payload') or {}
        operation_id = payload.get('operationId')
        if not operation_id:
            continue
        item = operations.setdefault(operation_id, {'operationId': operation_id, 'events': []})
        item['events'].append({
            'seq': event['seq'], 'ts': event['ts'], 'type': event['type'],
            'payload': payload,
        })
        item.update({
            'operation': payload.get('operation'), 'channel': payload.get('channel'),
            'planDigest': payload.get('planDigest'),
        })
        if event['type'] == 'channel_operation_receipt':
            item['status'] = payload.get('status')
        elif event['type'] == 'channel_operation_started':
            item.setdefault('status', 'pending')
        elif event['type'] == 'channel_operation_prepared':
            item['status'] = 'prepared'
    if args.operation_id and args.operation_id not in operations:
        raise SyncwheelError(f'unknown channel operation: {args.operation_id}')
    selected = list(operations.values())
    channel_filter = getattr(args, 'channel', None)
    status_filter = getattr(args, 'status', None)
    if channel_filter:
        selected = [item for item in selected if item.get('channel') == channel_filter]
    if status_filter:
        selected = [item for item in selected if item.get('status') == status_filter]
    output = (
        operations[args.operation_id]
        if args.operation_id else {'operations': selected[-20:]}
    )
    print(json.dumps(output, indent=2))
    return 0


def command_channel_operation_list(args):
    args.operation_id = None
    return command_channel_operation_show(args)


def command_channel_receipt_show(args):
    repo_root = resolve_repo_root(args.repo)
    _, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    receipts = []
    for event in load_ledger_events(repo_root, manifest_path):
        if event.get('type') not in {
            'channel_operation_receipt', 'channel_applied', 'channel_published',
            'channel_closed',
        }:
            continue
        payload = event.get('payload') or {}
        if args.channel and payload.get('channel') != args.channel:
            continue
        receipts.append({
            'seq': event['seq'], 'ts': event['ts'], 'type': event['type'],
            'receipt': payload,
        })
    print(json.dumps({'receipts': receipts[-20:]}, indent=2))
    return 0


def command_channel_contract(args):
    contract = {
        'contractVersion': 1,
        'manifestVersion': MANIFEST_VERSION_CHANNELS,
        'coordinationStateSchemaVersion': COORDINATION_STATE_SCHEMA_VERSION_CHANNELS,
        'schemas': {
            'channel': {
                'required': [
                    'id', 'branch', 'lifecycle', 'base', 'baseRevision', 'remote',
                    'composition',
                ],
                'compositionEntryRequired': [
                    'stack', 'branch', 'stackBase', 'stackBaseRevision',
                    'branchRevision', 'commits', 'dependsOn',
                ],
                'resolutionRequired': [
                    'forPinDigest', 'revision', 'tree', 'parentRevision',
                ],
            },
            'plan': {
                'schemaVersion': CHANNEL_PLAN_SCHEMA_VERSION,
                'digest': 'sha256-canonical-json-without-planDigest-and-operationId',
                'required': [
                    'kind', 'request', 'manifestDigestBefore', 'before', 'after',
                    'pinDigest', 'compositionDigest', 'actions', 'planDigest',
                ],
                'actionRequired': [
                    'id', 'type', 'target', 'before', 'intendedAfter',
                ],
            },
            'receipt': {
                'schemaVersion': CHANNEL_PLAN_SCHEMA_VERSION,
                'boundBy': ['operationId', 'planDigest', 'observationRevision'],
                'required': [
                    'operationId', 'planDigest', 'observationRevision', 'request',
                    'before', 'after', 'actions', 'actionOutcomes', 'status',
                    'startedAt', 'completedAt',
                ],
                'actionOutcomeStatuses': [
                    'succeeded', 'failed', 'partial', 'unknown', 'cancelled',
                    'not-attempted',
                ],
            },
            'operation': {
                'schemaVersion': LEDGER_SCHEMA_VERSION,
                'events': ['started', 'prepared', 'receipt'],
                'terminalStatuses': [
                    'succeeded', 'failed', 'partial', 'unknown', 'cancelled',
                ],
            },
        },
        'capabilities': [
            'pinned-composition', 'dependency-order', 'digest-bound-mutations',
            'local-materialization', 'exact-lease-publication',
            'active-active-coordinated-publication', 'channel-resolution-snapshot',
            'durable-operation-intent', 'observation-only-reconciliation',
        ],
        'truth': {
            'publishedBranchIsDeploymentProof': False,
            'remoteDeletionOnClose': False,
            'reconcileRetriesMutation': False,
        },
    }
    print(json.dumps(contract, indent=2))
    return 0


def channel_action_outcome(plan, action_id, status, observed_after=None, detail=None):
    action = next(
        (item for item in plan.get('actions', []) if item.get('id') == action_id),
        None,
    )
    if not action:
        raise SyncwheelError(f'channel plan is missing action id: {action_id}')
    return {
        'id': action['id'],
        'type': action['type'],
        'target': action.get('target'),
        'before': action.get('before'),
        'intendedAfter': action.get('intendedAfter'),
        'status': status,
        'observedAfter': observed_after or {},
        'detail': detail,
    }


def observe_remote_ref_outcome(repo_root, remote, ref, expected, intended):
    result = git(repo_root, 'ls-remote', '--heads', remote, ref, check=False)
    if result.returncode != 0:
        return {
            'phase': 'unavailable', 'remote': remote, 'ref': ref,
            'revision': None, 'detail': 'remote ref observation failed',
        }
    revision = None
    for line in result.stdout.splitlines():
        candidate, separator, observed_ref = line.partition('\t')
        if separator and observed_ref == ref:
            revision = candidate
            break
    if revision == intended:
        phase = 'applied'
    elif revision == expected:
        phase = 'pre'
    else:
        phase = 'divergent'
    return {
        'phase': phase, 'remote': remote, 'ref': ref, 'revision': revision,
        'detail': f'remote ref is {phase}',
    }


def observe_coordination_outcome(repo_root, mutation):
    remote = mutation.get('coordinationRemote')
    ref = mutation.get('coordinationStateRef')
    expected = mutation.get('expectedCoordinationStateRevision')
    intended_digest = mutation.get('intendedCoordinationManifestDigest')
    if not remote or not ref or not intended_digest:
        return {'phase': 'unavailable', 'detail': 'coordination intent is incomplete'}
    result = git(repo_root, 'ls-remote', '--heads', remote, ref, check=False)
    if result.returncode != 0:
        return {
            'phase': 'unavailable', 'remote': remote, 'ref': ref,
            'detail': 'coordination state observation failed',
        }
    revision = None
    for line in result.stdout.splitlines():
        candidate, separator, observed_ref = line.partition('\t')
        if separator and observed_ref == ref:
            revision = candidate
            break
    if revision is None:
        phase = 'pre' if expected is None else 'divergent'
        return {
            'phase': phase, 'remote': remote, 'ref': ref, 'revision': None,
            'manifestDigest': None, 'detail': f'coordination state is {phase}',
        }
    fetched = git(repo_root, 'fetch', '--quiet', remote, ref, check=False)
    if fetched.returncode != 0:
        return {
            'phase': 'unavailable', 'remote': remote, 'ref': ref,
            'revision': revision, 'detail': 'coordination state fetch failed',
        }
    try:
        state = coordination_state_from_commit(repo_root, 'FETCH_HEAD')
    except SyncwheelError as exc:
        return {
            'phase': 'unavailable', 'remote': remote, 'ref': ref,
            'revision': revision, 'detail': str(exc)[:300],
        }
    observed_digest = state.get('manifest_digest')
    if observed_digest == intended_digest:
        phase = 'applied'
    elif revision == expected:
        phase = 'pre'
    else:
        phase = 'divergent'
    return {
        'phase': phase, 'remote': remote, 'ref': ref, 'revision': revision,
        'manifestDigest': observed_digest, 'detail': f'coordination state is {phase}',
    }


def reduce_observed_phases(*phases):
    if any(phase in {'unavailable', 'divergent'} for phase in phases):
        return 'unknown'
    if all(phase == 'applied' for phase in phases):
        return 'succeeded'
    if all(phase == 'pre' for phase in phases):
        return 'failed'
    return 'partial'


def command_channel_reconcile_outcome(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    events = channel_operation_events(repo_root, manifest_path, args.operation_id)
    prepared = next(
        ((event.get('payload') or {}) for event in events
         if event.get('type') == 'channel_operation_prepared'),
        None,
    )
    started = next(
        ((event.get('payload') or {}) for event in events
         if event.get('type') == 'channel_operation_started'),
        None,
    )
    intent = prepared or started
    if not intent:
        raise SyncwheelError(f'unknown channel operation: {args.operation_id}')
    outcomes = [
        event.get('payload') or {} for event in events
        if event.get('type') == 'channel_operation_receipt'
    ]
    if outcomes and outcomes[-1].get('status') in {
        'succeeded', 'failed', 'partial', 'cancelled'
    }:
        print(json.dumps(outcomes[-1], indent=2))
        return 0
    mutation = intent.get('mutation') or {}
    kind = mutation.get('kind')
    status = 'unknown'
    detail = 'observation could not prove a terminal outcome'
    observation = {'kind': kind}
    action_outcomes = None
    if kind == 'local-ref':
        observed = ref_tip(repo_root, mutation.get('ref'))
        observation.update({'ref': mutation.get('ref'), 'revision': observed})
        if observed == mutation.get('intendedRevision'):
            status, detail = 'succeeded', 'local ref matches intended revision'
        elif observed == mutation.get('expectedRevision'):
            status, detail = 'failed', 'local ref remains at expected pre-operation revision'
    elif kind == 'remote-ref':
        channel_observed = observe_remote_ref_outcome(
            repo_root, mutation.get('remote'), mutation.get('ref'),
            mutation.get('expectedRevision'), mutation.get('intendedRevision'),
        )
        observation['channelRef'] = channel_observed
        if mutation.get('coordinationActive'):
            state_observed = observe_coordination_outcome(repo_root, mutation)
            observation['coordinationState'] = state_observed
            status = reduce_observed_phases(
                channel_observed['phase'], state_observed['phase']
            )
            detail = 'channel ref and coordination state were observed independently; no mutation retried'
            phase_status = {
                'applied': 'succeeded', 'pre': 'failed',
                'divergent': 'unknown', 'unavailable': 'unknown',
            }
            action_outcomes = [
                channel_action_outcome(
                    intent, 'publish-channel-ref', phase_status[channel_observed['phase']],
                    channel_observed, channel_observed['detail'],
                ),
                channel_action_outcome(
                    intent, 'publish-coordination-state', phase_status[state_observed['phase']],
                    state_observed, state_observed['detail'],
                ),
            ]
        else:
            status = reduce_observed_phases(channel_observed['phase'])
            detail = channel_observed['detail'] + '; no mutation was retried'
            action_outcomes = [channel_action_outcome(
                intent, 'publish-channel-ref', {
                    'applied': 'succeeded', 'pre': 'failed',
                    'divergent': 'unknown', 'unavailable': 'unknown',
                }[channel_observed['phase']], channel_observed, detail,
            )]
    elif kind == 'close':
        active = channel_map(manifest).get(intent.get('channel'))
        local = ref_tip(repo_root, mutation.get('ref'))
        current_manifest_digest = manifest_digest(manifest)
        if active is None and current_manifest_digest == mutation.get('proposedManifestDigest'):
            local_phase = 'applied'
        elif active is not None and current_manifest_digest == intent.get('manifestDigest'):
            local_phase = 'pre'
        else:
            local_phase = 'divergent'
        observation.update({
            'manifestDigest': current_manifest_digest, 'channelPresent': active is not None,
            'localRevision': local, 'manifestPhase': local_phase,
        })
        manifest_status = {
            'applied': 'succeeded', 'pre': 'failed', 'divergent': 'unknown',
        }[local_phase]
        action_outcomes = []
        if mutation.get('coordinationRemote'):
            state_observed = observe_coordination_outcome(repo_root, mutation)
            observation['coordinationState'] = state_observed
            status = reduce_observed_phases(local_phase, state_observed['phase'])
            state_status = {
                'applied': 'succeeded', 'pre': 'failed',
                'divergent': 'unknown', 'unavailable': 'unknown',
            }[state_observed['phase']]
            action_outcomes.append(channel_action_outcome(
                intent, 'publish-coordination-state', state_status,
                state_observed, state_observed['detail'],
            ))
        else:
            status = reduce_observed_phases(local_phase)
        action_outcomes.append(channel_action_outcome(
            intent, 'update-channel-manifest', manifest_status,
            {'manifestDigest': current_manifest_digest, 'channelPresent': active is not None},
            f'manifest is {local_phase}',
        ))
        if mutation.get('deleteLocal'):
            if local is None and local_phase == 'applied':
                delete_status = 'succeeded'
            elif local == mutation.get('expectedRevision') and local_phase == 'pre':
                delete_status = 'not-attempted'
            else:
                delete_status = 'unknown'
            action_outcomes.append(channel_action_outcome(
                intent, 'delete-local-channel-ref', delete_status,
                {'localRevision': local}, 'local deletion observed without retry',
            ))
            status = reduce_channel_action_outcomes(action_outcomes, status)
        detail = 'close outcome observed across local and coordinated state; no mutation retried'
    elif kind == 'manifest':
        active = channel_map(manifest).get(intent.get('channel'))
        observed_channel_digest = (
            canonical_json_digest(channel_summary_for_mutation(active)) if active else None
        )
        if observed_channel_digest == mutation.get('proposedChannelDigest'):
            status, detail = 'succeeded', 'manifest contains the intended channel state'
        elif manifest_digest(manifest) == intent.get('manifestDigest'):
            status, detail = 'failed', 'manifest remains at the pre-operation digest'
        observation.update({
            'manifestDigest': manifest_digest(manifest),
            'channelDigest': observed_channel_digest,
        })
    reconcile_plan = {
        'kind': 'channelPlan',
        'schemaVersion': CHANNEL_PLAN_SCHEMA_VERSION,
        'operation': 'reconcile-outcome',
        'request': {
            'operation': 'reconcile-outcome',
            'operationId': args.operation_id,
        },
        'targetOperationId': args.operation_id,
        'targetPlanDigest': intent.get('planDigest'),
        'manifestDigest': manifest_digest(manifest),
        'manifestDigestBefore': manifest_digest(manifest),
        'before': {
            'operation': intent,
            'observation': observation,
        },
        'after': {
            'status': status,
            'observation': observation,
        },
        'context': {
            'targetOperationId': args.operation_id,
            'targetPlanDigest': intent.get('planDigest'),
            'mutationRetried': False,
        },
        'pinDigest': intent.get('pinDigest'),
        'compositionDigest': intent.get('compositionDigest'),
        'observation': observation,
        'observationRevision': canonical_json_digest(observation),
        'proposedStatus': status,
        'detail': detail,
        'actions': [{
            'id': 'append-terminal-receipt',
            'type': 'append-observed-terminal-outcome',
            'target': args.operation_id,
            'before': {
                'status': outcomes[-1].get('status') if outcomes else 'pending',
                'observation': observation,
            },
            'intendedAfter': {
                'status': status,
                'reconciled': True,
                'mutationRetried': False,
            },
        }],
        'mutationRetried': False,
        'applyRequired': True,
    }
    reconcile_plan['planDigest'] = canonical_json_digest(reconcile_plan)
    if not getattr(args, 'apply', False):
        print(json.dumps(reconcile_plan, indent=2))
        return 0
    expected_digest = getattr(args, 'plan_digest', None)
    if not expected_digest:
        raise SyncwheelError('--plan-digest is required with --apply')
    if expected_digest != reconcile_plan['planDigest']:
        raise SyncwheelError(
            'channel reconciliation plan is stale; observe again and use the exact planDigest'
        )
    outcome = record_channel_operation_receipt(
        repo_root, manifest_path, intent, status, None, detail, reconciled=True,
        evidence={
            'reconciliationPlanDigest': reconcile_plan['planDigest'],
            'observationRevision': reconcile_plan['observationRevision'],
        },
        action_outcomes=action_outcomes,
    )
    print(json.dumps(outcome, indent=2))
    return 0


def command_channel_diff(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_delivery_manifest(manifest)
    channel = require_channel(manifest, args.channel)
    if args.other:
        other = require_channel(manifest, args.other)
        left = {entry['stack']: entry for entry in channel['composition']}
        right = {entry['stack']: entry for entry in other['composition']}
        output = {
            'channel': channel['id'],
            'otherChannel': other['id'],
            'baseEqual': (
                channel['base'] == other['base']
                and channel['baseRevision'] == other['baseRevision']
            ),
            'compositionDigestEqual': (
                channel_composition_digest(channel) == channel_composition_digest(other)
            ),
            'orderEqual': [entry['stack'] for entry in channel['composition']] == [
                entry['stack'] for entry in other['composition']
            ],
            'added': sorted(set(right) - set(left)),
            'removed': sorted(set(left) - set(right)),
            'changed': sorted(
                stack_id for stack_id in set(left) & set(right) if left[stack_id] != right[stack_id]
            ),
        }
    else:
        current = []
        for entry in channel['composition']:
            observed = ref_tip(repo_root, entry['branch'])
            current.append({
                'stack': entry['stack'],
                'pinnedRevision': entry['branchRevision'],
                'currentRevision': observed,
                'drifted': observed != entry['branchRevision'],
            })
        output = {'channel': channel['id'], 'comparison': 'pinned-vs-current', 'stacks': current}
    print(json.dumps(output, indent=2))
    return 0


def command_channel_plan(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_delivery_manifest(manifest)
    channel = require_channel(manifest, args.channel)
    print(json.dumps(build_channel_plan(
        repo_root, manifest, channel, args.operation, channel_operation_id_from_args(args)
    ), indent=2))
    return 0


def command_channel_apply(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_delivery_manifest(manifest)
    channel = require_channel(manifest, args.channel)
    if not args.apply:
        print(json.dumps(build_channel_plan(
            repo_root, manifest, channel, 'apply', channel_operation_id_from_args(args)
        ), indent=2))
        return 0
    plan = verify_channel_plan_digest(
        repo_root, manifest, channel, 'apply', args.plan_digest,
        channel_operation_id_from_args(args),
    )
    with manifest_write_transaction(repo_root, manifest_path, channel['id']):
        fresh_manifest = require_locked_manifest_observation(repo_root, manifest_path, plan)
        fresh_channel = require_channel(fresh_manifest, args.channel)
        fresh_plan = build_channel_plan(
            repo_root, fresh_manifest, fresh_channel, 'apply',
            channel_operation_id_from_args(args),
        )
        if fresh_plan['planDigest'] != plan['planDigest']:
            raise SyncwheelError(
                'channel plan is stale after lock acquisition; local channel ref was not updated'
            )
        channel = fresh_channel
        tip = materialize_channel_tip(repo_root, channel, plan)
        ref = f"refs/heads/{channel['branch']}"
        expected = plan['currentRevision'] or ZERO_OBJECT_ID
        record_channel_operation_prepared(
            repo_root, manifest_path, plan, channel,
            {
                'kind': 'local-ref', 'ref': ref, 'expectedRevision': plan['currentRevision'],
                'intendedRevision': tip,
            },
        )
        try:
            channel_mutation_checkpoint()
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'cancelled', channel,
                'cancelled before authoritative local ref mutation',
                evidence={'localRevision': plan['currentRevision']},
            )
            raise SyncwheelError('channel apply cancelled before authoritative change') from exc
        require_manifest_transaction_current(manifest_path)
        try:
            updated = git(repo_root, 'update-ref', ref, tip, expected, check=False)
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'unknown', channel,
                'interrupted after local ref mutation began',
                evidence={'localRevision': ref_tip(repo_root, ref)},
            )
            raise SyncwheelError(
                f'channel apply outcome is unknown; operation {plan["operationId"]} '
                'requires operation reconcile'
            ) from exc
        if updated.returncode != 0:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'failed', channel,
                'local channel ref changed before atomic update',
                evidence={'localRevision': ref_tip(repo_root, ref)},
            )
            raise SyncwheelError(
                'channel local branch changed before atomic ref update; retry from a new plan'
            )
        receipt = channel_receipt(channel, plan, 'applied', tip=tip)
        try:
            append_ledger_event(repo_root, 'channel_applied', receipt, manifest_path)
            operation_receipt = record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'succeeded', channel,
                evidence={'localRevision': tip},
            )
        except (KeyboardInterrupt, SystemExit) as exc:
            try:
                record_channel_operation_receipt(
                    repo_root, manifest_path, plan, 'unknown', channel,
                    'interrupted after local ref mutation completed',
                    evidence={'localRevision': ref_tip(repo_root, ref)},
                )
            except Exception:
                pass
            raise SyncwheelError(
                f'channel apply outcome is unknown; operation {plan["operationId"]} '
                'requires operation reconcile'
            ) from exc
        except Exception as exc:
            try:
                record_channel_operation_receipt(
                    repo_root, manifest_path, plan, 'unknown', channel,
                    f'local ref updated; receipt recording failed: {str(exc)[:300]}',
                    evidence={'localRevision': ref_tip(repo_root, ref)},
                )
            except Exception:
                pass
            raise SyncwheelError(
                f'local channel ref updated but receipt recording failed; operation '
                f'{plan["operationId"]} requires reconcile-outcome'
            ) from exc
    print(json.dumps({**receipt, **operation_receipt}, indent=2))
    return 0


def command_channel_publish(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_delivery_manifest(manifest)
    channel = require_channel(manifest, args.channel)
    if not args.apply:
        print(json.dumps(build_channel_plan(
            repo_root, manifest, channel, 'publish', channel_operation_id_from_args(args)
        ), indent=2))
        return 0
    plan = verify_channel_plan_digest(
        repo_root, manifest, channel, 'publish', args.plan_digest,
        channel_operation_id_from_args(args),
    )
    with manifest_write_transaction(repo_root, manifest_path, channel['id']):
        fresh_manifest = require_locked_manifest_observation(repo_root, manifest_path, plan)
        fresh_channel = require_channel(fresh_manifest, args.channel)
        fresh_plan = build_channel_plan(
            repo_root, fresh_manifest, fresh_channel, 'publish',
            channel_operation_id_from_args(args),
        )
        if fresh_plan['planDigest'] != plan['planDigest']:
            raise SyncwheelError('channel publication plan changed under mutation lock')
        manifest = fresh_manifest
        channel = fresh_channel
        if not plan['remoteObservationKnown']:
            raise SyncwheelError('channel remote tip is unknown; refusing publication')
        current = plan['currentRevision']
        if not current:
            raise SyncwheelError('channel has no local materialized branch; run channel apply first')
        applied = latest_channel_event(repo_root, manifest_path, channel['id'], 'channel_applied')
        if (
            not applied
            or applied.get('tip') != current
            or applied.get('compositionDigest') != plan['compositionDigest']
        ):
            raise SyncwheelError('channel local branch lacks current plan-bound apply evidence')
        ref = f"refs/heads/{channel['branch']}"
        coordination_state = None
        record_channel_operation_prepared(
            repo_root, manifest_path, plan, channel,
            {
                'kind': 'remote-ref', 'remote': channel['remote'], 'ref': ref,
                'expectedRevision': plan['remoteRevision'], 'intendedRevision': current,
                'coordinationActive': bool(coordination_is_active(manifest)),
                'expectedCoordinationStateRevision': plan.get('coordinationStateRevision'),
                'intendedCoordinationManifestDigest': plan.get('coordinationManifestDigest'),
                'coordinationRemote': (
                    coordination_config(manifest)['remote']
                    if coordination_is_active(manifest) else None
                ),
                'coordinationStateRef': (
                    coordination_state_ref(coordination_config(manifest))
                    if coordination_is_active(manifest) else None
                ),
            },
        )
        try:
            channel_mutation_checkpoint()
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'cancelled', channel,
                'cancelled before authoritative remote mutation',
                evidence={'remoteRevision': plan['remoteRevision']},
            )
            raise SyncwheelError('channel publish cancelled before authoritative change') from exc
        require_manifest_transaction_current(manifest_path)
        try:
            if coordination_is_active(manifest):
                with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink):
                    result = coordinated_publish(
                        repo_root, manifest, manifest_path, {ref: current},
                        f"channel:{channel['id']}", 'channel-pinned-composition',
                        operation_token=plan['operationId'],
                    )
                coordination_state = result.get('state_tip')
            else:
                lease = f"--force-with-lease={ref}:{plan['remoteRevision'] or ''}"
                pushed = run_authorized_push(
                    repo_root,
                    ['git', 'push', '--porcelain', lease, channel['remote'], f'{current}:{ref}'],
                    channel['remote'], [ref], check=False,
                )
                if pushed.returncode != 0:
                    detail = pushed.stderr.strip() or pushed.stdout.strip()
                    record_channel_operation_receipt(
                        repo_root, manifest_path, plan,
                        'failed' if 'stale info' in detail.lower() else 'unknown', channel,
                        detail[:400] or 'remote push failed',
                        evidence={'remoteRevision': plan['remoteRevision']},
                    )
                    raise SyncwheelError(
                        'channel publish lease lost or outcome is unknown; STOP without merge, '
                        'reset, rebase, or force'
                    )
        except (KeyboardInterrupt, SystemExit) as exc:
            observed_remote = channel_remote_observation(repo_root, channel)
            observed_after = {
                'remoteKnown': observed_remote['known'],
                'remoteRevision': observed_remote['revision'],
            }
            if coordination_is_active(manifest):
                observed_after['coordinationState'] = observe_coordination_outcome(
                    repo_root,
                    {
                        'coordinationRemote': coordination_config(manifest)['remote'],
                        'coordinationStateRef': coordination_state_ref(
                            coordination_config(manifest)
                        ),
                        'expectedCoordinationStateRevision': plan.get(
                            'coordinationStateRevision'
                        ),
                        'intendedCoordinationManifestDigest': plan.get(
                            'coordinationManifestDigest'
                        ),
                    },
                )
            action_outcomes = [
                channel_action_outcome(
                    plan, action['id'], 'unknown', observed_after,
                    'interrupted after remote mutation began; no mutation was retried',
                )
                for action in plan['actions']
            ]
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'unknown', channel,
                'interrupted after remote mutation began; no mutation was retried',
                evidence=observed_after,
                action_outcomes=action_outcomes,
            )
            raise SyncwheelError(
                f'channel publication outcome is unknown; operation {plan["operationId"]} '
                'requires operation reconcile'
            ) from exc
        except SyncwheelError as exc:
            if coordination_is_active(manifest):
                observed_after = {
                    'remoteRevision': channel_remote_observation(repo_root, channel),
                    'coordinationState': observe_coordination_outcome(
                        repo_root,
                        {
                            'coordinationRemote': coordination_config(manifest)['remote'],
                            'coordinationStateRef': coordination_state_ref(
                                coordination_config(manifest)
                            ),
                            'expectedCoordinationStateRevision': plan.get(
                                'coordinationStateRevision'
                            ),
                            'intendedCoordinationManifestDigest': plan.get(
                                'coordinationManifestDigest'
                            ),
                        },
                    ),
                }
                action_outcomes = [
                    channel_action_outcome(
                        plan, action['id'], 'unknown', observed_after, str(exc)[:400]
                    )
                    for action in plan['actions']
                ]
                try:
                    record_channel_operation_receipt(
                        repo_root, manifest_path, plan, 'unknown', channel,
                        str(exc)[:400], evidence=observed_after,
                        action_outcomes=action_outcomes,
                    )
                except Exception:
                    pass
            raise
        except Exception as exc:
            try:
                observed_after = {
                    'remoteRevision': channel_remote_observation(repo_root, channel),
                }
                if coordination_is_active(manifest):
                    observed_after['coordinationState'] = observe_coordination_outcome(
                        repo_root,
                        {
                            'coordinationRemote': coordination_config(manifest)['remote'],
                            'coordinationStateRef': coordination_state_ref(
                                coordination_config(manifest)
                            ),
                            'expectedCoordinationStateRevision': plan.get(
                                'coordinationStateRevision'
                            ),
                            'intendedCoordinationManifestDigest': plan.get(
                                'coordinationManifestDigest'
                            ),
                        },
                    )
                action_outcomes = [
                    channel_action_outcome(
                        plan, action['id'], 'unknown', observed_after, str(exc)[:400]
                    )
                    for action in plan['actions']
                ]
                record_channel_operation_receipt(
                    repo_root, manifest_path, plan, 'unknown', channel, str(exc)[:400],
                    evidence=observed_after, action_outcomes=action_outcomes,
                )
            except Exception:
                pass
            raise SyncwheelError(
                f'channel publication outcome is unknown; operation {plan["operationId"]} '
                'requires reconcile-outcome'
            ) from exc
        receipt = channel_receipt(
            channel, plan, 'published', tip=current, published_revision=current,
            coordination_state=coordination_state,
        )
        try:
            append_ledger_event(repo_root, 'channel_published', receipt, manifest_path)
            operation_receipt = record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'succeeded', channel,
                evidence={
                    'remoteRevision': current, 'coordinationState': coordination_state,
                },
            )
        except (KeyboardInterrupt, SystemExit) as exc:
            terminal_recorded = False
            try:
                succeeded_after = {
                    'remoteRevision': current,
                    'coordinationState': coordination_state,
                }
                succeeded_outcomes = [
                    channel_action_outcome(
                        plan, action['id'], 'succeeded', succeeded_after,
                        'authoritative publication completed before interruption',
                    )
                    for action in plan['actions']
                ]
                operation_receipt = record_channel_operation_receipt(
                    repo_root, manifest_path, plan, 'succeeded', channel,
                    'interrupted after remote mutation completed',
                    evidence=succeeded_after,
                    action_outcomes=succeeded_outcomes,
                )
                terminal_recorded = True
            except Exception:
                pass
            if terminal_recorded:
                raise SyncwheelError(
                    'channel publication succeeded and its terminal operation receipt was '
                    'recorded before interruption handling completed'
                ) from exc
            raise SyncwheelError(
                f'channel publication outcome is unknown; operation {plan["operationId"]} '
                'requires operation reconcile'
            ) from exc
        except Exception as exc:
            terminal_recorded = False
            try:
                succeeded_after = {
                    'remoteRevision': current,
                    'coordinationState': coordination_state,
                }
                succeeded_outcomes = [
                    channel_action_outcome(
                        plan, action['id'], 'succeeded', succeeded_after,
                        'authoritative publication completed; compatibility ledger event failed',
                    )
                    for action in plan['actions']
                ]
                operation_receipt = record_channel_operation_receipt(
                    repo_root, manifest_path, plan, 'succeeded', channel,
                    f'authoritative publication completed; compatibility ledger event failed: '
                    f'{str(exc)[:300]}',
                    evidence=succeeded_after,
                    action_outcomes=succeeded_outcomes,
                )
                terminal_recorded = True
            except Exception:
                pass
            if terminal_recorded:
                raise SyncwheelError(
                    'channel publication succeeded and its terminal operation receipt was '
                    'recorded, but the compatibility channel_published event failed'
                ) from exc
            raise SyncwheelError(
                f'remote channel publication succeeded but receipt recording failed; operation '
                f'{plan["operationId"]} requires reconcile-outcome'
            ) from exc
    print(json.dumps({**receipt, **operation_receipt}, indent=2))
    return 0


def close_channel_action_outcomes(
    plan,
    *,
    coordination_status=None,
    manifest_status=None,
    local_delete_status=None,
    observed_after=None,
    detail=None,
):
    statuses = {
        'publish-coordination-state': coordination_status,
        'update-channel-manifest': manifest_status,
        'delete-local-channel-ref': local_delete_status,
    }
    outcomes = []
    for action in plan.get('actions', []):
        status = statuses.get(action['id'])
        if status is None:
            status = 'not-attempted'
        outcomes.append(channel_action_outcome(
            plan, action['id'], status, observed_after, detail,
        ))
    return outcomes


def command_channel_close(args):
    if return_existing_channel_operation(args):
        return 0
    repo_root, manifest, manifest_path, channel, observed_digest, before = channel_edit_context(args)
    branch = channel['branch']
    ref = f'refs/heads/{branch}'
    current = ref_tip(repo_root, branch)
    if args.delete_local and current:
        if find_worktree_for_branch(repo_root, branch):
            raise SyncwheelError(f'cannot delete checked-out channel branch: {branch}')
        applied = latest_channel_event(repo_root, manifest_path, channel['id'], 'channel_applied')
        if not applied or applied.get('tip') != current:
            raise SyncwheelError('local channel branch is not backed by matching apply evidence')
    manifest['channels'] = [item for item in manifest['channels'] if item['id'] != channel['id']]
    close_coordination_active = coordination_is_active(manifest)
    close_coordination_config = (
        coordination_config(manifest) if close_coordination_active else None
    )
    close_coordination_target = (
        coordination_state_ref(close_coordination_config)
        if close_coordination_config else None
    )
    close_coordination_manifest_digest = (
        coordination_manifest_digest(manifest, repo_root)
        if close_coordination_active else None
    )
    remote_observation = channel_remote_observation(repo_root, channel)
    coordination_state_revision = None
    if coordination_is_active(manifest):
        close_config = coordination_config(manifest)
        state_ref = coordination_state_ref(close_config)
        coordination_state_revision = remote_ref_tips(
            repo_root, close_config['remote'], [state_ref]
        )[state_ref]
    observation = {
        'localRevision': current,
        'remoteKnown': remote_observation['known'],
        'remoteRevision': remote_observation['revision'],
        'deleteLocal': bool(args.delete_local),
        'reason': args.reason,
        'coordinationStateRevision': coordination_state_revision,
    }
    output = build_channel_mutation_plan(
        'close', manifest_path, observed_digest, manifest, None,
        {
            'channel': channel['id'], 'reason': args.reason,
            'deleteLocal': bool(args.delete_local),
            'remoteRefDeleted': False,
            'localRef': ref,
            'coordinationActive': close_coordination_active,
            'coordinationTarget': (
                f"{close_coordination_config['remote']}:{close_coordination_target}"
                if close_coordination_config else None
            ),
            'coordinationManifestDigest': close_coordination_manifest_digest,
        }, observation=observation, before_channel=before,
        operation_id=channel_operation_id_from_args(args),
    )
    output['remoteRefDeleted'] = False
    output['localRefDeletionRequested'] = bool(args.delete_local)
    if not args.apply:
        print(json.dumps(output, indent=2))
        return 0
    verify_channel_mutation_plan(args, output)
    with manifest_write_transaction(repo_root, manifest_path, channel['id']):
        require_locked_manifest_observation(repo_root, manifest_path, output)
        if ref_tip(repo_root, branch) != current:
            raise SyncwheelError('channel close plan is stale under mutation lock')
        locked_remote_observation = channel_remote_observation(repo_root, channel)
        if not remote_observation['known'] or not locked_remote_observation['known']:
            raise SyncwheelError(
                'channel close plan is stale: channel remote ref is unknown under mutation lock'
            )
        if locked_remote_observation['revision'] != remote_observation['revision']:
            raise SyncwheelError(
                'channel close plan is stale: channel remote ref changed under mutation lock'
            )
        if coordination_is_active(manifest):
            locked_config = coordination_config(manifest)
            locked_state_ref = coordination_state_ref(locked_config)
            locked_state_revision = remote_ref_tips(
                repo_root, locked_config['remote'], [locked_state_ref]
            )[locked_state_ref]
            if locked_state_revision != coordination_state_revision:
                raise SyncwheelError(
                    'channel close plan is stale: coordination state changed under mutation lock'
                )
        record_channel_operation_prepared(
            repo_root, manifest_path, output, channel,
            {
                'kind': 'close', 'ref': ref, 'expectedRevision': current,
                'deleteLocal': bool(args.delete_local),
                'proposedManifestDigest': manifest_digest(manifest),
                'remoteRevision': remote_observation['revision'],
                'coordinationStateRevision': coordination_state_revision,
                'expectedCoordinationStateRevision': coordination_state_revision,
                'intendedCoordinationManifestDigest': close_coordination_manifest_digest,
                'coordinationRemote': (
                    close_coordination_config['remote'] if close_coordination_config else None
                ),
                'coordinationStateRef': close_coordination_target,
            },
        )
        try:
            channel_mutation_checkpoint()
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, output, 'cancelled', channel,
                'cancelled before authoritative close mutation',
                evidence={
                    'manifestDigest': observed_digest,
                    'localRevision': current,
                    'coordinationState': coordination_state_revision,
                },
            )
            raise SyncwheelError('channel close cancelled before authoritative change') from exc
        require_manifest_transaction_current(manifest_path)
        coordination_state = None
        manifest_saved = False
        local_deleted = False
        coordination_attempted = False
        manifest_attempted = False
        local_delete_attempted = False
        try:
            if coordination_is_active(manifest):
                coordination_attempted = True
                config = coordination_config(manifest)
                remote_tip = remote_ref_tips(repo_root, config['remote'], [ref])[ref]
                with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink):
                    result = coordinated_publish(
                        repo_root, manifest, manifest_path, {}, f"channel-close:{channel['id']}", 'partial',
                        tombstone={
                            'channel': channel['id'], 'branch': branch, 'ref': ref,
                            'reason': args.reason, 'closed_at': iso_utc_now(), 'remote_tip': remote_tip,
                        },
                        operation_token=output['operationId'],
                    )
                coordination_state = result.get('state_tip')
            manifest_attempted = True
            save_manifest(manifest_path, manifest)
            manifest_saved = True
            append_ledger_event(
                repo_root, 'manifest_saved',
                manifest_event_payload(
                    manifest_path, manifest, 'channel_close',
                    {'channel': channel['id'], 'branch': branch, 'reason': args.reason},
                ), manifest_path,
            )
            if args.delete_local and current:
                local_delete_attempted = True
                deleted = git(repo_root, 'update-ref', '-d', ref, current, check=False)
                if deleted.returncode != 0:
                    raise SyncwheelError(
                        'local channel branch changed before deletion; manifest was closed, ref retained'
                    )
                local_deleted = True
        except (KeyboardInterrupt, SystemExit) as exc:
            try:
                observed_manifest, _ = load_manifest(repo_root, manifest_path)
                observed_after = {
                    'manifestDigest': manifest_digest(observed_manifest),
                    'localRevision': ref_tip(repo_root, ref),
                    'coordinationState': coordination_state,
                }
                action_outcomes = close_channel_action_outcomes(
                    output,
                    coordination_status=(
                        'succeeded' if coordination_state else (
                            'unknown' if coordination_attempted else None
                        )
                    ),
                    manifest_status=(
                        'succeeded' if manifest_saved else (
                            'unknown' if manifest_attempted else 'not-attempted'
                        )
                    ),
                    local_delete_status=(
                        'succeeded' if local_deleted else (
                            'unknown' if local_delete_attempted else 'not-attempted'
                        )
                    ),
                    observed_after=observed_after,
                    detail='interrupted after close mutation began; no mutation was retried',
                )
                record_channel_operation_receipt(
                    repo_root, manifest_path, output, 'unknown', channel,
                    'interrupted after close mutation began; no mutation was retried',
                    evidence=observed_after,
                    action_outcomes=action_outcomes,
                )
            except Exception:
                pass
            raise SyncwheelError(
                f'channel close outcome is unknown; operation {output["operationId"]} '
                'requires operation reconcile'
            ) from exc
        except Exception as exc:
            durability_unknown = isinstance(exc, ManifestDurabilityError)
            observed_manifest, _ = load_manifest(repo_root, manifest_path)
            observed_after = {
                'manifestDigest': (
                    manifest_digest(observed_manifest) if observed_manifest else None
                ),
                'localRevision': ref_tip(repo_root, ref),
                'coordinationState': coordination_state,
            }
            action_outcomes = close_channel_action_outcomes(
                output,
                coordination_status=(
                    'succeeded' if coordination_state else (
                        'unknown' if coordination_attempted else None
                    )
                ),
                manifest_status=(
                    'succeeded' if manifest_saved else (
                        'unknown' if durability_unknown else (
                            'failed' if manifest_attempted else 'not-attempted'
                        )
                    )
                ),
                local_delete_status=(
                    'succeeded' if local_deleted else (
                        'failed' if local_delete_attempted else 'not-attempted'
                    )
                ),
                observed_after=observed_after,
                detail=str(exc)[:400],
            )
            status = reduce_channel_action_outcomes(action_outcomes)
            try:
                record_channel_operation_receipt(
                    repo_root, manifest_path, output, status, channel, str(exc)[:400],
                    evidence=observed_after,
                    action_outcomes=action_outcomes,
                )
            except Exception:
                pass
            if isinstance(exc, SyncwheelError) and not durability_unknown:
                raise
            raise SyncwheelError(
                f'channel close outcome is {status}; operation {output["operationId"]} '
                'requires operation reconcile'
            ) from exc
        receipt = {
            'schemaVersion': CHANNEL_PLAN_SCHEMA_VERSION,
            'operationId': output['operationId'],
            'planDigest': output['planDigest'],
            'channel': channel['id'],
            'branch': branch,
            'reason': args.reason,
            'remoteRefDeleted': False,
            'localRefDeleted': local_deleted,
            'coordinationState': coordination_state,
            'deploymentAsserted': False,
            'recordedAt': iso_utc_now(),
        }
        try:
            append_ledger_event(repo_root, 'channel_closed', receipt, manifest_path)
            operation_receipt = record_channel_operation_receipt(
                repo_root, manifest_path, output, 'succeeded', channel,
                evidence={
                    'manifestDigest': manifest_digest(manifest),
                    'localRevision': ref_tip(repo_root, ref),
                    'coordinationState': coordination_state,
                },
            )
        except (KeyboardInterrupt, SystemExit) as exc:
            terminal_recorded = False
            try:
                completed_after = {
                    'manifestDigest': manifest_digest(manifest),
                    'localRevision': ref_tip(repo_root, ref),
                    'coordinationState': coordination_state,
                }
                completed_outcomes = close_channel_action_outcomes(
                    output,
                    coordination_status='succeeded' if close_coordination_active else None,
                    manifest_status='succeeded',
                    local_delete_status='succeeded' if args.delete_local else None,
                    observed_after=completed_after,
                    detail='authoritative close completed before interruption',
                )
                operation_receipt = record_channel_operation_receipt(
                    repo_root, manifest_path, output, 'succeeded', channel,
                    'interrupted after close mutation completed',
                    evidence=completed_after,
                    action_outcomes=completed_outcomes,
                )
                terminal_recorded = True
            except Exception:
                pass
            if terminal_recorded:
                raise SyncwheelError(
                    'channel close succeeded and its terminal operation receipt was recorded '
                    'before interruption handling completed'
                ) from exc
            raise SyncwheelError(
                f'channel close outcome is unknown; operation {output["operationId"]} '
                'requires operation reconcile'
            ) from exc
        except Exception as exc:
            terminal_recorded = False
            try:
                completed_after = {
                    'manifestDigest': manifest_digest(manifest),
                    'localRevision': ref_tip(repo_root, ref),
                    'coordinationState': coordination_state,
                }
                completed_outcomes = close_channel_action_outcomes(
                    output,
                    coordination_status='succeeded' if close_coordination_active else None,
                    manifest_status='succeeded',
                    local_delete_status='succeeded' if args.delete_local else None,
                    observed_after=completed_after,
                    detail='authoritative close completed; compatibility ledger event failed',
                )
                operation_receipt = record_channel_operation_receipt(
                    repo_root, manifest_path, output, 'succeeded', channel,
                    f'authoritative close completed; compatibility ledger event failed: '
                    f'{str(exc)[:300]}',
                    evidence=completed_after,
                    action_outcomes=completed_outcomes,
                )
                terminal_recorded = True
            except Exception:
                pass
            if terminal_recorded:
                raise SyncwheelError(
                    'channel close succeeded and its terminal operation receipt was recorded, '
                    'but the compatibility channel_closed event failed'
                ) from exc
            raise SyncwheelError(
                f'channel close applied but receipt recording failed; operation '
                f'{output["operationId"]} requires reconcile-outcome'
            ) from exc
    print(json.dumps({**receipt, **operation_receipt}, indent=2))
    return 0


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


def landing_policy(manifest):
    return manifest.get('landing') or normalize_landing_policy(None)


def landing_override_sets(args):
    requirement_ids = set(getattr(args, 'override_requirement', None) or [])
    group_ids = set(getattr(args, 'override_group', None) or [])
    override_all = bool(getattr(args, 'override_all_checks', False))
    reason = getattr(args, 'override_reason', None)
    if (requirement_ids or group_ids or override_all) and (not isinstance(reason, str) or not reason.strip()):
        raise SyncwheelError('check overrides require a non-empty --override-reason')
    return requirement_ids, group_ids, override_all, reason.strip() if isinstance(reason, str) else None


def landing_requirement_ids(requirement):
    if requirement is None:
        return set()
    ids = {requirement['id']}
    for kind in ('all', 'any'):
        for child in requirement.get(kind, []):
            ids.update(landing_requirement_ids(child))
    return ids


def landing_group_ids(requirement):
    if requirement is None:
        return set()
    groups = {requirement['id']} if any(kind in requirement for kind in ('all', 'any')) else set()
    for kind in ('all', 'any'):
        for child in requirement.get(kind, []):
            groups.update(landing_group_ids(child))
    return groups


def landing_leaf_ids(requirement):
    if requirement is None:
        return set()
    if any(kind in requirement for kind in ('all', 'any')):
        return set().union(*(landing_leaf_ids(child) for child in requirement.get('all', []) + requirement.get('any', [])))
    return {requirement['id']}


def landing_attestation_paths(args, requirement=None):
    paths = {}
    for raw in getattr(args, 'attestation', None) or []:
        requirement_id, separator, path = raw.partition('=')
        if not separator or not requirement_id or not path:
            raise SyncwheelError('--attestation must be requirement-id=receipt-path')
        if requirement_id in paths:
            raise SyncwheelError(f'duplicate --attestation for requirement {requirement_id}')
        paths[requirement_id] = path
    if requirement is not None:
        allowed = {
            requirement_id for requirement_id in landing_leaf_ids(requirement)
            if _landing_requirement_kind(requirement, requirement_id) == 'attestation'
        }
        unknown = sorted(set(paths) - allowed)
        if unknown:
            raise SyncwheelError('attestation supplied for non-attestation requirement(s): ' + ', '.join(unknown))
    return paths


def _landing_requirement_kind(requirement, requirement_id):
    if requirement['id'] == requirement_id:
        return next(kind for kind in ('all', 'any', 'local', 'attestation', 'pr') if kind in requirement)
    for kind in ('all', 'any'):
        for child in requirement.get(kind, []):
            found = _landing_requirement_kind(child, requirement_id)
            if found:
                return found
    return None


def landing_local_check(repo_root, requirement, revisions):
    config = requirement['local']
    revision = revisions[config['scope']]
    with tempfile.TemporaryDirectory(prefix='syncwheel-land-check-') as temporary:
        worktree = Path(temporary)
        added = git(repo_root, 'worktree', 'add', '--detach', '--quiet', str(worktree), revision, check=False)
        if added.returncode != 0:
            return {'passed': False, 'detail': added.stderr.strip() or 'cannot create isolated check worktree'}
        try:
            try:
                result = subprocess.run(
                    config['argv'], cwd=worktree, text=True, capture_output=True,
                    timeout=config['timeoutSeconds'], env=os.environ.copy(),
                )
            except subprocess.TimeoutExpired:
                return {'passed': False, 'detail': f"timed out after {config['timeoutSeconds']} seconds"}
            return {
                'passed': result.returncode == 0,
                'exitCode': result.returncode,
                'detail': (result.stderr or result.stdout).strip()[:1000] or None,
            }
        finally:
            git(repo_root, 'worktree', 'remove', '--force', str(worktree), check=False)


def landing_attestation_check(repo_root, requirement, revisions, manifest_digest_value, attestation_paths):
    config = requirement['attestation']
    receipt_path = attestation_paths.get(requirement['id'])
    if receipt_path is None:
        return {'passed': False, 'detail': 'no receipt was supplied'}
    try:
        receipt = json.loads(Path(receipt_path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {'passed': False, 'detail': f'invalid receipt: {exc}'}
    expected = {
        'requirementId': requirement['id'],
        'scope': config['scope'],
        'subjectRevision': revisions[config['scope']],
        'integrationRevision': revisions['integration'],
        'deliveryRevision': revisions['delivery'],
        'manifestDigest': manifest_digest_value,
    }
    result = run(config['verifierArgv'], cwd=repo_root, input_text=json.dumps({
        'receipt': receipt, 'expected': expected,
    }), check=False)
    if result.returncode != 0:
        return {'passed': False, 'detail': (result.stderr or result.stdout).strip()[:1000] or 'verifier failed'}
    try:
        verified = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {'passed': False, 'detail': f'verifier did not emit JSON: {exc}'}
    passed = (
        isinstance(verified, dict) and verified.get('valid') is True
        and verified.get('subjectRevision') == expected['subjectRevision']
        and isinstance(verified.get('issuer'), str) and bool(verified['issuer'])
    )
    return {
        'passed': passed,
        'issuer': verified.get('issuer') if isinstance(verified, dict) else None,
        'detail': None if passed else 'verifier response is not a valid bound attestation',
    }


def evaluate_landing_requirement(repo_root, requirement, revisions, manifest_digest_value, args):
    requirement_overrides, group_overrides, override_all, reason = landing_override_sets(args)
    kind = next(kind for kind in ('all', 'any', 'local', 'attestation', 'pr') if kind in requirement)
    overridden = override_all or requirement['id'] in requirement_overrides or (
        kind in {'all', 'any'} and requirement['id'] in group_overrides
    )
    if overridden:
        return {
            'id': requirement['id'], 'kind': kind, 'passed': True, 'status': 'overridden',
            'overrideReason': reason,
        }
    if kind in {'all', 'any'}:
        children = [
            evaluate_landing_requirement(repo_root, child, revisions, manifest_digest_value, args)
            for child in requirement[kind]
        ]
        passed = all(child['passed'] for child in children) if kind == 'all' else any(child['passed'] for child in children)
        return {
            'id': requirement['id'], 'kind': kind, 'passed': passed,
            'status': 'passed' if passed else ('requires-pr' if any(child['status'] == 'requires-pr' for child in children) else 'failed'),
            'children': children,
        }
    if kind == 'local':
        result = landing_local_check(repo_root, requirement, revisions)
    elif kind == 'attestation':
        result = landing_attestation_check(
            repo_root, requirement, revisions, manifest_digest_value,
            landing_attestation_paths(args),
        )
    else:
        result = {'passed': False, 'detail': 'remote PR checks require the promote-to-PR route'}
    return {
        'id': requirement['id'], 'kind': kind, 'passed': result['passed'],
        'status': 'passed' if result['passed'] else ('requires-pr' if kind == 'pr' else 'failed'),
        **{key: value for key, value in result.items() if key != 'passed'},
    }


def landing_require_clean_worktrees(repo_root):
    checked = []
    for worktree in get_worktrees(repo_root):
        path = worktree.get('path')
        if not path:
            continue
        ensure_clean_worktree(path)
        checked.append(path)
    return checked


def landing_stack_projection_is_exact(repo_root, stack):
    if not branch_exists(repo_root, stack['branch']):
        raise SyncwheelError(f"stack land STOP: source branch is missing: {stack['branch']}")
    if not ref_exists(repo_root, stack['base']):
        raise SyncwheelError(f"stack land STOP: source base is missing: {stack['base']}")
    declared = [commit_full_sha(repo_root, commit) for commit in stack['commits']]
    actual = rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
    if actual != declared:
        raise SyncwheelError(
            'stack land STOP: source branch does not exactly equal its declared stack commits; '
            'rebuild or update the manifest before landing'
        )
    return declared


def landing_target_observation(repo_root, manifest, stack):
    canonical = manifest['defaults']['canonical_remote']
    if stack['target_remote'] != canonical:
        raise SyncwheelError('stack land STOP: direct landing is limited to the canonical remote')
    if not remote_is_configured(repo_root, canonical):
        raise SyncwheelError(f'stack land STOP: canonical remote is not configured: {canonical}')
    target_ref = f"refs/heads/{stack['target_branch']}"
    if target_ref in managed_ref_names(manifest) or (
        coordination_config(manifest) and target_ref == coordination_state_ref(coordination_config(manifest))
    ):
        raise SyncwheelError('stack land STOP: delivery branch overlaps a Syncwheel-managed ref')
    observed = remote_ref_tips(repo_root, canonical, [target_ref])[target_ref]
    if not observed:
        raise SyncwheelError(f'stack land STOP: delivery branch is absent on canonical remote: {target_ref}')
    fetched = git(repo_root, 'fetch', '--quiet', canonical, target_ref, check=False)
    if fetched.returncode != 0 or ref_tip(repo_root, 'FETCH_HEAD') != observed:
        raise SyncwheelError('stack land STOP: delivery branch could not be fetched at its observed revision')
    return canonical, target_ref, observed


def landing_candidate_plan(repo_root, stack, delivery_revision, strategy):
    source_revision = commit_full_sha(repo_root, stack['branch'])
    if branch_contains(repo_root, delivery_revision, source_revision):
        return {'kind': 'already-landed', 'revision': delivery_revision, 'sourceRevision': source_revision}
    if branch_contains(repo_root, source_revision, delivery_revision):
        return {'kind': 'fast-forward', 'revision': source_revision, 'sourceRevision': source_revision}
    if strategy == 'ff-only':
        return {'kind': 'requires-pr', 'reason': 'delivery branch is not an ancestor of the source branch'}
    merge_tree = git(repo_root, 'merge-tree', '--write-tree', delivery_revision, source_revision, check=False)
    if merge_tree.returncode != 0:
        return {'kind': 'requires-pr', 'reason': 'automatic merge has conflicts'}
    match = re.search(r'^[0-9a-f]{40}$', merge_tree.stdout, re.MULTILINE)
    if not match:
        raise SyncwheelError('stack land STOP: merge-tree did not produce a tree object')
    authored = max(
        int(git(repo_root, 'show', '-s', '--format=%ct', delivery_revision).stdout.strip()),
        int(git(repo_root, 'show', '-s', '--format=%ct', source_revision).stdout.strip()),
    )
    return {
        'kind': 'merge', 'tree': match.group(0), 'parents': [delivery_revision, source_revision],
        'message': f"Merge stack '{stack['id']}' into {stack['target_branch']} via Syncwheel\n",
        'timestamp': authored,
        'sourceRevision': source_revision,
    }


def materialize_landing_candidate(repo_root, candidate):
    if candidate['kind'] != 'merge':
        return candidate.get('revision')
    env = {
        'GIT_AUTHOR_NAME': 'Syncwheel Landing', 'GIT_AUTHOR_EMAIL': 'landing@syncwheel.invalid',
        'GIT_COMMITTER_NAME': 'Syncwheel Landing', 'GIT_COMMITTER_EMAIL': 'landing@syncwheel.invalid',
        'GIT_AUTHOR_DATE': f"{candidate['timestamp']} +0000",
        'GIT_COMMITTER_DATE': f"{candidate['timestamp']} +0000",
    }
    return git(
        repo_root, 'commit-tree', candidate['tree'], '-p', candidate['parents'][0], '-p', candidate['parents'][1],
        input_text=candidate['message'], env=env,
    ).stdout.strip()


def landing_active_coordination_gate(repo_root, manifest):
    if not coordination_is_active(manifest):
        return None
    config = coordination_config(manifest)
    remote = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    state = remote.get('state')
    if not state or 'landing' not in state.get('manifest', {}):
        raise SyncwheelError(
            'stack land STOP: active-active coordination state predates landing policy; '
            'publish a coordinated manifest update before direct landing'
        )
    if state.get('manifest_digest') != coordination_manifest_digest(manifest, repo_root):
        raise SyncwheelError('stack land STOP: active-active coordination manifest is not aligned')
    if not coordination_state_matches_remote(repo_root, config, state):
        raise SyncwheelError('stack land STOP: active-active coordination refs are not aligned')
    managed = state.get('managed_refs') or {}
    for branch in [manifest['integration']['branch'], *(stack['branch'] for stack in manifest['stacks'])]:
        ref = f'refs/heads/{branch}'
        if managed.get(ref) != ref_tip(repo_root, branch):
            raise SyncwheelError(
                'stack land STOP: local active-active source or integration ref is not aligned: ' + branch
            )
    return {'remote': config['remote'], 'stateRef': coordination_state_ref(config), 'stateRevision': remote['tip']}


def build_stack_land_plan(repo_root, manifest, manifest_path, stack_id, args):
    require_delivery_manifest(manifest)
    stack = require_stack(manifest, stack_id)
    policy = landing_policy(manifest)
    requirement_overrides, group_overrides, _override_all, _reason = landing_override_sets(args)
    if policy['checks']:
        unknown_requirements = sorted(requirement_overrides - landing_leaf_ids(policy['checks']))
        if unknown_requirements:
            raise SyncwheelError('unknown or non-leaf --override-requirement id(s): ' + ', '.join(unknown_requirements))
        unknown_groups = sorted(group_overrides - landing_group_ids(policy['checks']))
        if unknown_groups:
            raise SyncwheelError('unknown or non-group --override-group id(s): ' + ', '.join(unknown_groups))
        landing_attestation_paths(args, policy['checks'])
    elif (
        requirement_overrides or group_overrides or getattr(args, 'attestation', None)
        or getattr(args, 'override_all_checks', False)
    ):
        raise SyncwheelError('landing policy does not define checks to override or attest')
    if policy['mode'] != 'direct' and not getattr(args, 'allow_direct', False):
        raise SyncwheelError(
            'stack land STOP: direct landing is disabled; use --allow-direct for this explicit request '
            'or run syncwheel stack promote ' + stack_id
        )
    validation = validate_manifest(repo_root, manifest)
    if validation['errors']:
        raise SyncwheelError('stack land STOP: manifest validation failed: ' + '; '.join(validation['errors']))
    derived_source = [
        commit_full_sha(repo_root, commit)
        for commit in rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
        if is_derived_projection_commit(repo_root, manifest, commit)
    ]
    if derived_source:
        raise SyncwheelError(
            'stack land STOP: source contains derived projection commit(s): '
            + ', '.join(derived_source)
        )
    worktrees = landing_require_clean_worktrees(repo_root)
    declared = landing_stack_projection_is_exact(repo_root, stack)
    integration = manifest['integration']
    if stack_id not in integration.get('stacks', []):
        raise SyncwheelError('stack land STOP: stack is not included in main-integration')
    if not branch_exists(repo_root, integration['branch']):
        raise SyncwheelError('stack land STOP: main-integration branch is missing')
    for commit in stack_integration_commits(stack):
        if not branch_contains(repo_root, integration['branch'], commit):
            raise SyncwheelError('stack land STOP: stack is not validated on main-integration')
    expected_integration_tree = materialize_integration_projection(repo_root, manifest)
    actual_integration_tree = ref_tree(repo_root, integration['branch'])
    if (
        actual_integration_tree != expected_integration_tree
        and not trees_differ_only_by_manifest(
            repo_root, actual_integration_tree, expected_integration_tree
        )
    ):
        raise SyncwheelError('stack land STOP: main-integration does not match the declared combined projection')
    remote, target_ref, delivery_revision = landing_target_observation(repo_root, manifest, stack)
    for dependency_id in stack.get('depends_on', []):
        dependency = require_stack(manifest, dependency_id)
        if not branch_contains(repo_root, delivery_revision, dependency['branch']):
            raise SyncwheelError(f'stack land STOP: dependency is not yet delivered: {dependency_id}')
    coordination = landing_active_coordination_gate(repo_root, manifest)
    candidate = landing_candidate_plan(repo_root, stack, delivery_revision, policy['strategy'])
    revisions = {
        'stack': commit_full_sha(repo_root, stack['branch']),
        'integration': commit_full_sha(repo_root, integration['branch']),
        'delivery': delivery_revision,
    }
    checks = (
        evaluate_landing_requirement(repo_root, policy['checks'], revisions, manifest_digest(manifest), args)
        if policy['checks'] else {'id': 'syncwheel-structural', 'kind': 'structural', 'passed': True, 'status': 'passed'}
    )
    ready = candidate['kind'] not in {'requires-pr'} and checks['passed']
    status = 'ready' if ready else ('requires-pr' if candidate['kind'] == 'requires-pr' or checks['status'] == 'requires-pr' else 'blocked')
    plan = {
        'schemaVersion': STACK_LAND_PLAN_SCHEMA_VERSION,
        'kind': 'stackLandPlan', 'stack': stack_id,
        'manifestDigest': manifest_digest(manifest), 'policy': policy,
        'request': {
            'allowDirect': bool(getattr(args, 'allow_direct', False)),
            'overrideRequirements': sorted(getattr(args, 'override_requirement', None) or []),
            'overrideGroups': sorted(getattr(args, 'override_group', None) or []),
            'overrideAllChecks': bool(getattr(args, 'override_all_checks', False)),
            'overrideReason': landing_override_sets(args)[3],
        },
        'source': {'branch': stack['branch'], 'revision': revisions['stack'], 'commits': declared, 'base': stack['base']},
        'integration': {'branch': integration['branch'], 'revision': revisions['integration']},
        'delivery': {'remote': remote, 'ref': target_ref, 'revision': delivery_revision},
        'coordination': coordination,
        'worktrees': worktrees, 'checks': checks, 'candidate': candidate,
        'status': status,
        'actions': [] if candidate['kind'] == 'already-landed' else [{
            'type': 'exact-lease-push', 'remote': remote, 'ref': target_ref,
            'expectedRevision': delivery_revision, 'strategy': candidate['kind'],
        }],
        'next': (
            f'syncwheel stack promote {stack_id}' if status == 'requires-pr'
            else (f'syncwheel stack close {stack_id}' if ready else 'resolve failed local checks and preview again')
        ),
    }
    plan['planDigest'] = canonical_json_digest(plan)
    requested = normalize_channel_operation_id(getattr(args, 'operation_id', None))
    plan['operationId'] = requested or 'land-' + hashlib.sha256(
        f"stack-land:{plan['planDigest']}".encode('utf-8')
    ).hexdigest()[:24]
    return plan


def stack_land_events(repo_root, manifest_path, operation_id):
    return [
        event for event in load_ledger_events(repo_root, manifest_path)
        if event.get('type') in {'stack_land_started', 'stack_land_prepared', 'stack_land_receipt'}
        and (event.get('payload') or {}).get('operationId') == operation_id
    ]


def record_stack_land_event(repo_root, manifest_path, event_type, plan, **extra):
    payload = {
        'operationId': plan['operationId'], 'planDigest': plan['planDigest'], 'stack': plan['stack'],
        'status': extra.pop('status', None), 'delivery': plan['delivery'], 'candidate': plan['candidate'],
        'request': plan.get('request') or {}, 'next': plan.get('next'),
        **extra,
    }
    append_ledger_event(repo_root, event_type, payload, manifest_path)
    return payload


def return_existing_stack_land_operation(repo_root, manifest_path, plan):
    events = stack_land_events(repo_root, manifest_path, plan['operationId'])
    if not events:
        return None
    if any((event.get('payload') or {}).get('planDigest') != plan['planDigest'] for event in events):
        raise SyncwheelError(f'stack land operation id collision: {plan["operationId"]} is bound to another plan')
    terminal = next((event.get('payload') or {} for event in reversed(events) if event.get('type') == 'stack_land_receipt'), None)
    if terminal:
        return terminal
    candidate_revision = materialize_landing_candidate(repo_root, plan['candidate']) if plan['candidate']['kind'] != 'requires-pr' else None
    observed = remote_ref_tips(repo_root, plan['delivery']['remote'], [plan['delivery']['ref']])[plan['delivery']['ref']]
    if candidate_revision and observed == candidate_revision:
        return record_stack_land_event(
            repo_root, manifest_path, 'stack_land_receipt', plan, status='succeeded-equivalent',
            observedRevision=observed, reconciled=True,
        )
    if observed == plan['delivery']['revision']:
        return record_stack_land_event(
            repo_root, manifest_path, 'stack_land_receipt', plan, status='failed',
            observedRevision=observed, reconciled=True,
        )
    raise SyncwheelError(
        f'stack land operation {plan["operationId"]} has an unknown outcome; remote delivery ref diverged'
    )


def reconcile_prepared_stack_land_operation(repo_root, manifest_path, operation_id, plan_digest):
    events = stack_land_events(repo_root, manifest_path, operation_id)
    if not events:
        return None
    if any((event.get('payload') or {}).get('planDigest') != plan_digest for event in events):
        raise SyncwheelError(f'stack land operation id collision: {operation_id} is bound to another plan')
    terminal = next(
        (event.get('payload') or {} for event in reversed(events)
         if event.get('type') == 'stack_land_receipt'),
        None,
    )
    if terminal:
        return terminal
    prepared = next(
        (event.get('payload') or {} for event in reversed(events)
         if event.get('type') == 'stack_land_prepared'),
        None,
    )
    if not prepared:
        raise SyncwheelError(f'stack land operation {operation_id} is incomplete without a prepared intent')
    delivery = prepared.get('delivery') or {}
    candidate = prepared.get('candidate') or {}
    if not all(isinstance(delivery.get(key), str) and delivery[key] for key in ('remote', 'ref', 'revision')):
        raise SyncwheelError(f'stack land operation {operation_id} has an invalid prepared delivery intent')
    candidate_revision = materialize_landing_candidate(repo_root, candidate) if candidate.get('kind') != 'requires-pr' else None
    observed = remote_ref_tips(repo_root, delivery['remote'], [delivery['ref']])[delivery['ref']]
    replay_plan = {
        'operationId': operation_id, 'planDigest': plan_digest, 'stack': prepared.get('stack'),
        'delivery': delivery, 'candidate': candidate, 'request': prepared.get('request') or {},
        'next': prepared.get('next'),
    }
    if candidate_revision and observed == candidate_revision:
        return record_stack_land_event(
            repo_root, manifest_path, 'stack_land_receipt', replay_plan,
            status='succeeded-equivalent', observedRevision=observed, reconciled=True,
        )
    if observed == delivery['revision']:
        return record_stack_land_event(
            repo_root, manifest_path, 'stack_land_receipt', replay_plan,
            status='failed', observedRevision=observed, reconciled=True,
        )
    raise SyncwheelError(f'stack land operation {operation_id} has an unknown outcome; remote delivery ref diverged')


def command_stack_land(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    if args.apply and getattr(args, 'operation_id', None) and getattr(args, 'plan_digest', None):
        existing = reconcile_prepared_stack_land_operation(
            repo_root, manifest_path, args.operation_id, args.plan_digest
        )
        if existing:
            print(json.dumps(existing, indent=2))
            return 0
    plan = build_stack_land_plan(repo_root, manifest, manifest_path, args.stack, args)
    if not args.apply:
        print(json.dumps(plan, indent=2))
        return 0
    if not isinstance(args.plan_digest, str) or not args.plan_digest:
        raise SyncwheelError('--plan-digest is required with --apply')
    if args.plan_digest != plan['planDigest']:
        raise SyncwheelError('stack land plan is stale; generate a new preview and use its exact planDigest')
    with manifest_write_transaction(repo_root, manifest_path, f'stack-land-{args.stack}'):
        manifest = require_locked_manifest_observation(repo_root, manifest_path, {'manifestDigestBefore': plan['manifestDigest']})
        current = build_stack_land_plan(repo_root, manifest, manifest_path, args.stack, args)
        if current['planDigest'] != args.plan_digest:
            raise SyncwheelError('stack land plan is stale after revalidation; generate a new preview')
        if current['status'] != 'ready':
            route = current['next']
            raise SyncwheelError(f'stack land STOP: plan is {current["status"]}; {route}')
        existing = return_existing_stack_land_operation(repo_root, manifest_path, current)
        if existing:
            print(json.dumps(existing, indent=2))
            return 0
        lease_token = None
        config = coordination_config(manifest)
        if coordination_is_active(manifest):
            lease_token = acquire_local_coordination_lease(repo_root, config, installation_id(create=True))
        try:
            record_stack_land_event(repo_root, manifest_path, 'stack_land_started', current, status='started')
            record_stack_land_event(repo_root, manifest_path, 'stack_land_prepared', current, status='prepared')
            if current['candidate']['kind'] == 'already-landed':
                receipt = record_stack_land_event(
                    repo_root, manifest_path, 'stack_land_receipt', current, status='already-landed',
                    observedRevision=current['delivery']['revision'],
                )
                print(json.dumps(receipt, indent=2))
                return 0
            candidate_revision = materialize_landing_candidate(repo_root, current['candidate'])
            command = [
                'git', 'push', f"--force-with-lease={current['delivery']['ref']}:{current['delivery']['revision']}",
                current['delivery']['remote'], f"{candidate_revision}:{current['delivery']['ref']}",
            ]
            result = run_authorized_push(
                repo_root, command, current['delivery']['remote'], [current['delivery']['ref']], check=False
            )
            observed = remote_ref_tips(repo_root, current['delivery']['remote'], [current['delivery']['ref']])[current['delivery']['ref']]
            if observed == candidate_revision:
                status = 'succeeded' if result.returncode == 0 else 'succeeded-equivalent'
                receipt = record_stack_land_event(
                    repo_root, manifest_path, 'stack_land_receipt', current, status=status,
                    candidateRevision=candidate_revision, observedRevision=observed,
                )
                print(json.dumps(receipt, indent=2))
                return 0
            status = 'failed' if observed == current['delivery']['revision'] else 'unknown'
            receipt = record_stack_land_event(
                repo_root, manifest_path, 'stack_land_receipt', current, status=status,
                candidateRevision=candidate_revision, observedRevision=observed,
                detail=(result.stderr or result.stdout).strip()[:1000] or None,
            )
            if status == 'failed':
                raise SyncwheelError(
                    'stack land STOP: canonical remote rejected direct landing; '
                    'use syncwheel stack promote ' + current['stack']
                )
            raise SyncwheelError(
                f'stack land outcome is unknown; repeat with --operation-id {current["operationId"]} '
                'and the same --plan-digest to reconcile without retrying the push'
            )
        finally:
            if lease_token:
                release_local_coordination_lease(repo_root, lease_token)


def pending_stack_close_operation(repo_root, manifest_path, stack_id):
    events = load_ledger_events(repo_root, manifest_path)
    terminal_tokens = {
        (event.get('payload') or {}).get('operation_token')
        for event in events
        if event.get('type') in {'stack_closed', 'stack_close_abandoned'}
    }
    for event in reversed(events):
        if event.get('type') != 'stack_close_intent':
            continue
        payload = event.get('payload') or {}
        token = payload.get('operation_token')
        if payload.get('stack') == stack_id and token and token not in terminal_tokens:
            return payload
    return None


def stack_closed_payload(intent, coordination_state=None, recovered=False):
    return {
        'stack': intent['stack'],
        'branch': intent['branch'],
        'reason': intent['reason'],
        'operation_token': intent['operation_token'],
        'coordination_state': coordination_state,
        'delivery_tip': intent.get('delivery_tip'),
        'recovered': recovered,
    }


def remote_first_close_failure(stack_id, exc):
    return SyncwheelError(
        f'{stack_id}: remote-first close could not inspect the coordination remote; '
        f'restore remote access, then retry:\n  '
        f'syncwheel stack close {stack_id} --force'
    )


def abandon_superseded_stack_close(repo_root, manifest_path, pending):
    append_ledger_event(
        repo_root,
        'stack_close_abandoned',
        {
            'stack': pending['stack'],
            'branch': pending.get('branch'),
            'operation_token': pending['operation_token'],
            'reason': 'close_superseded',
            'status': 'close_superseded',
        },
        manifest_path,
    )


def published_close_tombstone(repo_root, manifest, pending):
    config = coordination_config(manifest)
    if not config or not pending.get('remote_first'):
        return None
    closed_ref = pending.get('closed_ref')
    if not closed_ref:
        return None
    claim_ref = coordination_claim_ref(closed_ref)
    published = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    claim_tip = remote_ref_tips(repo_root, config['remote'], [claim_ref])[claim_ref]
    state = published.get('state') or {}
    if not claim_tip or state.get('claims', {}).get(closed_ref) != claim_tip:
        return None
    claim = fetch_coordination_claim(
        repo_root, config['remote'], claim_ref, claim_tip
    )
    if (
        claim.get('coordination_id') != config['id']
        or claim.get('source_ref') != closed_ref
        or claim.get('operation_token') != pending.get('operation_token')
        or claim.get('closed') is not True
    ):
        return None
    return {'state_tip': published['tip'], 'claim_tip': claim_tip}


def recover_pending_stack_close(repo_root, manifest, manifest_path, pending):
    if manifest_digest(manifest) != pending.get('manifest_digest_after'):
        return False
    published = published_close_tombstone(repo_root, manifest, pending)
    if pending.get('remote_first') and not published:
        return False
    append_ledger_event(
        repo_root,
        'stack_closed',
        stack_closed_payload(
            pending,
            coordination_state=(published or {}).get('state_tip') or pending.get('coordination_state'),
            recovered=True,
        ),
        manifest_path,
    )
    return True


def command_stack_close(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    original_manifest = copy.deepcopy(manifest)
    pending_close = pending_stack_close_operation(repo_root, manifest_path, args.stack)
    stack = stack_map(manifest).get(args.stack)
    if stack is None:
        if pending_close:
            try:
                if recover_pending_stack_close(
                    repo_root, manifest, manifest_path, pending_close
                ):
                    print(f'{args.stack}: recovered interrupted stack close')
                    return 0
            except SyncwheelError as exc:
                raise remote_first_close_failure(args.stack, exc) from exc
            raise SyncwheelError(
                f'{args.stack}: interrupted stack close found, but the manifest no longer '
                'matches its intended result; inspect the stack_close_intent ledger event '
                'and reconcile the manifest before retrying'
            )
        raise SyncwheelError(f'unknown stack: {args.stack}')
    branch = stack['branch']
    base_ref = stack.get('base') or manifest['defaults']['base_ref']

    referencing_channels = channel_ids_referencing_stack(manifest, args.stack)
    if referencing_channels:
        raise SyncwheelError(
            f"stack {args.stack} is referenced by active channel(s): "
            + ', '.join(referencing_channels)
            + '; remove or replace the stack in those channels, or close the channels first'
        )
    dependent_stacks = sorted(
        candidate['id'] for candidate in manifest.get('stacks', [])
        if candidate['id'] != args.stack
        and args.stack in candidate.get('depends_on', [])
    )
    if dependent_stacks:
        raise SyncwheelError(
            f"stack {args.stack} is required by dependent stack(s): "
            + ', '.join(dependent_stacks)
            + '; close or update those dependent stacks first'
        )

    reason = (
        pending_close.get('reason')
        if pending_close else (args.reason or 'closed')
    )
    pending_remote_state = None
    if (
        pending_close
        and coordination_is_active(manifest)
        and stack.get('state', 'published') == 'draft'
        and pending_close.get('remote_first')
    ):
        recovered_manifest = copy.deepcopy(manifest)
        recovered_manifest['stacks'] = [
            item for item in recovered_manifest['stacks'] if item['id'] != args.stack
        ]
        recovered_manifest['integration']['stacks'] = [
            item for item in recovered_manifest['integration'].get('stacks', [])
            if item != args.stack
        ]
        try:
            published = (
                published_close_tombstone(
                    repo_root, recovered_manifest, pending_close
                )
                if manifest_digest(recovered_manifest)
                == pending_close.get('manifest_digest_after')
                else None
            )
        except SyncwheelError as exc:
            raise remote_first_close_failure(args.stack, exc) from exc
        if published:
            require_manifest_transaction_current(manifest_path)
            save_manifest(manifest_path, recovered_manifest)
            append_ledger_event(
                repo_root,
                'stack_closed',
                stack_closed_payload(
                    pending_close,
                    coordination_state=published['state_tip'],
                    recovered=True,
                ),
                manifest_path,
            )
            abandon_pending_stack_creates(
                repo_root, manifest_path, args.stack, 'stack_closed'
            )
            print(f'{args.stack}: recovered interrupted remote-first stack close')
            return 0
        config = coordination_config(manifest)
        claim_ref = coordination_claim_ref(pending_close['closed_ref'])
        observed_at_intent = pending_close.get('expected_observed_refs') or {}
        generation_refs = {
            pending_close['closed_ref']: observed_at_intent.get(
                pending_close['closed_ref'], pending_close.get('expected_ref_tip')
            ),
            claim_ref: observed_at_intent.get(
                claim_ref, pending_close.get('expected_claim_tip')
            ),
        }
        try:
            pending_remote_state = read_remote_coordination_state(
                repo_root, config, fetch=True,
                local_manifest_version=manifest['version'],
            )
            current_refs = remote_ref_tips(
                repo_root, config['remote'], generation_refs
            )
        except SyncwheelError as exc:
            raise remote_first_close_failure(args.stack, exc) from exc
        if current_refs != generation_refs:
            abandon_superseded_stack_close(
                repo_root, manifest_path, pending_close
            )
            raise SyncwheelError(
                f'{args.stack}: close_superseded; the pending close generation is '
                'no longer current and was abandoned; this retry changed neither '
                'the manifest nor the remote'
            )
    delivery_tip = None
    if reason == 'absorbed':
        delivery_base = f"{stack['target_remote']}/{stack['target_branch']}"
        projected_tip = composed_stack_projection_tip(repo_root, stack)
        delivery_tip = fetch_observed_delivery_tip(
            repo_root, stack['target_remote'], stack['target_branch']
        )
        if not projected_tip or not stack_content_is_present_at_delivery_tip(
            repo_root,
            stack,
            delivery_tip,
            projected_tip=projected_tip,
        ):
            raise SyncwheelError(
                f"{args.stack}: cannot close as absorbed: content is not reachable from delivery base "
                f"{delivery_base} at {delivery_tip}; rebuilding integration projection "
                f"{manifest['integration']['branch']} "
                'would drop it. Deliver or preserve the stack first, then use a different close reason.'
            )

    # Check whether every commit in the stack is already reachable from base_ref.
    unmerged = []
    for sha in stack.get('commits') or []:
        result = git(repo_root, 'merge-base', '--is-ancestor', sha, base_ref, check=False)
        if result.returncode != 0:
            unmerged.append(sha)

    if reason == 'absorbed':
        unmerged = []
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

    if pending_close is None and args.reason is None:
        reason = 'merged' if not unmerged else 'closed'
    coordination_result = None
    config = None
    closed_ref = f'refs/heads/{branch}'
    remote_first_close = (
        coordination_is_active(manifest)
        and stack.get('state', 'published') == 'draft'
    )
    expected_state = None
    expected_observation = None
    remote_tip = None
    claim_tip = None
    publication_manifest = manifest
    if remote_first_close:
        config = coordination_config(manifest)
        try:
            expected_state = pending_remote_state or read_remote_coordination_state(
                repo_root, config, fetch=True,
                local_manifest_version=manifest['version'],
            )
            published_state = expected_state.get('state') or {}
            published_stacks = stack_snapshot_map(published_state.get('manifest') or {})
            if args.stack not in published_stacks:
                if published_state.get('manifest'):
                    publication_manifest = apply_coordination_snapshot(
                        manifest, published_state['manifest']
                    )
                else:
                    publication_manifest = copy.deepcopy(manifest)
                    publication_manifest['stacks'] = []
                    publication_manifest['integration']['stacks'] = []
            managed = list(dict.fromkeys([
                *managed_ref_names(publication_manifest), closed_ref,
            ]))
            claim_ref = coordination_claim_ref(closed_ref)
            expected_observation = remote_ref_tips(
                repo_root, config['remote'], [*managed, claim_ref]
            )
            remote_tip = expected_observation[closed_ref]
            claim_tip = expected_observation[claim_ref]
            if claim_tip:
                claim = fetch_coordination_claim(
                    repo_root, config['remote'], claim_ref, claim_tip
                )
                if claim['coordination_id'] != config['id']:
                    raise SyncwheelError(
                        f'{closed_ref} is claimed by coordination domain '
                        f'{claim["coordination_id"]}; refusing close'
                    )
        except SyncwheelError as exc:
            raise remote_first_close_failure(args.stack, exc) from exc
    operation_token = (
        pending_close.get('operation_token') if pending_close else str(uuid.uuid4())
    )
    close_intent = {
        'stack': args.stack,
        'branch': branch,
        'reason': reason,
        'operation_token': operation_token,
        'remote_first': remote_first_close,
        'closed_ref': closed_ref,
        'expected_coordination_state_tip': (
            expected_state.get('tip') if expected_state else None
        ),
        'expected_ref_tip': remote_tip,
        'expected_claim_tip': claim_tip,
        'expected_observed_refs': expected_observation,
        'manifest_digest_before': manifest_digest(original_manifest),
        'manifest_digest_after': manifest_digest(manifest),
        'coordination_manifest_digest_after': canonical_json_digest(
            coordination_manifest_snapshot(publication_manifest, repo_root)
        ),
        'delivery_tip': delivery_tip,
    }
    if pending_close is None:
        append_ledger_event(
            repo_root, 'stack_close_intent', close_intent, manifest_path
        )
    else:
        close_intent = pending_close
    require_manifest_transaction_current(manifest_path)
    if coordination_is_active(manifest):
        config = config or coordination_config(manifest)
        try:
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
                expected_coordination_state_tip=(
                    expected_state.get('tip')
                    if expected_state is not None
                    else EXPECTED_COORDINATION_STATE_UNSET
                ),
                expected_observed_refs=expected_observation,
                remedy_stack=args.stack,
                operation_token=operation_token,
                publication_manifest=publication_manifest,
            )
        except SyncwheelError as exc:
            raise SyncwheelError(
                f'{exc}\nRemote-first close did not save the manifest. Retry:\n  '
                f'syncwheel stack close {args.stack} --force'
            ) from exc
    save_manifest(manifest_path, manifest)
    close_intent['coordination_state'] = (
        coordination_result.get('state_tip') if coordination_result else None
    )
    append_ledger_event(
        repo_root,
        'stack_closed',
        stack_closed_payload(
            close_intent,
            coordination_state=close_intent.get('coordination_state'),
        ),
        manifest_path,
    )
    abandon_pending_stack_creates(
        repo_root, manifest_path, args.stack, 'stack_closed'
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


def stack_create_recorded(repo_root, manifest_path, stack_id):
    return any(
        event.get('type') == 'manifest_saved'
        and (event.get('payload') or {}).get('reason') == 'stack_create'
        and ((event.get('payload') or {}).get('context') or {}).get('stack') == stack_id
        for event in load_ledger_events(repo_root, manifest_path)
    )


def unmatched_stack_create_operations(repo_root, manifest_path, stack_id=None):
    """Return create intents not closed by completion or explicit abandonment."""
    events = load_ledger_events(repo_root, manifest_path)
    terminal_tokens = {
        ((event.get('payload') or {}).get('context') or {}).get('operation_token')
        for event in events
        if event.get('type') == 'manifest_saved'
        and (event.get('payload') or {}).get('reason') == 'stack_create'
    }
    terminal_tokens.update(
        (event.get('payload') or {}).get('operation_token')
        for event in events
        if event.get('type') == 'stack_create_abandoned'
    )
    last_closed_seq = {}
    for event in events:
        if event.get('type') != 'stack_closed':
            continue
        payload = event.get('payload') or {}
        closed_stack = payload.get('stack')
        if closed_stack:
            last_closed_seq[closed_stack] = event.get('seq', 0)
    pending = []
    for event in reversed(events):
        if event.get('type') != 'stack_create_intent':
            continue
        payload = event.get('payload') or {}
        if stack_id is not None and payload.get('stack') != stack_id:
            continue
        token = payload.get('operation_token')
        if (
            token
            and token not in terminal_tokens
            and event.get('seq', 0) > last_closed_seq.get(payload.get('stack'), 0)
        ):
            pending.append(payload)
    return pending


def pending_stack_create_operation(repo_root, manifest_path, stack_id, branch, tip):
    """Return the newest unmatched intent token for this exact create lifecycle."""
    for payload in unmatched_stack_create_operations(
        repo_root, manifest_path, stack_id
    ):
        if payload.get('branch') == branch and payload.get('tip') == tip:
            return payload.get('operation_token')
    return None


def abandon_pending_stack_creates(repo_root, manifest_path, stack_id, reason):
    for payload in unmatched_stack_create_operations(repo_root, manifest_path, stack_id):
        append_ledger_event(
            repo_root,
            'stack_create_abandoned',
            {
                'stack': stack_id,
                'branch': payload.get('branch'),
                'tip': payload.get('tip'),
                'operation_token': payload.get('operation_token'),
                'reason': reason,
            },
            manifest_path,
        )


def require_current_stack_create_operation(
    repo_root, manifest_path, stack_id, branch, tip, operation_token
):
    if not operation_token:
        return
    current = pending_stack_create_operation(
        repo_root, manifest_path, stack_id, branch, tip
    )
    if current != operation_token:
        raise SyncwheelError(
            f'{stack_id}: refusing late completion for superseded create operation '
            f'{operation_token}'
        )


def draft_create_retry_command(args):
    command = ['syncwheel', 'stack', 'create', args.stack, *args.specs, '--draft']
    options = (
        ('--base', args.base),
        ('--target-remote', args.target_remote),
        ('--target-branch', args.target_branch),
        ('--integration-branch', args.integration_branch),
        ('--purpose', args.purpose),
    )
    for flag, value in options:
        if value:
            command.extend([flag, value])
    for dependency in getattr(args, 'depends_on', None) or []:
        command.extend(['--depends-on', dependency])
    if args.include_in_integration:
        command.append('--include-in-integration')
    return quoted(command)


def recover_equivalent_draft_create(
    repo_root, manifest, manifest_path, stack, operation_token
):
    """Adopt a completed atomic draft publication after a local persistence failure."""
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        return None
    source_ref = f"refs/heads/{stack['branch']}"
    local_tip = ref_tip(repo_root, stack['branch'])
    published = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    state = published.get('state') or {}
    claim_ref = coordination_claim_ref(source_ref)
    observed = remote_ref_tips(
        repo_root, config['remote'], [source_ref, claim_ref]
    )
    claim_tip = observed[claim_ref]
    claim = (
        fetch_coordination_claim(repo_root, config['remote'], claim_ref, claim_tip)
        if claim_tip else None
    )
    publication_matches = (
        local_tip
        and state.get('managed_refs', {}).get(source_ref) == local_tip
        and observed[source_ref] == local_tip
        and state.get('claims', {}).get(source_ref) == claim_tip
        and claim
        and claim.get('coordination_id') == config['id']
        and claim.get('source_ref') == source_ref
        and claim.get('closed') is not True
        and state.get('manifest_digest') == canonical_json_digest(
            coordination_manifest_snapshot(manifest, repo_root)
        )
    )
    if publication_matches and (
        not operation_token or claim.get('operation_token') != operation_token
    ):
        raise SyncwheelError(
            f"{stack['id']}: published create claim token does not match the "
            'pending stack_create_intent; refusing foreign or superseded recovery'
        )
    if not publication_matches:
        return None
    return {'status': 'equivalent', 'state_tip': published['tip']}


def preflight_active_draft_create(repo_root, manifest, manifest_path, stack):
    """Validate ownership and composition before creating the local source branch."""
    config = coordination_config(manifest)
    if not config or config.get('mode') != 'active-active':
        return None
    source_ref = f"refs/heads/{stack['branch']}"
    planned_tip = deterministic_stack_replay_tip(repo_root, stack['base'], stack['commits'])
    if not planned_tip:
        raise SyncwheelError(
            f"{stack['id']}: cannot deterministically materialize draft source before coordinated publication"
        )
    planned_tip = commit_full_sha(repo_root, planned_tip)
    expected = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    managed = managed_ref_names(manifest)
    claim_ref = coordination_claim_ref(source_ref)
    observed = remote_ref_tips(
        repo_root, config['remote'], [*managed, claim_ref]
    )
    claim_tip = observed[claim_ref]
    if claim_tip:
        claim = fetch_coordination_claim(
            repo_root, config['remote'], claim_ref, claim_tip
        )
        if claim['coordination_id'] != config['id']:
            raise SyncwheelError(
                f'{source_ref} is claimed by coordination domain '
                f'{claim["coordination_id"]}; refusing publication'
            )
    coordination_state_refs = require_exclusive_coordination_ownership(
        repo_root, config, managed
    )
    if observed[source_ref] is not None and observed[source_ref] != planned_tip:
        raise SyncwheelError(
            f"{stack['id']}: unowned remote draft ref {source_ref} has a different tip; "
            'refusing to replace it'
        )
    validate_coordination_publication_base(
        repo_root,
        manifest,
        config,
        expected,
        {source_ref: planned_tip},
        remedy_stack=stack['id'],
        creation_remedy=True,
    )
    atomic_push_capability_probe(repo_root, config['remote'])
    return {
        'source_ref': source_ref,
        'planned_tip': planned_tip,
        'expected_coordination_state_tip': expected['tip'],
        'expected_observed_refs': observed,
        'expected_coordination_state_refs': coordination_state_refs,
    }


def command_stack_create(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stacks = stack_map(manifest)
    if args.stack in stacks:
        existing = stacks[args.stack]
        existing_tip = ref_tip(repo_root, existing['branch'])
        pending_operation = pending_stack_create_operation(
            repo_root,
            manifest_path,
            args.stack,
            existing['branch'],
            existing_tip,
        )
        if (
            args.draft
            and existing.get('state', 'published') == 'draft'
            and branch_exists(repo_root, existing['branch'])
            and (pending_operation or not stack_create_recorded(repo_root, manifest_path, args.stack))
        ):
            append_ledger_event(
                repo_root,
                'manifest_saved',
                manifest_event_payload(
                    manifest_path, manifest, 'stack_create',
                    {
                        'stack': args.stack,
                        'branch': existing['branch'],
                        'operation_token': pending_operation,
                        'recovered': True,
                    },
                ),
                manifest_path,
            )
            print(f"{args.stack}: recovered missing stack_create ledger event")
            return 0
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
    if getattr(args, 'depends_on', None):
        stack['depends_on'] = list(dict.fromkeys(args.depends_on))
    if stack.get('depends_on') and manifest['version'] != MANIFEST_VERSION_CHANNELS:
        raise SyncwheelError(
            'stack create --depends-on requires manifest version 3; migrate explicitly '
            'with channel create --apply first'
        )
    if args.purpose:
        stack['meta'] = {'purpose': args.purpose}
    manifest['stacks'].append(stack)
    validate_stack_dependency_graph(
        manifest['stacks'],
        require_declared_dependencies=manifest['version'] == MANIFEST_VERSION_CHANNELS,
    )
    integration_membership = manifest['defaults']['integration_membership']
    if (
        integration_membership == INTEGRATION_MEMBERSHIP_REQUIRED
        or args.include_in_integration
    ) and args.stack not in manifest['integration']['stacks']:
        manifest['integration']['stacks'].append(args.stack)
    coordination_result = None
    operation_token = None
    if args.draft:
        require_manifest_transaction_current(manifest_path)
        if branch_exists(repo_root, branch):
            existing_tip = ref_tip(repo_root, branch)
            operation_token = pending_stack_create_operation(
                repo_root, manifest_path, args.stack, branch, existing_tip
            )
            coordination_result = recover_equivalent_draft_create(
                repo_root, manifest, manifest_path, stack, operation_token
            )
            if coordination_result is not None:
                if operation_token is None:
                    abandon_pending_stack_creates(
                        repo_root, manifest_path, args.stack, 'superseded_by_recovery'
                    )
                    operation_token = str(uuid.uuid4())
                    append_ledger_event(
                        repo_root,
                        'stack_create_intent',
                        {
                            'stack': args.stack,
                            'branch': branch,
                            'tip': existing_tip,
                            'operation_token': operation_token,
                            'expected_coordination_state_tip': coordination_result['state_tip'],
                            'recovery': True,
                        },
                        manifest_path,
                    )
                save_manifest_with_ledger(
                    repo_root,
                    manifest_path,
                    manifest,
                    'stack_create',
                    {
                        'stack': args.stack,
                        'branch': branch,
                        'operation_token': operation_token,
                        'coordination_state': coordination_result.get('state_tip'),
                        'recovered': True,
                    },
                )
                capture_governed_worktrees_for_stack(repo_root, manifest, args.stack)
                print(f"{args.stack}: recovered equivalent published draft create")
                return 0
        preflight = preflight_active_draft_create(repo_root, manifest, manifest_path, stack)
        if preflight:
            operation_token = pending_stack_create_operation(
                repo_root, manifest_path, args.stack, branch, preflight['planned_tip']
            )
            if operation_token is None:
                abandon_pending_stack_creates(
                    repo_root, manifest_path, args.stack, 'superseded_by_new_create'
                )
                operation_token = str(uuid.uuid4())
                append_ledger_event(
                    repo_root,
                    'stack_create_intent',
                    {
                        'stack': args.stack,
                        'branch': branch,
                        'tip': preflight['planned_tip'],
                        'operation_token': operation_token,
                        'expected_coordination_state_tip': preflight['expected_coordination_state_tip'],
                    },
                    manifest_path,
                )
        cleanup_interrupted_draft_materialization(
            repo_root,
            stack,
            preflight['planned_tip'] if preflight else None,
            operation_token,
        )
        if branch_exists(repo_root, branch):
            if not preflight or ref_tip(repo_root, branch) != preflight['planned_tip']:
                raise SyncwheelError(f'draft stack branch already exists locally: {branch}')
        else:
            materialize_new_stack_branch(
                repo_root,
                stack,
                planned_tip=(preflight['planned_tip'] if preflight else None),
                operation_token=operation_token,
            )
            if preflight and ref_tip(repo_root, branch) != preflight['planned_tip']:
                raise SyncwheelError(f"{args.stack}: materialized draft tip differs from its reviewed projection")
        if coordination_is_active(manifest) and coordination_result is None:
            try:
                coordination_result = coordinated_publish(
                    repo_root,
                    manifest,
                    manifest_path,
                    {f"refs/heads/{branch}": ref_tip(repo_root, branch)},
                    f'create:{args.stack}',
                    'partial',
                    expected_coordination_state_tip=(
                        preflight['expected_coordination_state_tip'] if preflight else None
                    ),
                    expected_observed_refs=(preflight['expected_observed_refs'] if preflight else None),
                    expected_coordination_state_refs=(
                        preflight['expected_coordination_state_refs'] if preflight else None
                    ),
                    preflight_complete=bool(preflight),
                    remedy_stack=args.stack,
                    operation_token=operation_token,
                )
            except SyncwheelError as exc:
                raise SyncwheelError(
                    f'{exc}\nRetry this exact, idempotent create command after resolving the named condition:\n  '
                    f'{draft_create_retry_command(args)}'
                ) from exc
    save_manifest_with_ledger(
        repo_root,
        manifest_path,
        manifest,
        'stack_create',
        {
            'stack': args.stack,
            'branch': branch,
            'operation_token': operation_token,
            'coordination_state': coordination_result.get('state_tip') if coordination_result else None,
        },
    )
    capture_governed_worktrees_for_stack(
        repo_root,
        manifest,
        args.stack,
        manifest_path=manifest_path,
    )
    print(f"{args.stack}: created {branch} with {len(stack['commits'])} commits (state={stack['state']})")
    if coordination_result:
        print(f"  coordination state: {coordination_result['status']}")
    return 0


def draft_branch_creation_checkpoint():
    """Fault-injection seam immediately before atomic draft ref creation."""


def draft_materialization_worktree(repo_root, operation_token):
    if not operation_token:
        return None
    token = hashlib.sha256(
        f'{Path(repo_root).resolve()}:{operation_token}'.encode('utf-8')
    ).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / f'syncwheel-stack-create-{token}'


def cleanup_interrupted_draft_materialization(
    repo_root, stack, planned_tip, operation_token
):
    path = draft_materialization_worktree(repo_root, operation_token)
    if path is None:
        return
    registrations = [
        item for item in get_worktrees(repo_root)
        if Path(item.get('path', '')).resolve() == path.resolve()
    ]
    if not registrations:
        if path.exists():
            raise SyncwheelError(
                f"{stack['id']}: deterministic create worktree path exists without "
                f'a matching Git registration: {path}'
            )
        return
    registration = registrations[0]
    if (
        registration.get('branch') != stack['branch']
        or (planned_tip and registration.get('HEAD') != planned_tip)
    ):
        raise SyncwheelError(
            f"{stack['id']}: deterministic create worktree registration does not "
            'belong to the pending stack_create_intent'
        )
    removed = git(
        repo_root, 'worktree', 'remove', '--force', str(path), check=False
    )
    if removed.returncode != 0:
        raise SyncwheelError(
            f"{stack['id']}: could not remove interrupted create worktree {path}"
        )


def materialize_new_stack_branch(
    repo_root, stack, planned_tip=None, operation_token=None
):
    """Create a draft ref with create-only CAS and leave no worktree on failure."""
    planned_tip = planned_tip or deterministic_stack_replay_tip(
        repo_root, stack['base'], stack['commits']
    )
    if not planned_tip:
        raise SyncwheelError(
            f"{stack['id']}: cannot deterministically materialize draft source branch"
        )
    planned_tip = commit_full_sha(repo_root, planned_tip)
    branch_ref = f"refs/heads/{stack['branch']}"
    draft_branch_creation_checkpoint()
    created = git(
        repo_root,
        'update-ref',
        branch_ref,
        planned_tip,
        ZERO_OBJECT_ID,
        check=False,
    )
    if created.returncode != 0:
        raise SyncwheelError(
            f"{stack['id']}: draft ref {branch_ref} appeared concurrently; "
            'the existing ref was preserved. Inspect it, then retry with a new stack id.'
        )
    deterministic_worktree = draft_materialization_worktree(
        repo_root, operation_token
    )
    temporary = (
        None if deterministic_worktree
        else tempfile.TemporaryDirectory(prefix='syncwheel-stack-create-')
    )
    worktree = deterministic_worktree or Path(temporary.name)
    try:
        try:
            git(repo_root, 'worktree', 'add', str(worktree), stack['branch'])
            if ref_tip(repo_root, stack['branch']) != planned_tip:
                raise SyncwheelError(
                    f"{stack['id']}: materialized draft tip differs from its reviewed projection"
                )
        except BaseException:
            git(repo_root, 'worktree', 'remove', '--force', str(worktree), check=False)
            git(repo_root, 'update-ref', '-d', branch_ref, planned_tip, check=False)
            raise
        else:
            git(repo_root, 'worktree', 'remove', '--force', str(worktree), check=False)
    finally:
        if temporary:
            temporary.cleanup()


def recover_pending_stack_promote(
    repo_root, manifest, manifest_path, stack, pending
):
    rename = pending.get('rename')
    state_transition = pending.get('state_transition')
    if rename:
        if (
            rename.get('stack') != stack['id']
            or rename.get('from_branch') != stack['branch']
        ):
            raise SyncwheelError(
                f"{stack['id']}: pending promote intent does not match the local stack generation"
            )
        to_branch = rename.get('to_branch')
        if not to_branch or not branch_exists(repo_root, to_branch) or branch_exists(
            repo_root, stack['branch']
        ):
            raise SyncwheelError(
                f"{stack['id']}: pending promote intent requires the already-renamed "
                f'local branch {to_branch}'
            )
        stack['branch'] = to_branch
    elif state_transition != {
        'stack': stack['id'],
        'from_state': 'draft',
        'to_state': 'published',
    }:
        raise SyncwheelError(
            f"{stack['id']}: pending promote intent has an invalid state transition"
        )
    stack['state'] = 'published'
    stack['publication'] = {'enabled': True}
    tombstone = copy.deepcopy(pending.get('tombstone'))
    if tombstone:
        tombstone['closed_at'] = iso_utc_now()
    operation = renew_coordination_publication(
        repo_root, manifest, manifest_path, pending
    )
    result = coordinated_publish(
        repo_root,
        manifest,
        manifest_path,
        operation.get('changed_refs') or {},
        operation['scope'],
        operation['projection_status'],
        tombstone=tombstone,
        rename=rename,
        state_transition=state_transition,
        expected_coordination_state_tip=operation[
            'expected_coordination_state_tip'
        ],
        operation_token=operation['operation_token'],
    )
    save_manifest(manifest_path, manifest)
    append_ledger_event(
        repo_root,
        'stack_promoted',
        {
            'stack': stack['id'],
            'from_branch': rename.get('from_branch') if rename else stack['branch'],
            'branch': stack['branch'],
            'coordination_state': result.get('state_tip'),
            'recovered': True,
        },
        manifest_path,
    )
    complete_coordination_publication(repo_root, manifest_path, operation, result)
    print(f"{stack['id']}: recovered interrupted draft promotion")
    return 0


def command_stack_promote(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    pending_promotion = pending_coordination_publication_for_scope(
        repo_root, manifest_path, f'promote:{args.stack}'
    )
    if pending_promotion:
        return recover_pending_stack_promote(
            repo_root, manifest, manifest_path, stack, pending_promotion
        )
    if stack.get('state', 'published') != 'draft':
        raise SyncwheelError(f"{args.stack}: promote requires state draft (found {stack.get('state', 'published')})")
    from_branch = stack['branch']
    if not branch_exists(repo_root, from_branch):
        raise SyncwheelError(f"{args.stack}: cannot promote draft without a materialized branch: {from_branch}")
    to_branch = args.branch or f'pr/{safe_ref_segment(args.stack)}'
    if to_branch != from_branch:
        referencing_channels = channel_ids_referencing_stack(manifest, args.stack)
        if referencing_channels:
            raise SyncwheelError(
                f"stack {args.stack} promotion would change branch {from_branch!r} to "
                f"{to_branch!r}, but it is pinned by active channel(s): "
                + ', '.join(referencing_channels)
                + '; remove or replace the stack in those channels, or close the channels first'
            )
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
    publication_operation = None
    try:
        require_manifest_transaction_current(manifest_path)
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
                publication_operation = begin_coordination_publication(
                    repo_root,
                    manifest,
                    manifest_path,
                    {f'refs/heads/{to_branch}': ref_tip(repo_root, to_branch)},
                    f'promote:{args.stack}',
                    'partial',
                    tombstone=tombstone,
                    rename=rename,
                )
                coordination_result = coordinated_publish(
                    repo_root,
                    manifest,
                    manifest_path,
                    {f'refs/heads/{to_branch}': ref_tip(repo_root, to_branch)},
                    f'promote:{args.stack}',
                    'partial',
                    tombstone=tombstone,
                    rename=rename,
                    expected_coordination_state_tip=publication_operation[
                        'expected_coordination_state_tip'
                    ],
                    operation_token=publication_operation['operation_token'],
                )
            else:
                state_transition = {
                    'stack': args.stack,
                    'from_state': 'draft',
                    'to_state': 'published',
                }
                publication_operation = begin_coordination_publication(
                    repo_root,
                    manifest,
                    manifest_path,
                    {},
                    f'promote:{args.stack}',
                    'partial',
                    state_transition=state_transition,
                )
                coordination_result = coordinated_publish(
                    repo_root,
                    manifest,
                    manifest_path,
                    {},
                    f'promote:{args.stack}',
                    'partial',
                    state_transition=state_transition,
                    expected_coordination_state_tip=publication_operation[
                        'expected_coordination_state_tip'
                    ],
                    operation_token=publication_operation['operation_token'],
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
    if publication_operation:
        complete_coordination_publication(
            repo_root, manifest_path, publication_operation, coordination_result
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
    shared_channels = sorted(
        channel['id'] for channel in manifest.get('channels', [])
        if channel.get('lifecycle') == 'shared'
        and any(
            entry['stack'] == args.stack
            for entry in channel.get('composition', [])
        )
    )
    if shared_channels:
        raise SyncwheelError(
            f"stack {args.stack} cannot be demoted while referenced by shared channel(s): "
            + ', '.join(shared_channels)
            + '; remove or replace the stack in those channels, or close the channels first'
        )

    stack['state'] = 'draft'
    stack['publication'] = {'enabled': False}
    coordination_result = None
    publication_operation = None
    require_manifest_transaction_current(manifest_path)
    if coordination_is_active(manifest):
        state_transition = {
            'stack': args.stack,
            'from_state': 'published',
            'to_state': 'draft',
        }
        publication_operation = begin_coordination_publication(
            repo_root,
            manifest,
            manifest_path,
            {},
            f'demote:{args.stack}',
            'partial',
            state_transition=state_transition,
        )
        coordination_result = coordinated_publish(
            repo_root,
            manifest,
            manifest_path,
            {},
            f'demote:{args.stack}',
            'partial',
            state_transition=state_transition,
            expected_coordination_state_tip=publication_operation[
                'expected_coordination_state_tip'
            ],
            operation_token=publication_operation['operation_token'],
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
    if publication_operation:
        complete_coordination_publication(
            repo_root, manifest_path, publication_operation, coordination_result
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

    require_manifest_transaction_current(manifest_path)
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
            run(['git', 'worktree', 'add', str(path), branch], cwd=repo_root)
        return path
    if existing:
        return existing
    path = reconcile_worktree_path(repo_root, branch, effective_worktree_root(manifest, args.worktree_root))
    worktree_root = effective_worktree_root(manifest, args.worktree_root)
    if is_external_manifest_path(repo_root, manifest_path):
        ensure_syncwheel_worktree_root_excluded(repo_root, worktree_root)
    else:
        ensure_syncwheel_metadata_excluded(repo_root, manifest.get('syncwheel_tracking'), worktree_root)
    run(['git', 'worktree', 'add', str(path), branch], cwd=repo_root)
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


def build_stack_classify_integration_plan(repo_root, manifest, manifest_path, stack_id, specs):
    """Plan manifest-only ownership of commits already present on integration."""
    stack = require_stack(manifest, stack_id)
    integration_branch = manifest['integration']['branch']
    if stack_id not in manifest['integration'].get('stacks', []):
        raise SyncwheelError(
            f'stack is not included in {integration_branch}: {stack_id}'
        )
    if not branch_exists(repo_root, integration_branch):
        raise SyncwheelError(f'integration branch does not exist: {integration_branch}')
    commits = []
    for spec in specs:
        commits.extend(commit_list_for_spec(repo_root, spec))
    commits = list(dict.fromkeys(commit_full_sha(repo_root, commit) for commit in commits))
    if not commits:
        raise SyncwheelError('provide at least one integration commit to classify')
    for commit in commits:
        if not branch_contains(repo_root, integration_branch, commit):
            raise SyncwheelError(
                f'integration-only commit is not on {integration_branch}: {commit}'
            )
    owners = {}
    for candidate in manifest['stacks']:
        for commit in stack_integration_commits(candidate):
            if commit_exists(repo_root, commit):
                owners.setdefault(commit_full_sha(repo_root, commit), candidate['id'])
    conflicts = {
        commit: owners[commit]
        for commit in commits
        if commit in owners and owners[commit] != stack_id
    }
    if conflicts:
        detail = ', '.join(f'{commit} ({owner})' for commit, owner in conflicts.items())
        raise SyncwheelError(f'integration commit already belongs to another stack: {detail}')

    proposed = copy.deepcopy(manifest)
    proposed_stack = require_stack(proposed, stack_id)
    before = stack_integration_only_commits(proposed_stack)
    proposed_stack['integration_only_commits'] = list(dict.fromkeys([*before, *commits]))
    added = [commit for commit in commits if commit not in before]
    plan = {
        'schemaVersion': 1,
        'kind': 'stackIntegrationClassificationPlan',
        'stack': stack_id,
        'integrationBranch': integration_branch,
        'commits': commits,
        'addedCommits': added,
        'manifestPath': str(manifest_path),
        'manifestDigestBefore': manifest_digest(manifest),
        'proposedManifestDigest': manifest_digest(proposed),
        'before': {'integrationOnlyCommits': before},
        'after': {
            'integrationOnlyCommits': proposed_stack['integration_only_commits'],
        },
        'actions': [{
            'type': 'update_manifest',
            'path': str(manifest_path),
            'stack': stack_id,
        }],
        'refUpdates': [],
        'worktreeUpdates': [],
        'applyRequired': True,
    }
    plan['planDigest'] = canonical_json_digest(plan)
    return plan, proposed


def command_stack_classify_integration(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    plan, proposed = build_stack_classify_integration_plan(
        repo_root, manifest, manifest_path, args.stack, args.specs
    )
    if not args.apply:
        print(json.dumps(plan, indent=2))
        return 0
    if not isinstance(args.plan_digest, str) or not args.plan_digest:
        raise SyncwheelError('--plan-digest is required with --apply')
    if args.plan_digest != plan['planDigest']:
        raise SyncwheelError(
            'integration classification plan is stale; generate a new preview and use its exact planDigest'
        )
    with manifest_write_transaction(
        repo_root, manifest_path, f'stack-classify-integration-{args.stack}'
    ):
        current = require_locked_manifest_observation(repo_root, manifest_path, plan)
        current_plan, proposed = build_stack_classify_integration_plan(
            repo_root, current, manifest_path, args.stack, args.specs
        )
        if current_plan['planDigest'] != args.plan_digest:
            raise SyncwheelError(
                'integration classification plan is stale after revalidation; generate a new preview'
            )
        save_manifest_with_ledger(
            repo_root,
            manifest_path,
            proposed,
            'stack_classify_integration',
            {
                'stack': args.stack,
                'integration_branch': current_plan['integrationBranch'],
                'added_commits': current_plan['addedCommits'],
                'plan_digest': current_plan['planDigest'],
            },
        )
    print(json.dumps({
        'status': 'applied',
        'stack': args.stack,
        'commits': current_plan['commits'],
        'manifestDigest': current_plan['proposedManifestDigest'],
        'planDigest': current_plan['planDigest'],
        'refUpdates': [],
        'worktreeUpdates': [],
    }, indent=2))
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
    capture_governed_worktrees_for_stack(
        repo_root,
        manifest,
        args.stack,
        manifest_path=manifest_path,
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
    require_nonempty_desk_stack_rebuild(stack, mode)
    if not dry_run and mode == 'in-place':
        ensure_in_place_target(repo_root, stack['branch'], manifest, stack['id'])
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
                'replay_mode': result['mode'],
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
    require_manifest_transaction_current(manifest_path)
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
    capture_governed_worktrees_for_stack(
        repo_root,
        manifest,
        args.stack,
        manifest_path=manifest_path,
    )
    print(f"{args.stack}: captured {len(added_commits)} integration commit(s)")
    return 0


def command_stack_rebuild(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    stack = require_stack(manifest, args.stack)
    mode, worktree = select_replay_mode(
        repo_root,
        manifest,
        args,
        stack['branch'],
        resolve_stack_rebuild_location(repo_root, manifest, stack, args),
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
        changed_refs = {
            f"refs/heads/{stack['branch']}": ref_tip(repo_root, stack['branch'])
        }
        try:
            publication_operation = (
                None if args.dry_run else begin_coordination_publication(
                    repo_root,
                    manifest,
                    manifest_path,
                    changed_refs,
                    f"stack:{stack['id']}",
                    'partial',
                )
            )
            result = coordinated_publish(
                repo_root,
                manifest,
                manifest_path,
                changed_refs,
                f"stack:{stack['id']}",
                'partial',
                dry_run=args.dry_run,
                remedy_stack=stack['id'],
                expected_coordination_state_tip=(
                    publication_operation['expected_coordination_state_tip']
                    if publication_operation else EXPECTED_COORDINATION_STATE_UNSET
                ),
                operation_token=(
                    publication_operation['operation_token']
                    if publication_operation else None
                ),
            )
        except SyncwheelError as exc:
            if coordination_remote_is_reachable(repo_root, config['remote']):
                raise
            raise coordinated_publish_remote_failure(
                f"stack push {stack['id']}"
            ) from exc
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
                    'operation_token': publication_operation['operation_token'],
                    'recovered': bool(result.get('recovered')),
                },
                manifest_path,
            )
            complete_coordination_publication(
                repo_root, manifest_path, publication_operation, result
            )
        return 0
    remote = stack_push_remote(manifest, stack, args.remote)
    push_args = push_args_with_options(args)
    command = ['git', 'push', *push_args, remote, stack['branch']]
    if args.dry_run:
        print(quoted(command))
        return 0
    run_authorized_push(repo_root, command, remote, [f"refs/heads/{stack['branch']}"])
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
    worktree = resolve_git_worktree(
        repo_root, stack['branch'], manifest, args.worktree, args.auto_worktree
    )
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


def trees_differ_only_by_manifest(repo_root, left_tree, right_tree):
    changed = git(
        repo_root,
        'diff-tree',
        '--no-commit-id',
        '--name-only',
        '-r',
        '-z',
        left_tree,
        right_tree,
    ).stdout.split('\0')
    changed = [path for path in changed if path]
    return bool(changed) and set(changed) == {'.syncwheel/manifest.json'}


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
        'local_control_only_ahead': False,
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

    if report['relation'] == 'local_ahead':
        ahead_commits = rev_list(repo_root, f'{remote_ref}..{branch}')
        report['local_control_only_ahead'] = bool(ahead_commits) and all(
            is_manifest_only_commit(repo_root, commit) for commit in ahead_commits
        )

    try:
        projected_tree = materialize_integration_projection(repo_root, manifest, stack_ref_overrides)
        report['projected_tree'] = projected_tree
        if report['remote_tree']:
            report['remote_matches_projection'] = (
                report['remote_tree'] == projected_tree
                or trees_differ_only_by_manifest(repo_root, report['remote_tree'], projected_tree)
            )
        if report['local_tree']:
            report['local_matches_projection'] = (
                report['local_tree'] == projected_tree
                or trees_differ_only_by_manifest(repo_root, report['local_tree'], projected_tree)
            )
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
        'absorbed': False,
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
        absorbed = bool(stack['commits']) and all(
            branch_contains(repo_root, stack['base'], commit)
            for commit in stack['commits']
        )
        report['absorbed'] = absorbed
        if report['local_tree']:
            report['local_matches_projection'] = (
                report['local_tree'] == projected_tree
                or (
                    absorbed
                    and all(branch_contains(repo_root, branch, commit) for commit in stack['commits'])
                )
            )
        if report['remote_tree']:
            report['remote_matches_projection'] = (
                report['remote_tree'] == projected_tree
                or (
                    absorbed
                    and all(branch_contains(repo_root, remote_ref, commit) for commit in stack['commits'])
                )
            )
    except SyncwheelError as exc:
        report['projection_error'] = str(exc)
    return report


def reconcile_worktree_path(repo_root, branch, worktree_root):
    existing = find_worktree_for_branch(repo_root, branch)
    if existing:
        existing_path = Path(existing).resolve()
        if existing_path != Path(repo_root).resolve():
            return existing_path
    return configured_worktree_path(repo_root, branch, worktree_root)


def preflight_reconcile_mutation_targets(repo_root, manifest, actions, worktree_root):
    """Reject dirty local targets before reconciliation changes any managed state."""
    stack_actions = {'rebuild_stack', 'align_stack_to_remote'}
    integration_actions = {'rebuild_integration', 'align_integration_to_remote'}
    checked = set()

    for action in actions:
        if action['type'] in stack_actions:
            stack = require_stack(manifest, action['stack'])
            worktree = reconcile_worktree_path(repo_root, stack['branch'], worktree_root)
            path = Path(worktree).resolve()
            key = (stack['branch'], path)
            if key not in checked:
                ensure_non_in_place_target_clean(repo_root, stack['branch'], path)
                checked.add(key)
        elif action['type'] in integration_actions:
            integration = manifest['integration']
            branch = integration['branch']
            if get_current_branch(repo_root) == branch:
                path = Path(repo_root).resolve()
                key = (branch, path)
                if key not in checked:
                    ensure_clean_worktree(
                        path,
                        allowed_status_prefixes=['?? .syncwheel/'],
                        remedy_commands=primary_checkout_remedy_commands(manifest),
                    )
                    checked.add(key)
            else:
                worktree = reconcile_worktree_path(repo_root, branch, worktree_root)
                path = Path(worktree).resolve()
                key = (branch, path)
                if key not in checked:
                    ensure_non_in_place_target_clean(repo_root, branch, path)
                    checked.add(key)


def preflight_empty_desk_stack_rebuilds(repo_root, manifest, actions, args, worktree_root):
    """Reject empty desk rebuilds before reconcile touches managed metadata."""
    for action in actions:
        if action['type'] != 'rebuild_stack':
            continue
        stack = require_stack(manifest, action['stack'])
        worktree = reconcile_worktree_path(repo_root, stack['branch'], worktree_root)
        mode, _worktree = select_replay_mode(
            repo_root, manifest, args, stack['branch'], (worktree, False)
        )
        require_nonempty_desk_stack_rebuild(stack, mode)


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
        and not integration_report.get('local_control_only_ahead')
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
        or integration_report.get('local_control_only_ahead') is True
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
    elif action['type'] == 'resume_drop_absorbed_commit':
        line += ' detail=drop a patch-equivalent commit already absorbed by a historical merged stack'
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
    pending_merge_manifest = None
    pending_merge_preview = None
    if getattr(args, 'accept_merge', False):
        pending_merge_manifest = manifest
        pending_merge_preview = apply_pending_coordination_merge(
            repo_root, manifest, manifest_path, persist=False
        )
        manifest = pending_merge_preview
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
    if getattr(args, 'apply', False) and coordination_is_active(manifest):
        own_scope = (
            'full'
            if not args.stack and not getattr(args, 'skip_integration', False)
            else 'partial'
        )
        resolve_pending_coordination_publications(
            repo_root,
            manifest,
            manifest_path,
            adopt_tokens={
                intent.get('operation_token')
                for intent in pending_coordination_publications(repo_root, manifest_path)
                if intent.get('scope') == own_scope
            },
        )
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

    preflight_empty_desk_stack_rebuilds(repo_root, manifest, actions, args, worktree_root)
    preflight_reconcile_mutation_targets(repo_root, manifest, actions, worktree_root)
    require_manifest_transaction_current(manifest_path)
    if is_external_manifest_path(repo_root, manifest_path):
        ensure_syncwheel_worktree_root_excluded(repo_root, worktree_root)
    else:
        ensure_syncwheel_metadata_excluded(repo_root, manifest.get('syncwheel_tracking'), worktree_root)

    push_args = push_args_with_options(args)
    coordinated_push = args.push and coordination_is_active(manifest)
    if coordinated_push:
        coordinated_push_remote(args, coordination_config(manifest))
    if pending_merge_manifest is not None:
        manifest = apply_pending_coordination_merge(
            repo_root,
            pending_merge_manifest,
            manifest_path,
            persist=True,
            expected_digest=manifest_digest(pending_merge_preview),
        )
    coordinated_refs = {}
    coordinated_events = []
    publication_operation = None
    coordination_result = None
    deferred_manifest_updates = []
    for action in actions:
        if action['type'] == 'rebuild_stack':
            stack = require_stack(manifest, action['stack'])
            worktree = reconcile_worktree_path(repo_root, stack['branch'], worktree_root)
            ensure_non_in_place_target_clean(repo_root, stack['branch'], worktree)
            mode, worktree = select_replay_mode(
                repo_root, manifest, args, stack['branch'], (worktree, False)
            )
            require_nonempty_desk_stack_rebuild(stack, mode)
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
                    'replay_mode': result['mode'],
                },
                manifest_path,
            )
            if args.update_manifest:
                stack['commits'] = rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
                deferred_manifest_updates.append({
                    'stack': stack['id'],
                    'branch': stack['branch'],
                })
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
            run_authorized_push(
                repo_root, command, remote, [f"refs/heads/{stack['branch']}"]
            )
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
                        + format_remedy_suffix(primary_checkout_remedy_commands(manifest))
                    )
                ensure_clean_worktree(
                    repo_root,
                    allowed_status_prefixes=['?? .syncwheel/'],
                    remedy_commands=primary_checkout_remedy_commands(manifest),
                )
                worktree = None
                in_place = True
            else:
                worktree = reconcile_worktree_path(repo_root, integration['branch'], worktree_root)
                ensure_non_in_place_target_clean(repo_root, integration['branch'], worktree)
                in_place = False
            mode, worktree = select_replay_mode(
                repo_root,
                manifest,
                args,
                integration['branch'],
                (worktree, in_place),
                plumbing_supported=integration_supports_plumbing(manifest),
            )
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
            if in_place:
                acknowledge_in_place_manifest_replay(
                    repo_root, manifest_path, result['after_tip']
                )
            append_ledger_event(
                repo_root,
                'integration_rebuilt',
                {
                    'branch': integration['branch'],
                    'before_tip': result['before_tip'],
                    'after_tip': result['after_tip'],
                    'stacks': list(integration.get('stacks', [])),
                    'replay_mode': result['mode'],
                },
                manifest_path,
            )
        elif action['type'] == 'align_integration_to_remote':
            integration = manifest['integration']
            before_tip = ref_tip(repo_root, integration['branch'])
            use_primary_checkout = get_current_branch(repo_root) == integration['branch']
            if use_primary_checkout:
                ensure_clean_worktree(
                    repo_root,
                    allowed_status_prefixes=['?? .syncwheel/'],
                    remedy_commands=primary_checkout_remedy_commands(manifest),
                )
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
            if use_primary_checkout:
                acknowledge_in_place_manifest_replay(
                    repo_root, manifest_path, action['remote_ref']
                )
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
            run_authorized_push(
                repo_root, command, remote,
                [f"refs/heads/{manifest['integration']['branch']}"],
            )
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
        if full_scope and not local_manifest_projection_is_convergent(
            repo_root, manifest, manifest_path
        ):
            raise SyncwheelError(
                'full coordinated publish requires every managed local ref to match the manifest projection'
            )
        require_manifest_transaction_current(manifest_path)
        publication_scope = 'full' if full_scope else 'partial'
        projection_status = 'convergent' if full_scope else 'partial'
        adoptable = {
            intent.get('operation_token')
            for intent in pending_coordination_publications(repo_root, manifest_path)
            if intent.get('scope') == publication_scope
            and intent.get('fingerprint') == coordination_publication_identity(
                repo_root,
                manifest,
                intent.get('changed_refs') or {},
                publication_scope,
                projection_status,
            )[1]
        }
        resolve_pending_coordination_publications(
            repo_root, manifest, manifest_path, adopt_tokens=adoptable
        )
        publication_operation = pending_coordination_publication_for_scope(
            repo_root, manifest_path, publication_scope
        )
        if publication_operation:
            if publication_operation.get('operation_token') not in adoptable:
                raise SyncwheelError(
                    'reconcile retry no longer matches its pending coordinated publication intent'
                )
            pending_refs = publication_operation.get('changed_refs') or {}
            if coordinated_refs and coordinated_refs != pending_refs:
                raise SyncwheelError(
                    'reconcile retry no longer matches its pending coordinated publication intent'
                )
            coordinated_refs = pending_refs
            if not coordinated_events:
                by_ref = {
                    f"refs/heads/{stack['branch']}": stack
                    for stack in manifest['stacks']
                }
                integration_ref = f"refs/heads/{manifest['integration']['branch']}"
                for ref, tip in coordinated_refs.items():
                    if ref in by_ref:
                        stack = by_ref[ref]
                        coordinated_events.append({
                            'type': 'stack_pushed',
                            'stack': stack['id'],
                            'branch': stack['branch'],
                            'tip': tip,
                        })
                    elif ref == integration_ref:
                        coordinated_events.append({
                            'type': 'integration_pushed',
                            'branch': manifest['integration']['branch'],
                            'tip': tip,
                        })
                    else:
                        raise SyncwheelError(
                            f'reconcile pending publication contains unknown ref: {ref}'
                        )
        else:
            publication_operation = begin_coordination_publication(
                repo_root,
                manifest,
                manifest_path,
                coordinated_refs,
                publication_scope,
                projection_status,
            )
        coordination_result = coordinated_publish(
            repo_root,
            manifest,
            manifest_path,
            coordinated_refs,
            publication_scope,
            projection_status,
            expected_coordination_state_tip=publication_operation[
                'expected_coordination_state_tip'
            ],
            operation_token=publication_operation['operation_token'],
        )
        config = coordination_config(manifest)
        for event in coordinated_events:
            payload = {
                **event,
                'remote': config['remote'],
                'coordination_state': coordination_result.get('state_tip'),
                'coordination_status': coordination_result['status'],
                'operation_token': publication_operation['operation_token'],
                'recovered': bool(coordination_result.get('recovered')),
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
                    'operation_token': publication_operation['operation_token'],
                    'recovered': bool(coordination_result.get('recovered')),
                },
                manifest_path,
            )
    if args.apply and (resume_manifest_changed or deferred_manifest_updates):
        reason = 'resume_manifest_update' if resume_manifest_changed else 'reconcile_update_manifest'
        context = {'stacks': deferred_manifest_updates} if deferred_manifest_updates else None
        save_manifest_with_ledger(repo_root, manifest_path, manifest, reason, context)
        for update in deferred_manifest_updates:
            print(f"{update['stack']}: manifest updated from rebuilt branch")
    if args.apply and getattr(args, 'auto_gc', False) and coordination_is_active(manifest):
        gc_plan = run_coordination_gc(repo_root, manifest, apply=True, fetch=True)
        if gc_plan.get('applied_candidates'):
            print(f"automatic gc: processed {len(gc_plan['applied_candidates'])} eligible local artifact(s)")
    if publication_operation:
        complete_coordination_publication(
            repo_root, manifest_path, publication_operation, coordination_result
        )
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
    ensure_in_place_target(repo_root, integration['branch'], manifest)
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
    validation = validate_manifest(repo_root, manifest)
    narrowed = validation['details']['integration'].get(
        'derived_paths_narrowed'
    ) or []
    reason = getattr(args, 'reason', None)
    if narrowed and (
        not isinstance(reason, str) or not reason.strip()
    ):
        raise SyncwheelError(
            'derived-paths-narrowed reconciliation requires --reason; use: '
            + derived_paths_rebuild_remedy()
        )
    if isinstance(reason, str):
        reason = reason.strip()
    mode, worktree = select_replay_mode(
        repo_root,
        manifest,
        args,
        integration['branch'],
        resolve_int_rebuild_location(repo_root, manifest, args),
        plumbing_supported=integration_supports_plumbing(manifest),
    )
    if not args.dry_run and mode == 'in-place':
        ensure_in_place_target(repo_root, manifest['integration']['branch'], manifest)
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
        reconciled = []
        unique_narrowed = {
            (item['operation_id'], item['commit'], tuple(item['paths'])): item
            for item in narrowed
        }
        for item in unique_narrowed.values():
            if resolve_common_derived_provenance(
                repo_root,
                manifest,
                item['paths'],
                expected_commit=item['commit'],
            ):
                reconciled.append({
                    'operation_id': item['operation_id'],
                    'commit': item['commit'],
                    'paths': list(item['paths']),
                })
        append_ledger_event(
            repo_root,
            'integration_rebuilt',
            {
                'branch': manifest['integration']['branch'],
                'before_tip': result['before_tip'],
                'after_tip': result['after_tip'],
                'stacks': list(manifest['integration'].get('stacks', [])),
                'replay_mode': result['mode'],
                'reason': reason,
                'derived_provenance_reconciled': reconciled,
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
        changed_refs = {
            f"refs/heads/{integration['branch']}": ref_tip(
                repo_root, integration['branch']
            )
        }
        try:
            publication_operation = (
                None if args.dry_run else begin_coordination_publication(
                    repo_root,
                    manifest,
                    manifest_path,
                    changed_refs,
                    'integration',
                    'partial',
                )
            )
            result = coordinated_publish(
                repo_root,
                manifest,
                manifest_path,
                changed_refs,
                'integration',
                'partial',
                dry_run=args.dry_run,
                expected_coordination_state_tip=(
                    publication_operation['expected_coordination_state_tip']
                    if publication_operation else EXPECTED_COORDINATION_STATE_UNSET
                ),
                operation_token=(
                    publication_operation['operation_token']
                    if publication_operation else None
                ),
            )
        except SyncwheelError as exc:
            if coordination_remote_is_reachable(repo_root, config['remote']):
                raise
            raise coordinated_publish_remote_failure('int push') from exc
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
                    'operation_token': publication_operation['operation_token'],
                    'recovered': bool(result.get('recovered')),
                },
                manifest_path,
            )
            complete_coordination_publication(
                repo_root, manifest_path, publication_operation, result
            )
        return 0
    remote = args.remote or manifest['defaults']['publication_remote']
    push_args = push_args_with_options(args)
    command = ['git', 'push', *push_args, remote, integration['branch']]
    if args.dry_run:
        print(quoted(command))
        return 0
    run_authorized_push(
        repo_root, command, remote, [f"refs/heads/{integration['branch']}"]
    )
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
    worktree = resolve_git_worktree(repo_root, branch, manifest, args.worktree, args.auto_worktree)
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
        'authority': manifest_authority(manifest) if manifest_present else None,
        'authority_present': bool(manifest_present and 'authority' in manifest),
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
    print(f"authority: {format_authority_policy(report['authority'])}")
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


def format_authority_policy(policy):
    if not policy:
        return 'missing'
    allow = ','.join(policy['allow']) or '-'
    deny = ','.join(policy['deny'])
    return f"{policy['mode']} allow={allow} deny={deny}"


def authority_report(repo_root, manifest_path):
    manifest, manifest_path = load_manifest(repo_root, manifest_path)
    present = bool(manifest and 'authority' in manifest)
    policy = manifest_authority(manifest) if manifest else None
    warnings = []
    if manifest and not present:
        warnings.append(
            'authority is not declared; agents must treat this repository as '
            f'{AUTHORITY_MODE_HUMAN_GATED} until a maintainer sets it'
        )
    return {
        'repo_root': str(repo_root),
        'manifest_path': str(manifest_path),
        'manifest_present': manifest is not None,
        'authority': policy,
        'authority_present': present,
        'warnings': warnings,
    }


def print_authority_report(report):
    print(f"repo: {report['repo_root']}")
    print(f"manifest: {report['manifest_path'] if report['manifest_present'] else 'missing'}")
    print(f"authority: {format_authority_policy(report['authority'])}")
    print(f"authority_declared: {'yes' if report['authority_present'] else 'no'}")
    for warning in report['warnings']:
        print(f'warning: {warning}')


def command_repo_authority_status(args):
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    report = authority_report(repo_root, manifest_path)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print_authority_report(report)
    return 0


def command_repo_authority_set(args):
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    manifest, manifest_path = load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    ensure_manifest_in_repo(repo_root, manifest_path)
    current = manifest_authority(manifest)
    proposed = normalize_authority_policy(
        {'mode': args.mode, 'allow': list(args.allow or []), 'deny': []}, 'requested'
    )
    if not args.apply:
        print(f"current_authority: {format_authority_policy(current)}")
        print(f"proposed_authority: {format_authority_policy(proposed)}")
        print('dry_run: pass --apply to write this policy')
        return 0
    manifest['authority'] = proposed
    save_manifest_with_ledger(
        repo_root, manifest_path, manifest, 'repo_authority_set', {'authority': proposed}
    )
    if manifest.get('syncwheel_tracking') == SYNCWHEEL_TRACKING_GIT_TRACKED:
        git_add_paths(repo_root, [manifest_path], force_paths=[manifest_path])
    report = authority_report(repo_root, manifest_path)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_authority_report(report)
    return 0


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
        ensure_managed_repository_hooks(repo_root, manifest)
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


class SyncwheelRevisionBackend:
    """In-process facade for the Agentwheel revision-provider protocol."""

    MANIFEST_PRODUCT_PATH = '.syncwheel/manifest.json'

    def __init__(self, provider_module):
        self.provider = provider_module

    def _fail(self, message):
        raise self.provider.RevisionProviderError(message)

    def _repo_root(self, request):
        supplied = Path(request.repository_root)
        resolved = supplied.resolve(strict=False)
        if str(resolved) != request.repository_root:
            self._fail('repositoryRoot must be a canonical absolute path')
        result = git(resolved, 'rev-parse', '--show-toplevel', check=False)
        if result.returncode != 0:
            self._fail(f'repositoryRoot is not a Git worktree: {resolved}')
        top = Path(result.stdout.strip()).resolve()
        if top != resolved:
            self._fail(f'repositoryRoot must name the worktree root: {top}')
        return resolved

    def _manifest(self, repo_root):
        try:
            return require_manifest(repo_root, str(repo_root), None, None)
        except SyncwheelError as exc:
            self._fail(str(exc))

    def _read_product_path(self, repo_root, relative):
        """Read a regular file through descriptor-bound, no-follow traversal."""
        directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        nofollow = getattr(os, 'O_NOFOLLOW', 0)
        descriptors = []
        try:
            current = os.open(str(repo_root), directory_flags | nofollow)
            descriptors.append(current)
            parts = relative.split('/')
            for part in parts[:-1]:
                try:
                    current = os.open(
                        part, directory_flags | nofollow, dir_fd=current
                    )
                except FileNotFoundError:
                    return None
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        self._fail(
                            f'product path parent must not be a symbolic link: {relative}'
                        )
                    raise
                descriptors.append(current)
            try:
                descriptor = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=current)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    self._fail(f'product path must not be a symbolic link: {relative}')
                raise
            descriptors.append(descriptor)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                self._fail(f'product path must be a regular file or absent: {relative}')
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            fingerprint_before = (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                before.st_ctime_ns, stat.S_IMODE(before.st_mode),
            )
            fingerprint_after = (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns, stat.S_IMODE(after.st_mode),
            )
            if fingerprint_before != fingerprint_after:
                self._fail(f'product path changed while it was being read: {relative}')
            payload = b''.join(chunks)
            return {
                'sha256': hashlib.sha256(payload).hexdigest(),
                'bytes': payload,
                'mode': '100755' if after.st_mode & 0o111 else '100644',
                'fingerprint': list(fingerprint_after),
            }
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _file_sha256(self, repo_root, relative):
        observed = self._read_product_path(repo_root, relative)
        return observed['sha256'] if observed is not None else None

    def _hash_blob_bytes(self, repo_root, payload):
        result = subprocess.run(
            ['git', 'hash-object', '-w', '--stdin'],
            cwd=repo_root,
            input=payload,
            capture_output=True,
        )
        if result.returncode != 0:
            self._fail(
                'could not persist provider blob object: '
                + result.stderr.decode('utf-8', errors='replace').strip()
            )
        return result.stdout.decode().strip()

    def _capture_path_objects(self, repo_root, request, paths=None):
        expected = {
            item.path: item.after_sha256 for item in request.paths
        }
        selected = paths or [item.path for item in request.paths]
        objects = {}
        for relative in selected:
            observed = self._read_product_path(repo_root, relative)
            declared = expected.get(relative)
            actual = observed['sha256'] if observed is not None else None
            if relative in expected and actual != declared:
                self._fail(
                    f'after SHA-256 mismatch for {relative}: '
                    f'expected {declared!r}, found {actual!r}'
                )
            if observed is None:
                objects[relative] = {
                    'sha256': None, 'blob': None, 'mode': None,
                }
            else:
                objects[relative] = {
                    'sha256': observed['sha256'],
                    'blob': self._hash_blob_bytes(repo_root, observed['bytes']),
                    'mode': observed['mode'],
                }
        return objects

    def _validate_hashes(self, repo_root, request, stage):
        attribute = 'before_sha256' if stage == 'before' else 'after_sha256'
        for item in request.paths:
            expected = getattr(item, attribute)
            actual = self._file_sha256(repo_root, item.path)
            if actual != expected:
                self._fail(
                    f'{stage} SHA-256 mismatch for {item.path}: '
                    f'expected {expected!r}, found {actual!r}'
                )

    def _head_file_sha256(self, repo_root, head, relative):
        listing = git(repo_root, 'ls-tree', '-z', head, '--', relative, check=False)
        if listing.returncode != 0:
            self._fail(f'could not inspect {relative} at expectedHead {head}')
        entries = [entry for entry in listing.stdout.split('\0') if entry]
        if not entries:
            return None
        if len(entries) != 1:
            self._fail(f'ambiguous tree entry for product path: {relative}')
        entry = entries[0]
        metadata, separator, listed_path = entry.partition('\t')
        if not separator or listed_path != relative:
            self._fail(f'ambiguous tree entry for product path: {relative}')
        mode, object_type, object_id = metadata.split(' ', 2)
        if object_type != 'blob' or mode not in {'100644', '100755'}:
            self._fail(f'product path is not a regular file at expectedHead: {relative}')
        result = subprocess.run(
            ['git', 'cat-file', 'blob', object_id],
            cwd=repo_root,
            capture_output=True,
        )
        if result.returncode != 0:
            self._fail(f'could not read {relative} at expectedHead {head}')
        return hashlib.sha256(result.stdout).hexdigest()

    def _validate_before_hashes_at_head(self, repo_root, request):
        for item in request.paths:
            actual = self._head_file_sha256(repo_root, request.expected_head, item.path)
            if actual != item.before_sha256:
                self._fail(
                    f'before SHA-256 mismatch for {item.path} at expectedHead: '
                    f'expected {item.before_sha256!r}, found {actual!r}'
                )

    def _index_conflicts(self, repo_root):
        return bool(git(repo_root, 'ls-files', '-u').stdout.strip())

    def _index_is_clean(self, repo_root):
        return git(repo_root, 'diff', '--cached', '--quiet', check=False).returncode == 0

    def _dirty_paths(self, repo_root):
        paths = set()
        for arguments in (
            ('diff', '--name-only', '-z'),
            ('ls-files', '--others', '--exclude-standard', '-z'),
        ):
            output = git(repo_root, *arguments).stdout
            paths.update(item for item in output.split('\0') if item)
        return paths

    def _ensure_clean(self, repo_root):
        if self._index_conflicts(repo_root):
            self._fail('revision provider requires a conflict-free index')
        status = git(
            repo_root,
            'status',
            '--porcelain',
            '--untracked-files=all',
            env={'GIT_OPTIONAL_LOCKS': '0'},
        ).stdout
        if status.strip():
            self._fail('revision provider preflight requires a completely clean worktree and index')

    def _ensure_after_scope(self, repo_root, request, *, allowed_outside=()):
        if self._index_conflicts(repo_root):
            self._fail('revision provider refuses an index with conflicts')
        if not self._index_is_clean(repo_root):
            self._fail('revision provider refuses pre-staged changes')
        self._validate_hashes(repo_root, request, 'after')
        allowed = {item.path for item in request.paths}
        allowed.update(allowed_outside)
        outside = sorted(self._dirty_paths(repo_root) - allowed)
        if outside:
            self._fail('mutation changed paths outside the declared allowlist: ' + ', '.join(outside))
        for item in request.paths:
            tracked = git(
                repo_root, 'ls-files', '--error-unmatch', '--', item.path, check=False
            ).returncode == 0
            ignored = git(repo_root, 'check-ignore', '-q', '--', item.path, check=False).returncode == 0
            if not tracked and ignored and item.after_sha256 is not None:
                self._fail(f'revision provider refuses an ignored product path: {item.path}')

    def _worktrees(self, repo_root):
        return sorted(str(Path(item['path']).resolve()) for item in get_worktrees(repo_root))

    def _worktree_porcelain(self, repo_root):
        return git(repo_root, 'worktree', 'list', '--porcelain').stdout

    def _direct_ref_observation(self, name, object_oid):
        return {
            'name': name,
            'kind': 'direct',
            'objectOid': object_oid,
            'symbolicTarget': None,
        }

    def _symbolic_ref_result(self, repo_root, name):
        return git(
            repo_root,
            'symbolic-ref',
            '--quiet',
            '--no-recurse',
            name,
            check=False,
        )

    def _observe_ref(self, repo_root, name, *, allow_missing=True):
        if run(['git', 'check-ref-format', name], check=False).returncode != 0:
            self._fail(f'cannot inspect invalid full ref name: {name!r}')
        symbolic = self._symbolic_ref_result(repo_root, name)
        if symbolic.returncode == 0:
            kind = 'symbolic'
            symbolic_target = symbolic.stdout.strip()
            if (
                not symbolic_target
                or run(
                    ['git', 'check-ref-format', symbolic_target], check=False
                ).returncode != 0
            ):
                self._fail(
                    f'symbolic ref {name!r} has an invalid immediate target: '
                    f'{symbolic_target!r}'
                )
        elif symbolic.returncode == 1:
            kind = 'direct'
            symbolic_target = None
        else:
            detail = symbolic.stderr.strip() or symbolic.stdout.strip()
            suffix = f': {detail}' if detail else ''
            self._fail(
                f'could not inspect ref kind for {name!r} '
                f'(symbolic-ref exit {symbolic.returncode}){suffix}'
            )

        resolved = git(
            repo_root,
            'show-ref',
            '--verify',
            '--hash',
            '--',
            name,
            check=False,
        )
        object_oid = resolved.stdout.strip() if resolved.returncode == 0 else ''
        if not re.fullmatch(r'[0-9a-f]{40}', object_oid):
            if kind == 'direct' and allow_missing:
                return None
            detail = resolved.stderr.strip() or resolved.stdout.strip()
            suffix = f': {detail}' if detail else ''
            self._fail(
                f'could not resolve {kind} ref {name!r} to a full object OID'
                f'{suffix}'
            )
        return {
            'name': name,
            'kind': kind,
            'objectOid': object_oid,
            'symbolicTarget': symbolic_target,
        }

    def _refs_snapshot(self, repo_root, *prefixes):
        result = git(
            repo_root,
            'for-each-ref',
            '--format=%(refname)',
            *prefixes,
        )
        names = sorted(line for line in result.stdout.splitlines() if line)
        if len(names) != len(set(names)):
            self._fail('Git returned duplicate ref names while taking a snapshot')
        return {
            name: self._observe_ref(repo_root, name, allow_missing=False)
            for name in names
        }

    def _remote_refs(self, repo_root):
        return self._refs_snapshot(repo_root, 'refs/remotes/')

    def _managed_local_refs(self, repo_root, manifest):
        return {
            ref: self._observe_ref(repo_root, ref)
            for ref in sorted(managed_ref_names(manifest))
        }

    def _all_refs(self, repo_root):
        return self._refs_snapshot(repo_root, 'refs/')

    def _index_path(self, repo_root):
        raw = git(repo_root, 'rev-parse', '--git-path', 'index').stdout.strip()
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        return Path(os.path.abspath(path))

    def _read_regular_file(self, path, label):
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            self._fail(f'{label} does not exist: {path}')
        except OSError as exc:
            self._fail(f'could not open {label} without following links: {path}: {exc}')
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                self._fail(f'{label} is not a regular file: {path}')
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b''.join(chunks), metadata
        finally:
            os.close(descriptor)

    def _index_sha256(self, repo_root):
        path = self._index_path(repo_root)
        payload, _ = self._read_regular_file(path, 'Git index')
        return hashlib.sha256(payload).hexdigest()

    def _resolve_base_ref(self, repo_root, manifest):
        base_ref = manifest['defaults']['base_ref']
        if re.fullmatch(r'[0-9a-f]{40}', base_ref):
            try:
                base_sha = commit_full_sha(repo_root, base_ref)
            except SyncwheelError as exc:
                self._fail(
                    f'defaults.base_ref full SHA does not name a commit: '
                    f'{base_ref!r}: {exc}'
                )
            if base_sha != base_ref:
                self._fail(
                    'defaults.base_ref full SHA must identify a commit directly, '
                    'not an annotated tag or another peeled object'
                )
            return base_ref, None, base_sha, None, None

        if re.fullmatch(r'[0-9A-Fa-f]{4,40}', base_ref):
            self._fail(
                'defaults.base_ref must not be an abbreviated commit SHA or a '
                'non-lowercase full SHA; '
                'use the exact lowercase 40-hex commit SHA'
            )

        if base_ref.startswith('refs/'):
            candidates = [base_ref]
        elif base_ref.startswith(('heads/', 'remotes/', 'tags/')):
            candidates = [f'refs/{base_ref}']
        else:
            candidates = [
                f'refs/heads/{base_ref}',
                f'refs/remotes/{base_ref}',
                f'refs/tags/{base_ref}',
            ]

        resolved = []
        for candidate in candidates:
            if run(['git', 'check-ref-format', candidate], check=False).returncode != 0:
                continue
            observation = self._observe_ref(repo_root, candidate)
            if observation is None:
                continue
            if observation['kind'] == 'symbolic':
                self._fail(
                    f'defaults.base_ref {base_ref!r} resolves to symbolic ref '
                    f'{candidate!r} -> {observation["symbolicTarget"]!r}; '
                    'revision-provider v1 requires a direct object ref'
                )
            resolved.append((candidate, observation))

        if not resolved:
            self._fail(
                f'defaults.base_ref {base_ref!r} must be a direct resolvable Git ref '
                'or an exact lowercase 40-hex commit SHA; revision expressions are '
                'not accepted'
            )
        if len(resolved) != 1:
            self._fail(
                f'defaults.base_ref {base_ref!r} is ambiguous between direct refs: '
                + ', '.join(candidate for candidate, _ in resolved)
                + '; use one full refs/... name'
            )
        full_name, base_ref_observation = resolved[0]
        base_ref_object_sha = base_ref_observation['objectOid']
        try:
            base_sha = commit_full_sha(repo_root, full_name)
        except SyncwheelError as exc:
            self._fail(f'could not resolve defaults.base_ref {base_ref!r}: {exc}')
        return (
            base_ref,
            full_name,
            base_sha,
            base_ref_object_sha,
            base_ref_observation,
        )

    def _assert_base_ref_is_not_managed(self, manifest, base_ref, full_name):
        if full_name is None:
            return
        managed = set(managed_ref_names(manifest))
        if full_name in managed:
            self._fail(
                f'defaults.base_ref {base_ref!r} resolves to managed ref '
                f'{full_name!r}; a revision-provider base must not alias an '
                'integration, stack, or channel branch'
            )

    def _fresh_coordination_handoff(self, repo_root, manifest):
        if not coordination_is_active(manifest):
            return {'mode': 'disabled', 'stateTip': None, 'manifestDigest': None}
        config = coordination_config(manifest)
        try:
            _, local_coordination = coordination_profile(repo_root)
            pending = local_coordination.get('pending_merge')
            if isinstance(pending, dict) and pending.get('coordination_id') == config['id']:
                self._fail(
                    'active-active handoff has a pending coordination merge; resolve it first'
                )
            if local_coordination.get('locks'):
                self._fail('active-active handoff has local stack/worktree locks')
            if local_lease_is_active(local_coordination):
                self._fail('active-active handoff has an active local publication lease')
            remote = read_remote_coordination_state(
                repo_root,
                config,
                fetch=True,
                local_manifest_version=manifest['version'],
            )
            state = remote.get('state')
            if not remote.get('tip') or not state:
                self._fail(
                    'active-active handoff requires an initialized published coordination state'
                )
            current_refs = managed_ref_names(manifest)
            require_exclusive_coordination_ownership(repo_root, config, current_refs)
            local_digest = coordination_manifest_digest(manifest, repo_root)
            if state.get('manifest_digest') != local_digest:
                self._fail(
                    'active-active handoff manifest is not aligned with fresh coordination state'
                )
            if not coordination_state_matches_remote(repo_root, config, state):
                self._fail(
                    'active-active handoff coordination state does not match fresh remote refs'
                )
            drifted_local = []
            integration_ref = f"refs/heads/{manifest['integration']['branch']}"
            for ref in current_refs:
                expected_tip = state.get('managed_refs', {}).get(ref)
                local_tip = ref_tip(repo_root, ref)
                control_only_ahead = False
                if ref == integration_ref and expected_tip and local_tip:
                    ancestor = git(
                        repo_root,
                        'merge-base',
                        '--is-ancestor',
                        expected_tip,
                        local_tip,
                        check=False,
                    )
                    if ancestor.returncode == 0:
                        ahead = rev_list(repo_root, f'{expected_tip}..{local_tip}')
                        control_only_ahead = bool(ahead) and all(
                            is_manifest_only_commit(repo_root, commit) for commit in ahead
                        )
                if not expected_tip or (local_tip != expected_tip and not control_only_ahead):
                    drifted_local.append(
                        f'{ref} (local {local_tip or "missing"}, state {expected_tip or "missing"})'
                    )
            if drifted_local:
                self._fail(
                    'active-active handoff local managed refs are not aligned: '
                    + '; '.join(drifted_local)
                )
            return {
                'mode': 'active-active',
                'stateTip': remote['tip'],
                'manifestDigest': state['manifest_digest'],
            }
        except SyncwheelError as exc:
            self._fail(f'active-active handoff gate failed: {exc}')

    def _validate_repository(self, repo_root, request, *, require_clean):
        manifest, manifest_path = self._manifest(repo_root)
        if manifest.get('repository_mode') != 'delivery':
            self._fail('revision provider requires repository_mode="delivery"')
        if manifest.get('syncwheel_tracking') != SYNCWHEEL_TRACKING_GIT_TRACKED:
            self._fail('revision provider requires syncwheel_tracking="git-tracked"')
        tracked_manifest = git(
            repo_root, 'ls-files', '--error-unmatch', '--', self.MANIFEST_PRODUCT_PATH,
            check=False,
        )
        if tracked_manifest.returncode != 0:
            self._fail('revision provider requires a tracked .syncwheel/manifest.json')
        branch = get_current_branch(repo_root)
        integration_branch = manifest['integration']['branch']
        if branch != integration_branch:
            self._fail(
                f'revision provider requires the integration checkout: '
                f'expected {integration_branch!r}, found {branch!r}'
            )
        head = ref_tip(repo_root, 'HEAD')
        if head != request.expected_head:
            self._fail(
                f'integration HEAD changed: expected {request.expected_head}, found {head}'
            )
        digest = manifest_digest(manifest)
        if (
            request.expected_manifest_digest is not None
            and digest != request.expected_manifest_digest
        ):
            self._fail(
                'manifest digest changed: '
                f'expected {request.expected_manifest_digest}, found {digest}'
            )
        hooks = managed_push_guard_policy(repo_root, manifest)
        if hooks.get('required') and not hooks.get('ready') and not hooks.get('disabled'):
            self._fail(
                'managed repository guards are not ready; install or explicitly disable them '
                'before revision-provider preflight'
            )
        (
            base_ref,
            base_ref_full_name,
            base_ref_sha,
            base_ref_object_sha,
            base_ref_observation,
        ) = self._resolve_base_ref(repo_root, manifest)
        self._assert_base_ref_is_not_managed(
            manifest, base_ref, base_ref_full_name
        )
        validation = validate_manifest(repo_root, manifest)
        stale_paths = {
            item['path']
            for item in validation['details']['integration'].get(
                'derived_projection_stale'
            ) or []
        }
        declared_paths = {item.path for item in request.paths}
        repairs_all_stale_paths = bool(stale_paths) and (
            request.action == 'check' or stale_paths <= declared_paths
        )
        blocking_errors = [
            error for error in validation['errors']
            if not (
                repairs_all_stale_paths
                and error.startswith('derived-projection-stale:')
            )
        ]
        if blocking_errors:
            self._fail('Syncwheel validation failed: ' + '; '.join(blocking_errors))
        missing_declared = [
            item for item in validation['details']['stacks']
            if item['id'] in manifest['integration'].get('stacks', [])
            and item.get('missing_from_integration')
        ]
        if missing_declared:
            self._fail(
                'integration declared stack(s) are missing from integration: '
                + '; '.join(
                    f"{item['id']}=" + ','.join(item['missing_from_integration'])
                    for item in missing_declared
                )
            )
        unmapped = list(validation['details']['integration'].get('unmapped_commits') or [])
        if unmapped:
            self._fail(
                'integration already contains unmapped commits: ' + ', '.join(unmapped)
            )
        if require_clean:
            self._ensure_clean(repo_root)
            self._validate_hashes(repo_root, request, 'before')
        coordination = self._fresh_coordination_handoff(repo_root, manifest)
        remote_refs = self._remote_refs(repo_root)
        managed_local_refs = self._managed_local_refs(repo_root, manifest)
        ref_transaction_refs = dict(managed_local_refs)
        ref_transaction_refs.update(remote_refs)
        if base_ref_full_name:
            ref_transaction_refs[base_ref_full_name] = base_ref_observation
        integration_ref = f'refs/heads/{integration_branch}'
        ref_transaction_refs[integration_ref] = self._direct_ref_observation(
            integration_ref, head
        )
        ref_transaction_refs[f'refs/heads/{request.draft_branch}'] = None
        ref_transaction_refs = self._expand_symbolic_target_leases(
            repo_root, ref_transaction_refs
        )
        return {
            'repoRoot': repo_root,
            'manifest': manifest,
            'manifestPath': manifest_path,
            'manifestDigest': digest,
            'head': head,
            'integrationBranch': integration_branch,
            'worktrees': self._worktrees(repo_root),
            'remoteRefs': remote_refs,
            'managedLocalRefs': managed_local_refs,
            'refTransactionRefs': ref_transaction_refs,
            'baseRef': base_ref,
            'baseRefFullName': base_ref_full_name,
            'baseRefSha': base_ref_sha,
            'baseRefObjectSha': base_ref_object_sha,
            'baseRefObservation': base_ref_observation,
            'projectionBaseSha': base_ref_sha,
            'projectionBaseKind': 'manifest-base',
            'integrationCompositionDigest': integration_composition_digest(manifest),
            'indexSha256': self._index_sha256(repo_root),
            'unmappedIntegrationCommits': unmapped,
            'coordination': coordination,
        }

    def _journal_directory(self, request):
        repo_root = self._repo_root(request)
        return git_common_dir(repo_root) / 'syncwheel' / 'revision-provider'

    def _journal_path(self, request):
        return self._journal_directory(request) / f'{request.operation_id}.json'

    @contextlib.contextmanager
    def operation_lock(self, request):
        if fcntl is None:
            self._fail(f'revision-provider locking is unsupported on {sys.platform}')
        directory = self._journal_directory(request).parent
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / 'revision-provider.lock'
        with lock_path.open('a+') as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._fail(f'another revision-provider operation holds {lock_path}')
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load_journal(self, request):
        path = self._journal_path(request)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            self._fail(f'invalid revision-provider journal {path}: {exc}')
        if not isinstance(payload, dict):
            self._fail(f'invalid revision-provider journal root: {path}')
        return payload

    def _fsync_directory(self, directory):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def save_journal(self, request, journal):
        path = self._journal_path(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(journal, indent=2, sort_keys=True) + '\n'
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{request.operation_id}.', suffix='.tmp', dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'w') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def delete_journal(self, request):
        path = self._journal_path(request)
        path.unlink(missing_ok=True)
        if path.parent.exists():
            self._fsync_directory(path.parent)

    def _derived_paths_digest(self, paths):
        return canonical_json_digest(list(paths or []))

    def _manifest_lease_digest(self, journal):
        if journal.get('manifestReplaced'):
            return journal.get('manifestDigest')
        return journal.get('observedManifestDigest')

    def _pending_operation_manifest_matches(self, request, journal, manifest):
        """Recognize the provider's own manifest write before its journal catches up."""
        if (
            journal.get('projectionRoute') != 'manifest-base'
            or journal.get('phase') != 'product_committed'
            or not journal.get('productCommitSha')
        ):
            return False
        existing = stack_map(manifest).get(request.draft_stack_id)
        if existing != self._desired_stack(request, journal, manifest):
            return False
        stripped = self._manifest_without_operation_stack(manifest, request)
        return manifest_digest(stripped) == journal.get('observedManifestDigest')

    def _expiration_digest_pair(self, request, journal, manifest):
        if journal.get('projectionRoute') == 'derived':
            observed_composition = journal.get('integrationCompositionDigest')
            current_composition = integration_composition_digest(manifest)
            if observed_composition != current_composition:
                return observed_composition, current_composition
            observed_paths = journal.get('derivedPathsDigest')
            current_paths = self._derived_paths_digest(
                manifest['integration'].get('derived_paths') or []
            )
            return observed_paths, current_paths
        return self._manifest_lease_digest(journal), manifest_digest(manifest)

    def _expire_if_operation_lease_changed(self, request, journal, manifest):
        if journal.get('phase') in {'verified', 'expired'}:
            return
        route = journal.get('projectionRoute')
        if route == 'derived':
            current_composition = integration_composition_digest(manifest)
            if current_composition != journal.get('integrationCompositionDigest'):
                self.expire_manifest_invalidated(
                    request,
                    journal,
                    'integration composition changed while derived projection was pending',
                )
            current_paths = list(manifest['integration'].get('derived_paths') or [])
            if (
                current_paths != list(journal.get('derivedPaths') or [])
                or self._derived_paths_digest(current_paths)
                != journal.get('derivedPathsDigest')
            ):
                self.expire_manifest_invalidated(
                    request,
                    journal,
                    'integration.derived_paths changed while derived projection was pending',
                )
            return
        current_digest = manifest_digest(manifest)
        if current_digest == self._manifest_lease_digest(journal):
            return
        if self._pending_operation_manifest_matches(
            request, journal, manifest
        ):
            return
        reason = (
            'manifest changed after preflight'
            if route is None
            else 'manifest changed while revision-provider operation was pending'
        )
        self.expire_manifest_invalidated(request, journal, reason)

    def expire_manifest_invalidated(self, request, journal, reason):
        """Terminally expire a receipt whose manifest lease can no longer recover.

        This deliberately changes only local provider state.  The caller must
        start a new Agentwheel update, which obtains a fresh manifest lease.
        """
        remedy = 'run a new Agentwheel update'
        repo_root = self._repo_root(request)
        manifest, manifest_path = self._manifest(repo_root)
        observed_digest, current_digest = self._expiration_digest_pair(
            request, journal, manifest
        )
        expiration = journal.get('expiration') or {
            'reason': reason,
            'remedy': remedy,
            'observedDigest': observed_digest,
            'currentDigest': current_digest,
            'decidedAt': iso_utc_now(),
        }
        event_payload = {
            'operation_id': request.operation_id,
            'reason': expiration['reason'],
            'remedy': expiration['remedy'],
            'observed_digest': expiration['observedDigest'],
            'current_digest': expiration['currentDigest'],
            'decided_at': expiration['decidedAt'],
        }
        if journal.get('phase') != 'expired':
            journal['phase'] = 'expired'
            journal['expiration'] = expiration
            self.save_journal(request, journal)
            self.checkpoint('receipt_expired')
        recover_ledger_tail(repo_root, manifest_path)
        matches = [
            event for event in load_ledger_events(repo_root, manifest_path)
            if event.get('type') == 'revision_provider_expired'
            and (event.get('payload') or {}).get('operation_id') == request.operation_id
        ]
        if matches and (len(matches) != 1 or matches[0].get('payload') != event_payload):
            self._fail(
                f'ledger collision while expiring revision-provider operation '
                f'{request.operation_id}'
            )
        if not matches:
            append_ledger_event(
                repo_root, 'revision_provider_expired', event_payload, manifest_path
            )
            self.checkpoint('expiration_ledger_event_written')
        self._fail(
            f'operation {request.operation_id} expired: {expiration["reason"]}; {expiration["remedy"]}'
        )

    def check(self, request):
        observation = self._validate_repository(
            self._repo_root(request), request, require_clean=True
        )
        stacks = stack_map(observation['manifest'])
        if request.draft_stack_id in stacks:
            self._fail(f'draft stack id already exists: {request.draft_stack_id}')
        if any(
            stack['branch'] == request.draft_branch
            for stack in observation['manifest']['stacks']
        ) or branch_exists(observation['repoRoot'], request.draft_branch):
            self._fail(f'draft branch already exists: {request.draft_branch}')
        return observation

    def preflight(self, request):
        observation = self._validate_repository(
            self._repo_root(request), request, require_clean=False
        )
        self._validate_before_hashes_at_head(observation['repoRoot'], request)
        self._ensure_after_scope(observation['repoRoot'], request)
        stacks = stack_map(observation['manifest'])
        if request.draft_stack_id in stacks:
            self._fail(f'draft stack id already exists: {request.draft_stack_id}')
        if any(
            stack['branch'] == request.draft_branch
            for stack in observation['manifest']['stacks']
        ) or branch_exists(observation['repoRoot'], request.draft_branch):
            self._fail(f'draft branch already exists: {request.draft_branch}')
        return observation

    def verify_after_paths(self, request):
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        if get_current_branch(repo_root) != manifest['integration']['branch']:
            self._fail('integration branch changed after preflight')
        if ref_tip(repo_root, 'HEAD') != request.expected_head:
            self._fail('integration HEAD changed after preflight')
        journal = self.load_journal(request)
        if journal:
            self._expire_if_operation_lease_changed(request, journal, manifest)
            expected_refs = dict(journal['managedLocalRefs'])
            expected_refs.update(journal['baselineRemoteRefs'])
            if journal.get('baseRefFullName'):
                expected_refs[journal['baseRefFullName']] = journal[
                    'baseRefObservation'
                ]
            expected_refs[f'refs/heads/{request.draft_branch}'] = None
            self._assert_ref_leases(repo_root, expected_refs)
            self._assert_index_lease(
                repo_root, journal['baselineIndexSha256'], 'preflight'
            )
        self._ensure_after_scope(repo_root, request)

    def _prepare_exact_commit(self, repo_root, parent, path_objects, message):
        descriptor, index_name = tempfile.mkstemp(prefix='syncwheel-revision-index-')
        os.close(descriptor)
        index_path = Path(index_name)
        index_path.unlink(missing_ok=True)
        environment = {'GIT_INDEX_FILE': str(index_path)}
        try:
            git(repo_root, 'read-tree', parent, env=environment)
            for relative, entry in sorted(path_objects.items()):
                if entry['blob'] is None:
                    git(
                        repo_root, 'update-index', '--force-remove', '--', relative,
                        env=environment, check=False,
                    )
                    continue
                git(
                    repo_root,
                    'update-index',
                    '--add',
                    '--cacheinfo',
                    f"{entry['mode']},{entry['blob']},{relative}",
                    env=environment,
                )
            tree = git(repo_root, 'write-tree', env=environment).stdout.strip()
            parent_tree = ref_tree(repo_root, parent)
            if tree == parent_tree:
                return None
            command = with_git_identity(
                repo_root,
                ['git', 'commit-tree', tree, '-p', parent, '-F', '-'],
            )
            commit = run(command, cwd=repo_root, input_text=message).stdout.strip()
            changed = set(
                item
                for item in git(
                    repo_root,
                    'diff-tree',
                    '--no-commit-id',
                    '--name-only',
                    '-r',
                    '-z',
                    parent,
                    commit,
                ).stdout.split('\0')
                if item
            )
            if not changed or changed != set(path_objects):
                self._fail('prepared commit escaped the exact path allowlist')
            return {'commit': commit, 'tree': tree, 'pathObjects': path_objects}
        finally:
            index_path.unlink(missing_ok=True)

    def prepare_product_commit(self, request, message):
        repo_root = self._repo_root(request)
        self._ensure_after_scope(repo_root, request)
        path_objects = self._capture_path_objects(repo_root, request)
        prepared = self._prepare_exact_commit(
            repo_root,
            request.expected_head,
            path_objects,
            message,
        )
        if prepared is None:
            return None
        return {
            'candidateProductCommitSha': prepared['commit'],
            'candidateProductTreeSha': prepared['tree'],
            'productPathObjects': prepared['pathObjects'],
        }

    def _projection_reproduces_product_blobs(self, repo_root, projection, path_objects):
        if projection.get('status') != 'projected':
            return False
        for path, expected in path_objects.items():
            actual = self._tree_entry(repo_root, projection['tip'], path)
            actual_blob = actual['blob'] if actual is not None else None
            if actual_blob != expected['blob']:
                return False
        return True

    def _journal_product_path_objects(self, request, journal):
        path_objects = journal.get('productPathObjects')
        expected_paths = sorted(item.path for item in request.paths)
        valid = (
            isinstance(path_objects, dict)
            and sorted(path_objects) == expected_paths
            and bool(path_objects)
        )
        if valid:
            for value in path_objects.values():
                if not isinstance(value, dict) or set(value) != {
                    'sha256', 'blob', 'mode'
                }:
                    valid = False
                    break
                sha256 = value.get('sha256')
                blob = value.get('blob')
                mode = value.get('mode')
                deletion = sha256 is None and blob is None and mode is None
                regular = (
                    isinstance(sha256, str)
                    and re.fullmatch(r'[0-9a-f]{64}', sha256)
                    and isinstance(blob, str)
                    and re.fullmatch(r'[0-9a-f]{40,64}', blob)
                    and mode in {'100644', '100755'}
                )
                if not deletion and not regular:
                    valid = False
                    break
        if not valid:
            self._fail(
                'journaled productPathObjects is missing or invalid; release the '
                'prepared operation, restore the declared paths if needed, then run '
                'a new Agentwheel update'
            )
        return path_objects

    def prepare_draft_projection(self, request, journal):
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        if manifest_digest(manifest) != journal['observedManifestDigest']:
            self.expire_manifest_invalidated(
                request,
                journal,
                'manifest changed before draft object preparation',
            )
        projection = deterministic_stack_projection(
            repo_root,
            journal['baseRefSha'],
            [journal['candidateProductCommitSha']],
        )
        reproduces_product = self._projection_reproduces_product_blobs(
            repo_root,
            projection,
            journal['productPathObjects'],
        )
        if reproduces_product:
            return {
                'projectionRoute': 'manifest-base',
                'candidateDraftCommitSha': projection['tip'],
                'candidateDraftTreeSha': projection['tree'],
            }
        prefixes = manifest['integration'].get('derived_paths') or []
        paths = sorted(journal['productPathObjects'])
        if not prefixes or not all(any(path.startswith(prefix) for prefix in prefixes) for path in paths):
            conflicts = ', '.join(projection.get('paths') or []) or 'unknown path(s)'
            self._fail(
                'product commit has a conflicting draft projection; conflicts: '
                f'{conflicts}; base {projection.get("base", journal["baseRefSha"])}; '
                'derived route refuses paths outside integration.derived_paths'
            )
        content_digest = derived_projection_paths_digest({
            path: journal['productPathObjects'][path]['blob']
            for path in paths
        })
        prepared = self._prepare_exact_commit(
            repo_root,
            request.expected_head,
            journal['productPathObjects'],
            self.provider.product_commit_message(request)
            + f'Syncwheel-Derived-Projection: {request.operation_id}\n'
            + f'Syncwheel-Derived-Paths: {content_digest}\n',
        )
        if prepared is None:
            self._fail('derived projection unexpectedly has no product delta')
        return {
            'projectionRoute': 'derived',
            'candidateProductCommitSha': prepared['commit'],
            'candidateProductTreeSha': prepared['tree'],
            'integrationCompositionDigest': integration_composition_digest(manifest),
            'derivedPaths': list(prefixes),
            'derivedPathsDigest': self._derived_paths_digest(prefixes),
            'derivedContentDigest': content_digest,
        }

    def verify_projection_route(self, request, journal):
        """Recompute the persisted route without replacing its candidate objects."""
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        self._expire_if_operation_lease_changed(request, journal, manifest)
        path_objects = self._journal_product_path_objects(request, journal)
        projection = deterministic_stack_projection(
            repo_root,
            journal['baseRefSha'],
            [journal['candidateProductCommitSha']],
        )
        reproduces_product = self._projection_reproduces_product_blobs(
            repo_root,
            projection,
            path_objects,
        )
        recomputed_route = 'manifest-base' if reproduces_product else 'derived'
        if recomputed_route != journal.get('projectionRoute'):
            self._fail(
                'journaled projection route no longer matches its immutable candidate'
            )
        if recomputed_route == 'manifest-base':
            if (
                projection.get('tip') != journal.get('candidateDraftCommitSha')
                or projection.get('tree') != journal.get('candidateDraftTreeSha')
            ):
                self._fail('journaled draft projection object changed')
            return
        if journal.get('candidateDraftCommitSha') is not None:
            self._fail('journaled derived route unexpectedly owns a draft candidate')
        paths = sorted(path_objects)
        content_digest = derived_projection_paths_digest({
            path: path_objects[path]['blob']
            for path in paths
        })
        if content_digest != journal.get('derivedContentDigest'):
            self._fail('journaled derived path/content digest changed')
        candidate_provenance = [{
            'operation_id': request.operation_id,
            'commit': journal['candidateProductCommitSha'],
            'paths': paths,
            'paths_digest': content_digest,
            'composition_digest': journal['integrationCompositionDigest'],
        }]
        if not is_derived_projection_commit(
            repo_root,
            manifest,
            journal['candidateProductCommitSha'],
            provenance=candidate_provenance,
        ):
            self._fail(
                'journaled derived candidate is missing its path or trailer ownership proof'
            )

    def current_head(self, request):
        return ref_tip(self._repo_root(request), 'HEAD')

    def _tree_entry(self, repo_root, commit, relative):
        listing = git(repo_root, 'ls-tree', '-z', commit, '--', relative, check=False)
        if listing.returncode != 0:
            self._fail(f'could not inspect candidate tree path: {relative}')
        entries = [entry for entry in listing.stdout.split('\0') if entry]
        if not entries:
            return None
        if len(entries) != 1:
            self._fail(f'ambiguous candidate tree entry: {relative}')
        entry = entries[0]
        metadata, separator, listed_path = entry.partition('\t')
        if not separator or listed_path != relative:
            self._fail(f'ambiguous candidate tree entry: {relative}')
        mode, object_type, object_id = metadata.split(' ', 2)
        if object_type != 'blob' or mode not in {'100644', '100755'}:
            self._fail(f'candidate tree path is not a regular blob: {relative}')
        return {'mode': mode, 'blob': object_id}

    def _verify_candidate_object(
        self, repo_root, commit, tree, parent, path_objects
    ):
        if ref_tip(repo_root, f'{commit}^') != parent:
            self._fail('journaled candidate parent changed or is unavailable')
        if ref_tree(repo_root, commit) != tree:
            self._fail('journaled candidate tree does not match its object id')
        changed = {
            item for item in git(
                repo_root, 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z',
                parent, commit,
            ).stdout.split('\0') if item
        }
        if changed != set(path_objects):
            self._fail('journaled candidate tree escaped the exact path allowlist')
        for relative, expected in path_objects.items():
            actual = self._tree_entry(repo_root, commit, relative)
            if expected['blob'] is None:
                if actual is not None:
                    self._fail(f'candidate did not delete declared path: {relative}')
                continue
            if actual != {'mode': expected['mode'], 'blob': expected['blob']}:
                self._fail(f'candidate blob or mode lease failed for {relative}')
            result = subprocess.run(
                ['git', 'cat-file', 'blob', expected['blob']],
                cwd=repo_root,
                capture_output=True,
            )
            if result.returncode != 0:
                self._fail(f'candidate blob is unavailable for {relative}')
            if hashlib.sha256(result.stdout).hexdigest() != expected['sha256']:
                self._fail(f'candidate blob bytes do not match declared hash for {relative}')

    def _verify_worktree_objects(self, repo_root, path_objects):
        for relative, expected in path_objects.items():
            observed = self._read_product_path(repo_root, relative)
            if expected['blob'] is None:
                if observed is not None:
                    self._fail(f'declared deletion was replaced before ref update: {relative}')
                continue
            if observed is None:
                self._fail(f'declared file disappeared before ref update: {relative}')
            if (
                observed['sha256'] != expected['sha256']
                or observed['mode'] != expected['mode']
            ):
                self._fail(f'worktree bytes or mode changed before ref update: {relative}')

    def _expected_managed_refs(self, request, journal, integration_tip, *, draft_owned):
        expected = dict(journal['managedLocalRefs'])
        expected.update(journal['baselineRemoteRefs'])
        if journal.get('baseRefFullName'):
            expected[journal['baseRefFullName']] = journal['baseRefObservation']
        manifest, _ = self._manifest(self._repo_root(request))
        integration_ref = f"refs/heads/{manifest['integration']['branch']}"
        expected[integration_ref] = self._direct_ref_observation(
            integration_ref, integration_tip
        )
        draft_ref = f'refs/heads/{request.draft_branch}'
        expected[draft_ref] = (
            self._direct_ref_observation(
                draft_ref, journal['candidateDraftCommitSha']
            )
            if draft_owned
            else None
        )
        return expected

    def _assert_operation_worktree(self, repo_root, request, journal, kind):
        manifest, _ = self._manifest(repo_root)
        if kind == 'product':
            self._expire_if_operation_lease_changed(
                request, journal, manifest
            )
            self._ensure_after_scope(
                repo_root,
                request,
                allowed_outside=(
                    (self.MANIFEST_PRODUCT_PATH,)
                    if journal.get('projectionRoute') == 'derived'
                    else ()
                ),
            )
            return
        if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
            self._fail('control ref update requires a clean, conflict-free index')
        dirty = self._dirty_paths(repo_root)
        if dirty != {self.MANIFEST_PRODUCT_PATH}:
            self._fail(
                'control ref update requires only .syncwheel/manifest.json; found: '
                + ', '.join(sorted(dirty))
            )
        self._expire_if_operation_lease_changed(request, journal, manifest)

    def _assert_ref_leases(self, repo_root, expected):
        drift = []
        for ref, observation in sorted(expected.items()):
            actual = self._observe_ref(repo_root, ref)
            if actual != observation:
                drift.append(
                    f'{ref} (expected {observation or "missing"}, '
                    f'found {actual or "missing"})'
                )
        if drift:
            self._fail('managed local ref lease was lost: ' + '; '.join(drift))

    def _assert_index_lease(self, repo_root, expected, stage):
        actual = self._index_sha256(repo_root)
        if actual != expected:
            self._fail(
                f'real Git index lease was lost before {stage}: '
                f'expected {expected}, found {actual}'
            )

    def _expand_symbolic_target_leases(self, repo_root, expected):
        expanded = dict(expected)
        pending = [
            observation
            for observation in expanded.values()
            if observation is not None and observation['kind'] == 'symbolic'
        ]
        visited = set()
        while pending:
            symbolic = pending.pop()
            name = symbolic['name']
            if name in visited:
                continue
            visited.add(name)
            target = symbolic['symbolicTarget']
            target_observation = expanded.get(target)
            if target not in expanded:
                target_observation = self._observe_ref(
                    repo_root, target, allow_missing=False
                )
                expanded[target] = target_observation
            if target_observation is None:
                self._fail(
                    f'symbolic ref lease target disappeared: {name!r} -> {target!r}'
                )
            if target_observation['objectOid'] != symbolic['objectOid']:
                self._fail(
                    f'symbolic ref lease object differs from its immediate target: '
                    f'{name!r} -> {target!r}'
                )
            if target_observation['kind'] == 'symbolic':
                pending.append(target_observation)
        return expanded

    def _journal_ref_transaction_refs(self, request, journal):
        raw = journal.get('refTransactionRefs')
        if not isinstance(raw, dict) or not raw:
            self._fail(
                'operation journal has no complete ref-transaction observation set'
            )
        integration_branch = journal.get('integrationBranch')
        if (
            not isinstance(integration_branch, str)
            or run(
                ['git', 'check-ref-format', '--branch', integration_branch],
                check=False,
            ).returncode != 0
        ):
            self._fail('operation journal has an invalid integration branch')

        required = {
            f'refs/heads/{integration_branch}',
            f'refs/heads/{request.draft_branch}',
        }
        base_ref = journal.get('baseRefFullName')
        if base_ref is not None:
            if not isinstance(base_ref, str):
                self._fail('operation journal has an invalid base ref name')
            required.add(base_ref)
        for field in ('managedLocalRefs', 'baselineRemoteRefs'):
            source = journal.get(field)
            if not isinstance(source, dict):
                self._fail(f'operation journal has an invalid {field} snapshot')
            required.update(source)
            for name, observation in source.items():
                if name not in raw or raw[name] != observation:
                    self._fail(
                        f'operation journal ref-transaction snapshot omitted or '
                        f'changed {name!r} from {field}'
                    )
        if base_ref is not None and raw.get(base_ref) != journal.get(
            'baseRefObservation'
        ):
            self._fail(
                'operation journal ref-transaction snapshot changed its base ref'
            )
        missing = sorted(required.difference(raw))
        if missing:
            self._fail(
                'operation journal ref-transaction snapshot omitted required refs: '
                + ', '.join(missing)
            )

        observations = {}
        fields = {'name', 'kind', 'objectOid', 'symbolicTarget'}
        for name, observation in raw.items():
            if (
                not isinstance(name, str)
                or run(['git', 'check-ref-format', name], check=False).returncode != 0
            ):
                self._fail(
                    f'operation journal contains an invalid ref-transaction name: '
                    f'{name!r}'
                )
            if observation is None:
                observations[name] = None
                continue
            if not isinstance(observation, dict) or set(observation) != fields:
                self._fail(
                    f'operation journal contains an invalid typed ref observation '
                    f'for {name!r}'
                )
            kind = observation.get('kind')
            target = observation.get('symbolicTarget')
            if (
                observation.get('name') != name
                or kind not in {'direct', 'symbolic'}
                or not re.fullmatch(r'[0-9a-f]{40}', observation.get('objectOid') or '')
            ):
                self._fail(
                    f'operation journal contains an invalid typed ref observation '
                    f'for {name!r}'
                )
            if kind == 'direct' and target is not None:
                self._fail(
                    f'operation journal direct ref {name!r} has a symbolic target'
                )
            if kind == 'symbolic' and (
                not isinstance(target, str)
                or run(['git', 'check-ref-format', target], check=False).returncode != 0
            ):
                self._fail(
                    f'operation journal symbolic ref {name!r} has an invalid target'
                )
            observations[name] = observation

        for name, observation in observations.items():
            if observation is None or observation['kind'] != 'symbolic':
                continue
            target = observation['symbolicTarget']
            target_observation = observations.get(target)
            if target not in observations or target_observation is None:
                self._fail(
                    f'operation journal symbolic ref {name!r} omitted its typed '
                    f'referent {target!r}'
                )
            if target_observation['objectOid'] != observation['objectOid']:
                self._fail(
                    f'operation journal symbolic ref {name!r} differs from its '
                    f'referent {target!r}'
                )
        return observations

    def verify_recovery_gate(self, request, journal):
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        self._expire_if_operation_lease_changed(request, journal, manifest)
        expected = self._journal_ref_transaction_refs(request, journal)
        self._assert_no_ref_transaction_locks(repo_root, expected)

    def _ref_transaction_lock_paths(self, repo_root, expected):
        paths = []
        for ref in sorted(expected):
            raw = git(
                repo_root, 'rev-parse', '--git-path', f'{ref}.lock'
            ).stdout.strip()
            path = Path(raw)
            if not path.is_absolute():
                path = repo_root / path
            paths.append(Path(os.path.abspath(path)))
        packed_raw = git(
            repo_root, 'rev-parse', '--git-path', 'packed-refs.lock'
        ).stdout.strip()
        packed = Path(packed_raw)
        if not packed.is_absolute():
            packed = repo_root / packed
        paths.append(Path(os.path.abspath(packed)))
        head_raw = git(
            repo_root, 'rev-parse', '--git-path', 'HEAD.lock'
        ).stdout.strip()
        head = Path(head_raw)
        if not head.is_absolute():
            head = repo_root / head
        paths.append(Path(os.path.abspath(head)))
        return sorted(set(paths), key=str)

    def _assert_no_ref_transaction_locks(self, repo_root, expected):
        existing = [
            path
            for path in self._ref_transaction_lock_paths(repo_root, expected)
            if path.exists() or path.is_symlink()
        ]
        if existing:
            self._fail(
                'Git ref transaction lock ownership cannot be proven after an '
                'interrupted writer; automatic cleanup is forbidden. Verify no '
                'Git writer is active, inspect and remove only these exact lock '
                'paths manually, then retry recover: '
                + ', '.join(str(path) for path in existing)
            )

    def _cas_ref_with_leases(self, repo_root, target_ref, new_tip, expected):
        expected = self._expand_symbolic_target_leases(repo_root, expected)
        target_observation = expected[target_ref]
        if target_observation is not None and target_observation['kind'] != 'direct':
            self._fail(f'compare-and-swap target must be a direct ref: {target_ref}')
        self._assert_ref_leases(repo_root, expected)
        self._assert_no_ref_transaction_locks(repo_root, expected)
        commands = []
        for ref, observation in sorted(expected.items()):
            old = (
                observation['objectOid']
                if observation is not None
                else ('0' * 40)
            )
            commands.append('option no-deref')
            if ref == target_ref:
                commands.append(f'update {ref} {new_tip} {old}')
            else:
                commands.append(f'verify {ref} {old}')

        process = subprocess.Popen(
            ['git', 'update-ref', '--stdin'],
            cwd=repo_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        prepared = False
        try:
            process.stdin.write('start\n')
            process.stdin.flush()
            start_response = process.stdout.readline().strip()
            if start_response != 'start: ok':
                stdout, stderr = self._abort_ref_transaction(process)
                detail = stderr.strip() or stdout.strip() or start_response
                self._fail(f'managed ref transaction could not start: {detail}')

            process.stdin.write('\n'.join([*commands, 'prepare', '']))
            process.stdin.flush()
            prepare_response = process.stdout.readline().strip()
            if prepare_response != 'prepare: ok':
                stdout, stderr = self._abort_ref_transaction(process)
                detail = stderr.strip() or stdout.strip() or prepare_response
                self._fail(f'managed ref transaction could not prepare: {detail}')
            prepared = True

            self.checkpoint('ref_transaction_prepared')
            self._assert_ref_leases(repo_root, expected)

            process.stdin.write('commit\n')
            process.stdin.flush()
            process.stdin.close()
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
            returncode = process.wait()
            prepared = False
            if returncode != 0 or stdout.strip() != 'commit: ok':
                detail = stderr.strip() or stdout.strip()
                self._fail(
                    'managed ref compare-and-swap failed during commit: ' + detail
                )
        except BaseException:
            if prepared or process.poll() is None:
                self._abort_ref_transaction(process)
            raise

    def _abort_ref_transaction(self, process):
        if process.poll() is None and process.stdin and not process.stdin.closed:
            try:
                process.stdin.write('abort\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        stdout = process.stdout.read() if process.stdout else ''
        stderr = process.stderr.read() if process.stderr else ''
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
        process.wait()
        return stdout, stderr

    def _verify_draft_candidate(self, repo_root, request, journal):
        projection = deterministic_stack_projection(
            repo_root,
            journal.get('projectionBaseSha', journal['baseRefSha']),
            [journal['candidateProductCommitSha']],
        )
        if projection.get('status') != 'projected':
            self._fail('draft projection is no longer a non-empty conflict-free object')
        if (
            projection['tip'] != journal['candidateDraftCommitSha']
            or projection['tree'] != journal['candidateDraftTreeSha']
        ):
            self._fail('journaled draft projection object changed')

    def ensure_draft_ref_owned(self, request, journal):
        repo_root = self._repo_root(request)
        self.verify_recovery_gate(request, journal)
        if ref_tip(repo_root, 'HEAD') != request.expected_head:
            self._fail('integration advanced before draft ownership')
        self._assert_operation_worktree(repo_root, request, journal, 'product')
        self._verify_candidate_object(
            repo_root,
            journal['candidateProductCommitSha'],
            journal['candidateProductTreeSha'],
            request.expected_head,
            journal['productPathObjects'],
        )
        self._verify_worktree_objects(repo_root, journal['productPathObjects'])
        self._verify_draft_candidate(repo_root, request, journal)
        self._assert_index_lease(
            repo_root, journal['baselineIndexSha256'], 'draft ownership'
        )
        expected = self._expected_managed_refs(
            request, journal, request.expected_head, draft_owned=False
        )
        draft_ref = f'refs/heads/{request.draft_branch}'
        actual = self._observe_ref(repo_root, draft_ref)
        actual_oid = actual['objectOid'] if actual is not None else None
        if actual_oid == journal['candidateDraftCommitSha']:
            expected[draft_ref] = self._direct_ref_observation(
                draft_ref, journal['candidateDraftCommitSha']
            )
            self._assert_ref_leases(repo_root, expected)
            self.verify_recovery_gate(request, journal)
            return
        if actual_oid is not None:
            self._fail(f'draft branch collision: expected absent, found {actual_oid}')
        self._cas_ref_with_leases(
            repo_root, draft_ref, journal['candidateDraftCommitSha'], expected
        )
        self.verify_recovery_gate(request, journal)
        self.checkpoint('draft_ref_cas')

    def _run_commit_validation_hooks(self, repo_root, commit):
        hooks_dir, _ = active_hooks_dir(repo_root)
        prepare_commit_message = hooks_dir / 'prepare-commit-msg'
        if prepare_commit_message.is_file() and os.access(prepare_commit_message, os.X_OK):
            self._fail(
                'executable prepare-commit-msg hooks are unsupported by the deterministic '
                'revision provider; disable the hook or commit outside this provider'
            )
        descriptor, index_name = tempfile.mkstemp(prefix='syncwheel-hook-index-')
        os.close(descriptor)
        index_path = Path(index_name)
        index_path.unlink(missing_ok=True)
        message_descriptor, message_name = tempfile.mkstemp(
            prefix='syncwheel-commit-message-', suffix='.txt'
        )
        message_path = Path(message_name)
        message = git(repo_root, 'show', '-s', '--format=%B', commit).stdout
        try:
            with os.fdopen(message_descriptor, 'w') as handle:
                handle.write(message)
                handle.flush()
                os.fsync(handle.fileno())
            environment = os.environ.copy()
            environment.update(
                {
                    'GIT_INDEX_FILE': str(index_path),
                    'SYNCWHEEL_REVISION_PROVIDER_COMMIT': commit,
                }
            )
            git(repo_root, 'read-tree', commit, env={'GIT_INDEX_FILE': str(index_path)})
            for hook_name, arguments in (
                ('pre-commit', []),
                ('commit-msg', [str(message_path)]),
            ):
                hook = hooks_dir / hook_name
                if not hook.is_file() or not os.access(hook, os.X_OK):
                    continue
                result = subprocess.run(
                    [str(hook), *arguments],
                    cwd=repo_root,
                    text=True,
                    capture_output=True,
                    env=environment,
                )
                if result.returncode != 0:
                    detail = (result.stderr.strip() or result.stdout.strip())[:2000]
                    suffix = f': {detail}' if detail else ''
                    self._fail(
                        f'{hook_name} hook rejected prepared commit '
                        f'(exit {result.returncode}){suffix}'
                    )
            hook_tree = git(
                repo_root,
                'write-tree',
                env={'GIT_INDEX_FILE': str(index_path)},
            ).stdout.strip()
            if hook_tree != ref_tree(repo_root, commit):
                self._fail('commit hook modified the deterministic provider index')
            if message_path.read_text() != message:
                self._fail('commit-msg hook modified the deterministic provider message')
        finally:
            index_path.unlink(missing_ok=True)
            message_path.unlink(missing_ok=True)

    def _hook_repository_snapshot(self, repo_root, path_objects):
        observed_paths = {}
        for relative in path_objects:
            observed = self._read_product_path(repo_root, relative)
            observed_paths[relative] = None if observed is None else {
                'sha256': observed['sha256'], 'mode': observed['mode'],
            }
        return {
            'allRefs': self._all_refs(repo_root),
            'headSymbolic': git(
                repo_root, 'symbolic-ref', '--quiet', 'HEAD', check=False
            ).stdout.strip() or None,
            'headObject': ref_tip(repo_root, 'HEAD'),
            'worktreePorcelain': self._worktree_porcelain(repo_root),
            'indexSha256': self._index_sha256(repo_root),
            'indexTree': git(repo_root, 'write-tree').stdout.strip(),
            'status': git(
                repo_root,
                'status',
                '--porcelain=v1',
                '-z',
                '--untracked-files=all',
                env={'GIT_OPTIONAL_LOCKS': '0'},
            ).stdout,
            'paths': observed_paths,
        }

    def validate_prepared_commit(self, request, journal, kind):
        repo_root = self._repo_root(request)
        if kind == 'product':
            commit = journal['candidateProductCommitSha']
            tree = journal['candidateProductTreeSha']
            parent = request.expected_head
            path_objects = journal['productPathObjects']
            integration_tip = request.expected_head
            draft_owned = False
        elif kind == 'control':
            commit = journal['candidateControlCommitSha']
            tree = journal['candidateControlTreeSha']
            parent = journal['productCommitSha']
            path_objects = journal['controlPathObjects']
            integration_tip = journal['productCommitSha']
            draft_owned = True
        else:
            self._fail(f'unknown prepared commit kind: {kind}')
        self._assert_operation_worktree(repo_root, request, journal, kind)
        self._verify_candidate_object(
            repo_root, commit, tree, parent, path_objects
        )
        self._verify_worktree_objects(repo_root, path_objects)
        if kind == 'product' and journal.get('projectionRoute') != 'derived':
            self._verify_draft_candidate(repo_root, request, journal)
        expected_refs = self._expected_managed_refs(
            request, journal, integration_tip, draft_owned=draft_owned
        )
        self._assert_ref_leases(repo_root, expected_refs)
        expected_index = (
            journal['baselineIndexSha256']
            if kind == 'product'
            else journal.get('productIndexSha256')
        )
        if not expected_index:
            self._fail(f'{kind} index lease is missing from the operation journal')
        self._assert_index_lease(repo_root, expected_index, f'{kind} hook validation')
        before = self._hook_repository_snapshot(repo_root, path_objects)
        rejection = None
        try:
            self._run_commit_validation_hooks(repo_root, commit)
        except self.provider.RevisionProviderError as exc:
            rejection = exc
        after = self._hook_repository_snapshot(repo_root, path_objects)
        if after != before:
            self._fail(
                'commit hook changed repository state; no subsequent managed ref was moved'
            )
        if rejection is not None:
            raise rejection
        self._verify_candidate_object(
            repo_root, commit, tree, parent, path_objects
        )
        self._verify_worktree_objects(repo_root, path_objects)
        self._assert_ref_leases(repo_root, expected_refs)

    def publish_prepared_commit(self, request, commit, expected_parent):
        repo_root = self._repo_root(request)
        journal = self.load_journal(request)
        if journal is None:
            self._fail('prepared commit publication requires a journal')
        self.verify_recovery_gate(request, journal)
        parent = ref_tip(repo_root, f'{commit}^')
        if parent != expected_parent:
            self._fail(
                f'prepared commit parent mismatch: expected {expected_parent}, found {parent}'
            )
        current_head = ref_tip(repo_root, 'HEAD')
        if current_head not in {expected_parent, commit}:
            self._fail('integration HEAD changed before compare-and-swap publication')
        index_tree = git(repo_root, 'write-tree').stdout.strip()
        if index_tree not in {ref_tree(repo_root, expected_parent), ref_tree(repo_root, commit)}:
            self._fail('real index changed before compare-and-swap publication')
        manifest, _ = self._manifest(repo_root)
        branch = manifest['integration']['branch']
        if get_current_branch(repo_root) != branch:
            self._fail('primary checkout left the integration branch')
        if expected_parent == request.expected_head:
            derived = journal.get('projectionRoute') == 'derived'
            expected_index = journal['baselineIndexSha256']
            if not journal.get('productHooksValidated'):
                self._fail('product hooks were not durably validated before draft ownership')
            if not derived and ref_tip(repo_root, request.draft_branch) != journal.get(
                'candidateDraftCommitSha'
            ):
                self._fail('draft ownership is missing before integration publication')
            if current_head == expected_parent:
                self._assert_operation_worktree(
                    repo_root, request, journal, 'product'
                )
            self._verify_candidate_object(
                repo_root, commit, journal['candidateProductTreeSha'], expected_parent,
                journal['productPathObjects'],
            )
            self._verify_worktree_objects(repo_root, journal['productPathObjects'])
            if not derived:
                self._verify_draft_candidate(repo_root, request, journal)
            expected_refs = self._expected_managed_refs(
                request, journal, expected_parent, draft_owned=not derived
            )
        else:
            expected_index = journal.get('productIndexSha256')
            if not expected_index:
                self._fail('product index lease is missing before control publication')
            if not journal.get('controlHooksValidated'):
                self._fail('control hooks were not durably validated before publication')
            if current_head == expected_parent:
                self._assert_operation_worktree(
                    repo_root, request, journal, 'control'
                )
            self._verify_candidate_object(
                repo_root, commit, journal['candidateControlTreeSha'], expected_parent,
                journal['controlPathObjects'],
            )
            self._verify_worktree_objects(repo_root, journal['controlPathObjects'])
            expected_refs = self._expected_managed_refs(
                request, journal, expected_parent, draft_owned=True
            )
        integration_ref = f'refs/heads/{branch}'
        if current_head == commit:
            expected_refs[integration_ref] = self._direct_ref_observation(
                integration_ref, commit
            )
            self._assert_ref_leases(repo_root, expected_refs)
            self.verify_recovery_gate(request, journal)
            return commit
        self._assert_index_lease(repo_root, expected_index, 'integration publication')
        self._assert_ref_leases(repo_root, expected_refs)
        self._cas_ref_with_leases(repo_root, integration_ref, commit, expected_refs)
        self.verify_recovery_gate(request, journal)
        self.checkpoint(
            'integration_product_cas'
            if expected_parent == request.expected_head
            else 'integration_control_cas'
        )
        return commit

    def _deterministic_index(self, repo_root, commit, *, allowed_worktree_paths=()):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix='syncwheel-revision-aligned-index-'
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink(missing_ok=True)
        environment = {'GIT_INDEX_FILE': str(temporary)}
        try:
            git(repo_root, 'read-tree', commit, env=environment)
            refreshed = git(
                repo_root,
                'update-index',
                '--refresh',
                check=False,
                env=environment,
            )
            if refreshed.returncode != 0:
                mismatched = {
                    item
                    for item in git(
                        repo_root,
                        'diff-files',
                        '--name-only',
                        '-z',
                        env=environment,
                    ).stdout.split('\0')
                    if item
                }
                if mismatched - set(allowed_worktree_paths):
                    self._fail(
                        'working tree does not match the commit during index preparation: '
                        + (refreshed.stderr.strip() or refreshed.stdout.strip())
                    )
            tree = git(repo_root, 'write-tree', env=environment).stdout.strip()
            expected_tree = ref_tree(repo_root, commit)
            if tree != expected_tree:
                self._fail('deterministic replacement index does not match the commit tree')
            payload = temporary.read_bytes()
            return payload, hashlib.sha256(payload).hexdigest()
        finally:
            temporary.unlink(missing_ok=True)

    def _index_alignment_directory(self, request):
        return self._journal_directory(request) / 'index-alignment'

    def _ensure_index_alignment_directory(self, request):
        directory = self._index_alignment_directory(request)
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            self._fsync_directory(directory.parent)
        self._fsync_directory(directory)
        return directory

    def _index_alignment_record(
        self, request, journal, kind, commit, expected_sha, desired_sha
    ):
        filename = (
            f'{request.operation_id}-{kind}-{desired_sha}.index'
        )
        record = {
            'schemaVersion': 1,
            'kind': kind,
            'commitSha': commit,
            'expectedSha256': expected_sha,
            'desiredSha256': desired_sha,
            'backingFile': filename,
        }
        alignments = journal.get('indexAlignments')
        if alignments is None:
            alignments = {}
        if not isinstance(alignments, dict):
            self._fail('operation journal has invalid index-alignment ownership state')
        existing = alignments.get(kind)
        if existing is not None and existing != record:
            self._fail(f'{kind} index-alignment ownership record changed')
        if existing is None:
            updated = copy.deepcopy(journal)
            updated_alignments = dict(alignments)
            updated_alignments[kind] = record
            updated['indexAlignments'] = updated_alignments
            self.save_journal(request, updated)
            self.checkpoint(f'{kind}_index_alignment_prepared')
        return record

    def _prepare_index_backing(self, request, record, desired_payload, mode):
        directory = self._ensure_index_alignment_directory(request)
        backing = directory / record['backingFile']
        if backing.parent != directory:
            self._fail('index-alignment backing path escaped its ownership directory')
        try:
            existing_payload, metadata = self._read_regular_file(
                backing, 'journaled index-alignment backing file'
            )
        except self.provider.RevisionProviderError as exc:
            if backing.exists() or backing.is_symlink():
                raise
            descriptor = None
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, 'O_NOFOLLOW', 0)
            )
            try:
                descriptor = os.open(backing, flags, mode)
                os.fchmod(descriptor, mode)
                _write_all(descriptor, desired_payload)
                os.fsync(descriptor)
            except FileExistsError:
                self._fail(
                    f'index-alignment backing file appeared concurrently: {backing}'
                )
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            self._fsync_directory(directory)
            existing_payload, metadata = self._read_regular_file(
                backing, 'journaled index-alignment backing file'
            )
        if hashlib.sha256(existing_payload).hexdigest() != record['desiredSha256']:
            self._fail('journaled index-alignment backing bytes changed')
        if existing_payload != desired_payload:
            self._fail('journaled index-alignment backing payload is not deterministic')
        if stat.S_IMODE(metadata.st_mode) != mode:
            self._fail('journaled index-alignment backing mode changed')
        self.checkpoint(f"{record['kind']}_index_backing_fsynced")
        return backing

    def _same_regular_inode(self, first, second):
        try:
            first_stat = os.lstat(first)
            second_stat = os.lstat(second)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(first_stat.st_mode)
            and stat.S_ISREG(second_stat.st_mode)
            and first_stat.st_dev == second_stat.st_dev
            and first_stat.st_ino == second_stat.st_ino
        )

    def _remove_owned_index_lock(self, lock_path, backing):
        if not self._same_regular_inode(lock_path, backing):
            return False
        os.unlink(lock_path)
        self._fsync_directory(lock_path.parent)
        return True

    def _remove_index_backing(self, backing):
        try:
            metadata = os.lstat(backing)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            self._fail(f'index-alignment backing is no longer regular: {backing}')
        os.unlink(backing)
        self._fsync_directory(backing.parent)

    def recover_owned_index_lock(self, request, journal):
        index_path = self._index_path(self._repo_root(request))
        lock_path = Path(f'{index_path}.lock')
        if not (lock_path.exists() or lock_path.is_symlink()):
            return
        alignments = journal.get('indexAlignments')
        if not isinstance(alignments, dict):
            self._fail(f'Git index is locked by another writer: {lock_path}')
        owned = []
        for kind, record in alignments.items():
            if kind not in {'product', 'control'} or not isinstance(record, dict):
                self._fail('operation journal has invalid index-alignment ownership state')
            commit = record.get('commitSha')
            expected_sha = record.get('expectedSha256')
            desired_sha = record.get('desiredSha256')
            expected_filename = f'{request.operation_id}-{kind}-{desired_sha}.index'
            if (
                not isinstance(commit, str)
                or not self.provider.HEX_40.fullmatch(commit)
                or not isinstance(expected_sha, str)
                or not self.provider.HEX_64.fullmatch(expected_sha)
                or not isinstance(desired_sha, str)
                or not self.provider.HEX_64.fullmatch(desired_sha)
                or record.get('backingFile') != expected_filename
            ):
                self._fail('operation journal has invalid index-alignment ownership state')
            backing = self._index_alignment_directory(request) / expected_filename
            if self._same_regular_inode(lock_path, backing):
                owned.append((kind, record, backing))
        if len(owned) != 1:
            self._fail(f'Git index is locked by another writer: {lock_path}')
        kind, record, backing = owned[0]
        repo_root = self._repo_root(request)
        if ref_tip(repo_root, 'HEAD') != record['commitSha']:
            self._fail(
                f'journaled {kind} index.lock is not aligned with current HEAD; '
                'automatic recovery is unsafe'
            )
        current_sha = self._index_sha256(repo_root)
        if current_sha not in {
            record['expectedSha256'], record['desiredSha256']
        }:
            self._fail(
                f'real Git index lease was lost while recovering {kind} index.lock'
            )
        if not self._remove_owned_index_lock(lock_path, backing):
            self._fail('journaled index.lock ownership changed during recovery')
        self.checkpoint(f'{kind}_index_lock_recovered')

    def align_index(self, request, commit):
        repo_root = self._repo_root(request)
        if ref_tip(repo_root, 'HEAD') != commit:
            self._fail('cannot align index to a non-current revision')
        journal = self.load_journal(request)
        if journal is None:
            self._fail('index alignment requires an operation journal')
        if commit == journal.get('candidateProductCommitSha'):
            kind = 'product'
            expected_sha = journal['baselineIndexSha256']
        elif commit == journal.get('candidateControlCommitSha'):
            kind = 'control'
            expected_sha = journal.get('productIndexSha256')
        else:
            self._fail('index alignment commit is not owned by this operation')
        if not expected_sha:
            self._fail(f'{kind} index alignment has no predecessor lease')

        desired_payload, desired_sha = self._deterministic_index(
            repo_root,
            commit,
            allowed_worktree_paths=(
                (self.MANIFEST_PRODUCT_PATH,)
                if kind == 'product' and journal.get('projectionRoute') == 'derived'
                else ()
            ),
        )
        current_sha = self._index_sha256(repo_root)
        if current_sha not in {expected_sha, desired_sha}:
            self._fail(
                f'real Git index lease was lost before {kind} alignment: '
                f'expected {expected_sha}, found {current_sha}'
            )

        self.checkpoint(f'before_{kind}_index_lock')
        index_path = self._index_path(repo_root)
        lock_path = Path(f'{index_path}.lock')
        try:
            _, index_metadata = self._read_regular_file(index_path, 'Git index')
            mode = stat.S_IMODE(index_metadata.st_mode)
        except FileNotFoundError:
            self._fail(f'revision provider requires an existing Git index: {index_path}')

        current_sha = self._index_sha256(repo_root)
        if current_sha not in {expected_sha, desired_sha}:
            self._fail(
                f'real Git index lease was lost before {kind} alignment: '
                f'expected {expected_sha}, found {current_sha}'
            )
        record = self._index_alignment_record(
            request, journal, kind, commit, expected_sha, desired_sha
        )
        backing = self._prepare_index_backing(
            request, record, desired_payload, mode
        )

        if current_sha == desired_sha:
            if lock_path.exists() or lock_path.is_symlink():
                if not self._remove_owned_index_lock(lock_path, backing):
                    self._fail(f'Git index is locked by another writer: {lock_path}')
            self._fsync_directory(index_path.parent)
            self._remove_index_backing(backing)
            if git(repo_root, 'write-tree').stdout.strip() != ref_tree(repo_root, commit):
                self._fail('aligned index hash does not produce the current commit tree')
            return desired_sha

        lock_owned = False
        try:
            try:
                os.link(backing, lock_path, follow_symlinks=False)
                lock_owned = True
                self._fsync_directory(lock_path.parent)
            except FileExistsError:
                if not self._same_regular_inode(lock_path, backing):
                    self._fail(f'Git index is locked by another writer: {lock_path}')
                lock_owned = True
            if not self._same_regular_inode(lock_path, backing):
                self._fail('journaled index.lock ownership could not be proven')
            self.checkpoint(f'{kind}_index_lock_owned')
            locked_sha = self._index_sha256(repo_root)
            if locked_sha != expected_sha:
                self._fail(
                    f'real Git index lease was lost before {kind} alignment: '
                    f'expected {expected_sha}, found {locked_sha}'
                )
            final_sha = self._index_sha256(repo_root)
            if final_sha != expected_sha:
                self._fail(
                    f'real Git index changed while {kind} alignment held index.lock'
                )
            os.replace(lock_path, index_path)
            lock_owned = False
            fsync_directory_path(index_path.parent)
            self.checkpoint(f'{kind}_index_cas')
        except BaseException:
            if lock_owned:
                self._remove_owned_index_lock(lock_path, backing)
            raise
        finally:
            if lock_owned:
                self._remove_owned_index_lock(lock_path, backing)
        self._remove_index_backing(backing)
        if self._index_sha256(repo_root) != desired_sha:
            self._fail(f'real Git index did not retain the {kind} replacement bytes')
        if git(repo_root, 'write-tree').stdout.strip() != ref_tree(repo_root, commit):
            self._fail(f'real Git index did not align to the {kind} commit tree')
        return desired_sha

    def _desired_stack(self, request, journal, manifest):
        desired = {
            'id': request.draft_stack_id,
            'branch': request.draft_branch,
            'base': journal.get('projectionBaseSha', journal['baseRefSha']),
            'target_remote': manifest['defaults']['canonical_remote'],
            'target_branch': manifest['defaults']['base_branch'],
            'integration_branch': manifest['integration']['branch'],
            'commits': [journal['productCommitSha']],
            'state': 'draft',
            'publication': {'enabled': False},
            'meta': {
                'purpose': f'Own Agentwheel operation {request.operation_id}',
                'agentwheel_operation_id': request.operation_id,
                'agentwheel_plan_digest': request.plan_digest,
            },
        }
        if journal.get('projectionBaseKind') == 'integration-tip':
            desired['meta']['revision_provider_projection_base'] = {
                'kind': 'integration-tip',
                'sha': journal['projectionBaseSha'],
            }
        return desired

    def _manifest_without_operation_stack(self, manifest, request):
        stripped = copy.deepcopy(manifest)
        stripped['stacks'] = [
            stack for stack in stripped['stacks']
            if stack['id'] != request.draft_stack_id
        ]
        stripped['integration']['stacks'] = [
            stack_id for stack_id in stripped['integration'].get('stacks', [])
            if stack_id != request.draft_stack_id
        ]
        return stripped

    def _assert_draft_branch(self, repo_root, desired, journal):
        journal_tip = ref_tip(repo_root, desired['branch'])
        expected_tip = deterministic_stack_replay_tip(
            repo_root,
            journal.get('projectionBaseSha', journal['baseRefSha']),
            desired['commits'],
        )
        if not expected_tip:
            self._fail('owned draft projection is no longer reproducible')
        if journal_tip != expected_tip:
            self._fail(
                f'draft branch collision: expected {expected_tip}, found {journal_tip}'
            )

    def ensure_stack_owned(self, request, journal):
        repo_root = self._repo_root(request)
        if ref_tip(repo_root, 'HEAD') != journal['productCommitSha']:
            self._fail('product commit is not the current integration HEAD')
        self._assert_ref_leases(
            repo_root,
            self._expected_managed_refs(
                request, journal, journal['productCommitSha'], draft_owned=True
            ),
        )
        manifest, manifest_path = self._manifest(repo_root)
        with manifest_write_transaction(repo_root, manifest_path, 'revision-provider'):
            manifest, _ = self._manifest(repo_root)
            desired = self._desired_stack(request, journal, manifest)
            existing = stack_map(manifest).get(request.draft_stack_id)
            if existing is not None:
                if existing != desired:
                    self._fail(f'draft stack collision: {request.draft_stack_id}')
                stripped = self._manifest_without_operation_stack(manifest, request)
                if manifest_digest(stripped) != journal['observedManifestDigest']:
                    self.expire_manifest_invalidated(
                        request,
                        journal,
                        'manifest contains changes beyond this operation draft stack',
                    )
                if not branch_exists(repo_root, request.draft_branch):
                    self._fail('owned draft stack branch is missing')
                if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
                    self._fail('manifest recovery requires a clean, conflict-free index')
                if self._dirty_paths(repo_root) != {self.MANIFEST_PRODUCT_PATH}:
                    self._fail(
                        'manifest recovery found changes beyond the journaled manifest'
                    )
                self._assert_draft_branch(repo_root, desired, journal)
                context = {
                    'operation_id': request.operation_id,
                    'plan_digest': request.plan_digest,
                    'stack': request.draft_stack_id,
                    'branch': request.draft_branch,
                    'product_commit': journal['productCommitSha'],
                    'paths': sorted(journal['productPathObjects']),
                }
                if not journal.get('manifestReplaced'):
                    journal['manifestDigest'] = manifest_digest(manifest)
                    journal['manifestReplaced'] = True
                    self.save_journal(request, journal)
                    self.checkpoint('manifest_replaced')
                self._ensure_ownership_ledger_event(
                    repo_root, manifest_path, manifest, context, request, journal
                )
                return {'manifestDigest': manifest_digest(manifest)}

            if manifest_digest(manifest) != journal['observedManifestDigest']:
                self.expire_manifest_invalidated(
                    request, journal, 'manifest changed before draft ownership'
                )
            self._ensure_clean(repo_root)
            if any(stack['branch'] == request.draft_branch for stack in manifest['stacks']):
                self._fail(f'draft branch is owned by another stack: {request.draft_branch}')
            if ref_tip(repo_root, request.draft_branch) != journal.get(
                'candidateDraftCommitSha'
            ):
                self._fail('draft ref ownership must be proven before manifest ownership')
            self._assert_draft_branch(repo_root, desired, journal)
            manifest['stacks'].append(desired)
            if request.draft_stack_id not in manifest['integration']['stacks']:
                manifest['integration']['stacks'].append(request.draft_stack_id)
            validate_stack_dependency_graph(
                manifest['stacks'],
                require_declared_dependencies=manifest['version'] == MANIFEST_VERSION_CHANNELS,
            )
            desired_digest = manifest_digest(manifest)
            context = {
                'operation_id': request.operation_id,
                'plan_digest': request.plan_digest,
                'stack': request.draft_stack_id,
                'branch': request.draft_branch,
                'product_commit': journal['productCommitSha'],
                'paths': sorted(journal['productPathObjects']),
            }
            require_manifest_transaction_current(manifest_path)
            save_manifest(manifest_path, manifest)
            self.checkpoint('manifest_replace_written')
            journal['manifestDigest'] = desired_digest
            journal['manifestReplaced'] = True
            self.save_journal(request, journal)
            self.checkpoint('manifest_replaced')
            self._ensure_ownership_ledger_event(
                repo_root, manifest_path, manifest, context, request, journal
            )
            persisted, _ = self._manifest(repo_root)
            return {'manifestDigest': manifest_digest(persisted)}

    def _ensure_ownership_ledger_event(
        self, repo_root, manifest_path, manifest, context, request, journal
    ):
        recover_ledger_tail(repo_root, manifest_path)
        desired_payload = manifest_event_payload(
            manifest_path, manifest, 'revision_provider_stack_ownership', context
        )
        matching = []
        for event in load_ledger_events(repo_root, manifest_path):
            if event.get('type') != 'manifest_saved':
                continue
            payload = event.get('payload') or {}
            event_context = payload.get('context') or {}
            if event_context.get('operation_id') == request.operation_id:
                matching.append(event)
        if matching:
            if len(matching) != 1 or matching[0].get('payload') != desired_payload:
                self._fail(
                    f'ledger collision for revision-provider operation {request.operation_id}'
                )
        resolve_common_derived_provenance(
            repo_root, manifest, sorted(context['paths'])
        )
        if not matching:
            append_ledger_event(
                repo_root, 'manifest_saved', desired_payload, manifest_path
            )
            self.checkpoint('ledger_event_written')
        events = load_ledger_events(repo_root, manifest_path)
        write_ledger_checkpoint(repo_root, reduce_ledger_state(events), manifest_path)
        if not journal.get('ledgerAppended'):
            journal['ledgerAppended'] = True
            self.save_journal(request, journal)
            self.checkpoint('ledger_appended')

    def prepare_control_commit(self, request, message):
        repo_root = self._repo_root(request)
        journal = self.load_journal(request)
        if not journal or ref_tip(repo_root, 'HEAD') != journal.get('productCommitSha'):
            self._fail('control commit requires the product commit at integration HEAD')
        if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
            self._fail('control commit requires a clean, conflict-free index')
        dirty = self._dirty_paths(repo_root)
        if dirty != {self.MANIFEST_PRODUCT_PATH}:
            self._fail(
                'control commit must contain only .syncwheel/manifest.json; found: '
                + ', '.join(sorted(dirty))
            )
        path_objects = self._capture_path_objects(
            repo_root, request, paths=[self.MANIFEST_PRODUCT_PATH]
        )
        prepared = self._prepare_exact_commit(
            repo_root,
            journal['productCommitSha'],
            path_objects,
            message,
        )
        if prepared is None:
            return None
        return {
            'candidateControlCommitSha': prepared['commit'],
            'candidateControlTreeSha': prepared['tree'],
            'controlPathObjects': prepared['pathObjects'],
        }

    def _verify_invariants(self, repo_root, request, journal, expected_head):
        self.verify_recovery_gate(request, journal)
        if ref_tip(repo_root, 'HEAD') != expected_head:
            self._fail('integration HEAD does not match the operation receipt')
        if journal.get('projectionRoute') == 'derived':
            if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
                self._fail('derived revision-provider operation left index changes behind')
            outside = self._dirty_paths(repo_root) - {self.MANIFEST_PRODUCT_PATH}
            if outside:
                self._fail(
                    'derived revision-provider operation left product changes behind: '
                    + ', '.join(sorted(outside))
                )
        else:
            status = git(
                repo_root,
                'status',
                '--porcelain',
                '--untracked-files=all',
                env={'GIT_OPTIONAL_LOCKS': '0'},
            ).stdout
            if status.strip():
                self._fail('revision-provider operation left repository changes behind')
        manifest, _ = self._manifest(repo_root)
        digest = manifest_digest(manifest)
        if (
            journal.get('projectionRoute') != 'derived'
            and digest != journal['manifestDigest']
        ):
            self.expire_manifest_invalidated(
                request,
                journal,
                'manifest digest changed before terminal verification',
            )
        validation = validate_manifest(repo_root, manifest)
        if validation['errors']:
            self._fail('post-operation Syncwheel validation failed: ' + '; '.join(validation['errors']))
        unmapped = list(validation['details']['integration'].get('unmapped_commits') or [])
        if unmapped:
            self._fail('operation left unmapped integration commits: ' + ', '.join(unmapped))
        if self._worktrees(repo_root) != journal['baselineWorktrees']:
            self._fail('operation leaked or removed a Git worktree')
        if self._remote_refs(repo_root) != journal['baselineRemoteRefs']:
            self._fail('operation changed a remote-tracking ref; publication is forbidden')
        if journal.get('productCommitSha'):
            expected_refs = self._expected_managed_refs(
                request, journal, expected_head,
                draft_owned=journal.get('projectionRoute') != 'derived',
            )
        else:
            expected_refs = self._expected_managed_refs(
                request, journal, expected_head, draft_owned=False
            )
        self._assert_ref_leases(repo_root, expected_refs)
        expected_index = (
            (journal.get('controlIndexSha256') or journal.get('productIndexSha256'))
            if journal.get('productCommitSha')
            else journal['baselineIndexSha256']
        )
        if not expected_index:
            self._fail('terminal index lease is missing from the operation journal')
        self._assert_index_lease(repo_root, expected_index, 'terminal verification')
        self.verify_recovery_gate(request, journal)
        return {
            'resultingHead': expected_head,
            'manifestDigest': digest,
            'unmappedIntegrationCommits': unmapped,
        }

    def verify_final(self, request, journal):
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        desired = self._desired_stack(request, journal, manifest)
        if stack_map(manifest).get(request.draft_stack_id) != desired:
            self._fail('operation draft stack ownership is missing or changed')
        if not branch_exists(repo_root, request.draft_branch):
            self._fail('operation draft branch is missing')
        self._assert_draft_branch(repo_root, desired, journal)
        if ref_tip(repo_root, request.draft_branch) != journal.get(
            'candidateDraftCommitSha'
        ):
            self._fail('operation draft branch differs from its journaled object')
        return self._verify_invariants(
            repo_root, request, journal, journal['controlCommitSha']
        )

    def verify_derived_final(self, request, journal):
        repo_root = self._repo_root(request)
        manifest, manifest_path = self._manifest(repo_root)
        self._expire_if_operation_lease_changed(request, journal, manifest)
        if branch_exists(repo_root, request.draft_branch) or request.draft_stack_id in stack_map(manifest):
            self._fail('derived projection must not create a draft ref or stack')
        journal['manifestDigest'] = manifest_digest(manifest)
        payload = {
            'operation_id': request.operation_id,
            'commit': journal['productCommitSha'],
            'paths': sorted(journal['productPathObjects']),
            'paths_digest': journal['derivedContentDigest'],
            'composition_digest': journal['integrationCompositionDigest'],
        }
        matching = [
            event for event in load_ledger_events(repo_root, manifest_path)
            if event.get('type') == 'revision_provider_derived_commit'
            and (event.get('payload') or {}).get('operation_id') == request.operation_id
        ]
        if matching and (len(matching) != 1 or matching[0].get('payload') != payload):
            self._fail(f'ledger collision for derived revision-provider operation {request.operation_id}')
        record_common_derived_provenance(repo_root, manifest, payload)
        if not matching:
            append_ledger_event(repo_root, 'revision_provider_derived_commit', payload, manifest_path)
        return self._verify_invariants(
            repo_root, request, journal, journal['productCommitSha']
        )

    def verify_no_repository_delta(self, request, journal):
        repo_root = self._repo_root(request)
        if self._dirty_paths(repo_root):
            self._fail('no-delta operation still has working tree changes')
        journal['manifestDigest'] = journal['observedManifestDigest']
        return self._verify_invariants(repo_root, request, journal, request.expected_head)

    def verify_release(self, request, journal):
        repo_root = self._repo_root(request)
        observation = self._validate_repository(repo_root, request, require_clean=False)
        if observation['manifestDigest'] != journal['observedManifestDigest']:
            self._fail('cannot release after manifest mutation')
        self._ensure_after_scope(repo_root, request)
        manifest = observation['manifest']
        if request.draft_stack_id in stack_map(manifest) or branch_exists(
            repo_root, request.draft_branch
        ):
            self._fail('cannot release after draft ownership or ref mutation')
        expected_refs = self._expected_managed_refs(
            request, journal, request.expected_head, draft_owned=False
        )
        self._assert_ref_leases(repo_root, expected_refs)
        self._assert_index_lease(
            repo_root, journal['baselineIndexSha256'], 'operation release'
        )

    def checkpoint(self, phase):
        """Fault-injection seam; production intentionally performs no action."""
        return None


def command_revision_provider(args):
    try:
        import syncwheel_revision_provider as provider_module
    except ImportError as exc:
        raise SyncwheelError(f'revision-provider module is not installed: {exc}') from exc
    backend = SyncwheelRevisionBackend(provider_module)
    return provider_module.run_provider_stream(
        backend, sys.stdin, sys.stdout, sys.stderr
    )


def add_rebuild_args(parser):
    parser.add_argument('-w', '--worktree')
    parser.add_argument('-i', '--in-place', action='store_true')
    parser.add_argument(
        '--replay-mode',
        choices=REPLAY_MODE_CHOICES,
        default=None,
        help=(
            'replay execution mode; unset falls back to the repo profile, then the '
            'manifest default, then auto (plumbing when Git supports it, else ephemeral)'
        ),
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
    parser.add_argument(
        '--replay-mode',
        choices=REPLAY_MODE_CHOICES,
        help='replay execution mode for the rebuilds this reconcile applies',
    )
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

    revision_provider_p = sub.add_parser(
        'revision-provider',
        help='serve one strict Agentwheel revision-provider JSON request on stdin',
    )
    revision_provider_p.set_defaults(func=command_revision_provider)

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
    repo_authority_p = repo_sub.add_parser(
        'authority', help='inspect or set the repo-local agent authority policy'
    )
    repo_authority_sub = repo_authority_p.add_subparsers(dest='repo_authority_command', required=True)
    repo_authority_status_p = repo_authority_sub.add_parser('status', parents=[common])
    repo_authority_status_p.add_argument('-j', '--json', action='store_true')
    repo_authority_status_p.set_defaults(func=command_repo_authority_status)
    repo_authority_set_p = repo_authority_sub.add_parser('set', parents=[common])
    repo_authority_set_p.add_argument('mode', choices=sorted(AUTHORITY_MODES))
    repo_authority_set_p.add_argument(
        '--allow', action='append', choices=list(AUTHORITY_GRANTABLE_CLASSES),
        help='authority class agents may exercise without a human gate (repeatable)',
    )
    repo_authority_set_p.add_argument('-a', '--apply', action='store_true')
    repo_authority_set_p.add_argument('-j', '--json', action='store_true')
    repo_authority_set_p.set_defaults(func=command_repo_authority_set)

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

    hooks_p = sub.add_parser('hooks', help='plan, install, or remove managed repository guards')
    hooks_sub = hooks_p.add_subparsers(dest='hooks_command', required=True)
    hooks_status_p = hooks_sub.add_parser('status', parents=[common])
    hooks_status_p.set_defaults(func=command_hooks_status)
    hooks_install_p = hooks_sub.add_parser('install', parents=[common])
    hooks_install_p.add_argument('-a', '--apply', action='store_true')
    hooks_install_p.set_defaults(func=command_hooks_install)
    hooks_remove_p = hooks_sub.add_parser('remove', parents=[common])
    hooks_remove_p.add_argument('-a', '--apply', action='store_true')
    hooks_remove_p.add_argument('--disable', action='store_true', help='persist an explicit clone-local opt-out')
    hooks_remove_p.add_argument('--reason', help='required explanation for --disable')
    hooks_remove_p.set_defaults(func=command_hooks_remove)
    hooks_guard_p = hooks_sub.add_parser('guard', parents=[common], help=argparse.SUPPRESS)
    hooks_guard_p.add_argument('--remote-name', required=True)
    hooks_guard_p.add_argument('--remote-url', required=True)
    hooks_guard_p.set_defaults(func=command_hooks_guard)
    hooks_worktree_guard_p = hooks_sub.add_parser(
        'worktree-guard', parents=[common], help=argparse.SUPPRESS
    )
    hooks_worktree_guard_p.add_argument(
        '--event', required=True, choices=('pre-commit', 'post-checkout')
    )
    hooks_worktree_guard_p.set_defaults(func=command_hooks_worktree_guard)
    hooks_ref_guard_p = hooks_sub.add_parser(
        'ref-guard', parents=[common], help=argparse.SUPPRESS
    )
    hooks_ref_guard_p.add_argument('--phase', default='')
    hooks_ref_guard_p.set_defaults(func=command_hooks_ref_guard)

    use_p = sub.add_parser('use', help='show or set the repo-local default syncwheel profile', parents=[common])
    use_p.add_argument('personal', nargs='?', help='personal profile name to use by default')
    use_p.add_argument('-s', '--shared', action='store_true', help='clear the local profile and use the shared manifest')
    use_p.set_defaults(func=command_use)

    replay_mode_p = sub.add_parser(
        'replay-mode',
        help='show or set the repo-local default replay execution mode',
        parents=[common],
    )
    replay_mode_p.add_argument('mode', nargs='?', choices=REPLAY_MODE_CHOICES)
    replay_mode_p.add_argument(
        '-c', '--clear', action='store_true', help='remove the repo-local default'
    )
    replay_mode_p.add_argument('-j', '--json', action='store_true')
    replay_mode_p.set_defaults(func=command_replay_mode)

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

    coordination_claims_p = coordination_sub.add_parser(
        'claims', help='inspect or backfill per-source coordination claims'
    )
    coordination_claims_sub = coordination_claims_p.add_subparsers(
        dest='coordination_claims_command', required=True
    )
    coordination_claims_backfill_p = coordination_claims_sub.add_parser(
        'backfill', parents=[common],
        help='create missing per-source claim refs with create-only leases',
    )
    coordination_claims_backfill_p.add_argument('-a', '--apply', action='store_true')
    coordination_claims_backfill_p.add_argument('--reason')
    coordination_claims_backfill_p.set_defaults(func=command_coordination_claims_backfill)

    coordination_init_p = coordination_sub.add_parser('init', parents=[common])
    coordination_init_p.add_argument('-R', '--remote', help='configured publication remote for active-active coordination')
    coordination_init_p.add_argument('--coordination-id', help='public coordination-domain id')
    coordination_init_p.add_argument('-a', '--apply', action='store_true')
    coordination_init_p.set_defaults(func=command_coordination_init)

    coordination_disable_p = coordination_sub.add_parser('disable', parents=[common])
    coordination_disable_p.add_argument('-a', '--apply', action='store_true')
    coordination_disable_p.set_defaults(func=command_coordination_disable)

    coordination_provenance_p = coordination_sub.add_parser(
        'provenance',
        help='manage the clone-local derived provenance store under the git common dir',
    )
    coordination_provenance_sub = coordination_provenance_p.add_subparsers(
        dest='coordination_provenance_command', required=True
    )
    coordination_provenance_reset_p = coordination_provenance_sub.add_parser(
        'reset',
        parents=[common],
        help='discard clone-local derived provenance the coordination snapshot supersedes',
    )
    coordination_provenance_reset_p.add_argument(
        '--reason', required=True, help='recorded reason for discarding clone-local records'
    )
    coordination_provenance_reset_p.add_argument(
        '--all',
        action='store_true',
        help='clear the whole store, including an unreadable one',
    )
    coordination_provenance_reset_p.set_defaults(
        func=command_coordination_provenance_reset
    )

    coordination_repair_p = coordination_sub.add_parser(
        'repair',
        parents=[common],
        help='plan or apply a serialized coordination-state repair under an exact proof backend',
    )
    coordination_repair_p.add_argument(
        '--ref',
        required=False,
        help='full already-owned managed branch ref to repair (required while planning)',
    )
    coordination_repair_p.add_argument('-a', '--apply', action='store_true')
    coordination_repair_p.add_argument(
        '--freeze-backend',
        choices=[
            'github-lock',
            COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND,
            COORDINATION_REPAIR_FAST_FORWARD_BACKEND,
        ],
        default='github-lock',
        help=(
            'reviewed repair backend: github-lock remains unsupported; '
            'tree-equivalent-state-cas requires exact tree equality; '
            'fast-forward-state-cas requires exact bounded ancestry; '
            'both change only append-only coordination state'
        ),
    )
    coordination_repair_p.add_argument(
        '--plan-file',
        help='exact reviewed JSON plan; required with --apply',
    )
    coordination_repair_p.set_defaults(func=command_coordination_repair)

    coordination_compose_p = coordination_sub.add_parser(
        'compose',
        parents=[common],
        help='plan or apply an additive remote/local stack composition without rebuilding integration',
    )
    coordination_compose_p.add_argument('--stack', help='single locally added stack to publish')
    coordination_compose_p.add_argument(
        '--known-base-state',
        help='exact append-only coordination state from which the local proposal was derived',
    )
    coordination_compose_p.add_argument(
        '--known-base-snapshot-digest',
        help='exact manifest snapshot digest recorded by --known-base-state',
    )
    coordination_compose_p.add_argument('-a', '--apply', action='store_true')
    coordination_compose_p.add_argument('--plan-file', help='exact reviewed JSON plan')
    coordination_compose_p.set_defaults(func=command_coordination_compose)

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

    worktree_p = sub.add_parser('worktree', aliases=['wt'], help='manage governed local worktrees and safety locks')
    worktree_sub = worktree_p.add_subparsers(dest='worktree_command', required=True)

    worktree_open_p = worktree_sub.add_parser('open', parents=[common])
    worktree_open_p.add_argument('lane')
    worktree_open_p.add_argument('--into', help='optional existing stack that will own this lane\'s commits')
    worktree_open_p.add_argument('--full', action='store_true', help='mark this explicitly requested lane as eligible for dependency, build, test, and debug work')
    worktree_open_p.add_argument('-j', '--json', action='store_true')
    worktree_open_p.set_defaults(func=command_worktree_open)

    worktree_release_p = worktree_sub.add_parser('release', parents=[common])
    worktree_release_p.add_argument('lane')
    worktree_release_p.add_argument('--reason', required=True, help='why this dead or abandoned lane is being released')
    worktree_release_p.add_argument('-a', '--apply', action='store_true', help='create any recovery ref and remove the released lane record')
    worktree_release_p.add_argument('-j', '--json', action='store_true')
    worktree_release_p.set_defaults(func=command_worktree_release)

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
    check_p.add_argument('--strict', action='store_true', help='exit nonzero on warnings or planned actions')
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

    channel_p = sub.add_parser(
        'channel', aliases=['ch'],
        help='manage pinned, ordered, rebuildable deployment-channel branches',
    )
    channel_sub = channel_p.add_subparsers(dest='channel_command', required=True)

    channel_list_p = channel_sub.add_parser('list', aliases=['ls'], parents=[common])
    channel_list_p.add_argument('-j', '--json', action='store_true')
    channel_list_p.set_defaults(func=command_channel_list)

    channel_show_p = channel_sub.add_parser('show', aliases=['sh'], parents=[common])
    channel_show_p.add_argument('channel')
    channel_show_p.set_defaults(func=command_channel_show)

    channel_contract_p = channel_sub.add_parser('contract')
    channel_contract_p.set_defaults(func=command_channel_contract)

    channel_create_p = channel_sub.add_parser('create', aliases=['new'], parents=[common])
    channel_create_p.add_argument('channel')
    channel_create_p.add_argument('-b', '--branch')
    channel_create_p.add_argument('-B', '--base')
    channel_create_p.add_argument('-R', '--remote')
    channel_create_p.add_argument(
        '-L', '--lifecycle', choices=sorted(CHANNEL_LIFECYCLES), default='shared'
    )
    channel_create_p.add_argument('--expires-at')
    channel_create_p.add_argument('-s', '--stack', action='append', default=[])
    channel_create_p.add_argument('--operation-id')
    channel_create_p.add_argument('--plan-digest')
    channel_create_p.add_argument('-a', '--apply', action='store_true')
    channel_create_p.set_defaults(func=command_channel_create)

    channel_add_p = channel_sub.add_parser('add', parents=[common])
    channel_add_p.add_argument('channel')
    channel_add_p.add_argument('stack')
    channel_add_p.add_argument('--position', type=int)
    channel_add_p.add_argument('--operation-id')
    channel_add_p.add_argument('--plan-digest')
    channel_add_p.add_argument('-a', '--apply', action='store_true')
    channel_add_p.set_defaults(func=command_channel_add)

    channel_remove_p = channel_sub.add_parser('remove', aliases=['rm'], parents=[common])
    channel_remove_p.add_argument('channel')
    channel_remove_p.add_argument('stack')
    channel_remove_p.add_argument('--operation-id')
    channel_remove_p.add_argument('--plan-digest')
    channel_remove_p.add_argument('-a', '--apply', action='store_true')
    channel_remove_p.set_defaults(func=command_channel_remove)

    channel_replace_p = channel_sub.add_parser('replace', parents=[common])
    channel_replace_p.add_argument('channel')
    channel_replace_p.add_argument('old_stack')
    channel_replace_p.add_argument('new_stack')
    channel_replace_p.add_argument('--operation-id')
    channel_replace_p.add_argument('--plan-digest')
    channel_replace_p.add_argument('-a', '--apply', action='store_true')
    channel_replace_p.set_defaults(func=command_channel_replace)

    channel_refresh_p = channel_sub.add_parser('refresh', parents=[common])
    channel_refresh_p.add_argument('channel')
    channel_refresh_p.add_argument('-s', '--stack', action='append')
    channel_refresh_p.add_argument('--operation-id')
    channel_refresh_p.add_argument('--plan-digest')
    channel_refresh_p.add_argument('-a', '--apply', action='store_true')
    channel_refresh_p.set_defaults(func=command_channel_refresh)

    channel_diff_p = channel_sub.add_parser('diff', parents=[common])
    channel_diff_p.add_argument('channel')
    channel_diff_p.add_argument('-O', '--other')
    channel_diff_p.set_defaults(func=command_channel_diff)

    channel_promote_p = channel_sub.add_parser('promote', parents=[common])
    channel_promote_p.add_argument('source')
    channel_promote_p.add_argument('target')
    channel_promote_p.add_argument('--operation-id')
    channel_promote_p.add_argument('--plan-digest')
    channel_promote_p.add_argument('-a', '--apply', action='store_true')
    channel_promote_p.set_defaults(func=command_channel_promote)

    channel_resolve_p = channel_sub.add_parser('resolve', parents=[common])
    channel_resolve_p.add_argument('channel')
    channel_resolve_choice = channel_resolve_p.add_mutually_exclusive_group(required=True)
    channel_resolve_choice.add_argument('--revision')
    channel_resolve_choice.add_argument('--clear', action='store_true')
    channel_resolve_p.add_argument('--operation-id')
    channel_resolve_p.add_argument('--plan-digest')
    channel_resolve_p.add_argument('-a', '--apply', action='store_true')
    channel_resolve_p.set_defaults(func=command_channel_resolve)

    channel_plan_p = channel_sub.add_parser('plan', parents=[common])
    channel_plan_p.add_argument('channel')
    channel_plan_p.add_argument('--operation', choices=('apply', 'publish'), default='apply')
    channel_plan_p.add_argument('--operation-id')
    channel_plan_p.set_defaults(func=command_channel_plan)

    channel_apply_p = channel_sub.add_parser('apply', parents=[common])
    channel_apply_p.add_argument('channel')
    channel_apply_p.add_argument('--operation-id')
    channel_apply_p.add_argument('--plan-digest')
    channel_apply_p.add_argument('-a', '--apply', action='store_true')
    channel_apply_p.set_defaults(func=command_channel_apply)

    channel_publish_p = channel_sub.add_parser('publish', parents=[common])
    channel_publish_p.add_argument('channel')
    channel_publish_p.add_argument('--operation-id')
    channel_publish_p.add_argument('--plan-digest')
    channel_publish_p.add_argument('-a', '--apply', action='store_true')
    channel_publish_p.set_defaults(func=command_channel_publish)

    channel_close_p = channel_sub.add_parser('close', parents=[common])
    channel_close_p.add_argument('channel')
    channel_close_p.add_argument('-R', '--reason', default='closed')
    channel_close_p.add_argument('--delete-local', action='store_true')
    channel_close_p.add_argument('--operation-id')
    channel_close_p.add_argument('--plan-digest')
    channel_close_p.add_argument('-a', '--apply', action='store_true')
    channel_close_p.set_defaults(func=command_channel_close)

    channel_operation_p = channel_sub.add_parser('operation')
    channel_operation_sub = channel_operation_p.add_subparsers(
        dest='channel_operation_command', required=True
    )
    channel_operation_list_p = channel_operation_sub.add_parser('list', parents=[common])
    channel_operation_list_p.add_argument('--channel')
    channel_operation_list_p.add_argument(
        '--status', choices=(
            'pending', 'prepared', 'succeeded', 'failed', 'partial', 'unknown',
            'cancelled',
        )
    )
    channel_operation_list_p.set_defaults(func=command_channel_operation_list)
    channel_operation_show_p = channel_operation_sub.add_parser('show', parents=[common])
    channel_operation_show_p.add_argument('operation_id')
    channel_operation_show_p.set_defaults(func=command_channel_operation_show)
    channel_operation_reconcile_p = channel_operation_sub.add_parser(
        'reconcile', parents=[common]
    )
    channel_operation_reconcile_p.add_argument('operation_id')
    channel_operation_reconcile_p.add_argument('--plan-digest')
    channel_operation_reconcile_p.add_argument('-a', '--apply', action='store_true')
    channel_operation_reconcile_p.set_defaults(func=command_channel_reconcile_outcome)

    channel_receipt_p = channel_sub.add_parser('receipt')
    channel_receipt_sub = channel_receipt_p.add_subparsers(
        dest='channel_receipt_command', required=True
    )
    channel_receipt_show_p = channel_receipt_sub.add_parser('show', parents=[common])
    channel_receipt_show_p.add_argument('channel', nargs='?')
    channel_receipt_show_p.set_defaults(func=command_channel_receipt_show)

    channel_reconcile_p = channel_sub.add_parser('reconcile-outcome', parents=[common])
    channel_reconcile_p.add_argument('operation_id')
    channel_reconcile_p.add_argument('--plan-digest')
    channel_reconcile_p.add_argument('-a', '--apply', action='store_true')
    channel_reconcile_p.set_defaults(func=command_channel_reconcile_outcome)

    journal_p = sub.add_parser(
        'journal', help='inspect, snapshot, publish, or schedule a journal repository'
    )
    journal_sub = journal_p.add_subparsers(dest='journal_command', required=True)
    journal_status_p = journal_sub.add_parser('status', parents=[common])
    journal_status_p.add_argument('-j', '--json', action='store_true')
    journal_status_p.set_defaults(func=command_journal_status)
    journal_snapshot_p = journal_sub.add_parser('snapshot', parents=[common])
    journal_snapshot_p.add_argument('-a', '--apply', action='store_true')
    journal_snapshot_p.set_defaults(func=command_journal_snapshot)
    journal_publish_p = journal_sub.add_parser('publish', parents=[common])
    journal_publish_p.add_argument('-a', '--apply', action='store_true')
    journal_publish_p.set_defaults(func=command_journal_publish)
    journal_schedule_p = journal_sub.add_parser('schedule', parents=[common])
    journal_schedule_p.add_argument('schedule_command', choices=('install', 'status', 'remove'))
    journal_schedule_p.add_argument('-a', '--apply', action='store_true')
    journal_schedule_p.set_defaults(func=command_journal_schedule)

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
        '--depends-on', action='append', default=[],
        help='declare one prerequisite stack; may be repeated',
    )
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

    stack_classify_p = stack_sub.add_parser(
        'classify-integration',
        parents=[common],
        help='declare integration-only stack ownership without rebuilding refs',
    )
    stack_classify_p.add_argument('stack')
    stack_classify_p.add_argument('specs', nargs='+')
    stack_classify_p.add_argument('--plan-digest')
    stack_classify_p.add_argument('-a', '--apply', action='store_true')
    stack_classify_p.set_defaults(func=command_stack_classify_integration)

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

    stack_land_p = stack_sub.add_parser(
        'land', parents=[common],
        help='plan or safely direct-land a validated stack without creating a PR',
    )
    stack_land_p.add_argument('stack')
    stack_land_p.add_argument('--allow-direct', action='store_true')
    stack_land_p.add_argument('--attestation', action='append', default=[], metavar='ID=PATH')
    stack_land_p.add_argument('--override-requirement', action='append', default=[], metavar='ID')
    stack_land_p.add_argument('--override-group', action='append', default=[], metavar='ID')
    stack_land_p.add_argument('--override-all-checks', action='store_true')
    stack_land_p.add_argument('--override-reason')
    stack_land_p.add_argument('--operation-id')
    stack_land_p.add_argument('--plan-digest')
    stack_land_p.add_argument('-a', '--apply', action='store_true')
    stack_land_p.add_argument('-j', '--json', action='store_true')
    stack_land_p.set_defaults(func=command_stack_land)

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
    int_rebuild_p.add_argument(
        '--reason',
        help='required explanation when reconciling derived_paths narrowing',
    )
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


def command_hooks_status(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    print(json.dumps(managed_push_guard_policy(repo_root, manifest), indent=2, sort_keys=True))
    return 0


def command_hooks_install(args):
    repo_root = resolve_repo_root(args.repo)
    require_manifest(repo_root, args.repo, args.manifest, args.personal)
    print(json.dumps(install_managed_push_hook(repo_root, apply=args.apply), indent=2, sort_keys=True))
    return 0


def command_hooks_remove(args):
    repo_root = resolve_repo_root(args.repo)
    require_manifest(repo_root, args.repo, args.manifest, args.personal)
    print(json.dumps(remove_managed_push_hook(
        repo_root, apply=args.apply, disable=args.disable, reason=args.reason
    ), indent=2, sort_keys=True))
    return 0


def command_hooks_guard(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    updates = parse_pre_push_updates(sys.stdin)
    destinations = sorted({item['remoteRef'] for item in updates})
    protected = sorted(set(destinations).intersection(managed_push_refs(repo_root, manifest)))
    if not protected:
        return 0
    if verify_managed_push_authorization(repo_root, args.remote_name, destinations):
        return 0
    remedies = []
    integration_ref = f"refs/heads/{manifest['integration']['branch']}"
    state_ref = coordination_state_ref(coordination_config(manifest)) if coordination_config(manifest) else None
    stack_refs = {
        f"refs/heads/{stack['branch']}"
        for stack in manifest.get('stacks', [])
        if stack.get('branch')
    }
    delivery_refs = set(delivery_ref_names(manifest))
    for ref in protected:
        if ref == integration_ref:
            remedies.append('syncwheel int push')
        elif ref in delivery_refs:
            remedies.append('syncwheel stack land <stack> (or a pull request)')
        elif ref == state_ref:
            remedies.append('syncwheel publish (or the reviewed coordination repair workflow)')
        elif manifest.get('repository_mode') == 'journal' and ref == f"refs/heads/{manifest['journal']['branch']}":
            remedies.append('syncwheel journal publish --apply')
        elif any(ref == f"refs/heads/{channel['branch']}" for channel in manifest.get('channels', [])):
            remedies.append('syncwheel channel publish <channel> --apply --plan-file <reviewed-plan>')
        elif ref in stack_refs:
            remedies.append('syncwheel stack push <stack>')
        else:
            remedies.append(
                'syncwheel handoff, then use the reviewed historical-ref adoption/closure workflow'
            )
    raise SyncwheelError(
        'raw git push blocked for Syncwheel-managed ref(s): '
        + ', '.join(protected)
        + '. Use: ' + '; '.join(dict.fromkeys(remedies))
        + '. This local hook is a safety guard, not a security boundary; git push --no-verify bypasses it.'
    )


def command_hooks_worktree_guard(args):
    repo_root = resolve_repo_root(args.repo)
    worktrees = get_worktrees(repo_root)
    primary_root = Path(worktrees[0]['path']).resolve() if worktrees else repo_root
    manifest_path = resolve_manifest_path(
        primary_root, str(primary_root), args.manifest, args.personal
    )
    manifest, _ = load_manifest(primary_root, manifest_path)
    if manifest is None:
        return 0
    primary = primary_checkout_state(primary_root, manifest)
    current_path = Path(git(repo_root, 'rev-parse', '--show-toplevel').stdout.strip()).resolve()
    primary_path = Path(primary['path']).resolve() if primary.get('path') else None
    if primary_path is None or current_path != primary_path or primary['compliant']:
        return 0
    action = 'commit blocked' if args.event == 'pre-commit' else 'branch mismatch detected after checkout'
    raise SyncwheelError(
        f'primary checkout {action}: expected {primary["expected_branch"]!r}, '
        f'found {primary["branch"]!r} at {primary["path"]}. '
        'Keep the primary unchanged'
        + format_remedy_suffix(primary_checkout_remedy_commands(manifest))
        + '. '
        'restore the primary checkout losslessly before continuing. '
        'This local hook is a safety guard, not a security boundary; --no-verify can bypass it.'
    )


def command_hooks_ref_guard(args):
    # Git runs this for every ref transaction. Only the "prepared" phase can
    # veto, and only a rewind of a managed branch is worth vetoing: everything
    # else must stay out of the way of ordinary Git use.
    if args.phase != 'prepared':
        return 0
    updates = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) == 3:
            updates.append(parts)
    if not updates:
        return 0
    repo_root = resolve_repo_root(args.repo)
    worktrees = get_worktrees(repo_root)
    primary_root = Path(worktrees[0]['path']).resolve() if worktrees else repo_root
    manifest_path = resolve_manifest_path(
        primary_root, str(primary_root), args.manifest, args.personal
    )
    manifest, _ = load_manifest(primary_root, manifest_path)
    if manifest is None:
        return 0
    managed = set(managed_ref_names(manifest))
    if not managed:
        return 0
    rewinds = []
    for old, new, ref in updates:
        if ref not in managed:
            continue
        if set(new) == {'0'}:
            # Deletion is a lifecycle decision the branch commands already own.
            continue
        if set(old) == {'0'}:
            # "git branch -f" reports a zero old value, so a rewind looks exactly
            # like a creation. The prepared phase runs before the ref moves, so
            # read what the ref still points at and trust that instead.
            current = git(
                repo_root, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}',
                check=False,
            )
            old = current.stdout.strip()
            if current.returncode != 0 or not old:
                continue
        ancestor = git(repo_root, 'merge-base', '--is-ancestor', old, new, check=False)
        if ancestor.returncode != 0:
            rewinds.append((ref, old, new))
    if not rewinds:
        return 0
    if os.environ.get(MANAGED_REF_MOVE_AUTH_ENV) == '1':
        return 0
    detail = ', '.join(f'{ref} {old[:7]} -> {new[:7]}' for ref, old, new in rewinds)
    raise SyncwheelError(
        f'refusing to rewind managed ref(s): {detail}. '
        'The new tip does not contain the current one, so committed work would '
        'stop being reachable from this branch. Use the Syncwheel command that '
        'owns this branch (for example "reconcile --apply" or "stack rebuild"), '
        'or move to the intended ref instead. '
        'This local hook is a safety guard, not a security boundary; '
        'core.hooksPath and --no-verify can bypass it.'
    )


def command_self_mode(args):
    if not args.mode:
        settings = load_update_settings()
        print(settings['mode'])
        return 0
    path = set_update_mode(args.mode)
    print(f'{args.mode}\n{path}')
    return 0


def manifest_mutation_requested(args):
    always = {
        command_stack_close,
        command_stack_create,
        command_stack_promote,
        command_stack_demote,
        command_stack_sync,
        command_stack_absorb,
        command_stack_set,
        command_stack_resolve_integration,
        command_stack_add,
        command_stack_capture_integration,
    }
    if args.func in always:
        return True
    if args.func == command_init:
        return not getattr(args, 'stdout', False)
    if args.func in {
        command_coordination_init,
        command_coordination_disable,
        command_manifest_require_integration,
        command_repo_tracking_set,
    }:
        return bool(getattr(args, 'apply', False))
    if args.func in {command_reconcile, command_resume, command_sync, command_publish}:
        return bool(getattr(args, 'apply', False))
    if args.func == command_stack_rebuild:
        return bool(getattr(args, 'update_manifest', False))
    if args.func == command_stack_land:
        return bool(getattr(args, 'apply', False))
    if args.func in {
        command_channel_create,
        command_channel_add,
        command_channel_remove,
        command_channel_replace,
        command_channel_refresh,
        command_channel_promote,
        command_channel_resolve,
        command_channel_apply,
        command_channel_publish,
        command_channel_close,
        command_channel_reconcile_outcome,
    }:
        return bool(getattr(args, 'apply', False))
    return False


def default_hook_convergence_requested(args):
    if not hasattr(args, 'repo'):
        return False
    if args.func in {
        command_init,
        command_hooks_status,
        command_hooks_install,
        command_hooks_remove,
        command_hooks_guard,
        command_hooks_worktree_guard,
        command_hooks_ref_guard,
    }:
        return False
    if args.func == command_repo_tracking_set and bool(getattr(args, 'apply', False)):
        return False
    return True


def converge_default_repository_hooks(args):
    if not default_hook_convergence_requested(args):
        return None
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(
        repo_root, args.repo, getattr(args, 'manifest', None), getattr(args, 'personal', None)
    )
    if not manifest_path.exists():
        return None
    manifest, _ = load_manifest(repo_root, manifest_path)
    if manifest is None:
        return None
    return ensure_managed_repository_hooks(repo_root, manifest)


def governed_worktree_reaping_requested(args):
    always_mutating = {
        command_worktree_open, command_worktree_lock, command_worktree_unlock,
        command_sync, command_publish,
        command_stack_absorb, command_stack_add, command_stack_capture_integration,
        command_stack_close, command_stack_create,
        command_stack_demote, command_stack_promote,
        command_stack_resolve_integration, command_stack_set, command_stack_sync,
    }
    apply_gated = {
        command_reconcile,
        command_resume,
        command_stack_classify_integration,
        command_stack_land,
    }
    dry_run_gated = {
        command_stack_push,
        command_stack_rebuild,
        command_int_align_remote,
        command_int_push,
        command_int_rebuild,
    }
    if args.func in always_mutating:
        return True
    if args.func in apply_gated:
        return bool(getattr(args, 'apply', False))
    if args.func in dry_run_gated:
        return not bool(getattr(args, 'dry_run', False))
    if args.func in {command_stack_git, command_int_git}:
        return bool(getattr(args, 'auto_worktree', False) or getattr(args, 'worktree', None))
    return False


def governed_worktree_preflight(args):
    if not hasattr(args, 'repo'):
        return
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(
        repo_root, args.repo, getattr(args, 'manifest', None), getattr(args, 'personal', None)
    )
    manifest, _ = load_manifest(repo_root, manifest_path)
    if manifest is None:
        return
    emit_governed_worktree_warnings(repo_root, manifest, json_mode=bool(getattr(args, 'json', False)))
    if not governed_worktree_reaping_requested(args):
        return
    cleanup = reconcile_governed_worktrees(repo_root, manifest, manifest_path)
    if cleanup['failures']:
        details = ', '.join(
            f"{item['id']} [{item['code']}]" for item in cleanup['failures']
        )
        raise SyncwheelError(
            'governed worktree recovery is required before this mutation: '
            + details
            + '; cleanup failed closed, retry the named remedy'
        )
    dangerous = {
        command_stack_rebuild, command_stack_push, command_int_push, command_reconcile,
        command_resume, command_sync, command_publish, command_stack_land, command_stack_close,
    }
    if args.func not in dangerous:
        return
    blocked = [
        lane for lane in governed_worktree_diagnostics(repo_root, manifest)['lanes']
        if lane['code'] in {
            'dirty', 'outside_root', 'unregistered_worktree', 'unavailable',
            'invalid_lease', 'current_directory', 'branch_delete_failed',
            'branch_advanced', 'locked', 'worktree_remove_failed', 'reaping',
            'ledger_pending', 'recovery_ref_moved', 'lane_in_use',
            'registration_mismatch', 'path_reappeared',
        }
    ]
    if blocked:
        labels = ', '.join(item['id'] or item['branch'] or item['path'] for item in blocked)
        raise SyncwheelError(
            'governed worktree recovery is required before this mutation: ' + labels
        )


def execute_parsed_command(args):
    if args.command in JOURNAL_FORBIDDEN_COMMANDS and hasattr(args, 'repo'):
        repo_root = resolve_repo_root(args.repo)
        manifest_path = resolve_manifest_path(
            repo_root, args.repo, getattr(args, 'manifest', None), getattr(args, 'personal', None)
        )
        manifest, _ = load_manifest(repo_root, manifest_path)
        if manifest and manifest.get('repository_mode') == 'journal':
            raise SyncwheelError(
                f'{args.command} is forbidden for repository_mode="journal"; use journal commands'
            )
    guarded_publishers = {
        command_stack_push, command_int_push, command_journal_publish,
        command_channel_publish, command_reconcile, command_resume,
        command_sync, command_publish, command_stack_land,
    }
    if args.func in guarded_publishers and hasattr(args, 'repo'):
        mutating = (
            args.func in {command_stack_push, command_int_push}
            and not getattr(args, 'dry_run', False)
        ) or (
            args.func not in {command_stack_push, command_int_push}
            and bool(getattr(args, 'apply', False))
        )
        if mutating:
            repo_root = resolve_repo_root(args.repo)
            manifest, _ = require_manifest(
                repo_root, args.repo, getattr(args, 'manifest', None), getattr(args, 'personal', None)
            )
            ensure_managed_repository_hooks(repo_root, manifest)
    return args.func(args)


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
    # Syncwheel owns the managed branches, so its own child Git processes are
    # allowed to rewind them. The guard exists to stop every other caller.
    if args.func not in {
        command_hooks_guard,
        command_hooks_worktree_guard,
        command_hooks_ref_guard,
    }:
        global SYNCWHEEL_OWNS_REF_MOVES
        SYNCWHEEL_OWNS_REF_MOVES = True
    try:
        if (
            args.command != 'revision-provider'
            and args.func not in {
                command_hooks_guard,
                command_hooks_worktree_guard,
                command_hooks_ref_guard,
            }
        ):
            maybe_handle_startup_update_policy(args)
        if args.command != 'revision-provider':
            governed_worktree_preflight(args)
            converge_default_repository_hooks(args)
        if manifest_mutation_requested(args) and hasattr(args, 'repo'):
            repo_root = resolve_repo_root(args.repo)
            manifest_path = resolve_manifest_path(
                repo_root, args.repo, getattr(args, 'manifest', None), getattr(args, 'personal', None)
            )
            if args.func != command_init and manifest_path.exists():
                existing_manifest, _ = load_manifest(repo_root, manifest_path)
                if existing_manifest:
                    ensure_managed_repository_hooks(repo_root, existing_manifest)
            with manifest_write_transaction(repo_root, manifest_path, 'manifest-command'):
                return execute_parsed_command(args)
        return execute_parsed_command(args)
    except SyncwheelError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
