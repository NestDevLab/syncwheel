"""CLI grammar and command mutation metadata.

The application supplies a named handler registry. This module constructs only
argument parsers and behavior descriptions; it never imports command execution.
"""

import argparse

from syncwheel_worktree_policy import POLICY_VALUES
from syncwheel_policy import FIELD_VALUES, PRESETS

from syncwheel_contracts import (
    AUTHORITY_GRANTABLE_CLASSES,
    COORDINATION_REPAIR_DIGEST_HEAL_BACKEND,
    COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND,
    COORDINATION_REPAIR_FAST_FORWARD_BACKEND,
    COORDINATION_REPAIR_MISSING_REF_BACKEND,
    COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND,
    GITHUB_PR_MERGE_CHECKS,
    GITHUB_PR_MERGE_INTEGRATION,
    GITHUB_PR_MERGE_METHODS,
    RECONCILE_MODES,
    UPDATE_MODES,
)
from syncwheel_manifest import (
    AUTHORITY_MODES,
    CHANNEL_LIFECYCLES,
    REPLAY_MODE_CHOICES,
    SYNCWHEEL_TRACKING_VALUES,
)


def worktree_policy_field(value):
    fields = tuple(field.replace('_', '-') for field in POLICY_VALUES)
    if value not in fields:
        raise argparse.ArgumentTypeError('expected one of: ' + ', '.join(fields))
    return value


def execution_policy_field(value):
    if value not in ('preset', *(field.replace('_', '-') for field in FIELD_VALUES)):
        raise argparse.ArgumentTypeError('unknown execution policy field: ' + value)
    return value


def add_worktree_policy_parser(parent, commands, *, scope, common=None, name='worktree-policy', execution=False):
    policy = parent.add_parser(name, help='user, repository or lane preferences')
    fields = FIELD_VALUES if execution else POLICY_VALUES
    if scope == 'lane':
        policy.add_argument('lane', help='stable governed lane id')
    actions = policy.add_subparsers(dest='policy_action', required=True)
    operations = ('status', 'set', 'clear', 'record', 'release-status') if execution and scope == 'repo' else ('status', 'set', 'clear')
    for action in operations:
        parser = actions.add_parser(action, parents=[common] if common is not None else [])
        parser.set_defaults(func=commands['command_worktree_policy'], policy_scope=scope, execution_policy=execution)
        parser.add_argument('-j', '--json', action='store_true')
        if action == 'record':
            parser.add_argument('--receipt', action='append', required=True, help='explicit operator-verified JSON receipt')
        if action == 'release-status':
            parser.add_argument('--head', required=True, help='exact candidate commit SHA')
        if action == 'set':
            if scope == 'lane':
                parser.add_argument('--bind-generation', help='explicitly bind new overrides to this current generation')
            if execution:
                parser.add_argument('--preset', choices=tuple(PRESETS))
            for field, values in fields.items():
                parser.add_argument('--' + field.replace('_', '-'), choices=values)
        if action == 'clear':
            # Python 3.11 applies choices to the empty list for nargs='*'.
            # Validate each supplied field instead, preserving clear-all.
            parser.add_argument('fields', nargs='*', type=(execution_policy_field if execution else worktree_policy_field), metavar='FIELD',
                                help=('preference field or preset; omit to clear this scope' if execution else
                                      'expiry or writer-exclusivity; omit to clear both'))
        if action not in {'status', 'release-status'}:
            parser.add_argument('-a', '--apply', action='store_true')


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


