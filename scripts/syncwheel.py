#!/usr/bin/env python3
import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


class SyncwheelError(Exception):
    pass


ENV_REGISTRY_PATH = 'SYNCWHEEL_REPO_REGISTRY'
INTEGRATION_STRATEGIES = {'cherry-pick', 'merge-stacks'}
VERSION = '0.2.0'


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


def resolve_manifest_path(repo_root, repo_value=None, manifest_override=None):
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
    integration.setdefault('branch', 'integration/main')
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


def require_manifest(repo_root, repo_value=None, manifest_override=None):
    manifest_path = resolve_manifest_path(repo_root, repo_value, manifest_override)
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


def default_worktree_path(repo_root, branch):
    safe = branch.replace('/', '-').replace('\\', '-')
    return repo_root.parent / f'{repo_root.name}-wt-{safe}'


def find_worktree_for_branch(repo_root, branch):
    for worktree in get_worktrees(repo_root):
        if worktree.get('branch') == branch:
            return Path(worktree['path'])
    return None


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
            if item['branch_exists'] and not branch_contains(repo_root, stack['branch'], commit):
                item['missing_from_branch'].append(commit)
            if integration_exists and not branch_contains(repo_root, integration_branch, commit):
                item['missing_from_integration'].append(commit)
        details['stacks'].append(item)

    details['integration'] = {
        'branch': integration_branch,
        'exists': integration_exists,
        'base': integration['base'],
        'strategy': integration_strategy,
        'stacks': integration.get('stacks', []),
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
    manifest = {
        'version': 1,
        'defaults': {
            'canonical_remote': canonical_remote,
            'publication_remote': publication_remote,
            'base_branch': base_branch,
            'base_ref': f'{canonical_remote}/{base_branch}',
        },
        'integration': {
            'branch': args.integration_branch,
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
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists() and not args.force:
        raise SyncwheelError(f'manifest already exists: {manifest_path}')
    manifest_path.write_text(output)
    print(manifest_path)
    return 0


def command_status(args):
    repo_root = resolve_repo_root(args.repo)
    if args.fetch:
        git(repo_root, 'fetch', '--all', '--prune', '--quiet', check=False)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest)
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
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest)
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
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest)
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


def command_stack_list(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest)
    for stack in manifest['stacks']:
        print(f"{stack['id']}\t{stack['branch']}\tcommits={len(stack['commits'])}")
    return 0


def command_stack_show(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest)
    stack = require_stack(manifest, args.stack)
    print(json.dumps(stack, indent=2))
    return 0


def command_stack_sync(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest)
    stack = require_stack(manifest, args.stack)
    commits = rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
    stack['commits'] = commits
    save_manifest(manifest_path, manifest)
    print(f"{args.stack}: synced {len(commits)} commits from {stack['branch']}")
    return 0


def command_stack_set(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest)
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
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest)
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
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest)
    stack = require_stack(manifest, args.stack)
    worktree, in_place = resolve_stack_rebuild_location(repo_root, stack, args)
    if not args.dry_run and in_place:
        ensure_in_place_target(repo_root, stack['branch'])
    commands = materialize_pr_commands(repo_root, manifest, stack, worktree, in_place)
    run_command_list(commands, repo_root, not args.dry_run)
    return 0


def command_stack_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest)
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
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest)
    stack = require_stack(manifest, args.stack)
    worktree = find_worktree_for_branch(repo_root, stack['branch'])
    if not worktree:
        raise SyncwheelError(f"no worktree found for stack branch: {stack['branch']}")
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
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest)
    print(json.dumps(manifest['integration'], indent=2))
    return 0


def command_int_rebuild(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest)
    worktree, in_place = resolve_int_rebuild_location(repo_root, manifest, args)
    if not args.dry_run and in_place:
        ensure_in_place_target(repo_root, manifest['integration']['branch'])
    commands = materialize_integration_commands(repo_root, manifest, worktree, in_place)
    run_command_list(commands, repo_root, not args.dry_run)
    return 0


def command_int_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest)
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
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest)
    branch = manifest['integration']['branch']
    worktree = find_worktree_for_branch(repo_root, branch)
    if not worktree:
        raise SyncwheelError(f'no worktree found for integration branch: {branch}')
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
    return parser
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description='Deterministic syncwheel helper for fork/upstream/integration repos.')
    parser.add_argument('--version', action='version', version=f'syncwheel {VERSION}')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('-r', '--repo', help='target repo path or registered alias')
    common.add_argument('--manifest', help='path to a syncwheel manifest JSON file')
    sub = parser.add_subparsers(dest='command', required=True)

    repo_p = sub.add_parser('repo', help='manage repo aliases')
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

    init_p = sub.add_parser('init', help='create a starter manifest', parents=[common])
    init_p.add_argument('--canonical-remote', default='origin')
    init_p.add_argument('--publication-remote', default='fork')
    init_p.add_argument('--base-branch', default='main')
    init_p.add_argument('--integration-branch', default='integration/main')
    init_p.add_argument('--force', action='store_true')
    init_p.add_argument('--stdout', action='store_true')
    init_p.set_defaults(func=command_init)

    status_p = sub.add_parser('status', help='show repo and manifest state', parents=[common])
    status_p.add_argument('--fetch', action='store_true')
    status_p.add_argument('--json', action='store_true')
    status_p.set_defaults(func=command_status)

    validate_p = sub.add_parser('validate', help='validate the manifest against local git state', parents=[common])
    validate_p.add_argument('--json', action='store_true')
    validate_p.set_defaults(func=command_validate)

    plan_p = sub.add_parser('plan', help='emit a deterministic action plan from the manifest', parents=[common])
    plan_p.add_argument('--json', action='store_true')
    plan_p.set_defaults(func=command_plan)

    stack_p = sub.add_parser('stack', help='inspect, edit, rebuild, push, or run git for one stack')
    stack_sub = stack_p.add_subparsers(dest='stack_command', required=True)

    stack_list_p = stack_sub.add_parser('list', parents=[common])
    stack_list_p.set_defaults(func=command_stack_list)

    stack_show_p = stack_sub.add_parser('show', parents=[common])
    stack_show_p.add_argument('stack')
    stack_show_p.set_defaults(func=command_stack_show)

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

    stack_rebuild_p = stack_sub.add_parser('rebuild', parents=[common])
    stack_rebuild_p.add_argument('stack')
    add_rebuild_args(stack_rebuild_p)
    stack_rebuild_p.set_defaults(func=command_stack_rebuild)

    stack_push_p = stack_sub.add_parser('push', parents=[common])
    stack_push_p.add_argument('stack')
    add_push_args(stack_push_p)
    stack_push_p.set_defaults(func=command_stack_push)

    stack_git_p = stack_sub.add_parser('git', parents=[common])
    stack_git_p.add_argument('stack')
    add_git_args(stack_git_p)
    stack_git_p.set_defaults(func=command_stack_git)

    int_p = sub.add_parser('int', help='inspect, rebuild, push, or run git for integration')
    int_sub = int_p.add_subparsers(dest='int_command', required=True)

    int_show_p = int_sub.add_parser('show', parents=[common])
    int_show_p.set_defaults(func=command_int_show)

    int_rebuild_p = int_sub.add_parser('rebuild', parents=[common])
    add_rebuild_args(int_rebuild_p)
    int_rebuild_p.set_defaults(func=command_int_rebuild)

    int_push_p = int_sub.add_parser('push', parents=[common])
    add_push_args(int_push_p)
    int_push_p.set_defaults(func=command_int_push)

    int_git_p = int_sub.add_parser('git', parents=[common])
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
        return args.func(args)
    except SyncwheelError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
