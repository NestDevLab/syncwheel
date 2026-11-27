"""Replay selection, planning, execution and product projection.

Git transport, checkout leases and reviewed resolution queries are separate
explicit ports. No replay operation reaches into the CLI namespace.
"""

import datetime
import os
from pathlib import Path
import re
import shlex
import sys
import contextlib
import tempfile
import shutil
from typing import Protocol

from syncwheel_errors import SyncwheelError
from syncwheel_contracts import PROFILE_FILENAME, ZERO_OBJECT_ID, syncwheel_timestamp
from syncwheel_manifest import (
    REPLAY_MODE_CHOICES, normalize_replay_mode, stack_map,
    stack_integration_base_commits, stack_integration_only_commits, validate_stack_adoption,
)


def normalize_integration_desk_step_phases(manifest, receipt):
    """Recover missing legacy phase tags only from unambiguous frozen ownership.

    Completed steps may have been replaced by earlier captures. Inspect only the
    pending suffix, whose source declarations are still bound by the desk lease.
    """
    stacks = stack_map(manifest)
    for step in receipt['steps'][receipt['next']:]:
        if 'integration_only' in step:
            continue
        argv = step.get('argv') or []
        stack = stacks.get(step.get('stack_id'))
        if stack is None or 'cherry-pick' not in argv:
            continue
        commit = argv[-1]
        if commit not in stack_integration_only_commits(stack):
            continue
        if commit in stack_integration_base_commits(stack):
            raise SyncwheelError('legacy integration desk phase is ambiguous; preserve and review the desk')
        step['integration_only'] = True


def integration_desk_capture_parent(receipt, stack_id):
    step = receipt['steps'][receipt['next']]
    return receipt['head'] if step.get('integration_only') else receipt['stack_bases'][stack_id]


EPHEMERAL_WORKTREE_PLACEHOLDER = '<syncwheel-ephemeral-worktree>'

EMPTY_REPLAY_MESSAGE = (
    'The previous cherry-pick is now empty, possibly due to conflict resolution.\n'
    'If you wish to commit it anyway, use:\n\n'
    '    git commit --allow-empty\n\n'
    "Otherwise, please use 'git cherry-pick --skip'"
)


def replay_hygiene_env():
    return {
        'GIT_CONFIG_COUNT': '2',
        'GIT_CONFIG_KEY_0': 'rerere.enabled',
        'GIT_CONFIG_VALUE_0': 'false',
        'GIT_CONFIG_KEY_1': 'commit.gpgsign',
        'GIT_CONFIG_VALUE_1': 'false',
    }


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


def integration_supports_plumbing(manifest):
    return manifest['integration'].get('strategy', 'cherry-pick') == 'cherry-pick'


def require_nonempty_desk_stack_rebuild(stack, mode):
    """Reject desk rebuilds before an empty stack can materialize a branch."""
    if mode != 'desk' or stack['commits']:
        return
    raise SyncwheelError(
        f"stack {stack['id']!r} has no declared commits; cannot rebuild it with replay mode desk. "
        'Author on the integration branch, then capture the integration commit(s) into the stack.'
    )


def replay_step(kind, argv=None, env=None, render=None):
    return {
        'kind': kind,
        'argv': argv,
        'env': env,
        'render': render,
    }


def replay_exec_step(argv, env=None, stack_id=None):
    step = replay_step('exec', argv=argv, env=env, render=quoted(argv))
    if stack_id is not None:
        step['stack_id'] = stack_id
    return step


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


def quoted(parts):
    return ' '.join(shlex.quote(part) for part in parts)


def quoted_with_env(env, argv):
    assignments = [] if not env else [f'{key}={shlex.quote(value)}' for key, value in env.items()]
    return ' '.join([*assignments, quoted(argv)])


def git_command_cwd(default_cwd, command):
    if not command or command[0] != 'git':
        return Path(default_cwd)
    index = 1
    while index < len(command):
        if command[index] == '-C' and index + 1 < len(command):
            return Path(command[index + 1])
        index += 1
    return Path(default_cwd)


def shell_ref(reference):
    if reference.startswith('$'):
        return f'"{reference}"'
    return shlex.quote(reference)