def build_parser(commands, version):
    parser = argparse.ArgumentParser(description='Deterministic syncwheel helper for fork/upstream/integration repos.')
    parser.add_argument('--version', action='version', version=f'syncwheel {version}')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('-r', '--repo', help='target repo path or registered alias')
    common.add_argument('-M', '--manifest', help='path to a syncwheel manifest JSON file')
    common.add_argument('-p', '--personal', help='use .syncwheel/manifests/<name>.local.json')
    sub = parser.add_subparsers(dest='command', required=True)

    revision_provider_p = sub.add_parser(
        'revision-provider',
        help='serve one strict Agentwheel revision-provider JSON request on stdin',
    )
    revision_provider_p.set_defaults(func=commands['command_revision_provider'])
    revision_provider_sub = revision_provider_p.add_subparsers(dest='revision_provider_command')
    resolve_prepared_p = revision_provider_sub.add_parser(
        'resolve-prepared',
        help='bind an exact reviewed draft object to an unresolved prepared operation',
    )
    resolve_prepared_p.add_argument('-r', '--repo', help='canonical operation repository path or alias')
    resolve_prepared_p.add_argument('operation_id')
    resolve_prepared_p.add_argument('--resolution', required=True, help='full resolution commit OID')
    resolve_prepared_p.add_argument('--reason', required=True)
    resolve_prepared_p.add_argument('--plan-digest')
    resolve_prepared_p.add_argument('--apply', action='store_true')
    resolve_prepared_p.set_defaults(func=commands['command_revision_provider_resolve_prepared'])

    repo_p = sub.add_parser('repo', aliases=['r'], help='manage repo aliases')
    repo_sub = repo_p.add_subparsers(dest='repo_command', required=True)

    repo_add_p = repo_sub.add_parser('add', help='add/update one repo alias')
    repo_add_p.add_argument('alias')
    repo_add_p.add_argument('path')
    repo_add_p.add_argument('-M', '--manifest', help='optional default manifest path for this alias')
    repo_add_p.set_defaults(func=commands['command_repo_add'])

    repo_manifest_p = repo_sub.add_parser('set-manifest', help='set/clear default manifest path for one alias')
    repo_manifest_p.add_argument('alias')
    repo_manifest_p.add_argument('manifest', nargs='?', help='manifest path; omit with --clear to remove')
    repo_manifest_p.add_argument('-c', '--clear', action='store_true')
    repo_manifest_p.set_defaults(func=commands['command_repo_set_manifest'])

    repo_rm_p = repo_sub.add_parser('rm', help='remove one repo alias')
    repo_rm_p.add_argument('alias')
    repo_rm_p.set_defaults(func=commands['command_repo_rm'])

    repo_ls_p = repo_sub.add_parser('ls', help='list repo aliases')
    repo_ls_p.add_argument('-j', '--json', action='store_true')
    repo_ls_p.set_defaults(func=commands['command_repo_ls'])

    add_worktree_policy_parser(repo_sub, commands, scope='repo', common=common)
    add_worktree_policy_parser(repo_sub, commands, scope='repo', common=common, name='execution-policy', execution=True)

    repo_mode_p = repo_sub.add_parser('mode', help='migrate the repository delivery model')
    repo_mode_sub = repo_mode_p.add_subparsers(dest='mode_command', required=True)
    repo_mode_set_p = repo_mode_sub.add_parser('set', parents=[common])
    repo_mode_set_p.add_argument('repository_mode', choices=['journal'])
    repo_mode_set_p.add_argument('--branch', default='main')
    repo_mode_set_p.add_argument('--remote', default='origin')
    repo_mode_set_p.add_argument('--include', action='append')
    repo_mode_set_p.add_argument('--exclude', action='append')
    repo_mode_set_p.add_argument('--interval', default='30m')
    repo_mode_set_p.add_argument('--max-file-bytes', type=int, default=10485760)
    repo_mode_set_p.add_argument('--manifest-version', type=int, choices=[1, 2], default=1)
    repo_mode_set_p.add_argument('--reason', required=True)
    repo_mode_set_p.add_argument('-a', '--apply', action='store_true')
    repo_mode_set_p.set_defaults(func=commands['command_repo_mode_set'])

    repo_tracking_p = repo_sub.add_parser('tracking', help='inspect or set repo-local syncwheel tracking policy')
    repo_tracking_sub = repo_tracking_p.add_subparsers(dest='tracking_command', required=True)

    repo_tracking_status_p = repo_tracking_sub.add_parser('status', parents=[common])
    repo_tracking_status_p.add_argument('-j', '--json', action='store_true')
    repo_tracking_status_p.set_defaults(func=commands['command_repo_tracking_status'])

    repo_tracking_set_p = repo_tracking_sub.add_parser('set', parents=[common])
    repo_tracking_set_p.add_argument('tracking', choices=sorted(SYNCWHEEL_TRACKING_VALUES))
    repo_tracking_set_p.add_argument('-W', '--worktree-root', help='default repo-relative syncwheel worktree/cache root')
    repo_tracking_set_p.add_argument('-a', '--apply', action='store_true')
    repo_tracking_set_p.add_argument('-j', '--json', action='store_true')
    repo_tracking_set_p.set_defaults(func=commands['command_repo_tracking_set'])
    repo_authority_p = repo_sub.add_parser(
        'authority', help='inspect or set the repo-local agent authority policy'
    )
    repo_authority_sub = repo_authority_p.add_subparsers(dest='repo_authority_command', required=True)
    repo_authority_status_p = repo_authority_sub.add_parser('status', parents=[common])
    repo_authority_status_p.add_argument('-j', '--json', action='store_true')
    repo_authority_status_p.set_defaults(func=commands['command_repo_authority_status'])
    repo_authority_set_p = repo_authority_sub.add_parser('set', parents=[common])
    repo_authority_set_p.add_argument('mode', choices=sorted(AUTHORITY_MODES))
    repo_authority_set_p.add_argument(
        '--allow', action='append', choices=list(AUTHORITY_GRANTABLE_CLASSES),
        help='authority class agents may exercise without a human gate (repeatable)',
    )
    repo_authority_set_p.add_argument('-a', '--apply', action='store_true')
    repo_authority_set_p.add_argument('-j', '--json', action='store_true')
    repo_authority_set_p.set_defaults(func=commands['command_repo_authority_set'])

    repo_pr_merge_p = repo_sub.add_parser(
        'pr-merge-policy', help='inspect or set the private GitHub PR merge policy'
    )
    repo_pr_merge_sub = repo_pr_merge_p.add_subparsers(
        dest='pr_merge_policy_command', required=True
    )
    repo_pr_merge_status_p = repo_pr_merge_sub.add_parser('status', parents=[common])
    repo_pr_merge_status_p.add_argument('-j', '--json', action='store_true')
    repo_pr_merge_status_p.set_defaults(func=commands['command_repo_pr_merge_policy_status'])
    repo_pr_merge_set_p = repo_pr_merge_sub.add_parser('set', parents=[common])
    repo_pr_merge_set_p.add_argument('provider', choices=('github',))
    repo_pr_merge_set_p.add_argument('--repository', required=True)
    repo_pr_merge_set_p.add_argument('--base', action='append', required=True)
    repo_pr_merge_set_p.add_argument('--method', choices=sorted(GITHUB_PR_MERGE_METHODS), required=True)
    repo_pr_merge_set_p.add_argument('--allow-bypass', action='append', default=[])
    repo_pr_merge_set_p.add_argument('--merge-actor', action='append', required=True)
    repo_pr_merge_set_p.add_argument('--pr-author', action='append')
    repo_pr_merge_set_p.add_argument('--commit-author', action='append')
    repo_pr_merge_set_p.add_argument('--head-repository', action='append')
    repo_pr_merge_set_p.add_argument('--checks', choices=sorted(GITHUB_PR_MERGE_CHECKS), default='all')
    repo_pr_merge_set_p.add_argument('--integration', choices=sorted(GITHUB_PR_MERGE_INTEGRATION), default='required')
    repo_pr_merge_set_p.add_argument('-a', '--apply', action='store_true')
    repo_pr_merge_set_p.add_argument('-j', '--json', action='store_true')
    repo_pr_merge_set_p.set_defaults(func=commands['command_repo_pr_merge_policy_set'])
    repo_pr_merge_clear_p = repo_pr_merge_sub.add_parser('clear', parents=[common])
    repo_pr_merge_clear_p.add_argument('-a', '--apply', action='store_true')
    repo_pr_merge_clear_p.add_argument('-j', '--json', action='store_true')
    repo_pr_merge_clear_p.set_defaults(func=commands['command_repo_pr_merge_policy_clear'])

    self_p = sub.add_parser('self', help='inspect or update the syncwheel installation itself')
    self_sub = self_p.add_subparsers(dest='self_command', required=True)

    add_worktree_policy_parser(self_sub, commands, scope='global')
    add_worktree_policy_parser(self_sub, commands, scope='global', name='execution-policy', execution=True)

    self_status_p = self_sub.add_parser('status', help='show syncwheel install/update status')
    self_status_p.add_argument('-f', '--fetch', action='store_true', help='refresh remote tracking info before reporting')
    self_status_p.add_argument('-j', '--json', action='store_true')
    self_status_p.set_defaults(func=commands['command_self_status'])

    self_check_p = self_sub.add_parser('check-update', help='check whether a newer syncwheel version exists')
    self_check_p.add_argument('-f', '--fetch', action='store_true', help='refresh remote tracking info before checking')
    self_check_p.add_argument('-j', '--json', action='store_true')
    self_check_p.set_defaults(func=commands['command_self_check_update'])

    self_update_p = self_sub.add_parser('update', help='update this syncwheel install')
    self_update_p.add_argument('-n', '--dry-run', action='store_true')
    self_update_p.add_argument('-F', '--no-fetch', action='store_true')
    self_update_p.set_defaults(func=commands['command_self_update'])

    self_hooks_p = self_sub.add_parser('install-hooks', help='install syncwheel Git hooks in this syncwheel checkout')
    self_hooks_p.add_argument('-n', '--dry-run', action='store_true')
    self_hooks_p.set_defaults(func=commands['command_self_install_hooks'])

    self_mode_p = self_sub.add_parser('mode', help='show or set automatic update policy: off, notify, auto')
    self_mode_p.add_argument('mode', nargs='?', choices=sorted(UPDATE_MODES))
    self_mode_p.set_defaults(func=commands['command_self_mode'])

    hooks_p = sub.add_parser('hooks', help='plan, install, or remove managed repository guards')
    hooks_sub = hooks_p.add_subparsers(dest='hooks_command', required=True)
    hooks_status_p = hooks_sub.add_parser('status', parents=[common])
    hooks_status_p.set_defaults(func=commands['command_hooks_status'])
    hooks_install_p = hooks_sub.add_parser('install', parents=[common])
    hooks_install_p.add_argument('-a', '--apply', action='store_true')
    hooks_install_p.add_argument(
        '--reason',
        help='required explanation when changing the guarded integration branch',
    )
    hooks_install_p.set_defaults(func=commands['command_hooks_install'])
    hooks_remove_p = hooks_sub.add_parser('remove', parents=[common])
    hooks_remove_p.add_argument('-a', '--apply', action='store_true')
    hooks_remove_p.add_argument('--disable', action='store_true', help='persist an explicit clone-local opt-out')
    hooks_remove_p.add_argument('--reason', help='required explanation for --disable')
    hooks_remove_p.set_defaults(func=commands['command_hooks_remove'])
    hooks_guard_p = hooks_sub.add_parser('guard', parents=[common], help=argparse.SUPPRESS)
    hooks_guard_p.add_argument('--remote-name', required=True)
    hooks_guard_p.add_argument('--remote-url', required=True)
    hooks_guard_p.set_defaults(func=commands['command_hooks_guard'])
    hooks_worktree_guard_p = hooks_sub.add_parser(
        'worktree-guard', parents=[common], help=argparse.SUPPRESS
    )
    hooks_worktree_guard_p.add_argument(
        '--event', required=True, choices=('pre-commit', 'post-checkout')
    )
    hooks_worktree_guard_p.set_defaults(func=commands['command_hooks_worktree_guard'])
    hooks_ref_guard_p = hooks_sub.add_parser(
        'ref-guard', parents=[common], help=argparse.SUPPRESS
    )
    hooks_ref_guard_p.add_argument('--phase', default='')
    hooks_ref_guard_p.set_defaults(func=commands['command_hooks_ref_guard'])

    use_p = sub.add_parser('use', help='show or set the repo-local default syncwheel profile', parents=[common])
    use_p.add_argument('personal', nargs='?', help='personal profile name to use by default')
    use_p.add_argument('-s', '--shared', action='store_true', help='clear the local profile and use the shared manifest')
    use_p.set_defaults(func=commands['command_use'])

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
    replay_mode_p.set_defaults(func=commands['command_replay_mode'])

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
    init_p.set_defaults(func=commands['command_init'])

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
    coordination_claims_backfill_p.set_defaults(func=commands['command_coordination_claims_backfill'])

    coordination_init_p = coordination_sub.add_parser('init', parents=[common])
    coordination_init_p.add_argument('-R', '--remote', help='configured publication remote for active-active coordination')
    coordination_init_p.add_argument('--coordination-id', help='public coordination-domain id')
    coordination_init_p.add_argument('-a', '--apply', action='store_true')
    coordination_init_p.set_defaults(func=commands['command_coordination_init'])

    coordination_disable_p = coordination_sub.add_parser('disable', parents=[common])
    coordination_disable_p.add_argument('-a', '--apply', action='store_true')
    coordination_disable_p.set_defaults(func=commands['command_coordination_disable'])

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
        func=commands['command_coordination_provenance_reset']
    )
    coordination_provenance_supersede_p = coordination_provenance_sub.add_parser(
        'supersede', parents=[common],
        help='plan or apply exact replay-backed supersession of stale derived records',
    )
    coordination_provenance_supersede_p.add_argument('--reconciliation')
    coordination_provenance_supersede_p.add_argument('--path', action='append')
    coordination_provenance_supersede_p.add_argument('--reason')
    coordination_provenance_supersede_p.add_argument('--plan-file')
    coordination_provenance_supersede_p.add_argument('-a', '--apply', action='store_true')
    coordination_provenance_supersede_p.set_defaults(
        func=commands['command_coordination_provenance_supersede']
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
            COORDINATION_REPAIR_MISSING_REF_BACKEND,
            COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND,
            COORDINATION_REPAIR_DIGEST_HEAL_BACKEND,
        ],
        default='github-lock',
        help=(
            'reviewed repair backend: github-lock remains unsupported; '
            'tree-equivalent-state-cas requires exact tree equality; '
            'fast-forward-state-cas requires exact bounded ancestry; '
            'missing-ref-create-cas atomically recreates one absent owned ref; '
            'state-digest-migration republishes a pre-0.42.2 state under the '
            'control-manifest digest; state-digest-heal recomputes an '
            'unrecognized (neither raw nor legacy) manifest_digest from the '
            'manifest already committed at the aligned integration tip'
        ),
    )
    coordination_repair_p.add_argument(
        '--plan-file',
        help='exact reviewed JSON plan; required with --apply',
    )
    coordination_repair_p.set_defaults(func=commands['command_coordination_repair'])

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
        '--known-base-manifest-digest',
        '--known-base-snapshot-digest',
        dest='known_base_manifest_digest',
        help=(
            'exact integration-tip control manifest digest recorded by '
            '--known-base-state'
        ),
    )
    coordination_compose_p.add_argument('-a', '--apply', action='store_true')
    coordination_compose_p.add_argument('--plan-file', help='exact reviewed JSON plan')
    coordination_compose_p.set_defaults(func=commands['command_coordination_compose'])

    handoff_p = sub.add_parser(
        'handoff',
        help='read-only active-active coordination diagnostic for a multi-device handoff',
        parents=[common],
    )
    handoff_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    handoff_p.add_argument('-j', '--json', action='store_true')
    handoff_p.set_defaults(func=commands['command_handoff'], fetch=True)

    gc_p = sub.add_parser(
        'gc',
        help='report or reap eligible local tombstoned worktrees, branches, and backups',
        parents=[common],
    )
    gc_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    gc_p.add_argument('-a', '--apply', action='store_true')
    gc_p.add_argument('-j', '--json', action='store_true')
    gc_p.add_argument(
        '-l', '--lane', dest='lanes', action='append', metavar='LANE',
        help='limit governed lane cleanup to this lane id (repeatable); other artifacts are only reported',
    )
    gc_p.set_defaults(func=commands['command_gc'], fetch=True)

    worktree_p = sub.add_parser('worktree', aliases=['wt'], help='manage governed local worktrees and safety locks')
    worktree_sub = worktree_p.add_subparsers(dest='worktree_command', required=True)
    add_worktree_policy_parser(worktree_sub, commands, scope='lane', common=common, name='policy')
    add_worktree_policy_parser(worktree_sub, commands, scope='lane', common=common, name='execution-policy', execution=True)

    worktree_open_p = worktree_sub.add_parser('open', parents=[common])
    worktree_open_p.add_argument('lane')
    worktree_open_p.add_argument('--into', help='optional existing stack that will own this lane\'s commits')
    worktree_open_p.add_argument('--base', help='with --into, start at that stack\'s exact projected tip instead of the current checkout HEAD')
    worktree_open_p.add_argument('--full', action='store_true', help='mark this explicitly requested lane as eligible for dependency, build, test, and debug work')
    worktree_open_p.add_argument('-j', '--json', action='store_true')
    worktree_open_p.set_defaults(func=commands['command_worktree_open'])

    worktree_list_p = worktree_sub.add_parser(
        'list', aliases=['ls'], parents=[common],
        help='list governed lanes with their state, health and remedy; read-only',
    )
    worktree_list_p.add_argument('-P', '--problems', action='store_true', help='show only lanes that need attention')
    worktree_list_p.add_argument('-j', '--json', action='store_true')
    worktree_list_p.set_defaults(func=commands['command_worktree_list'])

    worktree_release_p = worktree_sub.add_parser('release', parents=[common])
    worktree_release_p.add_argument('lane')
    worktree_release_p.add_argument('--reason', required=True, help='why this dead or abandoned lane is being released')
    worktree_release_p.add_argument('-a', '--apply', action='store_true', help='create any recovery ref and remove the released lane record')
    worktree_release_p.add_argument('-j', '--json', action='store_true')
    worktree_release_p.set_defaults(func=commands['command_worktree_release'])

    retire_copy_p = worktree_sub.add_parser(
        'retire-copy', parents=[common], help='preview cleanup of a proven duplicate lane copy',
    )
    retire_copy_p.add_argument('lane')
    retire_copy_p.add_argument('--commit', metavar='REV', help='select one declared comparison commit')
    retire_copy_p.add_argument('--reason', required=True, help='record why this duplicate is being retired')
    retire_copy_p.add_argument('-a', '--apply', action='store_true')
    retire_copy_p.add_argument('--plan-digest')
    retire_copy_p.add_argument('--confirm-writers-stopped', action='store_true')
    retire_copy_p.add_argument('-j', '--json', action='store_true', help='output JSON (the default)')
    retire_copy_p.set_defaults(func=commands['command_worktree_retire_copy'])

    worktree_lock_p = worktree_sub.add_parser('lock', parents=[common])
    worktree_lock_p.add_argument('stack')
    worktree_lock_p.set_defaults(func=commands['command_worktree_lock'])

    worktree_unlock_p = worktree_sub.add_parser('unlock', parents=[common])
    worktree_unlock_p.add_argument('stack')
    worktree_unlock_p.set_defaults(func=commands['command_worktree_unlock'])

    status_p = sub.add_parser('status', aliases=['st'], help='show repo and manifest state', parents=[common])
    status_p.add_argument('-f', '--fetch', action='store_true')
    status_p.add_argument('-j', '--json', action='store_true')
    status_p.set_defaults(func=commands['command_status'])

    validate_p = sub.add_parser('validate', aliases=['v'], help='validate the manifest against local git state', parents=[common])
    validate_p.add_argument('-j', '--json', action='store_true')
    validate_p.set_defaults(func=commands['command_validate'])

    plan_p = sub.add_parser('plan', aliases=['pl'], help='emit a deterministic action plan from the manifest', parents=[common])
    plan_p.add_argument('-j', '--json', action='store_true')
    plan_p.set_defaults(func=commands['command_plan'])

    check_p = sub.add_parser('check', aliases=['ck'], help='fetch, validate, and print the current action plan', parents=[common])
    check_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    check_p.add_argument('--strict', action='store_true', help='exit nonzero on warnings or planned actions')
    check_p.add_argument('-j', '--json', action='store_true')
    check_p.set_defaults(func=commands['command_check'], fetch=True)

    ledger_p = sub.add_parser('ledger', help='inspect the append-only syncwheel ledger', parents=[common])
    ledger_sub = ledger_p.add_subparsers(dest='ledger_command', required=True)

    ledger_show_p = ledger_sub.add_parser('show', aliases=['sh'], parents=[common])
    ledger_show_p.add_argument('-j', '--json', action='store_true')
    ledger_show_p.set_defaults(func=commands['command_ledger_show'])

    reconcile_p = sub.add_parser(
        'reconcile',
        aliases=['rec'],
        help='reconcile manifest, stack branches, integration, and remote tips',
        parents=[common],
    )
    add_reconcile_args(reconcile_p, include_apply_push=True)
    reconcile_p.set_defaults(func=commands['command_reconcile'], fetch=True, update_manifest=True, mode='standard')

    resume_p = sub.add_parser(
        'resume',
        help='resume reconcile from a shared remote state on a new machine',
        parents=[common],
    )
    add_reconcile_args(resume_p, include_apply_push=True)
    resume_p.set_defaults(func=commands['command_resume'], fetch=True, update_manifest=True, mode='resume')

    sync_p = sub.add_parser(
        'sync',
        help='apply the safe local reconcile lifecycle without pushing remotes',
        parents=[common],
    )
    add_reconcile_args(sync_p, include_apply_push=False, include_push_options=False)
    sync_p.set_defaults(
        func=commands['command_sync'],
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
    publish_p.set_defaults(func=commands['command_publish'], fetch=True, update_manifest=True, apply=True, push=True, mode='standard')

    manifest_p = sub.add_parser('manifest', aliases=['m'], help='inspect, compare, and migrate syncwheel manifests')
    manifest_sub = manifest_p.add_subparsers(dest='manifest_command', required=True)

    manifest_compare_p = manifest_sub.add_parser('compare', parents=[common])
    manifest_compare_p.add_argument('-O', '--other-manifest')
    manifest_compare_p.add_argument('-P', '--other-personal')
    manifest_compare_p.add_argument('-j', '--json', action='store_true')
    manifest_compare_p.set_defaults(func=commands['command_manifest_compare'])

    manifest_recover_p = manifest_sub.add_parser(
        'recover-control', parents=[common],
        help='plan or restore tracked control metadata from a verified native replay backup',
    )
    manifest_recover_p.add_argument('--from-backup', required=True)
    manifest_recover_p.add_argument('--plan-digest')
    manifest_recover_p.add_argument('--reason')
    manifest_recover_p.add_argument('-a', '--apply', action='store_true')
    manifest_recover_p.set_defaults(func=commands['command_manifest_recover_control'])

    manifest_stack_recovery_p = manifest_sub.add_parser(
        'recover-stack', parents=[common],
        help='withdraw one stack whose dirty declaration is proven by the native ledger',
    )
    manifest_stack_recovery_p.add_argument('stack')
    manifest_stack_recovery_p.add_argument('--reason', required=True)
    manifest_stack_recovery_p.add_argument('--plan-digest')
    manifest_stack_recovery_p.add_argument('-a', '--apply', action='store_true')
    manifest_stack_recovery_p.set_defaults(func=commands['command_manifest_recover_stack'])

    manifest_require_integration_p = manifest_sub.add_parser(
        'require-integration',
        help='require every declared stack to participate in integration',
        parents=[common],
    )
    manifest_require_integration_p.add_argument('-a', '--apply', action='store_true')
    manifest_require_integration_p.add_argument('-j', '--json', action='store_true')
    manifest_require_integration_p.set_defaults(func=commands['command_manifest_require_integration'])

    channel_p = sub.add_parser(
        'channel', aliases=['ch'],
        help='manage pinned, ordered, rebuildable deployment-channel branches',
    )
    channel_sub = channel_p.add_subparsers(dest='channel_command', required=True)

    channel_list_p = channel_sub.add_parser('list', aliases=['ls'], parents=[common])
    channel_list_p.add_argument('-j', '--json', action='store_true')
    channel_list_p.set_defaults(func=commands['command_channel_list'])

    channel_show_p = channel_sub.add_parser('show', aliases=['sh'], parents=[common])
    channel_show_p.add_argument('channel')
    channel_show_p.set_defaults(func=commands['command_channel_show'])

    channel_contract_p = channel_sub.add_parser('contract')
    channel_contract_p.set_defaults(func=commands['command_channel_contract'])

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
    channel_create_p.set_defaults(func=commands['command_channel_create'])

    channel_add_p = channel_sub.add_parser('add', parents=[common])
    channel_add_p.add_argument('channel')
    channel_add_p.add_argument('stack')
    channel_add_p.add_argument('--position', type=int)
    channel_add_p.add_argument('--operation-id')
    channel_add_p.add_argument('--plan-digest')
    channel_add_p.add_argument('-a', '--apply', action='store_true')
    channel_add_p.set_defaults(func=commands['command_channel_add'])

    channel_remove_p = channel_sub.add_parser('remove', aliases=['rm'], parents=[common])
    channel_remove_p.add_argument('channel')
    channel_remove_p.add_argument('stack')
    channel_remove_p.add_argument('--operation-id')
    channel_remove_p.add_argument('--plan-digest')
    channel_remove_p.add_argument('-a', '--apply', action='store_true')
    channel_remove_p.set_defaults(func=commands['command_channel_remove'])

    channel_replace_p = channel_sub.add_parser('replace', parents=[common])
    channel_replace_p.add_argument('channel')
    channel_replace_p.add_argument('old_stack')
    channel_replace_p.add_argument('new_stack')
    channel_replace_p.add_argument('--operation-id')
    channel_replace_p.add_argument('--plan-digest')
    channel_replace_p.add_argument('-a', '--apply', action='store_true')
    channel_replace_p.set_defaults(func=commands['command_channel_replace'])

    channel_refresh_p = channel_sub.add_parser('refresh', parents=[common])
    channel_refresh_p.add_argument('channel')
    channel_refresh_p.add_argument('-s', '--stack', action='append')
    channel_refresh_p.add_argument('--operation-id')
    channel_refresh_p.add_argument('--plan-digest')
    channel_refresh_p.add_argument('-a', '--apply', action='store_true')
    channel_refresh_p.set_defaults(func=commands['command_channel_refresh'])

    channel_diff_p = channel_sub.add_parser('diff', parents=[common])
    channel_diff_p.add_argument('channel')
    channel_diff_p.add_argument('-O', '--other')
    channel_diff_p.set_defaults(func=commands['command_channel_diff'])

    channel_promote_p = channel_sub.add_parser('promote', parents=[common])
    channel_promote_p.add_argument('source')
    channel_promote_p.add_argument('target')
    channel_promote_p.add_argument('--operation-id')
    channel_promote_p.add_argument('--plan-digest')
    channel_promote_p.add_argument('-a', '--apply', action='store_true')
    channel_promote_p.set_defaults(func=commands['command_channel_promote'])

    channel_resolve_p = channel_sub.add_parser('resolve', parents=[common])
    channel_resolve_p.add_argument('channel')
    channel_resolve_choice = channel_resolve_p.add_mutually_exclusive_group(required=True)
    channel_resolve_choice.add_argument('--revision')
    channel_resolve_choice.add_argument('--clear', action='store_true')
    channel_resolve_p.add_argument('--operation-id')
    channel_resolve_p.add_argument('--plan-digest')
    channel_resolve_p.add_argument('-a', '--apply', action='store_true')
    channel_resolve_p.set_defaults(func=commands['command_channel_resolve'])

    channel_plan_p = channel_sub.add_parser('plan', parents=[common])
    channel_plan_p.add_argument('channel')
    channel_plan_p.add_argument('--operation', choices=('apply', 'publish'), default='apply')
    channel_plan_p.add_argument('--operation-id')
    channel_plan_p.set_defaults(func=commands['command_channel_plan'])

    channel_apply_p = channel_sub.add_parser('apply', parents=[common])
    channel_apply_p.add_argument('channel')
    channel_apply_p.add_argument('--operation-id')
    channel_apply_p.add_argument('--plan-digest')
    channel_apply_p.add_argument('-a', '--apply', action='store_true')
    channel_apply_p.set_defaults(func=commands['command_channel_apply'])

    channel_publish_p = channel_sub.add_parser('publish', parents=[common])
    channel_publish_p.add_argument('channel')
    channel_publish_p.add_argument('--operation-id')
    channel_publish_p.add_argument('--plan-digest')
    channel_publish_p.add_argument('-a', '--apply', action='store_true')
    channel_publish_p.set_defaults(func=commands['command_channel_publish'])

    channel_close_p = channel_sub.add_parser('close', parents=[common])
    channel_close_p.add_argument('channel')
    channel_close_p.add_argument('-R', '--reason', default='closed')
    channel_close_p.add_argument('--delete-local', action='store_true')
    channel_close_p.add_argument('--operation-id')
    channel_close_p.add_argument('--plan-digest')
    channel_close_p.add_argument('-a', '--apply', action='store_true')
    channel_close_p.set_defaults(func=commands['command_channel_close'])

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
    channel_operation_list_p.set_defaults(func=commands['command_channel_operation_list'])
    channel_operation_show_p = channel_operation_sub.add_parser('show', parents=[common])
    channel_operation_show_p.add_argument('operation_id')
    channel_operation_show_p.set_defaults(func=commands['command_channel_operation_show'])
    channel_operation_reconcile_p = channel_operation_sub.add_parser(
        'reconcile', parents=[common]
    )
    channel_operation_reconcile_p.add_argument('operation_id')
    channel_operation_reconcile_p.add_argument('--plan-digest')
    channel_operation_reconcile_p.add_argument('-a', '--apply', action='store_true')
    channel_operation_reconcile_p.set_defaults(func=commands['command_channel_reconcile_outcome'])

    channel_receipt_p = channel_sub.add_parser('receipt')
    channel_receipt_sub = channel_receipt_p.add_subparsers(
        dest='channel_receipt_command', required=True
    )
    channel_receipt_show_p = channel_receipt_sub.add_parser('show', parents=[common])
    channel_receipt_show_p.add_argument('channel', nargs='?')
    channel_receipt_show_p.set_defaults(func=commands['command_channel_receipt_show'])

    channel_reconcile_p = channel_sub.add_parser('reconcile-outcome', parents=[common])
    channel_reconcile_p.add_argument('operation_id')
    channel_reconcile_p.add_argument('--plan-digest')
    channel_reconcile_p.add_argument('-a', '--apply', action='store_true')
    channel_reconcile_p.set_defaults(func=commands['command_channel_reconcile_outcome'])

    journal_p = sub.add_parser(
        'journal', help='inspect, snapshot, publish, pull, or schedule a journal repository'
    )
    journal_sub = journal_p.add_subparsers(dest='journal_command', required=True)
    journal_status_p = journal_sub.add_parser('status', parents=[common])
    journal_status_p.add_argument('-j', '--json', action='store_true')
    journal_status_p.set_defaults(func=commands['command_journal_status'])
    journal_snapshot_p = journal_sub.add_parser('snapshot', parents=[common])
    journal_snapshot_p.add_argument('--path', dest='paths', action='append', help='snapshot only this exact path; repeatable')
    journal_snapshot_p.add_argument('-a', '--apply', action='store_true')
    journal_snapshot_p.set_defaults(func=commands['command_journal_snapshot'])
    journal_publish_p = journal_sub.add_parser('publish', parents=[common])
    journal_publish_p.add_argument('--committed-only', action='store_true', help='publish existing commits without snapshotting working files')
    journal_publish_p.add_argument('-a', '--apply', action='store_true')
    journal_publish_p.set_defaults(func=commands['command_journal_publish'])
    journal_pull_p = journal_sub.add_parser(
        'pull', parents=[common],
        help='fast-forward the journal branch to the remote tip when it is a strict ancestor',
    )
    journal_pull_p.add_argument('-a', '--apply', action='store_true')
    journal_pull_p.add_argument(
        '--park-conflicts', action='store_true',
        help='with --apply, save conflicting local versions under the git dir, '
        'fast-forward, and three-way merge them back where possible',
    )
    journal_pull_p.set_defaults(func=commands['command_journal_pull'])
    journal_recover_p = journal_sub.add_parser('recover', parents=[common], help='replay unpublished diverged snapshots without publishing')
    journal_recover_p.add_argument('--apply', action='store_true')
    journal_recover_p.add_argument('--rollback', metavar='RECEIPT')
    journal_recover_p.set_defaults(func=commands['command_journal_recover'])
    journal_schedule_p = journal_sub.add_parser('schedule', parents=[common])
    journal_schedule_p.add_argument('schedule_command', choices=('install', 'status', 'remove'))
    journal_schedule_p.add_argument('--committed-only', action='store_true', help='schedule publication without working-tree snapshots')
    journal_schedule_p.add_argument('-a', '--apply', action='store_true')
    journal_schedule_p.set_defaults(func=commands['command_journal_schedule'])

    stack_p = sub.add_parser(
        'stack',
        aliases=['s', 'spoke'],
        help='inspect, create, edit, rebuild, push, or run git for one stack/spoke',
    )
    stack_sub = stack_p.add_subparsers(dest='stack_command', required=True)

    stack_list_p = stack_sub.add_parser('list', aliases=['ls'], parents=[common])
    stack_list_p.set_defaults(func=commands['command_stack_list'])

    stack_show_p = stack_sub.add_parser('show', aliases=['sh'], parents=[common])
    stack_show_p.add_argument('stack')
    stack_show_p.set_defaults(func=commands['command_stack_show'])

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
        '--before-stack', metavar='MEMBER',
        help='include a new v1 draft immediately before this integration member',
    )
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
    stack_create_p.set_defaults(func=commands['command_stack_create'])

    stack_promote_p = stack_sub.add_parser(
        'promote',
        aliases=['pro'],
        parents=[common],
        help='promote a draft stack and rename its branch for PR publication',
    )
    stack_promote_p.add_argument('stack')
    stack_promote_p.add_argument('-b', '--branch', help='published PR branch name (default: pr/<stack>)')
    stack_promote_p.set_defaults(func=commands['command_stack_promote'])

    stack_demote_p = stack_sub.add_parser(
        'demote',
        aliases=['dem'],
        parents=[common],
        help='demote a published stack to draft without renaming its branch',
    )
    stack_demote_p.add_argument('stack')
    stack_demote_p.set_defaults(func=commands['command_stack_demote'])

    stack_sync_p = stack_sub.add_parser('sync', parents=[common])
    stack_sync_p.add_argument('stack')
    stack_sync_p.set_defaults(func=commands['command_stack_sync'])

    stack_absorb_p = stack_sub.add_parser('absorb', parents=[common])
    stack_absorb_p.add_argument('stack')
    stack_absorb_p.add_argument('paths', nargs='*', help='optional pathspecs to absorb from the integration worktree')
    stack_absorb_p.add_argument('-s', '--staged', action='store_true', help='absorb staged changes instead of unstaged working tree changes')
    stack_absorb_p.add_argument('-N', '--no-amend', dest='amend', action='store_false', help='create a new stack commit instead of amending the stack tip')
    stack_absorb_p.add_argument('-m', '--message', help='commit message used with --no-amend')
    stack_absorb_p.add_argument('-w', '--worktree', help='stack branch worktree to reuse or create')
    stack_absorb_p.add_argument('-W', '--worktree-root', help='directory where stack absorb creates a worktree when needed')
    stack_absorb_p.add_argument('-f', '--force', action='store_true', help='allow absorbing when the current checkout is not the integration branch')
    stack_absorb_p.set_defaults(func=commands['command_stack_absorb'], amend=True)

    stack_set_p = stack_sub.add_parser('set', parents=[common])
    stack_set_p.add_argument('stack')
    stack_set_p.add_argument('specs', nargs='*')
    stack_set_p.add_argument('--target-branch', help='update delivery target metadata without rebuilding refs')
    stack_set_p.add_argument('--preserve-worktrees', metavar='REASON', help='leave governed worktrees untouched; shared tracked manifests may still be committed')
    stack_set_p.add_argument('--base', help='pin the base for exact history adoption')
    stack_set_p.add_argument('--adopt-tip', help='adopt this existing tip without rewriting its history')
    stack_set_p.add_argument('--plan-digest', help='digest of the exact adoption preview')
    stack_set_p.add_argument('--apply', action='store_true', help='apply the reviewed adoption plan')
    stack_set_p.add_argument(
        '--published',
        action='store_true',
        help="reset the stack's commits to its entry in the published coordination state",
    )
    stack_set_p.set_defaults(func=commands['command_stack_set'])

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
    stack_resolve_p.add_argument('--worktree', help='capture the staged resolution from a managed integration desk')
    stack_resolve_p.set_defaults(func=commands['command_stack_resolve_integration'])

    stack_classify_p = stack_sub.add_parser(
        'classify-integration',
        parents=[common],
        help='declare integration-only stack ownership without rebuilding refs',
    )
    stack_classify_p.add_argument('stack')
    stack_classify_p.add_argument('specs', nargs='+')
    stack_classify_p.add_argument('--plan-digest')
    stack_classify_p.add_argument('-a', '--apply', action='store_true')
    stack_classify_p.set_defaults(func=commands['command_stack_classify_integration'])

    stack_add_p = stack_sub.add_parser('add', parents=[common])
    stack_add_p.add_argument('stack')
    stack_add_p.add_argument('specs', nargs='+')
    stack_add_p.add_argument(
        '--at-source', nargs=2, metavar=('TIP', 'MANIFEST_SHA256'),
        help='bind recipe registration to an exact source ref and reviewed manifest file bytes',
    )
    stack_add_p.set_defaults(func=commands['command_stack_add'])

    stack_capture_p = stack_sub.add_parser(
        'capture-integration',
        aliases=['capture'],
        parents=[common],
        help='assign integration-first commits to a stack and materialize its branch',
    )
    stack_capture_p.add_argument('stack')
    stack_capture_p.add_argument('specs', nargs='+')
    stack_capture_p.add_argument(
        '--retain-captured-worktrees', metavar='REASON',
        help='recapture contiguous committed history without removing locked captured copies',
    )
    stack_capture_p.set_defaults(func=commands['command_stack_capture_integration'])

    stack_rebuild_p = stack_sub.add_parser('rebuild', aliases=['rb'], parents=[common])
    stack_rebuild_p.add_argument('stack')
    stack_rebuild_p.add_argument(
        '--create-only', action='store_true',
        help='require a missing source branch and create it atomically in plumbing mode',
    )
    add_rebuild_args(stack_rebuild_p)
    stack_rebuild_p.set_defaults(func=commands['command_stack_rebuild'])

    stack_push_p = stack_sub.add_parser('push', parents=[common])
    stack_push_p.add_argument('stack')
    add_push_args(stack_push_p)
    stack_push_p.add_argument(
        '--retain-captured-worktrees', metavar='REASON',
        help='preserve locked, clean captured copies whose HEAD exactly matches the published stack',
    )
    stack_push_p.add_argument(
        '--allow-equivalent-candidate', action='store_true',
        help='with retention, publish an adopted fast-forward candidate with the exact preserved lane tree',
    )
    stack_push_p.set_defaults(func=commands['command_stack_push'])

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
    stack_land_p.set_defaults(func=commands['command_stack_land'])

    stack_merge_pr_p = stack_sub.add_parser(
        'merge-pr', parents=[common],
        help='plan or apply a policy-bound GitHub PR merge',
    )
    stack_merge_pr_p.add_argument('stack')
    stack_merge_pr_p.add_argument('--operation-id')
    stack_merge_pr_p.add_argument('--plan-digest')
    stack_merge_pr_p.add_argument('-a', '--apply', action='store_true')
    stack_merge_pr_p.add_argument('-j', '--json', action='store_true')
    stack_merge_pr_p.set_defaults(func=commands['command_stack_merge_pr'])

    stack_release_desk_p = stack_sub.add_parser(
        'release-desk', parents=[common], help='preserve and release a stack replay desk',
    )
    stack_release_desk_p.add_argument('stack')
    stack_release_desk_p.add_argument('--legacy-worktree')
    stack_release_desk_p.add_argument('--reason', required=True)
    stack_release_desk_p.add_argument('--confirm-writers-stopped', action='store_true')
    stack_release_desk_p.add_argument('--plan-digest')
    stack_release_desk_p.add_argument('-a', '--apply', action='store_true')
    stack_release_desk_p.add_argument('-j', '--json', action='store_true')
    stack_release_desk_p.set_defaults(func=commands['command_stack_release_desk'])

    stack_archive_p = stack_sub.add_parser(
        'archive', parents=[common], help='retain source history and defer a stack without delivery claims',
    )
    stack_archive_p.add_argument('stack')
    stack_archive_p.add_argument('--reason', required=True)
    stack_archive_p.add_argument('--resume-intent', required=True)
    stack_archive_p.add_argument('--plan-digest')
    stack_archive_p.add_argument('-a', '--apply', action='store_true')
    stack_archive_p.add_argument('-j', '--json', action='store_true')
    stack_archive_p.set_defaults(func=commands['command_stack_archive'])

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
        help=(
            'closure proof: merged when every declared commit is reachable from the base; '
            'absorbed for squash/rebase delivery verified by final content; abandoned or a custom string otherwise'
        ),
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
        help='close without commit-ancestry proof; not needed for a verified --reason absorbed close',
    )
    stack_close_p.set_defaults(func=commands['command_stack_close'], delete_branch=False)

    stack_git_p = stack_sub.add_parser('git', aliases=['g'], parents=[common])
    stack_git_p.add_argument('stack')
    add_git_args(stack_git_p)
    stack_git_p.set_defaults(func=commands['command_stack_git'])

    int_p = sub.add_parser('int', aliases=['i'], help='inspect, align, rebuild, push, or run git for integration')
    int_sub = int_p.add_subparsers(dest='int_command', required=True)

    int_show_p = int_sub.add_parser('show', aliases=['sh'], parents=[common])
    int_show_p.set_defaults(func=commands['command_int_show'])

    int_sync_status_p = int_sub.add_parser('sync-status', parents=[common])
    int_sync_status_p.add_argument('-R', '--remote')
    int_sync_status_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    int_sync_status_p.add_argument('-j', '--json', action='store_true')
    int_sync_status_p.set_defaults(func=commands['command_int_sync_status'], fetch=True)

    int_align_remote_p = int_sub.add_parser('align-remote', parents=[common])
    int_align_remote_p.add_argument('-R', '--remote')
    int_align_remote_p.add_argument('-F', '--no-fetch', dest='fetch', action='store_false')
    int_align_remote_p.add_argument('-n', '--dry-run', action='store_true')
    int_align_remote_p.add_argument('-f', '--force', action='store_true')
    int_align_remote_p.set_defaults(func=commands['command_int_align_remote'], fetch=True)

    int_rebuild_p = int_sub.add_parser('rebuild', aliases=['rb'], parents=[common])
    add_rebuild_args(int_rebuild_p)
    int_rebuild_p.add_argument(
        '--reason',
        help=(
            'operator reason recorded with the integration rebuild; required for '
            'ai-managed repositories and when reconciling derived_paths narrowing'
        ),
    )
    int_rebuild_p.set_defaults(func=commands['command_int_rebuild'])

    int_push_p = int_sub.add_parser('push', parents=[common])
    add_push_args(int_push_p)
    int_push_p.add_argument(
        '--adopt-remote',
        metavar='TIP',
        help='first coordinated publish only: replace the existing remote integration '
             'branch, which must be at exactly this full commit id',
    )
    int_push_p.set_defaults(func=commands['command_int_push'])

    int_git_p = int_sub.add_parser('git', aliases=['g'], parents=[common])
    add_git_args(int_git_p)
    int_git_p.set_defaults(func=commands['command_int_git'])

    attach_command_behaviors(parser, command_behavior_table(commands))
    return parser


def entrypoint_behavior_table(commands, internal_writers=()):
    """Single registry for command and internal state-writer behavior."""
    table = {}

    def register(
        functions, *, mutates='never', manifest_mutates='never', remedy=False,
        command=True, primary_scope='product',
    ):
        for function in functions:
            if function in table:
                raise RuntimeError(f'duplicate command behavior: {function.__name__}')
            table[function] = {
                'command': command,
                'mutates': mutates,
                'manifestMutates': manifest_mutates,
                'primaryGuardRemedy': remedy,
                'primaryScope': primary_scope,
            }

    register((
        commands['command_repo_ls'],
        commands['command_repo_tracking_status'],
        commands['command_repo_authority_status'],
        commands['command_repo_pr_merge_policy_status'],
        commands['command_self_status'],
        commands['command_self_check_update'],
        commands['command_hooks_status'],
        commands['command_hooks_guard'],
        commands['command_hooks_worktree_guard'],
        commands['command_hooks_ref_guard'],
        commands['command_handoff'],
        commands['command_status'],
        commands['command_worktree_list'],
        commands['command_validate'],
        commands['command_plan'],
        commands['command_check'],
        commands['command_ledger_show'],
        commands['command_manifest_compare'],
        commands['command_channel_list'],
        commands['command_channel_show'],
        commands['command_channel_contract'],
        commands['command_channel_diff'],
        commands['command_channel_plan'],
        commands['command_channel_operation_list'],
        commands['command_channel_operation_show'],
        commands['command_channel_receipt_show'],
        commands['command_journal_status'],
        commands['command_stack_list'],
        commands['command_stack_show'],
        commands['command_int_show'],
        commands['command_int_sync_status'],
    ))
    # The revision-provider command deliberately owns its narrower transaction
    # inside the backend, after request and lease validation have completed.
    register((commands['command_revision_provider'],), mutates='always', manifest_mutates='internal')
    register((commands['command_revision_provider_resolve_prepared'],), mutates='apply', manifest_mutates='internal')
    register((
        commands['command_repo_add'],
        commands['command_repo_set_manifest'],
        commands['command_repo_rm'],
        commands['command_worktree_lock'],
        commands['command_worktree_unlock'],
    ), mutates='always')
    register((commands['command_worktree_open'],), mutates='always', remedy=True)
    register((commands['command_stack_release_desk'],), mutates='apply')
    register((commands['command_stack_set'],), mutates='stack-set', manifest_mutates='stack-set')
    register((commands['command_stack_close'], commands['command_stack_sync']), mutates='always',
             manifest_mutates='always', primary_scope='metadata')
    register((
        commands['command_stack_create'],
        commands['command_stack_promote'],
        commands['command_stack_demote'],
        commands['command_stack_absorb'],
        commands['command_stack_resolve_integration'],
        commands['command_stack_add'],
        commands['command_coordination_provenance_reset'],
    ), mutates='always', manifest_mutates='always')
    register(
        (commands['command_stack_capture_integration'],),
        mutates='always',
        manifest_mutates='always',
        remedy=True,
    )
    register((
        commands['command_repo_authority_set'],
        commands['command_repo_pr_merge_policy_set'],
        commands['command_repo_pr_merge_policy_clear'],
        commands['command_stack_archive'],
    ), mutates='apply', manifest_mutates='apply', primary_scope='metadata')
    register((
        commands['command_repo_tracking_set'],
        commands['command_coordination_init'],
        commands['command_coordination_disable'],
        commands['command_coordination_compose'],
        commands['command_reconcile'],
        commands['command_resume'],
        commands['command_sync'],
        commands['command_publish'],
        commands['command_manifest_require_integration'],
        commands['command_channel_create'],
        commands['command_channel_add'],
        commands['command_channel_remove'],
        commands['command_channel_replace'],
        commands['command_channel_refresh'],
        commands['command_channel_promote'],
        commands['command_channel_resolve'],
        commands['command_channel_apply'],
        commands['command_channel_publish'],
        commands['command_channel_close'],
        commands['command_channel_reconcile_outcome'],
        commands['command_stack_classify_integration'],
        commands['command_stack_land'],
        commands['command_stack_merge_pr'],
        commands['command_worktree_release'],
        commands['command_gc'],
    ), mutates='apply', manifest_mutates='apply')
    register((commands['command_worktree_retire_copy'],), mutates='apply')
    register((
        commands['command_coordination_claims_backfill'],
        commands['command_coordination_provenance_supersede'],
    ), mutates='apply', manifest_mutates='apply')
    register((
        commands['command_journal_snapshot'],
        commands['command_journal_publish'],
        commands['command_journal_pull'], commands['command_journal_recover'],
    ), mutates='apply')
    register((commands['command_repo_mode_set'],), mutates='apply', manifest_mutates='apply')
    register((commands['command_manifest_recover_control'],), mutates='apply', manifest_mutates='apply', remedy=True)
    register((commands['command_manifest_recover_stack'],), mutates='apply', manifest_mutates='apply', remedy=True)
    # digest-heal is the one repair class that appends a local ledger event
    # alongside the remote state CAS.
    register((commands['command_coordination_repair'],), mutates='apply', manifest_mutates='apply')
    # Guard lifecycle writes the profile ledger under its own atomic writes, so it
    # is registered without joining the global manifest transaction.
    register(
        (commands['command_hooks_install'], commands['command_hooks_remove']),
        mutates='apply', manifest_mutates='internal', remedy=True,
    )
    register((
        commands['command_self_update'],
        commands['command_self_install_hooks'],
    ), mutates='execute')
    register((
        commands['command_int_align_remote'],
        commands['command_stack_push'],
        commands['command_int_rebuild'],
        commands['command_int_push'],
    ), mutates='execute', manifest_mutates='execute')
    register(
        (commands['command_stack_rebuild'],),
        mutates='execute',
        manifest_mutates='update-manifest',
    )
    register((commands['command_worktree_policy'],), mutates='apply')
    register((commands['command_self_mode'],), mutates='mode')
    register((commands['command_use'],), mutates='profile-selection')
    register((commands['command_replay_mode'],), mutates='mode-or-clear')
    register((commands['command_init'],), mutates='init', manifest_mutates='init')
    register(
        (commands['command_journal_schedule'],),
        mutates='schedule-apply', manifest_mutates='internal',
    )
    register((commands['command_stack_git'], commands['command_int_git']), mutates='git-passthrough')
    register(internal_writers, manifest_mutates='internal', command=False)
    return table