def empty_replay_shell_error():
    lines = []
    for line in EMPTY_REPLAY_MESSAGE.splitlines():
        lines.append(f"  printf '%s\\n' {shlex.quote(line)} >&2")
    return lines


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
    return (
        'syncwheel int rebuild --replay-mode desk '
        '--reason "resolve reviewed integration replay conflict"'
    )


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


def replay_target(
    stack=None,
    integration=None,
    worktree=None,
    return_tree=False,
    skip_contained=False,
    stack_ref_overrides=None,
    expected_tip=None,
):
    # Projections skip contained commits; rebuilds replay their declared history.
    if stack is not None:
        return {
            'kind': 'stack',
            'stack_id': stack['id'],
            'stack': stack,
            'expected_tip': expected_tip,
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
        'expected_tip': expected_tip,
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




def backup_branch_name(branch, timestamp):
    return f'backup/{branch}-before-syncwheel-{timestamp}'


class ReplayRepository(Protocol):
    root: Path

    def git(self, repo_root, *args, **kwargs): ...
    def run(self, command, **kwargs): ...
    def shell(self, command, **kwargs): ...
    def commit_identity_env(self, repo_root, commit): ...
    def commit_full_sha(self, repo_root, ref): ...
    def commit_short_sha(self, repo_root, ref): ...
    def branch_contains(self, repo_root, branch, commit): ...
    def branch_exists(self, repo_root, branch): ...
    def ref_tip(self, repo_root, ref): ...
    def ref_tree(self, repo_root, ref): ...
    def commit_changed_files(self, repo_root, commit, limit=None): ...
    def get_current_branch(self, repo_root): ...
    def load_repo_profile(self, repo_root): ...
    def find_worktree_for_branch(self, repo_root, branch): ...
    def with_git_identity(self, repo_root, command): ...
    def managed_process_env(self, extra=None, authorize=True): ...


class ReplayProjection(Protocol):
    def merge_stack_resolution_commits(self, repo_root, stack, stack_ref): ...
    def stack_content_absorbed_at(self, repo_root, stack, tip): ...


class ReplayCheckoutLeases(Protocol):
    def checkout_reset_destination_lease(self, repo_root, branch, **kwargs): ...
    def checkout_source_lease(self, repo_root, manifest_path): ...


class ReplayEngine:
    def __init__(self, repository: ReplayRepository, projection: ReplayProjection,
                 checkout: ReplayCheckoutLeases):
        self.repository = repository
        self.projection = projection
        self.checkout = checkout

    def replay_commit_env(self, commit):
        repo_root = self.repository.root
        env = replay_hygiene_env()
        env.update(self.repository.commit_identity_env(repo_root, commit))
        return env

    def deterministic_stack_replay_tip(self, base, commits):
        """Return the exact tip produced by replaying commits onto base, or None."""
        repo_root = self.repository.root
        projection = self.deterministic_stack_projection(base, commits)
        return projection.get('tip') if projection['status'] == 'projected' else None

    def deterministic_stack_projection(self, base, commits):
        """Construct a stack projection entirely in the object database.

        The detailed status lets callers distinguish an empty projection from a
        conflicting one without falling back to a temporary worktree.
        """
        repo_root = self.repository.root
        head = base
        for declared_commit in commits:
            commit = self.repository.commit_full_sha(repo_root, declared_commit)
            if len(self.repository.git(repo_root, 'rev-list', '--parents', '-n', '1', commit).stdout.split()) > 2:
                return {'status': 'unsupported', 'commit': commit, 'base': head,
                        'detail': 'merge commit replay would rewrite its ancestry'}
            merge = self.repository.git(
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
                named = self.repository.git(
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
            if not tree or tree == self.repository.ref_tree(repo_root, head):
                return {'status': 'empty', 'commit': commit, 'base': head}
            head = self.repository.git(
                repo_root,
                'commit-tree',
                tree,
                '-p',
                head,
                '-m',
                self.replay_commit_message(commit),
                env=self.replay_commit_env(commit),
            ).stdout.strip()
        return {'status': 'projected', 'tip': head, 'tree': self.repository.ref_tree(repo_root, head)}

    def git_supports_write_tree(self):
        """Detect whether this Git can run ``merge-tree --write-tree``."""
        repo_root = self.repository.root
        result = self.repository.git(repo_root, '--version', check=False)
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

    def configured_replay_mode(self, manifest):
        """Return the configured default and where it came from, most specific first."""
        repo_root = self.repository.root
        profile_mode = self.repository.load_repo_profile(repo_root).get('replay_mode')
        if profile_mode is not None:
            return normalize_replay_mode(profile_mode, f'{PROFILE_FILENAME} replay_mode'), 'profile'
        manifest_mode = ((manifest or {}).get('defaults') or {}).get('replay_mode')
        if manifest_mode is not None:
            return normalize_replay_mode(manifest_mode, 'manifest defaults.replay_mode'), 'manifest'
        return 'auto', 'builtin'

    def auto_replay_mode(
        self,
        branch,
        location,
        *,
        explicit_worktree=False,
        plumbing_supported=True,
    ):
        """Pick the cheapest mode that applies. Unavailability falls back, never fails."""
        repo_root = self.repository.root
        _worktree, in_place = location
        if in_place:
            return 'in-place'
        if explicit_worktree or self.repository.find_worktree_for_branch(repo_root, branch):
            return 'desk'
        if plumbing_supported and self.git_supports_write_tree():
            return 'plumbing'
        return 'ephemeral'

    def select_replay_mode(
        self,
        manifest,
        args,
        branch,
        location,
        *,
        plumbing_supported=True,
    ):
        """Four-tier selection: command flags, repo profile, manifest default, auto."""
        repo_root = self.repository.root
        mode = requested_replay_mode(args)
        explicit_worktree = bool(getattr(args, 'worktree', None))
        if mode is None and (explicit_worktree or getattr(args, 'in_place', False)):
            # --worktree and --in-place are command flags too, so they outrank a
            # configured default and let auto read the location they asked for.
            mode = 'auto'
        if mode is None:
            mode = self.configured_replay_mode(manifest)[0]
        if mode == 'auto':
            mode = self.auto_replay_mode(
                branch,
                location,
                explicit_worktree=explicit_worktree,
                plumbing_supported=plumbing_supported,
            )
        return self.resolve_replay_mode(location, mode)

    def resolve_replay_mode(self, location, requested_mode):
        """Map an already-resolved replay location to an available execution mode."""
        repo_root = self.repository.root
        if requested_mode not in REPLAY_MODE_CHOICES:
            raise SyncwheelError(f'unsupported replay mode: {requested_mode}')
        if requested_mode == 'auto':
            raise SyncwheelError('replay mode auto must be resolved before this point')
        if requested_mode == 'plumbing':
            if self.git_supports_write_tree():
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

    def planned_replay_mode(self, manifest, branch, plumbing_supported=True):
        """Name the mode a rebuild of ``branch`` would take, for plan output."""
        repo_root = self.repository.root
        mode = self.configured_replay_mode(manifest)[0]
        if mode != 'auto':
            return mode
        return self.auto_replay_mode(
            branch,
            (None, self.repository.get_current_branch(repo_root) == branch),
            plumbing_supported=plumbing_supported,
        )

    def worktree_matches_branch(self, branch, worktree):
        repo_root = self.repository.root
        if worktree is None:
            return False
        found = self.repository.find_worktree_for_branch(repo_root, branch)
        if not found:
            return False
        return found.resolve() == Path(worktree).resolve()

    def replay_commit_message(self, commit):
        repo_root = self.repository.root
        return self.repository.git(repo_root, 'show', '-s', '--format=%B', commit).stdout.rstrip('\n')

    def replay_cherry_pick_args(self, commit, base, *, projection):
        repo_root = self.repository.root
        parents = self.repository.git(repo_root, 'rev-list', '--parents', '-n', '1', commit).stdout.split()[1:]
        if len(parents) <= 1:
            return ['cherry-pick', commit]
        if not projection:
            raise SyncwheelError(
                f'merge commit {commit} cannot be rebuilt by cherry-pick without losing its '
                'fast-forward ancestry; preserve the declared merge tip and publish it directly'
            )
        contained = [index for index, parent in enumerate(parents, 1)
                     if self.repository.branch_contains(repo_root, base, parent)]
        if len(contained) != 1:
            raise SyncwheelError(
                f'merge commit {commit} has no unique parent contained in stack base {base}; '
                'cannot project its tree safely'
            )
        return ['cherry-pick', '-m', str(contained[0]), commit]

    def adopted_integration_tips(self, manifest):
        """Validate exact adopted recipes before preserving their ancestry in replay."""
        repo = self.repository
        tips = set()
        stacks = stack_map(manifest)
        for identifier in manifest['integration']['stacks']:
            stack = stacks[identifier]
            adopted = validate_stack_adoption(stack)
            if not adopted or stack.get('integration_resolution'):
                continue
            source_tip = repo.ref_tip(repo.root, stack['branch'])
            if ((source_tip is not None and source_tip != adopted['tip'])
                    or not repo.branch_contains(repo.root, adopted['tip'], adopted['base'])):
                raise SyncwheelError(f"adopted history for stack {identifier!r} differs from its source")
            if stack['commits']:
                tips.add(adopted['tip'])
        return tips

    def plumbing_replay_script(self, branch, base, commits, expected_tip=None, preserved_tips=()):
        """Render an object-only replay as one POSIX shell transaction."""
        repo_root = self.repository.root
        lines = ['set -e']
        head = base
        for declared_commit in commits:
            commit = self.repository.commit_full_sha(repo_root, declared_commit)
            parent = shell_ref(head)
            if commit in preserved_tips:
                message = shlex.quote(f'Merge adopted stack tip {commit}')
                commit_tree = quoted_with_env(self.replay_commit_env(commit), ['git', 'commit-tree'])
                lines.extend([
                    f'if git merge-base --is-ancestor {parent} {commit}; then',
                    f'  N={commit}',
                    f'elif git merge-base --is-ancestor {commit} {parent}; then',
                    f'  N={parent}',
                    'else',
                    f'  T=$(git merge-tree --write-tree {parent} {commit}) || {{',
                    '    status=$?',
                    "    printf '%s\n' \"$T\" >&2",
                    '    exit "$status"',
                    '  }',
                    f'  N=$({commit_tree} "$T" -p {parent} -p {commit} -m {message})',
                    'fi',
                ])
                head = '$N'
                continue
            self.replay_cherry_pick_args(commit, base, projection=False)
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
            commit_tree = quoted_with_env(self.replay_commit_env(commit), ['git', 'commit-tree'])
            lines.append(
                f'N=$({commit_tree} "$T" -p {parent} -m {shlex.quote(self.replay_commit_message(commit))})'
            )
            head = '$N'
        update = f'git update-ref refs/heads/{branch} {shell_ref(head)}'
        if expected_tip is not None:
            update += f' {shell_ref(expected_tip)}'
        lines.append(update)
        return '\n'.join(lines)

    def ensure_plumbing_target_is_unchecked_out(self, branch):
        repo_root = self.repository.root
        worktree = self.repository.find_worktree_for_branch(repo_root, branch)
        if worktree:
            raise SyncwheelError(
                f"replay mode plumbing requires target branch {branch!r} to be unchecked out; "
                'use --replay-mode ephemeral or desk'
            )

    def replay_plan(self, manifest, target, mode):
        """Build one replay plan without executing it."""
        repo_root = self.repository.root
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
            'expected_tip': target.get('expected_tip'),
            'source_reset_lease': target.get('source_reset_lease'),
            'reset_destination_lease': target.get('reset_destination_lease'),
            'emit_output': not projection,
        }
        preserved_tips = self.adopted_integration_tips(manifest) if replay_kind == 'integration' else set()
        steps = []
        stacks = {}
        if projection:
            if mode != 'desk' or worktree is None:
                raise SyncwheelError('tree projection requires a desk worktree')
            steps.append(replay_exec_step([
                'git', 'worktree', 'add', '--detach', '--quiet', str(worktree), base,
            ]))
        else:
            timestamp = syncwheel_timestamp()
            steps.append(replay_exec_step(['git', 'fetch', '--all', '--prune']))
            if replay_kind == 'stack' and target.get('expected_tip') is not None:
                old = target['expected_tip']
                backup = (['git', 'branch', backup_branch_name(branch, timestamp), old]
                          if old != ZERO_OBJECT_ID else None)
            else:
                backup = self.backup_branch_command(branch, timestamp)
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
            elif self.worktree_matches_branch(branch, worktree):
                steps.append(replay_exec_step(['git', '-C', str(worktree), 'reset', '--hard', base]))
            elif worktree is not None:
                expected_tip = self.repository.ref_tip(repo_root, branch) or ZERO_OBJECT_ID
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
            if target.get('skip_contained'):
                plumbing_commits = [
                    commit for commit in plumbing_commits
                    if not self.repository.branch_contains(repo_root, base, commit)
                ]
            steps.append(replay_shell_step(
                self.plumbing_replay_script(
                    branch,
                    base,
                    plumbing_commits,
                    expected_tip=target.get('expected_tip'),
                    preserved_tips=preserved_tips,
                ),
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
                    [*prefix, *self.replay_cherry_pick_args(commit, base, projection=projection)],
                    self.replay_commit_env(commit),
                ))
        elif integration.get('strategy', 'cherry-pick') == 'cherry-pick':
            stacks_by_id = stacks = stack_map(manifest)
            for stack_id in integration['stacks']:
                for commit in stack_integration_base_commits(stacks_by_id[stack_id]):
                    argv = ([*prefix, 'merge', '--ff', '--no-edit', commit,
                             '-m', f'Merge adopted stack tip {commit}']
                            if commit in preserved_tips else
                            [*prefix, *self.replay_cherry_pick_args(commit, base, projection=projection)])
                    steps.append(replay_exec_step(
                        argv, self.replay_commit_env(commit), stack_id=stack_id,
                    ))
            for stack_id in integration['stacks']:
                for commit in stack_integration_only_commits(stacks_by_id[stack_id]):
                    steps.append(replay_exec_step(
                        [*prefix, *self.replay_cherry_pick_args(commit, base, projection=projection)],
                        self.replay_commit_env(commit),
                        stack_id=stack_id,
                    ))
                    steps[-1]['integration_only'] = True
        elif integration.get('strategy') == 'merge-stacks':
            stacks_by_id = stacks = stack_map(manifest)
            stack_ref_overrides = target.get('stack_ref_overrides') or {}
            for stack_id in integration['stacks']:
                stack = stacks_by_id[stack_id]
                stack_ref = stack_ref_overrides.get(stack_id, stack['branch'])
                resolved = self.projection.merge_stack_resolution_commits(repo_root, stack, stack_ref)
                if resolved is not None:
                    for commit in resolved:
                        steps.append(replay_exec_step(
                            [*prefix, *self.replay_cherry_pick_args(commit, base, projection=projection)],
                            self.replay_commit_env(commit), stack_id=stack_id,
                        ))
                    continue
                steps.append(replay_exec_step(
                    [
                        *prefix,
                        'merge',
                        '--no-ff',
                        stack_ref,
                        '-m',
                        f"Merge stack '{stack_id}' into {branch}",
                    ],
                    self.replay_commit_env(stack_ref),
                    stack_id=stack_id,
                ))
            for stack_id in integration['stacks']:
                for commit in stack_integration_only_commits(stacks_by_id[stack_id]):
                    step = replay_exec_step(
                        [*prefix, 'cherry-pick', commit],
                        self.replay_commit_env(commit), stack_id=stack_id,
                    )
                    step['integration_only'] = True
                    steps.append(step)
        else:
            raise SyncwheelError(f"unsupported integration strategy: {integration.get('strategy')}")

        if mode == 'ephemeral':
            update_ref = [
                'git', '-C', str(worktree), 'update-ref', f'refs/heads/{branch}', 'HEAD',
            ]
            if target.get('expected_tip') is not None:
                update_ref.append(target['expected_tip'])
            steps.append(replay_exec_step(update_ref))

        return {
            'mode': mode,
            'target': plan_target,
            'steps': steps,
            'fallback_from': None,
            'stacks': stacks,
        }

    def replay_stacks_touching(self, steps, stack_id, paths):
        """Other stacks whose already replayed commits change any of ``paths``."""
        repo_root = self.repository.root
        touching = []
        for step in steps:
            other = step.get('stack_id')
            if other in (None, stack_id) or other in touching or 'cherry-pick' not in step['argv']:
                continue
            if set(self.repository.commit_changed_files(repo_root, step['argv'][-1])) & set(paths):
                touching.append(other)
        return touching

    def integration_replay_step_failure(self, plan, index, exc):
        repo_root = self.repository.root
        target = plan['target']
        step = plan['steps'][index]
        stack_id = step['stack_id']
        label = 'integration projection' if target.get('return_tree') else 'integration replay'
        base = target['base']
        argv = step['argv']
        commit = argv[-1]
        short = self.repository.commit_short_sha(repo_root, commit)
        if self.projection.stack_content_absorbed_at(repo_root, plan['stacks'][stack_id], base):
            return SyncwheelError(
                f'{label}: stack {stack_id} is already contained in {base} (stopped at '
                f'commit {short}); close it with syncwheel stack close {stack_id} '
                '--reason absorbed, then retry'
            )
        command_cwd = git_command_cwd(repo_root, argv)
        merge_base = f"{commit}^{argv[argv.index('-m') + 1] if '-m' in argv else ''}"
        onto_head = self.repository.git(
            command_cwd, 'merge-tree', '--write-tree', f'--merge-base={merge_base}',
            'HEAD', commit, check=False,
        )
        if onto_head.returncode == 1:
            paths = merge_tree_conflict_paths(onto_head.stdout)
        elif onto_head.returncode == 0 and onto_head.stdout.split()[:1] == [self.repository.ref_tree(command_cwd, 'HEAD')]:
            paths = self.repository.commit_changed_files(repo_root, commit)
        else:
            paths = []
        others = self.replay_stacks_touching(plan['steps'][:index], stack_id, paths)
        detail = (
            f"stack(s) {', '.join(others)} already changed {', '.join(paths)}" if others
            else f"conflict in {', '.join(paths)}" if onto_head.returncode == 1 and paths
            else (str(exc).splitlines() or ['replay failed'])[0]
        )
        return SyncwheelError(
            f'{label}: stack {stack_id} commit {short} does not replay onto {base} '
            f'({detail})\nRetry with a desk worktree:\n  {replay_conflict_retry_command(target)}'
        )

    def execute_replay_steps(self, plan):
        """Execute an already-materialized replay plan."""
        repo_root = self.repository.root
        target = plan['target']
        branch = target['branch']
        before_tip = self.repository.ref_tip(repo_root, branch)
        result = {
            'status': 'applied',
            'mode': plan['mode'],
            'branch': branch,
            'before_tip': before_tip,
            'after_tip': before_tip,
            'conflict': None,
        }
        cleanup_worktree = (target['worktree'] if target.get('return_tree')
                            and not plan.get('checked_projection_trial') else None)
        try:
            for index, step in enumerate(plan['steps']):
                if step['kind'] == 'exec':
                    argv = step['argv']
                    env = step['env']
                    if target.get('skip_contained') and 'cherry-pick' in argv:
                        command_cwd = git_command_cwd(repo_root, argv)
                        parents = self.repository.git(repo_root, 'rev-list', '--parents', '-n', '1', argv[-1]).stdout.split()[1:]
                        if len(parents) > 1 and self.repository.ref_tree(command_cwd, 'HEAD') == self.repository.ref_tree(repo_root, argv[-1]):
                            continue
                        if self.repository.branch_contains(command_cwd, 'HEAD', argv[-1]):
                            continue
                    effective_argv = argv if env is not None else self.repository.with_git_identity(repo_root, argv)
                    if plan.get('checked_projection_trial'):
                        effective_argv = [effective_argv[0], '-c', 'core.hooksPath=/dev/null', *effective_argv[1:]]
                    try:
                        self.repository.run(effective_argv, cwd=repo_root, env=env)
                    except SyncwheelError as exc:
                        if target.get('return_tree') and step.get('stack_id') and 'merge' in argv:
                            raise SyncwheelError(
                                f"integration projection: stack {step['stack_id']} failed to merge:\n{exc}"
                            ) from exc
                        if not (
                            target.get('skip_contained')
                            and step.get('stack_id')
                            and 'cherry-pick' in argv
                        ):
                            raise
                        raise self.integration_replay_step_failure(
                            plan, index, exc
                        ) from exc
                    register_desk = plan.get('register_created_desk')
                    if register_desk and argv[:3] == ['git', 'worktree', 'add']:
                        register_desk(Path(argv[-2]))
                elif step['kind'] == 'shell':
                    process_env = self.repository.managed_process_env(step['env'])
                    result_shell = self.repository.shell(
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
                reset_destination_lease = target.get('reset_destination_lease')
                if reset_destination_lease is None:
                    target_worktree = self.repository.find_worktree_for_branch(repo_root, branch)
                else:
                    observed_reset_destination = self.checkout.checkout_reset_destination_lease(
                        repo_root,
                        branch,
                        status_base=reset_destination_lease.get('status_base'),
                        allowed_paths=reset_destination_lease.get('allowed_paths'),
                        allowed_path_prefixes=reset_destination_lease.get(
                            'allowed_path_prefixes'
                        ),
                    )
                    if observed_reset_destination != reset_destination_lease:
                        raise SyncwheelError(
                            'integration reset destination changed before published replay reset; '
                            'refusing to overwrite it'
                        )
                    observed_path = observed_reset_destination['worktree_path']
                    target_worktree = Path(observed_path) if observed_path else None
                source_reset_lease = target.get('source_reset_lease')
                if target_worktree and source_reset_lease:
                    observed_source_lease = self.checkout.checkout_source_lease(
                        repo_root,
                        Path(repo_root) / source_reset_lease['manifest_path'],
                    )
                    if (
                        observed_source_lease is None
                        or observed_source_lease['reset_guard_digest']
                        != source_reset_lease['reset_guard_digest']
                    ):
                        raise SyncwheelError(
                            'source checkout changed before published integration replay reset; '
                            'refusing to overwrite it'
                        )
                if target_worktree:
                    self.repository.run(['git', '-C', str(target_worktree), 'reset', '--hard', branch], cwd=repo_root)
            if target.get('return_tree'):
                result['commit'] = self.repository.ref_tip(target['worktree'], 'HEAD')
                result['tree'] = self.repository.ref_tree(repo_root, result['commit'])
            result['after_tip'] = self.repository.ref_tip(repo_root, branch)
            return result
        finally:
            if cleanup_worktree:
                self.repository.git(repo_root, 'worktree', 'remove', '--force', cleanup_worktree, check=False)

    def execute_replay(self, plan, apply):
        """Apply a replay plan or render its dry-run transcript.

        Dry-run output is an executable POSIX shell transcript. Non-plumbing
        ``exec`` steps retain ``quoted(argv)`` exactly; plumbing uses a shell step
        because its object IDs flow through command substitutions.
        """
        repo_root = self.repository.root
        if not apply:
            target = plan['target']
            branch = target['branch']
            before_tip = self.repository.ref_tip(repo_root, branch)
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
            self.ensure_plumbing_target_is_unchecked_out(plan['target']['branch'])

        if plan['mode'] == 'ephemeral':
            with tempfile.TemporaryDirectory(prefix='syncwheel-replay-') as tmp:
                worktree = Path(tmp)
                bound_plan = bind_ephemeral_worktree(plan, worktree)
                try:
                    return self.execute_replay_steps(bound_plan)
                finally:
                    self.repository.git(repo_root, 'worktree', 'remove', '--force', worktree, check=False)
        return self.execute_replay_steps(plan)

    def materialize_channel_tip(self, channel, plan):
        """Replay exact pins, excluding only ancestry already in the pinned base."""
        repo_root = self.repository.root
        if self.repository.find_worktree_for_branch(repo_root, channel['branch']):
            raise SyncwheelError(
                f"channel branch {channel['branch']!r} is checked out; close its worktree before apply"
            )
        resolution = channel.get('resolution')
        if resolution:
            return resolution['revision']
        temporary_branch = f"syncwheel/channel-build/{plan['planDigest'][:24]}"
        temporary_ref = f'refs/heads/{temporary_branch}'
        if self.repository.ref_tip(repo_root, temporary_ref):
            raise SyncwheelError(f'temporary channel build ref already exists: {temporary_ref}')
        synthetic = self.channel_projection_stack(channel, temporary_branch)
        mode = 'plumbing' if self.git_supports_write_tree() else 'ephemeral'
        replay = self.replay_plan(None, replay_target(stack=synthetic), mode)
        try:
            with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink):
                result = self.execute_replay(replay, True)
            require_replay_success(result)
            tip = result['after_tip']
            if not tip:
                raise SyncwheelError('channel materialization did not produce a commit')
            return tip
        finally:
            self.repository.git(repo_root, 'update-ref', '-d', temporary_ref, check=False)

    def channel_projection_stack(self, channel, branch):
        repo_root = self.repository.root
        commits = [
            commit
            for entry in channel.get('composition', [])
            for commit in entry.get('commits', [])
            if not self.repository.branch_contains(repo_root, channel['baseRevision'], commit)
        ]
        return {'id': f"channel-{channel['id']}", 'branch': branch,
                'base': channel['baseRevision'], 'commits': commits}

    def materialize_channel_tree(self, channel):
        """Project selected immutable pins without updating a channel/build ref."""
        resolution = channel.get('resolution')
        if resolution:
            return self.repository.ref_tree(self.repository.root, resolution['revision'])
        stack = self.channel_projection_stack(channel, channel['branch'])
        if not stack['commits']:
            return self.repository.ref_tree(self.repository.root, channel['baseRevision'])
        return self.materialize_stack_projection(stack, skip_contained=False)

    def materialize_stack_projection(self, stack, *, skip_contained=True):
        repo_root = self.repository.root
        with tempfile.TemporaryDirectory(prefix='syncwheel-stack-projection-') as tmp:
            plan = self.replay_plan(
                None,
                replay_target(
                    stack=stack,
                    worktree=Path(tmp),
                    return_tree=True,
                    skip_contained=skip_contained,
                ),
                'desk',
            )
            return self.execute_replay(plan, True)['tree']

    @contextlib.contextmanager
    def projection_trial(self, prefix):
        repo_root = self.repository.root
        directory = Path(tempfile.mkdtemp(prefix=prefix))
        try:
            yield directory
        finally:
            entries = self.repository.git(
                repo_root, 'worktree', 'list', '--porcelain', '-z',
            ).stdout.split('\0')
            if f'worktree {directory}' in entries:
                removed = self.repository.git(repo_root, 'worktree', 'remove', '--force',
                                              str(directory), check=False)
                if removed.returncode:
                    raise SyncwheelError(
                        f'projection trial cleanup failed; retained detached checkout at {directory}: '
                        + removed.stderr.strip()
                    )
            if directory.exists():
                shutil.rmtree(directory)

    def materialize_capture_tip(self, stack):
        for commit in stack['commits']:
            self.replay_cherry_pick_args(commit, stack['base'], projection=False)
        with self.projection_trial('syncwheel-stack-capture-') as tmp:
            plan = self.replay_plan(
                None, replay_target(stack=stack, worktree=Path(tmp), return_tree=True), 'desk',
            )
            plan['checked_projection_trial'] = True
            return self.execute_replay(plan, True)['commit']

    def materialize_integration_replay(self, manifest, stack_ref_overrides=None):
        """Build the ordinary replay commit without moving refs or the source index.

        Keep the detached commit as well as its tree: ancestry reconciliation needs
        the actual replay graph before deciding whether to move integration.
        """
        with self.projection_trial('syncwheel-projection-') as tmp:
            plan = self.replay_plan(
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
            plan['checked_projection_trial'] = True
            return self.execute_replay(plan, True)['commit']

    def materialize_integration_projection(self, manifest, stack_ref_overrides=None):
        repo_root = self.repository.root
        commit = self.materialize_integration_replay(manifest, stack_ref_overrides)
        return self.repository.ref_tree(repo_root, commit)

    def backup_branch_command(self, branch, timestamp):
        repo_root = self.repository.root
        if not self.repository.branch_exists(repo_root, branch):
            return None
        return ['git', 'branch', backup_branch_name(branch, timestamp), branch]