def command_behavior_table(commands):
    """Command-only projection of the authoritative entrypoint registry."""
    return {
        function: behavior
        for function, behavior in entrypoint_behavior_table(commands).items()
        if behavior['command']
    }


def command_parser_nodes(parser):
    stack = [parser]
    seen = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())


def attach_command_behaviors(parser, table):
    registered = set()
    for command_parser in command_parser_nodes(parser):
        function = command_parser._defaults.get('func')
        if function is None:
            continue
        behavior = table.get(function)
        if behavior is None:
            raise RuntimeError(f'missing command behavior: {function.__name__}')
        registered.add(function)
        command_parser.set_defaults(
            mutates=behavior['mutates'],
            manifest_mutates=behavior['manifestMutates'],
            primary_guard_remedy=behavior['primaryGuardRemedy'],
        )
    unused = sorted(function.__name__ for function in set(table) - registered)
    if unused:
        raise RuntimeError('unregistered command behavior(s): ' + ', '.join(unused))


def build_test_slot_parser():
    """Grammar for the separate cooperative test-slot entrypoint."""
    parser = argparse.ArgumentParser(prog='syncwheel-test-slot',
        description='Run or recover a reviewed cooperative job in one physical test slot.')
    parser.add_argument('action', choices=['status', 'inspect', 'run', 'recover', 'cleanup', '_worker'])
    parser.add_argument('workspace')
    parser.add_argument('input', nargs='?', help='request JSON for inspect/run; recorded nonce for recover/cleanup')
    parser.add_argument('--timeout', type=int, default=300)
    return parser
