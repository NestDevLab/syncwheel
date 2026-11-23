#!/usr/bin/env python3
import argparse
import copy
import contextlib
from contextvars import ContextVar
import datetime
import errno
import fnmatch
import functools
import hashlib
import importlib.metadata
import json
import os
import re
import signal
import sqlite3
import shutil
import shlex
import socket
import stat
import struct
import tempfile
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Direct-file consumers load this facade without putting scripts/ on sys.path.
# Resolve the installed sibling modules through the same directory as this file.
_MODULE_DIRECTORY = str(Path(__file__).resolve().parent)
if _MODULE_DIRECTORY not in sys.path:
    sys.path.insert(0, _MODULE_DIRECTORY)

from syncwheel_integration_history import (
    IntegrationHistory,
)

from syncwheel_projection import (
    ProjectionService,
    checkout_path_fingerprint,
    checkout_path_observation,
    published_integration_replay_target,
    published_integration_tip_reuse_fallback,
    published_integration_tip_reuse_refusal,
    regular_checkout_path_bytes,
)

import syncwheel_manifest_store as _manifest_storage
from syncwheel_desks import DeskLifecycle
from syncwheel_stack_recovery import COMMIT_ID as RECOVERY_COMMIT_ID
from syncwheel_stack_recovery import MANIFEST as STACK_RECOVERY_MANIFEST
from syncwheel_stack_recovery import StackRecovery
from syncwheel_retire_copy import (
    RetireCopy,
    observed_file as retire_copy_observed_file,
    remedy as retire_copy_remedy,
    require_no_visible_lane_users,
    stat_key as retire_copy_stat_key,
)
from syncwheel_manifest_store import (
    ManifestStore,
    CONTROL_MANIFEST_EXPLICIT_RECOVERY_IDENTITY_VERSION,
    CONTROL_MANIFEST_IDENTITY_VERSION,
    CONTROL_MANIFEST_IDENTITY_VERSIONS,
    CONTROL_MANIFEST_LEGACY_IDENTITY_VERSION,
    ENV_TEST_CONTROL_MANIFEST_PAUSE,
    ENV_TEST_CONTROL_MANIFEST_READY,
    ENV_TEST_CONTROL_MANIFEST_RELEASE,
    ENV_TEST_CONTROL_MANIFEST_SIGKILL,
    LEDGER_SEGMENT_MAX_EVENTS,
    _recover_ledger_tail_unlocked,
    _repair_incomplete_ledger_tail,
    _write_ledger_checkpoint_unlocked,
    active_manifest_write_transaction,
    active_manifest_write_transaction_for_repo,
    canonical_manifest_file_text,
    channel_mutation_checkpoint,
    control_manifest_difference,
    control_manifest_divergence_remedy,
    control_manifest_event_idempotency_key,
    control_manifest_intent_idempotency_key,
    control_manifest_io_checkpoint,
    control_manifest_obstruction_refusal,
    control_manifest_obstruction_warning,
    control_manifest_operation_intent,
    control_manifest_operation_records,
    control_manifest_relative_path,
    control_manifest_source_allowance,
    ensure_directory_durable,
    explicit_control_recovery_intent,
    explicit_control_recovery_refusal,
    external_ledger_root,
    integration_manifest_path,
    is_external_manifest_path,
    is_personal_manifest_path,
    ledger_checkpoint_path,
    ledger_checkpoints_dir,
    ledger_events_dir,
    ledger_fsync_directory_path,
    ledger_io_checkpoint,
    ledger_root,
    ledger_write_lock,
    load_control_manifest_events,
    load_ledger_events,
    load_ledger_state,
    load_manifest,
    load_repaired_ledger_events,
    manifest_policy_from_file,
    next_ledger_segment_path,
    pending_control_manifest_intents,
    pending_explicit_control_recoveries,
    preflight_control_manifest_digest,
    recover_ledger_tail,
    refuse_duplicate_terminal_publication_event,
    repo_relative_path,
    require_locked_manifest_observation,
    require_manifest_transaction_current,
    restore_guarded_control_manifests,
    save_manifest,
    status_line_paths,
    write_ledger_checkpoint,
)

from syncwheel_stacks import (
    StackLifecycle,
    ordinary_empty_draft_close,
    stack_closed_payload,
    manifest_scope_options,
    stack_close_command,
    draft_create_retry_command,
    coordination_draft_recovery_ref,
    draft_branch_creation_checkpoint,
    draft_push_refusal,
    stack_push_remote,
    channel_ids_referencing_stack,
    coordinated_push_remote,
    passthrough_args,
    push_args_with_options,
)

from syncwheel_revision_backend import (
    SyncwheelRevisionBackend as _RevisionBackend,
    JournalRevisionBackend as _JournalRevisionBackend,
)
from syncwheel_manifest import integration_composition_digest, SYNCWHEEL_TRACKING_GIT_TRACKED
from syncwheel_ledger import manifest_stack_history_summary, manifest_event_payload
from syncwheel_files import _write_all, fsync_directory_path
from syncwheel_provenance import (
    ProvenanceStore,
    derived_projection_paths_digest,
    DERIVED_PATHS_REBUILD_REASON,
    DERIVED_PATHS_TRAILER,
    DERIVED_PROJECTION_TRAILER,
    DERIVED_PROVENANCE_DISCARD_REASON,
    DERIVED_PROVENANCE_OVERRIDE_FIELDS,
    DERIVED_PROVENANCE_RESET_REASON,
    DERIVED_PROVENANCE_STORE_VERSION,
    apply_derived_provenance_overrides,
    default_derived_provenance_store,
    derived_paths_rebuild_remedy,
    derived_provenance_reset_remedy,
    normalize_derived_provenance_store,
    projected_derived_provenance_store,
    resolve_derived_provenance_overrides,
)
from syncwheel_contracts import MANAGED_REF_MOVE_AUTH_ENV, INTEGRATION_STRATEGIES

from syncwheel_publication import (
    PublicationCoordinator,
    COORDINATION_GIT_IDENTITY_CONFIG,
    COORDINATION_GIT_IDENTITY_ENV,
    COORDINATION_LEASE_SECONDS,
    COORDINATION_PUBLICATION_LOCK_STALE_SECONDS,
    COORDINATION_PUBLICATION_LOCK_TIMEOUT_SECONDS,
    STACK_OWNER_STATE_SCAN_LIMIT,
    _COORDINATION_PUBLICATION_LOCK_STATE,
    coordination_manifest_remote_roles,
    coordination_publication_busy_error,
    coordination_publication_lock_holders,
    coordination_tombstones,
    coordination_unclaimed_owned_refs,
    delivery_ref_names,
    format_stack_owner,
    local_lease_is_active,
    managed_ref_names,
    report_advisory_unclaimed_owned_refs,
)
from syncwheel_manifest import (
    validate_stack_adoption,
    manifest_digest,
    canonical_manifest_json,
    channel_composition_digest,
)
from syncwheel_ledger import manifest_channel_history_summary

import syncwheel_publication_intents as _publication_policy
from syncwheel_publication_intents import (
    PublicationIntents,
    COORDINATION_CLAIM_FILE,
    COORDINATION_CLAIM_HISTORY_SCAN_LIMIT,
    EXPECTED_COORDINATION_STATE_UNSET,
    VERIFIED_STACK_PUBLICATION_RECOVERY,
    coordinated_publish_remote_failure,
    coordinated_publish_retry_hint,
    coordination_claim_proves_operation,
    coordination_operation_touched_refs,
    coordination_scope_remedy,
    coordination_state_document_matches_operation,
    pending_coordination_publication_remedy,
    publication_intent_owner_recovers,
    require_stack_publication_refs,
    stack_publication_source_refs,
)
from syncwheel_coordination import (
    COORDINATION_CLAIM_PREFIX,
    coordination_claim_ref,
    coordination_tombstone_ref,
    coordination_identity_snapshot,
    coordination_state_operation_manifest_digest,
)

from syncwheel_coordination_state import (
    CoordinationStateReader,
    COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST,
    COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT,
    COORDINATION_STATE_DIGEST_FORM_ORPHANED,
    COORDINATION_STATE_FILE,
)
from syncwheel_manifest import coordination_state_ref

from syncwheel_worktrees import (
    GovernedWorktrees,
    GOVERNED_WORKTREE_LOCK_STALE_SECONDS,
    GOVERNED_WORKTREE_LOCK_TIMEOUT_SECONDS,
    GOVERNED_WORKTREE_REGISTRY_VERSION,
    GOVERNED_WORKTREE_TERMINAL_EVENT_TYPES,
    GOVERNED_WORKTREE_WARNING_DETAIL_LIMIT,
    RESET,
    YELLOW,
    _REGISTRY_EXPECTED_DIGEST_UNSET,
    completed_governed_worktree_lane,
    governed_worktree_cleanup_intent_payload,
    governed_worktree_cleanup_key,
    governed_worktree_cleanup_lock_reason,
    governed_worktree_generation_token,
    governed_worktree_lane_lease_expired,
    governed_worktree_owner_is_dead,
    governed_worktree_pending_remedy,
    governed_worktree_reaped_payload,
    governed_worktree_recovery_ref,
    governed_worktree_registry_busy_error,
    governed_worktree_registry_file_digest,
    governed_worktree_registry_payload,
    governed_worktree_release_note_key,
    prepare_governed_worktree_cleanup,
)
from syncwheel_manifest import (
    syncwheel_worktree_root,
    resolve_worktree_root_path,
    effective_worktree_root,
    path_is_relative_to,
    require_stack,
)
from syncwheel_contracts import iso_utc_now, parse_coordination_timestamp

from syncwheel_replay import (
    ReplayEngine,
    replay_hygiene_env,
    requested_replay_mode,
    integration_supports_plumbing,
    require_nonempty_desk_stack_rebuild,
    replay_step,
    replay_exec_step,
    replay_shell_step,
    replay_step_render,
    quoted,
    quoted_with_env,
    git_command_cwd,
    shell_ref,
    empty_replay_shell_error,
    merge_tree_conflict_paths,
    replay_conflict_retry_command,
    require_replay_success,
    replay_target,
    bind_ephemeral_worktree,
    syncwheel_timestamp,
    backup_branch_name,
    EPHEMERAL_WORKTREE_PLACEHOLDER,
    EMPTY_REPLAY_MESSAGE,
)
from syncwheel_manifest import (
    stack_integration_base_commits,
    bound_merge_stack_resolution_commits,
    stack_integration_only_commits,
    stack_integration_commits,
    stack_delivery_identity,
    stack_map,
)
from syncwheel_contracts import PROFILE_FILENAME, ZERO_OBJECT_ID

from syncwheel_journal_workflow import JournalWorkflow

import syncwheel_journal as _journal
from syncwheel_journal import (
    journal_path_matches,
    journal_path_sensitive,
    journal_secret_reason,
    journal_file_fingerprint,
    journal_read_admitted_file,
    JOURNAL_SENSITIVE_PARTS,
    JOURNAL_SECRET_PATTERNS,
)
from syncwheel_ledger import (
    default_ledger_state,
    branch_ref_matches,
    apply_ledger_event,
    reduce_ledger_state,
    LEDGER_SCHEMA_VERSION,
    COORDINATION_PUBLICATION_TERMINAL_EVENT_TYPES,
)

import syncwheel_coordination as _coordination
from syncwheel_coordination import (
    COORDINATION_REMOTE_ROLE_CANONICAL,
    COORDINATION_REMOTE_ROLE_PUBLICATION,
    COORDINATION_STATE_SCHEMA_VERSION,
    COORDINATION_STATE_SCHEMA_VERSION_CHANNELS,
    COORDINATION_STATE_SCHEMA_VERSION_RESOLUTIONS,
    additive_coordination_snapshot_delta,
    changed_snapshot_fields,
    changed_stack_ids,
    channel_snapshot_map,
    coordination_integration_resolutions,
    coordination_snapshot_managed_ref_names,
    coordination_snapshot_schema,
    local_proposal_diff,
    local_proposal_lines,
    merge_coordination_snapshots,
    snapshot_globals,
    stack_snapshot_map,
)

import syncwheel_cli as _cli
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

from syncwheel_errors import (
    ControlManifestAlignmentDrift,
    CoordinationRemoteRejection,
    DerivedProvenanceDurabilityError,
    IntegrationReconciliationLeaseDrift,
    ManifestDurabilityError,
    SyncwheelError,
)

from syncwheel_manifest import (
    AUTHORITY_CLASSES,
    AUTHORITY_CLASS_DESTRUCTIVE_REWRITE,
    AUTHORITY_CLASS_RUNTIME_CHANGE,
    AUTHORITY_CLASS_SOURCE_CHANGE,
    AUTHORITY_MODES,
    AUTHORITY_MODE_AI_MANAGED,
    AUTHORITY_MODE_HUMAN_GATED,
    CHANNEL_LIFECYCLES,
    COORDINATION_CLAIM_MODES,
    COORDINATION_MODES,
    COORDINATION_STATE_PREFIX,
    DEFAULT_COORDINATION_GC,
    DEFAULT_INTEGRATION_BRANCH,
    DEFAULT_JOURNAL_INTERVAL,
    DEFAULT_SYNCWHEEL_WORKTREE_ROOT,
    DERIVED_OPERATION_ID,
    DERIVED_PROVENANCE_FIELDS,
    GOVERNED_WORKTREE_CAPACITY_ENFORCEMENTS,
    GOVERNED_WORKTREE_DEFAULT_CAPACITY,
    INTEGRATION_MEMBERSHIP_LEGACY,
    INTEGRATION_MEMBERSHIP_POLICIES,
    INTEGRATION_MEMBERSHIP_REQUIRED,
    MANIFEST_VERSIONS,
    MANIFEST_VERSION_CHANNELS,
    MANIFEST_VERSION_COORDINATED,
    REPLAY_MODE_CHOICES,
    REPOSITORY_MODES,
    STACK_STATES,
    SYNCWHEEL_TRACKING_LOCAL_ONLY,
    SYNCWHEEL_TRACKING_VALUES,
    active_coordination_config,
    authority_allows,
    canonical_json_digest,
    channel_pin_digest,
    control_manifest_stays_untracked,
    coordination_config,
    coordination_is_active,
    default_authority_policy,
    default_coordination_id,
    default_coordination_state_branch,
    derive_stack_dependencies,
    disabled_coordination_config,
    governed_worktree_capacity,
    manifest_authority,
    manifest_is_local_only,
    normalize_authority_policy,
    normalize_channel_entry,
    normalize_channel_resolution,
    normalize_channel_timestamp,
    normalize_coordination,
    normalize_coordination_gc,
    normalize_coordination_id,
    normalize_derived_provenance,
    normalize_governed_worktree_capacity,
    normalize_integration_membership,
    normalize_landing_policy,
    normalize_landing_requirement,
    normalize_manifest_channels,
    normalize_manifest_data,
    normalize_replay_mode,
    normalize_syncwheel_tracking,
    normalize_syncwheel_worktree_root,
    safe_ref_segment,
    stack_base_owner_id,
    symbolic_base_branch_for_remote,
    validate_channel_branch_ownership,
    validate_channel_dependency_order,
    validate_channel_stack_authority,
    validate_stack_dependency_graph,
)

from syncwheel_index import (
    INDEX_CACHE_ONLY_EXTENSIONS,
    INDEX_ENTRY_ASSUME_VALID,
    INDEX_ENTRY_EXTENDED,
    INDEX_ENTRY_NAME_MASK,
    INDEX_OBJECT_FORMATS,
    INDEX_SEMANTIC_DIGEST_DOMAIN,
    IndexLease,
    IndexObservation,
    _index_varint,
    index_semantic_digest,
    parse_index_semantics,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX snapshotting reports unsupported at runtime
    fcntl = None


ENV_REGISTRY_PATH = 'SYNCWHEEL_REPO_REGISTRY'
ENV_REPO = 'SYNCWHEEL_REPO'
ENV_PERSONAL = 'SYNCWHEEL_PERSONAL'
ENV_UPDATE_MODE = 'SYNCWHEEL_UPDATE_MODE'
ENV_UPDATE_INTERVAL_SECONDS = 'SYNCWHEEL_UPDATE_INTERVAL_SECONDS'
ENV_UPDATE_STATE_PATH = 'SYNCWHEEL_UPDATE_STATE_PATH'
ENV_UPDATE_SETTINGS_PATH = 'SYNCWHEEL_UPDATE_SETTINGS_PATH'
ENV_REMOTE_VERSION_URL = 'SYNCWHEEL_REMOTE_VERSION_URL'
ENV_UV_TOOL_SOURCE = 'SYNCWHEEL_UV_TOOL_SOURCE'
MANIFEST_VERSION_LEGACY = 1
COORDINATION_REPAIR_PLAN_SCHEMA_VERSION = 1
COORDINATION_COMPOSE_PLAN_SCHEMA_VERSION = 1
COORDINATION_REPAIR_TREE_EQUIVALENT_PROOF = 'exact-tree-equality'
COORDINATION_REPAIR_FAST_FORWARD_PROOF = 'exact-fast-forward-ancestry'
COORDINATION_REPAIR_MISSING_REF_PROOF = 'exact-missing-ref-create'
COORDINATION_REPAIR_DIGEST_MIGRATION_PROOF = 'exact-control-manifest-digest'
COORDINATION_REPAIR_DIGEST_MIGRATION_CLASS = 'legacy-digest-migration'
COORDINATION_REPAIR_DIGEST_MIGRATION_PRECONDITION = (
    'exact-control-manifest-digest-and-state-only-cas'
)
COORDINATION_REPAIR_DIGEST_HEAL_PROOF = 'exact-recomputed-control-manifest-digest'
COORDINATION_REPAIR_DIGEST_HEAL_CLASS = 'digest-heal'
COORDINATION_REPAIR_DIGEST_HEAL_PRECONDITION = (
    'exact-recomputed-control-manifest-digest-and-state-only-cas'
)
COORDINATION_REPAIR_MAX_ADVANCE_COMMITS = 1024
CONTROL_MANIFEST_GIT_IDENTITY_ENV = {
    'GIT_AUTHOR_NAME': 'Syncwheel Control',
    'GIT_AUTHOR_EMAIL': 'control@syncwheel.invalid',
    'GIT_COMMITTER_NAME': 'Syncwheel Control',
    'GIT_COMMITTER_EMAIL': 'control@syncwheel.invalid',
}
LEGACY_SYNCWHEEL_WORKTREE_ROOTS = ('var/syncwheel',)
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
_READONLY_DIAGNOSTICS = ContextVar('readonly_diagnostics', default=False)
_EXPLICIT_REF_AUTHORIZATION = ContextVar('explicit_ref_authorization', default=False)
SYNCWHEEL_OWNS_REF_MOVES = False
SYNCWHEEL_REF_AUTH_REPO = None
SYNCWHEEL_REF_AUTH_TTL_SECONDS = 30
SYNCWHEEL_REF_AUTH_OPERATION_TTL_SECONDS = 3600
FALLBACK_GIT_IDENTITY_CONFIG = [
    '-c',
    'user.name=Syncwheel',
    '-c',
    'user.email=syncwheel@example.com',
]
WARNED_GIT_IDENTITY_PATHS = set()
COMMIT_CREATING_GIT_ACTIONS = {'cherry-pick', 'commit', 'commit-tree', 'merge', 'revert'}
CHANNEL_PLAN_SCHEMA_VERSION = 1
STACK_LAND_PLAN_SCHEMA_VERSION = 1
GITHUB_PR_MERGE_PLAN_SCHEMA_VERSION = 1
GITHUB_PR_MERGE_POLICY_KEY = 'github_pr_merge'
GITHUB_PR_MERGE_BYPASSES = {'private_free_rules', 'required_reviews'}
GITHUB_PR_MERGE_ADAPTER_TIMEOUT_SECONDS = 60
GITHUB_PR_MERGE_ADAPTER_MAX_OUTPUT = 1_048_576
GOVERNED_WORKTREE_DEFAULT_LEASE_SECONDS = 120 * 60
GOVERNED_WORKTREE_LOCK_INCOMPLETE_GRACE_SECONDS = 0.25
PENDING_STACK_LIST_LIMIT = 2600
PENDING_RESET_LIST_LIMIT = 600
PENDING_STACK_COMMIT_LIMIT = 5
GOVERNED_WORKTREE_REAP_PENDING_REASONS = frozenset({
    'reaping',
    'worktree_remove_failed',
    'branch_delete_failed',
    'recovery_ref_moved',
    'archive_push_failed',
    'registration_mismatch',
    'ledger_pending',
})
JOURNAL_FORBIDDEN_COMMANDS = {
    'stack', 's', 'spoke', 'int', 'i', 'publish', 'sync', 'reconcile', 'rec',
    'resume', 'coordination', 'coord', 'handoff', 'gc', 'worktree', 'wt',
    'plan', 'pl', 'manifest', 'm', 'channel', 'ch',
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


# Git exports these to its hooks. Syncwheel always names the repository it means
# through cwd or -C, so inheriting them would point a child Git at another
# worktree's git dir or index and rewrite it; an explicit env= override wins.
AMBIENT_GIT_LOCATION_ENV = (
    'GIT_DIR',
    'GIT_COMMON_DIR',
    'GIT_WORK_TREE',
    'GIT_INDEX_FILE',
    'GIT_PREFIX',
    'GIT_NAMESPACE',
)


def managed_process_env(extra=None, authorize=True):
    process_env = os.environ.copy()
    for name in AMBIENT_GIT_LOCATION_ENV:
        process_env.pop(name, None)
    diagnostics = _READONLY_DIAGNOSTICS.get()
    explicit_authorization = _EXPLICIT_REF_AUTHORIZATION.get()
    if explicit_authorization:
        process_env.pop(MANAGED_REF_MOVE_AUTH_ENV, None)
    if (not diagnostics and not explicit_authorization and authorize
            and SYNCWHEEL_OWNS_REF_MOVES and SYNCWHEEL_REF_AUTH_REPO):
        process_env[MANAGED_REF_MOVE_AUTH_ENV] = authorize_ref_move(SYNCWHEEL_REF_AUTH_REPO)
    if extra:
        process_env.update(extra)
    if diagnostics:
        process_env.pop(MANAGED_REF_MOVE_AUTH_ENV, None)
        process_env.update(GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='')
    return process_env


# These never reach pre-commit or reference-transaction, so a ref-move
# authorization (a synced file write) would go unused.
READ_ONLY_GIT_COMMANDS = frozenset({
    'cat-file', 'check-ignore', 'check-ref-format', 'diff', 'diff-files',
    'diff-index', 'diff-tree', 'for-each-ref', 'log', 'ls-files', 'ls-remote',
    'ls-tree', 'merge-base', 'patch-id', 'rev-list', 'rev-parse', 'show',
    'show-ref',
})
READ_ONLY_GIT_SUBCOMMANDS = {
    'config': frozenset({'--get', '--get-all', '--get-regexp'}),
    'remote': frozenset({'get-url'}),
    'worktree': frozenset({'list'}),
}
GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({'-C', '-c', '--git-dir', '--work-tree'})


def git_command_is_read_only(cmd):
    if not cmd or cmd[0] != 'git':
        return False
    index = 1
    while index < len(cmd) and cmd[index].startswith('-'):
        option = cmd[index]
        if option in GIT_GLOBAL_OPTIONS_WITH_VALUE:
            index += 2
        elif option.startswith(('--git-dir=', '--work-tree=')) or option == '--no-pager':
            index += 1
        else:
            return False
    if index >= len(cmd):
        return False
    subcommand, rest = cmd[index], cmd[index + 1:]
    if subcommand in READ_ONLY_GIT_COMMANDS:
        return True
    if subcommand == 'remote' and not rest:
        return True
    return bool(rest) and rest[0] in READ_ONLY_GIT_SUBCOMMANDS.get(subcommand, ())


def run(cmd, cwd=None, check=True, input_text=None, env=None, authorize=True):
    process_env = managed_process_env(
        env, authorize=authorize and not git_command_is_read_only(cmd)
    )
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


def replay_commit_env(repo_root, commit):
    return _replay_engine(repo_root).replay_commit_env(
        commit,
    )


def control_manifest_commit_env(repo_root, parent, identity_version):
    """Fixed-identity control-commit environment, dated from its parent.

    Version 1 recomputes the legacy formula that copied the parent's identity.
    The encoding pin matters: another i18n.commitEncoding adds an encoding
    header and changes the SHA that every consumer recomputes.
    """
    if identity_version == CONTROL_MANIFEST_LEGACY_IDENTITY_VERSION:
        return replay_commit_env(repo_root, parent)
    timestamp = git(repo_root, 'show', '-s', '--format=%ct', parent).stdout.strip()
    env = replay_hygiene_env()
    env.update({
        'GIT_CONFIG_COUNT': '3',
        'GIT_CONFIG_KEY_2': 'i18n.commitEncoding',
        'GIT_CONFIG_VALUE_2': 'UTF-8',
        # Git applies this after GIT_CONFIG_*, so an ambient value would win.
        'GIT_CONFIG_PARAMETERS': '',
    })
    env.update(CONTROL_MANIFEST_GIT_IDENTITY_ENV)
    env.update({
        'GIT_AUTHOR_DATE': f'{timestamp} +0000',
        'GIT_COMMITTER_DATE': f'{timestamp} +0000',
    })
    return env


def get_repo_root(explicit=None):
    cwd = explicit or os.getcwd()
    result = run(['git', 'rev-parse', '--show-toplevel'], cwd=cwd)
    return Path(result.stdout.strip())


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
    key = ('common_dir', str(repo_root))
    if key in _RUN_MEMO:
        return _RUN_MEMO[key]
    result = git(repo_root, 'rev-parse', '--git-common-dir')
    path = Path(result.stdout.strip())
    common = (Path(repo_root) / path).resolve() if not path.is_absolute() else path.resolve()
    _RUN_MEMO[key] = common
    return common


def primary_guard_path(repo_root):
    return git_common_dir(repo_root) / 'syncwheel' / 'guard.json'


def manifest_for_guard(repo_root):
    manifest, _ = require_manifest(repo_root, str(repo_root), None, None)
    return manifest


def primary_guard_validation_error(payload):
    if not isinstance(payload, dict):
        return 'primary guard configuration must be a JSON object'
    if type(payload.get('version')) is not int or payload['version'] != 1:
        return 'primary guard configuration has an unsupported or missing version'
    branch = payload.get('integrationBranch')
    if not isinstance(branch, str) or not branch.strip():
        return 'primary guard configuration requires a non-empty integrationBranch'
    enabled = payload.get('enabled')
    if not isinstance(enabled, bool):
        return 'primary guard configuration requires boolean enabled state'
    reason = payload.get('reason')
    if not enabled and (not isinstance(reason, str) or not reason.strip()):
        return 'disabled primary guard configuration requires a non-empty reason'
    return None


def inspect_primary_guard(repo_root):
    path = primary_guard_path(repo_root)
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return None, 'primary guard configuration is missing'
    except json.JSONDecodeError:
        return None, 'primary guard configuration contains invalid JSON'
    except ValueError:
        return None, 'primary guard configuration is unreadable'
    except OSError:
        return None, 'primary guard configuration is unreadable'
    error = primary_guard_validation_error(payload)
    return (None, error) if error else (payload, None)


def load_primary_guard(repo_root):
    guard, _ = inspect_primary_guard(repo_root)
    return guard


GUARDED_BRANCH_REF_PREFIX = 'refs/heads/'


def guardable_branch_ref(ref):
    """The guard only ever protects a branch ref.

    Remote-tracking refs, tags, notes, stash and the per-worktree pseudo-refs
    git writes during fetch, merge, rebase or worktree creation can never be the
    integration ref, so they stay outside the guard even when its configuration
    cannot be read.
    """
    return (
        ref.startswith(GUARDED_BRANCH_REF_PREFIX)
        and len(ref) > len(GUARDED_BRANCH_REF_PREFIX)
    )


def packed_ref_values(repo_root):
    path = git_common_dir(repo_root) / 'packed-refs'
    try:
        text = path.read_text(encoding='utf-8')
    except (FileNotFoundError, UnicodeDecodeError):
        return {}
    values = {}
    for line in text.splitlines():
        if not line or line[0] in '#^':
            continue
        parts = line.split(' ', 1)
        if len(parts) == 2:
            values[parts[1]] = parts[0]
    return values


def value_preserving_ref_updates(repo_root, updates):
    """Updates that leave each ref resolving to the value it has now.

    git pack-refs moves a loose ref in two transactions: `0 -> current` into
    packed-refs, then `current -> 0` to prune the loose copy. A real delete of
    a packed ref also carries a `0 -> 0` line for the packed store, and a
    loose-only ref has no packed value left behind, so neither qualifies.
    """
    refs = sorted({ref for _, _, ref in updates})
    listed = git(
        repo_root, 'for-each-ref', '--format=%(objectname) %(refname)', *refs,
        check=False,
    )
    current = {}
    if listed.returncode == 0:
        for line in listed.stdout.splitlines():
            oid, _, name = line.partition(' ')
            current[name] = oid
    packed = None
    preserved = set()
    for old, new, ref in updates:
        value = current.get(ref)
        if not value:
            continue
        if new.strip('0'):
            if new == value:
                preserved.add((old, new, ref))
            continue
        if old != value:
            continue
        if packed is None:
            packed = packed_ref_values(repo_root)
        if packed.get(ref) == value:
            preserved.add((old, new, ref))
    return preserved


def primary_guard_repair_remedy(error):
    return (
        f'{error}; run syncwheel hooks install --apply using the manifest that '
        'owns the primary checkout'
    )


def require_primary_guard(repo_root):
    guard, error = inspect_primary_guard(repo_root)
    if error:
        raise SyncwheelError(primary_guard_repair_remedy(error))
    return guard


def atomic_write_private_json(path, payload, *, indent=None):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=indent, sort_keys=True) + '\n').encode('utf-8')
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.tmp-', dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return path


def primary_guard_branch(manifest):
    if not isinstance(manifest, dict):
        return None
    section = 'journal' if manifest.get('repository_mode') == 'journal' else 'integration'
    config = manifest.get(section)
    branch = config.get('branch') if isinstance(config, dict) else None
    return branch if isinstance(branch, str) and branch.strip() else None


def primary_guard_payload(manifest, *, enabled=True, reason=None):
    branch = primary_guard_branch(manifest)
    if not isinstance(branch, str) or not branch:
        raise SyncwheelError('cannot persist primary guard without an integration branch')
    payload = {
        'version': 1,
        'integrationBranch': branch.strip(),
        'enabled': enabled,
        'reason': reason.strip() if isinstance(reason, str) else reason,
    }
    error = primary_guard_validation_error(payload)
    if error:
        raise SyncwheelError(error)
    return payload


def save_primary_guard(repo_root, manifest, *, enabled=True, reason=None):
    payload = primary_guard_payload(manifest, enabled=enabled, reason=reason)
    path = primary_guard_path(repo_root)
    atomic_write_private_json(path, payload, indent=2)
    return payload


def ref_auth_dir(repo_root):
    # authorize=False breaks the cycle: issuing an authorization needs this
    # directory, and managed_process_env() issues one for every other command.
    key = ('common_dir', str(repo_root))
    common = _RUN_MEMO.get(key)
    if common is None:
        result = run(
            ['git', 'rev-parse', '--git-common-dir'], cwd=repo_root, check=False,
            authorize=False,
        )
        if result.returncode != 0:
            raise SyncwheelError('cannot resolve git common directory for ref authorization')
        path = Path(result.stdout.strip())
        common = (Path(repo_root) / path).resolve() if not path.is_absolute() else path.resolve()
        _RUN_MEMO[key] = common
    return common / 'syncwheel' / 'ref-auth'


def ref_auth_events_path(repo_root):
    return git_common_dir(repo_root) / 'syncwheel' / 'ref-auth-events.jsonl'


def process_start_identity(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        closing = stat.rfind(')')
        fields = stat[closing + 2:].split()
        if closing > 0 and len(fields) > 19:
            return f'proc:{fields[19]}'
    except OSError:
        pass
    try:
        result = subprocess.run(
            ['ps', '-o', 'lstart=', '-p', str(pid)],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    started = ' '.join(result.stdout.split()) if result.returncode == 0 else ''
    return f'ps:{started}' if started else None


def authorize_ref_move(repo_root, reusable=False):
    directory = ref_auth_dir(repo_root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex + uuid.uuid4().hex
    pid = os.getpid()
    pid_start = process_start_identity(pid)
    if not pid_start:
        raise SyncwheelError('cannot establish process identity for ref authorization')
    payload = {
        'nonce': nonce,
        'pid': pid,
        'pidStart': pid_start,
        'expiresAt': time.time() + (
            SYNCWHEEL_REF_AUTH_OPERATION_TTL_SECONDS if reusable else SYNCWHEEL_REF_AUTH_TTL_SECONDS
        ),
        'remaining': ['pre-commit', 'reference-transaction'],
        'operation': 'syncwheel',
        'reusable': reusable,
    }
    path = directory / nonce
    atomic_write_private_json(path, payload)
    return nonce


@contextlib.contextmanager
def ref_move_authorization(repo_root, reusable=False):
    nonce = authorize_ref_move(repo_root, reusable=reusable)
    try:
        yield nonce
    finally:
        (ref_auth_dir(repo_root) / nonce).unlink(missing_ok=True)


def ref_move_authorized(repo_root, event):
    nonce = os.environ.get(MANAGED_REF_MOVE_AUTH_ENV)
    if not nonce or not re.fullmatch(r'[0-9a-f]{64}', nonce):
        return False
    path = ref_auth_dir(repo_root) / nonce
    try:
        payload = json.loads(path.read_text())
        pid = int(payload['pid'])
        pid_start = payload['pidStart']
        if (
            pid <= 0
            or not isinstance(pid_start, str)
            or not pid_start
            or not process_is_alive(pid)
            or process_start_identity(pid) != pid_start
            or payload['nonce'] != nonce
            or payload['expiresAt'] < time.time()
            or event not in payload['remaining']
        ):
            return False
        if payload.get('reusable'):
            return True
        payload['remaining'].remove(event)
        if payload['remaining']:
            atomic_write_private_json(path, payload)
        else:
            path.unlink()
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def process_is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def append_ref_auth_event(repo_root, event):
    path = ref_auth_events_path(repo_root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(event, sort_keys=True) + '\n').encode('utf-8')
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError('short ref authorization event write')
            offset += written
        os.fsync(descriptor)
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return event


def discard_stale_ref_authorization(repo_root, path, now):
    try:
        stale = path.lstat().st_mtime + SYNCWHEEL_REF_AUTH_TTL_SECONDS < now
    except OSError:
        return False
    if not stale:
        return False
    event = {
        'version': 1,
        'type': 'ref_authorization_discarded',
        'file': path.name,
        'reason': 'malformed_or_unreadable',
        'recordedAt': iso_utc_now(),
    }
    try:
        append_ref_auth_event(repo_root, event)
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def clear_ref_authorizations(repo_root):
    owner_pid = os.getpid()
    owner_start = process_start_identity(owner_pid)
    now = time.time()
    directory = ref_auth_dir(repo_root)
    if directory.exists():
        for path in directory.iterdir():
            try:
                payload = json.loads(path.read_text())
                nonce_pid = int(payload['pid'])
                nonce_start = payload['pidStart']
                if (
                    nonce_pid <= 0
                    or not isinstance(nonce_start, str)
                    or not nonce_start
                ):
                    raise ValueError('invalid ref authorization owner')
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                discard_stale_ref_authorization(repo_root, path, now)
                continue
            current_start = process_start_identity(nonce_pid)
            owned = (
                nonce_pid == owner_pid
                and owner_start is not None
                and nonce_start == owner_start
            )
            owner_is_gone = not process_is_alive(nonce_pid)
            owner_was_recycled = (
                current_start is not None and current_start != nonce_start
            )
            if owned or owner_is_gone or owner_was_recycled:
                path.unlink(missing_ok=True)


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


def managed_hook_cli_resolution(repo_root):
    """Resolve an executable CLI outside every checkout and configured lane root."""
    executable = shutil.which('syncwheel')
    if not executable:
        return None, 'stable syncwheel CLI is not on PATH'
    try:
        path = Path(executable).resolve(strict=True)
    except OSError:
        return None, 'stable syncwheel CLI cannot be resolved'
    if not path.is_file() or not os.access(path, os.X_OK):
        return None, f'syncwheel CLI is not executable: {path}'
    try:
        with path.open('rb') as handle:
            first_line = handle.readline(4096)
    except OSError:
        return None, f'syncwheel CLI cannot be read: {path}'
    if first_line.startswith(b'#!'):
        try:
            interpreter_parts = shlex.split(
                first_line[2:].decode('utf-8', errors='strict').strip()
            )
            interpreter = Path(interpreter_parts[0]).resolve(strict=True)
        except (IndexError, OSError, UnicodeDecodeError, ValueError):
            return None, f'syncwheel CLI has an unstable interpreter: {path}'
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            return None, f'syncwheel CLI interpreter is not executable: {interpreter}'
        if interpreter.name == 'env':
            return None, f'syncwheel CLI interpreter depends on PATH: {path}'

    repo_root = Path(repo_root).resolve()
    worktrees = get_worktrees(repo_root)
    primary = Path(worktrees[0]['path']).resolve() if worktrees else repo_root
    excluded_roots = [repo_root, primary, git_common_dir(repo_root)]
    excluded_roots.extend(
        Path(item['path']).resolve()
        for item in worktrees
        if item.get('path')
    )
    excluded_roots.append(primary / 'var')
    for manifest_root in dict.fromkeys((primary, repo_root)):
        manifest_path = manifest_root / '.syncwheel' / 'manifest.json'
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        try:
            excluded_roots.append(
                resolve_worktree_root_path(
                    primary, syncwheel_worktree_root(manifest)
                )
            )
        except SyncwheelError:
            continue
    for root in excluded_roots:
        if path_is_relative_to(path, root):
            return None, f'syncwheel CLI is inside repository or lane state: {path}'
    return shlex.quote(str(path)), None


def managed_hook_syncwheel_command(repo_root):
    """Return only a stable installed CLI command suitable for persisted hooks."""
    command, _ = managed_hook_cli_resolution(repo_root)
    return command


def missing_cli_hook_content(marker):
    return '#!/bin/sh\n' + marker + '\nprintf "%s\\n" "syncwheel guard degraded: install a stable syncwheel CLI, then run syncwheel hooks install --apply" >&2\nexit 1\n'


def managed_push_hook_content(repo_root, backup_exists):
    syncwheel_command = managed_hook_syncwheel_command(repo_root)
    if not syncwheel_command:
        return missing_cli_hook_content(MANAGED_PUSH_HOOK_MARKER)
    chain = (
        'chain_status=0\n'
        'if [ -x "$hook_dir/pre-push.syncwheel-chain" ]; then\n'
        '  "$hook_dir/pre-push.syncwheel-chain" "$@" <"$input"\n'
        '  chain_status=$?\n'
        'fi\n'
        if backup_exists else ''
    )
    return (
        '#!/bin/sh\n'
        f'{MANAGED_PUSH_HOOK_MARKER}\n'
        'set -u\n'
        'hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'input=$(mktemp "${TMPDIR:-/tmp}/syncwheel-pre-push.XXXXXX")\n'
        'trap \'rm -f "$input"\' EXIT HUP INT TERM\n'
        'cat >"$input"\n'
        + 'guard_status=0\n'
        + f'if [ ! -x {syncwheel_command} ]; then\n'
        + '  printf "%s\\n" "syncwheel guard degraded: stable syncwheel CLI is unavailable; repair the installation, then run syncwheel hooks install --apply" >&2\n'
        + '  guard_status=1\n'
        + 'else\n'
        + f'  {syncwheel_command} hooks guard --remote-name "${{1:-}}" '
        + '--remote-url "${2:-}" <"$input"\n'
        + '  guard_status=$?\n'
        + '  if [ "$guard_status" -eq 126 ] || [ "$guard_status" -eq 127 ]; then\n'
        + '    printf "%s\\n" "syncwheel guard degraded: stable syncwheel CLI failed to execute; repair the installation, then run syncwheel hooks install --apply" >&2\n'
        + '  fi\n'
        + 'fi\n'
        + chain
        + 'if [ "$guard_status" -ne 0 ]; then exit "$guard_status"; fi\n'
        + 'exit "${chain_status:-0}"\n'
    )


def managed_worktree_hook_content(repo_root, hook_name, backup_exists):
    if hook_name not in {'pre-commit', 'post-checkout'}:
        raise SyncwheelError(f'unsupported primary-checkout hook: {hook_name}')
    marker = (
        MANAGED_PRIMARY_PRE_COMMIT_MARKER
        if hook_name == 'pre-commit'
        else MANAGED_PRIMARY_POST_CHECKOUT_MARKER
    )
    chain = (
        'chain_status=0\n'
        f'if [ -x "$hook_dir/{hook_name}.syncwheel-chain" ]; then\n'
        f'  "$hook_dir/{hook_name}.syncwheel-chain" "$@"\n'
        '  chain_status=$?\n'
        'fi\n'
        if backup_exists else ''
    )
    syncwheel_command = managed_hook_syncwheel_command(repo_root)
    if not syncwheel_command:
        return missing_cli_hook_content(marker)
    return (
        '#!/bin/sh\n'
        f'{marker}\n'
        'set -u\n'
        'hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        + 'guard_status=0\n'
        + f'if [ ! -x {syncwheel_command} ]; then\n'
        + '  printf "%s\\n" "syncwheel guard degraded: stable syncwheel CLI is unavailable; repair the installation, then run syncwheel hooks install --apply" >&2\n'
        + '  guard_status=1\n'
        + 'else\n'
        + f'  {syncwheel_command} hooks worktree-guard --event {hook_name}\n'
        + '  guard_status=$?\n'
        + '  if [ "$guard_status" -eq 126 ] || [ "$guard_status" -eq 127 ]; then\n'
        + '    printf "%s\\n" "syncwheel guard degraded: stable syncwheel CLI failed to execute; repair the installation, then run syncwheel hooks install --apply" >&2\n'
        + '  fi\n'
        + 'fi\n'
        + chain
        + 'if [ "$guard_status" -ne 0 ]; then exit "$guard_status"; fi\n'
        + 'exit "${chain_status:-0}"\n'
    )


def managed_ref_move_hook_content(repo_root, backup_exists):
    chain = (
        'chain_status=0\n'
        'if [ -x "$hook_dir/reference-transaction.syncwheel-chain" ]; then\n'
        '  "$hook_dir/reference-transaction.syncwheel-chain" "$@" <"$input"\n'
        '  chain_status=$?\n'
        'fi\n'
        if backup_exists else ''
    )
    syncwheel_command = managed_hook_syncwheel_command(repo_root)
    if not syncwheel_command:
        return missing_cli_hook_content(MANAGED_REF_MOVE_MARKER)
    # Git runs this for every ref transaction. A missing guard is unsafe, so
    # only an explicit successful guard result permits a primary ref move.
    return (
        '#!/bin/sh\n'
        f'{MANAGED_REF_MOVE_MARKER}\n'
        'set -u\n'
        'hook_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1\n'
        'input=$(mktemp "${TMPDIR:-/tmp}/syncwheel-reference-transaction.XXXXXX")\n'
        'trap \'rm -f "$input"\' EXIT HUP INT TERM\n'
        'cat >"$input" || exit 1\n'
        'guard_status=0\n'
        + 'if [ "${1:-}" = "prepared" ] && [ -s "$input" ]; then\n'
        + f'  if [ ! -x {syncwheel_command} ]; then\n'
        + '  printf "%s\\n" "syncwheel guard degraded: stable syncwheel CLI is unavailable; repair the installation, then run syncwheel hooks install --apply" >&2\n'
        + '    guard_status=1\n'
        + '  else\n'
        + f'    {syncwheel_command} hooks ref-guard --phase "$1" <"$input"\n'
        + '    guard_status=$?\n'
        + '    if [ "$guard_status" -eq 126 ] || [ "$guard_status" -eq 127 ]; then\n'
        + '      printf "%s\\n" "syncwheel guard degraded: stable syncwheel CLI failed to execute; repair the installation, then run syncwheel hooks install --apply" >&2\n'
        + '    fi\n'
        + '  fi\n'
        + 'fi\n'
        + chain
        + 'if [ "$guard_status" -ne 0 ]; then exit "$guard_status"; fi\n'
        + 'exit "${chain_status:-0}"\n'
    )


def managed_hook_content(repo_root, hook_name, backup_exists):
    if hook_name == 'pre-push':
        return managed_push_hook_content(repo_root, backup_exists)
    if hook_name == 'reference-transaction':
        return managed_ref_move_hook_content(repo_root, backup_exists)
    return managed_worktree_hook_content(repo_root, hook_name, backup_exists)


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
    except (OSError, ValueError):
        metadata = None
    chained_digest = hashlib.sha256(backup.read_bytes()).hexdigest() if backup.is_file() else None
    managed_matches = bool(
        marker and metadata and metadata.get('digest') == digest
    )
    chain_matches = bool(
        metadata
        and metadata.get('chainedDigest') == chained_digest
        and (backup.exists() == (metadata.get('chainedDigest') is not None))
    )
    chain_repairable = bool(
        managed_matches
        and metadata.get('chainedDigest') is not None
        and backup.is_file()
        and not chain_matches
    )
    owned = bool(managed_matches and chain_matches)
    expected = hashlib.sha256(
        managed_hook_content(repo_root, hook_name, backup.exists()).encode()
    ).hexdigest()
    _, cli_cause = managed_hook_cli_resolution(repo_root)
    ready = owned and digest == expected and cli_cause is None
    degraded = not ready
    degraded_cause = None
    if cli_cause:
        degraded_cause = f'{hook_name}: {cli_cause}'
    elif not hook.exists():
        degraded_cause = f'{hook_name}: managed hook is missing'
    elif not owned:
        degraded_cause = f'{hook_name}: managed hook or chain differs from installed metadata'
    elif digest != expected:
        degraded_cause = f'{hook_name}: managed hook differs from expected content'
    return {
        'name': hook_name,
        'hooksPath': configured,
        'hooksDir': str(hooks_dir),
        'hook': str(hook),
        'exists': hook.exists(),
        'owned': owned,
        'ready': ready,
        'degraded': degraded,
        'degradedCause': degraded_cause,
        'marker': marker,
        'digest': digest,
        'expectedDigest': expected,
        'metadata': str(metadata_path),
        'managedMatches': managed_matches,
        'chained': backup.exists(),
        'chainedDigest': chained_digest,
        'chainMatches': chain_matches,
        'chainRepairable': chain_repairable,
        'status': (
            'degraded' if cli_cause else ('installed' if ready else
            ('stale' if owned else ('conflict' if hook.exists() or metadata_path.exists() else 'absent'))
            )
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
        'degraded': any(hook['degraded'] for hook in hooks.values()),
        'degradedCauses': [
            hook['degradedCause'] for hook in hooks.values()
            if hook['degradedCause']
        ],
        'expectedDigest': hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest(),
        'hooks': hooks,
    }


def primary_guard_manifest(repo_root):
    worktrees = get_worktrees(repo_root)
    if not worktrees or not worktrees[0].get('path'):
        return None, None, False
    root = Path(worktrees[0]['path'])
    path = root / '.syncwheel' / 'manifest.json'
    try:
        manifest, _ = _manifest_storage.load_manifest(root, path)
        return manifest, path, False
    except (OSError, ValueError, SyncwheelError, AttributeError, TypeError):
        return None, path, True


def guard_coordination_ref(manifest):
    if coordination_is_active(manifest):
        return coordination_state_ref(coordination_config(manifest))
    return None


def guard_projection_refs(manifest):
    refs = {f'refs/heads/{primary_guard_branch(manifest)}'}
    state_ref = guard_coordination_ref(manifest)
    if state_ref:
        refs.add(state_ref)
    return refs


def managed_push_guard_policy(repo_root, manifest, manifest_path=None):
    guard, guard_error = inspect_primary_guard(repo_root)
    selected_branch = primary_guard_branch(manifest)
    branch_matches = bool(
        guard and guard['integrationBranch'] == selected_branch
    )
    disabled = bool(guard and not guard['enabled'])
    reason = guard.get('reason') if guard else None
    tracking = manifest.get('syncwheel_tracking')
    required = tracking == SYNCWHEEL_TRACKING_GIT_TRACKED and bool(
        managed_ref_names(manifest)
        or coordination_is_active(manifest)
        or manifest.get('repository_mode') == 'journal'
    )
    bundle = managed_hook_bundle_status(repo_root)
    hook = bundle['hooks']['pre-push']
    degraded_causes = []
    shared_guard_matches = False
    if guard_error:
        degraded_causes.append(primary_guard_repair_remedy(guard_error))
    elif guard:
        primary_manifest, primary_path, invalid_primary = primary_guard_manifest(repo_root)
        primary_branch = primary_guard_branch(primary_manifest)
        guarded_branch = guard['integrationBranch']
        alternate_path = (
            manifest_path is not None and primary_path is not None
            and Path(manifest_path).resolve() != primary_path.resolve()
        )
        if invalid_primary and alternate_path:
            degraded_causes.append(primary_guard_repair_remedy(
                'cannot verify ownership from the primary checkout manifest'
            ))
        elif alternate_path and primary_branch == guarded_branch and manifest != primary_manifest:
            # A lane may consume shared stack sources. The publication claim
            # checks own stack ownership separately from this projection guard.
            selected_refs = guard_projection_refs(manifest)
            if manifest.get('repository_mode') != 'journal':
                selected_refs.update(managed_ref_names(manifest))
            overlap = selected_refs & guard_projection_refs(primary_manifest)
            if overlap:
                degraded_causes.append(
                    'selected manifest claims ' + ', '.join(sorted(overlap))
                    + ', which belongs to the primary checkout; choose distinct refs '
                    'for the selected manifest'
                )
            else:
                shared_guard_matches = True
        elif not branch_matches:
            # A caller without selection provenance may be validating a
            # proposed change to the primary manifest before writing it.
            expected = (primary_branch if alternate_path else None) or selected_branch
            scope = 'primary projection' if alternate_path and primary_branch else 'selected manifest'
            degraded_causes.append(primary_guard_repair_remedy(
                f'primary guard targets {guarded_branch!r}, but the {scope} expects {expected!r}'
            ))
    enforced = bool(guard and guard['enabled'] and (branch_matches or shared_guard_matches))
    if guard and guard['enabled']:
        degraded_causes.extend(primary_guard_repair_remedy(cause) for cause in bundle['degradedCauses'])
    degraded = bool(degraded_causes)
    ready = enforced and bundle['ready'] and not degraded
    migration_pending = required and guard_error == 'primary guard configuration is missing'
    return {
        'status': (
            'degraded' if degraded else
            ('disabled' if disabled else ('ready' if ready else 'optional'))
        ),
        'required': required,
        'disabled': disabled,
        'enforced': enforced,
        'migrationPending': migration_pending,
        'disabledReason': reason if disabled else None,
        'ready': ready,
        'degraded': degraded,
        'degradedCauses': degraded_causes,
        'guard': guard,
        'guardError': guard_error,
        'targetMatchesManifest': branch_matches,
        'expectedDigest': bundle['expectedDigest'],
        'hook': hook,
        'hooks': bundle['hooks'],
        'mode': (
            'required-pending-migration' if migration_pending else
            ('required-degraded' if required and degraded else
             ('degraded' if degraded else
              ('disabled' if disabled else
               ('required' if enforced else 'optional'))))
        ),
    }


def ensure_managed_repository_hooks(repo_root, manifest, manifest_path=None):
    policy = managed_push_guard_policy(repo_root, manifest, manifest_path)
    if not policy['required'] or policy['disabled']:
        return policy
    # An absent bundle is visible in status but does not turn ordinary repository
    # commands into an installation gate. Only an installed hook decides Git
    # mutations, where a missing CLI fails closed in the hook itself.
    return policy


def install_one_managed_hook(repo_root, hook_name, apply=False):
    status = managed_hook_status(repo_root, hook_name)
    if status['ready']:
        return {'action': 'none', **status}
    if status['chainRepairable']:
        plan = {
            'action': 'refresh-chain-metadata',
            'name': hook_name,
            'hook': status['hook'],
            'chainExisting': True,
            'digest': status['digest'],
            'apply': apply,
        }
        if not apply:
            return plan
        metadata_path = Path(status['metadata'])
        metadata = json.loads(metadata_path.read_text())
        metadata['chainedDigest'] = status['chainedDigest']
        atomic_write_private_json(metadata_path, metadata, indent=2)
        return {
            'action': 'chain-metadata-refreshed',
            **managed_hook_status(repo_root, hook_name),
        }
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
    content = managed_hook_content(repo_root, hook_name, backup.exists() or chain_existing)
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


def install_managed_push_hook(
    repo_root, apply=False, manifest=None, manifest_path=None, reason=None,
):
    if apply and managed_hook_syncwheel_command(repo_root) is None:
        raise SyncwheelError('cannot install primary guard: stable syncwheel CLI is not resolvable; install it outside var/worktrees')
    manifest_supplied = manifest is not None
    selected_manifest = manifest or manifest_for_guard(repo_root)
    desired_guard = primary_guard_payload(selected_manifest)
    current_guard, current_guard_error = inspect_primary_guard(repo_root)
    retarget = bool(
        current_guard_error is None
        and current_guard
        and current_guard['integrationBranch'] != desired_guard['integrationBranch']
    )
    guard_action = (
        'retarget' if retarget else
        ('none' if current_guard_error is None and current_guard == desired_guard else
         ('install' if current_guard_error == 'primary guard configuration is missing' else 'update'))
    )
    if apply and retarget and (not isinstance(reason, str) or not reason.strip()):
        raise SyncwheelError('retargeting the primary guard requires --reason')
    plans = {
        name: install_one_managed_hook(repo_root, name, apply=False)
        for name in MANAGED_REPOSITORY_HOOKS
    }
    if apply:
        if retarget:
            if manifest_path is None:
                if manifest_supplied:
                    raise SyncwheelError(
                        'retargeting the primary guard requires the selected manifest path'
                    )
                _, manifest_path = require_manifest(
                    repo_root, str(repo_root), None, None
                )
            append_ledger_event(repo_root, 'primary_guard_retargeted', {
                'actor': os.environ.get('USER', 'unknown'),
                'previousIntegrationBranch': current_guard['integrationBranch'],
                'integrationBranch': desired_guard['integrationBranch'],
                'reason': reason.strip(),
                'phase': 'intent',
            }, manifest_path)
        save_primary_guard(repo_root, selected_manifest)
        results = {
            name: install_one_managed_hook(repo_root, name, apply=True)
            for name in MANAGED_REPOSITORY_HOOKS
        }
    else:
        results = plans
    bundle = managed_hook_bundle_status(repo_root)
    policy = managed_push_guard_policy(repo_root, selected_manifest, manifest_path)
    pre_push = bundle['hooks']['pre-push']
    changed = [item['action'] for item in results.values() if item['action'] != 'none']
    return {
        **pre_push,
        'action': (
            ('installed' if apply else 'install')
            if changed or guard_action != 'none'
            else 'none'
        ),
        'guardAction': guard_action,
        'guard': desired_guard,
        'retargetReasonRequired': retarget,
        'reason': reason.strip() if retarget and isinstance(reason, str) else None,
        'chainExisting': results['pre-push'].get('chainExisting', pre_push['chained']),
        'ready': policy['ready'],
        'degraded': policy['degraded'],
        'degradedCauses': policy['degradedCauses'],
        'expectedDigest': bundle['expectedDigest'],
        'hooks': bundle['hooks'] if apply else results,
    }


def remove_managed_push_hook(
    repo_root, apply=False, disable=False, reason=None,
    manifest=None, manifest_path=None,
):
    if apply and not disable:
        raise SyncwheelError('removing primary guard requires --disable --reason')
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
        if disable and (manifest is None or manifest_path is None):
            resolved_manifest, resolved_path = require_manifest(
                repo_root, str(repo_root), None, None
            )
            manifest = manifest or resolved_manifest
            manifest_path = manifest_path or resolved_path
        if disable:
            plan['guard'] = primary_guard_payload(
                manifest, enabled=False, reason=reason
            )
            plan['ledgerRoot'] = str(_manifest_storage.ledger_root(repo_root, manifest_path))
        return plan
    if disable:
        if manifest is None or manifest_path is None:
            resolved_manifest, resolved_path = require_manifest(
                repo_root, str(repo_root), None, None
            )
            manifest = manifest or resolved_manifest
            manifest_path = manifest_path or resolved_path
        append_ledger_event(repo_root, 'primary_guard_disabled', {
            'actor': os.environ.get('USER', 'unknown'),
            'reason': reason.strip(),
            'phase': 'intent',
        }, manifest_path)
    for name, status in statuses.items():
        if not status['owned']:
            continue
        _, hook, backup, metadata_path, _ = managed_hook_paths(repo_root, name)
        hook.unlink()
        metadata_path.unlink()
        if backup.exists():
            os.replace(backup, hook)
    if disable:
        save_primary_guard(repo_root, manifest, enabled=False, reason=reason.strip())
    return {
        'action': 'disable' if disable else 'removed',
        'reason': reason.strip() if disable else None,
        'hooks': managed_hook_bundle_status(repo_root)['hooks'],
    }


def locally_configured_push_refs(manifest):
    refs = set(managed_ref_names(manifest))
    refs.update(coordination_claim_ref(ref) for ref in list(refs))
    refs.update(delivery_ref_names(manifest))
    state_ref = guard_coordination_ref(manifest)
    if state_ref:
        refs.add(state_ref)
    if manifest.get('repository_mode') == 'journal':
        refs.add(f"refs/heads/{manifest['journal']['branch']}")
    return refs


def managed_push_refs(repo_root, manifest):
    refs = locally_configured_push_refs(manifest)
    config = coordination_config(manifest)
    if config and config.get('mode') == 'active-active':
        previous = read_remote_coordination_state(
            repo_root, config, fetch=True, local_manifest_version=manifest['version']
        )
        if previous.get('state'):
            refs.update(previous['state'].get('managed_refs') or {})
            refs.update(
                coordination_claim_ref(ref)
                for ref in previous['state'].get('managed_refs') or {}
            )
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


PUSH_OPTIONS_WITH_VALUES = {'-o', '--push-option', '--receive-pack', '--exec', '--repo'}


def push_command_updates(command, remote):
    """Return (source, destination ref) for every refspec of a git push command."""
    arguments = list(command[2:])
    deleting = any(argument in {'-d', '--delete'} for argument in arguments)
    index = 0
    while index < len(arguments) and arguments[index].startswith('-'):
        index += 2 if arguments[index] in PUSH_OPTIONS_WITH_VALUES else 1
    if index >= len(arguments) or arguments[index] != remote:
        raise SyncwheelError(f'cannot read the refspecs of this push to {remote}: {quoted(command)}')
    updates = []
    for spec in arguments[index + 1:]:
        source, separator, destination = spec.lstrip('+').partition(':')
        if not separator:
            destination = source
        if not destination.startswith('refs/'):
            destination = f'refs/heads/{destination}'
        updates.append((None if deleting or not source else source, destination))
    return updates


def normalized_remote_url(repo_root, url):
    """Reduce scp-style, ssh://, https:// and path URLs to one comparable form."""
    value = url.strip().rstrip('/')
    value = value[:-len('.git')] if value.endswith('.git') else value
    value = value[len('file://'):] if value.startswith('file://') else value
    if value.startswith(('/', '.')):
        return str((Path(repo_root) / value).resolve(strict=False))
    match = re.match(
        r'^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?([^/:]+)(?::\d+(?=/))?[:/]+(.+)$', value, re.I,
    )
    return f'{match.group(1).lower()}/{match.group(2)}' if match else value


def push_reaches_canonical_remote(repo_root, manifest, remote):
    """Decide by push URL, never by name: a second name for the same URL is the same remote."""
    canonical = manifest['defaults']['canonical_remote']
    if remote == canonical:
        return True

    def push_url(name):
        result = git(repo_root, 'remote', 'get-url', '--push', name, check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    canonical_url = push_url(canonical)
    if not canonical_url:
        # An unresolvable canonical remote cannot be ruled out.
        return True
    return normalized_remote_url(repo_root, push_url(remote) or remote) == (
        normalized_remote_url(repo_root, canonical_url)
    )


def local_only_manifest_destination_allowed(repo_root, manifest, remote, ref):
    integration = manifest['integration']['branch']
    if ref == f'refs/heads/{integration}':
        return not (
            integration == manifest['defaults']['base_branch']
            and push_reaches_canonical_remote(repo_root, manifest, remote)
        )
    return ref.startswith((
        f'refs/heads/{COORDINATION_STATE_PREFIX}',
        f'refs/heads/{COORDINATION_CLAIM_PREFIX}',
    )) or (
        coordination_is_active(manifest)
        and ref == coordination_state_ref(coordination_config(manifest))
    )


def control_manifest_blob_matches_base(repo_root, manifest, entry):
    """Whether the pushed manifest is exactly the one the integration base already carries.

    Comparing with the base, not the projection, matters: a stack that captured
    a control commit makes the projection carry the manifest too.
    """
    base = manifest['integration'].get('base') or manifest['defaults'].get('base_ref')
    if not base:
        return False
    resolved = git(repo_root, 'rev-parse', '--verify', '--quiet', f'{base}^{{commit}}', check=False)
    if resolved.returncode != 0:
        return False
    return tree_path_entry(
        repo_root, resolved.stdout.strip(), _manifest_storage.control_manifest_relative_path(repo_root),
    ) == entry


def refuse_local_only_manifest_push(repo_root, manifest, command, remote):
    """Keep a local-only manifest off every ref but the integration branch and state refs.

    Every Syncwheel push runs this before anything is sent, so the rule holds
    for any route that can move a ref, whatever command built the push.
    """
    if (
        not manifest_is_local_only(manifest)
        or manifest.get('repository_mode') == 'journal'
    ):
        return
    for source, ref in push_command_updates(command, remote):
        if source is None or local_only_manifest_destination_allowed(
            repo_root, manifest, remote, ref,
        ):
            continue
        resolved = git(
            repo_root, 'rev-parse', '--verify', '--quiet', f'{source}^{{commit}}', check=False,
        )
        if resolved.returncode != 0:
            raise SyncwheelError(f'cannot inspect {source} before pushing it to {remote} {ref}')
        entry = tree_path_entry(
            repo_root, resolved.stdout.strip(), _manifest_storage.control_manifest_relative_path(repo_root),
        )
        if entry is None or control_manifest_blob_matches_base(repo_root, manifest, entry):
            continue
        raise SyncwheelError(
            f'refusing to push {ref} to {remote}: its tree carries the local-only '
            'control manifest, which may only reach the integration branch and '
            'Syncwheel coordination refs. Rebuild the branch without the control '
            'commit, then push again'
        )


def forced_push_requested(push_args):
    return any(
        part in {'--force', '-f', '--force-with-lease'}
        or part.startswith('--force-with-lease=')
        for part in push_args
    )


def without_force_push_args(push_args):
    return [
        part for part in push_args
        if part not in {'--force', '-f', '--force-with-lease', '--no-force-with-lease'}
        and not part.startswith('--force-with-lease=')
    ]


def explicit_push_leases(push_args):
    """Leases the caller pinned to an exact expected tip, kept as written."""
    return [
        part for part in push_args
        if part.startswith('--force-with-lease=') and ':' in part
    ]


def stack_tips_from_ledger(repo_root, stack_id, manifest_path=None):
    """Every tip a rebuild in this clone produced for one stack.

    Only rebuild results count: they are replays of declared commits. A
    rebuild's before tip and a pushed tip record whatever the branch held,
    which may be a commit another session made in a worktree of this clone.
    """
    tips = [
        (event.get('payload') or {}).get('after_tip')
        for event in load_ledger_events(repo_root, manifest_path)
        if event.get('type') == 'stack_rebuilt'
        and (event.get('payload') or {}).get('stack') == stack_id
    ]
    return [tip for tip in dict.fromkeys(tips) if isinstance(tip, str) and tip]


def legacy_stack_push_lease(repo_root, stack, remote, local_tip, push_args=(), manifest_path=None):
    """Return the lease arguments for a forced stack push, or refuse to discard work.

    A bare --force-with-lease trusts the remote-tracking ref, which the last
    fetch already moved onto commits another session pushed; the push would
    then silently drop them. Observe the live remote tip instead, refuse if
    it holds a commit the stack does not own, and pin the lease to
    that tip. A lease the caller pinned to an exact tip is kept as written:
    naming the tip is the explicit acknowledgement of what it replaces.
    """
    explicit = explicit_push_leases(push_args)
    if explicit:
        return explicit
    ref = f"refs/heads/{stack['branch']}"
    try:
        remote_tip = remote_ref_tips(repo_root, remote, [ref])[ref]
    except SyncwheelError as exc:
        raise SyncwheelError(
            f"stack push {stack['id']}: cannot observe {remote} {ref} before a forced push: {exc}"
        ) from exc
    lease = [f'--force-with-lease={ref}:{remote_tip or ""}']
    if not remote_tip or remote_tip == local_tip or branch_contains(repo_root, local_tip, remote_tip):
        return lease
    if not commit_exists(repo_root, remote_tip):
        raise SyncwheelError(
            f"stack push {stack['id']}: {remote} {ref} is at {remote_tip}, which this clone "
            f'has not fetched; fetch it and run `syncwheel check` before a forced push'
        )
    # Owned history: the local tip, the stack's declared commits and base, and
    # every tip a rebuild in this clone produced for the stack, so an earlier
    # rebuild of the same stack is recognised even when its patch-id changed.
    owned_tips = [local_tip, stack.get('base'), *stack.get('commits', []),
                  *stack_tips_from_ledger(repo_root, stack['id'], manifest_path)]
    owned_tips = [
        tip for tip in dict.fromkeys(owned_tips)
        if isinstance(tip, str) and tip and commit_exists(repo_root, tip)
    ]
    candidates = [
        line for line in git(
            repo_root, 'rev-list', remote_tip, '--not', *owned_tips,
        ).stdout.splitlines() if line.strip()
    ]
    foreign = candidates
    if candidates:
        base = stack.get('base')
        local_range = f'{base}..{local_tip}' if base and commit_exists(repo_root, base) else local_tip
        local_commits = rev_list(repo_root, local_range)
        local_patch_ids = {
            patch for patch in commit_patch_ids(repo_root, local_commits).values() if patch
        }
        candidate_patch_ids = commit_patch_ids(repo_root, candidates)
        foreign = [
            commit for commit in candidates
            if not candidate_patch_ids.get(commit)
            or candidate_patch_ids[commit] not in local_patch_ids
        ]
    if foreign:
        raise SyncwheelError(
            f"stack push {stack['id']} STOP: {remote} {ref} holds {len(foreign)} commit(s) the "
            f"stack does not own ({', '.join(commit[:12] for commit in foreign[:5])}"
            f"{', ...' if len(foreign) > 5 else ''}); another session pushed there. A forced push "
            'would discard them: add them to the stack, or, once you know they may go, pass '
            f'`--force-with-lease={ref}:{remote_tip}` to replace exactly that tip'
        )
    return lease


def run_authorized_push(repo_root, command, remote, refs, check=True, *, manifest):
    refuse_local_only_manifest_push(repo_root, manifest, command, remote)
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
    if manifest_override:
        if personal:
            raise SyncwheelError('use either --personal or --manifest, not both')
        return Path(manifest_override).expanduser()
    personal = resolve_personal(repo_root, personal)
    if personal:
        return personal_manifest_path(repo_root, personal)
    if repo_value:
        registry, _ = load_repo_registry()
        alias_entry = registry.get(repo_value)
        if alias_entry and alias_entry.get('manifest'):
            return Path(alias_entry['manifest']).expanduser()
    return repo_root / '.syncwheel' / 'manifest.json'


def resolve_command_manifest_selection(args):
    options = vars(args)
    pinned = (options.get('_stack_sync_selection') or options.get('_stack_absorb_selection')
              or options.get('_stack_capture_selection'))
    if pinned is not None:
        return pinned
    tracking_selection = options.get('_tracking_selection')
    if tracking_selection is not None:
        _, repo_root, manifest_path, _ = tracking_selection
        return repo_root, manifest_path
    repo_root = resolve_repo_root(args.repo)
    return repo_root, resolve_manifest_path(
        repo_root, args.repo, getattr(args, 'manifest', None), getattr(args, 'personal', None)
    )


def branch_exists(repo_root, branch):
    return git(repo_root, 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}', check=False).returncode == 0


def ref_exists(repo_root, ref):
    return git(repo_root, 'rev-parse', '--verify', '--quiet', ref, check=False).returncode == 0


# Answers about full commit ids never change while one command runs: a
# commit that exists stays, and ancestry between two fixed commits is fixed.
# Remembering them removes thousands of git processes from whole-repository
# commands. Refs are never remembered, since they move. main() clears both.
_KNOWN_COMMITS = {}
_KNOWN_ANCESTRY = {}
_RUN_MEMO = {}


def clear_commit_memo():
    _KNOWN_COMMITS.clear()
    _KNOWN_ANCESTRY.clear()
    _RUN_MEMO.clear()


def _is_full_object_id(repo_root, value):
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and re.fullmatch(r'[0-9a-f]+', value) is not None
        and len(value) == git_object_id_length(repo_root)
    )


def remember_commits(repo_root, commits):
    known = _KNOWN_COMMITS.setdefault(str(repo_root), set())
    for commit in commits:
        if _is_full_object_id(repo_root, commit):
            known.add(commit)


def prime_commits(repo_root, refs):
    """Learn in one git process which full commit ids exist as commits."""
    candidates = sorted({ref for ref in refs if _is_full_object_id(repo_root, ref)}
                        - _KNOWN_COMMITS.get(str(repo_root), set()))
    if not candidates:
        return
    result = git(
        repo_root, 'cat-file', '--batch-check=%(objectname) %(objecttype)',
        input_text=''.join(f'{ref}\n' for ref in candidates), check=False,
    )
    if result.returncode != 0:
        return
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == 'commit':
            found.append(parts[0])
    remember_commits(repo_root, found)


def _commit_is_known(repo_root, value):
    return value in _KNOWN_COMMITS.get(str(repo_root), ()) and _is_full_object_id(repo_root, value)


def commit_exists(repo_root, ref):
    if _commit_is_known(repo_root, ref):
        return True
    result = git(repo_root, 'rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}', check=False)
    if result.returncode != 0:
        return False
    # Remember the peeled commit, never the input: an annotated tag id is
    # full-length hex too, and must keep resolving to the commit it names.
    remember_commits(repo_root, [result.stdout.strip()])
    return True


def branch_contains(repo_root, branch, commit):
    fixed = _is_full_object_id(repo_root, branch) and _is_full_object_id(repo_root, commit)
    key = (str(repo_root), commit, branch)
    if fixed and key in _KNOWN_ANCESTRY:
        return _KNOWN_ANCESTRY[key]
    result = git(repo_root, 'merge-base', '--is-ancestor', commit, branch, check=False).returncode
    contained = result == 0
    # Exit 1 means "not an ancestor"; anything else is an error and is not remembered.
    if fixed and result in (0, 1):
        _KNOWN_ANCESTRY[key] = contained
    return contained


def commit_full_sha(repo_root, ref):
    if _commit_is_known(repo_root, ref):
        return ref
    full = git(repo_root, 'rev-parse', f'{ref}^{{commit}}').stdout.strip()
    remember_commits(repo_root, [full])
    return full


def commit_parents(repo_root, commit):
    """Return the parents of a commit; fixed for a full commit id, so remembered."""
    fixed = _is_full_object_id(repo_root, commit)
    key = ('parents', str(repo_root), commit)
    if fixed and key in _RUN_MEMO:
        return list(_RUN_MEMO[key])
    result = git(repo_root, 'rev-list', '--parents', '-n', '1', commit)
    parents = result.stdout.strip().split()[1:]
    if fixed:
        _RUN_MEMO[key] = tuple(parents)
    return parents


def commit_parent_count(repo_root, commit):
    return len(commit_parents(repo_root, commit))


def commit_first_parent(repo_root, commit):
    parents = commit_parents(repo_root, commit)
    return parents[0] if parents else None


def commit_patch_id(repo_root, commit):
    full_sha = (
        commit if re.fullmatch(r'[0-9a-f]+', commit) and len(commit) == git_object_id_length(repo_root)
        else commit_full_sha(repo_root, commit)
    )
    return commit_patch_ids(repo_root, [full_sha])[full_sha]


@functools.lru_cache(maxsize=16)
def git_object_id_length(repo_root):
    algorithm = git(repo_root, 'config', '--get', 'extensions.objectformat', check=False).stdout.strip()
    return 64 if algorithm == 'sha256' else 40


# Config sections and environment variables that cannot change the output of
# `git log -p` piped into `git patch-id --stable`. They are left out of the
# patch-ID cache key so that a new tracking branch, a credential helper or a
# tracing variable does not start a new, empty cache. Everything else stays in
# the key, so an unknown input still invalidates the cache.
PATCH_ID_IRRELEVANT_CONFIG_SECTIONS = frozenset({
    'advice', 'alias', 'branch', 'commit', 'credential', 'fetch', 'gc', 'gpg',
    'http', 'init', 'maintenance', 'pack', 'protocol', 'pull', 'push', 'receive',
    'remote', 'rerere', 'safe', 'sendemail', 'tag', 'transfer', 'url', 'user',
})
PATCH_ID_IRRELEVANT_ENV_PREFIXES = (
    'GIT_TRACE', 'GIT_AUTHOR_', 'GIT_COMMITTER_', 'GIT_HTTP_', 'GIT_PUSH_OPTION_',
)
PATCH_ID_IRRELEVANT_ENV = frozenset({
    'GIT_ASKPASS', 'GIT_CURL_VERBOSE', 'GIT_EDITOR', 'GIT_FLUSH',
    'GIT_INDEX_FILE', 'GIT_OPTIONAL_LOCKS', 'GIT_PAGER', 'GIT_PREFIX',
    'GIT_PROGRESS_DELAY', 'GIT_REFLOG_ACTION', 'GIT_SEQUENCE_EDITOR', 'GIT_SSH',
    'GIT_SSH_COMMAND', 'GIT_TERMINAL_PROMPT',
})


PATCH_ID_SEMANTICS_SEED = b'stable:git-log-p:no-textconv:no-ext-diff:no-walk:no-merges:v3\0'


def patch_id_relevant_config(config):
    """Drop `git config --list --null` entries that cannot change a diff."""
    kept = []
    for entry in config.split('\0'):
        if not entry:
            continue
        key = entry.split('\n', 1)[0]
        if key.split('.', 1)[0].lower() in PATCH_ID_IRRELEVANT_CONFIG_SECTIONS:
            continue
        kept.append(entry + '\0')
    return ''.join(kept)


def patch_id_relevant_environment_key(key):
    if not key.startswith('GIT_'):
        return False
    if key in PATCH_ID_IRRELEVANT_ENV:
        return False
    return not key.startswith(PATCH_ID_IRRELEVANT_ENV_PREFIXES)


@functools.lru_cache(maxsize=16)
def patch_id_semantics_key(repo_root):
    """Snapshot Git diff semantics once per CLI process and repository."""
    config = patch_id_relevant_config(git(repo_root, 'config', '--list', '--null').stdout)
    # v3: computed with --no-textconv --no-ext-diff. Rows written under the
    # earlier seeds may come from a textconv driver and are never read.
    digest = hashlib.sha256(PATCH_ID_SEMANTICS_SEED)
    digest.update(config.encode('utf-8', 'surrogateescape'))
    digest.update(git(repo_root, '--version').stdout.encode())
    # Replace refs and grafts can change a commit's effective parents or diff
    # without changing its object ID.
    digest.update(git(repo_root, 'for-each-ref',
                      '--format=%(refname) %(objectname)', 'refs/replace/').stdout.encode())
    # Legacy git patch-id inherits the CLI's cwd, which can select a different
    # object format from the target repository (notably SHA-1 versus SHA-256).
    digest.update(run(['git', 'rev-parse', '--show-object-format'], cwd=Path.cwd(),
                      check=False).stdout.encode())
    digest.update(patch_id_relevant_config(
        run(['git', 'config', '--list', '--null'], cwd=Path.cwd()).stdout
    ).encode('utf-8', 'surrogateescape'))
    for key, value in sorted(os.environ.items()):
        if patch_id_relevant_environment_key(key):
            digest.update(key.encode() + b'=' + value.encode('utf-8', 'surrogateescape') + b'\0')
    common_dir = git_common_dir(repo_root)
    attributes = [('info/attributes', common_dir / 'info' / 'attributes'),
                  ('info/grafts', common_dir / 'info' / 'grafts'),
                  ('shallow', common_dir / 'shallow')]
    tracked = git(repo_root, 'ls-files', '-z', '--cached', '--others',
                  '--', '.gitattributes', '**/.gitattributes').stdout
    attributes.extend((name, Path(repo_root) / name) for name in tracked.split('\0') if name)
    global_attributes = git(repo_root, 'config', '--path', '--get', 'core.attributesfile',
                            check=False).stdout.strip()
    if global_attributes:
        global_path = Path(global_attributes).expanduser()
        attributes.append(('core.attributesfile', global_path if global_path.is_absolute()
                           else Path(repo_root) / global_path))
    else:
        config_home = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        attributes.append(('global/attributes', config_home / 'git' / 'attributes'))
    for name, path in attributes:
        digest.update(name.encode('utf-8', 'surrogateescape') + b'\0')
        try:
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        except FileNotFoundError:
            digest.update(b'absent\0')
    return digest.hexdigest()


def patch_id_cache_connection(repo_root):
    path = git_common_dir(repo_root) / 'syncwheel' / 'patch-ids-v2.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.execute('CREATE TABLE IF NOT EXISTS patch_ids ('
                       'semantics TEXT NOT NULL, commit_sha TEXT NOT NULL, '
                       'patch_id TEXT, row_digest TEXT NOT NULL, '
                       'PRIMARY KEY (semantics, commit_sha))')
    return connection


def patch_id_row_digest(semantics, commit, patch_id):
    digest = hashlib.sha256(b'patch-id-row-v2\0')
    for value in (semantics, commit, patch_id):
        encoded = value.encode() if value is not None else b''
        digest.update((len(encoded) if value is not None else -1).to_bytes(8, 'big', signed=True))
        digest.update(encoded)
    return digest.hexdigest()


def batch_uncached_patch_ids(repo_root, commits):
    """One parent walk and one streaming log/patch-id pipeline for all misses."""
    if not commits:
        return {}
    input_text = ''.join(commit + '\n' for commit in commits)
    parents = git(repo_root, 'rev-list', '--parents', '--no-walk=unsorted', '--stdin',
                  input_text=input_text)
    result = {}
    eligible = []
    for line in parents.stdout.splitlines():
        parts = line.split()
        result[parts[0]] = None
        if len(parts) == 2:
            eligible.append(parts[0])
    if not eligible:
        return result
    header_lookup = {}
    for sha in eligible:
        for header in {sha, sha[:40]}:
            header_lookup[header] = sha if header not in header_lookup else None
    with tempfile.TemporaryFile(mode='w+t') as revisions, tempfile.TemporaryFile(mode='w+t') as log_errors:
        revisions.write(''.join(commit + '\n' for commit in eligible))
        revisions.seek(0)
        log = subprocess.Popen(
            # Git's own diff only: textconv filters and external diff drivers
            # are programs that may read any config or environment, which the
            # cache key deliberately leaves out.
            ['git', 'log', '-p', '--no-textconv', '--no-ext-diff', '--no-walk=unsorted',
             '--no-merges', '--format=medium', '--stdin'],
            cwd=repo_root, stdin=revisions, stdout=subprocess.PIPE, stderr=log_errors,
            text=True, env=managed_process_env(),
        )
        patch = subprocess.Popen(
            ['git', 'patch-id', '--stable'], stdin=log.stdout, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=managed_process_env(),
        )
        log.stdout.close()
        patch_stdout, patch_stderr = patch.communicate()
        log_status = log.wait()
        log_errors.seek(0)
        log_stderr = log_errors.read()
    if log_status or patch.returncode:
        raise SyncwheelError(log_stderr.strip() or patch_stderr.strip() or 'patch-id batch failed')
    for line in patch_stdout.splitlines():
        patch_id, commit = line.split()
        # git patch-id truncates the commit header to its cwd object format.
        matching = header_lookup.get(commit)
        if not matching or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', patch_id):
            raise SyncwheelError('unexpected git patch-id batch output')
        result[matching] = patch_id
    return result


def commit_patch_ids(repo_root, commits):
    commits = list(dict.fromkeys(commits))
    if not commits:
        return {}
    if _READONLY_DIAGNOSTICS.get():
        # The shared SQLite cache API creates directories, schema and rows.
        return batch_uncached_patch_ids(repo_root, commits)
    semantics = patch_id_semantics_key(repo_root)
    found = {}
    try:
        with contextlib.closing(patch_id_cache_connection(repo_root)) as connection, connection:
            for commit in commits:
                row = connection.execute(
                    'SELECT patch_id, row_digest FROM patch_ids WHERE semantics=? AND commit_sha=?',
                    (semantics, commit),
                ).fetchone()
                if (row and (row[0] is None or (isinstance(row[0], str)
                                            and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', row[0])))
                        and row[1] == patch_id_row_digest(semantics, commit, row[0])):
                    found[commit] = row[0]
            missing = [commit for commit in commits if commit not in found]
            if missing:
                computed = batch_uncached_patch_ids(repo_root, missing)
                connection.executemany(
                    'INSERT OR REPLACE INTO patch_ids VALUES (?, ?, ?, ?)',
                    [(semantics, commit, computed[commit],
                      patch_id_row_digest(semantics, commit, computed[commit])) for commit in missing],
                )
                found.update(computed)
    except (sqlite3.DatabaseError, OSError):
        # A damaged or unwritable cache is never authoritative; recompute without it.
        return batch_uncached_patch_ids(repo_root, commits)
    return found


def patch_ids_reachable_from_ref(repo_root, ref):
    return {patch_id for patch_id in commit_patch_ids(repo_root, rev_list(repo_root, ref)).values()
            if patch_id}


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


def stack_content_absorbed_at(repo_root, stack, tip):
    commits = stack.get('commits') or []
    projected_tip = composed_stack_projection_tip(repo_root, stack)
    if (
        not projected_tip and commits and not ref_tip(repo_root, stack['branch'])
        and commit_exists(repo_root, commits[0])
    ):
        # no local branch to take the base from: replay from the first commit's parent
        base = commit_first_parent(repo_root, commits[0])
        projected_tip = base and deterministic_stack_replay_tip(repo_root, base, commits)
    return bool(
        projected_tip and stack_content_is_present_at_delivery_tip(
            repo_root, stack, tip, projected_tip=projected_tip,
        )
    ) or merged_stack_tip_matches_delivery(repo_root, stack, tip)


def merged_stack_tip_matches_delivery(repo_root, stack, delivery_tip):
    """Prove a squash of an exact merge tip when deterministic replay is impossible.

    A merge commit has no single patch to replay. Only accept its declared,
    materialized tip when every declared source commit is in its ancestry and
    all paths changed by those commits still have the same delivered content.
    Inspect every merge parent so conflict resolutions cannot disappear.
    """
    commits = stack.get('commits') or []
    if not commits:
        return False
    tip = ref_tip(repo_root, stack['branch'])
    if not tip or tip != commit_full_sha(repo_root, commits[-1]):
        return False
    if commit_parent_count(repo_root, tip) < 2:
        return False
    if any(
        git(repo_root, 'merge-base', '--is-ancestor', commit, tip, check=False).returncode != 0
        for commit in commits
    ):
        return False
    paths = set()
    for commit in commits:
        parents = git(repo_root, 'rev-list', '--parents', '-n', '1', commit).stdout.split()[1:]
        for parent in parents:
            changed = git(
                repo_root, 'diff', '--name-only', '--no-renames', '-z', parent, commit
            ).stdout
            paths.update(path for path in changed.split('\0') if path)
    if not paths:
        return False
    return git(
        repo_root, 'diff', '--quiet', tip, delivery_tip, '--', *sorted(paths), check=False
    ).returncode == 0


def commit_short_sha(repo_root, commit):
    return git(repo_root, 'rev-parse', '--short', f'{commit}^{{commit}}').stdout.strip()


def commit_subject(repo_root, commit):
    return git(repo_root, 'show', '-s', '--format=%s', commit).stdout.strip()


def commit_changed_files(repo_root, commit, limit=None):
    fixed = _is_full_object_id(repo_root, commit)
    key = ('changed_files', str(repo_root), commit)
    if fixed and key in _RUN_MEMO:
        files = list(_RUN_MEMO[key])
        return files[:limit] if limit else files
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
    if fixed:
        _RUN_MEMO[key] = tuple(files)
    return files[:limit] if limit else files


def is_manifest_only_commit(repo_root, commit):
    """Whether a commit changes only tracked Syncwheel control metadata."""
    files = commit_changed_files(repo_root, commit)
    if bool(files) and set(files) == {'.syncwheel/manifest.json'}:
        return True
    # Initialization also writes the managed ignore block. Preserve the strict
    # control-transition proof so unrelated ignore changes stay unowned.
    parents = commit_parents(repo_root, commit)
    return len(parents) == 1 and integration_control_only_transition(repo_root, commit, parents[0])


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


def derived_projection_commit_paths_digest(repo_root, commit, paths):
    return _provenance_store().derived_projection_commit_paths_digest(
        repo_root, commit, paths,
    )


def derived_provenance_store_path(repo_root):
    return _provenance_store().derived_provenance_store_path(
        repo_root,
    )


def derived_provenance_store_lock_path(repo_root):
    return _provenance_store().derived_provenance_store_lock_path(
        repo_root,
    )






def load_derived_provenance_store(repo_root):
    return _provenance_store().load_derived_provenance_store(
        repo_root,
    )


def save_derived_provenance_store(repo_root, store):
    return _provenance_store().save_derived_provenance_store(
        repo_root, store,
    )


def derived_provenance_store_lock(repo_root):
    return _provenance_store().derived_provenance_store_lock(
        repo_root,
    )


def local_coordination_provenance_state(repo_root, manifest):
    return _provenance_store().local_coordination_provenance_state(
        repo_root, manifest,
    )


def shared_derived_provenance_records(repo_root, manifest, coordination_state=None):
    return _provenance_store().shared_derived_provenance_records(
        repo_root, manifest, coordination_state,
    )






def derived_provenance_snapshot(repo_root, manifest, coordination_state=None):
    return _provenance_store().derived_provenance_snapshot(
        repo_root, manifest, coordination_state,
    )


def derived_provenance_records(repo_root, manifest, coordination_state=None):
    return _provenance_store().derived_provenance_records(
        repo_root, manifest, coordination_state,
    )


def update_common_derived_provenance(
    repo_root,
    manifest,
    paths,
    record,
    *,
    expected_commit=None,
    coordination_state=None,
):
    return _provenance_store().update_common_derived_provenance(
        repo_root, manifest, paths, record, expected_commit=expected_commit, coordination_state=coordination_state,
    )




def record_common_derived_provenance(
    repo_root, manifest, record, coordination_state=None
):
    return _provenance_store().record_common_derived_provenance(
        repo_root, manifest, record, coordination_state,
    )


def resolve_common_derived_provenance(
    repo_root,
    manifest,
    paths,
    *,
    expected_commit=None,
    coordination_state=None,
):
    return _provenance_store().resolve_common_derived_provenance(
        repo_root, manifest, paths, expected_commit=expected_commit, coordination_state=coordination_state,
    )


def is_provenance_bound_derived_projection_commit(repo_root, commit, provenance):
    return _provenance_store().is_provenance_bound_derived_projection_commit(
        repo_root, commit, provenance,
    )


def is_derived_projection_commit(repo_root, manifest, commit, provenance=None):
    return _provenance_store().is_derived_projection_commit(
        repo_root, manifest, commit, provenance,
    )


def narrowed_derived_provenance_records(repo_root, manifest, provenance=None):
    return _provenance_store().narrowed_derived_provenance_records(
        repo_root, manifest, provenance,
    )






def stale_derived_projection_records(
    repo_root, manifest, integration_branch, provenance=None
):
    return _provenance_store().stale_derived_projection_records(
        repo_root, manifest, integration_branch, provenance,
    )


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
    if shared_manifest and _READONLY_DIAGNOSTICS.get():
        shared_expected = primary_guard_branch(read_primary_authority_manifest(shared_manifest))
    elif shared_manifest and shared_manifest.exists():
        try:
            shared_expected = primary_guard_branch(json.loads(shared_manifest.read_text()))
        except (OSError, ValueError):
            pass
    active_expected = primary_guard_branch(manifest)
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


def primary_checkout_state_entry(entry):
    """True for repo-local Syncwheel state, tracked or not."""
    paths = _manifest_storage.status_line_paths(entry)
    return bool(paths) and all(path.startswith('.syncwheel/') for path in paths)


def primary_checkout_dirty_entries(repo_root):
    worktrees = get_worktrees(repo_root)
    primary_path = Path(worktrees[0]['path']).resolve() if worktrees else Path(repo_root).resolve()
    entries = local_worktree_status(primary_path)
    if entries is None:
        raise SyncwheelError(f'cannot inspect primary checkout: {primary_path}')
    # The primary authoring guard owns tracked product work. Repo-local Syncwheel
    # state and untracked files have dedicated command preflights already, and the
    # capture and lane remedies this guard names cannot move Syncwheel state
    # anyway, so counting it would refuse a mutation nothing can repair.
    return [
        entry for entry in entries
        if not entry.startswith('?? ') and not primary_checkout_state_entry(entry)
    ]


def primary_checkout_dirty_warning_lines(repo_root, manifest):
    entries = primary_checkout_dirty_entries(repo_root)
    if not entries:
        return []
    return [
        'primary checkout is dirty: ' + str(len(entries)) + ' tracked file'
        + ('' if len(entries) == 1 else 's')
        + ' not owned by the current user'
    ]


def emit_primary_checkout_dirty_warnings(repo_root, manifest, json_mode=False):
    lines = primary_checkout_dirty_warning_lines(repo_root, manifest)
    if not lines or json_mode or not sys.stderr.isatty():
        return lines
    color = '' if os.environ.get('NO_COLOR') else YELLOW
    reset = '' if not color else RESET
    for line in lines:
        print(f'{color}WARNING: {line}{reset}', file=sys.stderr)
    return lines


def require_clean_primary_checkout(repo_root, manifest, manifest_path=None, stack_id=None):
    lines = primary_checkout_dirty_warning_lines(repo_root, manifest)
    if not lines:
        return
    raise SyncwheelError(
        lines[0] + format_remedy_suffix(
            primary_checkout_remedy_commands(
                manifest, stack_id=stack_id, repo_root=repo_root, manifest_path=manifest_path,
            )
        )
    )




def ensure_clean_worktree(
    path, allowed_status_prefixes=None, remedy_commands=None, allowed_paths=None,
    allowed_path_prefixes=None,
):
    result = run(['git', '-C', str(path), 'status', '--porcelain'], check=False)
    if result.returncode != 0:
        raise SyncwheelError(f'{path} is not a git worktree')
    allowed_status_prefixes = tuple(allowed_status_prefixes or [])
    allowed_paths = set(allowed_paths or ())
    allowed_path_prefixes = tuple(
        str(prefix).replace('\\', '/').rstrip('/') + '/'
        for prefix in (allowed_path_prefixes or ())
    )
    remaining = []
    for line in result.stdout.splitlines():
        entry = line.strip()
        if not entry:
            continue
        if allowed_status_prefixes and any(entry.startswith(prefix) for prefix in allowed_status_prefixes):
            continue
        paths = _manifest_storage.status_line_paths(line)
        if allowed_paths and paths and paths <= allowed_paths:
            continue
        if allowed_path_prefixes and paths and all(
            any(item.startswith(prefix) for prefix in allowed_path_prefixes)
            for item in paths
        ):
            continue
        remaining.append(entry)
    if remaining:
        raise SyncwheelError(f'{path} is not clean' + format_remedy_suffix(remedy_commands or []))






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
        '.syncwheel/manifests/*.local-ledger/',
        syncwheel_ignore_pattern(worktree_root),
    ]


def split_syncwheel_managed_gitignore(text, worktree_root):
    """Split one exact managed block from byte-preserving surrounding text."""
    if not isinstance(text, str):
        return None
    lines = text.splitlines(keepends=True)
    content = [line[:-1] if line.endswith('\n') else line for line in lines]
    starts = [
        index for index, line in enumerate(content)
        if line == SYNCWHEEL_GITIGNORE_MARKER
    ]
    ends = [
        index for index, line in enumerate(content)
        if line == SYNCWHEEL_GITIGNORE_END_MARKER
    ]
    if not starts and not ends:
        return {'unmanaged': text, 'managed': None}
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return None
    start = starts[0]
    end = ends[0]
    observed_patterns = content[start + 1:end]
    if observed_patterns != syncwheel_gitignore_patterns(worktree_root):
        return None
    return {
        'unmanaged': ''.join(lines[:start] + lines[end + 1:]),
        'managed': ''.join(lines[start:end + 1]),
    }


def split_historical_syncwheel_managed_gitignore(text, worktree_root):
    """Recognize the current block and the exact pre-local-ledger block."""
    current = split_syncwheel_managed_gitignore(text, worktree_root)
    if current is not None:
        return current
    if not isinstance(text, str):
        return None
    lines = text.splitlines(keepends=True)
    content = [line[:-1] if line.endswith('\n') else line for line in lines]
    starts = [
        index for index, line in enumerate(content)
        if line == SYNCWHEEL_GITIGNORE_MARKER
    ]
    ends = [
        index for index, line in enumerate(content)
        if line == SYNCWHEEL_GITIGNORE_END_MARKER
    ]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return None
    start = starts[0]
    end = ends[0]
    legacy_patterns = syncwheel_gitignore_patterns(worktree_root)
    legacy_patterns.remove('.syncwheel/manifests/*.local-ledger/')
    if content[start + 1:end] != legacy_patterns:
        return None
    return {
        'unmanaged': ''.join(lines[:start] + lines[end + 1:]),
        'managed': ''.join(lines[start:end + 1]),
    }


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
        gitignore_updated = write_managed_block(
            repo_root / '.gitignore',
            SYNCWHEEL_GITIGNORE_MARKER,
            SYNCWHEEL_GITIGNORE_END_MARKER,
            syncwheel_gitignore_patterns(worktree_root),
            legacy_patterns,
        )
        transaction = _manifest_storage.active_manifest_write_transaction_for_repo(repo_root)
        if gitignore_updated and transaction is not None:
            transaction['gitignoreWritten'] = True
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




def backup_branch_command(repo_root, branch, timestamp):
    return _replay_engine(repo_root).backup_branch_command(
        branch, timestamp,
    )


def ensure_in_place_target(
    repo_root, target_branch, manifest, stack_id=None, allowed_paths=None, manifest_path=None,
):
    remedies = primary_checkout_remedy_commands(
        manifest, stack_id=stack_id, repo_root=repo_root, manifest_path=manifest_path,
    )
    current_branch = get_current_branch(repo_root)
    if current_branch != target_branch:
        raise SyncwheelError(
            f'in-place materialization requires current branch {target_branch!r}; '
            f'current branch is {current_branch!r}' + format_remedy_suffix(remedies)
        )
    ensure_clean_worktree(
        repo_root, remedy_commands=remedies, allowed_paths=allowed_paths,
    )






def merge_stack_resolution_commits(repo_root, stack, stack_ref):
    """A reviewed integration replacement never rewrites the source branch."""
    resolved = bound_merge_stack_resolution_commits(stack)
    if resolved is None:
        return None
    binding = stack['integration_resolution']
    if binding and binding.get('source_tip') != commit_full_sha(repo_root, stack_ref):
        raise SyncwheelError(
            f"stack {stack['id']} integration resolution is stale; "
            'run syncwheel int rebuild --replay-mode desk --reason "review changed source"'
        )
    return resolved
















def control_manifest_lock_path(repo_root, branch):
    return _manifest_store().control_manifest_lock_path(
        repo_root, branch,
    )


def control_manifest_branch_lock(repo_root, branch):
    return _manifest_store().control_manifest_branch_lock(
        repo_root, branch,
    )


def channel_mutation_lock(repo_root, manifest_path, channel_id):
    return _manifest_store().channel_mutation_lock(
        repo_root, manifest_path, channel_id,
    )








def manifest_write_transaction(
    repo_root, manifest_path, owner='manifest-command', *, commit_tracked_changes=True,
):
    return _manifest_store().manifest_write_transaction(
        repo_root, manifest_path, owner, commit_tracked_changes=commit_tracked_changes,
    )










































def require_ignored_ledger_paths(repo_root, paths):
    return _manifest_store().require_ignored_ledger_paths(
        repo_root, paths,
    )


def append_ledger_event(
    repo_root, event_type, payload, manifest_path=None, idempotency_key=None,
):
    return _manifest_store().append_ledger_event(
        repo_root, event_type, payload, manifest_path, idempotency_key,
    )


def save_manifest_with_ledger(repo_root, manifest_path, manifest, reason, context=None, event_type='manifest_saved'):
    return _manifest_store().save_manifest_with_ledger(
        repo_root, manifest_path, manifest, reason, context, event_type,
    )


def acknowledge_in_place_manifest_replay(repo_root, manifest_path, replay_tip):
    return _manifest_store().acknowledge_in_place_manifest_replay(
        repo_root, manifest_path, replay_tip,
    )








def manifest_from_tree(repo_root, commit, path):
    return _manifest_store().manifest_from_tree(
        repo_root, commit, path,
    )


def materialize_control_manifest_commit(
    repo_root, manifest, parent, identity_version=CONTROL_MANIFEST_IDENTITY_VERSION,
):
    return _manifest_store().materialize_control_manifest_commit(
        repo_root, manifest, parent, identity_version,
    )


def control_manifest_commit_identity_version(repo_root, manifest, parent, commit):
    return _manifest_store().control_manifest_commit_identity_version(
        repo_root, manifest, parent, commit,
    )


def control_manifest_actor(repo_root):
    return _manifest_store().control_manifest_actor(
        repo_root,
    )


def control_manifest_event_payload(
    repo_root, manifest_path, manifest, control_commit, replay_mode, reason, command, context,
    operation_id=None,
):
    return _manifest_store().control_manifest_event_payload(
        repo_root, manifest_path, manifest, control_commit, replay_mode, reason, command, context, operation_id,
    )


def control_manifest_intent_payload(
    repo_root, manifest, source_digest, replay_tip, control_commit,
    replay_mode, reason, command, operation_id,
    identity_version=CONTROL_MANIFEST_IDENTITY_VERSION,
):
    return _manifest_store().control_manifest_intent_payload(
        repo_root, manifest, source_digest, replay_tip, control_commit, replay_mode, reason, command, operation_id, identity_version,
    )






def abandon_control_manifest_intent(
    repo_root, manifest_path, intent, observed_tip, outcome,
):
    return _manifest_store().abandon_control_manifest_intent(
        repo_root, manifest_path, intent, observed_tip, outcome,
    )












def staged_control_manifest_migration(repo_root):
    return _manifest_store().staged_control_manifest_migration(
        repo_root,
    )


def control_manifest_owns_the_blocking_dirt(repo_root):
    return _manifest_store().control_manifest_owns_the_blocking_dirt(
        repo_root,
    )




def local_only_control_manifest_untrack_step(repo_root, manifest):
    return _manifest_store().local_only_control_manifest_untrack_step(
        repo_root, manifest,
    )


def untrack_local_only_control_manifest(repo_root, manifest):
    return _manifest_store().untrack_local_only_control_manifest(
        repo_root, manifest,
    )


def local_only_control_manifest_guard(repo_root, manifest, manifest_path):
    return _manifest_store().local_only_control_manifest_guard(
        repo_root, manifest, manifest_path,
    )




def control_manifest_checkout_obstruction(
    repo_root, integration_worktree, replay_tip, control_commit, expected_digest,
):
    return _manifest_store().control_manifest_checkout_obstruction(
        repo_root, integration_worktree, replay_tip, control_commit, expected_digest,
    )






def align_control_manifest_worktree(
    repo_root, manifest, replay_tip, control_commit, expected_digest,
    obstruction_state=None, preserve_ignore=False,
):
    return _manifest_store().align_control_manifest_worktree(
        repo_root, manifest, replay_tip, control_commit, expected_digest, obstruction_state, preserve_ignore,
    )


def capture_checkout_source_lease(repo_root, manifest_path, target):
    return _manifest_store().capture_checkout_source_lease(
        repo_root, manifest_path, target,
    )


def persist_local_only_control_manifest(
    repo_root, manifest_path, manifest, replay_tip, replay_mode, reason, command,
    operation_id, persist_source, source_lease_out,
):
    return _manifest_store().persist_local_only_control_manifest(
        repo_root, manifest_path, manifest, replay_tip, replay_mode, reason, command, operation_id, persist_source, source_lease_out,
    )


def restore_control_manifest_after_integration_rebuild(
    repo_root, manifest_path, manifest, replay_tip, replay_mode,
    reason='restore_control_manifest_after_integration_rebuild', command='reconcile',
    operation_id=None, persist_source=True, source_lease_out=None,
    identity_version=CONTROL_MANIFEST_IDENTITY_VERSION,
    preserved_ignore=None, pre_cas_check=None,
):
    return _manifest_store().restore_control_manifest_after_integration_rebuild(
        repo_root, manifest_path, manifest, replay_tip, replay_mode, reason, command, operation_id, persist_source, source_lease_out, identity_version, preserved_ignore, pre_cas_check,
    )


def integration_reconciliation_source(repo_root, manifest_path):
    return _integration_history().integration_reconciliation_source(
        repo_root, manifest_path,
    )


def integration_reconciliation_object(repo_root, manifest, local_tip, replay_tip, tree, inputs, published_parent=None):
    return _integration_history().integration_reconciliation_object(
        repo_root, manifest, local_tip, replay_tip, tree, inputs, published_parent,
    )


def verify_reconciliation_published_parent(repo_root, manifest, local_tip, replay_tip,
                                          published_parent, inputs, observation):
    return _integration_history().verify_reconciliation_published_parent(
        repo_root, manifest, local_tip, replay_tip, published_parent, inputs, observation,
    )


def integration_reconciliation_inputs(repo_root, manifest, inputs):
    return _integration_history().integration_reconciliation_inputs(
        repo_root, manifest, inputs,
    )


def integration_reconciliation_publishing_states(repo_root, observation):
    return _integration_history().integration_reconciliation_publishing_states(
        repo_root, observation,
    )


def integration_reconciliation_published_witness(repo_root, manifest, tip, observation):
    return _integration_history().integration_reconciliation_published_witness(
        repo_root, manifest, tip, observation,
    )


def integration_reconciliation_historical_derived(repo_root, commit, tip, observation):
    return _integration_history().integration_reconciliation_historical_derived(
        repo_root, commit, tip, observation,
    )


def integration_reconciliation_proof(repo_root, commit, manifest_path, observation):
    return _integration_history().integration_reconciliation_proof(
        repo_root, commit, manifest_path, observation,
    )


def _integration_reconciliation_proof(repo_root, commit, manifest_path, observation):
    return _integration_history()._integration_reconciliation_proof(
        repo_root, commit, manifest_path, observation,
    )


def integration_reconciliation_provenance(manifest, store, shared):
    return _integration_history().integration_reconciliation_provenance(
        manifest, store, shared,
    )


def published_integration_has_no_unique_product(repo_root, manifest, tip, observation, delivery_tip):
    return _integration_history().published_integration_has_no_unique_product(
        repo_root, manifest, tip, observation, delivery_tip,
    )


def historically_delivered_integration_boundary(repo_root, manifest, tip, observation, delivery_tip):
    return _integration_history().historically_delivered_integration_boundary(
        repo_root, manifest, tip, observation, delivery_tip,
    )


def historical_stack_has_exact_squash_delivery(
    repo_root, stack, delivery_observation, defaults, coordination_remote=None,
):
    return _integration_history().historical_stack_has_exact_squash_delivery(
        repo_root, stack, delivery_observation, defaults, coordination_remote,
    )


def historical_managed_tip_has_exact_squash_delivery(
    repo_root, managed_tip, stack, delivery_observation, defaults,
    coordination_remote=None,
):
    return _integration_history().historical_managed_tip_has_exact_squash_delivery(
        repo_root, managed_tip, stack, delivery_observation, defaults, coordination_remote,
    )


def historical_rebuilt_stack_tip_is_absorbed(
    repo_root, old_stack, new_stack, old_tip, delivery_observation,
):
    return _integration_history().historical_rebuilt_stack_tip_is_absorbed(
        repo_root, old_stack, new_stack, old_tip, delivery_observation,
    )


def abandoned_stack_paths_match_delivery(repo_root, commits, local_tip, published_tip, delivery_tip):
    return _integration_history().abandoned_stack_paths_match_delivery(
        repo_root, commits, local_tip, published_tip, delivery_tip,
    )


def historically_closed_integration_commits(
    repo_root, tip, observation, candidates=None, delivery_tip=None,
    delivery_observation=None, merge_parent_candidates=None, abandonment_only=False,
    absorbed_merge_tips=None,
):
    return _integration_history().historically_closed_integration_commits(
        repo_root, tip, observation, candidates, delivery_tip, delivery_observation, merge_parent_candidates, abandonment_only, absorbed_merge_tips,
    )


def historically_abandoned_unmapped_commits(repo_root, manifest, commits):
    return _integration_history().historically_abandoned_unmapped_commits(
        repo_root, manifest, commits,
    )


def integration_only_replay_is_empty(repo_root, commit, head, tree):
    return _integration_history().integration_only_replay_is_empty(
        repo_root, commit, head, tree,
    )


def historically_empty_integration_only_commits(repo_root, manifest, commits, manifest_path=None):
    return _integration_history().historically_empty_integration_only_commits(
        repo_root, manifest, commits, manifest_path,
    )


def historically_reconciled_unmapped_commits(repo_root, manifest, commits):
    return _integration_history().historically_reconciled_unmapped_commits(
        repo_root, manifest, commits,
    )


def integration_control_only_transition(repo_root, commit, parent):
    return _integration_history().integration_control_only_transition(
        repo_root, commit, parent,
    )


def integration_reconciliation_history(
    repo_root, manifest, tip, provenance, manifest_path=None, observation=None,
    detached_replay=False, delivery_tip=None, delivery_observation=None,
):
    return _integration_history().integration_reconciliation_history(
        repo_root, manifest, tip, provenance, manifest_path, observation, detached_replay, delivery_tip, delivery_observation,
    )


def apply_integration_reconciliation(repo_root, manifest_path, manifest, intent):
    return _integration_history().apply_integration_reconciliation(
        repo_root, manifest_path, manifest, intent,
    )


def integration_divergence_is_control_only(repo_root, local_tip, published_tip):
    return _integration_history().integration_divergence_is_control_only(
        repo_root, local_tip, published_tip,
    )


def reconcile_integration_ancestry(repo_root, manifest_path, manifest, command, reason):
    return _integration_history().reconcile_integration_ancestry(
        repo_root, manifest_path, manifest, command, reason,
    )


def _restore_control_manifest_after_integration_rebuild_locked(
    repo_root, manifest_path, manifest, replay_tip, replay_mode,
    reason='restore_control_manifest_after_integration_rebuild', command='reconcile',
    operation_id=None, persist_source=True, source_lease_out=None,
    identity_version=CONTROL_MANIFEST_IDENTITY_VERSION,
    preserved_ignore=None, pre_cas_check=None,
):
    return _manifest_store()._restore_control_manifest_after_integration_rebuild_locked(
        repo_root, manifest_path, manifest, replay_tip, replay_mode, reason, command, operation_id, persist_source, source_lease_out, identity_version, preserved_ignore, pre_cas_check,
    )




def resolve_superseded_control_manifest_intent(
    repo_root, manifest_path, manifest, intent, branch, allow_new_operation,
):
    return _manifest_store().resolve_superseded_control_manifest_intent(
        repo_root, manifest_path, manifest, intent, branch, allow_new_operation,
    )


def recover_incomplete_control_manifest_persistence(
    repo_root, manifest_path, manifest, allow_new_operation=False,
    recovery_state=None, intents_only=False,
):
    return _manifest_store().recover_incomplete_control_manifest_persistence(
        repo_root, manifest_path, manifest, allow_new_operation, recovery_state, intents_only,
    )


def skip_implicit_control_recovery_on_dirty_primary(repo_root, manifest_path):
    """Refuse implicit recovery of either selected or canonical control state."""
    worktrees = get_worktrees(repo_root)
    primary = Path(worktrees[0]['path']).resolve() if worktrees else repo_root
    status = local_worktree_status(primary)
    if status is None:
        raise SyncwheelError(f'cannot inspect primary checkout: {primary}')
    if any(not primary_checkout_state_entry(entry) for entry in status):
        # Do not repair a ledger tail, create a recovery intent or move refs
        # merely because an unrelated command is running on a dirty primary.
        locations = (
            (primary, primary / '.syncwheel' / 'manifest.json'),
            (repo_root, manifest_path),
        )
        for root, path in dict.fromkeys(locations):
            pending = pending_control_manifest_intents(load_ledger_events(root, path))
            if pending:
                intent = pending[0]['payload']
                if explicit_control_recovery_intent(intent):
                    raise SyncwheelError(_manifest_storage.explicit_control_recovery_refusal())
                command = intent.get('command') or 'the interrupted Syncwheel command'
                raise SyncwheelError(
                    'primary checkout is dirty and control manifest recovery is pending. '
                    f'Preserve the checkout changes and retry {command} from a clean primary checkout'
                )
        return True
    return False


def ref_tip(repo_root, ref):
    result = git(repo_root, 'rev-parse', ref, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()




def require_manifest(repo_root, repo_value=None, manifest_override=None, personal=None):
    manifest_path = resolve_manifest_path(repo_root, repo_value, manifest_override, personal)
    manifest, manifest_path = _manifest_storage.load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    return manifest, manifest_path


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


def pin_stack_for_channel(repo_root, manifest, stack_id):
    stack = require_stack(manifest, stack_id)
    if stack.get('adopted_history'):
        raise SyncwheelError('channel pins do not yet support adopted stack histories')
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
    for event in reversed(_manifest_storage.load_ledger_events(repo_root, manifest_path)):
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


def rev_list(repo_root, rev_range, *, offline=False):
    options = {'env': {'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': ''}} if offline else {}
    result = git(repo_root, 'rev-list', '--reverse', rev_range, **options)
    commits = [line for line in result.stdout.splitlines() if line.strip()]
    remember_commits(repo_root, commits)
    return commits


def commit_list_for_spec(repo_root, spec):
    if '..' in spec:
        return rev_list(repo_root, spec)
    if not commit_exists(repo_root, spec):
        raise SyncwheelError(f'commit does not exist: {spec}')
    return [git(repo_root, 'rev-parse', spec).stdout.strip()]


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


def primary_checkout_absorb_commands(repo_root, manifest, manifest_path, stack_ids):
    if repo_root is None or manifest_path is None:
        return []
    entries = [
        entry for entry in local_worktree_status(repo_root) or []
        if not entry.startswith('?? ') and not primary_checkout_state_entry(entry)
    ]
    if not entries or any(
        entry[:2] in {'DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'} for entry in entries
    ):
        return []
    staged_paths = git(
        repo_root, 'diff', '--cached', '--name-only', '--no-renames', '-z', '--', env={'GIT_OPTIONAL_LOCKS': '0'},
    ).stdout.strip('\0').split('\0')
    staged = any(staged_paths)
    if staged and (
        any(path.startswith('.syncwheel/') for path in staged_paths)
        or any(entry[0] != ' ' and entry[1] != ' ' for entry in entries)
    ):
        return []
    paths = staged_paths if staged else git(
        repo_root, 'diff', '--name-only', '--no-renames', '-z', '--', env={'GIT_OPTIONAL_LOCKS': '0'},
    ).stdout.split('\0')
    paths = [path for path in paths if path and not path.startswith('.syncwheel/')]
    if not paths:
        return []
    # Preserve exact paths even if a protected file name contains Git
    # pathspec syntax or would otherwise be parsed as a command option.
    paths = [
        ':(literal)' + path if any(char in path for char in '*?[\\') or path.startswith(('-', ':'))
        else path
        for path in paths
    ]
    branch = get_current_branch(repo_root)
    default_branch = manifest.get('integration', {}).get('branch')
    stacks = stack_map(manifest)
    targets = [
        identifier for identifier in stack_ids
        if not stacks[identifier].get('adopted_history')
        and (stacks[identifier].get('integration_branch') or default_branch) == branch
    ]
    has_divergent_target = False
    if targets and not stacks[targets[0]]['commits']:
        try:
            _stack_lifecycle(repo_root).require_empty_stack_absorb_base(stacks[targets[0]])
        except SyncwheelError:
            targets = []
            has_divergent_target = True
    selection = manifest_path_remedy_options(repo_root, manifest_path)
    commands = []
    if (not stack_ids or has_divergent_target) and branch == default_branch:
        if not staged:
            commands.append(
                'syncwheel stack create --draft <stack-id> --purpose "Capture primary checkout work" '
                + shlex.join(selection)
            )
        if not staged or stacks:
            targets = ['<stack-id>']
    # Show a bounded set of exact examples. A staged absorb must account
    # for the whole staged product patch to pass its separate index guard.
    if staged and len(paths) > 5:
        return commands
    path_groups = [paths] if staged else [[path] for path in paths[:5]]
    for identifier in targets:
        target = identifier if identifier == '<stack-id>' else shlex.quote(identifier)
        for group in path_groups:
            rest = [
                *group, *(['--staged'] if staged else []),
                '-N', '-m', 'Describe the changes', *selection,
            ]
            commands.append(f'syncwheel stack absorb {target} {shlex.join(rest)}')
    return commands


def primary_checkout_remedy_commands(manifest, stack_id=None, repo_root=None, manifest_path=None):
    """Offer worktree or patch remedies without treating a patch as a commit."""
    if manifest.get('repository_mode') == 'journal':
        return ['syncwheel journal snapshot --apply', 'syncwheel journal publish --apply']
    operation = interrupted_git_operation(repo_root) if repo_root is not None else None
    if operation:
        return [
            f'syncwheel int git -- {operation} --continue',
            f'syncwheel int git -- {operation} --abort',
        ]
    if (
        manifest_is_local_only(manifest)
        and repo_root is not None
        and control_manifest_owns_the_blocking_dirt(repo_root)
    ):
        # Capturing into a stack would publish the manifest this repo keeps local.
        commands = []
        if repo_root is not None and staged_control_manifest_migration(repo_root):
            commands.append(
                'git commit -m "chore: untrack the Syncwheel manifest" '
                '-- .syncwheel/manifest.json'
            )
        commands.append('syncwheel int rebuild --reason "<why this rebuild>"')
        return commands
    stack_ids = manifest_remedy_stack_ids(manifest, stack_id) if stack_id else []
    commands = primary_checkout_absorb_commands(repo_root, manifest, manifest_path, stack_ids)
    selection = (
        ' ' + shlex.join(manifest_path_remedy_options(repo_root, manifest_path))
        if repo_root is not None and manifest_path is not None else ''
    )
    commands.extend(
        f'syncwheel worktree open <lane> --into {shlex.quote(identifier)}{selection}'
        for identifier in stack_ids
    )
    if not commands:
        commands.append(f'syncwheel worktree open <lane>{selection}')
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
    duplicate = (f"If all changes are already declared, preview: {retire_copy_remedy(lane['id'])}. "
                 if commit_first and lane.get('target') else '')
    return duplicate + prefix + 'Use: ' + '; '.join(commands)


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


def checked_repo_profile_path(repo_root):
    path = repo_profile_path(repo_root)
    if path.parent.is_symlink():
        raise SyncwheelError(f'profile directory is a symbolic link: {path.parent}')
    if path.is_symlink():
        raise SyncwheelError(f'profile path is a symbolic link: {path}')
    if path.exists() and not path.is_file():
        raise SyncwheelError(f'profile path is not a regular file: {path}')
    return path


def save_repo_profile(repo_root, profile):
    return atomic_write_private_json(checked_repo_profile_path(repo_root), profile, indent=2)


def normalize_github_pr_merge_policy(value, path='github_pr_merge'):
    """Validate the clone-local, deliberately narrow GitHub merge policy."""
    if not isinstance(value, dict):
        raise SyncwheelError(f'{path} must be an object')
    allowed = {
        'enabled', 'repository', 'base_branches', 'merge_method',
        'allowed_bypasses', 'merge_actors', 'pr_authors', 'commit_authors',
        'head_repositories', 'checks', 'integration',
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise SyncwheelError(f'{path} contains unsupported keys: ' + ', '.join(unknown))

    def string_list(field, *, required=False, allow_empty=False):
        candidate = value.get(field)
        if candidate is None and not required:
            return None
        if not isinstance(candidate, list) or (not candidate and not allow_empty):
            raise SyncwheelError(f'{path}.{field} must be a non-empty string array')
        normalized = []
        for index, item in enumerate(candidate):
            if not isinstance(item, str) or not item.strip() or '\x00' in item:
                raise SyncwheelError(f'{path}.{field}[{index}] must be a non-empty string')
            normalized.append(item.strip())
        if len(set(item.casefold() for item in normalized)) != len(normalized):
            raise SyncwheelError(f'{path}.{field} contains duplicate case-insensitive values')
        return normalized

    enabled = value.get('enabled')
    if not isinstance(enabled, bool):
        raise SyncwheelError(f'{path}.enabled must be a boolean')
    repository = value.get('repository')
    if not isinstance(repository, str) or not re.fullmatch(
        r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository.strip()
    ):
        raise SyncwheelError(f'{path}.repository must be an OWNER/REPO identifier')
    base_branches = string_list('base_branches', required=True)
    for index, item in enumerate(base_branches):
        if any(char in item for char in '?[]\\') or ('*' in item and not re.fullmatch(r'[^*]+/\*', item)):
            raise SyncwheelError(
                f'{path}.base_branches[{index}] must be a branch name or a PREFIX/* pattern'
            )
    method = value.get('merge_method')
    if method not in GITHUB_PR_MERGE_METHODS:
        raise SyncwheelError(
            f'{path}.merge_method must be one of: ' + ', '.join(sorted(GITHUB_PR_MERGE_METHODS))
        )
    bypasses = string_list('allowed_bypasses', required=True, allow_empty=True)
    if any(item not in GITHUB_PR_MERGE_BYPASSES for item in bypasses):
        accepted = ', '.join(sorted(GITHUB_PR_MERGE_BYPASSES))
        raise SyncwheelError(f'{path}.allowed_bypasses accepts only: {accepted}')
    checks = value.get('checks')
    if checks not in GITHUB_PR_MERGE_CHECKS:
        accepted = ', '.join(sorted(GITHUB_PR_MERGE_CHECKS))
        raise SyncwheelError(f'{path}.checks accepts only: {accepted}')
    integration = value.get('integration', 'required')
    if integration not in GITHUB_PR_MERGE_INTEGRATION:
        accepted = ', '.join(sorted(GITHUB_PR_MERGE_INTEGRATION))
        raise SyncwheelError(f'{path}.integration accepts only: {accepted}')
    merge_actors = string_list('merge_actors', required=True)
    filters = {
        field: string_list(field)
        for field in ('pr_authors', 'commit_authors', 'head_repositories')
    }
    if not any(filters.values()):
        raise SyncwheelError(
            f'{path} requires at least one of pr_authors, commit_authors, or head_repositories'
        )
    normalized = {
        'enabled': enabled,
        'repository': repository.strip(),
        'base_branches': base_branches,
        'merge_method': method,
        'allowed_bypasses': bypasses,
        'merge_actors': merge_actors,
        'checks': checks,
    }
    if integration != 'required':
        normalized['integration'] = integration
    for field, candidate in filters.items():
        if candidate is not None:
            normalized[field] = candidate
    return normalized


def github_pr_merge_base_allowed(base_branch, allowed):
    """Match a delivery base against exact names and PREFIX/* release patterns."""
    if not isinstance(base_branch, str) or not base_branch:
        return False
    for item in allowed:
        if item.endswith('/*'):
            if base_branch.startswith(item[:-1]) and len(base_branch) > len(item) - 1:
                return True
        elif base_branch == item:
            return True
    return False


def github_pr_merge_policy_from_profile(profile):
    value = profile.get(GITHUB_PR_MERGE_POLICY_KEY)
    if value is None:
        return None
    return normalize_github_pr_merge_policy(value)


def github_pr_merge_policy_status(repo_root):
    path = repo_profile_path(repo_root)
    profile = load_repo_profile(repo_root)
    policy = None
    error = None
    if GITHUB_PR_MERGE_POLICY_KEY in profile:
        try:
            policy = github_pr_merge_policy_from_profile(profile)
        except SyncwheelError as exc:
            error = str(exc)
    ignored = git(
        repo_root, 'check-ignore', '--quiet', '--no-index', '--',
        '.syncwheel/profile.local.json', check=False,
    ).returncode == 0
    tracked = git_path_is_tracked(repo_root, path)
    return {
        'path': str(path),
        'configured': policy is not None and error is None,
        'enabled': bool(policy and policy.get('enabled')),
        'policy': policy,
        'error': error,
        'ignored': ignored,
        'tracked': tracked,
        'failClosed': not (policy and policy.get('enabled')) or bool(error) or tracked or not ignored,
    }


def ensure_github_pr_merge_policy_is_private(repo_root):
    path = checked_repo_profile_path(repo_root)
    if git_path_is_tracked(repo_root, path):
        raise SyncwheelError(
            f'{path} is tracked; refusing to write a private PR merge policy'
        )
    ignored = git(
        repo_root, 'check-ignore', '--quiet', '--no-index', '--',
        '.syncwheel/profile.local.json', check=False,
    ).returncode == 0
    if not ignored:
        raise SyncwheelError(
            '.syncwheel/profile.local.json is not ignored; refusing to write a private PR merge policy'
        )


def github_pr_merge_policy_edit(repo_root, proposed, *, apply, clear=False, json_mode=False):
    ensure_github_pr_merge_policy_is_private(repo_root)
    current_profile = load_repo_profile(repo_root)
    next_profile = dict(current_profile)
    if clear:
        next_profile.pop(GITHUB_PR_MERGE_POLICY_KEY, None)
    else:
        next_profile[GITHUB_PR_MERGE_POLICY_KEY] = normalize_github_pr_merge_policy(proposed)
    output = {
        'path': str(repo_profile_path(repo_root)),
        'current': current_profile.get(GITHUB_PR_MERGE_POLICY_KEY),
        'proposed': next_profile.get(GITHUB_PR_MERGE_POLICY_KEY),
        'preservedKeys': sorted(key for key in current_profile if key != GITHUB_PR_MERGE_POLICY_KEY),
        'ignored': True,
        'tracked': False,
        'applied': bool(apply),
        'dryRun': not apply,
    }
    if not apply:
        print(json.dumps(output, indent=2, sort_keys=True) if json_mode else json.dumps(output, indent=2))
        return 0
    save_repo_profile(repo_root, next_profile)
    append_ledger_event(
        repo_root,
        'github_pr_merge_policy_changed',
        {
            'operation': 'clear' if clear else 'set',
            'enabled': bool(next_profile.get(GITHUB_PR_MERGE_POLICY_KEY, {}).get('enabled')),
            'repository': next_profile.get(GITHUB_PR_MERGE_POLICY_KEY, {}).get('repository'),
        },
        repo_root / '.syncwheel' / 'manifest.json',
    )
    output['status'] = github_pr_merge_policy_status(repo_root)
    print(json.dumps(output, indent=2, sort_keys=True) if json_mode else json.dumps(output, indent=2))
    return 0


def command_repo_pr_merge_policy_status(args):
    repo_root = resolve_repo_root(args.repo)
    report = github_pr_merge_policy_status(repo_root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"policy: {'configured' if report['configured'] else 'missing/invalid'}")
        print(f"enabled: {'yes' if report['enabled'] else 'no'}")
        print(f"private: {'yes' if report['ignored'] and not report['tracked'] else 'no'}")
        if report['error']:
            print(f"error: {report['error']}")
    return 0


def command_repo_pr_merge_policy_set(args):
    repo_root = resolve_repo_root(args.repo)
    proposed = {
        'enabled': True,
        'repository': args.repository,
        'base_branches': list(args.base),
        'merge_method': args.method,
        'allowed_bypasses': list(args.allow_bypass),
        'merge_actors': list(args.merge_actor),
        'checks': args.checks,
    }
    if args.integration != 'required':
        proposed['integration'] = args.integration
    for option, field in (
        (args.pr_author, 'pr_authors'),
        (args.commit_author, 'commit_authors'),
        (args.head_repository, 'head_repositories'),
    ):
        if option:
            proposed[field] = list(option)
    return github_pr_merge_policy_edit(
        repo_root, proposed, apply=args.apply, json_mode=args.json,
    )


def command_repo_pr_merge_policy_clear(args):
    repo_root = resolve_repo_root(args.repo)
    return github_pr_merge_policy_edit(
        repo_root, None, apply=args.apply, clear=True, json_mode=args.json,
    )


def github_adapter_request(repo_root, request):
    """Call only the fixed Syncwheel GitHub adapter entrypoint."""
    adapter = shutil.which('syncwheel-github')
    command = [adapter] if adapter else [sys.executable, str(Path(__file__).with_name('syncwheel_github.py'))]
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            input=json.dumps(request, sort_keys=True),
            text=True,
            capture_output=True,
            timeout=GITHUB_PR_MERGE_ADAPTER_TIMEOUT_SECONDS,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncwheelError(f'GitHub adapter failed: {exc}') from exc
    output = result.stdout or ''
    if len(output) > GITHUB_PR_MERGE_ADAPTER_MAX_OUTPUT:
        raise SyncwheelError('GitHub adapter response exceeds the merge observation size limit')
    try:
        response = json.loads(output)
    except json.JSONDecodeError as exc:
        detail = (result.stderr or result.stdout or 'adapter returned invalid JSON').strip()
        raise SyncwheelError(f'GitHub adapter returned invalid JSON: {detail[:1000]}') from exc
    if not isinstance(response, dict) or response.get('ok') is not True:
        # A GitHub merge command can return a non-zero status after GitHub has
        # accepted or rejected the request. Preserve that bounded command
        # result so the caller can re-observe the PR and write a receipt
        # instead of losing the operation at the adapter boundary.
        if (
            request.get('operation') == 'merge'
            and isinstance(response, dict)
            and isinstance(response.get('returncode'), int)
            and isinstance(response.get('argv'), list)
        ):
            return response
        detail = response.get('error') if isinstance(response, dict) else None
        raise SyncwheelError(f'GitHub adapter error: {(detail or result.stderr or "unknown error")[:1000]}')
    return response.get('observation', response)


def github_blocker(blockers, code, detail, **evidence):
    item = {'code': code, 'detail': detail}
    if evidence:
        item['evidence'] = evidence
    blockers.append(item)


def github_ci_check_result(check, head):
    if not isinstance(check, dict):
        return None
    if 'headSha' in check and (not head or check['headSha'] != head):
        return None
    if check.get('kind') == 'check-run' and (
        not isinstance(check.get('name'), str) or not check['name']
        or str(check.get('status') or '').upper() != 'COMPLETED'
    ):
        return None
    status = str(check.get('conclusion') or check.get('status') or '').upper()
    return status if status in {'SUCCESS', 'SKIPPED'} else None


def github_check_positive_integer(value):
    return type(value) is int and value > 0


def github_workflow_run_creation(check):
    value = check.get('runCreatedAt')
    if not isinstance(value, str):
        return None
    try:
        created = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    return created if created.tzinfo is not None else None


def github_effective_checks(checks, head):
    groups = {}
    for index, check in enumerate(checks):
        if (isinstance(check, dict) and check.get('kind') == 'check-run'
                and head and check.get('headSha') == head
                and isinstance(check.get('name'), str) and check['name']
                and all(github_check_positive_integer(check.get(key))
                        for key in ('appId', 'workflowId', 'checkRunId'))):
            key = (check['appId'], check['workflowId'], check['name'])
            groups.setdefault(key, []).append((index, check))

    superseded_indexes = set()
    for group in groups.values():
        by_run = {}
        run_order = {}
        for index, check in group:
            run_id = check.get('runId')
            number = check.get('runNumber')
            created = github_workflow_run_creation(check)
            if (not github_check_positive_integer(run_id)
                    or not github_check_positive_integer(number) or created is None):
                break
            if run_order.setdefault(run_id, (number, created)) != (number, created):
                break
            by_run.setdefault(run_id, []).append((index, check))
        else:
            numbers = {number for number, _ in run_order.values()}
            ordered = sorted(run_order, key=run_order.get)
            dates = [run_order[run_id][1] for run_id in ordered]
            if len(numbers) != len(run_order) or dates != sorted(dates):
                continue
            attempts_by_run = {
                run_id: [check.get('runAttempt') for _, check in rows]
                for run_id, rows in by_run.items() if len(rows) > 1
            }
            if any(
                not all(github_check_positive_integer(attempt) for attempt in attempts)
                or len(set(attempts)) != len(attempts)
                for attempts in attempts_by_run.values()
            ):
                continue
            for run_id in ordered[:-1]:
                superseded_indexes.update(index for index, _ in by_run[run_id])
            if ordered[-1] in attempts_by_run:
                newest_attempt = max(attempts_by_run[ordered[-1]])
                superseded_indexes.update(
                    index for index, check in by_run[ordered[-1]] if check['runAttempt'] < newest_attempt
                )


    effective, superseded = [], []
    for index, check in enumerate(checks):
        (superseded if index in superseded_indexes else effective).append(check)
    return (
        sorted(effective, key=lambda check: json.dumps(check, sort_keys=True)),
        sorted(superseded, key=lambda check: json.dumps(check, sort_keys=True)),
    )


def github_required_checks(rules):
    """Extract the name and optional provider of each observed required check."""
    def requirement(item, provider_key):
        if isinstance(item, str):
            return (item, None) if item else None
        if not isinstance(item, dict):
            return None
        name = item.get('context') or item.get('name')
        if not isinstance(name, str) or not name:
            return None
        provider = item.get(provider_key)
        if provider is None or (provider_key == 'app_id' and type(provider) is int and provider == -1):
            return name, None
        return name, provider if github_check_positive_integer(provider) else 'unproven'

    required_checks = []
    protection = rules.get('branchProtection') if isinstance(rules, dict) else None
    required = protection.get('required_status_checks') if isinstance(protection, dict) else None
    if isinstance(required, dict):
        explicit = [item for candidate in required.get('checks') or []
                    if (item := requirement(candidate, 'app_id')) is not None]
        explicit_names = {name for name, _ in explicit}
        required_checks.extend(explicit)
        required_checks.extend(item for candidate in required.get('contexts') or []
                               if (item := requirement(candidate, 'app_id')) and item[0] not in explicit_names)
    rulesets = rules.get('rulesets') if isinstance(rules, dict) else None
    if isinstance(rulesets, dict):
        rulesets = rulesets.get('rules') or rulesets.get('nodes') or []
    for rule in rulesets or []:
        if not isinstance(rule, dict) or str(rule.get('type') or rule.get('rule_type') or '').lower() != 'required_status_checks':
            continue
        parameters = rule.get('parameters') or {}
        required_checks.extend(item for candidate in parameters.get('required_status_checks') or []
                               if (item := requirement(candidate, 'integration_id')) is not None)
    return list(dict.fromkeys(required_checks))


def github_rule_review_required(rules):
    protection = rules.get('branchProtection') if isinstance(rules, dict) else None
    if isinstance(protection, dict) and protection.get('required_pull_request_reviews'):
        return True
    rulesets = rules.get('rulesets') if isinstance(rules, dict) else None
    if isinstance(rulesets, dict):
        rulesets = rulesets.get('rules') or rulesets.get('nodes') or []
    for rule in rulesets or []:
        if not isinstance(rule, dict):
            continue
        rule_type = str(rule.get('type') or rule.get('rule_type') or '').lower()
        parameters = rule.get('parameters') or {}
        if rule_type in {'pull_request', 'required_pull_request_reviews'}:
            count = parameters.get('required_approving_review_count', 1) if isinstance(parameters, dict) else 1
            try:
                requires_approval = int(count) > 0
            except (TypeError, ValueError):
                requires_approval = True
            if not parameters or requires_approval:
                return True
    return False


def github_private_free_rules_unavailable(repository_info, rules):
    """Recognize GitHub Free's explicit private-repository rules API limit."""
    if not isinstance(repository_info, dict) or repository_info.get('isPrivate') is not True:
        return False
    if not isinstance(rules, dict):
        return False
    plan_limit = 'upgrade to github pro or make this repository public to enable this feature'
    unavailable = False
    for prefix in ('branchProtection', 'rulesets'):
        status = rules.get(f'{prefix}Status')
        detail = str(rules.get(f'{prefix}Error') or '').casefold()
        if status in (None, 200, 404):
            continue
        if status == 403 and plan_limit in detail:
            unavailable = True
            continue
        return False
    return unavailable


def github_rules_blockers(rules, blockers, *, repository_info=None, allowed_bypasses=(), warnings=None):
    if not isinstance(rules, dict):
        return None
    private_free_fallback = (
        'private_free_rules' in set(allowed_bypasses or [])
        and github_private_free_rules_unavailable(repository_info, rules)
    )
    if private_free_fallback and warnings is not None:
        warnings.append({
            'code': 'private_free_rules_unavailable',
            'detail': (
                'GitHub Free does not expose rules APIs for this private repository; '
                'the merge remains subject to GitHub server-side enforcement'
            ),
        })
    if rules.get('mergeQueue') is True or rules.get('merge_queue') is True:
        github_blocker(blockers, 'merge_queue', 'merge queue is enabled and unsupported')
    if (
        not private_free_fallback
        and rules.get('branchProtectionStatus') not in (None, 200, 404)
        and rules.get('branchProtectionError')
    ):
        github_blocker(blockers, 'rules_unavailable', 'GitHub branch protection could not be observed')
    rulesets = rules.get('rulesets')
    if isinstance(rulesets, dict):
        rulesets = rulesets.get('rules') or rulesets.get('nodes') or []
    known = {
        'pull_request', 'required_pull_request_reviews', 'required_status_checks',
        'non_fast_forward', 'deletion', 'required_linear_history', 'merge_queue',
    }
    for rule in rulesets or []:
        if not isinstance(rule, dict):
            continue
        rule_type = str(rule.get('type') or rule.get('rule_type') or '').lower()
        if rule_type and rule_type not in known:
            github_blocker(blockers, 'unknown_rule', f'unrecognized GitHub rule: {rule_type}')
        if rule_type == 'merge_queue':
            github_blocker(blockers, 'merge_queue', 'merge queue rule is unsupported')
    if (
        not private_free_fallback
        and rules.get('rulesetsStatus') not in (None, 200, 404)
        and rules.get('rulesetsError')
    ):
        github_blocker(blockers, 'rules_unavailable', 'GitHub rules could not be observed')
    return 'private_free_rules' if private_free_fallback else None


def github_stack_git_preflight(repo_root, manifest, stack, blockers, warnings, integration_validation='required'):
    source_revision = None
    declared = []
    try:
        declared = [commit_full_sha(repo_root, item) for item in stack.get('commits', [])]
    except SyncwheelError as exc:
        github_blocker(blockers, 'stack_commit_missing', str(exc))
    if stack.get('state', 'published') != 'published':
        github_blocker(blockers, 'stack_not_published', 'stack must have state=published')
    if not isinstance(stack.get('branch'), str) or not branch_exists(repo_root, stack['branch']):
        github_blocker(blockers, 'source_branch_missing', 'stack source branch is missing')
    else:
        source_revision = ref_tip(repo_root, stack['branch'])
        if declared or stack.get('adopted_history'):
            try:
                actual = rev_list(repo_root, f"{stack.get('base', '')}..{stack['branch']}")
            except SyncwheelError as exc:
                github_blocker(blockers, 'stack_base_missing', str(exc))
            else:
                try:
                    expected = declared_stack_history(repo_root, stack)
                    adopted = validate_stack_adoption(stack)
                except SyncwheelError as exc:
                    github_blocker(blockers, 'stack_projection_mismatch', str(exc))
                else:
                    if actual != expected or (adopted and source_revision != adopted['tip']):
                        github_blocker(
                            blockers, 'stack_projection_mismatch',
                            'source branch does not exactly equal declared stack history',
                            declared=expected, actual=actual,
                        )
    remote = stack.get('target_remote') or manifest['defaults']['publication_remote']
    if not remote_is_configured(repo_root, remote):
        github_blocker(blockers, 'publication_remote_missing', f'publication remote is not configured: {remote}')
    elif stack.get('branch'):
        try:
            remote_tip = remote_ref_tips(repo_root, remote, [f"refs/heads/{stack['branch']}"])[f"refs/heads/{stack['branch']}"]
            if not remote_tip:
                github_blocker(blockers, 'source_remote_missing', 'source branch is absent on the publication remote')
            elif source_revision and remote_tip != source_revision:
                github_blocker(blockers, 'source_remote_drift', 'remote source branch differs from stack revision', remote=remote_tip, expected=source_revision)
        except SyncwheelError as exc:
            github_blocker(blockers, 'source_remote_unavailable', str(exc))
    integration = manifest.get('integration') or {}
    integration_branch = integration.get('branch')
    in_integration = stack.get('id') in integration.get('stacks', [])
    if manifest['defaults'].get('integration_membership') == INTEGRATION_MEMBERSHIP_REQUIRED and not in_integration:
        github_blocker(blockers, 'integration_membership', 'stack is not present in required integration')
    elif in_integration and (not integration_branch or not branch_exists(repo_root, integration_branch)):
        github_blocker(blockers, 'integration_missing', 'integration branch is missing')
    elif in_integration:
        missing = [
            commit for commit in stack_integration_commits(stack)
            if not branch_contains(repo_root, integration_branch, commit)
        ]
        # The clone-local policy may declare integration validation advisory:
        # the PR is then judged on its own checks, pinned head and live base,
        # and an integration branch that is behind (for example because other
        # stacks fail to rebuild) is reported instead of blocking. Membership
        # rules above still apply.
        if missing and integration_validation == 'advisory':
            warnings.append({
                'code': 'integration_not_validated',
                'detail': (
                    f'{len(missing)} stack commit(s) are absent from {integration_branch}; '
                    'the merge policy declares integration validation advisory'
                ),
                'commits': missing,
            })
        else:
            for commit in missing:
                github_blocker(
                    blockers, 'integration_not_validated',
                    f'stack commit is absent from integration: {commit}; rebuild the integration, '
                    'or declare integration validation advisory in the clone-local merge policy',
                )
    for dependency_id in stack.get('depends_on', []):
        try:
            dependency = require_stack(manifest, dependency_id)
        except SyncwheelError as exc:
            github_blocker(blockers, 'dependency_missing', str(exc), dependency=dependency_id)
            continue
        if not branch_contains(repo_root, stack.get('base'), dependency['branch']):
            github_blocker(blockers, 'dependency_not_absorbed', f'dependency is not absorbed: {dependency_id}')
    try:
        diagnostics = governed_worktree_diagnostics(repo_root, manifest)
        for lane in diagnostics.get('lanes', []):
            if lane.get('target') == stack.get('id') and lane.get('code') in {'dirty', 'locked', 'lane_in_use', 'unavailable'}:
                github_blocker(blockers, 'associated_lane_dirty', lane.get('remedy') or lane.get('code'))
            elif lane.get('code') == 'dirty':
                warnings.append({'code': 'unrelated_dirty_worktree', 'path': lane.get('path')})
    except (OSError, SyncwheelError) as exc:
        warnings.append({'code': 'worktree_diagnostics_unavailable', 'detail': str(exc)[:400]})
    return source_revision, declared


def github_pr_merge_plan_digest(plan):
    unsigned = copy.deepcopy(plan)
    unsigned.pop('planDigest', None)
    unsigned.pop('operationId', None)
    unsigned.pop('supersededChecks', None)
    return canonical_json_digest(unsigned)


def read_primary_authority_manifest(shared_path):
    """Read the canonical manifest without following control-path redirects."""
    try:
        directory = os.open(shared_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            descriptor = os.open(
                shared_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, encoding='utf-8') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise SyncwheelError('canonical manifest must be a regular file')
                return normalize_manifest_data(json.load(stream), shared_path)
        finally:
            os.close(directory)
    except FileNotFoundError:
        return None
    except (AttributeError, OSError, TypeError, ValueError, SyncwheelError) as exc:
        raise SyncwheelError(f'cannot read canonical primary authority from {shared_path}: {exc}') from exc


def primary_authority_manifest_path(repo_root):
    worktrees = get_worktrees(repo_root)
    if not worktrees or not worktrees[0].get('path'):
        raise SyncwheelError('cannot identify the primary worktree for repository authority')
    return Path(worktrees[0]['path']).resolve() / '.syncwheel' / 'manifest.json'


def validate_selected_primary_authority_path(repo_root, manifest_path):
    shared_path = primary_authority_manifest_path(repo_root)
    selected_path = Path(manifest_path)
    if Path(os.path.abspath(selected_path)) == shared_path or selected_path.resolve() == shared_path:
        read_primary_authority_manifest(shared_path)


def resolve_repository_authority(repo_root, manifest, manifest_path):
    shared_path = primary_authority_manifest_path(repo_root)
    shared = read_primary_authority_manifest(shared_path)
    shared_policy = manifest_authority(shared)
    selected_path = Path(manifest_path).resolve()
    selected_declared = 'authority' in manifest
    shared_declared = shared is not None and 'authority' in shared
    if selected_path == shared_path:
        policy = shared_policy
        source = 'shared' if shared_declared else 'default'
    elif not selected_declared:
        policy = shared_policy
        source = 'inherited' if shared_declared else 'default'
    else:
        selected = manifest_authority(manifest)
        excess = sorted(set(selected['allow']) - set(shared_policy['allow']))
        if excess:
            raise SyncwheelError(
                f'selected authority exceeds canonical primary authority at {shared_path}: '
                + ', '.join(excess)
            )
        policy = normalize_authority_policy({
            **selected,
            'deny': sorted(set(selected['deny']) | set(shared_policy['deny'])),
        })
        source = 'selected'
    return policy, {
        'source': source,
        'selected_manifest_path': str(selected_path),
        'shared_manifest_path': str(shared_path),
        'shared_manifest_present': shared is not None,
        'shared_authority_present': shared_declared,
        'shared_policy': shared_policy,
    }


def build_github_pr_merge_plan(repo_root, manifest, manifest_path, stack_id, args):
    require_delivery_manifest(manifest)
    policy = github_pr_merge_policy_from_profile(load_repo_profile(repo_root))
    if policy is None:
        raise SyncwheelError('GitHub PR merge policy is missing or invalid; configure profile.local.json first')
    if not policy['enabled']:
        raise SyncwheelError('GitHub PR merge policy is disabled')
    authority, authority_provenance = resolve_repository_authority(repo_root, manifest, manifest_path)
    if authority['mode'] != AUTHORITY_MODE_AI_MANAGED or AUTHORITY_CLASS_SOURCE_CHANGE not in authority['allow']:
        raise SyncwheelError('GitHub PR merge requires authority.mode=ai-managed with source_change allowed')
    stack = require_stack(manifest, stack_id)
    blockers = []
    warnings = []
    validation = validate_manifest(repo_root, manifest, manifest_path)
    for error in validation.get('errors', []):
        github_blocker(blockers, 'manifest_invalid', error)
    source_revision, declared = github_stack_git_preflight(
        repo_root, manifest, stack, blockers, warnings,
        integration_validation=policy.get('integration', 'required'),
    )
    base_branch = stack.get('target_branch') or manifest['defaults']['base_branch']
    if not github_pr_merge_base_allowed(base_branch, policy['base_branches']):
        github_blocker(blockers, 'base_branch_not_allowed', f'base branch is not allowlisted: {base_branch}')
    pr_number = None
    github = stack.get('github')
    if isinstance(github, dict):
        pr_number = github.get('pr')
    if pr_number is not None and (isinstance(pr_number, bool) or not re.fullmatch(r'\d+', str(pr_number))):
        github_blocker(blockers, 'invalid_pr_number', 'stack github.pr must be a positive integer')
        pr_number = None
    request = {
        'operation': 'observe',
        'repository': policy['repository'],
        'targetRemote': stack.get('target_remote') or manifest['defaults']['publication_remote'],
        'baseBranch': base_branch,
        'headBranch': stack.get('branch'),
        'pullRequestNumber': int(pr_number) if pr_number is not None else None,
    }
    observation = None
    adapter_error = None
    try:
        observation = github_adapter_request(repo_root, request)
    except SyncwheelError as exc:
        adapter_error = str(exc)
        github_blocker(blockers, 'github_observation_failed', adapter_error)
    pr = (observation or {}).get('pr') if isinstance(observation, dict) else None
    selection = (observation or {}).get('selection') if isinstance(observation, dict) else None
    if pr is None:
        count = selection.get('count') if isinstance(selection, dict) else None
        if count == 0:
            github_blocker(blockers, 'pull_request_not_found', 'no open PR matched exact head and base')
        elif count is not None:
            github_blocker(blockers, 'pull_request_ambiguous', f'exact PR lookup returned {count} matches')
        else:
            github_blocker(blockers, 'pull_request_missing', 'adapter did not return a unique pull request')
    identity = (observation or {}).get('identity') if isinstance(observation, dict) else {}
    repository_info = (observation or {}).get('repositoryInfo') if isinstance(observation, dict) else {}
    rules = (observation or {}).get('rules') if isinstance(observation, dict) else {}
    if isinstance(observation, dict) and observation.get('repository', '').casefold() != policy['repository'].casefold():
        github_blocker(blockers, 'repository_mismatch', 'observed repository differs from local policy')
    actor = identity.get('login') if isinstance(identity, dict) else None
    if not isinstance(actor, str) or actor.casefold() not in {item.casefold() for item in policy['merge_actors']}:
        github_blocker(blockers, 'merge_actor_not_allowed', 'effective gh actor is not allowlisted', actor=actor)
    permissions = repository_info.get('permissions') if isinstance(repository_info, dict) else {}
    if not isinstance(permissions, dict) or permissions.get('admin') is not True:
        github_blocker(blockers, 'merge_actor_not_admin', 'effective gh actor has no repository admin permission')
    checks, superseded_checks = [], []
    if isinstance(pr, dict):
        if pr_number is not None and pr.get('number') != int(pr_number):
            github_blocker(blockers, 'pull_request_number_mismatch', 'observed PR number differs from the stack declaration')
        if pr.get('state') != 'OPEN':
            github_blocker(blockers, 'pull_request_not_open', 'pull request is not open')
        if pr.get('isDraft') is True:
            github_blocker(blockers, 'pull_request_draft', 'pull request is draft')
        if pr.get('headRefName') != stack.get('branch'):
            github_blocker(blockers, 'head_branch_mismatch', 'PR head branch differs from stack branch')
        if source_revision and pr.get('headRefOid') != source_revision:
            github_blocker(blockers, 'head_sha_mismatch', 'PR head SHA differs from stack revision')
        if pr.get('baseRefName') != base_branch:
            github_blocker(blockers, 'base_branch_mismatch', 'PR base branch differs from stack target')
        author = pr.get('author')
        if policy.get('pr_authors') is not None and (
            not isinstance(author, str) or author.casefold() not in {item.casefold() for item in policy['pr_authors']}
        ):
            github_blocker(blockers, 'pr_author_not_allowed', 'PR author is not allowlisted', author=author)
        head_repo = pr.get('headRepository')
        if policy.get('head_repositories') is not None and (
            not isinstance(head_repo, str) or head_repo.casefold() not in {item.casefold() for item in policy['head_repositories']}
        ):
            github_blocker(blockers, 'head_repository_not_allowed', 'PR source repository is not allowlisted', headRepository=head_repo)
        commit_authors = pr.get('commitAuthors') or []
        if policy.get('commit_authors') is not None:
            allowed = {item.casefold() for item in policy['commit_authors']}
            if not commit_authors:
                github_blocker(blockers, 'commit_authors_missing', 'commit author identities could not be resolved')
            for commit in commit_authors:
                login = commit.get('login') if isinstance(commit, dict) else None
                if not isinstance(login, str) or login.casefold() not in allowed:
                    github_blocker(blockers, 'commit_author_not_allowed', 'a commit author is not allowlisted', commit=commit.get('sha') if isinstance(commit, dict) else None, login=login)
        review = pr.get('review') or {}
        if review.get('changesRequested') or review.get('decision') == 'CHANGES_REQUESTED':
            github_blocker(blockers, 'changes_requested', 'review changes requested')
        if review.get('unresolvedThreads'):
            github_blocker(blockers, 'unresolved_review_threads', 'review has active unresolved threads')
        if pr.get('threadsTruncated') is True:
            github_blocker(blockers, 'review_threads_truncated', 'not all review threads were observed')
        decision = review.get('decision')
        if decision == 'REVIEW_REQUIRED' and not github_rule_review_required(rules):
            github_blocker(blockers, 'review_rule_unproven', 'review is required but the effective GitHub rule was not observed')
        elif decision not in (None, '', 'APPROVED', 'REVIEW_REQUIRED'):
            github_blocker(blockers, 'unknown_review_state', f'unrecognized review decision: {decision}')
        if pr.get('mergeable') != 'MERGEABLE':
            github_blocker(blockers, 'not_mergeable', f"mergeable={pr.get('mergeable')}")
        if pr.get('mergeStateStatus') in {'BEHIND', 'DIRTY', 'UNKNOWN', 'UNSTABLE'}:
            github_blocker(blockers, 'base_obsolete_or_dirty', f"mergeStateStatus={pr.get('mergeStateStatus')}")
        head = pr.get('headRefOid')
        checks, superseded_checks = github_effective_checks(pr.get('checks') or [], head)
        required_checks = github_required_checks(rules)
        if pr.get('checksIncomplete') is True and (policy['checks'] == 'all' or required_checks):
            github_blocker(blockers, 'checks_observation_incomplete', 'exact-head check history could not be observed')
        if policy['checks'] == 'all':
            if not checks:
                github_blocker(blockers, 'checks_missing', 'at least one successful check is required')
            else:
                for check in checks:
                    if github_ci_check_result(check, head) is None:
                        github_blocker(blockers, 'check_failed_or_pending', f'check did not conclude SUCCESS/SKIPPED: {check}')
        else:
            warnings.append({
                'code': 'checks_not_required',
                'detail': 'the clone-local merge policy does not require CI checks',
            })
        for name, provider in required_checks:
            named = [
                check for check in checks if isinstance(check, dict) and check.get('name') == name
            ]
            if provider is not None:
                matches = [check for check in named if github_check_positive_integer(check.get('appId'))
                           and check['appId'] == provider]
                unverified = [check for check in named if not github_check_positive_integer(check.get('appId'))]
                if not matches or provider == 'unproven':
                    github_blocker(blockers, 'required_check_missing', f'required check from its GitHub app is absent: {name}')
                    continue
                named = [*matches, *unverified]
            if not named:
                github_blocker(blockers, 'required_check_missing', f'required check is absent: {name}')
            for required_check in named:
                if github_ci_check_result(required_check, head) is None:
                    github_blocker(
                        blockers,
                        'required_check_failed_or_pending',
                        f'required check did not conclude SUCCESS/SKIPPED: {required_check}',
                    )
        if decision == 'REVIEW_REQUIRED' and 'required_reviews' not in policy['allowed_bypasses']:
            github_blocker(blockers, 'required_reviews_not_authorized', 'review bypass is not allowed by the merge policy')
            path = 'normal'
        elif decision == 'REVIEW_REQUIRED':
            path = 'admin-review-bypass'
        else:
            path = 'normal'
        if pr.get('mergeStateStatus') == 'BLOCKED' and decision != 'REVIEW_REQUIRED':
            github_blocker(blockers, 'blocked_for_unknown_reason', 'GitHub reports BLOCKED for a reason other than proven required review')
    else:
        path = 'normal'
    allow_methods = repository_info.get('allowMergeMethods') if isinstance(repository_info, dict) else {}
    if isinstance(allow_methods, dict) and allow_methods.get(policy['merge_method']) is not True:
        github_blocker(blockers, 'merge_method_disabled', f"merge method is disabled: {policy['merge_method']}")
    rules_fallback = github_rules_blockers(
        rules,
        blockers,
        repository_info=repository_info,
        allowed_bypasses=policy['allowed_bypasses'],
        warnings=warnings,
    )
    try:
        target_base_sha = remote_ref_tips(
            repo_root,
            stack.get('target_remote') or manifest['defaults']['publication_remote'],
            [f'refs/heads/{base_branch}'],
        )[f'refs/heads/{base_branch}']
    except SyncwheelError as exc:
        target_base_sha = None
        github_blocker(blockers, 'base_observation_failed', str(exc))
    if target_base_sha is None:
        github_blocker(blockers, 'base_remote_missing', 'delivery base branch is absent on the target remote')
    if pr and target_base_sha:
        target_remote = stack.get('target_remote') or manifest['defaults']['publication_remote']
        fetched = git(repo_root, 'fetch', '--quiet', target_remote, f'refs/heads/{base_branch}', check=False)
        fetched_base_sha = ref_tip(repo_root, 'FETCH_HEAD') if fetched.returncode == 0 else None
        if fetched_base_sha != target_base_sha:
            github_blocker(blockers, 'base_fetch_failed', 'delivery base changed or could not be fetched at its observed revision', observed=target_base_sha, fetched=fetched_base_sha)
        else:
            historical_base_sha = pr.get('baseRefOid')
            if historical_base_sha and not branch_contains(repo_root, target_base_sha, historical_base_sha):
                github_blocker(blockers, 'base_changed', 'PR base is not an ancestor of the live delivery base', pr=historical_base_sha, observed=target_base_sha)
            if source_revision and not branch_contains(repo_root, source_revision, target_base_sha):
                github_blocker(blockers, 'base_not_in_head', 'PR head does not contain the live delivery base', head=source_revision, observed=target_base_sha)
    if not isinstance(pr, dict):
        pr = {}
    number = pr.get('number') or (int(pr_number) if pr_number is not None else None)
    command = None
    if number and source_revision:
        command = ['gh', 'pr', 'merge', str(number), '--repo', policy['repository'], f"--{policy['merge_method']}"]
        if path == 'admin-review-bypass':
            command.append('--admin')
        command.extend(['--match-head-commit', source_revision])
    plan = {
        'schemaVersion': GITHUB_PR_MERGE_PLAN_SCHEMA_VERSION,
        'kind': 'githubPrMergePlan',
        'stack': stack_id,
        'branch': stack.get('branch'),
        'commit': source_revision,
        'declaredCommits': declared,
        'repository': policy['repository'],
        'targetRemote': stack.get('target_remote') or manifest['defaults']['publication_remote'],
        'baseBranch': base_branch,
        'pullRequest': {
            'number': number,
            'url': pr.get('url'),
            'headSha': pr.get('headRefOid'),
            'baseSha': pr.get('baseRefOid'),
        },
        'headSha': pr.get('headRefOid'),
        'baseSha': target_base_sha,
        'author': pr.get('author'),
        'commitAuthors': pr.get('commitAuthors') or [],
        'headRepository': pr.get('headRepository'),
        'mergeActor': actor,
        'checks': checks,
        'supersededChecks': superseded_checks,
        'review': pr.get('review') or {},
        'threads': (pr.get('review') or {}).get('threads') or [],
        'rules': rules,
        'rulesFallback': rules_fallback,
        'mergeMethod': policy['merge_method'],
        'path': path,
        'command': command,
        'adapterRequest': request,
        'policy': policy,
        'authority': authority,
        'authorityProvenance': authority_provenance,
        'source': {'branch': stack.get('branch'), 'revision': source_revision, 'base': stack.get('base'), 'commits': declared},
        'integration': {'branch': (manifest.get('integration') or {}).get('branch'), 'revision': ref_tip(repo_root, (manifest.get('integration') or {}).get('branch'))},
        'warnings': warnings,
        'blockers': blockers,
        'status': 'ready' if not blockers and command else 'blocked',
        'next': 'apply with the exact operationId and planDigest' if not blockers and command else 'resolve blockers and preview again',
    }
    plan['planDigest'] = github_pr_merge_plan_digest(plan)
    requested = normalize_channel_operation_id(getattr(args, 'operation_id', None))
    plan['operationId'] = requested or 'github-pr-merge-' + plan['planDigest'][:24]
    return plan


def github_pr_merge_events(repo_root, manifest_path, operation_id):
    return [
        event for event in _manifest_storage.load_ledger_events(repo_root, manifest_path)
        if event.get('type') in {'github_pr_merge_started', 'github_pr_merge_prepared', 'github_pr_merge_receipt'}
        and (event.get('payload') or {}).get('operationId') == operation_id
    ]


def record_github_pr_merge_event(repo_root, manifest_path, event_type, plan, **extra):
    payload = {
        'operationId': plan['operationId'],
        'planDigest': plan['planDigest'],
        'stack': plan['stack'],
        'plan': plan,
        **extra,
    }
    append_ledger_event(repo_root, event_type, payload, manifest_path)
    return payload


def github_pr_merge_postverify(repo_root, plan):
    remote = plan['source'].get('targetRemote') or plan['policy'].get('repository')
    target_remote = None
    stack = plan.get('source') or {}
    # The Git remote alias is intentionally taken from the stack/manifest, not
    # from the GitHub repository string. It is evidence only, never a command.
    try:
        target_remote = plan.get('targetRemote') or 'origin'
        base_ref = f"{target_remote}/{plan['baseBranch']}"
        fetched = git(
            repo_root, 'fetch', '--quiet', target_remote,
            f"refs/heads/{plan['baseBranch']}:refs/remotes/{target_remote}/{plan['baseBranch']}",
            check=False,
        )
        if fetched.returncode != 0:
            return {'status': 'unavailable', 'detail': 'could not fetch delivery base'}
        if plan['mergeMethod'] in {'squash', 'rebase'}:
            cherry = git(repo_root, 'cherry', base_ref, plan['branch'], check=False)
            if cherry.returncode != 0:
                return {'status': 'unavailable', 'detail': 'git cherry verification failed'}
            return {'status': 'verified', 'gitCherry': cherry.stdout.splitlines(), 'unabsorbed': any(line.startswith('+') for line in cherry.stdout.splitlines())}
        return {'status': 'verified', 'ancestry': branch_contains(repo_root, base_ref, plan['headSha'])}
    except (OSError, SyncwheelError) as exc:
        return {'status': 'unavailable', 'detail': str(exc)[:400]}


def github_pr_merge_classify(plan, observation, command_ok):
    pr = (observation or {}).get('pr') if isinstance(observation, dict) else None
    if isinstance(pr, dict) and pr.get('state') == 'MERGED' and pr.get('headRefOid') == plan.get('headSha'):
        return 'succeeded' if command_ok else 'succeeded-equivalent'
    if isinstance(pr, dict) and pr.get('state') == 'OPEN' and (
        pr.get('headRefOid') == plan.get('headSha') and pr.get('baseRefOid') == (plan.get('pullRequest') or {}).get('baseSha')
    ):
        return 'failed'
    return 'unknown'


def github_pr_merge_receipt(repo_root, manifest_path, plan, status, observation, command_result=None, detail=None):
    pr = (observation or {}).get('pr') if isinstance(observation, dict) else {}
    receipt = {
        'schemaVersion': GITHUB_PR_MERGE_PLAN_SCHEMA_VERSION,
        'kind': 'githubPrMergeReceipt',
        'stack': plan['stack'],
        'operationId': plan['operationId'],
        'planDigest': plan['planDigest'],
        'status': status,
        'path': plan['path'],
        'command': plan.get('command'),
        'pullRequest': plan.get('pullRequest'),
        'headSha': plan.get('headSha'),
        'mergedAt': pr.get('mergedAt') if isinstance(pr, dict) else None,
        'mergedBy': pr.get('mergedBy') if isinstance(pr, dict) else None,
        'mergeCommit': pr.get('mergeCommit') if isinstance(pr, dict) else None,
        'postVerification': github_pr_merge_postverify(repo_root, plan) if status.startswith('succeeded') else None,
        'adapterResult': command_result,
        'detail': detail,
        'recordedAt': iso_utc_now(),
    }
    append_ledger_event(repo_root, 'github_pr_merge_receipt', receipt, manifest_path)
    return receipt


def reconcile_github_pr_merge_operation(repo_root, manifest_path, operation_id, plan_digest):
    events = github_pr_merge_events(repo_root, manifest_path, operation_id)
    if not events:
        return None
    if any((event.get('payload') or {}).get('planDigest') != plan_digest for event in events):
        raise SyncwheelError(f'GitHub PR merge operation id collision: {operation_id} is bound to another plan')
    terminal = next((event.get('payload') or {} for event in reversed(events) if event.get('type') == 'github_pr_merge_receipt'), None)
    if terminal:
        return terminal
    prepared = next((event.get('payload') or {} for event in reversed(events) if event.get('type') == 'github_pr_merge_prepared'), None)
    if not prepared:
        raise SyncwheelError(f'GitHub PR merge operation {operation_id} is incomplete without prepared evidence')
    plan = prepared.get('plan') or {}
    observation = github_adapter_request(repo_root, {**(plan.get('adapterRequest') or {}), 'pullRequestNumber': (plan.get('pullRequest') or {}).get('number')})
    status = github_pr_merge_classify(plan, observation, False)
    return github_pr_merge_receipt(
        repo_root, manifest_path, plan, status, observation,
        detail='reconciled without retrying gh pr merge',
    )


def command_stack_merge_pr(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    if args.apply:
        if not args.operation_id or not args.plan_digest:
            raise SyncwheelError('stack merge-pr --apply requires --operation-id and --plan-digest')
        existing = reconcile_github_pr_merge_operation(
            repo_root, manifest_path, args.operation_id, args.plan_digest,
        )
        if existing:
            print(json.dumps(existing, indent=2, sort_keys=True))
            return 0 if existing.get('status', '').startswith('succeeded') else 2
    plan = build_github_pr_merge_plan(repo_root, manifest, manifest_path, args.stack, args)
    if not args.apply:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if args.plan_digest != plan['planDigest']:
        raise SyncwheelError('GitHub PR merge plan is stale; generate a new preview and use its exact planDigest')
    if plan['status'] != 'ready':
        raise SyncwheelError('GitHub PR merge STOP: plan is blocked; resolve blockers and preview again')
    shared_path = Path(plan['authorityProvenance']['shared_manifest_path'])
    with channel_mutation_lock(shared_path.parent.parent, shared_path, 'github-pr-merge-authority'):
        current_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
        if current_manifest is None:
            raise SyncwheelError('GitHub PR merge selected manifest disappeared; preview again')
        current_authority = resolve_repository_authority(repo_root, current_manifest, manifest_path)
        if current_authority != (plan['authority'], plan['authorityProvenance']):
            raise SyncwheelError('GitHub PR merge authority changed during observation; preview again')
        record_github_pr_merge_event(repo_root, manifest_path, 'github_pr_merge_started', plan, status='started')
        record_github_pr_merge_event(repo_root, manifest_path, 'github_pr_merge_prepared', plan, status='prepared')
        merge_result = github_adapter_request(repo_root, {
            'operation': 'merge',
            'repository': plan['repository'],
            'pullRequestNumber': plan['pullRequest']['number'],
            'method': plan['mergeMethod'],
            'admin': plan['path'] == 'admin-review-bypass',
            'headSha': plan['headSha'],
        })
    observed = github_adapter_request(repo_root, {
        **plan['adapterRequest'],
        'pullRequestNumber': plan['pullRequest']['number'],
    })
    status = github_pr_merge_classify(plan, observed, bool(merge_result.get('ok')))
    receipt = github_pr_merge_receipt(
        repo_root, manifest_path, plan, status, observed, merge_result,
        detail=(merge_result.get('stderr') or merge_result.get('stdout')) if status in {'failed', 'unknown'} else None,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if status.startswith('succeeded') else 2


def governed_worktree_registry_path(repo_root):
    return _governed_worktrees(repo_root).governed_worktree_registry_path()


def governed_worktree_lock_path(repo_root):
    return _governed_worktrees(repo_root).governed_worktree_lock_path()


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


def log_metadata_lock_recovery(
    log_path, label, stale_path, metadata, reason, token
):
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
            f'recovered an uninitialized {label} lock '
            f'({reason}); its creator, if still running, retries'
        )
    else:
        message = (
            f'recovered stale {label} lock from pid '
            f"{metadata.get('pid', 'unknown')} ({reason})"
        )
    print(f'WARNING: {message}; retained {stale_path.name}', file=sys.stderr)


def metadata_lock_descriptor_matches_path(lock_path, descriptor):
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
def exclusive_metadata_lock(
    lock_path,
    *,
    label,
    recovery_log_path,
    timeout_seconds,
    stale_seconds,
    busy_error,
    metadata=None,
    stale_lock_remedy=None,
):
    """Hold an fsynced, owner-describing lock, optionally recovering a dead owner."""
    if fcntl is None:
        raise SyncwheelError(
            f'crash-safe {label} locking requires POSIX file locking support'
        )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    descriptor = None
    metadata = metadata or governed_worktree_lock_metadata()
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
                if not metadata_lock_descriptor_matches_path(lock_path, candidate):
                    os.close(candidate)
                    continue
            except BaseException:
                owns_path = metadata_lock_descriptor_matches_path(lock_path, candidate)
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
                        if stale_lock_remedy:
                            raise SyncwheelError(
                                f'{label} lock needs recovery at {lock_path}; {stale_lock_remedy}'
                            )
                        stale_token = uuid.uuid4().hex
                        stale_path = lock_path.with_name(
                            f'{lock_path.name}.stale-{syncwheel_timestamp()}-{stale_token[:12]}'
                        )
                        # The flock serializes conforming contenders on this inode;
                        # a waiter that opened it before this rename verifies the
                        # path inode again and retries rather than moving a new lock.
                        os.replace(lock_path, stale_path)
                        fsync_directory_path(lock_path.parent)
                        log_metadata_lock_recovery(
                            recovery_log_path,
                            label,
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
                try:
                    with open(str(lock_path), 'rb') as handle:
                        live_metadata = parse_governed_worktree_lock_metadata(
                            handle.read(65536)
                        )
                except OSError:
                    live_metadata = {}
                raise busy_error(live_metadata, age > stale_seconds)
            time.sleep(0.05)
    try:
        yield lock_path, metadata
    finally:
        try:
            if metadata_lock_descriptor_matches_path(lock_path, descriptor):
                lock_path.unlink()
                fsync_directory_path(lock_path.parent)
        except FileNotFoundError:
            pass
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def governed_worktree_registry_lock(repo_root):
    return _governed_worktrees(repo_root).governed_worktree_registry_lock()


def prune_governed_worktree_stale_locks(repo_root, manifest_path=None):
    return _governed_worktrees(repo_root).prune_governed_worktree_stale_locks(
        manifest_path,
    )


def load_governed_worktree_registry(repo_root):
    return _governed_worktrees(repo_root).load_governed_worktree_registry()


def governed_worktree_registry_io_checkpoint(stage):
    """Fault-injection seam around the registry's durable CAS write."""
    return None


def save_governed_worktree_registry(
    repo_root,
    registry,
    expected_digest=_REGISTRY_EXPECTED_DIGEST_UNSET,
):
    return _governed_worktrees(repo_root).save_governed_worktree_registry(
        registry, expected_digest,
    )


def governed_worktree_registry_cas_persister(repo_root, registry):
    return _governed_worktrees(repo_root).governed_worktree_registry_cas_persister(
        registry,
    )


def governed_worktree_root(repo_root, manifest):
    return _governed_worktrees(repo_root).governed_worktree_root(
        manifest,
    )


def governed_worktree_lane_status(repo_root, manifest, lane, now=None, manifest_path=None):
    return _governed_worktrees(repo_root).governed_worktree_lane_status(
        manifest, lane, now, manifest_path,
    )


def governed_worktree_diagnostics(repo_root, manifest, manifest_path=None):
    return _governed_worktrees(repo_root).governed_worktree_diagnostics(
        manifest, manifest_path,
    )


def governed_worktree_warning_lines(repo_root, manifest, manifest_path=None):
    return _governed_worktrees(repo_root).governed_worktree_warning_lines(
        manifest, manifest_path,
    )


def emit_governed_worktree_warnings(repo_root, manifest, json_mode=False, manifest_path=None, *, lanes=None):
    return _governed_worktrees(repo_root).emit_governed_worktree_warnings(
        manifest, json_mode, manifest_path, lanes=lanes,
    )


GOVERNED_WORKTREE_RECOVERY_PREFIX = 'refs/syncwheel/recovery/lanes/'
GOVERNED_WORKTREE_ARCHIVE_PREFIX = 'refs/syncwheel/archive/lanes/'


def governed_worktree_archive_ref(lane):
    """The remote ref that keeps a removed lane's tip off this machine.

    It mirrors the local recovery ref name, so one removal has one name in
    both places. It lives outside refs/heads, so forges do not list it as a
    branch, but anyone who can fetch the repository can fetch it.
    """
    recovery_ref = lane['recovery_ref']
    suffix = (
        recovery_ref[len(GOVERNED_WORKTREE_RECOVERY_PREFIX):]
        if recovery_ref.startswith(GOVERNED_WORKTREE_RECOVERY_PREFIX)
        else safe_ref_segment(recovery_ref)
    )
    return f'{GOVERNED_WORKTREE_ARCHIVE_PREFIX}{suffix}'


def governed_worktree_archive_remote(repo_root, manifest):
    """The first configured of the publication and canonical remotes.

    Returns (remote, None), or (None, reason) when none of them is configured:
    'no_remotes' when the repository has no remote at all, otherwise
    'archive_remote_not_configured'.
    """
    defaults = (manifest or {}).get('defaults') or {}
    names = [
        name for name in (defaults.get('publication_remote'), defaults.get('canonical_remote'))
        if name
    ] or ['origin']
    for name in names:
        if remote_is_configured(repo_root, name):
            return name, None
    configured = git(repo_root, 'remote', check=False).stdout.split()
    return None, ('archive_remote_not_configured' if configured else 'no_remotes')


def remote_ref_tip(repo_root, remote, ref):
    result = git(
        repo_root, 'ls-remote', '--refs', remote, ref, check=False,
        env={'GIT_TERMINAL_PROMPT': '0'},
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip() or f'could not read {remote}'
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref:
            return True, parts[0]
    return True, None


def archive_governed_worktree_tip(repo_root, manifest, lane, tip):
    """Push a lane's tip to a hidden ref on the remote before its folder goes.

    Returns None when the tip is on the remote (or there is no remote to push
    to), or a failure detail; on failure the caller keeps the lane.
    """
    remote, missing = governed_worktree_archive_remote(repo_root, manifest)
    if missing == 'no_remotes':
        # Nothing exists to push to: only the local recovery ref can keep the tip.
        lane['archive_ref'] = None
        lane['archive_skipped'] = 'no_remotes'
        return None
    if missing:
        return {
            'code': 'archive_push_failed',
            'remedy': (
                'neither the publication nor the canonical remote of the manifest is '
                'configured in this clone, so the lane tip cannot be saved; the lane was '
                'kept. Add the publication remote, then retry the same command'
            ),
        }
    if (
        lane.get('archive_ref')
        and lane.get('archive_remote') == remote
        and lane.get('archive_confirmed_tip') == tip
    ):
        # Already confirmed on the remote by an earlier attempt: no network.
        return None
    archive_ref = lane.get('archive_ref') or governed_worktree_archive_ref(lane)
    lane['archive_ref'] = archive_ref
    lane['archive_remote'] = remote
    lane.pop('archive_skipped', None)

    def failure(detail, remedy='Fix access to the remote, then retry the same command'):
        return {
            'code': 'archive_push_failed',
            'remedy': f'could not save {tip} to {remote} {archive_ref} ({detail}); the lane was kept. {remedy}',
        }

    readable, current = remote_ref_tip(repo_root, remote, archive_ref)
    if not readable:
        return failure(current)
    if current == tip:
        lane['archive_confirmed_tip'] = tip
        return None
    if current is not None:
        return failure(
            f'the remote ref already points at {current}',
            'Inspect that remote ref; Syncwheel never overwrites it',
        )
    try:
        result = run_authorized_push(
            repo_root,
            ['git', 'push', '--porcelain', remote, f'{tip}:{archive_ref}'],
            remote,
            [archive_ref],
            check=False,
            manifest=manifest,
        )
    except SyncwheelError as exc:
        return failure(str(exc))
    if result.returncode != 0:
        return failure(result.stderr.strip() or result.stdout.strip() or 'push failed')
    readable, current = remote_ref_tip(repo_root, remote, archive_ref)
    if not readable or current != tip:
        return failure('the remote did not confirm the pushed tip')
    lane['archive_confirmed_tip'] = tip
    return None


def governed_worktree_cleanup_tip(repo_root, lane):
    return _governed_worktrees(repo_root).governed_worktree_cleanup_tip(
        lane,
    )


def governed_worktree_cleanup_checkpoint(stage):
    """Fault-injection seam at process-death boundaries in lane cleanup."""
    return None


def append_governed_worktree_cleanup_intent(repo_root, lane, manifest_path=None):
    return _governed_worktrees(repo_root).append_governed_worktree_cleanup_intent(
        lane, manifest_path,
    )


def governed_worktree_cleanup_ledger(repo_root, manifest_path=None):
    return _governed_worktrees(repo_root).governed_worktree_cleanup_ledger(
        manifest_path,
    )


def recover_governed_worktree_registry_from_ledger(
    repo_root,
    registry,
    persist,
    manifest_path=None,
):
    return _governed_worktrees(repo_root).recover_governed_worktree_registry_from_ledger(
        registry, persist, manifest_path,
    )


def governed_worktree_release_terminal(repo_root, lane_id, manifest_path=None):
    return _governed_worktrees(repo_root).governed_worktree_release_terminal(
        lane_id, manifest_path,
    )


def append_governed_worktree_release_note(repo_root, lane_id, reason, terminal, manifest_path=None):
    return _governed_worktrees(repo_root).append_governed_worktree_release_note(
        lane_id, reason, terminal, manifest_path,
    )


def ensure_governed_worktree_recovery_ref(repo_root, recovery_ref, tip):
    return _governed_worktrees(repo_root).ensure_governed_worktree_recovery_ref(
        recovery_ref, tip,
    )


def verify_governed_worktree_recovery_ref(repo_root, recovery_ref, tip):
    return _governed_worktrees(repo_root).verify_governed_worktree_recovery_ref(
        recovery_ref, tip,
    )


def governed_worktree_record_for_path(repo_root, path):
    return _governed_worktrees(repo_root).governed_worktree_record_for_path(
        path,
    )


def governed_worktree_admin_dir_for_path(repo_root, path):
    return _governed_worktrees(repo_root).governed_worktree_admin_dir_for_path(
        path,
    )


def governed_worktree_registration_matches(repo_root, lane, path, admin_dir, lock_reason):
    return _governed_worktrees(repo_root).governed_worktree_registration_matches(
        lane, path, admin_dir, lock_reason,
    )


def lock_governed_worktree_for_cleanup(repo_root, lane):
    return _governed_worktrees(repo_root).lock_governed_worktree_for_cleanup(
        lane,
    )


def unlock_governed_worktree_cleanup(repo_root, lock):
    return _governed_worktrees(repo_root).unlock_governed_worktree_cleanup(
        lock,
    )


def governed_worktree_final_probe(path, anchored_tip, missing_at_classification):
    return _governed_worktrees(path).governed_worktree_final_probe(
        path, anchored_tip, missing_at_classification,
    )


def delete_governed_worktree_branch_with_anchor(repo_root, lane, tip):
    return _governed_worktrees(repo_root).delete_governed_worktree_branch_with_anchor(
        lane, tip,
    )


def governed_worktree_pending_cleanup_candidates(repo_root, manifest, registry=None):
    """Cleanup candidates whose removal an explicit operation already started.

    These are captured lanes and reaped lanes whose terminal ledger event is
    still missing. Active lanes are never included, whatever their lease.
    """
    return [
        lane for lane in governed_worktree_cleanup_candidates(repo_root, manifest, registry)
        if lane['state'] != 'active'
    ]


def governed_worktree_cleanup_candidates(repo_root, manifest, registry=None):
    return _governed_worktrees(repo_root).governed_worktree_cleanup_candidates(
        manifest, registry,
    )


def reap_governed_worktree_lane(
    repo_root,
    manifest,
    lane,
    persist=None,
    manifest_path=None,
    event_type='governed_worktree_reaped',
    event_reason='expired',
):
    return _governed_worktrees(repo_root).reap_governed_worktree_lane(
        manifest, lane, persist, manifest_path, event_type, event_reason,
    )


def append_governed_worktree_cleanup_event(repo_root, lane, manifest_path=None):
    return _governed_worktrees(repo_root).append_governed_worktree_cleanup_event(
        lane, manifest_path,
    )


def reconcile_governed_worktrees(
    repo_root, manifest, manifest_path=None, candidate_ids=None, repair_all_paths=False,
    *, protect_foreign_lanes=False,
):
    return _governed_worktrees(repo_root).reconcile_governed_worktrees(
        manifest, manifest_path, candidate_ids, repair_all_paths,
        protect_foreign_lanes=protect_foreign_lanes,
    )


def coordination_profile(repo_root):
    return _publication_coordinator(repo_root).coordination_profile()


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


def public_coordination_remote_ref(role, branch, path):
    return _publication_coordinator(None).public_coordination_remote_ref(
        role, branch, path,
    )


def public_coordination_ref(value, remote_roles, repo_root=None, path='manifest ref'):
    return _publication_coordinator(repo_root).public_coordination_ref(
        value, remote_roles, repo_root, path,
    )


def is_valid_coordination_branch_ref(value):
    return (
        isinstance(value, str)
        and value.startswith('refs/heads/')
        and run(['git', 'check-ref-format', value], check=False).returncode == 0
    )


def coordination_public_remote_ref_parts(value, path):
    return _coordination.coordination_public_remote_ref_parts(
        value, path, branch_is_valid=is_valid_coordination_branch_ref,
    )


def local_coordination_ref(value, defaults):
    return _coordination.local_coordination_ref(
        value, defaults, branch_is_valid=is_valid_coordination_branch_ref,
    )


def coordination_manifest_snapshot(manifest, repo_root=None, coordination_state=None):
    return _publication_coordinator(repo_root).coordination_manifest_snapshot(
        manifest, repo_root, coordination_state,
    )


def coordination_manifest_digest(manifest, repo_root=None):
    return _publication_coordinator(repo_root).coordination_manifest_digest(
        manifest, repo_root,
    )


def coordination_state_control_manifest(repo_root, state, remote=None):
    return _coordination_state_reader(repo_root).coordination_state_control_manifest(
        state, remote,
    )


def coordination_state_manifest_digest(repo_root, state, remote=None):
    return _coordination_state_reader(repo_root).coordination_state_manifest_digest(
        state, remote,
    )


def coordination_state_legacy_manifest_digest(repo_root, control_manifest, state):
    return _coordination_state_reader(repo_root).coordination_state_legacy_manifest_digest(
        control_manifest, state,
    )


def coordination_state_manifest_digest_classification(repo_root, state, remote=None):
    return _coordination_state_reader(repo_root).coordination_state_manifest_digest_classification(
        state, remote,
    )


def classify_coordination_state_manifest_digest(repo_root, state, remote=None):
    return _coordination_state_reader(repo_root).classify_coordination_state_manifest_digest(
        state, remote,
    )


def coordination_state_manifest_digest_form(repo_root, state, remote=None):
    return _coordination_state_reader(repo_root).coordination_state_manifest_digest_form(
        state, remote,
    )


def verify_coordination_state_manifest_digest(repo_root, state, remote=None):
    return _coordination_state_reader(repo_root).verify_coordination_state_manifest_digest(
        state, remote,
    )


def record_coordination_state_digest_migration(
    repo_root, manifest_path, config, digest_form, parent_state, state_tip, digest,
):
    return _publication_coordinator(repo_root).record_coordination_state_digest_migration(
        manifest_path, config, digest_form, parent_state, state_tip, digest,
    )


def record_coordination_state_digest_heal(repo_root, manifest_path, config, plan, state_tip):
    return append_ledger_event(
        repo_root,
        'coordination_state_digest_healed',
        {
            'coordination_id': config['id'],
            'parent_state': plan['expectedStateTip'],
            'state_tip': state_tip,
            'repair_class': COORDINATION_REPAIR_DIGEST_HEAL_CLASS,
            'from_digest': plan['recordedManifestDigest'],
            'to_digest': plan['expectedManifestDigest'],
        },
        manifest_path,
    )


def coordination_operation_manifest_digest(manifest, repo_root=None):
    return _publication_coordinator(repo_root).coordination_operation_manifest_digest(
        manifest, repo_root,
    )


def remote_ref_tips(repo_root, remote, refs):
    return _coordination_state_reader(repo_root).remote_ref_tips(
        remote, refs,
    )


def validate_coordination_snapshot_refs(snapshot):
    return _coordination.validate_coordination_snapshot_refs(
        snapshot, branch_is_valid=is_valid_coordination_branch_ref,
    )


def validate_coordination_state(state, expected_id=None, claims_mode='advisory'):
    return _coordination.validate_coordination_state(
        state, expected_id, claims_mode, branch_is_valid=is_valid_coordination_branch_ref,
    )


def coordination_state_from_commit(
    repo_root, commit, expected_id=None, claims_mode='advisory'
):
    return _coordination_state_reader(repo_root).coordination_state_from_commit(
        commit, expected_id, claims_mode,
    )


def read_remote_coordination_state(repo_root, config, fetch=True, local_manifest_version=None):
    return _coordination_state_reader(repo_root).read_remote_coordination_state(
        config, fetch, local_manifest_version,
    )


def read_remote_coordination_states(repo_root, config, *, fetch=True):
    return _coordination_state_reader(repo_root).read_remote_coordination_states(
        config, fetch=fetch,
    )


def coordination_ownership_conflicts(repo_root, config, managed_refs, states=None):
    return _coordination_state_reader(repo_root).coordination_ownership_conflicts(
        config, managed_refs, states,
    )


def require_exclusive_coordination_ownership(
    repo_root,
    config,
    managed_refs,
    expected_state_refs=None,
    *, fetch=True,
):
    return _coordination_state_reader(repo_root).require_exclusive_coordination_ownership(
        config, managed_refs, expected_state_refs, fetch=fetch,
    )


def coordination_state_managed_manifest_digest(repo_root, manifest, managed, integration_ref):
    return _publication_coordinator(repo_root).coordination_state_managed_manifest_digest(
        manifest, managed, integration_ref,
    )


def build_coordination_state(
    repo_root, manifest, config, previous, observed_refs, changed_refs, scope,
    projection_status, installation, tombstone=None, claim_commits=None,
    operation_token=None, refresh_manifest_digest=False,
):
    return _publication_coordinator(repo_root).build_coordination_state(
        manifest, config, previous, observed_refs, changed_refs, scope, projection_status, installation, tombstone, claim_commits, operation_token, refresh_manifest_digest,
    )


def build_coordination_repair_state(
    previous_state, previous_tip, repaired_ref, repaired_tip, installation,
    *, operation_token=None, claim_commit=None, repair_evidence=None,
):
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
    if operation_token is not None:
        child['operation_token'] = operation_token
    if claim_commit is not None:
        child.setdefault('claims', {})[repaired_ref] = claim_commit
        child['claims'] = dict(sorted(child['claims'].items()))
    if repair_evidence is not None:
        child['repair_evidence'] = repair_evidence
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


def build_digest_migration_coordination_repair_state(
    previous_state, previous_tip, plan, installation
):
    """Republish an unchanged state under the control-manifest digest form.

    This is the one repair that rewrites manifest_digest: the parent recorded the
    pre-0.42.2 normalized-snapshot digest, which no publication guard can satisfy.
    """
    child = build_coordination_repair_state(
        previous_state,
        previous_tip,
        plan['repairedRef'],
        plan['expectedRemoteTip'],
        installation,
    )
    child['changed_refs'] = {}
    child['publication_scope'] = f"repair-digest:{plan['repairedRef']}"
    child['manifest_digest'] = plan['expectedManifestDigest']
    child['repair_evidence'] = {
        'schemaVersion': 1,
        'planDigest': plan['planDigest'],
        'proof': COORDINATION_REPAIR_DIGEST_MIGRATION_PROOF,
        'ref': plan['repairedRef'],
        'recordedTip': plan['expectedRecordedTip'],
        'observedTip': plan['expectedRemoteTip'],
        'recordedManifestDigest': plan['recordedManifestDigest'],
        'manifestDigest': plan['expectedManifestDigest'],
    }
    return validate_coordination_state(child, previous_state['coordination_id'])


def build_digest_heal_coordination_repair_state(
    previous_state, previous_tip, plan, installation
):
    """Recompute an orphaned manifest_digest from the manifest already committed at the tip.

    Unlike the legacy-digest migration, the old recorded value is not required
    to be any recognized prior form: any value is accepted as stale, because
    the replacement is a deterministic function of content already committed
    and verified at the registered integration tip, and needs no further
    proof than reading that tree.
    """
    child = build_coordination_repair_state(
        previous_state,
        previous_tip,
        plan['repairedRef'],
        plan['expectedRemoteTip'],
        installation,
    )
    child['changed_refs'] = {}
    child['publication_scope'] = f"repair-digest-heal:{plan['repairedRef']}"
    child['manifest_digest'] = plan['expectedManifestDigest']
    child['repair_evidence'] = {
        'schemaVersion': 1,
        'planDigest': plan['planDigest'],
        'proof': COORDINATION_REPAIR_DIGEST_HEAL_PROOF,
        'ref': plan['repairedRef'],
        'recordedTip': plan['expectedRecordedTip'],
        'observedTip': plan['expectedRemoteTip'],
        'recordedManifestDigest': plan['recordedManifestDigest'],
        'manifestDigest': plan['expectedManifestDigest'],
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
        # Partial publications record declared but unpublished peers as null.
        # Only the selected repair target must already have an exact tip.
        if owned_tip is None and owned_ref != repaired_ref:
            continue
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
    drifted_peers = {
        ref: (tip, observed_refs[ref])
        for ref, tip in previous['state']['managed_refs'].items()
        if ref != repaired_ref and observed_refs[ref] != tip
    }
    if drifted_peers:
        raise SyncwheelError(
            'coordination repair STOP: non-target managed refs differ from recorded state: '
            + ', '.join(sorted(drifted_peers))
        )
    expected_recorded = previous['state']['managed_refs'][repaired_ref]
    observed = observed_refs[repaired_ref]
    missing_ref_restore_required = observed is None
    active_refs = coordination_snapshot_managed_ref_names(previous['state']['manifest'])
    if missing_ref_restore_required and repaired_ref not in active_refs:
        raise SyncwheelError(
            'coordination repair refuses to recreate an inactive or tombstoned managed ref'
        )
    if (
        missing_ref_restore_required
        and freeze_backend != COORDINATION_REPAIR_MISSING_REF_BACKEND
    ):
        raise SyncwheelError(
            f'coordination repair managed ref is absent: {repaired_ref}; '
            f'use --freeze-backend {COORDINATION_REPAIR_MISSING_REF_BACKEND}'
        )
    if missing_ref_restore_required and not commit_exists(repo_root, expected_recorded):
        raise SyncwheelError(
            'coordination repair missing-ref proof requires the recorded commit object'
        )
    ref_repair_required = observed is not None and expected_recorded != observed
    digest_classification = coordination_state_manifest_digest_classification(
        repo_root, previous['state'], config['remote']
    )
    digest_migration_required = (
        not ref_repair_required
        and digest_classification['form'] == COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT
    )
    digest_heal_required = (
        not ref_repair_required
        and digest_classification['form'] == COORDINATION_STATE_DIGEST_FORM_ORPHANED
    )
    if missing_ref_restore_required:
        status = 'missing-ref-restore-required'
    elif ref_repair_required:
        status = 'repair-required'
    elif digest_migration_required:
        status = 'digest-migration-required'
    elif digest_heal_required:
        status = 'digest-heal-required'
    else:
        status = 'noop'
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
    if digest_migration_required:
        payload.update({
            'repairClass': COORDINATION_REPAIR_DIGEST_MIGRATION_CLASS,
            'stateDigestForm': digest_classification['form'],
            'recordedManifestDigest': digest_classification['recorded_digest'],
            'expectedManifestDigest': digest_classification['control_manifest_digest'],
            'proof': COORDINATION_REPAIR_DIGEST_MIGRATION_PROOF,
            'precondition': COORDINATION_REPAIR_DIGEST_MIGRATION_PRECONDITION,
        })
    if digest_heal_required:
        payload.update({
            'repairClass': COORDINATION_REPAIR_DIGEST_HEAL_CLASS,
            'stateDigestForm': digest_classification['form'],
            'recordedManifestDigest': digest_classification['recorded_digest'],
            'expectedManifestDigest': digest_classification['control_manifest_digest'],
            'proof': COORDINATION_REPAIR_DIGEST_HEAL_PROOF,
            'precondition': COORDINATION_REPAIR_DIGEST_HEAL_PRECONDITION,
        })
    if missing_ref_restore_required:
        claim_ref = coordination_claim_ref(repaired_ref)
        claim_tip = remote_ref_tips(
            repo_root, config['remote'], [claim_ref]
        )[claim_ref]
        if claim_tip:
            claim = fetch_coordination_claim(
                repo_root, config['remote'], claim_ref, claim_tip
            )
            if (
                claim.get('coordination_id') != config['id']
                or claim.get('source_ref') != repaired_ref
            ):
                raise SyncwheelError(
                    'coordination repair missing-ref claim belongs to another owner'
                )
        payload.update({
            'repairClass': 'missing-managed-ref-create',
            'proof': COORDINATION_REPAIR_MISSING_REF_PROOF,
            'precondition': 'exact-missing-ref-and-atomic-create-state-cas',
            'expectedClaimRef': claim_ref,
            'expectedClaimTip': claim_tip,
        })
    if freeze_backend == COORDINATION_REPAIR_TREE_EQUIVALENT_BACKEND and ref_repair_required:
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
    if freeze_backend == COORDINATION_REPAIR_FAST_FORWARD_BACKEND and ref_repair_required:
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
            manifest=kwargs['manifest'],
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


class MissingRefCreateCasCoordinationRepairBackend(CoordinationRepairBackend):
    """Atomically recreate one absent owned ref and append its repair state."""

    name = COORDINATION_REPAIR_MISSING_REF_BACKEND
    proof = COORDINATION_REPAIR_MISSING_REF_PROOF

    def _verify_observations(
        self, repo_root, coordination, remote, state_ref, expected_state_tip,
        guarded_refs, claim_ref=None, expected_claim_tip=None,
    ):
        require_exclusive_coordination_ownership(repo_root, coordination, guarded_refs)
        refs = [state_ref, *guarded_refs]
        if claim_ref:
            refs.append(claim_ref)
        observed = remote_ref_tips(repo_root, remote, refs)
        if observed.get(state_ref) != expected_state_tip:
            raise SyncwheelError(
                'coordination repair STOP: state lease was lost before missing-ref CAS'
            )
        drifted = {
            ref: (tip, observed.get(ref)) for ref, tip in guarded_refs.items()
            if observed.get(ref) != tip
        }
        if drifted:
            raise SyncwheelError(
                'coordination repair STOP: guarded refs drifted before missing-ref CAS'
            )
        if claim_ref and observed.get(claim_ref) != expected_claim_tip:
            raise SyncwheelError(
                'coordination repair STOP: claim lease was lost before missing-ref CAS'
            )
        return observed

    def preflight(self, **kwargs):
        self._verify_observations(
            kwargs['repo_root'],
            kwargs['coordination'],
            kwargs['remote'],
            kwargs['state_ref'],
            kwargs['expected_state_tip'],
            kwargs['guarded_refs'],
            kwargs.get('claim_ref'),
            kwargs.get('expected_claim_tip'),
        )
        if kwargs['guarded_refs'].get(kwargs['repaired_ref']) is not None:
            raise SyncwheelError('coordination repair missing-ref target is no longer absent')
        return {'proof': self.proof}

    def apply(self, **kwargs):
        self.preflight(**kwargs)
        repaired_ref = kwargs['repaired_ref']
        repaired_tip = kwargs['repaired_tip']
        claim_ref = kwargs['claim_ref']
        new_claim_tip = kwargs['new_claim_tip']
        command = [
            'git',
            'push',
            '--atomic',
            f'--force-with-lease={repaired_ref}:',
            f"--force-with-lease={claim_ref}:{kwargs['expected_claim_tip'] or ''}",
            f"--force-with-lease={kwargs['state_ref']}:{kwargs['expected_state_tip']}",
            kwargs['remote'],
            f'{repaired_tip}:{repaired_ref}',
            f'{new_claim_tip}:{claim_ref}',
            f"{kwargs['new_state_tip']}:{kwargs['state_ref']}",
        ]
        result = run_authorized_push(
            kwargs['repo_root'],
            command,
            kwargs['remote'],
            [repaired_ref, claim_ref, kwargs['state_ref']],
            check=False,
            manifest=kwargs['manifest'],
        )
        if result.returncode != 0:
            observed = remote_ref_tips(
                kwargs['repo_root'],
                kwargs['remote'],
                [repaired_ref, claim_ref, kwargs['state_ref']],
            )
            if (
                observed.get(repaired_ref) is None
                and observed.get(claim_ref) == kwargs['expected_claim_tip']
                and observed.get(kwargs['state_ref']) == kwargs['expected_state_tip']
            ):
                raise SyncwheelError(
                    'coordination repair missing-ref CAS was rejected without mutation'
                )
            if (
                observed.get(repaired_ref) != repaired_tip
                or observed.get(claim_ref) != new_claim_tip
                or observed.get(kwargs['state_ref']) != kwargs['new_state_tip']
            ):
                raise SyncwheelError(
                    'coordination repair outcome is unknown after missing-ref CAS rejection'
                )
        return {'proof': self.proof, 'atomicCreate': True}

    def postflight(self, **kwargs):
        self._verify_observations(
            kwargs['repo_root'],
            kwargs['coordination'],
            kwargs['remote'],
            kwargs['state_ref'],
            kwargs['expected_state_tip'],
            kwargs['guarded_refs'],
            kwargs.get('claim_ref'),
            kwargs.get('expected_claim_tip'),
        )
        return {'proof': self.proof}


class DigestMigrationStateCasCoordinationRepairBackend(
    TreeEquivalentStateCasCoordinationRepairBackend
):
    """Rewrite only the recorded control digest of an otherwise unchanged state."""

    name = COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND
    proof = COORDINATION_REPAIR_DIGEST_MIGRATION_PROOF


class DigestHealStateCasCoordinationRepairBackend(
    TreeEquivalentStateCasCoordinationRepairBackend
):
    """Rewrite only an orphaned recorded control digest of an otherwise unchanged state."""

    name = COORDINATION_REPAIR_DIGEST_HEAL_BACKEND
    proof = COORDINATION_REPAIR_DIGEST_HEAL_PROOF


def coordination_missing_ref_repair_identity(previous_state, plan):
    identity = {
        'coordination_id': previous_state['coordination_id'],
        'scope': f"repair:{plan['repairedRef']}",
        'projection_status': previous_state.get('projection_status'),
        'manifest_digest': coordination_state_operation_manifest_digest(previous_state),
        'changed_refs': {plan['repairedRef']: plan['expectedRecordedTip']},
        'tombstone': None,
        'rename': None,
        'state_transition': None,
        'repair_plan_digest': plan['planDigest'],
    }
    return identity, canonical_json_digest(identity)


def begin_coordination_missing_ref_repair(
    repo_root, manifest, manifest_path, previous, plan,
):
    identity, fingerprint = coordination_missing_ref_repair_identity(
        previous['state'], plan
    )
    with coordination_publication_lock(repo_root):
        existing = pending_coordination_publication_after_resolution(
            repo_root, manifest, manifest_path, fingerprint
        )
        if existing:
            return {**existing, 'retry': True}
        observed = read_remote_coordination_state(
            repo_root,
            coordination_config(manifest),
            fetch=True,
            local_manifest_version=manifest['version'],
        )
        if observed['tip'] != plan['expectedStateTip']:
            raise SyncwheelError(
                'coordination repair state changed before its intent could be recorded'
            )
        payload = {
            **identity,
            'fingerprint': fingerprint,
            'operation_token': str(uuid.uuid4()),
            'expected_coordination_state_tip': observed['tip'],
            'owner': coordination_publication_owner(repo_root),
        }
        append_ledger_event(
            repo_root, 'coordination_publish_intent', payload, manifest_path
        )
        return {**payload, 'retry': False}


def recover_landed_coordination_missing_ref_repair(
    repo_root, manifest, manifest_path, plan,
):
    config = coordination_config(manifest)
    matching = [
        intent for intent in pending_coordination_publications(repo_root, manifest_path)
        if intent.get('repair_plan_digest') == plan.get('planDigest')
    ]
    if not matching:
        return None
    if len(matching) != 1:
        raise SyncwheelError(
            'multiple coordination repair intents match the reviewed plan'
        )
    operation = matching[0]
    observed = read_remote_coordination_state(
        repo_root, config, fetch=True, local_manifest_version=manifest['version']
    )
    if not coordinated_operation_landed(
        repo_root, config, observed, operation, claims_fallback=True
    ):
        return None
    complete_coordination_publication(
        repo_root,
        manifest_path,
        operation,
        {'state_tip': observed['tip'], 'status': 'recovered', 'recovered': True},
    )
    return {
        'status': 'repaired',
        'state_tip': observed['tip'],
        'parent_state': plan['expectedStateTip'],
        'plan_digest': plan['planDigest'],
        'backend': COORDINATION_REPAIR_MISSING_REF_BACKEND,
        'backend_result': {'proof': plan.get('proof'), 'recovered': True},
        'proof': plan.get('proof'),
        'recovered': True,
    }


def apply_coordination_repair_plan(repo_root, manifest, plan, backend=None, manifest_path=None):
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
        elif plan.get('freezeBackend') == COORDINATION_REPAIR_MISSING_REF_BACKEND:
            backend = MissingRefCreateCasCoordinationRepairBackend()
        elif plan.get('freezeBackend') == COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND:
            backend = DigestMigrationStateCasCoordinationRepairBackend()
        elif plan.get('freezeBackend') == COORDINATION_REPAIR_DIGEST_HEAL_BACKEND:
            backend = DigestHealStateCasCoordinationRepairBackend()
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
    missing_ref_repair = backend.name == COORDINATION_REPAIR_MISSING_REF_BACKEND
    digest_migration_repair = backend.name == COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND
    digest_heal_repair = backend.name == COORDINATION_REPAIR_DIGEST_HEAL_BACKEND
    state_only_repair = (
        tree_equivalent_repair or fast_forward_repair or digest_migration_repair
        or digest_heal_repair
    )
    if plan.get('status') == 'missing-ref-restore-required' and not missing_ref_repair:
        raise SyncwheelError(
            'coordination repair missing-ref restore requires the '
            f'{COORDINATION_REPAIR_MISSING_REF_BACKEND} backend'
        )
    if missing_ref_repair:
        required_proof = {
            'repairClass', 'proof', 'expectedClaimRef', 'expectedClaimTip',
        }
        missing_proof = sorted(required_proof - set(plan))
        if missing_proof:
            raise SyncwheelError(
                'coordination repair missing-ref plan is missing: '
                + ', '.join(missing_proof)
            )
        if (
            plan.get('status') != 'missing-ref-restore-required'
            or plan.get('repairClass') != 'missing-managed-ref-create'
            or plan.get('proof') != COORDINATION_REPAIR_MISSING_REF_PROOF
            or plan.get('precondition')
            != 'exact-missing-ref-and-atomic-create-state-cas'
            or plan.get('expectedRemoteTip') is not None
            or not isinstance(plan.get('expectedRecordedTip'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', plan['expectedRecordedTip'])
            or plan.get('guardedRefs', {}).get(plan.get('repairedRef')) is not None
            or plan.get('expectedClaimRef')
            != coordination_claim_ref(plan.get('repairedRef'))
            or (
                plan.get('expectedClaimTip') is not None
                and not re.fullmatch(r'[0-9a-f]{40}', plan['expectedClaimTip'])
            )
        ):
            raise SyncwheelError('coordination repair missing-ref proof is invalid')
    if plan.get('status') == 'digest-migration-required' and not digest_migration_repair:
        raise SyncwheelError(
            'coordination repair digest migration requires the '
            f'{COORDINATION_REPAIR_DIGEST_MIGRATION_BACKEND} backend'
        )
    if plan.get('status') == 'digest-heal-required' and not digest_heal_repair:
        raise SyncwheelError(
            'coordination repair digest heal requires the '
            f'{COORDINATION_REPAIR_DIGEST_HEAL_BACKEND} backend'
        )
    if digest_migration_repair:
        required_proof = {
            'repairClass', 'stateDigestForm', 'recordedManifestDigest',
            'expectedManifestDigest', 'proof',
        }
        missing_proof = sorted(required_proof - set(plan))
        if missing_proof:
            raise SyncwheelError(
                'coordination repair digest-migration plan is missing: '
                + ', '.join(missing_proof)
            )
        if (
            plan.get('status') != 'digest-migration-required'
            or plan.get('repairClass') != COORDINATION_REPAIR_DIGEST_MIGRATION_CLASS
            or plan.get('stateDigestForm')
            != COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT
            or plan.get('proof') != COORDINATION_REPAIR_DIGEST_MIGRATION_PROOF
            or plan.get('precondition')
            != COORDINATION_REPAIR_DIGEST_MIGRATION_PRECONDITION
            or plan.get('expectedRecordedTip') != plan.get('expectedRemoteTip')
            or not isinstance(plan.get('expectedManifestDigest'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', plan['expectedManifestDigest'])
            or plan.get('recordedManifestDigest') == plan['expectedManifestDigest']
        ):
            raise SyncwheelError('coordination repair digest-migration proof is invalid')
    if digest_heal_repair:
        required_proof = {
            'repairClass', 'stateDigestForm', 'recordedManifestDigest',
            'expectedManifestDigest', 'proof',
        }
        missing_proof = sorted(required_proof - set(plan))
        if missing_proof:
            raise SyncwheelError(
                'coordination repair digest-heal plan is missing: '
                + ', '.join(missing_proof)
            )
        if (
            plan.get('status') != 'digest-heal-required'
            or plan.get('repairClass') != COORDINATION_REPAIR_DIGEST_HEAL_CLASS
            or plan.get('stateDigestForm') != COORDINATION_STATE_DIGEST_FORM_ORPHANED
            or plan.get('proof') != COORDINATION_REPAIR_DIGEST_HEAL_PROOF
            or plan.get('precondition') != COORDINATION_REPAIR_DIGEST_HEAL_PRECONDITION
            or plan.get('expectedRecordedTip') != plan.get('expectedRemoteTip')
            or not isinstance(plan.get('expectedManifestDigest'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', plan['expectedManifestDigest'])
            or plan.get('recordedManifestDigest') == plan['expectedManifestDigest']
        ):
            raise SyncwheelError('coordination repair digest-heal proof is invalid')
    if tree_equivalent_repair and plan.get('status') == 'repair-required':
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
    if fast_forward_repair and plan.get('status') == 'repair-required':
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
    resolved_manifest_path = (
        Path(manifest_path)
        if manifest_path else repo_root / '.syncwheel' / 'manifest.json'
    )
    if missing_ref_repair:
        recovered = recover_landed_coordination_missing_ref_repair(
            repo_root, manifest, resolved_manifest_path, plan
        )
        if recovered is not None:
            return recovered
    current_plan, previous = coordination_repair_plan(
        repo_root, manifest, plan['repairedRef'], plan['freezeBackend']
    )
    comparison_keys = [
        'stateRef', 'expectedStateTip', 'expectedRecordedTip', 'expectedRemoteTip',
        'guardedRefs', 'localManifestDigest', 'precondition',
    ]
    if digest_migration_repair:
        comparison_keys.extend([
            'status', 'repairClass', 'stateDigestForm', 'recordedManifestDigest',
            'expectedManifestDigest', 'proof',
        ])
    if missing_ref_repair:
        comparison_keys.extend([
            'status', 'repairClass', 'proof', 'expectedClaimRef',
            'expectedClaimTip',
        ])
    if digest_heal_repair:
        comparison_keys.extend([
            'status', 'repairClass', 'stateDigestForm', 'recordedManifestDigest',
            'expectedManifestDigest', 'proof',
        ])
    if tree_equivalent_repair and plan.get('status') == 'repair-required':
        comparison_keys.extend([
            'repairClass', 'expectedRecordedTree', 'expectedRemoteTree', 'proof',
        ])
    if fast_forward_repair and plan.get('status') == 'repair-required':
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
        repaired_ref=plan['repairedRef'],
        repaired_tip=plan['expectedRecordedTip'],
        claim_ref=plan.get('expectedClaimRef'),
        expected_claim_tip=plan.get('expectedClaimTip'),
    )
    installation = installation_id(create=True)
    operation = None
    if missing_ref_repair:
        operation = begin_coordination_missing_ref_repair(
            repo_root, manifest, resolved_manifest_path, previous, plan
        )
        claim_commit = create_coordination_claim_commit(
            repo_root,
            plan['repairedRef'],
            config['id'],
            operation['operation_token'],
            plan['expectedClaimTip'],
            publication_scope=operation['scope'],
            changed_refs=operation['changed_refs'],
        )
        child = build_coordination_repair_state(
            previous['state'],
            previous['tip'],
            plan['repairedRef'],
            plan['expectedRecordedTip'],
            installation,
            operation_token=operation['operation_token'],
            claim_commit=claim_commit,
            repair_evidence={
                'schemaVersion': 1,
                'planDigest': supplied_digest,
                'proof': plan['proof'],
                'ref': plan['repairedRef'],
                'recordedTip': plan['expectedRecordedTip'],
                'claimRef': plan['expectedClaimRef'],
                'claimCommit': claim_commit,
            },
        )
    elif tree_equivalent_repair:
        child = build_tree_equivalent_coordination_repair_state(
            previous['state'], previous['tip'], plan, installation
        )
    elif fast_forward_repair:
        child = build_fast_forward_coordination_repair_state(
            previous['state'], previous['tip'], plan, installation
        )
    elif digest_migration_repair:
        child = build_digest_migration_coordination_repair_state(
            previous['state'], previous['tip'], plan, installation
        )
    elif digest_heal_repair:
        child = build_digest_heal_coordination_repair_state(
            previous['state'], previous['tip'], plan, installation
        )
    else:
        child = build_coordination_repair_state(
            previous['state'], previous['tip'], plan['repairedRef'], plan['expectedRemoteTip'], installation
        )
    child_tip = create_coordination_state_commit(repo_root, child, previous['tip'])
    result = backend.apply(
        repo_root=repo_root,
        manifest=manifest,
        coordination=config,
        remote=config['remote'],
        state_ref=plan['stateRef'],
        expected_state_tip=plan['expectedStateTip'],
        new_state_tip=child_tip,
        guarded_refs=plan['guardedRefs'],
        repaired_ref=plan['repairedRef'],
        repaired_tip=plan['expectedRecordedTip'],
        claim_ref=plan.get('expectedClaimRef'),
        expected_claim_tip=plan.get('expectedClaimTip'),
        new_claim_tip=(child.get('claims') or {}).get(plan['repairedRef']),
    )
    expected_post_refs = dict(plan['guardedRefs'])
    if missing_ref_repair:
        expected_post_refs[plan['repairedRef']] = plan['expectedRecordedTip']
    observed = backend.observe(
        repo_root, config['remote'], [plan['stateRef'], *expected_post_refs]
    )
    if observed.get(plan['stateRef']) != child_tip:
        raise SyncwheelError('coordination repair outcome is unknown: state CAS was not observed')
    drifted = {
        ref: (tip, observed.get(ref)) for ref, tip in expected_post_refs.items()
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
        guarded_refs=expected_post_refs,
        repaired_ref=plan['repairedRef'],
        repaired_tip=plan['expectedRecordedTip'],
        claim_ref=plan.get('expectedClaimRef'),
        expected_claim_tip=(child.get('claims') or {}).get(plan['repairedRef']),
    )
    verified = coordination_state_from_commit(repo_root, child_tip, config['id'])
    git_parent = git(repo_root, 'rev-parse', f'{child_tip}^').stdout.strip()
    if (
        git_parent != previous['tip']
        or verified['parent_state'] != previous['tip']
        or verified['managed_refs'][plan['repairedRef']] != (
            plan['expectedRecordedTip'] if missing_ref_repair
            else plan['expectedRemoteTip']
        )
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
        if digest_migration_repair and (
            verified.get('manifest_digest') != plan['expectedManifestDigest']
            or verified.get('manifest') != previous['state']['manifest']
            or evidence.get('recordedManifestDigest') != plan['recordedManifestDigest']
            or evidence.get('manifestDigest') != plan['expectedManifestDigest']
        ):
            raise SyncwheelError(
                'coordination repair post-verification failed: invalid digest-migration evidence'
            )
        if digest_heal_repair and (
            verified.get('manifest_digest') != plan['expectedManifestDigest']
            or verified.get('manifest') != previous['state']['manifest']
            or evidence.get('recordedManifestDigest') != plan['recordedManifestDigest']
            or evidence.get('manifestDigest') != plan['expectedManifestDigest']
        ):
            raise SyncwheelError(
                'coordination repair post-verification failed: invalid digest-heal evidence'
            )
    if missing_ref_repair:
        evidence = verified.get('repair_evidence')
        claim_tip = (verified.get('claims') or {}).get(plan['repairedRef'])
        if (
            verified.get('operation_token') != operation['operation_token']
            or claim_tip != (child.get('claims') or {}).get(plan['repairedRef'])
            or not isinstance(evidence, dict)
            or evidence.get('planDigest') != supplied_digest
            or evidence.get('proof') != plan.get('proof')
            or evidence.get('claimCommit') != claim_tip
        ):
            raise SyncwheelError(
                'coordination repair post-verification failed: invalid missing-ref evidence'
            )
        complete_coordination_publication(
            repo_root,
            resolved_manifest_path,
            operation,
            {'state_tip': child_tip, 'status': 'published'},
        )
    repaired = {
        'status': 'repaired',
        'state_tip': child_tip,
        'parent_state': previous['tip'],
        'plan_digest': supplied_digest,
        'backend': backend.name,
        'backend_result': result,
        'proof': plan.get('proof'),
    }
    if digest_migration_repair:
        repaired['state_digest_form'] = COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST
        repaired['manifest_digest'] = plan['expectedManifestDigest']
        repaired['previous_manifest_digest'] = plan['recordedManifestDigest']
    if digest_heal_repair:
        repaired['state_digest_form'] = COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST
        repaired['manifest_digest'] = plan['expectedManifestDigest']
        repaired['previous_manifest_digest'] = plan['recordedManifestDigest']
        resolved_manifest_path = (
            Path(manifest_path) if manifest_path else repo_root / '.syncwheel' / 'manifest.json'
        )
        record_coordination_state_digest_heal(
            repo_root, resolved_manifest_path, config, plan, child_tip
        )
    return repaired


def coordination_compose_local_control_suffix(
    repo_root, published_integration_tip, local_integration_tip,
):
    """Prove the complete local-only suffix is linear manifest control history."""
    refusal = (
        'coordination compose local integration must equal the published tip or '
        'carry a complete linear manifest-only control suffix'
    )
    if not local_integration_tip:
        raise SyncwheelError(refusal)
    if local_integration_tip == published_integration_tip:
        return []
    ancestor = git(
        repo_root,
        'merge-base',
        '--is-ancestor',
        published_integration_tip,
        local_integration_tip,
        check=False,
    )
    if ancestor.returncode != 0:
        raise SyncwheelError(refusal)
    reverse_suffix = []
    current = local_integration_tip
    while current != published_integration_tip:
        parents = git(
            repo_root, 'show', '-s', '--format=%P', current, check=False
        ).stdout.split()
        if len(parents) != 1 or not is_manifest_only_commit(repo_root, current):
            raise SyncwheelError(refusal)
        reverse_suffix.append(current)
        current = parents[0]
    return list(reversed(reverse_suffix))


def coordination_compose_stack_plan(
    repo_root, manifest, stack_id, known_base_state_tip, known_base_manifest_digest
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
    if base_state['manifest_digest'] != known_base_manifest_digest:
        raise SyncwheelError('coordination compose known base manifest digest does not match state')
    verify_coordination_state_manifest_digest(repo_root, base_state, config['remote'])
    latest_digest = verify_coordination_state_manifest_digest(
        repo_root, latest['state'], config['remote']
    )
    local_snapshot = coordination_manifest_snapshot(manifest, repo_root)
    composition = compose_additive_coordination_snapshots(
        base_state['manifest'], local_snapshot, latest['state']['manifest'], stack_id
    )
    merged_snapshot = composition['merged']
    stack = require_stack(manifest, stack_id)
    source_ref = f"refs/heads/{stack['branch']}"
    source_tip = ref_tip(repo_root, stack['branch'])
    if not source_tip or not re.fullmatch(r'[0-9a-f]{40}', source_tip):
        raise SyncwheelError('coordination compose requested stack has no exact local source tip')
    claimed_refs = list(dict.fromkeys([*latest['state']['managed_refs'], source_ref]))
    require_exclusive_coordination_ownership(repo_root, config, claimed_refs)
    observed_refs = remote_ref_tips(
        repo_root, config['remote'], [*claimed_refs, coordination_claim_ref(source_ref)]
    )
    drifted = {
        ref: (tip, observed_refs.get(ref))
        for ref, tip in latest['state']['managed_refs'].items()
        if observed_refs.get(ref) != tip
    }
    if drifted:
        raise SyncwheelError('coordination compose STOP: remote state does not match managed refs')
    remote_has_composed_snapshot = latest['state']['manifest'] == merged_snapshot
    remote_source_tip = observed_refs.get(source_ref)
    if remote_has_composed_snapshot:
        if latest['state']['managed_refs'].get(source_ref) != source_tip or remote_source_tip != source_tip:
            raise SyncwheelError('coordination compose remote composed state has a different source tip')
        status = 'adopt-only'
    else:
        if (
            remote_source_tip is not None
            and remote_source_tip != latest['state']['managed_refs'].get(source_ref)
        ):
            raise SyncwheelError('coordination compose refuses to adopt an existing unowned source ref')
        status = 'publish-required'
    integration_ref = f"refs/heads/{manifest['integration']['branch']}"
    integration_tip = latest['state']['managed_refs'].get(integration_ref)
    if not integration_tip or observed_refs.get(integration_ref) != integration_tip:
        raise SyncwheelError('coordination compose requires an exact unchanged integration tip')
    local_integration_tip = ref_tip(repo_root, manifest['integration']['branch'])
    local_control_suffix = coordination_compose_local_control_suffix(
        repo_root, integration_tip, local_integration_tip
    )
    proposed_manifest = apply_coordination_snapshot(manifest, merged_snapshot)
    validation = validate_manifest(repo_root, proposed_manifest)
    if ref_tip(repo_root, manifest['integration']['branch']) != local_integration_tip:
        raise SyncwheelError('coordination compose local integration changed during planning')
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
        'knownBaseManifestDigest': known_base_manifest_digest,
        'expectedRemoteStateTip': latest['tip'],
        'expectedRemoteManifestDigest': latest['state']['manifest_digest'],
        'localProposalDigest': manifest_digest(manifest),
        'composedSnapshot': merged_snapshot,
        'proposedManifestDigest': manifest_digest(
            retain_local_compose_stacks(
                manifest, proposed_manifest, composition['localRetainedStacks']
            )
        ),
        'integrationManifestDigest': latest_digest['control_manifest_digest'],
        'stack': stack_id,
        'sourceRef': source_ref,
        'sourceTip': source_tip,
        'expectedRemoteSourceTip': remote_source_tip,
        'guardedRefs': dict(sorted(observed_refs.items())),
        'integrationRef': integration_ref,
        'expectedIntegrationTip': integration_tip,
        'expectedIntegrationTree': ref_tree(repo_root, integration_tip),
        'localIntegrationTip': local_integration_tip,
        'localIntegrationControlSuffix': local_control_suffix,
        'localIntegrationControlSuffixDigest': canonical_json_digest(
            local_control_suffix
        ),
        'unmappedIntegrationCommits': unmapped,
        'projectionStatus': 'partial',
        'integrationMutation': False,
        'localAddedStacks': composition['localAddedStacks'],
        'localRetainedStacks': composition['localRetainedStacks'],
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
    publication_landed = False
    if publication_operation:
        config = coordination_config(proposed_manifest)
        observed_publication = read_remote_coordination_state(
            repo_root,
            config,
            fetch=True,
            local_manifest_version=proposed_manifest['version'],
        )
        publication_landed = coordinated_operation_landed(
            repo_root,
            config,
            observed_publication,
            publication_operation,
            claims_fallback=True,
        )
    if publication_operation and publication_landed:
        if observed_publication['tip'] != plan['expectedRemoteStateTip']:
            raise SyncwheelError('coordination compose STOP: reviewed plan drifted')
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
            plan.get('knownBaseManifestDigest'),
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
        if (
            result.get('status') not in {'recovered', 'already_published'}
            or result.get('state_tip') != plan['expectedRemoteStateTip']
            or latest['state'].get('manifest') != plan['composedSnapshot']
        ):
            raise SyncwheelError('coordination compose STOP: reviewed plan drifted')
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
            or state.get('manifest_digest') != plan['integrationManifestDigest']
            or state.get('manifest') != plan['composedSnapshot']
            or state.get('changed_refs') != {plan['sourceRef']: plan['sourceTip']}
            or state.get('managed_refs', {}).get(plan['integrationRef']) != plan['expectedIntegrationTip']
            or state.get('projection_status') != 'partial'
            or state.get('tombstones') != latest['state'].get('tombstones')
        ):
            raise SyncwheelError('coordination compose post-verification failed: invalid state child')
        verify_coordination_state_manifest_digest(repo_root, state, config['remote'])
        require_exclusive_coordination_ownership(
            repo_root, config, state['managed_refs']
        )
        observed = remote_ref_tips(repo_root, config['remote'], state['managed_refs'])
        if observed != state['managed_refs']:
            raise SyncwheelError('coordination compose post-verification failed: managed refs drifted')
    observed_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if not observed_manifest or manifest_digest(observed_manifest) != plan['localProposalDigest']:
        raise SyncwheelError(
            'coordination compose local adoption pending: manifest drifted after remote publication'
        )
    try:
        save_manifest_with_ledger(
            repo_root,
            manifest_path,
            retain_local_compose_stacks(
                observed_manifest, proposed_manifest, plan['localRetainedStacks']
            ),
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
    verified_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
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
    return _publication_coordinator(repo_root).create_coordination_state_commit(
        state, parent_tip,
    )


def coordination_claim_from_commit(repo_root, commit):
    return _publication_intents(repo_root).coordination_claim_from_commit(
        commit,
    )


def create_coordination_claim_commit(
    repo_root,
    source_ref,
    coordination_id,
    operation_token,
    parent_tip=None,
    *,
    closed=False,
    reason=None,
    publication_scope=None,
    changed_refs=(),
):
    return _publication_coordinator(repo_root).create_coordination_claim_commit(
        source_ref, coordination_id, operation_token, parent_tip, closed=closed, reason=reason, publication_scope=publication_scope, changed_refs=changed_refs,
    )


def fetch_coordination_claim(repo_root, remote, claim_ref, tip):
    return _publication_intents(repo_root).fetch_coordination_claim(
        remote, claim_ref, tip,
    )


def atomic_push_capability_probe(repo_root, remote):
    return _publication_coordinator(repo_root).atomic_push_capability_probe(
        remote,
    )


def acquire_local_coordination_lease(repo_root, config, installation):
    return _publication_coordinator(repo_root).acquire_local_coordination_lease(
        config, installation,
    )


def release_local_coordination_lease(repo_root, token):
    return _publication_coordinator(repo_root).release_local_coordination_lease(
        token,
    )


def record_pending_coordination_merge(repo_root, config, expected, latest, manifest):
    return _publication_coordinator(repo_root).record_pending_coordination_merge(
        config, expected, latest, manifest,
    )


def clear_pending_coordination_merge(repo_root, config):
    return _publication_coordinator(repo_root).clear_pending_coordination_merge(
        config,
    )


def record_coordination_state_seen(repo_root, config, state_tip):
    return _publication_coordinator(repo_root).record_coordination_state_seen(
        config, state_tip,
    )


def published_stack_owner(repo_root, stack_id, state_tip):
    return _publication_coordinator(repo_root).published_stack_owner(
        stack_id, state_tip,
    )


def stack_owner_summary(repo_root, manifest_path, config, stack_id, state_tip=None):
    return _publication_coordinator(repo_root).stack_owner_summary(
        manifest_path, config, stack_id, state_tip,
    )


def bounded_stack_entries(entries, limit=PENDING_STACK_LIST_LIMIT):
    shown, used = [], 0
    for entry in entries:
        if shown and used + len(entry) > limit:
            break
        shown.append(entry)
        used += len(entry) + 2
    hidden = len(entries) - len(shown)
    return '; '.join(shown) + (f'; and {hidden} more stack(s)' if hidden else '')


def stack_reset_is_safe(repo_root, published_stack, local_stack):
    return _publication_coordinator(repo_root).stack_reset_is_safe(
        published_stack, local_stack,
    )


def missing_declared_stacks_message(repo_root, manifest_path, manifest, missing):
    config = coordination_config(manifest) if coordination_is_active(manifest) else None
    published = local = tip = None
    if config:
        try:
            observed = read_remote_coordination_state(
                repo_root, config, fetch=True, local_manifest_version=manifest['version']
            )
            tip = observed['tip']
            published = stack_snapshot_map((observed.get('state') or {}).get('manifest') or {})
            local = stack_snapshot_map(coordination_manifest_snapshot(manifest, repo_root))
        except SyncwheelError:
            config = published = None
    entries = []
    for stack_id, commits in missing.items():
        status = ''
        if published is not None:
            status = (
                'published, ' if published.get(stack_id) == local.get(stack_id)
                else 'local proposal, '
            )
        owner = format_stack_owner(stack_owner_summary(
            repo_root, manifest_path, config, stack_id, state_tip=tip
        ))
        hidden = len(commits) - PENDING_STACK_COMMIT_LIMIT
        shas = ','.join(commits[:PENDING_STACK_COMMIT_LIMIT]) + (
            f' and {hidden} more' if hidden > 0 else ''
        )
        entries.append(f'{stack_id} ({status}missing {shas}; {owner})')
    return (
        bounded_stack_entries(entries)
        + '. The owner of each listed stack must publish or reset it; '
        'run syncwheel handoff for details'
    )


def handoff_misalignment_detail(repo_root, manifest_path, manifest, config, observed):
    published = (observed.get('state') or {}).get('manifest')
    try:
        local = coordination_manifest_snapshot(manifest, repo_root)
    except SyncwheelError:
        return ''
    if local == published:
        return (
            ': the local integration control manifest is not published; run '
            'syncwheel int rebuild, then syncwheel int push'
        )
    diff = local_proposal_diff(local, published)
    pending = [*diff['added'], *diff['changed']]
    if not pending and not diff['removed'] and not diff['integration']:
        return (
            ': the local manifest differs from the published state outside its stack '
            'list; run syncwheel handoff for details'
        )
    entries = []
    for stack_id in [*pending, *diff['removed']]:
        change = (
            f"changed {', '.join(diff['changed'][stack_id])}" if stack_id in diff['changed']
            else 'added' if stack_id in diff['added'] else 'published but absent locally'
        )
        owner = format_stack_owner(stack_owner_summary(
            repo_root, manifest_path, config, stack_id, state_tip=observed.get('tip')
        ))
        entries.append(f'{stack_id} ({change}; {owner})')
    listed = bounded_stack_entries(entries)
    if diff['integration']:
        listed += f"; integration changed: {', '.join(diff['integration'])}"
    remedy = '. The owner of each listed stack must publish or reset it; run syncwheel handoff for details'
    published_stacks = stack_snapshot_map(published or {})
    local_stacks = stack_snapshot_map(local)
    resettable = [
        f'syncwheel stack set {stack_id} --published' for stack_id in diff['changed']
        if stack_reset_is_safe(repo_root, published_stacks[stack_id], local_stacks[stack_id])
    ]
    if resettable:
        remedy += '; to drop a local commit change, run ' + bounded_stack_entries(
            resettable, PENDING_RESET_LIST_LIMIT
        )
    return ': unpublished local proposal: ' + listed + remedy


def require_sha1_repository(repo_root, operation):
    return _publication_coordinator(repo_root).require_sha1_repository(
        operation,
    )


def coordination_state_chain_contains(repo_root, coordination_id, base_tip, latest_tip):
    return _publication_coordinator(repo_root).coordination_state_chain_contains(
        coordination_id, base_tip, latest_tip,
    )


def compose_additive_coordination_snapshots(base, local, remote, requested_stack):
    return _coordination.compose_additive_coordination_snapshots(
        base, local, remote, requested_stack, branch_is_valid=is_valid_coordination_branch_ref,
    )


def retain_local_compose_stacks(local_manifest, composed_manifest, retained):
    """Put unpublished local stacks back after adopting a composed snapshot."""
    adopted = json.loads(json.dumps(composed_manifest))
    local_stacks = stack_snapshot_map(local_manifest)
    local_members = (local_manifest.get('integration') or {}).get('stacks') or []
    for stack_id in retained:
        adopted['stacks'].append(json.loads(json.dumps(local_stacks[stack_id])))
        if stack_id in local_members:
            adopted['integration']['stacks'].append(stack_id)
    return adopted


def apply_coordination_snapshot(manifest, snapshot):
    return _coordination.apply_coordination_snapshot(
        manifest, snapshot, branch_is_valid=is_valid_coordination_branch_ref,
    )


def coordination_state_matches_remote(repo_root, config, state):
    return _publication_coordinator(repo_root).coordination_state_matches_remote(
        config, state,
    )


def coordination_branch_worktrees(repo_root, branch):
    return _publication_coordinator(repo_root).coordination_branch_worktrees(
        branch,
    )


def align_equivalent_coordination_refs(repo_root, config, state, changed_refs):
    return _publication_coordinator(repo_root).align_equivalent_coordination_refs(
        config, state, changed_refs,
    )


def classify_coordination_race(repo_root, manifest, config, expected, changed_refs, projection_status):
    return _publication_coordinator(repo_root).classify_coordination_race(
        manifest, config, expected, changed_refs, projection_status,
    )


def fetch_coordination_ref_tip(repo_root, config, ref, expected_tip):
    return _publication_coordinator(repo_root).fetch_coordination_ref_tip(
        config, ref, expected_tip,
    )


def coordination_ref_is_safe_successor(repo_root, config, ref, remote_tip, candidate_tip):
    return _publication_coordinator(repo_root).coordination_ref_is_safe_successor(
        config, ref, remote_tip, candidate_tip,
    )


def deterministic_stack_replay_tip(repo_root, base, commits):
    return _replay_engine(repo_root).deterministic_stack_replay_tip(
        base, commits,
    )


def deterministic_stack_projection(repo_root, base, commits):
    return _replay_engine(repo_root).deterministic_stack_projection(
        base, commits,
    )


def coordination_stack_ref_is_exact_rebase(
    repo_root,
    manifest,
    remote_snapshot,
    stack_id,
    remote_tip,
    candidate_tip,
):
    return _publication_coordinator(repo_root).coordination_stack_ref_is_exact_rebase(
        manifest, remote_snapshot, stack_id, remote_tip, candidate_tip,
    )


def integration_partial_stack_adoption_allowed(
    remote_integration, local_integration, added, changed_stack_refs, tombstone=None
):
    return _publication_coordinator(None).integration_partial_stack_adoption_allowed(
        remote_integration, local_integration, added, changed_stack_refs, tombstone,
    )


def validate_coordination_changed_ref_successors(
    repo_root,
    manifest,
    config,
    state,
    remote_snapshot,
    local_snapshot,
    changed_refs,
):
    return _publication_coordinator(repo_root).validate_coordination_changed_ref_successors(
        manifest, config, state, remote_snapshot, local_snapshot, changed_refs,
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
    manifest_path=None,
):
    return _publication_coordinator(repo_root).validate_coordination_publication_base(
        manifest, config, expected, changed_refs, tombstone, rename, state_transition, remedy_stack, creation_remedy, manifest_path,
    )


def coordination_publication_lock_path(repo_root):
    return _publication_coordinator(repo_root).coordination_publication_lock_path()


def coordination_publication_lock(repo_root, **options):
    return _publication_coordinator(repo_root).coordination_publication_lock(**options)


def coordination_publication_lock_required(function):
    """Run a publication-state transition under this clone's single-writer lock."""
    @functools.wraps(function)
    def wrapper(repo_root, *args, **kwargs):
        with coordination_publication_lock(repo_root):
            return function(repo_root, *args, **kwargs)
    return wrapper


def coordination_publication_owner(repo_root):
    return _publication_coordinator(repo_root).coordination_publication_owner()


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
    identity_changed_refs=None,
):
    return _publication_intents(repo_root).coordination_publication_identity(
        manifest, changed_refs, scope, projection_status, tombstone=tombstone, rename=rename, state_transition=state_transition, publication_manifest=publication_manifest, identity_changed_refs=identity_changed_refs,
    )


def pending_coordination_publications(repo_root, manifest_path):
    return _publication_intents(repo_root).pending_coordination_publications(
        manifest_path,
    )


def pending_coordination_publication(repo_root, manifest_path, fingerprint):
    return _publication_intents(repo_root).pending_coordination_publication(
        manifest_path, fingerprint,
    )


def pending_coordination_publication_for_scope(
    repo_root, manifest_path, scope
):
    return _publication_intents(repo_root).pending_coordination_publication_for_scope(
        manifest_path, scope,
    )


def coordination_remote_is_reachable(repo_root, remote):
    return _publication_intents(repo_root).coordination_remote_is_reachable(
        remote,
    )


def require_new_managed_refs_absent(
    config, state, changed_refs, observed_refs, repo_root=None, adopt_remote_tips=None,
    integration_ref=None,
):
    """Refuse to take over a remote branch the published state never managed.

    The publication lease is the observed tip, so without this check a branch
    pushed outside coordination would be silently replaced by a new stack.
    Before any state exists (the first coordinated publication) every remote
    branch is unmanaged: it may only be moved forward, unless the caller named
    its exact tip as the one to replace.
    """
    if not state:
        require_first_publication_keeps_remote_work(
            config, changed_refs, observed_refs, repo_root, adopt_remote_tips or {},
            integration_ref,
        )
        return
    if adopt_remote_tips:
        raise SyncwheelError(
            '--adopt-remote only applies to the first coordinated publication; '
            'coordination state is already published'
        )
    managed = state.get('managed_refs') or {}
    closed_tips = {
        coordination_tombstone_ref(item): item.get('remote_tip')
        for item in state.get('tombstones') or []
        if isinstance(item, dict)
    }
    for ref in sorted(changed_refs):
        observed = observed_refs.get(ref)
        if ref in managed or not observed or observed == changed_refs[ref]:
            continue
        if closed_tips.get(ref) and closed_tips[ref] == observed:
            continue
        raise SyncwheelError(
            f'{ref} already exists on {config["remote"]} at {observed} but is not '
            'managed by the published coordination state; refusing to replace it. '
            'Pick another branch for the new stack, or delete the remote branch '
            'after confirming nobody owns it'
        )


def require_first_publication_keeps_remote_work(
    config, changed_refs, observed_refs, repo_root, adopt_remote_tips,
    integration_ref=None,
):
    for ref, expected_tip in adopt_remote_tips.items():
        if ref not in changed_refs or observed_refs.get(ref) != expected_tip:
            raise SyncwheelError(
                f'first coordinated publish STOP: --adopt-remote expected {ref} '
                f'at {expected_tip}, observed {observed_refs.get(ref) or "absent"}; '
                'inspect the current remote tip and preview again'
            )
    for ref in sorted(changed_refs):
        observed = observed_refs.get(ref)
        new_tip = changed_refs[ref]
        if not observed or observed == new_tip or adopt_remote_tips.get(ref) == observed:
            continue
        if repo_root is not None and commit_exists(repo_root, observed):
            if branch_contains(repo_root, new_tip, observed):
                continue
            detail = 'is not an ancestor of the local tip'
        else:
            detail = 'has not been fetched by this clone'
        remedy = 'Fetch it and fold any work you need into stacks first.'
        if ref == integration_ref:
            remedy += (
                ' Once nothing on it may be lost, replace exactly that tip with '
                f'`syncwheel int push --adopt-remote {observed}`'
            )
        else:
            remedy += ' Reconcile the stack with that branch or choose a different branch name.'
        raise SyncwheelError(
            f'first coordinated publish STOP: {ref} already exists on {config["remote"]} '
            f'at {observed}, which {detail}; publishing {new_tip} would discard its '
            f'commits. {remedy}'
        )


def coordinated_publish_command_failure(repo_root, config, exc, remedy):
    return _publication_intents(repo_root).coordinated_publish_command_failure(
        config, exc, remedy,
    )


def coordination_state_commits_since(repo_root, tip, since_tip):
    return _publication_intents(repo_root).coordination_state_commits_since(
        tip, since_tip,
    )


def coordination_state_document_from_commit(repo_root, commit):
    return _publication_intents(repo_root).coordination_state_document_from_commit(
        commit,
    )


def coordination_commit_operation_token(repo_root, commit):
    return _publication_intents(repo_root).coordination_commit_operation_token(
        commit,
    )


def coordination_claim_history_matches(
    repo_root, config, claim_tip, source_ref, predicate
):
    return _publication_intents(repo_root).coordination_claim_history_matches(
        config, claim_tip, source_ref, predicate,
    )


def coordination_claim_history_carries_token(
    repo_root, config, claim_tip, source_ref, operation_token
):
    return _publication_intents(repo_root).coordination_claim_history_carries_token(
        config, claim_tip, source_ref, operation_token,
    )


def coordination_state_claims_carry_operation(repo_root, config, state, operation):
    return _publication_intents(repo_root).coordination_state_claims_carry_operation(
        config, state, operation,
    )


def coordination_remote_claims_prove_operation(repo_root, config, operation):
    return _publication_intents(repo_root).coordination_remote_claims_prove_operation(
        config, operation,
    )


def coordinated_operation_landing_evidence(
    repo_root, config, observed, operation, *, claims_fallback=False
):
    return _publication_intents(repo_root).coordinated_operation_landing_evidence(
        config, observed, operation, claims_fallback=claims_fallback,
    )


def coordinated_operation_landed(
    repo_root, config, observed, operation, *, claims_fallback=False
):
    return _publication_intents(repo_root).coordinated_operation_landed(
        config, observed, operation, claims_fallback=claims_fallback,
    )


def abandoned_publication_rename_is_restorable(repo_root, config, intent):
    return _publication_intents(repo_root).abandoned_publication_rename_is_restorable(
        config, intent,
    )


def restore_abandoned_publication_rename(repo_root, config, intent):
    return _publication_intents(repo_root).restore_abandoned_publication_rename(
        config, intent,
    )


def resolve_pending_coordination_publications(
    repo_root, manifest, manifest_path, *, adopt_tokens=()
):
    return _publication_intents(repo_root).resolve_pending_coordination_publications(
        manifest, manifest_path, adopt_tokens=adopt_tokens,
    )


def renew_coordination_publication(repo_root, manifest, manifest_path, pending):
    return _publication_intents(repo_root).renew_coordination_publication(
        manifest, manifest_path, pending,
    )


def pending_coordination_publication_after_resolution(
    repo_root, manifest, manifest_path, fingerprint
):
    return _publication_intents(repo_root).pending_coordination_publication_after_resolution(
        manifest, manifest_path, fingerprint,
    )


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
    identity_changed_refs=None,
):
    return _publication_intents(repo_root).begin_coordination_publication(
        manifest, manifest_path, changed_refs, scope, projection_status, tombstone=tombstone, rename=rename, state_transition=state_transition, publication_manifest=publication_manifest, expected_state_tip=expected_state_tip, identity_changed_refs=identity_changed_refs,
    )


def complete_coordination_publication(
    repo_root, manifest_path, operation, result
):
    return _publication_intents(repo_root).complete_coordination_publication(
        manifest_path, operation, result,
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
    return _publication_coordinator(repo_root).coordinated_publication_matches_remote(
        config, state, changed_refs, touched_refs, scope, projection_status, manifest_digest_value, operation_token,
    )


def coordinated_publish(repo_root, *args, **kwargs):
    return _publication_coordinator(repo_root).coordinated_publish(*args, **kwargs)


def coordinated_publish_cycle(
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
    return _publication_coordinator(repo_root).coordinated_publish_cycle(
        manifest, manifest_path, changed_refs, scope, projection_status, dry_run, tombstone, rename, state_transition, expected_coordination_state_tip, expected_observed_refs, expected_coordination_state_refs, preflight_complete, remedy_stack, creation_remedy, operation_token, publication_manifest,
    )




def local_manifest_projection_is_convergent(repo_root, manifest, manifest_path=None):
    return _publication_coordinator(repo_root).local_manifest_projection_is_convergent(
        manifest, manifest_path,
    )


def apply_pending_coordination_merge(
    repo_root, manifest, manifest_path, *, persist=True, expected_digest=None
):
    return _publication_coordinator(repo_root).apply_pending_coordination_merge(
        manifest, manifest_path, persist=persist, expected_digest=expected_digest,
    )


def local_worktree_status(path):
    result = run(
        ['git', '-C', str(path), 'status', '--porcelain'],
        check=False, env={'GIT_OPTIONAL_LOCKS': '0'},
    )
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


def coordination_gc_plan(
    repo_root, manifest, fetch=True, state_info=None, implicit=False, stack_scope=None,
):
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
        if stack_scope is not None and stack_id not in stack_scope:
            skipped.append(f'{label}: outside the selected stack scope')
            continue
        if tombstone.get('reason') == 'archived':
            skipped.append(f'{label}: archived source is retained; explicit recovery review required')
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

    backups = backup_branch_records(repo_root)
    if implicit:
        if backups:
            skipped.append('backup refs: only an explicit syncwheel gc deletes backups')
        backups = []
    worktree_branches = set(worktrees)
    for index, backup in enumerate(backups):
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


def coordination_gc_candidate_is_current(
    repo_root, manifest, candidate, fetch, state_info, implicit=False, stack_scope=None,
):
    refreshed = coordination_gc_plan(
        repo_root,
        manifest,
        fetch=fetch,
        state_info=None if fetch else state_info,
        implicit=implicit,
        stack_scope=stack_scope,
    )
    target = coordination_gc_candidate_key(candidate)
    return any(
        coordination_gc_candidate_key(current) == target
        for current in refreshed['candidates']
    )


def coordination_gc_note_skip(plan, candidate):
    label = candidate.get('stack') or candidate.get('branch') or candidate.get('path') or 'unknown'
    plan['skipped'].append(f'{label}: no longer eligible immediately before deletion')


def run_coordination_gc(
    repo_root, manifest, apply=False, fetch=True, state_info=None, implicit=False, stack_scope=None,
):
    scope = {'implicit': implicit, 'stack_scope': stack_scope}
    plan = coordination_gc_plan(repo_root, manifest, fetch=fetch, state_info=state_info, **scope)
    if not apply or not plan['enabled']:
        return plan
    removed_worktrees = set()
    applied_candidates = []
    for candidate in plan['candidates']:
        if candidate['type'] != 'remove_worktree':
            continue
        if not coordination_gc_candidate_is_current(
            repo_root, manifest, candidate, fetch, state_info, **scope
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
            repo_root, manifest, candidate, fetch, state_info, **scope
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
    named_lanes = list(dict.fromkeys(getattr(args, 'lanes', None) or []))
    candidate_ids = None
    if named_lanes:
        eligible = {lane['id'] for lane in governed_candidates}
        diagnostics = {
            lane['id']: lane
            for lane in governed_worktree_diagnostics(repo_root, manifest, manifest_path)['lanes']
            if lane.get('id')
        }
        refused = []
        for lane_id in named_lanes:
            if lane_id in eligible:
                continue
            lane = diagnostics.get(lane_id)
            refused.append(
                f'{lane_id} [{lane["code"] or "active"}]' if lane else f'{lane_id} [unknown]'
            )
        if refused:
            raise SyncwheelError(
                'gc --lane names lanes that are not cleanup candidates: '
                + ', '.join(refused)
                + '; inspect them with syncwheel status, or use worktree release <lane>'
            )
        governed_candidates = [lane for lane in governed_candidates if lane['id'] in named_lanes]
        candidate_ids = set(named_lanes)
    governed_result = (
        reconcile_governed_worktrees(
            repo_root,
            manifest,
            manifest_path,
            candidate_ids=candidate_ids,
        )
        if args.apply else {'reaped': [], 'failures': []}
    )
    # A gc that names its lanes removes only those lanes; other local
    # artifacts are reported, never removed.
    plan = run_coordination_gc(
        repo_root, manifest, apply=args.apply and not named_lanes, fetch=args.fetch,
    )
    plan['applied'] = bool(args.apply)
    plan['governed_worktrees'] = governed_worktree_diagnostics(repo_root, manifest, manifest_path)
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
    validation = validate_manifest(repo_root, manifest, manifest_path)
    config = coordination_config(manifest)
    output = {
        'manifest_path': str(manifest_path),
        'manifest_version': manifest['version'],
        'validation': validation,
        'governed_worktrees': governed_worktree_diagnostics(repo_root, manifest, manifest_path),
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
        local_snapshot = coordination_manifest_snapshot(manifest, repo_root)
        state = state_info['state']
        output['coordination'].update({
            'state_tip': state_info['tip'],
            'state_status': 'uninitialized' if not state else 'published',
            'manifest_relation': (
                'no_published_state' if not state
                else (
                    'aligned'
                    if state.get('manifest') == local_snapshot
                    else 'local_proposal_differs'
                )
            ),
            'local_proposal': (
                local_proposal_diff(local_snapshot, state.get('manifest')) if state else None
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
            if coordination['local_proposal']:
                for line in local_proposal_lines(coordination['local_proposal']):
                    print(f'  {line}')
            print(f"ownership conflicts: {len(coordination['ownership_conflicts'])}")
            print(f"gc candidates: {len(coordination['gc']['candidates'])}")
    has_ownership_conflict = bool(output['coordination'].get('ownership_conflicts'))
    return 1 if validation['errors'] or has_ownership_conflict else 0


def default_worktree_path(repo_root, branch):
    safe = branch.replace('/', '-').replace('\\', '-')
    return repo_root.parent / f'{repo_root.name}-wt-{safe}'


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
    # Rebase keeps the branch name in its per-worktree state while HEAD is detached.
    paused = []
    for record in get_worktrees(repo_root):
        if not record.get('detached'):
            continue
        path = Path(record['path'])
        # A prunable worktree still has authoritative rebase state in its
        # common-Git registration. Do not run a subprocess in a missing cwd.
        git_dir = governed_worktree_admin_dir_for_path(repo_root, path)
        if git_dir is None:
            if not path.is_dir():
                continue
            git_dir = Path(git(path, 'rev-parse', '--absolute-git-dir').stdout.strip())
        for directory in ('rebase-merge', 'rebase-apply'):
            head_name = git_dir / directory / 'head-name'
            if head_name.is_file() and not head_name.is_symlink():
                if head_name.read_text().strip() == f'refs/heads/{branch}':
                    paused.append(path)
                    break
    if len(paused) > 1:
        raise SyncwheelError(f'multiple paused rebase worktrees name branch: {branch}')
    if paused:
        if not paused[0].is_dir():
            raise SyncwheelError(f'paused rebase worktree is missing: {paused[0]}; restore it before resuming')
        return paused[0]
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


def git_supports_write_tree(repo_root):
    return _replay_engine(repo_root).git_supports_write_tree()


def configured_replay_mode(repo_root, manifest):
    return _replay_engine(repo_root).configured_replay_mode(
        manifest,
    )


def auto_replay_mode(
    repo_root,
    branch,
    location,
    *,
    explicit_worktree=False,
    plumbing_supported=True,
):
    return _replay_engine(repo_root).auto_replay_mode(
        branch, location, explicit_worktree=explicit_worktree, plumbing_supported=plumbing_supported,
    )


def select_replay_mode(
    repo_root,
    manifest,
    args,
    branch,
    location,
    *,
    plumbing_supported=True,
):
    return _replay_engine(repo_root).select_replay_mode(
        manifest, args, branch, location, plumbing_supported=plumbing_supported,
    )


def resolve_replay_mode(repo_root, location, requested_mode):
    return _replay_engine(repo_root).resolve_replay_mode(
        location, requested_mode,
    )


def require_journal_manifest(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    if manifest.get('repository_mode') != 'journal':
        raise SyncwheelError('journal commands require manifest repository_mode="journal"')
    return repo_root, manifest, manifest_path


def journal_status_entries(repo_root):
    result = git(
        repo_root, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
        '--ignore-submodules=all', '--no-renames',
    )
    return _journal.parse_status_entries(result.stdout)


def journal_real_index_clean(repo_root):
    return git(repo_root, 'diff', '--cached', '--quiet', 'HEAD', '--', check=False).returncode == 0


def journal_admission(repo_root, journal, paths=None):
    return _journal.admit_paths(
        repo_root, journal, journal_status_entries(repo_root), paths,
        read_file=journal_read_admitted_file,
    )


def journal_snapshot(repo_root, manifest, apply=False, paths=None):
    return JournalWorkflow(_JournalRepository(repo_root)).snapshot(manifest, apply, paths)


def journal_remote_tip(repo_root, remote, branch):
    return JournalWorkflow(_JournalRepository(repo_root)).remote_tip(remote, branch)


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
    payload = journal_snapshot(repo_root, manifest, apply=args.apply, paths=args.paths)
    print(json.dumps(payload, indent=2))
    return 0


def command_journal_publish(args):
    repo_root, manifest, _ = require_journal_manifest(args)
    payload = journal_publish(repo_root, manifest, apply=args.apply, committed_only=args.committed_only is True)
    output = dict(payload)
    output['excluded_count'] = len(output.pop('excluded'))
    output['pull'] = dict(output['pull'])
    output['pull']['kept_local_count'] = len(output['pull'].pop('kept_local'))
    print(json.dumps(output, indent=2))
    return 0


def journal_publish(repo_root, manifest, apply=False, committed_only=False):
    return JournalWorkflow(_JournalRepository(repo_root)).publish(manifest, apply, committed_only)


def git_bytes(repo_root, *args, ok_codes=(0,)):
    result = subprocess.run(
        ['git', *args], cwd=repo_root, capture_output=True, env=managed_process_env(authorize=False),
    )
    if result.returncode not in ok_codes:
        raise SyncwheelError(result.stderr.decode(errors='replace').strip() or f"git {' '.join(args)} failed")
    return result.stdout


def journal_pull(repo_root, manifest, apply=False, park_conflicts=False):
    return JournalWorkflow(_JournalRepository(repo_root)).pull(manifest, apply, park_conflicts)


def command_journal_pull(args):
    repo_root, manifest, _ = require_journal_manifest(args)
    payload = journal_pull(
        repo_root, manifest, apply=args.apply, park_conflicts=args.park_conflicts
    )
    print(json.dumps(payload, indent=2))
    if payload.get('resolution_required'):
        print(
            'journal pull: unresolved conflicts parked in '
            f"{payload['parked']['directory']}; merge them into the working tree, "
            'then run syncwheel journal publish --apply',
            file=sys.stderr,
        )
    return 0


def journal_recover(repo_root, manifest, apply=False, rollback=None, manifest_path=None):
    return JournalWorkflow(_JournalRepository(repo_root)).recover(manifest, apply, rollback, manifest_path)


def command_journal_recover(args):
    repo_root, manifest, manifest_path = require_journal_manifest(args)
    payload = journal_recover(repo_root, manifest, apply=args.apply, rollback=args.rollback,
                              manifest_path=manifest_path)
    print(json.dumps(payload, indent=2))
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


def journal_unit_contents(repo_root, manifest, committed_only=False):
    executable = journal_executable()
    repo = str(repo_root.resolve())
    interval = manifest['journal']['interval']
    def systemd_quote(value):
        return '"' + value.replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"') + '"'

    publish_option = ' --committed-only' if committed_only else ''
    service = '\n'.join([
        '[Unit]', 'Description=Syncwheel journal snapshot and publish', '',
        '[Service]', 'Type=oneshot',
        f'ExecStart={systemd_quote(executable)} journal publish --repo {systemd_quote(repo)}{publish_option} --apply', '',
    ])
    timer = '\n'.join([
        '[Unit]', 'Description=Run Syncwheel journal periodically', '',
        '[Timer]', f'OnActiveSec={interval}', f'OnUnitInactiveSec={interval}', '',
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
    service, timer = journal_unit_contents(repo_root, manifest, committed_only=args.committed_only is True)
    # The previous managed timer had no initial trigger. Accept only that exact
    # template as an upgrade/removal candidate, never an arbitrary unit file.
    old_timer = timer.replace(f'OnActiveSec={manifest["journal"]["interval"]}\n', '').replace(
        'OnUnitInactiveSec=' + manifest['journal']['interval'] + '\n',
        'OnUnitInactiveSec=' + manifest['journal']['interval'] + '\nPersistent=true\n',
    )
    managed_files = ((service_path, service), (timer_path, timer))
    alternate_service, _ = journal_unit_contents(
        repo_root, manifest, committed_only=not (args.committed_only is True)
    )

    def is_owned_variant(path, content):
        return (path == timer_path and content == old_timer) or (
            path == service_path and content == alternate_service
        )
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
        active = journal_systemctl('is-active', f'{unit_id}.timer', check=False)
        next_elapse = journal_systemctl(
            'show', f'{unit_id}.timer', '--property=NextElapseUSecMonotonic', '--value', check=False
        )
        next_value = next_elapse.stdout.strip()
        payload['armed'] = (
            payload['enabled'] and active.returncode == 0 and next_elapse.returncode == 0
            and next_value not in ('', '0', 'infinity')
        )
        payload['next_elapse_monotonic'] = next_value if next_elapse.returncode == 0 else None
    elif args.schedule_command == 'install' and args.apply:
        upgrading_legacy = timer_path.exists() and timer_path.read_text() == old_timer
        conflicts = [str(path) for path, content in managed_files if path.exists()
                     and path.read_text() != content and not is_owned_variant(path, path.read_text())]
        if conflicts:
            raise SyncwheelError('journal scheduler unit collision; refusing overwrite: ' + ', '.join(conflicts))
        payload['managed_ref_guard'] = install_managed_push_hook(repo_root, apply=True)
        service_path.parent.mkdir(parents=True, exist_ok=True)
        service_path.write_text(service)
        timer_path.write_text(timer)
        journal_systemctl('daemon-reload')
        journal_systemctl('enable', '--now', f'{unit_id}.timer')
        if upgrading_legacy:
            journal_systemctl('restart', f'{unit_id}.timer')
    elif args.schedule_command == 'remove' and args.apply:
        conflicts = [str(path) for path, content in managed_files if path.exists()
                     and path.read_text() != content and not is_owned_variant(path, path.read_text())]
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
    status_short = git(
        repo_root, 'status', '--short', '--branch', check=False, env={'GIT_OPTIONAL_LOCKS': '0'},
    ).stdout.splitlines()
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


def validate_manifest(repo_root, manifest, manifest_path=None):
    warnings = []
    errors = []
    prime_commits(repo_root, [
        commit
        for stack in manifest.get('stacks', [])
        for key in ('commits', 'integration_commits', 'integration_only_commits')
        for commit in (stack.get(key) or [])
        if isinstance(commit, str)
    ])
    details = {'stacks': [], 'channels': [], 'integration': {}, 'coordination': {}}
    hooks = managed_push_guard_policy(repo_root, manifest, manifest_path)
    details['hooks'] = hooks
    if hooks['disabled']:
        warnings.append(f"managed repository guards explicitly disabled: {hooks['disabledReason']}")
    if hooks['migrationPending']:
        warnings.append(
            'managed repository guards required; this clone is pending migration. '
            + '; '.join(hooks['degradedCauses'])
        )
    elif hooks['required'] and hooks['degraded']:
        warnings.append(
            'managed repository guards are degraded. causes: '
            + '; '.join(hooks['degradedCauses'])
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
                if (
                    manifest_is_local_only(manifest)
                    and coordination['remote'] == manifest['defaults']['canonical_remote']
                ):
                    warnings.append(
                        'local-only coordination state is published on the canonical '
                        f"remote {coordination['remote']}: {coordination['state_branch']}"
                    )
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
        if stack.get('adopted_history'):
            try:
                stack_declared_shas.update(declared_stack_history(repo_root, stack))
            except SyncwheelError as exc:
                errors.append(f"stack {stack['id']}: {exc}")
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
        if stack.get('adopted_history') and not stack.get('integration_resolution'):
            declared_commit_shas.update(stack_declared_shas)
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
    return _replay_engine(repo_root).planned_replay_mode(
        manifest, branch, plumbing_supported,
    )


def build_plan(repo_root, manifest, validation):
    if manifest.get('repository_mode') == 'journal':
        # A journal repository has no stacks or integration branch to plan;
        # its only actions are journal recover and publish.
        return []
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
                    'state': 'published',
                    'publication': {'enabled': True},
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
        if stack.get('adopted_history'):
            actions.append({
                'type': 'resume_manual_review', 'stack': stack_id,
                'commit': item['commit'], 'short': item['short'], 'subject': item['subject'],
                'reason': 'adopted_history_requires_explicit_adoption',
            })
            continue
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
    ledger_state = _manifest_storage.load_ledger_state(repo_root, manifest_path)
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


def command_argv_env(command):
    if isinstance(command, tuple):
        return command
    return command, None


def worktree_matches_branch(repo_root, branch, worktree):
    return _replay_engine(repo_root).worktree_matches_branch(
        branch, worktree,
    )


def replay_commit_message(repo_root, commit):
    return _replay_engine(repo_root).replay_commit_message(
        commit,
    )


def replay_cherry_pick_args(repo_root, commit, base, *, projection):
    return _replay_engine(repo_root).replay_cherry_pick_args(
        commit, base, projection=projection,
    )


def plumbing_replay_script(repo_root, branch, base, commits, expected_tip=None):
    return _replay_engine(repo_root).plumbing_replay_script(
        branch, base, commits, expected_tip,
    )


def ensure_plumbing_target_is_unchecked_out(repo_root, branch):
    return _replay_engine(repo_root).ensure_plumbing_target_is_unchecked_out(
        branch,
    )


def replay_plan(repo_root, manifest, target, mode):
    return _replay_engine(repo_root).replay_plan(
        manifest, target, mode,
    )


def replay_stacks_touching(repo_root, steps, stack_id, paths):
    return _replay_engine(repo_root).replay_stacks_touching(
        steps, stack_id, paths,
    )


def integration_replay_step_failure(repo_root, plan, index, exc):
    return _replay_engine(repo_root).integration_replay_step_failure(
        plan, index, exc,
    )


def execute_replay_steps(repo_root, plan):
    return _replay_engine(repo_root).execute_replay_steps(
        plan,
    )


def execute_replay(repo_root, plan, apply):
    return _replay_engine(repo_root).execute_replay(
        plan, apply,
    )


def materialize_stack_projection(repo_root, stack):
    return _replay_engine(repo_root).materialize_stack_projection(
        stack,
    )


def materialize_integration_replay(repo_root, manifest, stack_ref_overrides=None):
    return _replay_engine(repo_root).materialize_integration_replay(
        manifest, stack_ref_overrides,
    )


def materialize_integration_projection(repo_root, manifest, stack_ref_overrides=None):
    return _replay_engine(repo_root).materialize_integration_projection(
        manifest, stack_ref_overrides,
    )






def checkout_reset_guard(repo_root):
    return _projection_service().checkout_reset_guard(
        repo_root,
    )


def checkout_reset_destination_lease(
    repo_root, branch, status_base=None, allowed_paths=None,
    allowed_path_prefixes=None,
):
    return _projection_service().checkout_reset_destination_lease(
        repo_root, branch, status_base, allowed_paths, allowed_path_prefixes,
    )


def checkout_source_lease(repo_root, manifest_path):
    return _projection_service().checkout_source_lease(
        repo_root, manifest_path,
    )




def tree_path_entry(repo_root, tree, relative):
    return _projection_service().tree_path_entry(
        repo_root, tree, relative,
    )


def tree_path_bytes(repo_root, entry):
    return _projection_service().tree_path_bytes(
        repo_root, entry,
    )


def integration_tree_changed_paths(repo_root, left, right):
    return _projection_service().integration_tree_changed_paths(
        repo_root, left, right,
    )


def materialize_control_manifest_projection_tree(
    repo_root, manifest, replay_tree, gitignore_bytes=None, manifest_bytes=None,
):
    return _projection_service().materialize_control_manifest_projection_tree(
        repo_root, manifest, replay_tree, gitignore_bytes, manifest_bytes,
    )


def observe_published_integration_tip(repo_root, manifest, *, fetch=True):
    return _projection_service().observe_published_integration_tip(
        repo_root, manifest, fetch=fetch,
    )


def integration_projection_ref_observation(repo_root, kind, identifier, spec):
    return _projection_service().integration_projection_ref_observation(
        repo_root, kind, identifier, spec,
    )


def integration_projection_input_snapshot(repo_root, manifest):
    return _projection_service().integration_projection_input_snapshot(
        repo_root, manifest,
    )






def plan_published_integration_tip_reuse(
    repo_root, manifest, manifest_path, *, refresh_inputs=True,
):
    return _projection_service().plan_published_integration_tip_reuse(
        repo_root, manifest, manifest_path, refresh_inputs=refresh_inputs,
    )


def published_integration_tip_reuse_is_current(
    repo_root, manifest, manifest_path, plan
):
    return _projection_service().published_integration_tip_reuse_is_current(
        repo_root, manifest, manifest_path, plan,
    )




def published_integration_replay_is_current(
    repo_root, manifest, manifest_path, plan, expected_source_lease,
):
    return _projection_service().published_integration_replay_is_current(
        repo_root, manifest, manifest_path, plan, expected_source_lease,
    )


def unpublished_local_integration_control_tip(repo_root, manifest, manifest_path=None):
    return _projection_service().unpublished_local_integration_control_tip(
        repo_root, manifest, manifest_path,
    )


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


def ensure_non_in_place_target_clean(repo_root, branch, worktree, allowed_paths=None):
    if worktree is None:
        return
    path = Path(worktree).resolve()
    if worktree_matches_branch(repo_root, branch, path):
        ensure_clean_worktree(path, allowed_paths=allowed_paths)
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
        existing_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
        if existing_manifest and existing_manifest.get('version') == MANIFEST_VERSION_CHANNELS:
            raise SyncwheelError(
                'init refuses to replace an existing manifest version 3; '
                'close or migrate channel state through its governed commands'
            )
        if not args.force:
            raise SyncwheelError(f'manifest already exists: {manifest_path}')
    shared_manifest = not args.personal and not args.manifest
    if shared_manifest:
        _manifest_storage.require_manifest_transaction_current(manifest_path)
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
            acknowledge_in_place_manifest_replay(
                repo_root, manifest_path, ref_tip(repo_root, integration_branch)
            )
    _manifest_storage.save_manifest(manifest_path, manifest)
    if not _manifest_storage.is_external_manifest_path(repo_root, manifest_path):
        ensure_syncwheel_metadata_excluded(
            repo_root, manifest.get('syncwheel_tracking'), syncwheel_worktree_root(manifest),
        )
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


def coordination_provenance_supersede_plan(
    repo_root, manifest, manifest_path, reconciliation, paths, reason,
):
    """Prove that an authenticated integration replay replaced exact derived bytes."""
    if manifest.get('version') != MANIFEST_VERSION_CHANNELS or not coordination_is_active(manifest):
        raise SyncwheelError('provenance supersede requires active-active manifest version 3')
    if not reason or not reason.strip():
        raise SyncwheelError('provenance supersede requires --reason')
    if not paths or len(paths) != len(set(paths)):
        raise SyncwheelError('provenance supersede requires unique --path values')
    observed = observe_published_integration_tip(repo_root, manifest)
    if not observed or observed['status'] != 'current':
        raise SyncwheelError('provenance supersede requires current published coordination state')
    reconciliation = commit_full_sha(repo_root, reconciliation)
    proof = integration_reconciliation_proof(
        repo_root, reconciliation, manifest_path, observed
    )
    if not proof:
        raise SyncwheelError('provenance supersede requires a valid Syncwheel reconciliation proof')
    integration_tip = ref_tip(repo_root, manifest['integration']['branch'])
    if not integration_tip or not branch_contains(repo_root, integration_tip, reconciliation):
        raise SyncwheelError('integration does not contain the proved reconciliation commit')
    first_parent, replay_parent = proof['parents'][:2]
    records, diverged = derived_provenance_snapshot(
        repo_root, manifest, observed['state']
    )
    if diverged:
        raise SyncwheelError('clone-local derived provenance diverges from published state')
    published_records = {
        tuple(item['paths']): item
        for item in shared_derived_provenance_records(
            repo_root, manifest, observed['state']
        )
    }
    selected = []
    requested = set(paths)
    for record in records:
        record_paths = set(record['paths'])
        if not record_paths & requested:
            continue
        if not record_paths <= requested:
            raise SyncwheelError('provenance supersede must select every path in a record')
        if published_records.get(tuple(record['paths'])) != record:
            raise SyncwheelError('selected provenance is not the published record')
        if not is_provenance_bound_derived_projection_commit(
            repo_root, record['commit'], [record]
        ) or not branch_contains(repo_root, first_parent, record['commit']):
            raise SyncwheelError('selected provenance is not authenticated in the old history')
        entries = []
        for path in record['paths']:
            old = tree_path_entry(repo_root, record['commit'], path)
            prior = tree_path_entry(repo_root, first_parent, path)
            replay = tree_path_entry(repo_root, replay_parent, path)
            merged = tree_path_entry(repo_root, reconciliation, path)
            current = tree_path_entry(repo_root, integration_tip, path)
            if old is None or old != prior or replay is None or (
                replay == old or replay != merged or replay != current
            ):
                raise SyncwheelError(
                    f'provenance supersede lacks exact replay replacement for {path}: '
                    f'old={old}, prior={prior}, replay={replay}, merged={merged}, current={current}'
                )
            entries.append({'path': path, 'old': old, 'replay': replay})
        selected.append({'record': record, 'entries': entries})
        requested -= record_paths
    if requested:
        raise SyncwheelError(
            'no current derived provenance for selected path(s): '
            + ', '.join(sorted(requested))
        )
    store = load_derived_provenance_store(repo_root)
    plan = {
        'operation': 'coordination-provenance-supersede',
        'reason': reason.strip(),
        'manifestDigest': manifest_digest(manifest),
        'stateTip': observed['state_tip'],
        'publishedIntegrationTip': observed['published_tip'],
        'integrationTip': integration_tip,
        'reconciliation': reconciliation,
        'parents': [first_parent, replay_parent],
        'paths': sorted(paths),
        'records': selected,
        'localStoreDigest': canonical_json_digest(store),
        'remoteRefs': observed['remote_refs'],
    }
    plan['planDigest'] = canonical_json_digest(plan)
    return plan


def command_coordination_provenance_supersede(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    if not args.apply:
        if args.plan_file:
            raise SyncwheelError('--plan-file is only valid with --apply')
        if not args.reconciliation or not args.path:
            raise SyncwheelError('planning requires --reconciliation and --path')
        plan = coordination_provenance_supersede_plan(
            repo_root, manifest, manifest_path,
            args.reconciliation, args.path, args.reason,
        )
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if not args.plan_file:
        raise SyncwheelError('provenance supersede --apply requires --plan-file')
    try:
        reviewed = json.loads(Path(args.plan_file).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncwheelError(f'cannot read provenance supersede plan: {exc}') from exc
    if not isinstance(reviewed, dict) or reviewed.get('operation') != (
        'coordination-provenance-supersede'
    ):
        raise SyncwheelError('invalid provenance supersede plan')
    if args.reconciliation and commit_full_sha(repo_root, args.reconciliation) != reviewed.get('reconciliation'):
        raise SyncwheelError('--reconciliation differs from reviewed plan')
    if args.path and sorted(args.path) != reviewed.get('paths'):
        raise SyncwheelError('--path differs from reviewed plan')
    if args.reason and args.reason.strip() != reviewed.get('reason'):
        raise SyncwheelError('--reason differs from reviewed plan')
    ensure_in_place_target(repo_root, manifest['integration']['branch'], manifest, manifest_path=manifest_path)
    with coordination_publication_lock(repo_root):
        with derived_provenance_store_lock(repo_root):
            current = coordination_provenance_supersede_plan(
                repo_root, manifest, manifest_path,
                reviewed['reconciliation'], reviewed['paths'], reviewed['reason'],
            )
            if current != reviewed:
                raise SyncwheelError(
                    'provenance supersede evidence or exact state/ref lease changed; '
                    'review a new plan'
                )
            shared = shared_derived_provenance_records(
                repo_root, manifest,
                read_remote_coordination_state(
                    repo_root, coordination_config(manifest),
                    local_manifest_version=manifest['version'],
                )['state'],
            )
            store = load_derived_provenance_store(repo_root)
            for item in reviewed['records']:
                record = item['record']
                store = projected_derived_provenance_store(
                    store, shared, record['paths'], None,
                    expected_commit=record['commit'],
                )
            # A second state read closes the remote race before the local CAS.
            state = read_remote_coordination_state(
                repo_root, coordination_config(manifest),
                local_manifest_version=manifest['version'],
            )
            if state['tip'] != reviewed['stateTip']:
                raise SyncwheelError('provenance supersede state lease changed before save')
            save_derived_provenance_store(repo_root, store)
            append_ledger_event(
                repo_root, 'derived_provenance_superseded', reviewed, manifest_path,
            )
    print(json.dumps({
        'status': 'local-provenance-superseded',
        'planDigest': reviewed['planDigest'],
        'stateTip': reviewed['stateTip'],
        'paths': reviewed['paths'],
        'publication': 'pending syncwheel int push',
    }, indent=2, sort_keys=True))
    return 0


def begin_coordination_claims_backfill(
    repo_root, manifest, manifest_path, state, claimed_refs, expected_state_tip
):
    """Record the terminalizable intent a claims backfill pushes under."""
    identity = {
        'coordination_id': coordination_config(manifest)['id'],
        'scope': 'claims-backfill',
        'projection_status': state.get('projection_status'),
        'manifest_digest': coordination_state_operation_manifest_digest(state),
        'changed_refs': {},
        'claimed_refs': list(claimed_refs),
        'tombstone': None,
        'rename': None,
        'state_transition': None,
    }
    fingerprint = canonical_json_digest(identity)
    existing = pending_coordination_publication_after_resolution(
        repo_root, manifest, manifest_path, fingerprint
    )
    if existing:
        return {**existing, 'retry': True}
    payload = {
        **identity,
        'fingerprint': fingerprint,
        'operation_token': str(uuid.uuid4()),
        'expected_coordination_state_tip': expected_state_tip,
        'owner': coordination_publication_owner(repo_root),
    }
    append_ledger_event(
        repo_root, 'coordination_publish_intent', payload, manifest_path
    )
    return {**payload, 'retry': False}


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
    if not coordination_remote_is_reachable(repo_root, config['remote']):
        raise coordinated_publish_remote_failure(
            'coordination claims backfill --apply --reason <reason>'
            if args.apply else 'coordination claims backfill'
        )
    if args.apply:
        resolve_pending_coordination_publications(repo_root, manifest, manifest_path)
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
            create[source_ref] = None
            claims[source_ref] = None
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
    if not create and dict(sorted(claims.items())) == dict(
        sorted((state.get('claims') or {}).items())
    ):
        print(json.dumps({
            'coordination_id': config['id'], 'state_tip': published['tip'],
            'created_claims': [], 'unclaimed_owned_refs': [],
        }, indent=2, sort_keys=True))
        return 0
    with coordination_publication_lock(repo_root):
        operation = begin_coordination_claims_backfill(
            repo_root, manifest, manifest_path, state, sorted(create), published['tip']
        )
        operation_token = operation['operation_token']
        for source_ref in sorted(create):
            create[source_ref] = create_coordination_claim_commit(
                repo_root, source_ref, config['id'], operation_token, None,
                publication_scope='claims-backfill',
            )
            claims[source_ref] = create[source_ref]
        child = copy.deepcopy(state)
        child['publication_id'] = str(uuid.uuid4())
        child['parent_state'] = published['tip']
        child['created_at'] = iso_utc_now()
        child['syncwheel_version'] = VERSION
        child['installation_id'] = installation_id(create=True)
        child['claims'] = dict(sorted(claims.items()))
        child['changed_refs'] = {}
        child['publication_scope'] = 'claims-backfill'
        child['operation_token'] = operation_token
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
            manifest=manifest,
        )
        if result.returncode != 0:
            raise SyncwheelError(
                'claims backfill lost its create-only CAS; run syncwheel handoff and retry'
            )
        append_ledger_event(repo_root, 'coordination_claims_backfilled', {
            'coordination_id': config['id'], 'state_tip': state_commit,
            'refs': sorted(create), 'reason': args.reason.strip(),
            'operation_token': operation_token,
        }, manifest_path)
        complete_coordination_publication(
            repo_root, manifest_path, operation,
            {'state_tip': state_commit, 'status': 'published'},
        )
    print(json.dumps({
        'coordination_id': config['id'], 'state_tip': state_commit,
        'created_claims': sorted(create), 'unclaimed_owned_refs': [],
    }, indent=2, sort_keys=True))
    return 0


def command_coordination_repair(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
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
    result = apply_coordination_repair_plan(repo_root, manifest, plan, manifest_path=manifest_path)
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
        if not args.stack or not args.known_base_state or not args.known_base_manifest_digest:
            raise SyncwheelError(
                'coordination compose planning requires --stack, --known-base-state, '
                'and --known-base-manifest-digest'
            )
        plan, _, _ = coordination_compose_stack_plan(
            repo_root,
            manifest,
            args.stack,
            args.known_base_state,
            args.known_base_manifest_digest,
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
        (args.known_base_manifest_digest, 'knownBaseManifestDigest'),
    ):
        if option and option != plan.get(key):
            raise SyncwheelError('coordination compose arguments do not match the reviewed plan')
    result = apply_coordination_compose_stack_plan(
        repo_root, manifest, manifest_path, plan
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_worktree_list(args):
    """List governed lanes and their health without changing anything."""
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = load_diagnostic_manifest(repo_root, args)
    if manifest is None:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    if manifest.get('repository_mode') == 'journal':
        raise SyncwheelError('worktree is forbidden for repository_mode="journal"; use journal commands')
    diagnostics = governed_worktree_diagnostics(repo_root, manifest, manifest_path)
    lanes = [
        lane for lane in diagnostics['lanes']
        if not args.problems or lane.get('code')
    ]
    desks = _desk_lifecycle(repo_root).desks()
    if args.json:
        print(json.dumps({
            'registryPath': diagnostics['registry_path'],
            'lanes': lanes,
            'desks': desks if not args.problems else [],
            'counts': governed_worktree_problem_counts(diagnostics['lanes']),
        }, indent=2, sort_keys=True))
        return 0
    for desk in desks if not args.problems else ():
        print(f"{desk['stack']}  kind=replay-desk  branch={desk['branch']}")
        print(f"    path: {desk['path']}")
    if not lanes and (not desks or args.problems):
        print('no governed worktree lanes' + (' need attention' if args.problems else ''))
    for lane in lanes:
        label = lane.get('id') or lane.get('branch') or lane.get('path')
        print(
            f"{label}  state={lane.get('state')}  health={lane.get('code') or 'ok'}  "
            f"target={lane.get('target') or '-'}  branch={lane.get('branch') or '-'}"
        )
        print(f"    path: {lane.get('path')}")
        if lane.get('code') and lane.get('remedy'):
            print(f"    remedy: {lane['remedy']}")
    return 0


def governed_worktree_problem_counts(lanes):
    counts = {}
    for lane in lanes:
        if lane.get('code'):
            counts[lane['code']] = counts.get(lane['code'], 0) + 1
    return dict(sorted(counts.items()))


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
    stack = require_stack(manifest, args.into) if args.into else None
    base_ref = getattr(args, 'base', None)
    if base_ref and not stack:
        raise SyncwheelError('worktree open --base requires --into <stack>')
    branch = f'syncwheel/lane/{lane_id}'
    root = governed_worktree_root(repo_root, manifest)
    path = root / branch.replace('/', '-').replace('\\', '-')
    if not path_is_relative_to(path, root):
        raise SyncwheelError('configured worktree path escapes syncwheel_worktree_root')
    stack_materialization = None
    with governed_worktree_registry_lock(repo_root):
        registry, registry_path = load_governed_worktree_registry(repo_root)
        recovered = copy.deepcopy(registry)
        recover_governed_worktree_registry_from_ledger(
            repo_root,
            recovered,
            lambda: None,
            manifest_path,
        )
        if recovered != registry:
            raise SyncwheelError(
                'governed worktree registry has pending ledger recovery; '
                'resolve shared state before opening another lane'
            )
        persist = governed_worktree_registry_cas_persister(repo_root, registry)
        warnings = governed_worktree_warning_lines(repo_root, manifest)
        active = [
            item for item in registry['lanes']
            if item['state'] in {'active', 'captured_pending_cleanup'}
        ]
        capacity = governed_worktree_capacity(manifest)
        if len(active) >= capacity['limit']:
            message = (
                f"governed worktree capacity reached ({capacity['limit']}); "
                'capture or queue an existing lane before opening another'
                + format_remedy_suffix(governed_lane_queue_commands(manifest, active))
            )
            if capacity['enforcement'] == 'error':
                raise SyncwheelError(message)
            warnings.append(message)
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
        base = ref_tip(repo_root, base_ref or 'HEAD')
        if not base:
            raise SyncwheelError('cannot open a governed worktree: base commit is missing')
        if base_ref:
            stack_branch_ref = f"refs/heads/{stack['branch']}"
            stack_tip = ref_tip(repo_root, stack_branch_ref)
            commits = stack.get('commits') or []
            can_materialize = (
                stack_tip is None
                and not commits
                and stack.get('state', 'published') == 'published'
                and git(repo_root, 'symbolic-ref', '--quiet', stack_branch_ref, check=False).returncode == 1
            )
            projected_tip = None
            if stack_tip is not None or can_materialize:
                adopted = validate_stack_adoption(stack)
                projected_tip = (adopted['tip'] if adopted else
                                 deterministic_stack_replay_tip(repo_root, stack['base'], commits))
                if projected_tip:
                    projected_tip = commit_full_sha(repo_root, projected_tip)
            if base != projected_tip or (base != stack_tip and not can_materialize):
                raise SyncwheelError(
                    f'worktree open --base must resolve to the current exact projection '
                    f'of stack {args.into!r}; rebuild or align that stack before opening a lane'
                )
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
            admin = governed_worktree_admin_dir_for_path(repo_root, path)
            if not admin:
                raise SyncwheelError('cannot prove the new governed worktree Git administration')
            lane['admin_dir'] = str(admin)
            if base_ref and can_materialize:
                created = git(
                    repo_root, 'update-ref', '--no-deref', stack_branch_ref, base,
                    '0' * len(base), check=False,
                )
                if created.returncode != 0:
                    raise SyncwheelError(
                        f"cannot materialize missing stack branch {stack_branch_ref}: "
                        f"{created.stderr.strip()}"
                    )
                stack_materialization = {'branch': stack['branch'], 'tip': base}
            registry['lanes'].append(lane)
            persist()
        except BaseException as error:
            # A durability failure can occur after the registry replacement.
            current_registry, _ = load_governed_worktree_registry(repo_root)
            if lane in current_registry['lanes']:
                raise SyncwheelError(
                    f'governed worktree lane is visible after a registry write failure: '
                    f'{lane_id}; inspect it before retrying'
                ) from error
            removed = git(repo_root, 'worktree', 'remove', str(path), check=False)
            if removed.returncode == 0:
                removed = git(
                    repo_root, 'update-ref', '--no-deref', '-d', f'refs/heads/{branch}',
                    base, check=False,
                )
            if stack_materialization:
                rolled_back = git(
                    repo_root, 'update-ref', '--no-deref', '-d', stack_branch_ref, base,
                    check=False,
                )
                if rolled_back.returncode != 0:
                    raise SyncwheelError(
                        f'{error}; could not roll back {stack_branch_ref}: '
                        f'{rolled_back.stderr.strip()}'
                    ) from error
            if removed.returncode != 0:
                raise SyncwheelError(
                    f'{error}; could not fully roll back lane {lane_id}: '
                    f'{removed.stderr.strip()}'
                ) from error
            raise
    output = {'lane': lane, 'registry_path': str(registry_path), 'warnings': warnings}
    if stack_materialization:
        output['stack_materialization'] = stack_materialization
    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        mode = 'full' if lane['full'] else 'light'
        print(f"opened {mode} governed worktree {lane['id']}: {lane['path']}")
        print(f"  branch: {lane['branch']}")
        print(f"  lease: {lane['lease_expires_at']}")
        if lane['target']:
            print(f"  target stack: {lane['target']}")
        if stack_materialization:
            print(f"  materialized stack branch: {stack_materialization['branch']} at {base}")
        for warning in warnings:
            print(f'  warning: {warning}')
    return 0


def command_worktree_retire_copy(args):
    repo_root, manifest_path = resolve_command_manifest_selection(args)
    manifest_path = Path(os.path.abspath(manifest_path))
    lane_id = safe_ref_segment(args.lane)
    reason = args.reason.strip()
    if not reason:
        raise SyncwheelError('worktree retire-copy requires a nonempty --reason')
    if args.apply and not args.confirm_writers_stopped:
        raise SyncwheelError('worktree retire-copy --apply requires --confirm-writers-stopped')
    if not args.apply and (args.plan_digest or args.confirm_writers_stopped):
        raise SyncwheelError('--plan-digest and --confirm-writers-stopped require --apply')
    common = git_common_dir(repo_root)
    primary_root = Path(get_worktrees(repo_root)[0]['path']).resolve()
    selection = ['--repo', str(primary_root), '--manifest', str(manifest_path)]
    worktrees = _governed_worktrees(repo_root)
    git_env = {'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1'}
    def identify():
        if ('GIT_REPLACE_REF_BASE' in os.environ
                or git(repo_root, 'for-each-ref', '--format=%(refname)', 'refs/replace').stdout.strip()):
            raise SyncwheelError('retire-copy refuses Git replacement refs')
        if 'GIT_GRAFT_FILE' in os.environ or os.path.lexists(common / 'info' / 'grafts'):
            raise SyncwheelError('retire-copy refuses Git grafts')
        if manifest_path.resolve() != manifest_path:
            raise SyncwheelError('retire-copy requires an exact manifest path without redirects')
        manifest = read_primary_authority_manifest(manifest_path)
        if not manifest or manifest.get('repository_mode') != 'delivery':
            raise SyncwheelError('worktree retire-copy requires a delivery manifest')
        registry, _ = worktrees.load_governed_worktree_registry()
        matches = [lane for lane in registry['lanes'] if lane['id'] == lane_id]
        if len(matches) != 1:
            raise SyncwheelError(f'unknown or ambiguous governed worktree lane: {lane_id}')
        lane = matches[0]
        if (
            lane['state'] != 'active' or not lane.get('generation_token')
            or not lane.get('target') or lane.get('pending_reason')
            or any(key.startswith('cleanup_') for key in lane)
            or parse_coordination_timestamp(lane.get('lease_expires_at', '')) is None
        ):
            raise SyncwheelError('retire-copy requires an active lane with an exact generation, lease, and target stack')
        if lane.get('owner') != governed_worktree_owner() or lane['owner'].startswith('unknown@'):
            raise SyncwheelError('retire-copy requires the current, known lane owner')
        stack = require_stack(manifest, lane['target'])
        path = Path(lane['path'])
        root = governed_worktree_root(repo_root, manifest)
        if (
            not path.is_absolute() or path.resolve() != path or not path.is_dir()
            or path == root or not path.is_relative_to(root) or path == primary_root
            or common.is_relative_to(path)
        ):
            raise SyncwheelError('retire-copy requires the original governed linked worktree')
        admin = worktrees.governed_worktree_admin_dir_for_path(path)
        worktree = worktrees.governed_worktree_record_for_path(path)
        if (not admin or str(admin) != lane.get('admin_dir') or not worktree
                or worktree.get('branch') != lane['branch'] or worktree.get('prunable')
                or worktree.get('locked') == governed_worktree_cleanup_lock_reason(lane)
                or not worktrees.governed_worktree_registration_matches(
                    lane, path, admin, worktree.get('locked')
                )
                or admin != Path(git(path, 'rev-parse', '--absolute-git-dir').stdout.strip())
                or os.path.lexists(admin / 'locked') and (admin / 'locked').is_symlink()):
            raise SyncwheelError('retire-copy could not verify the original Git worktree registration')
        dotgit, _ = retire_copy_observed_file(path / '.git')
        if not dotgit or not stat.S_ISREG(dotgit['stat'][2]):
            raise SyncwheelError('retire-copy requires a regular linked-worktree .git file')
        if any(
            stack['id'] in (coordination.get('locks') or {}) or local_lease_is_active(coordination)
            for _, coordination in (coordination_profile(checkout)
                                    for checkout in {repo_root, primary_root, path})
        ) or os.path.lexists(coordination_publication_lock_path(repo_root)):
            raise SyncwheelError('retire-copy refuses an active worktree lock or coordination lease')
        if Path.cwd().resolve().is_relative_to(path):
            raise SyncwheelError('run retire-copy from outside the lane after stopping its writers')
        require_no_visible_lane_users(path)
        source_ref = f"refs/heads/{lane['branch']}"
        source = ref_tip(repo_root, source_ref)
        if (not source or source != worktree.get('HEAD') or source != ref_tip(path, 'HEAD')
                or git(repo_root, 'symbolic-ref', '--quiet', source_ref, check=False).returncode != 1):
            raise SyncwheelError('retire-copy requires the exact direct lane source ref and HEAD')
        stack_tip = ref_tip(repo_root, f"refs/heads/{stack['branch']}")
        if not stack_tip:
            raise SyncwheelError('retire-copy requires a materialized target stack')
        history = declared_stack_history(repo_root, stack)
        requested = (
            git(repo_root, 'rev-parse', '--verify', '--end-of-options',
                f'{args.commit}^{{commit}}', env=git_env).stdout.strip() if args.commit else None
        )
        if not history or requested and requested not in history:
            raise SyncwheelError('retire-copy comparison must be a commit declared by the lane target stack')
        return {
            'lane': lane, 'path': str(path), 'root': str(root),
            'rootIdentity': retire_copy_stat_key(root.lstat()),
            'checkoutIdentity': retire_copy_stat_key(path.lstat()),
            'admin': str(admin), 'adminIdentity': retire_copy_stat_key(admin.lstat()),
            'dotGit': dotgit, 'head': source, 'sourceRef': source_ref,
            'retentionLock': worktree.get('locked'), 'manifest': str(manifest_path),
            'manifestDigest': manifest_digest(manifest), 'stack': stack['id'], 'stackTip': stack_tip,
            'declaredHistory': history, 'requestedCommit': requested,
        }

    operation = RetireCopy(env=managed_process_env(authorize=False), common=common,
                           lane_id=lane_id, reason=reason, requested_commit=args.commit,
                           selection=selection, identity=identify,
                           lock_metadata=governed_worktree_lock_metadata,
                           stale_lock_reason=governed_worktree_stale_lock_reason)
    try:
        if args.apply:
            require_private_tracking_path(common, governed_worktree_lock_path(repo_root))
            with worktrees.governed_worktree_registry_lock():
                result = operation.apply(args.plan_digest)
        else:
            plan, token = operation.plan()
            result = operation.output(plan, token)
    except OSError as exc:
        raise SyncwheelError(f'retire-copy could not complete: {exc}') from exc
    print(json.dumps(result, indent=2, sort_keys=True))
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
            'current_directory',
            'lane_in_use',
            'reaping',
            'worktree_remove_failed',
            'branch_delete_failed',
            'branch_advanced',
            'ledger_pending',
            'recovery_ref_moved',
            'archive_push_failed',
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
        if lane.get('archive_ref'):
            print(f"  saved on {lane.get('archive_remote')}: {lane['archive_ref']}")
        elif lane.get('archive_skipped'):
            print(f"  not saved on a remote: {lane['archive_skipped']}")
    return 0


def governed_worktree_commit_set(repo_root, lane):
    return _governed_worktrees(repo_root).governed_worktree_commit_set(
        lane,
    )


def retained_captured_worktree_snapshot(repo_root, manifest, stack_id, *, capture=False, active_only=False, candidate_remote=None):
    """Prove that retained copies cannot contribute unpublished working-tree state."""
    stack = require_stack(manifest, stack_id)
    registry, _ = load_governed_worktree_registry(repo_root)
    owned = set(declared_stack_history(repo_root, stack))
    target_tip = ref_tip(repo_root, stack['branch'])
    root = governed_worktree_root(repo_root, manifest).resolve(strict=False)
    snapshots = []
    candidate = None
    if candidate_remote is not None:
        adopted = validate_stack_adoption(stack)
        if not adopted or target_tip != adopted['tip']:
            raise SyncwheelError('equivalent candidate delivery requires an unchanged adopted stack tip')
        ref = f"refs/heads/{stack['branch']}"
        if git(repo_root, 'symbolic-ref', '-q', ref, check=False).returncode == 0:
            raise SyncwheelError('equivalent candidate delivery requires a direct source ref')
        remote_tip = remote_ref_tips(repo_root, candidate_remote, [ref]).get(ref)
        if not remote_tip or not commit_exists(repo_root, remote_tip):
            raise SyncwheelError('equivalent candidate delivery requires an existing remote head available locally; fetch and inspect it first')
        if not branch_contains(repo_root, target_tip, remote_tip):
            raise SyncwheelError('equivalent candidate must fast-forward the published remote head')
        candidate = {'remote': candidate_remote, 'remote_ref': ref,
                     'remote_tip': remote_tip, 'candidate_tip': target_tip,
                     'tree': ref_tree(repo_root, target_tip), 'equivalence': 'exact-tree'}
    for lane in registry['lanes']:
        if lane.get('target') != stack_id or lane['state'] == 'reaped':
            continue
        if active_only and lane.get('pending_reason') == 'archive_push_failed':
            # The native recovery ref preserves this lane. A failed remote
            # archive has never blocked its independent stack publication.
            continue

        def refuse(reason):
            raise SyncwheelError(f"cannot retain captured worktree {lane['id']!r}: {reason}")

        active = lane['state'] == 'active'
        if not lane.get('generation_token') or (not active and (
            active_only or lane['state'] != 'captured_pending_cleanup'
        )):
            refuse('an active lane or completed native capture is required')
        if not active and parse_coordination_timestamp(lane.get('captured_at', '')) is None:
            refuse('native capture evidence is missing')
        if lane.get('pending_reason') or any(key.startswith('cleanup_') for key in lane):
            refuse('an interrupted cleanup must be recovered first')
        path = Path(lane['path'])
        if not path.is_absolute() or path.resolve(strict=False) != path or not path.is_dir():
            refuse('the original worktree path is missing or redirected')
        if not path_is_relative_to(path, root):
            refuse('the worktree is outside the governed root')
        worktree = find_worktree_record_for_branch(repo_root, lane['branch'])
        if not worktree or Path(worktree['path']).resolve(strict=False) != path:
            refuse('the Git worktree registration does not match')
        lock = worktree.get('locked')
        if (not active or candidate) and (not lock or lock == governed_worktree_cleanup_lock_reason(lane)):
            refuse('an explicit Git worktree retention lock is required')
        status = git(path, 'status', '--porcelain=v1', '--untracked-files=all',
                     '--ignore-submodules=none', check=False)
        if status.returncode or status.stdout.strip():
            refuse('tracked or untracked source changes remain; if already declared, preview: '
                   + retire_copy_remedy(lane['id']))
        tip = ref_tip(repo_root, lane['branch'])
        if candidate and (
            git(repo_root, 'symbolic-ref', '-q', f"refs/heads/{lane['branch']}", check=False).returncode == 0
            or git(path, 'symbolic-ref', '-q', 'HEAD', check=False).stdout.strip()
            != f"refs/heads/{lane['branch']}"
        ):
            refuse('the preserved lane must retain its direct branch and attached HEAD')
        if not target_tip or not tip or ref_tip(path, 'HEAD') != tip or (not capture and not candidate and tip != target_tip):
            refuse('the captured HEAD and stack publication HEAD must match exactly')
        commits = governed_worktree_commit_set(repo_root, lane)
        unowned = [commit for commit in commits if commit not in owned
                   and not branch_contains(repo_root, stack['base'], commit)]
        if candidate and ref_tree(repo_root, tip) != candidate['tree']:
            refuse('candidate tree differs from the preserved lane; exact tree equivalence is required')
        if not candidate and (unowned or (tip not in owned and tip != commit_full_sha(repo_root, stack['base']))):
            refuse('the captured commits are not fully owned by the selected stack')
        if capture:
            base = commit_full_sha(repo_root, stack['base'])
            rows = [line.split() for line in git(
                repo_root, 'rev-list', '--reverse', '--parents', f'{base}..{tip}'
            ).stdout.splitlines()]
            declared = list(dict.fromkeys(commit_full_sha(repo_root, item) for item in stack['commits']))
            if [row[0] for row in rows] != declared:
                refuse('recapture requires the complete declared history on the stack base')
            previous = base
            for row in rows:
                if row[1:] != [previous]:
                    refuse('recapture requires contiguous single-parent history')
                previous = row[0]
        snapshots.append({
            'lane': dict(lane), 'tip': tip, 'target_tip': target_tip, 'lock': lock,
            'owned_commits': sorted(owned),
            **({'candidate_delivery': {**candidate, 'preserved_ref': f"refs/heads/{lane['branch']}",
                                       'preserved_tip': tip}} if candidate else {}),
        })
    if candidate and not snapshots:
        raise SyncwheelError('equivalent candidate delivery requires a preserved governed lane')
    return sorted(snapshots, key=lambda item: item['lane']['id'])


def retained_capture_manifest(repo_root, manifest, args):
    if args.func != command_stack_capture_integration:
        return manifest
    proposed = copy.deepcopy(manifest)
    stack = require_stack(proposed, args.stack)
    commits = [commit_full_sha(repo_root, item) for item in stack['commits']]
    for spec in args.specs:
        commits.extend(commit_list_for_spec(repo_root, spec))
    stack['commits'] = list(dict.fromkeys(commits))
    return proposed


def verify_retained_captured_worktrees(repo_root, manifest, args, *, after_capture=False):
    active_only = getattr(args, 'retain_captured_worktrees', None) is None
    if active_only and not hasattr(args, '_active_publication_snapshot'):
        return []
    expected = getattr(args, '_active_publication_snapshot' if active_only
                       else '_retained_captured_worktree_snapshot', None)
    capture = args.func == command_stack_capture_integration and not after_capture
    proposed = retained_capture_manifest(repo_root, manifest, args) if capture else manifest
    observed = retained_captured_worktree_snapshot(
        repo_root, proposed, args.stack, capture=capture, active_only=active_only,
        candidate_remote=getattr(args, '_preserved_candidate_remote', None),
    )
    if after_capture and expected is not None:
        expected = [{**item, 'target_tip': item['tip']} for item in expected]
    if expected is None or observed != expected:
        raise SyncwheelError('retained captured worktrees changed after preflight; refusing publication')
    return observed


def capture_governed_worktrees_for_stack(repo_root, manifest, stack_id, manifest_path=None):
    return _governed_worktrees(repo_root).capture_governed_worktrees_for_stack(
        manifest, stack_id, manifest_path,
    )


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
    manifest, _manifest_path = _manifest_storage.load_manifest(
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


def load_diagnostic_manifest(repo_root, args):
    path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    if getattr(args, 'fetch', False):
        return _manifest_storage.load_manifest(repo_root, path)
    shared = primary_authority_manifest_path(repo_root)
    shared_manifest = read_primary_authority_manifest(shared)
    if Path(os.path.abspath(path)) == shared or path.resolve() == shared:
        return shared_manifest, path
    return _manifest_storage.load_manifest(repo_root, path)


def command_status(args):
    repo_root = resolve_repo_root(args.repo)
    if args.fetch:
        git(repo_root, 'fetch', '--all', '--prune', '--quiet', check=False)
    manifest, manifest_path = load_diagnostic_manifest(repo_root, args)
    snapshot = collect_repo_snapshot(repo_root, manifest)
    output = {'snapshot': snapshot, 'manifest_path': str(manifest_path), 'manifest_present': manifest is not None}
    if manifest:
        validation = validate_manifest(repo_root, manifest, manifest_path)
        output['validation'] = validation
        output['plan'] = build_plan(repo_root, manifest, validation)
        output['governed_worktrees'] = governed_worktree_diagnostics(repo_root, manifest, manifest_path)
        if _READONLY_DIAGNOSTICS.get():
            emit_governed_worktree_warnings(
                repo_root, manifest, args.json, manifest_path, lanes=output['governed_worktrees']['lanes'],
            )
        output['authority'], output['authority_provenance'] = resolve_repository_authority(
            repo_root, manifest, manifest_path,
        )
    if args.json:
        print(json.dumps(output, indent=2))
        primary = output['validation']['details'].get('primary_checkout') if manifest else None
        return 1 if primary and not primary['compliant'] else 0
    print(f"repo: {snapshot['repo_root']}")
    print(f"current_branch: {snapshot['current_branch']}")
    print(f"canonical_remote_head: {snapshot['canonical_remote_head'] or 'unknown'}")
    print(f"manifest: {manifest_path if manifest else 'missing'}")
    if manifest:
        print(f"authority: {format_authority_policy(output['authority'])}")
        print(f"authority_source: {output['authority_provenance']['source']}")
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
    primary = output['validation']['details'].get('primary_checkout') if manifest else None
    return 1 if primary and not primary['compliant'] else 0


def command_validate(args):
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    manifest, manifest_path = _manifest_storage.load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    validation = validate_manifest(repo_root, manifest, manifest_path)
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
    manifest, manifest_path = _manifest_storage.load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    validation = validate_manifest(repo_root, manifest, manifest_path)
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
    manifest, manifest_path = load_diagnostic_manifest(repo_root, args)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    snapshot = collect_repo_snapshot(repo_root, manifest)
    validation = validate_manifest(repo_root, manifest, manifest_path)
    journal = manifest.get('repository_mode') == 'journal'
    plan = build_plan(repo_root, manifest, validation)
    diagnostics = [] if journal else integration_commit_diagnostics(repo_root, manifest, validation)
    governed = governed_worktree_diagnostics(repo_root, manifest, manifest_path)
    if _READONLY_DIAGNOSTICS.get():
        emit_governed_worktree_warnings(repo_root, manifest, args.json, manifest_path, lanes=governed['lanes'])
    readiness_blockers = []
    if validation['errors']:
        readiness_blockers.append('validation_errors')
    if validation['warnings']:
        readiness_blockers.append('validation_warnings')
    if journal and snapshot['working_tree_dirty']:
        readiness_blockers.append('dirty_worktree')
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
        if journal:
            output['repository_mode'] = 'journal'
        print(json.dumps(output, indent=2))
        return 1 if validation['errors'] or (args.strict and not readiness['ready']) else 0
    print(f"repo: {snapshot['repo_root']}")
    print(f"branch: {snapshot['current_branch']}")
    print(f"manifest: {manifest_path}")
    label = 'journal validation' if journal else 'validation'
    if validation['errors'] or validation['warnings']:
        print(f'\n{label}:')
        for line in validation['errors']:
            print(f'  - ERROR: {line}')
        for line in validation['warnings']:
            print(f'  - WARN: {line}')
    else:
        print(f'\n{label}: OK')
    if journal:
        if snapshot['working_tree_dirty']:
            print('\njournal worktree has uncommitted changes')
    else:
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


def channel_coordination_operation_token(repo_root, manifest_path, plan):
    """Mint or recover the coordination token of one channel operation.

    ``operationId`` is the caller's clone-local idempotency key and may be
    reused by another clone for another plan, so it can never be the token that
    proves a publication landed.
    """
    for event in channel_operation_events(
        repo_root, manifest_path, plan['operationId']
    ):
        recorded = (event.get('payload') or {}).get('coordinationOperationToken')
        if isinstance(recorded, str) and recorded:
            return recorded
    return str(uuid.uuid4())


def channel_operation_payload(plan, channel=None, mutation=None):
    plan_channel = plan.get('channel')
    plan_channel_id = (
        plan_channel.get('id') if isinstance(plan_channel, dict) else plan_channel
    )
    payload = {
        'operationId': plan['operationId'],
        'coordinationOperationToken': plan.get('coordinationOperationToken'),
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
        event for event in _manifest_storage.load_ledger_events(repo_root, manifest_path)
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
            for event in reversed(_manifest_storage.load_ledger_events(repo_root, manifest_path))
            if event.get('type') == 'channel_operation_prepared'
            and (event.get('payload') or {}).get('operationId') == plan['operationId']
        ),
        {},
    )
    started = next(
        (
            event.get('payload') or {}
            for event in _manifest_storage.load_ledger_events(repo_root, manifest_path)
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
        _manifest_storage.require_locked_manifest_observation(repo_root, manifest_path, plan)
        if locked_check:
            locked_check()
        record_channel_operation_prepared(repo_root, manifest_path, plan, channel, mutation)
        try:
            _manifest_storage.channel_mutation_checkpoint()
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'cancelled', channel,
                'cancelled before authoritative manifest mutation',
                evidence={'manifestDigest': plan['manifestDigestBefore']},
            )
            raise SyncwheelError('channel mutation cancelled before authoritative change') from exc
        _manifest_storage.require_manifest_transaction_current(manifest_path)
        try:
            _manifest_storage.save_manifest(manifest_path, manifest)
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'unknown', channel,
                'interrupted after authoritative manifest mutation began',
                evidence={'manifestDigest': manifest_digest(_manifest_storage.load_manifest(repo_root, manifest_path)[0])},
            )
            raise SyncwheelError(
                f'channel mutation outcome is unknown; operation {plan["operationId"]} '
                'requires operation reconcile'
            ) from exc
        except Exception as exc:
            if isinstance(exc, ManifestDurabilityError):
                observed_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
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
        event for event in _manifest_storage.load_ledger_events(repo_root, manifest_path)
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
    for event in _manifest_storage.load_ledger_events(repo_root, manifest_path):
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
        fresh_manifest = _manifest_storage.require_locked_manifest_observation(repo_root, manifest_path, plan)
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
            _manifest_storage.channel_mutation_checkpoint()
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'cancelled', channel,
                'cancelled before authoritative local ref mutation',
                evidence={'localRevision': plan['currentRevision']},
            )
            raise SyncwheelError('channel apply cancelled before authoritative change') from exc
        _manifest_storage.require_manifest_transaction_current(manifest_path)
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
        fresh_manifest = _manifest_storage.require_locked_manifest_observation(repo_root, manifest_path, plan)
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
        plan['coordinationOperationToken'] = channel_coordination_operation_token(
            repo_root, manifest_path, plan
        )
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
            _manifest_storage.channel_mutation_checkpoint()
        except (KeyboardInterrupt, SystemExit) as exc:
            record_channel_operation_receipt(
                repo_root, manifest_path, plan, 'cancelled', channel,
                'cancelled before authoritative remote mutation',
                evidence={'remoteRevision': plan['remoteRevision']},
            )
            raise SyncwheelError('channel publish cancelled before authoritative change') from exc
        _manifest_storage.require_manifest_transaction_current(manifest_path)
        try:
            if coordination_is_active(manifest):
                with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink):
                    result = coordinated_publish(
                        repo_root, manifest, manifest_path, {ref: current},
                        f"channel:{channel['id']}", 'channel-pinned-composition',
                        operation_token=plan['coordinationOperationToken'],
                    )
                coordination_state = result.get('state_tip')
            else:
                lease = f"--force-with-lease={ref}:{plan['remoteRevision'] or ''}"
                pushed = run_authorized_push(
                    repo_root,
                    ['git', 'push', '--porcelain', lease, channel['remote'], f'{current}:{ref}'],
                    channel['remote'], [ref], check=False, manifest=manifest,
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
        _manifest_storage.require_locked_manifest_observation(repo_root, manifest_path, output)
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
        output['coordinationOperationToken'] = channel_coordination_operation_token(
            repo_root, manifest_path, output
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
            _manifest_storage.channel_mutation_checkpoint()
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
        _manifest_storage.require_manifest_transaction_current(manifest_path)
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
                        operation_token=output['coordinationOperationToken'],
                    )
                coordination_state = result.get('state_tip')
            manifest_attempted = True
            _manifest_storage.save_manifest(manifest_path, manifest)
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
                observed_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
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
            observed_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
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
    if stack.get('adopted_history'):
        raise SyncwheelError('stack land does not yet support adopted stack histories')
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
    validation = validate_manifest(repo_root, manifest, manifest_path)
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
        event for event in _manifest_storage.load_ledger_events(repo_root, manifest_path)
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
        manifest = _manifest_storage.require_locked_manifest_observation(repo_root, manifest_path, {'manifestDigestBefore': plan['manifestDigest']})
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
                repo_root, command, current['delivery']['remote'], [current['delivery']['ref']],
                check=False, manifest=manifest,
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
    return _stack_lifecycle(repo_root).pending_stack_close_operation(
        manifest_path, stack_id,
    )




def remote_first_close_failure(repo_root, manifest, stack_id, exc, kind='remote-first', retry=None):
    return _stack_lifecycle(repo_root).remote_first_close_failure(
        manifest, stack_id, exc, kind, retry,
    )






def abandon_superseded_stack_close(repo_root, manifest_path, pending):
    return _stack_lifecycle(repo_root).abandon_superseded_stack_close(
        manifest_path, pending,
    )


def abandoned_close_reason(repo_root, manifest_path, stack_id):
    return _stack_lifecycle(repo_root).abandoned_close_reason(
        manifest_path, stack_id,
    )


def supersede_unpublished_stack_close(repo_root, manifest_path, pending):
    return _stack_lifecycle(repo_root).supersede_unpublished_stack_close(
        manifest_path, pending,
    )


def stack_recreated_after_close_intent(repo_root, manifest_path, pending):
    return _stack_lifecycle(repo_root).stack_recreated_after_close_intent(
        manifest_path, pending,
    )


def published_close_reached_remote(repo_root, config, observed, pending):
    return _stack_lifecycle(repo_root).published_close_reached_remote(
        config, observed, pending,
    )


def abandon_unlanded_stack_close(repo_root, config, manifest_path, pending, observed):
    return _stack_lifecycle(repo_root).abandon_unlanded_stack_close(
        config, manifest_path, pending, observed,
    )


def scoped_close_publication_manifest(manifest, snapshot, stack_id):
    return _stack_lifecycle(None).scoped_close_publication_manifest(
        manifest, snapshot, stack_id,
    )


def published_close_tombstone(repo_root, manifest, pending):
    return _stack_lifecycle(repo_root).published_close_tombstone(
        manifest, pending,
    )


def recover_pending_stack_close(repo_root, manifest, manifest_path, pending):
    return _stack_lifecycle(repo_root).recover_pending_stack_close(
        manifest, manifest_path, pending,
    )


def stack_archive_writer_guard(repo_root, manifest, stack):
    return _stack_lifecycle(repo_root).stack_archive_writer_guard(
        manifest, stack,
    )


def build_stack_archive_plan(repo_root, manifest, stack_id, reason, resume_intent):
    return _stack_lifecycle(repo_root).build_stack_archive_plan(
        manifest, stack_id, reason, resume_intent,
    )


def verify_stack_archive_proof(repo_root, proof, stack=None):
    return _stack_lifecycle(repo_root).verify_stack_archive_proof(
        proof, stack,
    )


def command_stack_release_desk(args):
    repo_root, manifest_path = resolve_command_manifest_selection(args)
    manifest_path = Path(os.path.abspath(manifest_path))
    require_stack_sync_metadata_paths(repo_root, manifest_path)
    manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    require_delivery_manifest(manifest)
    stack = require_stack(manifest, args.stack)

    def require_no_writer():
        current, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
        if not current or manifest_digest(current) != manifest_digest(manifest):
            raise SyncwheelError('selected manifest changed during desk release')
        roots = {repo_root, primary_authority_manifest_path(repo_root).parent.parent}
        for root in roots:
            _, coordination = coordination_profile(root)
            if stack['id'] in (coordination.get('locks') or {}) or local_lease_is_active(coordination):
                raise SyncwheelError('desk release refuses an active worktree lock or coordination lease')

    require_no_writer()
    try:
        result = _desk_lifecycle(repo_root).release(args, manifest, manifest_path, stack, require_no_writer)
    except OSError as error:
        raise SyncwheelError(f'desk release could not complete: {error}') from error
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_stack_archive(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_archive(args, manifest, manifest_path)


def command_stack_close(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_close(args, manifest, manifest_path)


def stack_create_recorded(repo_root, manifest_path, stack_id):
    return _stack_lifecycle(repo_root).stack_create_recorded(
        manifest_path, stack_id,
    )


def unmatched_stack_create_operations(repo_root, manifest_path, stack_id=None):
    return _stack_lifecycle(repo_root).unmatched_stack_create_operations(
        manifest_path, stack_id,
    )


def pending_stack_create_operation(repo_root, manifest_path, stack_id, branch, tip):
    return _stack_lifecycle(repo_root).pending_stack_create_operation(
        manifest_path, stack_id, branch, tip,
    )


def abandon_pending_stack_creates(repo_root, manifest_path, stack_id, reason):
    return _stack_lifecycle(repo_root).abandon_pending_stack_creates(
        manifest_path, stack_id, reason,
    )


def require_current_stack_create_operation(
    repo_root, manifest_path, stack_id, branch, tip, operation_token
):
    return _stack_lifecycle(repo_root).require_current_stack_create_operation(
        manifest_path, stack_id, branch, tip, operation_token,
    )




def recover_equivalent_draft_create(
    repo_root, manifest, manifest_path, stack, operation_token
):
    return _stack_lifecycle(repo_root).recover_equivalent_draft_create(
        manifest, manifest_path, stack, operation_token,
    )


def preflight_active_draft_create(repo_root, manifest, manifest_path, stack):
    return _stack_lifecycle(repo_root).preflight_active_draft_create(
        manifest, manifest_path, stack,
    )


def command_stack_create(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_create(args, manifest, manifest_path)




def draft_materialization_worktree(repo_root, operation_token):
    return _stack_lifecycle(repo_root).draft_materialization_worktree(
        operation_token,
    )


def cleanup_interrupted_draft_materialization(
    repo_root, stack, planned_tip, operation_token
):
    return _stack_lifecycle(repo_root).cleanup_interrupted_draft_materialization(
        stack, planned_tip, operation_token,
    )


def materialize_new_stack_branch(
    repo_root, stack, planned_tip=None, operation_token=None
):
    return _stack_lifecycle(repo_root).materialize_new_stack_branch(
        stack, planned_tip, operation_token,
    )




def anchor_diverged_draft_branch(
    repo_root, manifest_path, stack_id, from_branch, draft_tip
):
    return _stack_lifecycle(repo_root).anchor_diverged_draft_branch(
        manifest_path, stack_id, from_branch, draft_tip,
    )


def realign_promoted_local_branches(
    repo_root, config, stack_id, from_branch, to_branch, promoted_tip,
    manifest_path=None,
):
    return _stack_lifecycle(repo_root).realign_promoted_local_branches(
        config, stack_id, from_branch, to_branch, promoted_tip, manifest_path,
    )


def equivalent_remote_stack_promotion(
    repo_root, manifest, stack, from_branch, to_branch,
):
    return _stack_lifecycle(repo_root).equivalent_remote_stack_promotion(
        manifest, stack, from_branch, to_branch,
    )


def adopt_equivalent_remote_stack_promotion(
    repo_root, manifest, manifest_path, stack, from_branch, to_branch,
):
    return _stack_lifecycle(repo_root).adopt_equivalent_remote_stack_promotion(
        manifest, manifest_path, stack, from_branch, to_branch,
    )


def recover_pending_stack_promote(
    repo_root, manifest, manifest_path, stack, pending
):
    return _stack_lifecycle(repo_root).recover_pending_stack_promote(
        manifest, manifest_path, stack, pending,
    )


def complete_pending_promote_intents(
    repo_root, manifest, manifest_path, *, apply=True, skip_stacks=(),
    only_stacks=None,
):
    return _stack_lifecycle(repo_root).complete_pending_promote_intents(
        manifest, manifest_path, apply=apply, skip_stacks=skip_stacks, only_stacks=only_stacks,
    )


def resolve_pending_promote_intents(
    repo_root, manifest, manifest_path, *, apply=True, skip_stacks=(),
    only_stacks=None,
):
    return _stack_lifecycle(repo_root).resolve_pending_promote_intents(
        manifest, manifest_path, apply=apply, skip_stacks=skip_stacks, only_stacks=only_stacks,
    )


def publish_updated_draft_before_promotion(repo_root, manifest, manifest_path, stack):
    return _stack_lifecycle(repo_root).publish_updated_draft_before_promotion(
        manifest, manifest_path, stack,
    )


def command_stack_promote(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_promote(args, manifest, manifest_path)


def command_stack_demote(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_demote(args, manifest, manifest_path)


def manifest_path_remedy_options(repo_root, manifest_path):
    manifest_path = Path(os.path.abspath(manifest_path))
    selection = ['--repo', str(repo_root)]
    if _manifest_storage.is_personal_manifest_path(repo_root, manifest_path):
        personal = manifest_path.name.removesuffix('.local.json')
        if personal_manifest_path(repo_root, personal) == manifest_path:
            return [*selection, '--personal', personal]
    return [*selection, '--manifest', str(manifest_path)]


def command_stack_sync(args):
    repo_root, manifest_path = resolve_command_manifest_selection(args)
    manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if manifest is None:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    return _stack_lifecycle(repo_root).command_stack_sync(
        args, manifest, manifest_path, shlex.join(manifest_path_remedy_options(repo_root, manifest_path)),
    )


def stack_absorb_preflight(args):
    if args.func != command_stack_absorb:
        return
    repo_root, path = resolve_command_manifest_selection(args)
    manifest, _ = _manifest_storage.load_manifest(repo_root, path)
    if manifest is not None and manifest.get('repository_mode') != 'journal':
        stack = require_stack(manifest, args.stack)
        _stack_lifecycle(repo_root).require_stack_absorb_allowed(
            stack, args, shlex.join(manifest_path_remedy_options(repo_root, path)),
        )
        _stack_lifecycle(repo_root).require_empty_stack_absorb_base(stack)


def command_stack_absorb(args):
    repo_root, manifest_path = resolve_command_manifest_selection(args)
    manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if manifest is None:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    return _stack_lifecycle(repo_root).command_stack_absorb(
        args, manifest, manifest_path, shlex.join(manifest_path_remedy_options(repo_root, manifest_path)),
    )


def observed_adopted_stack_command(args):
    if args.func == command_stack_sync:
        args._stack_sync_selection = resolve_stack_sync_selection(args)
    root, path = resolve_command_manifest_selection(args)
    manifest, _ = _manifest_storage.load_manifest(root, path)
    if not manifest or manifest.get('repository_mode') == 'journal':
        return None
    stack = require_stack(manifest, args.stack)
    if not stack.get('adopted_history'):
        return None
    validate_selected_primary_authority_path(root, path)
    selection = shlex.join(manifest_path_remedy_options(root, path))
    stack_adoption_preflight(args)
    refuse_stack_sync_control_recovery(root, path, 'adopted stack command')
    lifecycle = _stack_lifecycle(root)
    lifecycle.refuse_pending_stack_sync_promotion(path, selection)
    if args.func == command_stack_sync:
        return lifecycle.observe_adopted_stack_sync(args, stack, selection)
    lifecycle.require_stack_absorb_allowed(stack, args, selection)
    raise SyncwheelError('adopted stack changed during observation; inspect and retry')


def resolve_stack_absorb_location(repo_root, manifest_path, manifest, stack, args):
    return _stack_lifecycle(repo_root).resolve_stack_absorb_location(
        manifest_path, manifest, stack, args,
    )


def command_stack_set(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_set(args, manifest, manifest_path)


def reset_stack_to_published(repo_root, manifest, manifest_path, stack, args):
    return _stack_lifecycle(repo_root).reset_stack_to_published(
        manifest, manifest_path, stack, args,
    )


def command_stack_resolve_integration(args):
    """Record conflict-resolved commits that already materialize on integration."""
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_resolve_integration(args, manifest, manifest_path)


def build_stack_classify_integration_plan(repo_root, manifest, manifest_path, stack_id, specs):
    return _stack_lifecycle(repo_root).build_stack_classify_integration_plan(
        manifest, manifest_path, stack_id, specs,
    )


def command_stack_classify_integration(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    return _stack_lifecycle(repo_root).command_stack_classify_integration(args, manifest, manifest_path)


def validate_stack_update(repo_root, manifest, stack, previous_commits):
    return _stack_lifecycle(repo_root).validate_stack_update(
        manifest, stack, previous_commits,
    )


def validate_integration_first_base(repo_root, manifest, added_commits):
    return _stack_lifecycle(repo_root).validate_integration_first_base(
        manifest, added_commits,
    )


def command_stack_add(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_add(args, manifest, manifest_path)


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
    return _stack_lifecycle(repo_root).rebuild_stack_from_manifest(
        manifest, manifest_path, stack, dry_run=dry_run, mode=mode, worktree=worktree,
    )


def command_stack_capture_integration(args):
    repo_root, manifest_path = resolve_command_manifest_selection(args)
    refuse_stack_sync_control_recovery(repo_root, manifest_path, 'stack capture-integration')
    manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if manifest is None:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    return _stack_lifecycle(repo_root).command_stack_capture_integration(args, manifest, manifest_path)


def command_stack_rebuild(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_rebuild(args, manifest, manifest_path)


def command_stack_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_push(args, manifest, manifest_path)


def command_stack_git(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    return _stack_lifecycle(repo_root).command_stack_git(args, manifest, manifest_path)


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


def integration_control_manifest_from_tree(repo_root, tree):
    return _integration_history().integration_control_manifest_from_tree(
        repo_root, tree,
    )


def local_only_control_manifest_is_inherited(repo_root, manifest, projected_tree=None):
    """Whether the projection, not a control commit, puts the manifest in a tree."""
    if projected_tree is None:
        projected_tree = materialize_integration_projection(repo_root, manifest)
    return integration_control_manifest_from_tree(repo_root, projected_tree) is not None


def integration_control_matches_selected(repo_root, manifest, tree, projected_tree=None):
    committed = integration_control_manifest_from_tree(repo_root, tree)
    if control_manifest_stays_untracked(manifest):
        return committed is None or local_only_control_manifest_is_inherited(
            repo_root, manifest, projected_tree,
        )
    return committed is not None and manifest_digest(committed) == manifest_digest(manifest)


def require_selected_integration_control(repo_root, manifest, tip):
    if tip and integration_control_matches_selected(repo_root, manifest, tip):
        return
    if control_manifest_stays_untracked(manifest):
        raise SyncwheelError(
            'integration tip carries the local-only control manifest; rebuild the '
            'integration branch so it stays out of the published tree'
        )
    raise SyncwheelError('integration tip does not contain the selected control manifest; rebuild the reviewed selection')


def integration_tree_matches_product_projection(repo_root, manifest, candidate_tree, product_tree):
    """Compare product bytes independently of which valid control manifest is selected."""
    committed = integration_control_manifest_from_tree(repo_root, candidate_tree)
    entry = tree_path_entry(repo_root, candidate_tree, '.syncwheel/manifest.json')
    if entry is not None and committed is None:
        return False
    if candidate_tree == product_tree:
        return True
    changed = integration_tree_changed_paths(repo_root, candidate_tree, product_tree)
    if committed is None or not changed or not set(changed).issubset({
        '.syncwheel/manifest.json', '.gitignore',
    }):
        return False
    if '.gitignore' not in changed:
        return True
    candidate_entry = tree_path_entry(repo_root, candidate_tree, '.gitignore')
    product_entry = tree_path_entry(repo_root, product_tree, '.gitignore')
    if (
        candidate_entry is None or candidate_entry['mode'] != '100644'
        or (product_entry is not None and product_entry['mode'] != '100644')
    ):
        return False
    try:
        candidate = split_syncwheel_managed_gitignore(
            tree_path_bytes(repo_root, candidate_entry).decode('utf-8'), syncwheel_worktree_root(manifest)
        )
        product = split_syncwheel_managed_gitignore(
            tree_path_bytes(repo_root, product_entry).decode('utf-8'), syncwheel_worktree_root(manifest)
        )
    except UnicodeDecodeError:
        return False
    return bool(candidate and product and candidate['managed'] is not None
                and candidate['unmanaged'] == product['unmanaged'])


def integration_sync_report(repo_root, manifest, remote=None, stack_ref_overrides=None):
    integration = manifest['integration']
    branch = integration['branch']
    remote_ref = remote_integration_ref(manifest, remote)
    local_oid = ref_tip(repo_root, branch)
    remote_oid = ref_tip(repo_root, remote_ref)
    report = {
        'branch': branch, 'remote_ref': remote_ref,
        'local_oid': local_oid, 'remote_oid': remote_oid,
        'local_exists': bool(local_oid), 'remote_exists': bool(remote_oid),
        'relation': 'missing', 'ahead': None, 'behind': None,
        'local_tree': ref_tree(repo_root, local_oid) if local_oid else None,
        'remote_tree': ref_tree(repo_root, remote_oid) if remote_oid else None,
        'projected_tree': None,
        'remote_matches_projection': None, 'local_matches_projection': None,
        'remote_matches_product_projection': None, 'local_matches_product_projection': None,
        'remote_control_manifest_matches_selected': None, 'local_control_manifest_matches_selected': None,
        'local_control_only_ahead': False,
    }
    if local_oid and remote_oid:
        ahead, behind = rev_left_right_count(repo_root, local_oid, remote_oid)
        report['ahead'], report['behind'] = ahead, behind
        if ahead == 0 and behind == 0:
            report['relation'] = 'aligned'
        elif ahead == 0:
            report['relation'] = 'local_behind'
        elif behind == 0:
            report['relation'] = 'local_ahead'
        else:
            report['relation'] = 'diverged'
    elif local_oid:
        report['relation'] = 'local_only'
    elif remote_oid:
        report['relation'] = 'remote_only'
    if report['relation'] == 'local_ahead':
        ahead_commits = rev_list(repo_root, f'{remote_oid}..{local_oid}')
        report['local_control_only_ahead'] = bool(ahead_commits) and all(
            is_manifest_only_commit(repo_root, commit) for commit in ahead_commits
        )
    try:
        projected_tree = materialize_integration_projection(repo_root, manifest, stack_ref_overrides)
        report['projected_tree'] = projected_tree
        for side in ('local', 'remote'):
            tree = report[f'{side}_tree']
            if tree:
                product = integration_tree_matches_product_projection(repo_root, manifest, tree, projected_tree)
                report[f'{side}_matches_product_projection'] = product
                report[f'{side}_matches_projection'] = product  # Compatibility alias.
                report[f'{side}_control_manifest_matches_selected'] = (
                    integration_control_matches_selected(
                        repo_root, manifest, tree, projected_tree,
                    )
                )
    except SyncwheelError as exc:
        report['projection_error'] = str(exc)
    return report


def stack_remote_ref(manifest, stack, remote=None):
    remote = remote or stack.get('publication_remote') or manifest['defaults']['publication_remote']
    return f"{remote}/{stack['branch']}"


def declared_stack_history(repo_root, stack):
    """Expand only explicitly adopted history; ordinary merge recipes own no ancestors."""
    adopted = validate_stack_adoption(stack)
    if adopted:
        if not branch_contains(repo_root, adopted['tip'], adopted['base']):
            raise SyncwheelError('adopted history base is not an ancestor of its tip')
        return rev_list(repo_root, f"{adopted['base']}..{adopted['tip']}")
    return [commit_full_sha(repo_root, item) for item in stack['commits']]


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


def preflight_reconcile_mutation_targets(repo_root, manifest, actions, worktree_root, manifest_path=None):
    """Reject dirty local targets before reconciliation changes any managed state."""
    stack_actions = {'rebuild_stack', 'align_stack_to_remote'}
    integration_actions = {'rebuild_integration', 'align_integration_to_remote'}
    checked = set()

    for action in actions:
        if action['type'] in stack_actions:
            stack = require_stack(manifest, action['stack'])
            if stack.get('adopted_history'):
                raise SyncwheelError('adopted history requires explicit stack set --base BASE --adopt-tip TIP')
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
                        allowed_path_prefixes=['.syncwheel/'],
                        remedy_commands=primary_checkout_remedy_commands(
                            manifest, repo_root=repo_root, manifest_path=manifest_path,
                        ),
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
    # A local-only control manifest is never published, so control-manifest
    # parity with the remote can neither justify a push nor excuse a rebuild.
    local_only_control = control_manifest_stays_untracked(manifest)
    control_only_ahead = (
        False if local_only_control
        else integration_report.get('local_control_only_ahead')
    )
    remote_control_drift = (
        False if local_only_control
        else integration_report.get('remote_control_manifest_matches_selected') is False
    )
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
            or (integration_report.get('local_control_manifest_matches_selected') is False
                and not control_only_ahead)
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
        and not control_only_ahead
        and not integration_report.get('projection_error')
        and integration_report['remote_exists']
        and integration_report.get('remote_matches_projection') is True
        and integration_report.get('remote_control_manifest_matches_selected') is True
        and (integration_report.get('local_matches_projection') is not True
             or integration_report.get('local_control_manifest_matches_selected') is not True)
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
        and integration_report.get('local_control_manifest_matches_selected') is True
        and integration_report.get('remote_matches_projection') is True
        and integration_report.get('remote_control_manifest_matches_selected') is True
        and integration_report['relation'] != 'aligned'
        and not control_only_ahead
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
        or remote_control_drift
        or control_only_ahead is True
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
    state = _manifest_storage.load_ledger_state(repo_root, manifest_path)
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
    reconcile_remedy = (
        'publish' if getattr(args, 'command', None) == 'publish'
        else 'reconcile --apply --push'
    )
    reconcile_remote = (coordination_config(manifest) or {}).get('remote')
    if (
        getattr(args, 'push', False)
        and coordination_is_active(manifest)
        and reconcile_remote
        and not coordination_remote_is_reachable(repo_root, reconcile_remote)
    ):
        raise coordinated_publish_remote_failure(reconcile_remedy)
    complete_pending_promote_intents(
        repo_root, manifest, manifest_path,
        apply=bool(getattr(args, 'apply', False)),
    )
    if args.apply and not getattr(args, '_skip_implicit_control_recovery', False):
        manifest = recover_incomplete_control_manifest_persistence(
            repo_root, manifest_path, manifest
        )
    preflight_manifest = json.loads(json.dumps(manifest))
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
        initial_validation = validate_manifest(repo_root, manifest, manifest_path)
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
    validation = validate_manifest(repo_root, manifest, manifest_path)
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
    preflight_reconcile_mutation_targets(repo_root, manifest, actions, worktree_root, manifest_path)
    _manifest_storage.preflight_control_manifest_digest(repo_root, manifest_path, preflight_manifest)
    _manifest_storage.require_manifest_transaction_current(manifest_path)
    if _manifest_storage.is_external_manifest_path(repo_root, manifest_path):
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
    control_manifest_restored = False
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
            adopted = validate_stack_adoption(stack)
            publication_tip = ref_tip(repo_root, stack['branch'])
            if adopted and publication_tip != adopted['tip']:
                raise SyncwheelError('adopted stack source changed; inspect and adopt the exact intended tip')
            refusal = draft_push_refusal(manifest, stack, stack_push_remote(manifest, stack, args.remote))
            if refusal:
                raise SyncwheelError(refusal)
            if coordinated_push:
                ref = f"refs/heads/{stack['branch']}"
                coordinated_refs[ref] = publication_tip
                coordinated_events.append({
                    'type': 'stack_pushed',
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'tip': coordinated_refs[ref],
                    'actor': governed_worktree_owner(),
                })
                continue
            remote = stack_push_remote(manifest, stack, args.remote)
            stack_push_args = list(push_args)
            if forced_push_requested(stack_push_args):
                # Same rule as `stack push`: never discard a commit another
                # session pushed to this stack branch; pin the exact lease.
                leases = legacy_stack_push_lease(
                    repo_root, stack, remote, publication_tip,
                    stack_push_args, manifest_path,
                )
                stack_push_args = [*leases, *without_force_push_args(stack_push_args)]
            refspec = f"{publication_tip}:refs/heads/{stack['branch']}"
            command = ['git', 'push', *stack_push_args, remote, refspec]
            run_authorized_push(
                repo_root, command, remote, [f"refs/heads/{stack['branch']}"],
                manifest=manifest,
            )
            print(quoted(command))
            append_ledger_event(
                repo_root,
                'stack_pushed',
                {
                    'stack': stack['id'],
                    'branch': stack['branch'],
                    'remote': remote,
                    'tip': publication_tip,
                    'actor': governed_worktree_owner(),
                },
                manifest_path,
            )
        elif action['type'] == 'rebuild_integration':
            integration = manifest['integration']
            if reconcile_integration_ancestry(
                repo_root, manifest_path, manifest,
                'syncwheel reconcile --apply', 'reconcile integration rebuild',
            ):
                control_manifest_restored = True
                continue
            reset_destination_allowed_paths = _manifest_storage.control_manifest_source_allowance(
                repo_root, manifest_path
            )
            reset_destination_allowed_prefixes = []
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
                    allowed_path_prefixes=['.syncwheel/'],
                    remedy_commands=primary_checkout_remedy_commands(
                        manifest, repo_root=repo_root, manifest_path=manifest_path,
                    ),
                    allowed_paths=reset_destination_allowed_paths,
                )
                reset_destination_allowed_prefixes = ['.syncwheel/']
                worktree = None
                in_place = True
            else:
                worktree = reconcile_worktree_path(repo_root, integration['branch'], worktree_root)
                ensure_non_in_place_target_clean(
                    repo_root, integration['branch'], worktree,
                    allowed_paths=reset_destination_allowed_paths,
                )
                in_place = False
            mode, worktree = select_replay_mode(
                repo_root,
                manifest,
                args,
                integration['branch'],
                (worktree, in_place),
                plumbing_supported=integration_supports_plumbing(manifest),
            )
            reuse = plan_published_integration_tip_reuse(
                repo_root, manifest, manifest_path
            ) if coordinated_push else None
            if reuse and reuse['status'] in {'refuse', 'fallback'}:
                raise SyncwheelError(reuse['reason'])
            if reuse and reuse['status'] == 'retain':
                if not published_integration_tip_reuse_is_current(
                    repo_root, manifest, manifest_path, reuse
                ):
                    raise SyncwheelError(
                        'published integration reuse lease changed before decision; '
                        'retry reconcile from a fresh observation'
                    )
                print(
                    f"{integration['branch']}: retained published integration tip "
                    f"{reuse['publishedTip']} after exact control-path verification"
                )
                continue
            published_replay = bool(reuse and reuse['status'] == 'replay')
            if published_replay and not published_integration_tip_reuse_is_current(
                repo_root, manifest, manifest_path, reuse
            ):
                raise SyncwheelError(
                    'published integration replay lease changed before execution; '
                    'retry reconcile from a fresh observation'
                )
            reset_destination_lease = (
                checkout_reset_destination_lease(
                    repo_root,
                    integration['branch'],
                    allowed_paths=reset_destination_allowed_paths,
                    allowed_path_prefixes=reset_destination_allowed_prefixes,
                )
                if published_replay
                else None
            )
            replay_mode = 'ephemeral' if published_replay else mode
            guarded = local_only_control_manifest_guard(
                repo_root, manifest, manifest_path
            )
            target = (
                published_integration_replay_target(
                    manifest, reuse, worktree, reset_destination_lease
                )
                if published_replay
                else replay_target(integration=integration, worktree=worktree)
            )
            try:
                result = execute_replay(
                    repo_root,
                    replay_plan(
                        repo_root,
                        manifest,
                        target,
                        replay_mode,
                    ),
                    True,
                )
            finally:
                _manifest_storage.restore_guarded_control_manifests(guarded)
            require_replay_success(result)
            if result['mode'] == 'in-place':
                acknowledge_in_place_manifest_replay(
                    repo_root, manifest_path, result['after_tip']
                )
            post_replay_source_lease = {} if published_replay else None
            control_manifest_restored = restore_control_manifest_after_integration_rebuild(
                repo_root, manifest_path, manifest, result['after_tip'], result['mode'],
                reason='reconcile integration rebuild', command='syncwheel reconcile --apply',
                source_lease_out=post_replay_source_lease,
            ) or control_manifest_restored
            integration_after_tip = ref_tip(repo_root, integration['branch'])
            if published_replay and not published_integration_replay_is_current(
                repo_root, manifest, manifest_path, reuse,
                post_replay_source_lease,
            ):
                raise SyncwheelError(
                    'published integration replay result or lease changed before publication; '
                    'the remote was not updated'
                )
            append_ledger_event(
                repo_root,
                'integration_rebuilt',
                {
                    'branch': integration['branch'],
                    'before_tip': result['before_tip'],
                    'after_tip': integration_after_tip,
                    'stacks': list(integration.get('stacks', [])),
                    'replay_mode': result['mode'],
                    'actor': control_manifest_actor(repo_root),
                    'reason': 'reconcile integration rebuild',
                    'command': 'syncwheel reconcile --apply',
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
                    allowed_path_prefixes=['.syncwheel/'],
                    remedy_commands=primary_checkout_remedy_commands(
                        manifest, repo_root=repo_root, manifest_path=manifest_path,
                    ),
                )
                worktree = None
            else:
                worktree = reconcile_worktree_path(repo_root, integration['branch'], worktree_root)
                ensure_non_in_place_target_clean(repo_root, integration['branch'], worktree)
            guarded = local_only_control_manifest_guard(
                repo_root, manifest, manifest_path
            )
            commands = materialize_remote_align_commands(
                repo_root,
                integration['branch'],
                action['remote_ref'],
                worktree,
            )
            # This helper fetches after planning. Validate the fetched object
            # before any backup/ref/index effect, then use only its immutable OID.
            run_command_list(commands[:1], repo_root, True)
            remote_tip = ref_tip(repo_root, action['remote_ref'])
            require_selected_integration_control(repo_root, manifest, remote_tip)
            if not integration_tree_matches_product_projection(
                repo_root, manifest, ref_tree(repo_root, remote_tip),
                materialize_integration_projection(repo_root, manifest, stack_ref_overrides),
            ):
                raise SyncwheelError('fetched integration does not match the selected product projection')
            commands = [[remote_tip if arg == action['remote_ref'] else arg for arg in command]
                        for command in commands[1:]]
            try:
                run_command_list(commands, repo_root, True)
            finally:
                _manifest_storage.restore_guarded_control_manifests(guarded)
            if use_primary_checkout:
                acknowledge_in_place_manifest_replay(repo_root, manifest_path, remote_tip)
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
            branch = manifest['integration']['branch']
            ref = f'refs/heads/{branch}'
            tip = ref_tip(repo_root, branch)
            require_selected_integration_control(repo_root, manifest, tip)
            command = ['git', 'push', *push_args, remote, f'{tip}:{ref}']
            run_authorized_push(
                repo_root, command, remote, [ref], manifest=manifest,
            )
            print(quoted(command))
            append_ledger_event(
                repo_root,
                'integration_pushed',
                {
                    'branch': branch,
                    'remote': remote,
                    'tip': tip,
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
        _manifest_storage.require_manifest_transaction_current(manifest_path)
        publication_scope = 'full' if full_scope else 'partial'
        projection_status = 'convergent' if full_scope else 'partial'
        adoptable = {
            intent.get('operation_token')
            for intent in pending_coordination_publications(repo_root, manifest_path)
            if intent.get('scope') == publication_scope
            # A changed desired tuple must be re-proved by the existing landing resolver,
            # not adopted and then frozen as a mismatched retry. Empty-action retries keep
            # their existing intent so already-published operations remain idempotent.
            and (not coordinated_refs or intent.get('changed_refs') == coordinated_refs)
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
        effective_refs = (publication_operation.get('changed_refs') or {}) if publication_operation else coordinated_refs
        integration_ref = f"refs/heads/{manifest['integration']['branch']}"
        if integration_ref in effective_refs:
            require_selected_integration_control(repo_root, manifest, effective_refs[integration_ref])
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
                            'actor': governed_worktree_owner(),
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
        config = coordination_config(manifest)
        try:
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
        except SyncwheelError as exc:
            failure = coordinated_publish_command_failure(
                repo_root, config, exc, reconcile_remedy
            )
            if failure is None:
                raise
            raise failure from exc
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
    if args.apply and not control_manifest_restored and (resume_manifest_changed or deferred_manifest_updates):
        reason = 'resume_manifest_update' if resume_manifest_changed else 'reconcile_update_manifest'
        context = {'stacks': deferred_manifest_updates} if deferred_manifest_updates else None
        save_manifest_with_ledger(repo_root, manifest_path, manifest, reason, context)
        for update in deferred_manifest_updates:
            print(f"{update['stack']}: manifest updated from rebuilt branch")
    if args.apply and getattr(args, 'auto_gc', False) and coordination_is_active(manifest):
        gc_plan = run_coordination_gc(
            repo_root,
            manifest,
            apply=True,
            fetch=True,
            implicit=True,
            stack_scope=set(args.stack) if args.stack else None,
        )
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
    validation = validate_manifest(repo_root, manifest, manifest_path)
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
    ensure_in_place_target(repo_root, integration['branch'], manifest, manifest_path=manifest_path)
    report = integration_sync_report(repo_root, manifest, args.remote)
    if not report['remote_exists']:
        raise SyncwheelError(f"remote integration ref does not exist: {report['remote_ref']}")
    if report.get('projection_error'):
        raise SyncwheelError(f"cannot project integration from manifest: {report['projection_error']}")
    if not args.force:
        require_selected_integration_control(repo_root, manifest, report['remote_oid'])
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
    if args.dry_run:
        guarded = []
        untrack_step = local_only_control_manifest_untrack_step(repo_root, manifest)
        if untrack_step:
            print(quoted(untrack_step))
    else:
        guarded = local_only_control_manifest_guard(
            repo_root, manifest, manifest_path
        )
    commands = []
    backup = backup_branch_command(repo_root, integration['branch'], timestamp)
    if backup:
        commands.append(backup)
    commands.append(['git', 'reset', '--hard', report['remote_oid']])
    try:
        run_command_list(commands, repo_root, not args.dry_run)
    finally:
        _manifest_storage.restore_guarded_control_manifests(guarded)
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


def integration_desk_receipt_path(repo_root, manifest_path):
    key = hashlib.sha256(str(Path(manifest_path).resolve()).encode()).hexdigest()[:20]
    return git_common_dir(repo_root) / 'syncwheel' / 'integration-desks' / (key + '.json')


def integration_desk_inputs(repo_root, manifest, manifest_path):
    inputs = {
        'manifest': manifest_digest(manifest),
        'replay': integration_projection_input_snapshot(repo_root, manifest),
        'integration_tip': ref_tip(repo_root, manifest['integration']['branch']),
    }
    # Legacy cherry-pick projections preserve adopted tips through merge steps.
    # Observe their source refs as well as the ordinary projection inputs so a
    # detached desk freezes every merge operand and detects source-ref drift.
    if manifest['integration'].get('strategy', 'cherry-pick') != 'merge-stacks':
        stacks = stack_map(manifest)
        adopted_sources = []
        for stack_id in manifest['integration']['stacks']:
            stack = stacks[stack_id]
            adopted = validate_stack_adoption(stack)
            if adopted:
                observed = integration_projection_ref_observation(
                    repo_root, 'adopted-stack', stack_id, stack['branch'],
                )
                if observed['tip'] != adopted['tip']:
                    raise SyncwheelError('adopted stack source changed; inspect and adopt the exact intended tip')
                adopted_sources.append(observed)
        if adopted_sources:
            inputs['adopted_sources'] = adopted_sources
    return inputs


def normalized_integration_desk_inputs(repo_root, inputs, previous_tip):
    observed = copy.deepcopy(inputs)
    current_tip = observed['integration_tip']
    if previous_tip != current_tip and branch_contains(repo_root, current_tip, previous_tip):
        transitions = rev_list(repo_root, f'{previous_tip}..{current_tip}')
        if all(
            len(git(repo_root, 'rev-list', '--parents', '-n', '1', value).stdout.split()) == 2
            and integration_control_only_transition(repo_root, value, value + '^')
            for value in transitions
        ):
            # Normal command completion may commit the selected control manifest.
            # Accept only proven control-only successors, never a product edit.
            observed['integration_tip'] = previous_tip
    return observed


def read_integration_desk(repo_root, manifest, manifest_path, worktree):
    receipt_path = integration_desk_receipt_path(repo_root, manifest_path)
    if not receipt_path.exists():
        raise SyncwheelError('no managed integration resolution desk; run int rebuild --replay-mode desk first')
    receipt = json.loads(receipt_path.read_text())
    desk = Path(worktree).resolve()
    if str(desk) != receipt['worktree'] or git_common_dir(desk) != git_common_dir(repo_root):
        raise SyncwheelError('integration resolution desk does not match its receipt')
    if git(desk, 'symbolic-ref', '-q', 'HEAD', check=False).returncode == 0:
        raise SyncwheelError('integration resolution desk must remain detached')
    observed = normalized_integration_desk_inputs(
        repo_root, integration_desk_inputs(repo_root, manifest, manifest_path),
        receipt['inputs']['integration_tip'],
    )
    capture = receipt.get('capture')
    saved_capture = capture and observed == normalized_integration_desk_inputs(
        repo_root, capture['inputs'], receipt['inputs']['integration_tip'],
    )
    if receipt['inputs'] != observed and not saved_capture:
        changed = ', '.join(key for key in observed if observed[key] != receipt['inputs'].get(key))
        raise SyncwheelError('integration resolution desk inputs changed (' + changed + '); preserve it and use int rebuild --replay-mode desk --worktree <new-path> --reason "review changed inputs"')
    allowed_heads = {receipt['head']}
    if saved_capture:
        allowed_heads.add(capture['commit'])
    if ref_tip(desk, 'HEAD') not in allowed_heads:
        raise SyncwheelError('integration resolution desk HEAD changed; refusing to capture unrelated work')
    return receipt_path, receipt, desk


def finish_integration_desk_capture(repo_root, manifest, manifest_path, receipt_path, receipt, desk):
    capture = receipt['capture']
    if (git(desk, 'diff', '--name-only', '--diff-filter=U').stdout.strip()
            or git(desk, 'diff', '--quiet', check=False).returncode != 0
            or git(desk, 'write-tree').stdout.strip() != capture['tree']):
        raise SyncwheelError('saved integration capture has changed desk content; preserve and review it')
    context = capture.get('context') or {}
    if context.get('empty_integration_only') and not any(
        event.get('type') == 'manifest_saved'
        and event.get('payload', {}).get('reason') == 'stack_resolve_integration'
        and event['payload'].get('manifest_hash') == manifest_digest(manifest)
        and event['payload'].get('context') == context
        for event in _manifest_storage.load_ledger_events(repo_root, manifest_path)
    ):
        append_ledger_event(repo_root, 'manifest_saved', manifest_event_payload(
            manifest_path, manifest, 'stack_resolve_integration', context,
        ), manifest_path)
    # A write-ahead receipt binds the saved manifest, reviewed tree and exact object.
    # Retry completes the same capture even after reset cleared MERGE_HEAD.
    git(desk, 'reset', '--hard', capture['commit'])
    receipt.update(head=capture['commit'], status='ready', next=receipt['next'] + 1)
    receipt.pop('operation', None)
    receipt.pop('capture')
    receipt['inputs'] = integration_desk_inputs(repo_root, manifest, manifest_path)
    atomic_write_private_json(receipt_path, receipt, indent=2)


def capture_integration_desk(repo_root, manifest, manifest_path, stack, args):
    if args.specs or args.empty:
        raise SyncwheelError('--worktree cannot be combined with commit specs or --empty')
    with control_manifest_branch_lock(repo_root, manifest['integration']['branch']):
        receipt_path, receipt, desk = read_integration_desk(
            repo_root, manifest, manifest_path, args.worktree,
        )
        step = receipt['steps'][receipt['next']] if receipt['next'] < len(receipt['steps']) else {}
        if receipt.get('status') != 'conflict' or step.get('stack_id') != stack['id']:
            raise SyncwheelError('this stack has no pending conflict in the integration resolution desk')
        capture = receipt.get('capture')
        if capture and manifest_digest(manifest) == capture['inputs']['manifest']:
            finish_integration_desk_capture(
                repo_root, manifest, manifest_path, receipt_path, receipt, desk,
            )
            print(f"{stack['id']}: completed previously saved integration capture from {desk}")
            return 0
        if git(desk, 'diff', '--name-only', '--diff-filter=U').stdout.strip():
            raise SyncwheelError('integration resolution desk still has unresolved paths; resolve and stage them first')
        if git(desk, 'diff', '--quiet', check=False).returncode != 0:
            raise SyncwheelError('integration resolution desk has unstaged changes; review and stage the complete resolution')
        operation = receipt['operation']
        if operation.get('empty') and not integration_desk_has_exact_empty_step(desk, receipt, step):
            raise SyncwheelError('empty integration-only desk step changed; preserve and review it')
        if ref_tip(desk, operation['ref']) != operation['tip']:
            raise SyncwheelError('integration resolution desk operation changed; refusing unrelated resolution')
        tree = git(desk, 'write-tree').stdout.strip()
        parent = (receipt['head'] if step.get('integration_only')
                  else receipt['stack_bases'][stack['id']])
        commit = parent
        commits = []
        if tree != ref_tree(repo_root, commit):
            commit = git(
                repo_root, 'commit-tree', tree, '-p', commit,
                '-m', f"Resolve stack '{stack['id']}' on integration",
                env=replay_hygiene_env(),
            ).stdout.strip()
            commits = [commit]
        # The merge result is one reviewed replacement of this stack's contribution.
        # Source commits and the source branch remain untouched.
        read_integration_desk(repo_root, manifest, manifest_path, args.worktree)
        # None for cherry-pick: its inputs pin commits, there's no source ref to bind to
        source_tip = next((item['tip'] for item in receipt['inputs']['replay']['refs']
                           if item['kind'] == 'merge-stack' and item['id'] == stack['id']), None)
        if step.get('integration_only'):
            previous = stack_integration_only_commits(stack)
            offset = previous.index(step['argv'][-1])
            stack['integration_only_commits'] = previous[:offset] + commits + previous[offset + 1:]
        else:
            pending = [item['argv'][-1] for item in receipt['steps'][receipt['next'] + 1:]
                       if item.get('stack_id') == stack['id']
                       and not item.get('integration_only')]
            stack['integration_commits'] = commits + pending
            if source_tip:
                stack['integration_resolution'] = {'source_tip': source_tip}
        context = {'stack': stack['id'], 'commits': commits, 'worktree': str(desk),
                   'source_tip': source_tip or ref_tip(repo_root, stack['branch']),
                   **({'empty_integration_only': {'commit': step['argv'][-1],
                                                  'head': receipt['head'], 'tree': tree}}
                      if operation.get('empty') else {})}
        receipt['capture'] = {
            'commit': commit, 'tree': tree, 'context': context,
            'inputs': integration_desk_inputs(repo_root, manifest, manifest_path),
        }
        atomic_write_private_json(receipt_path, receipt, indent=2)
        _manifest_storage.require_manifest_transaction_current(manifest_path)
        save_manifest_with_ledger(
            repo_root, manifest_path, manifest, 'stack_resolve_integration',
            context,
        )
        finish_integration_desk_capture(
            repo_root, manifest, manifest_path, receipt_path, receipt, desk,
        )
        print(f"{stack['id']}: recorded {len(commits)} resolved integration commits from {desk}")
    return 0


def integration_desk_has_exact_empty_step(desk, receipt, step):
    """Recognize only a stopped integration-only pick with no tree change."""
    argv = step.get('argv') or []
    return bool(
        step.get('integration_only') and 'cherry-pick' in argv
        and commit_parent_count(desk, argv[-1]) == 1
        and ref_tip(desk, 'CHERRY_PICK_HEAD') == argv[-1]
        and ref_tip(desk, 'HEAD') == receipt['head']
        and not ref_tip(desk, 'MERGE_HEAD') and not ref_tip(desk, 'REVERT_HEAD')
        and not git(desk, 'status', '--porcelain', '--untracked-files=all').stdout.strip()
        and git(desk, 'write-tree').stdout.strip() == ref_tree(desk, receipt['head'])
        and integration_only_replay_is_empty(desk, argv[-1], receipt['head'],
                                              ref_tree(desk, receipt['head']))
    )


def integration_desk_is_detached(repo_root, manifest, args):
    integration = manifest['integration']
    if integration.get('strategy') == 'merge-stacks':
        return True
    existing = find_worktree_for_branch(repo_root, integration['branch'])
    if not existing:
        return False
    # git won't check out the integration branch twice, so that checkout can't be the desk
    if args.worktree:
        return Path(args.worktree).resolve() != existing
    return get_current_branch(repo_root) == integration['branch']


def rebuild_integration_desk(repo_root, manifest, manifest_path, args):
    """Prepare/reuse an isolated conflict desk before any shared-ref replay.

    Ordinary rebuild remains responsible for publication ancestry, checkout leases
    and the final integration update after the reviewed resolutions are recorded.
    """
    receipt_path = integration_desk_receipt_path(repo_root, manifest_path)
    git(repo_root, 'fetch', '--all', '--prune')
    with control_manifest_branch_lock(repo_root, manifest['integration']['branch']):
        saved = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
        reuse = saved is not None and (not args.worktree or str(Path(args.worktree).resolve()) == saved['worktree'])
        if reuse:
            chosen = args.worktree or saved['worktree']
            receipt_path, receipt, desk = read_integration_desk(
                repo_root, manifest, manifest_path, chosen,
            )
        else:
            desk = Path(args.worktree).resolve() if args.worktree else configured_worktree_path(
                repo_root, manifest['integration']['branch'], effective_worktree_root(manifest),
            ).resolve()
            if desk.exists():
                raise SyncwheelError(f'refusing to overwrite an existing resolution desk path: {desk}')
            # A detached desk never moves integration, even when it is checked out
            # in the primary. Its frozen source/ref inputs are checked on capture.
            inputs = integration_desk_inputs(repo_root, manifest, manifest_path)
            review_manifest = copy.deepcopy(manifest)
            for stack in review_manifest['stacks']:
                binding = stack.get('integration_resolution')
                if binding and binding['source_tip'] != commit_full_sha(repo_root, stack['branch']):
                    stack.pop('integration_commits', None)
                    stack.pop('integration_resolution', None)
            plan = replay_plan(
                repo_root, review_manifest,
                replay_target(integration=review_manifest['integration'], worktree=desk,
                              return_tree=True, skip_contained=True), 'desk',
            )
            if inputs != integration_desk_inputs(repo_root, manifest, manifest_path):
                raise SyncwheelError('integration inputs changed while planning the resolution desk; retry')
            # Execute the observed objects, never a moving source branch name.
            source_tips = {item['id']: item['tip'] for item in (
                inputs['replay']['refs'] + inputs.get('adopted_sources', [])
            ) if item['kind'] in {'merge-stack', 'adopted-stack'}}
            for step in plan['steps'][1:]:
                if 'merge' in step['argv']:
                    step['argv'][step['argv'].index('merge') + 2] = source_tips[step['stack_id']]
            desk.parent.mkdir(parents=True, exist_ok=True)
            git(repo_root, 'worktree', 'add', '--detach', '--quiet', str(desk), inputs['replay']['refs'][0]['tip'])
            receipt = {
                'worktree': str(desk), 'inputs': inputs,
                'head': ref_tip(desk, 'HEAD'), 'next': 0, 'status': 'ready',
                'steps': plan['steps'][1:], 'stack_bases': {},
            }
            if saved is not None:
                archive = receipt_path.with_name(receipt_path.stem + '-' + uuid.uuid4().hex + '.json')
                atomic_write_private_json(archive, saved, indent=2)
            atomic_write_private_json(receipt_path, receipt, indent=2)
        if receipt['status'] == 'conflict':
            stack_id = receipt['steps'][receipt['next']]['stack_id']
            raise SyncwheelError(
                f"resolution desk: {desk}\nResolve and stage the paths, then run:\n  "
                + quoted(['syncwheel', 'stack', 'resolve-integration', stack_id, '--worktree', str(desk)])
            )
        if git(desk, 'status', '--porcelain').stdout.strip():
            raise SyncwheelError('integration resolution desk has unrecorded work; refusing to replay over it')
        while receipt['next'] < len(receipt['steps']):
            step = receipt['steps'][receipt['next']]
            argv = step['argv']
            if not step.get('integration_only'):
                receipt['stack_bases'].setdefault(step['stack_id'], receipt['head'])
            atomic_write_private_json(receipt_path, receipt, indent=2)
            skip = 'cherry-pick' in argv and branch_contains(desk, 'HEAD', argv[-1])
            if not skip:
                try:
                    run(argv if step['env'] is not None else with_git_identity(repo_root, argv),
                        cwd=repo_root, env=step['env'])
                except SyncwheelError:
                    paths = git(desk, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines()
                    empty = integration_desk_has_exact_empty_step(desk, receipt, step)
                    if not paths and not empty:
                        raise
                    # Recheck the full source/ref/manifest lease after Git failed,
                    # including a legacy ready receipt with CHERRY_PICK_HEAD.
                    _manifest_storage.require_manifest_transaction_current(manifest_path)
                    _manifest_storage.preflight_control_manifest_digest(repo_root, manifest_path, manifest)
                    read_integration_desk(repo_root, manifest, manifest_path, desk)
                    operation_ref = 'MERGE_HEAD' if 'merge' in argv else 'CHERRY_PICK_HEAD'
                    receipt.update(status='conflict', operation={
                        'ref': operation_ref, 'tip': ref_tip(desk, operation_ref),
                        **({'empty': True} if empty else {}),
                    })
                    atomic_write_private_json(receipt_path, receipt, indent=2)
                    raise SyncwheelError(
                        f"resolution desk: {desk}\nstack: {step['stack_id']}\n"
                        + ('Empty integration-only step; review its unchanged tree.' if empty else '\n'.join(paths))
                        + '\nResolve and stage the paths, then run:\n  '
                        + quoted(['syncwheel', 'stack', 'resolve-integration', step['stack_id'], '--worktree', str(desk)])
                    )
            receipt.update(head=ref_tip(desk, 'HEAD'), next=receipt['next'] + 1)
            atomic_write_private_json(receipt_path, receipt, indent=2)
        # Release the branch lock before the normal guarded ancestry transaction.
    normal = copy.copy(args)
    normal.replay_mode, normal.worktree, normal.in_place = 'ephemeral', None, False
    result = command_int_rebuild(normal)
    # Keep the desk and its receipt for audit/retry. Never delete operator files.
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    receipt['inputs'] = integration_desk_inputs(repo_root, manifest, manifest_path)
    atomic_write_private_json(receipt_path, receipt, indent=2)
    return result


def require_int_rebuild_reason(args, repo_root, manifest, manifest_path):
    authority, provenance = resolve_repository_authority(repo_root, manifest or {}, manifest_path)
    if (
        not args.dry_run
        and not args.reason
        and AUTHORITY_MODE_AI_MANAGED in (authority['mode'], provenance['shared_policy']['mode'])
    ):
        raise SyncwheelError('int rebuild requires --reason for an ai-managed repository')


def command_int_rebuild(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    require_int_rebuild_reason(args, repo_root, manifest, manifest_path)
    if not args.dry_run:
        complete_pending_promote_intents(repo_root, manifest, manifest_path, apply=True)
        if not getattr(args, '_skip_implicit_control_recovery', False):
            manifest = recover_incomplete_control_manifest_persistence(
                repo_root,
                manifest_path,
                manifest,
                allow_new_operation=bool(args.reason),
            )
    _manifest_storage.preflight_control_manifest_digest(repo_root, manifest_path, manifest)
    if getattr(args, 'replay_mode', None) == 'desk' and integration_desk_is_detached(repo_root, manifest, args):
        if args.dry_run:
            desk = Path(args.worktree).resolve() if args.worktree else configured_worktree_path(
                repo_root, manifest['integration']['branch'], effective_worktree_root(manifest),
            ).resolve()
            print(f'resolution desk: {desk}')
            review_manifest = copy.deepcopy(manifest)
            for stack in review_manifest['stacks']:
                binding = stack.get('integration_resolution')
                if binding and binding['source_tip'] != commit_full_sha(repo_root, stack['branch']):
                    stack.pop('integration_commits', None)
                    stack.pop('integration_resolution', None)
            plan = replay_plan(repo_root, review_manifest, replay_target(
                integration=review_manifest['integration'], worktree=desk,
                return_tree=True, skip_contained=True), 'desk')
            for step in plan['steps']:
                print(replay_step_render(step))
            print('would replay frozen stack inputs; conflicts remain in the desk for reviewed capture')
            return 0
        return rebuild_integration_desk(repo_root, manifest, manifest_path, args)
    integration = manifest['integration']
    validation = validate_manifest(repo_root, manifest, manifest_path)
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
    if not args.dry_run and reconcile_integration_ancestry(
        repo_root, manifest_path, manifest, 'syncwheel int rebuild',
        reason or 'rebuild integration while preserving published history',
    ):
        return 0
    mode, worktree = select_replay_mode(
        repo_root,
        manifest,
        args,
        integration['branch'],
        resolve_int_rebuild_location(repo_root, manifest, args),
        plumbing_supported=integration_supports_plumbing(manifest),
    )
    source_allowance = _manifest_storage.control_manifest_source_allowance(repo_root, manifest_path)
    if not args.dry_run and mode == 'in-place':
        ensure_in_place_target(
            repo_root, manifest['integration']['branch'], manifest,
            allowed_paths=source_allowance, manifest_path=manifest_path,
        )
    if not args.dry_run and mode in ('ephemeral', 'plumbing'):
        ensure_non_in_place_target_clean(
            repo_root,
            manifest['integration']['branch'],
            find_worktree_for_branch(repo_root, manifest['integration']['branch']),
            allowed_paths=source_allowance,
        )
    if not args.dry_run and mode == 'desk':
        ensure_non_in_place_target_clean(
            repo_root, manifest['integration']['branch'], worktree,
            allowed_paths=source_allowance,
        )
    reuse = plan_published_integration_tip_reuse(
        repo_root,
        manifest,
        manifest_path,
        refresh_inputs=not args.dry_run,
    )
    if reuse and reuse['status'] == 'refuse':
        raise SyncwheelError(reuse['reason'])
    if reuse and reuse['status'] == 'retain':
        if args.dry_run:
            print(
                'would retain published integration tip '
                f"{reuse['publishedTip']} after exact control-path verification; "
                'apply re-fetches and rechecks every source, replay, state, and ref lease'
            )
            return 0
        if not published_integration_tip_reuse_is_current(
            repo_root, manifest, manifest_path, reuse
        ):
            raise SyncwheelError(
                'published integration reuse lease changed before decision; '
                'retry int rebuild from a fresh observation'
            )
        receipt = {
            'branch': integration['branch'],
            'published_tip': reuse['publishedTip'],
            'published_tree': reuse['publishedTree'],
            'projected_tree': reuse['projectedTree'],
            'coordination_state': reuse['stateTip'],
            'manifest_digest': reuse['manifestDigest'],
            'source_lease': reuse['sourceLease']['digest'],
            'changed_paths': reuse['changedPaths'],
            'path_proof': reuse['pathProof'],
            'actor': control_manifest_actor(repo_root),
            'reason': reason or 'retain exact published integration control bytes',
            'command': 'syncwheel int rebuild',
        }
        append_ledger_event(
            repo_root,
            'integration_published_tip_reused',
            receipt,
            manifest_path,
            idempotency_key=(
                'integration-published-tip-reused:'
                + canonical_json_digest({
                    'state': reuse['stateTip'],
                    'tip': reuse['publishedTip'],
                    'manifest': reuse['manifestDigest'],
                    'source': reuse['sourceLease']['digest'],
                })
            ),
        )
        print(
            f"{integration['branch']}: retained published integration tip "
            f"{reuse['publishedTip']} after exact control-path verification"
        )
        return 0
    published_replay = bool(reuse and reuse['status'] == 'replay')
    if (
        published_replay
        and not args.dry_run
        and not published_integration_tip_reuse_is_current(
            repo_root, manifest, manifest_path, reuse
        )
    ):
        raise SyncwheelError(
            'published integration replay lease changed before execution; '
            'retry int rebuild from a fresh observation'
        )
    reset_destination_lease = (
        checkout_reset_destination_lease(
            repo_root,
            integration['branch'],
            allowed_paths=source_allowance,
        )
        if published_replay and not args.dry_run
        else None
    )
    replay_mode = 'ephemeral' if published_replay else mode
    if args.dry_run:
        guarded = []
        untrack_step = local_only_control_manifest_untrack_step(repo_root, manifest)
        if untrack_step:
            print(quoted(untrack_step))
    else:
        guarded = local_only_control_manifest_guard(
            repo_root, manifest, manifest_path
        )
    target = (
        published_integration_replay_target(
            manifest, reuse, worktree, reset_destination_lease
        )
        if published_replay
        else replay_target(integration=integration, worktree=worktree)
    )
    try:
        result = execute_replay(
            repo_root,
            replay_plan(
                repo_root,
                manifest,
                target,
                replay_mode,
            ),
            not args.dry_run,
        )
    finally:
        _manifest_storage.restore_guarded_control_manifests(guarded)
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
        if result['mode'] == 'in-place':
            acknowledge_in_place_manifest_replay(
                repo_root, manifest_path, result['after_tip']
            )
        post_replay_source_lease = {} if published_replay else None
        restore_control_manifest_after_integration_rebuild(
            repo_root, manifest_path, manifest, result['after_tip'], result['mode'],
            reason=args.reason or 'restore_control_manifest_after_integration_rebuild',
            command='syncwheel int rebuild',
            source_lease_out=post_replay_source_lease,
        )
        integration_after_tip = ref_tip(repo_root, manifest['integration']['branch'])
        if published_replay and not published_integration_replay_is_current(
            repo_root, manifest, manifest_path, reuse,
            post_replay_source_lease,
        ):
            raise SyncwheelError(
                'published integration replay result or lease changed before publication; '
                'the remote was not updated'
            )
        append_ledger_event(
            repo_root,
            'integration_rebuilt',
            {
                'branch': manifest['integration']['branch'],
                'before_tip': result['before_tip'],
                'after_tip': integration_after_tip,
                'stacks': list(manifest['integration'].get('stacks', [])),
                'replay_mode': result['mode'],
                'actor': control_manifest_actor(repo_root),
                'reason': reason or 'restore_control_manifest_after_integration_rebuild',
                'command': 'syncwheel int rebuild',
                'derived_provenance_reconciled': reconciled,
            },
            manifest_path,
        )
    return 0


def command_int_push(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    complete_pending_promote_intents(
        repo_root, manifest, manifest_path, apply=not args.dry_run
    )
    if getattr(args, 'adopt_remote', None) and not coordination_is_active(manifest):
        raise SyncwheelError('--adopt-remote only applies to a coordinated (active-active) manifest')
    if coordination_is_active(manifest):
        if not args.dry_run:
            if not getattr(args, '_skip_implicit_control_recovery', False):
                manifest = recover_incomplete_control_manifest_persistence(
                    repo_root, manifest_path, manifest
                )
            replay_tip = ref_tip(repo_root, manifest['integration']['branch'])
            if replay_tip:
                restore_control_manifest_after_integration_rebuild(
                    repo_root,
                    manifest_path,
                    manifest,
                    replay_tip,
                    'control',
                    reason='publish integration control manifest',
                    command='syncwheel int push',
                )
        integration = manifest['integration']
        config = coordination_config(manifest)
        coordinated_push_remote(args, config)
        integration_ref = f"refs/heads/{integration['branch']}"
        changed_refs = {integration_ref: ref_tip(repo_root, integration['branch'])}
        adopt_remote = getattr(args, 'adopt_remote', None)
        if adopt_remote and not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', adopt_remote):
            raise SyncwheelError('--adopt-remote needs the full commit id of the remote tip')
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
                adopt_remote_tips=(
                    {integration_ref: adopt_remote.lower()} if adopt_remote else None
                ),
            )
        except SyncwheelError as exc:
            failure = coordinated_publish_command_failure(
                repo_root, config, exc, 'int push'
            )
            if failure is None:
                raise
            raise failure from exc
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
    integration = manifest['integration']
    remote = args.remote or manifest['defaults']['publication_remote']
    push_args = push_args_with_options(args)
    ref = f"refs/heads/{integration['branch']}"
    tip = ref_tip(repo_root, integration['branch'])
    command = ['git', 'push', *push_args, remote, f'{tip}:{ref}']
    if args.dry_run:
        refuse_local_only_manifest_push(repo_root, manifest, command, remote)
        print(quoted(['git', 'push', *push_args, remote, integration['branch']]))
        return 0
    run_authorized_push(
        repo_root, command, remote, [ref], manifest=manifest,
    )
    print(quoted(command))
    append_ledger_event(
        repo_root,
        'integration_pushed',
        {
            'branch': integration['branch'],
            'remote': remote,
            'tip': tip,
        },
        manifest_path,
    )
    return 0


def command_int_git(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    branch = manifest['integration']['branch']
    git_args = passthrough_args(args.git_args)
    if not git_args:
        raise SyncwheelError('int git requires git arguments after --')
    # Reject unsafe commands before an optional checkout can be created.
    refusal = int_git_passthrough_refusal(repo_root, git_args, branch)
    if refusal:
        raise SyncwheelError(refusal)
    worktree = resolve_git_worktree(repo_root, branch, manifest, args.worktree, args.auto_worktree)
    refusal = int_git_passthrough_refusal(worktree, git_args, branch)
    if refusal:
        raise SyncwheelError(refusal)
    started_on_branch = get_current_branch(worktree) == branch
    _, command, _ = split_git_passthrough(git_args)
    if command in GIT_INTEGRATION_REF_COMMANDS:
        # One passthrough can land many commits (cherry-pick, rebase, am), so the
        # authorization lasts for this process. Output is captured: no editor can open.
        with ref_move_authorization(repo_root, reusable=True) as authorization:
            result = run(
                ['git', *git_args], cwd=worktree, check=False, authorize=False,
                env={MANAGED_REF_MOVE_AUTH_ENV: authorization, 'GIT_EDITOR': 'true'},
            )
    else:
        # Other Git commands cannot borrow a generated or inherited ref token.
        result = run(
            ['git', *git_args], cwd=worktree, check=False, authorize=False,
            env={MANAGED_REF_MOVE_AUTH_ENV: ''},
        )
    if result.stdout:
        print(result.stdout, end='')
    if result.stderr:
        print(result.stderr, end='', file=sys.stderr)
    current = get_current_branch(worktree)
    # A paused rebase detaches HEAD on purpose; its --continue or --abort returns.
    if started_on_branch and current != branch and interrupted_git_operation(worktree) is None:
        print(
            f'error: int git left {worktree} on {current} instead of '
            f'the integration branch {branch}; switch it back with '
            f'`syncwheel int git -- switch {branch}`',
            file=sys.stderr,
        )
        return result.returncode or 2
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
    manifest, path = _manifest_storage.load_manifest(repo_root, path)
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








def control_recovery_pending(repo_root, manifest_path):
    # A preview must not acquire a write lock, repair/truncate a ledger tail or
    # create metadata. Incomplete input is held for explicit ledger recovery.
    pending = _manifest_storage.pending_control_manifest_intents(_manifest_storage.load_ledger_events(repo_root, manifest_path))
    if not pending:
        return None
    if len(pending) != 1 or not _manifest_storage.explicit_control_recovery_intent(pending[0]['payload']):
        raise SyncwheelError('control recovery refuses unrelated or ambiguous pending intents')
    return pending[0]['payload']


def control_recovery_observation(repo_root):
    paths = {
        path: checkout_path_observation(repo_root, path)['fingerprint']
        for path in ('.syncwheel/manifest.json', '.gitignore')
    }
    return {
        'head': ref_tip(repo_root, 'HEAD'),
        'branch': get_current_branch(repo_root),
        'indexTree': git(repo_root, 'write-tree').stdout.strip(),
        'status': git(repo_root, 'status', '--porcelain', '--untracked-files=all').stdout,
        'refs': git(repo_root, 'for-each-ref', '--format=%(refname) %(objectname)').stdout,
        'paths': paths,
    }


def control_recovery_plan(repo_root, manifest_path, backup_ref):
    """Prove a local failed replay before restoring its declared control input."""
    if Path(manifest_path).resolve() != _manifest_storage.integration_manifest_path(repo_root).resolve():
        raise SyncwheelError('control recovery requires the canonical repository manifest')
    current, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if not current:
        raise SyncwheelError('control recovery requires a readable replay manifest')
    branch = current['integration']['branch']
    backup_ref = backup_ref.removeprefix('refs/heads/')
    if not backup_ref.startswith(f'backup/{branch}-before-syncwheel-'):
        raise SyncwheelError('control recovery requires an explicit native integration backup ref')
    source = ref_tip(repo_root, f'refs/heads/{backup_ref}')
    if not source:
        raise SyncwheelError('control recovery backup ref is missing')
    original = manifest_from_tree(repo_root, source, _manifest_storage.integration_manifest_path(repo_root))
    if original is None:
        raise SyncwheelError('control recovery backup has no tracked manifest')
    desired = normalize_manifest_data(original, manifest_path)
    if desired['integration']['branch'] != branch:
        raise SyncwheelError('control recovery backup names a different integration branch')
    if any(m.get('syncwheel_tracking') != SYNCWHEEL_TRACKING_GIT_TRACKED
           or m.get('repository_mode') != 'delivery' or coordination_is_active(m)
           for m in (current, desired)):
        raise SyncwheelError('control recovery supports local git-tracked delivery manifests only')
    before = control_recovery_observation(repo_root)
    if before['branch'] != branch:
        raise SyncwheelError('control recovery requires the integration checkout')
    if git(repo_root, 'diff', '--cached', '--name-only').stdout.strip():
        raise SyncwheelError('control recovery refuses a staged index')
    if regular_checkout_path_bytes(repo_root, '.syncwheel/manifest.json') != git(
        repo_root, 'show', f"{before['head']}:.syncwheel/manifest.json"
    ).stdout.encode():
        raise SyncwheelError('control recovery refuses an edited replay manifest')
    changed = [line[3:] for line in before['status'].splitlines()]
    if any(path != '.gitignore' for path in changed):
        raise SyncwheelError('control recovery refuses unrelated checkout changes')
    ignore_changed = bool(changed)
    if ignore_changed:
        baseline = git(repo_root, 'show', f"{before['head']}:.gitignore").stdout
        expected, _ = replace_managed_block(
            baseline, SYNCWHEEL_GITIGNORE_MARKER, SYNCWHEEL_GITIGNORE_END_MARKER,
            syncwheel_gitignore_patterns(syncwheel_worktree_root(desired)),
            all_syncwheel_managed_patterns(syncwheel_worktree_root(desired)),
        )
        if regular_checkout_path_bytes(repo_root, '.gitignore') != expected.encode():
            raise SyncwheelError('control recovery refuses unrelated ignore changes')
    replay = materialize_integration_replay(repo_root, desired)
    differences = git(repo_root, 'diff', '--name-only', replay, before['head']).stdout.splitlines()
    if any(path not in {'.syncwheel/manifest.json', '.gitignore'} for path in differences):
        raise SyncwheelError('control recovery replay does not reproduce the current product tree')
    if control_recovery_observation(repo_root) != before:
        raise SyncwheelError('control recovery inputs changed during proof')
    pending = control_recovery_pending(repo_root, manifest_path)
    replay_tip = pending['replay_tip'] if pending else before['head']
    identity_version = (pending.get('control_identity', CONTROL_MANIFEST_LEGACY_IDENTITY_VERSION)
                        if pending else CONTROL_MANIFEST_EXPLICIT_RECOVERY_IDENTITY_VERSION)
    control_commit = materialize_control_manifest_commit(repo_root, desired, replay_tip, identity_version)
    if pending and (
        pending.get('command') != 'syncwheel manifest recover-control'
        or pending.get('integration_branch') != branch
        or pending.get('expected_manifest_digest') != manifest_digest(desired)
        or pending.get('expected_control_commit') != control_commit
        or before['head'] not in {replay_tip, control_commit}
    ):
        raise SyncwheelError('control recovery pending intent differs from the explicit backup proof')
    if control_recovery_observation(repo_root) != before:
        raise SyncwheelError('control recovery inputs changed during object preparation')
    plan = {
        'schemaVersion': 2, 'kind': 'controlManifestRecoveryPlan',
        'backupRef': backup_ref, 'backupCommit': source, 'observation': before,
        'replayCommit': replay, 'manifestDigestBefore': manifest_digest(current),
        'manifestDigestAfter': manifest_digest(desired), 'commitManagedIgnore': False,
        'preservedManagedIgnore': ignore_changed, 'expectedControlCommit': control_commit,
        'alreadyRestored': control_commit == before['head'],
        'pendingIntent': pending, 'controlReplayTip': replay_tip,
        'controlIdentity': identity_version,
    }
    plan['planDigest'] = canonical_json_digest(plan)
    return plan, desired


def control_recovery_checkpoint():
    """Fault-injection seam before a reviewed control recovery mutation."""
    return None


def command_manifest_recover_control(args):
    repo_root = resolve_repo_root(args.repo)
    manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
    plan, desired = control_recovery_plan(repo_root, manifest_path, args.from_backup)
    if args.apply:
        if not args.reason or not args.reason.strip():
            raise SyncwheelError('control recovery apply requires --reason')
        if not args.plan_digest or args.plan_digest != plan['planDigest']:
            raise SyncwheelError('control recovery requires the exact current --plan-digest')
        def require_frozen_source():
            if control_recovery_observation(repo_root) != plan['observation']:
                raise SyncwheelError('control recovery inputs changed before apply')
        control_recovery_checkpoint()
        require_frozen_source()
        if control_recovery_pending(repo_root, manifest_path) != plan['pendingIntent']:
            raise SyncwheelError('control recovery pending intent changed before apply')
        transaction = _manifest_storage.active_manifest_write_transaction(manifest_path)
        if transaction is None:
            raise SyncwheelError('control recovery requires its native manifest transaction')
        previous = transaction.get('preserveGitignore')
        previous_operation = transaction.get('explicitControlRecoveryOperation')
        transaction['preserveGitignore'] = True
        pending = plan['pendingIntent']
        if pending:
            transaction['explicitControlRecoveryOperation'] = pending['operation_id']
        try:
            if not plan['alreadyRestored'] or pending:
                restore_control_manifest_after_integration_rebuild(
                    repo_root, manifest_path, desired, plan['controlReplayTip'],
                    'control-recovery', reason=args.reason,
                    command='syncwheel manifest recover-control',
                    operation_id=pending['operation_id'] if pending else None,
                    identity_version=plan['controlIdentity'],
                    preserved_ignore=plan['observation']['paths']['.gitignore'],
                    pre_cas_check=require_frozen_source,
                )
        finally:
            if previous is None:
                transaction.pop('preserveGitignore', None)
            else:
                transaction['preserveGitignore'] = previous
            if previous_operation is None:
                transaction.pop('explicitControlRecoveryOperation', None)
            else:
                transaction['explicitControlRecoveryOperation'] = previous_operation
        # The isolated control CAS owns the manifest. Never let transaction exit
        # stage a preserved or late-edited ignore file through the ordinary writer.
        transaction = _manifest_storage.active_manifest_write_transaction(manifest_path)
        if transaction is not None:
            transaction['manifestWritten'] = False
            transaction['gitignoreWritten'] = False
        observed = control_recovery_observation(repo_root)
        expected_refs = plan['observation']['refs'].replace(
            f"refs/heads/{plan['observation']['branch']} {plan['observation']['head']}\n",
            f"refs/heads/{plan['observation']['branch']} {plan['expectedControlCommit']}\n",
        )
        if (observed['head'] != plan['expectedControlCommit']
                or observed['branch'] != plan['observation']['branch']
                or observed['refs'] != expected_refs
                or observed['indexTree'] != ref_tree(repo_root, plan['expectedControlCommit'])
                or observed['status'] != plan['observation']['status']
                or observed['paths']['.gitignore'] != plan['observation']['paths']['.gitignore']
                or regular_checkout_path_bytes(repo_root, '.syncwheel/manifest.json')
                   != _manifest_storage.canonical_manifest_file_text(desired).encode()):
            raise SyncwheelError(
                'control recovery inputs changed after control publication; '
                'concurrent state preserved; inspect the control receipt before recovery'
            )
    print(json.dumps({**plan, 'applied': bool(args.apply)}, indent=2))
    return 0


class _StackRecoveryPorts:
    """Bridge local withdrawal proof to native manifest, ledger and Git services."""

    def __init__(self, root, path, common):
        self.root = root
        self.path = path
        self.common = common
        self.store = _manifest_store()
        self.env = managed_process_env({
            'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1',
            'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': '',
        }, authorize=False)
        actual = Path(os.fsdecode(self.binary_git('rev-parse', '--absolute-git-dir').strip()))
        if actual != common or common.resolve() != common:
            raise SyncwheelError('stack withdrawal requires the original primary Git administration')
        self.index = common / 'index'
        self.registry = governed_worktree_registry_path(root)
        self.native_refs_prepared = False

    def binary_git(self, *arguments, allowed=(0,), index=None):
        env = self.env.copy()
        options = ['-c', 'core.fsmonitor=false']
        if index is not None:
            env['GIT_INDEX_FILE'] = str(index)
            # A temporary proof index must never invoke post-index-change for
            # an index that is not the repository's real one.
            options.extend(('-c', 'core.hooksPath=/dev/null'))
        result = subprocess.run(['git', *options, *arguments], cwd=self.root,
                                env=env, capture_output=True)
        if result.returncode not in allowed:
            raise SyncwheelError(result.stderr.decode(errors='replace').strip()
                                 or f'Git {arguments[0]} failed ({result.returncode})')
        return result.stdout

    def git(self, *arguments, **kwargs):
        return os.fsdecode(self.binary_git(*arguments, **kwargs))

    def require_ledger_paths(self):
        ledger = _manifest_storage.ledger_root(self.root, self.path)
        directory = _manifest_storage.ledger_events_dir(self.root, self.path)
        for item in (ledger, directory):
            require_private_tracking_path(self.root, item, directory=True)
        paths = [ledger / 'ledger.lock', _manifest_storage.ledger_checkpoint_path(self.root, self.path),
                 directory / '000001.jsonl', *sorted(directory.glob('*.jsonl'))]
        for item in paths:
            require_private_tracking_path(self.root, item)
        self.store.require_ignored_ledger_paths(self.root, list(dict.fromkeys(paths)))

    def ledger_segments(self, *, raw=False):
        self.require_ledger_paths()
        directory = _manifest_storage.ledger_events_dir(self.root, self.path)
        result = {}
        for item in sorted(directory.glob('*.jsonl')):
            before, value = retire_copy_observed_file(item)
            if before is None or not stat.S_ISREG(before['stat'][2]):
                raise SyncwheelError('native ledger segment changed during stack withdrawal')
            result[item.name] = value if raw else {'size': len(value), 'sha256': before['sha256']}
        return result

    def pending_index_lock(self):
        return os.path.lexists(self.index.with_name('index.lock'))

    def require_supported_git(self, own_lock):
        from syncwheel_retire_copy import same_inode
        if (
            any(os.path.lexists(self.common / part) for part in ('info/grafts', 'shallow'))
            or any(name in os.environ for name in ('GIT_GRAFT_FILE', 'GIT_REPLACE_REF_BASE', 'GIT_SHALLOW_FILE'))
            or self.git('for-each-ref', '--format=%(refname)', 'refs/replace').strip()
        ):
            raise SyncwheelError('stack withdrawal refuses Git history replacements, grafts, or shallow history')
        for name in ('HEAD.lock', 'MERGE_HEAD', 'MERGE_MSG', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                     'rebase-merge', 'rebase-apply', 'sequencer', 'BISECT_LOG', 'AUTO_MERGE'):
            # Git's prepared native ref transaction can temporarily hold HEAD.lock
            # when updating the checked-out branch. It stays forbidden elsewhere.
            if name == 'HEAD.lock' and self.native_refs_prepared:
                continue
            if os.path.lexists(self.common / name):
                raise SyncwheelError(f'stack withdrawal refuses an interrupted Git operation ({name})')
        if self.pending_index_lock() and (own_lock is None or not same_inode(self.index.with_name('index.lock'), own_lock)):
            raise SyncwheelError('Git index is locked by another writer')
        if self.git('config', '--get-regexp', r'^filter\.', allowed=(0, 1)).strip():
            raise SyncwheelError('stack withdrawal refuses configured Git filters during checkout proof')
        attributes = self.binary_git('check-attr', '-z', '--all', '--', STACK_RECOVERY_MANIFEST).split(b'\0')
        if any(attribute in {b'filter', b'working-tree-encoding', b'text', b'eol', b'ident', b'crlf'}
               for attribute in attributes[1::3]):
            raise SyncwheelError('stack withdrawal refuses conversion attributes on the canonical manifest')
        if self.git('rev-parse', '--shared-index-path').strip() or self.git(
            'config', '--type=bool', '--get', 'core.sparseCheckout', allowed=(0, 1)
        ).strip() == 'true':
            raise SyncwheelError('stack withdrawal refuses sparse or split indexes')

    def observe(self, own_lock):
        from syncwheel_retire_copy import checked_parent, directory_key
        self.require_supported_git(own_lock)
        direct_head = self.git('symbolic-ref', '--quiet', 'HEAD', allowed=(0, 1)).strip()
        if not direct_head.startswith('refs/heads/'):
            raise SyncwheelError('stack withdrawal requires a primary branch checkout')
        branch = direct_head.removeprefix('refs/heads/')
        head = self.direct_ref(direct_head)
        if not head:
            raise SyncwheelError('primary integration ref is missing')
        index, _ = retire_copy_observed_file(self.index, allow_link=own_lock is not None)
        if index is None:
            raise SyncwheelError('primary Git index is missing')
        if any(not line.startswith(b'H ') for line in
               self.binary_git('ls-files', '-v', '-z').split(b'\0') if line):
            raise SyncwheelError('stack withdrawal refuses an index with special entry flags')
        entries = self.index_entries()
        registry, _ = retire_copy_observed_file(self.registry)
        require_private_tracking_path(self.common, self.registry)
        with checked_parent(self.root, STACK_RECOVERY_MANIFEST) as (parent, leaf, parents):
            file, _ = retire_copy_observed_file(Path(f'/proc/self/fd/{parent}') / leaf)
            if file and not stat.S_ISREG(file['stat'][2]):
                raise SyncwheelError('stack withdrawal requires an original regular manifest file')
        status = [row for row in self.git(
            'status', '--porcelain=v1', '-z', '--untracked-files=all',
            '--ignore-submodules=none', '--no-renames',
        ).split('\0') if row]
        if retire_copy_observed_file(self.index, allow_link=own_lock is not None)[0] != index:
            raise SyncwheelError('primary Git index changed during stack withdrawal observation')
        return {
            'head': head, 'branch': branch, 'ref': direct_head, 'index': index,
            'status': status, 'indexEntriesDigest': canonical_json_digest(entries),
            'registry': registry, 'manifest': file,
            'manifestParents': parents, 'rootIdentity': directory_key(self.root),
            'commonIdentity': directory_key(self.common),
        }

    def index_entries(self, index=None):
        rows = []
        for row in self.git('ls-files', '--stage', '-z', index=index).split('\0'):
            if not row:
                continue
            header, name = row.split('\t', 1)
            mode, oid, stage = header.split()
            if stage != '0':
                raise SyncwheelError('stack withdrawal refuses a conflicted index')
            rows.append([name, mode, oid])
        return sorted(rows)

    def tree_entries(self, commit):
        rows = []
        for row in self.git('ls-tree', '-r', '-z', commit).split('\0'):
            if row:
                header, name = row.split('\t', 1)
                mode, _, oid = header.split()
                rows.append([name, mode, oid])
        return sorted(rows)

    def tree_entries_digest(self, commit):
        return canonical_json_digest(self.tree_entries(commit))

    def build_index(self, commit, index):
        self.git('-c', 'core.splitIndex=false', 'read-tree', commit, index=index)
        if self.index_entries(index=index) != self.tree_entries(commit):
            raise SyncwheelError('prepared withdrawal index does not match its isolated control commit')

    def head_manifest(self, head):
        return self.binary_git('show', f'{head}:{STACK_RECOVERY_MANIFEST}')

    def direct_ref(self, ref):
        if self.git('symbolic-ref', '--quiet', ref, allowed=(0, 1)).strip():
            raise SyncwheelError(f'stack withdrawal refuses symbolic ref {ref!r}')
        return self.git('rev-parse', '--verify', '--quiet', '--end-of-options',
                        f'{ref}^{{commit}}', allowed=(0, 1)).strip() or None

    def commit(self, expression):
        return self.git('rev-parse', '--verify', '--end-of-options', f'{expression}^{{commit}}').strip()

    def full_declared_commit(self, oid):
        if not isinstance(oid, str) or not RECOVERY_COMMIT_ID.fullmatch(oid) or self.commit(oid) != oid:
            raise SyncwheelError('the native withdrawal ledger contains an unresolved commit generation')
        return oid

    def require_mode(self, head, dirty, branch):
        if any(
            item.get('repository_mode') != 'delivery'
            or item.get('syncwheel_tracking') != SYNCWHEEL_TRACKING_GIT_TRACKED
            or coordination_is_active(item)
            or item['integration']['branch'] != branch
            for item in (head, dirty)
        ):
            raise SyncwheelError('stack withdrawal requires local git-tracked delivery on the primary integration branch')
        if os.path.lexists(coordination_publication_lock_path(self.root)):
            raise SyncwheelError('stack withdrawal refuses a pending coordination publication')
        _, coordination = coordination_profile(self.root)
        if local_lease_is_active(coordination):
            raise SyncwheelError('stack withdrawal refuses an active local coordination lease')

    def require_unpublished_tracking(self, branch):
        tracking = self.git('for-each-ref', '--format=%(refname)', 'refs/remotes').splitlines()
        if any(ref.removeprefix('refs/remotes/').partition('/')[2] == branch for ref in tracking):
            raise SyncwheelError('local remote-tracking refs show prior stack publication')

    def unresolved(self, stack, events):
        from syncwheel_stack_adoption import pending_adoptions
        if pending_adoptions(events) or unmatched_stack_create_operations(self.root, self.path, stack):
            raise SyncwheelError('a native stack creation or adoption is unresolved')
        if pending_stack_close_operation(self.root, self.path, stack):
            raise SyncwheelError('a native close is unresolved for this stack')

    def verify_anchors(self, anchors):
        if any(self.direct_ref(ref) != expected for ref, expected in anchors.items()):
            raise SyncwheelError('withdrawal recovery ref changed or is missing')

    def anchors_published(self, anchors):
        actual = {ref: self.direct_ref(ref) for ref in anchors}
        if all(tip is None for tip in actual.values()):
            return False
        if actual != anchors:
            raise SyncwheelError('withdrawal recovery refs are incomplete or differ from the reviewed plan')
        return True

    def publish_refs(self, plan, control, anchors, check_prepared):
        if self.anchors_published(anchors):
            raise SyncwheelError('an unjournaled withdrawal recovery ref already exists')
        primary_ref = plan['observation']['ref']
        before = plan['beforeHead']
        primary_operation = (
            f'verify {primary_ref} {before}' if control == before
            else f'update {primary_ref} {control} {before}'
        )
        records = [
            f"verify {plan['sourceRef']} {plan['sourceTip']}",
            *(f'create {ref} {commit}' for ref, commit in anchors.items()),
            primary_operation,
        ]
        # Native reference-transaction hooks receive the scoped authorization.
        # The no-write diagnostics context is restored before further observation.
        def verify_prepared_checkout():
            self.native_refs_prepared = True
            try:
                check_prepared()
            finally:
                self.native_refs_prepared = False

        diagnostic_scope = _READONLY_DIAGNOSTICS.set(False)
        try:
            require_private_tracking_path(self.common, ref_auth_dir(self.root), directory=True)
            with ref_move_authorization(self.root) as nonce:
                env = {
                    MANAGED_REF_MOVE_AUTH_ENV: nonce, 'GIT_OPTIONAL_LOCKS': '0',
                    'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': '', 'GIT_NO_REPLACE_OBJECTS': '1',
                }
                _StackRepository(self.root).capture_ref_transaction(
                    records, env, checked_out_ref=primary_ref, check_prepared=verify_prepared_checkout,
                    fsync_refs=True,
                )
        finally:
            _READONLY_DIAGNOSTICS.reset(diagnostic_scope)

    def stale_lock_reason(self, owner):
        return governed_worktree_stale_lock_reason(owner)

    def lock_metadata(self):
        return governed_worktree_lock_metadata()

    def write_private_json(self, path, payload):
        require_private_tracking_path(self.common, path)
        atomic_write_private_json(path, payload)
        fsync_directory_path(path.parent)

    def refresh_checkpoint(self, operation, plan, token):
        from syncwheel_ledger import reduce_ledger_state
        checkpoint = _manifest_storage.ledger_checkpoint_path(self.root, self.path)

        def expected_image():
            events = operation.events()
            operation.require_ledger(plan, events, token)
            state = reduce_ledger_state(events)
            return (json.dumps(state, indent=2, sort_keys=True) + '\n').encode()

        if retire_copy_observed_file(checkpoint)[1] == expected_image():
            return
        with ledger_write_lock(self.root, self.path):
            payload = expected_image()
            if retire_copy_observed_file(checkpoint)[1] != payload:
                # The native checkpoint is a projection of the authoritative
                # event frames under this same ledger lock.
                _manifest_storage.write_ledger_checkpoint(self.root, None, self.path)

    def persist_stack_withdrawal(self, operation, plan, desired, token, intent):
        branch = plan['observation']['branch']
        require_private_tracking_path(self.common, governed_worktree_lock_path(self.root))
        require_private_tracking_path(self.common, control_manifest_lock_path(self.root, branch))
        with manifest_write_transaction(self.root, self.path, 'manifest-recover-stack', commit_tracked_changes=False):
            transaction = _manifest_storage.active_manifest_write_transaction(self.path)
            transaction['preserveGitignore'] = True
            with control_manifest_branch_lock(self.root, branch), governed_worktree_registry_lock(self.root):
                return self.store.persist_explicit_stack_withdrawal(
                    self.root, self.path, desired, plan, token, operation, intent,
                )


def command_manifest_recover_stack(args):
    try:
        root, path = resolve_command_manifest_selection(args)
        root = Path(root)
        canonical = primary_authority_manifest_path(root)
        path = Path(os.path.abspath(path))
        if (args.personal or path != canonical or root != canonical.parent.parent
                or path.resolve() != path):
            raise SyncwheelError('manifest recover-stack requires the canonical primary checkout and manifest')
        stack = safe_ref_segment(args.stack)
        if stack != args.stack:
            raise SyncwheelError('manifest recover-stack requires the exact stack id')
        common = git_common_dir(root)
        operation: StackRecovery = StackRecovery(_StackRecoveryPorts(root, path, common), root, path,
                                                 common, stack, args.reason.strip())
        result = operation.execute_stack_withdrawal(apply=args.apply, token=args.plan_digest)
    except (OSError, UnicodeError) as exc:
        raise SyncwheelError(f'stack withdrawal could not complete: {exc}') from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


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




def git_path_is_tracked(repo_root, path):
    relative = _manifest_storage.repo_relative_path(repo_root, path)
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


def tracking_manifest_scope(repo_root, manifest_path):
    """Classify the selected path before following any metadata symlinks."""
    checkout = Path(repo_root).resolve()
    shared_path = primary_authority_manifest_path(checkout)
    primary = shared_path.parent.parent
    selected = Path(os.path.abspath(manifest_path))
    if selected == shared_path:
        return 'shared', primary, primary
    for owner in dict.fromkeys((checkout, primary)):
        if (
            selected.parent == owner / '.syncwheel' / 'manifests'
            and selected.name.endswith('.local.json')
        ):
            return 'personal', owner, primary
    return 'alternate', checkout, primary


def require_private_tracking_path(owner, path, *, directory=False):
    """Refuse redirects or special files anywhere in a private metadata path."""
    relative = path.relative_to(owner)
    candidate = owner
    for part in relative.parts:
        candidate /= part
        try:
            observed = candidate.lstat()
        except FileNotFoundError:
            return
        mode = observed.st_mode
        if stat.S_ISLNK(mode):
            raise SyncwheelError(f'private tracking metadata cannot be a symbolic link: {candidate}')
        if directory or candidate != path:
            if not stat.S_ISDIR(mode):
                raise SyncwheelError(f'private tracking metadata directory is not a directory: {candidate}')
        elif not stat.S_ISREG(mode) or observed.st_nlink > 1:
            raise SyncwheelError(f'private tracking metadata must be a regular, unlinked file: {candidate}')


def tracking_set_selection(args):
    repo_value = args.repo or os.environ.get(ENV_REPO)
    repo_root = resolve_repo_root(repo_value)
    manifest_path = resolve_manifest_path(repo_root, repo_value, args.manifest, args.personal)
    scope, owner, primary = tracking_manifest_scope(repo_root, manifest_path)
    if scope == 'personal':
        manifest_path = Path(os.path.abspath(manifest_path))
        require_private_tracking_path(owner, manifest_path)
    elif scope != 'shared' or repo_root.resolve() != primary:
        raise SyncwheelError(
            'tracking migration requires the canonical manifest in the primary checkout; '
            'select a personal manifest to change private tracking'
        )
    else:
        validate_selected_primary_authority_path(repo_root, manifest_path)
    return scope, owner, Path(os.path.abspath(manifest_path)), primary


def syncwheel_tracking_report(repo_root, manifest_path):
    scope, owner, _ = tracking_manifest_scope(repo_root, manifest_path)
    if scope == 'personal':
        manifest_path = Path(os.path.abspath(manifest_path))
        require_private_tracking_path(owner, manifest_path)
        repo_root = owner
    validate_selected_primary_authority_path(repo_root, manifest_path)
    manifest, manifest_path = _manifest_storage.load_manifest(repo_root, manifest_path)
    manifest_present = manifest is not None
    tracking = manifest.get('syncwheel_tracking') if manifest else None
    worktree_root = syncwheel_worktree_root(manifest)
    effective_root = resolve_worktree_root_path(repo_root, worktree_root)
    info_exclude = git_info_exclude_path(repo_root)
    gitignore = repo_root / '.gitignore'
    manifest_relative = _manifest_storage.repo_relative_path(repo_root, manifest_path)
    manifest_tracked = git_path_is_tracked(repo_root, manifest_path)
    warnings = []
    actions = []

    if scope != 'shared':
        if scope == 'personal':
            if not manifest_present:
                actions.append('select an existing personal manifest')
            elif manifest_tracked or git(
                repo_root, 'check-ignore', '--quiet', '--no-index', '--', manifest_relative, check=False,
            ).returncode != 0:
                warnings.append('personal tracking requires an untracked, already ignored manifest')
            elif tracking is None:
                actions.append('set tracking on the selected personal manifest with --apply')
        else:
            actions.append('select the canonical or a personal manifest to change tracking')
    elif not manifest_present:
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

    policy, provenance = (
        resolve_repository_authority(repo_root, manifest, manifest_path) if manifest_present else (None, None)
    )
    return {
        'tracking_scope': scope,
        'repo_root': str(repo_root),
        'manifest_path': str(manifest_path),
        'manifest_present': manifest_present,
        'manifest_in_repo': manifest_relative is not None,
        'manifest_tracked': manifest_tracked,
        'syncwheel_tracking': tracking,
        'syncwheel_tracking_present': tracking is not None,
        'authority': policy,
        'authority_provenance': provenance,
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
    print(f"tracking_scope: {report['tracking_scope']}")
    print(f"repo: {report['repo_root']}")
    print(f"manifest: {report['manifest_path'] if report['manifest_present'] else 'missing'}")
    print(f"syncwheel_tracking: {report['syncwheel_tracking'] or 'missing'}")
    print(f"authority: {format_authority_policy(report['authority'])}")
    if report['authority_provenance']:
        print(f"authority_source: {report['authority_provenance']['source']}")
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
    validate_selected_primary_authority_path(repo_root, manifest_path)
    manifest, manifest_path = _manifest_storage.load_manifest(repo_root, manifest_path)
    present = bool(manifest and 'authority' in manifest)
    policy, provenance = (
        resolve_repository_authority(repo_root, manifest, manifest_path) if manifest else (None, None)
    )
    warnings = []
    if provenance and provenance['source'] == 'default':
        warnings.append(
            'authority is not declared; agents must treat this repository as '
            f'{AUTHORITY_MODE_HUMAN_GATED} until a maintainer sets it'
        )
    return {
        'repo_root': str(repo_root),
        'manifest_path': str(manifest_path),
        'manifest_present': manifest is not None,
        'authority': policy,
        'authority_provenance': provenance,
        'authority_present': present,
        'warnings': warnings,
    }


def print_authority_report(report):
    print(f"repo: {report['repo_root']}")
    print(f"manifest: {report['manifest_path'] if report['manifest_present'] else 'missing'}")
    print(f"authority: {format_authority_policy(report['authority'])}")
    if report['authority_provenance']:
        print(f"authority_source: {report['authority_provenance']['source']}")
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
    manifest, manifest_path = _manifest_storage.load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    ensure_manifest_in_repo(repo_root, manifest_path)
    inherited, provenance = resolve_repository_authority(repo_root, {}, manifest_path)
    proposed = normalize_authority_policy(
        {'mode': args.mode, 'allow': list(args.allow or []), 'deny': []}, 'requested'
    )
    shared_path = Path(provenance['shared_manifest_path'])
    selected_is_shared = provenance['selected_manifest_path'] == str(shared_path)
    lock = (
        channel_mutation_lock(shared_path.parent.parent, shared_path, 'repo-authority')
        if args.apply and not selected_is_shared else contextlib.nullcontext()
    )
    with lock:
        if not selected_is_shared:
            resolve_repository_authority(repo_root, {**manifest, 'authority': proposed}, manifest_path)
        if not args.apply:
            if not selected_is_shared and 'authority' in manifest:
                print(f"current_declared_authority: {format_authority_policy(manifest_authority(manifest))}")
            else:
                print(f"current_authority: {format_authority_policy(inherited)}")
            print(f"proposed_authority: {format_authority_policy(proposed)}")
            print('dry_run: pass --apply to write this policy')
            return 0
        manifest['authority'] = proposed
        save_manifest_with_ledger(
            repo_root, manifest_path, manifest, 'repo_authority_set', {'authority': proposed}
        )
        report = authority_report(repo_root, manifest_path)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_authority_report(report)
    return 0


def restore_pending_mode_migration(root, path, pending_path):
    pending = json.loads(pending_path.read_text())
    original = Path(pending['archive']) / 'manifest.json'
    original_data = json.loads(original.read_text())
    dirty = {entry['path'] for entry in journal_status_entries(root)}
    if dirty - {'.syncwheel/manifest.json', '.gitignore'}:
        raise SyncwheelError('mode migration recovery refuses unrelated working changes: ' + ', '.join(sorted(dirty)))
    observed, _ = _manifest_storage.load_manifest(root, path)
    if manifest_digest(observed) not in {pending['beforeDigest'], pending['afterDigest']}:
        raise SyncwheelError('mode migration recovery manifest lease lost')
    if get_current_branch(root) not in {pending['branch'], pending['targetBranch']}:
        raise SyncwheelError('mode migration recovery branch lease lost')
    for branch, tip in pending['branchTips'].items():
        if ref_tip(root, f'refs/heads/{branch}') != tip:
            raise SyncwheelError('mode migration recovery HEAD lease lost')
    guard = pending['guard']
    observed_guard, guard_error = inspect_primary_guard(root)
    if (guard_error and primary_guard_path(root).exists()) or observed_guard not in (guard, pending['targetGuard']):
        raise SyncwheelError('mode migration recovery guard lease lost; pending recovery retained')
    for item in pending['metadataFiles']:
        metadata_path = Path(item['path'])
        if metadata_path.is_symlink() or read_text_if_exists(metadata_path) not in (item['before'], item['after']):
            raise SyncwheelError('mode migration recovery metadata lease lost: ' + str(metadata_path))
    # Retarget first so post-checkout also recognizes the restored branch.
    if guard is None:
        primary_guard_path(root).unlink(missing_ok=True)
    else:
        atomic_write_private_json(primary_guard_path(root), guard)
    with ref_move_authorization(root) as authorization:
        git(root, 'switch', pending['branch'],
            env={MANAGED_REF_MOVE_AUTH_ENV: authorization})
    # Restore through the canonical transaction writer, then retain the exact
    # committed bytes from the initially clean checkout.
    _manifest_storage.save_manifest(path, original_data)
    with ref_move_authorization(root) as authorization:
        git(root, '--literal-pathspecs', 'restore', '--source=' + pending['originalHead'],
            '--worktree', '--', '.syncwheel/manifest.json',
            env={MANAGED_REF_MOVE_AUTH_ENV: authorization})
    for item in pending['metadataFiles']:
        metadata_path = Path(item['path'])
        if item['existed']:
            metadata_path.write_text(item['before'])
        else:
            metadata_path.unlink(missing_ok=True)
    # Keep append-only migration audit evidence local even when restoring a
    # pre-migration checkout that lacked the managed ignore block.
    exclude = git_info_exclude_path(root)
    if exclude and (root / '.syncwheel/ledger').exists():
        text = read_text_if_exists(exclude)
        if '.syncwheel/ledger/' not in text.splitlines():
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(text + ('\n' if text and not text.endswith('\n') else '')
                               + '.syncwheel/ledger/\n')
    atomic_write_private_json(Path(pending['archive']) / 'rollback.json', {
        'restoredBranch': pending['branch'], 'restoredHead': pending['originalHead'],
        'manifestDigest': pending['beforeDigest'],
    })
    pending_path.unlink()


def command_repo_mode_set(args):
    root = resolve_repo_root(args.repo)
    if args.personal or (args.manifest and Path(args.manifest).resolve() != (root / '.syncwheel/manifest.json').resolve()):
        raise SyncwheelError('mode migration requires the shared default manifest')
    path = root / '.syncwheel/manifest.json'
    pending_path = git_common_dir(root) / 'syncwheel' / 'mode-migration-pending.json'
    if pending_path.exists():
        if not args.apply:
            print(json.dumps({'mode': 'plan', 'recovery_required': True,
                              'remedy': 'repeat repo mode set journal with --apply to restore the interrupted migration'}))
            return 0
        restore_pending_mode_migration(root, path, pending_path)
    manifest, path = require_manifest(root, args.repo, args.manifest, args.personal)
    if args.personal or path.resolve() != (root / '.syncwheel/manifest.json').resolve():
        raise SyncwheelError('mode migration requires the shared default manifest')
    worktrees = get_worktrees(root)
    if worktrees and Path(worktrees[0]['path']).resolve() != root.resolve():
        raise SyncwheelError('mode migration requires the clean primary checkout')
    if git(root, 'status', '--porcelain', '--untracked-files=all').stdout.strip():
        raise SyncwheelError('mode migration requires a clean primary checkout')
    if not args.reason.strip():
        raise SyncwheelError('mode migration requires a non-empty --reason')
    if manifest.get('repository_mode') == 'journal':
        raise SyncwheelError('repository is already in journal mode')
    if manifest.get('channels'):
        raise SyncwheelError('close or archive deployment channels before mode migration')
    branch = args.branch
    if run(['git', 'check-ref-format', '--branch', branch], check=False).returncode:
        raise SyncwheelError('invalid journal branch')
    target = ref_tip(root, f'refs/heads/{branch}')
    if target is None or ref_tree(root, target) != ref_tree(root, 'HEAD'):
        raise SyncwheelError('journal target branch must exist with the exact current tree; converge first')
    if not remote_is_configured(root, args.remote):
        raise SyncwheelError('journal remote must be configured')
    occupied = [item for item in worktrees if item.get('branch') == branch
                and Path(item['path']).resolve() != root.resolve()]
    if occupied:
        raise SyncwheelError('journal target branch is checked out in another worktree')
    old = json.loads(path.read_text())
    updated = dict(old)
    dropped = ['integration', 'stacks', 'coordination', 'channels', 'landing']
    for field in dropped:
        updated.pop(field, None)
    updated.update(version=args.manifest_version, repository_mode='journal', journal={
        'branch': branch, 'remote': args.remote, 'include': args.include or ['**'],
        'exclude': args.exclude or ['var/**', '.syncwheel/ledger/**', '.syncwheel/*.local.json'],
        'max_file_bytes': args.max_file_bytes, 'interval': args.interval,
    })
    # Validate the candidate through the canonical loader before touching state.
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as candidate:
        json.dump(updated, candidate)
        candidate.flush()
        normalized, _ = _manifest_storage.load_manifest(root, candidate.name)
    archive_id = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    archive = git_common_dir(root) / 'syncwheel' / 'mode-migrations' / archive_id
    refs = {f'refs/archive/syncwheel-mode/{archive_id}/head': ref_tip(root, 'HEAD')}
    for stack in manifest.get('stacks', []):
        for index, commit in enumerate(stack.get('commits') or []):
            refs[f'refs/archive/syncwheel-mode/{archive_id}/stacks/{stack["id"]}/{index}'] = commit_full_sha(root, commit)
        tip = ref_tip(root, stack['branch'])
        if tip:
            refs[f'refs/archive/syncwheel-mode/{archive_id}/branches/{stack["id"]}'] = tip
    for ref, tip in refs.items():
        existing = ref_tip(root, ref)
        if existing and existing != tip:
            raise SyncwheelError('migration archive ref collision: ' + ref)
    metadata_files = []
    worktree_root = syncwheel_worktree_root(normalized)
    git_tracked = normalized.get('syncwheel_tracking') == SYNCWHEEL_TRACKING_GIT_TRACKED
    exclusion_targets = [
        (git_info_exclude_path(root), SYNCWHEEL_LOCAL_EXCLUDE_MARKER, SYNCWHEEL_LOCAL_EXCLUDE_END_MARKER,
         [] if git_tracked else syncwheel_local_exclude_patterns(worktree_root)),
    ]
    if git_tracked:
        exclusion_targets.insert(0, (
            root / '.gitignore', SYNCWHEEL_GITIGNORE_MARKER, SYNCWHEEL_GITIGNORE_END_MARKER,
            syncwheel_gitignore_patterns(worktree_root),
        ))
    for metadata_path, marker, end, patterns in exclusion_targets:
        if metadata_path is None:
            continue
        if metadata_path.is_symlink():
            raise SyncwheelError('mode migration refuses symlink metadata: ' + str(metadata_path))
        before = read_text_if_exists(metadata_path)
        after, _ = replace_managed_block(before, marker, end, patterns,
                                         all_syncwheel_managed_patterns(worktree_root))
        metadata_files.append({'path': str(metadata_path), 'before': before,
                               'after': after, 'existed': metadata_path.exists()})
    guard, guard_error = inspect_primary_guard(root)
    if guard_error and primary_guard_path(root).exists():
        raise SyncwheelError('repair the existing primary guard before mode migration')
    payload = {'mode': 'apply' if args.apply else 'plan', 'manifest': updated,
               'dropped_fields': [field for field in dropped if field in old],
               'archive': str(archive), 'archive_refs': refs, 'reason': args.reason,
               'managed_ref_guard': install_managed_push_hook(
                   root, manifest=normalized, manifest_path=path, reason=args.reason)}
    if args.apply:
        if managed_hook_syncwheel_command(root) is None:
            raise SyncwheelError('mode migration requires a stable resolvable syncwheel CLI before changing state')
        archive.mkdir(parents=True, exist_ok=True)
        original = path.read_bytes()
        archived = archive / 'manifest.json'
        if archived.exists() and archived.read_bytes() != original:
            raise SyncwheelError('migration manifest archive collision')
        archived.write_bytes(original)
        for ref, tip in refs.items():
            if not ref_tip(root, ref):
                git(root, 'update-ref', ref, tip, '0' * 40)
        original_branch = get_current_branch(root)
        atomic_write_private_json(pending_path, {
            'archive': str(archive), 'branch': original_branch, 'targetBranch': branch,
            'originalHead': ref_tip(root, 'HEAD'), 'guard': guard,
            'targetGuard': primary_guard_payload(normalized), 'metadataFiles': metadata_files,
            'beforeDigest': manifest_digest(manifest), 'afterDigest': manifest_digest(normalized),
            'branchTips': {original_branch: ref_tip(root, 'HEAD'), branch: target},
        })
        try:
            # Installing the retargeted guard can also append an audit event.
            # Apply the reviewed exclusions before either migration writer.
            for item in metadata_files:
                metadata_path = Path(item['path'])
                if metadata_path.is_symlink() or read_text_if_exists(metadata_path) != item['before']:
                    raise SyncwheelError('mode migration metadata changed before apply: ' + str(metadata_path))
                if item['after'] != item['before']:
                    metadata_path.parent.mkdir(parents=True, exist_ok=True)
                    metadata_path.write_text(item['after'])
                    transaction = _manifest_storage.active_manifest_write_transaction(path)
                    if transaction is not None and metadata_path == root / '.gitignore':
                        transaction['gitignoreWritten'] = True
            payload['managed_ref_guard'] = install_managed_push_hook(
                root, apply=True, manifest=normalized, manifest_path=path, reason=args.reason
            )
            with ref_move_authorization(root) as authorization:
                git(root, 'switch', branch, env={MANAGED_REF_MOVE_AUTH_ENV: authorization})
            _manifest_storage.save_manifest(path, updated)
            append_ledger_event(root, 'repository_mode_migrated', {
                'from': 'delivery', 'to': 'journal', 'reason': args.reason,
                'archive': str(archive), 'archive_refs': refs,
            }, path)
            pending_path.unlink()
        except BaseException as exc:
            try:
                restore_pending_mode_migration(root, path, pending_path)
            except Exception as recovery_error:
                raise SyncwheelError(
                    'mode migration interrupted; pending recovery retained. Retry repo mode set journal --apply: '
                    + str(recovery_error)
                ) from exc
            raise
    print(json.dumps(payload, indent=2))
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
    relative = _manifest_storage.repo_relative_path(repo_root, manifest_path)
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
        relative = _manifest_storage.repo_relative_path(repo_root, resolved)
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


def commit_git_tracked_manifest(
    repo_root, manifest_path, include_manifest=True, include_gitignore=False,
):
    if _manifest_storage.is_external_manifest_path(repo_root, manifest_path) or _manifest_storage.is_personal_manifest_path(
        repo_root, manifest_path
    ):
        return None
    manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if not manifest or manifest.get('syncwheel_tracking') != SYNCWHEEL_TRACKING_GIT_TRACKED:
        return None
    paths = [manifest_path] if include_manifest else []
    if include_gitignore:
        paths.append(repo_root / '.gitignore')
    relatives = [
        relative for path in paths
        if (relative := _manifest_storage.repo_relative_path(repo_root, path))
    ]
    if not relatives:
        return None
    git_add_paths(repo_root, paths, force_paths=[manifest_path])
    changed = git(
        repo_root, 'diff', '--cached', '--quiet', 'HEAD', '--', *relatives, check=False
    )
    if changed.returncode == 0:
        return None
    if changed.returncode != 1:
        raise SyncwheelError(
            changed.stderr.strip() or changed.stdout.strip()
            or f'could not inspect staged Syncwheel metadata: {", ".join(relatives)}'
        )
    run(
        with_git_identity(
            repo_root,
            [
                'git', 'commit', '--only', '-m',
                'chore(syncwheel): update tracked metadata', '--', *relatives,
            ],
        ),
        cwd=repo_root,
    )
    return git(repo_root, 'rev-parse', 'HEAD').stdout.strip()


def git_rm_cached_paths(repo_root, paths):
    relatives = []
    for path in paths:
        relative = _manifest_storage.repo_relative_path(repo_root, path)
        if relative:
            relatives.append(relative)
    if relatives:
        git(repo_root, 'rm', '--cached', '--ignore-unmatch', '--', *relatives)


def require_private_tracking_update(args, owner, manifest_path, primary):
    from syncwheel_stack_adoption import pending_adoptions

    require_private_tracking_path(owner, manifest_path)
    require_metadata_primary_index(primary)
    if owner != primary:
        require_metadata_primary_index(owner)
    for root, path in dict.fromkeys(((owner, manifest_path), (primary, primary / '.syncwheel' / 'manifest.json'))):
        events_dir = _manifest_storage.ledger_events_dir(root, path)
        require_private_tracking_path(root, events_dir, directory=True)
        for segment in events_dir.glob('*.jsonl'):
            require_private_tracking_path(root, segment)
        events = load_ledger_events(root, path)
        if pending_control_manifest_intents(events):
            raise SyncwheelError('control manifest recovery is pending; complete the interrupted operation first')
        if path == manifest_path and pending_adoptions(events):
            raise SyncwheelError('interrupted stack adoption; complete the pending adoption first')
    private_paths = (
        _manifest_storage.ledger_root(owner, manifest_path) / 'ledger.lock',
        _manifest_storage.ledger_checkpoint_path(owner, manifest_path),
    )
    for path in private_paths:
        require_private_tracking_path(owner, path)
    require_private_metadata_paths(args, owner, manifest_path)


def command_repo_tracking_set(args):
    selection = getattr(args, '_tracking_selection', None) or tracking_set_selection(args)
    scope, repo_root, manifest_path, primary = selection
    if scope == 'personal' and args.apply:
        require_private_tracking_update(args, repo_root, manifest_path, primary)
        with manifest_write_transaction(
            repo_root, manifest_path, 'personal-tracking', commit_tracked_changes=False,
        ):
            require_private_tracking_update(args, repo_root, manifest_path, primary)
            transaction = _manifest_storage.active_manifest_write_transaction(manifest_path)
            transaction['preserveGitignore'] = True
            return update_repo_tracking(args, repo_root, manifest_path, personal=True)
    return update_repo_tracking(args, repo_root, manifest_path, personal=scope == 'personal')


def update_repo_tracking(args, repo_root, manifest_path, *, personal):
    if not personal:
        validate_selected_primary_authority_path(repo_root, manifest_path)
    manifest, manifest_path = _manifest_storage.load_manifest(repo_root, manifest_path)
    if not manifest:
        raise SyncwheelError(f'manifest not found: {manifest_path}')
    ensure_manifest_in_repo(repo_root, manifest_path)
    tracking = normalize_syncwheel_tracking(args.tracking)
    worktree_root = normalize_syncwheel_worktree_root(args.worktree_root or syncwheel_worktree_root(manifest))
    manifest['syncwheel_tracking'] = tracking
    manifest['syncwheel_worktree_root'] = worktree_root

    warnings = []
    if not personal and tracking == SYNCWHEEL_TRACKING_GIT_TRACKED:
        conflicts = gitignore_manual_syncwheel_conflicts(repo_root)
        if conflicts:
            warnings.append(
                '.gitignore contains manual .syncwheel ignore entries outside the Syncwheel managed block; '
                'remove or audit them before applying git-tracked setup'
            )
    if warnings and args.apply:
        raise SyncwheelError('; '.join(warnings))

    if personal or not args.apply:
        report = syncwheel_tracking_report(repo_root, manifest_path)
        report['syncwheel_tracking'] = tracking
        report['syncwheel_tracking_present'] = True
        report['syncwheel_worktree_root'] = worktree_root
        report['effective_worktree_root'] = str(resolve_worktree_root_path(repo_root, worktree_root))
        report['warnings'].extend(warnings)
        if args.apply:
            _manifest_store().save_personal_tracking(
                repo_root, manifest_path, manifest,
                {'syncwheel_tracking': tracking, 'syncwheel_worktree_root': worktree_root},
            )
            report['actions'].clear()
        else:
            report['actions'].append(f'apply syncwheel_tracking={tracking}')
        if personal and args.json:
            print(json.dumps({**report, 'apply': args.apply}, indent=2))
        else:
            print_syncwheel_tracking_report(report)
            if not args.apply:
                print('dry_run: pass --apply to write this setup')
        return 0

    readme_path = repo_root / '.syncwheel' / 'manifests' / 'README.md'
    ensure_syncwheel_metadata_excluded(repo_root, tracking, worktree_root)
    if tracking == SYNCWHEEL_TRACKING_GIT_TRACKED:
        readme_path = ensure_manifests_readme(repo_root)
        save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'repo_tracking_set',
            {'syncwheel_tracking': tracking, 'syncwheel_worktree_root': worktree_root},
        )
        ensure_managed_repository_hooks(repo_root, manifest, manifest_path)
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


# Git rebuilds these from the entries, the worktree, or its own bookkeeping.


class SyncwheelRevisionBackend(_RevisionBackend):
    """Compose the in-process backend without changing its public constructor."""

    def __init__(self, provider_module):
        super().__init__(
            provider_module,
            repository=_RevisionRepository(), manifest_store=_RevisionManifestStore(),
            ledger=_RevisionLedger(), guards=_RevisionGuards(),
            provenance=_RevisionProvenance(), replay_factory=_RevisionReplayFactory(),
            state_factory=_RevisionStateFactory(),
            coordination_factory=_RevisionCoordinationFactory(),
        )


class JournalRevisionBackend(_JournalRevisionBackend, SyncwheelRevisionBackend):
    """Journal specialization with the same public constructor and base relation."""


def command_revision_provider_resolve_prepared(args):
    import syncwheel_revision_provider as provider_module
    backend = SyncwheelRevisionBackend(provider_module)
    root = resolve_repo_root(args.repo)
    if not provider_module.OPERATION_ID.fullmatch(args.operation_id):
        raise SyncwheelError('invalid revision-provider operation ID')
    path = git_common_dir(root) / 'syncwheel' / 'revision-provider' / f'{args.operation_id}.json'
    try:
        journal = json.loads(path.read_text())
        intent = {key: value for key, value in journal['request'].items()
                  if key != 'expectedManifestDigest' or value is not None}
        request = provider_module.parse_request({**intent, 'action': 'recover'})
        if request.operation_id != args.operation_id or request.repository_root != str(root):
            raise SyncwheelError('prepared operation does not belong to this repository')
        with backend.operation_lock(request):
            result = backend.resolve_prepared(
                request, args.resolution, args.reason,
                apply=args.apply, plan_digest=args.plan_digest,
            )
    except (OSError, ValueError, KeyError, TypeError, provider_module.RevisionProviderError) as exc:
        raise SyncwheelError(str(exc)) from exc
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_revision_provider(args):
    try:
        import syncwheel_revision_provider as provider_module
    except ImportError as exc:
        raise SyncwheelError(f'revision-provider module is not installed: {exc}') from exc
    backend = SyncwheelRevisionBackend(provider_module)
    return provider_module.run_provider_stream(
        backend, sys.stdin, sys.stdout, sys.stderr
    )


def build_parser():
    return _cli.build_parser(command_handlers(), VERSION)


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
    manifest, manifest_path = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    print(json.dumps(managed_push_guard_policy(repo_root, manifest, manifest_path), indent=2, sort_keys=True))
    return 0


def command_hooks_install(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    print(json.dumps(
        install_managed_push_hook(
            repo_root,
            apply=args.apply,
            manifest=manifest,
            manifest_path=manifest_path,
            reason=args.reason,
        ),
        indent=2,
        sort_keys=True,
    ))
    return 0


def command_hooks_remove(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, manifest_path = require_manifest(
        repo_root, args.repo, args.manifest, args.personal
    )
    result = remove_managed_push_hook(
        repo_root,
        apply=args.apply,
        disable=args.disable,
        reason=args.reason,
        manifest=manifest,
        manifest_path=manifest_path,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def command_hooks_guard(args):
    repo_root = resolve_repo_root(args.repo)
    manifest, _ = require_manifest(repo_root, args.repo, args.manifest, args.personal)
    updates = parse_pre_push_updates(sys.stdin)
    destinations = sorted({item['remoteRef'] for item in updates})
    protected_refs = managed_push_refs(repo_root, manifest)
    primary_manifest, _, _ = primary_guard_manifest(repo_root)
    if primary_manifest and primary_manifest != manifest:
        # The selected scope already checked its remote history. The primary
        # may publish elsewhere, so add only its local declarations here.
        protected_refs.update(locally_configured_push_refs(primary_manifest))
    protected = sorted(set(destinations).intersection(protected_refs))
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
    current_path = Path(git(repo_root, 'rev-parse', '--show-toplevel').stdout.strip()).resolve()
    if current_path != primary_root:
        return 0
    guard, guard_error = inspect_primary_guard(repo_root)
    branch = get_current_branch(primary_root)
    manifest_path = resolve_manifest_path(primary_root, str(primary_root), args.manifest, args.personal)
    manifest, _ = _manifest_storage.load_manifest(primary_root, manifest_path)
    remedies = (
        primary_checkout_remedy_commands(manifest, repo_root=primary_root, manifest_path=manifest_path)
        if manifest else ['syncwheel worktree open <lane> --into <stack>']
    )
    commit_blocked = (
        f'primary checkout commit blocked: {primary_root} is the shared integration projection. '
        'Only a Syncwheel-authorized control commit may be created there'
        + format_remedy_suffix(remedies)
        + '. This local hook is a safety guard, not a security boundary; '
        'syncwheel hooks remove --disable --reason "..." --apply is the explicit opt-out.'
    )
    if guard_error:
        # Nothing left names the integration branch, so the primary cannot be
        # checked against it. A checkout only gets a warning, because the guard
        # has no branch to claim it should have been on; a manual commit is
        # still refused, because the primary is the shared projection either way.
        print(
            f'syncwheel guard degraded: {primary_guard_repair_remedy(guard_error)}',
            file=sys.stderr,
        )
        if args.event != 'pre-commit' or ref_move_authorized(repo_root, 'pre-commit'):
            return 0
        raise SyncwheelError(f'{primary_guard_repair_remedy(guard_error)}. {commit_blocked}')
    if not guard['enabled']:
        return 0
    expected = guard.get('integrationBranch')
    if args.event == 'pre-commit' and branch == expected:
        if ref_move_authorized(repo_root, 'pre-commit'):
            return 0
        raise SyncwheelError(commit_blocked)
    if branch == expected:
        return 0
    action = 'commit blocked' if args.event == 'pre-commit' else 'branch mismatch detected after checkout'
    raise SyncwheelError(
        f'primary checkout {action}: expected {expected!r}, '
        f'found {branch!r} at {primary_root}. '
        'Keep the primary unchanged'
        + format_remedy_suffix(primary_checkout_remedy_commands(manifest))
        + '. '
        'restore the primary checkout losslessly before continuing. '
        'This local hook is a safety guard, not a security boundary; --no-verify can bypass it.'
    )


def command_hooks_ref_guard(args):
    # Git runs this for every ref transaction. Only the "prepared" phase can
    # veto, and only a branch ref can be the integration ref, so a transaction
    # that carries none leaves before this touches Git at all: fetch, worktree
    # creation and git's own internal ref writes are not the guarded surface.
    if args.phase != 'prepared':
        return 0
    updates = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) == 3:
            updates.append(parts)
    candidates = [item for item in updates if guardable_branch_ref(item[2])]
    if not candidates:
        return 0
    repo_root = resolve_repo_root(args.repo)
    guard, guard_error = inspect_primary_guard(repo_root)
    if guard_error:
        # guard.json is the only thing that names the integration branch, and
        # nothing in the working tree stands in for it: a journal manifest
        # declares no integration branch, the manifest need not be on the
        # checked-out branch, and the selected manifest can live outside the
        # repository. Degraded therefore keeps every branch ref.
        protected = [(ref, old, new) for old, new, ref in candidates]
    else:
        if not guard['enabled']:
            return 0
        integration_ref = f"refs/heads/{guard['integrationBranch']}"
        protected = [
            (ref, old, new) for old, new, ref in candidates if ref == integration_ref
        ]
    if not protected:
        return 0
    # before the authorization check, so a no-op like pack-refs doesn't spend it
    preserved = value_preserving_ref_updates(
        repo_root, [(old, new, ref) for ref, old, new in protected]
    )
    protected = [
        (ref, old, new) for ref, old, new in protected
        if (old, new, ref) not in preserved
    ]
    if not protected:
        return 0
    if ref_move_authorized(repo_root, 'reference-transaction'):
        return 0
    detail = ', '.join(f'{ref} {old[:7]} -> {new[:7]}' for ref, old, new in protected)
    if guard_error:
        raise SyncwheelError(
            f'{primary_guard_repair_remedy(guard_error)}. Until it is repaired the '
            f'guard cannot tell the integration ref from any other branch, so it '
            f'refuses every branch ref: {detail}. '
            'This local hook is a safety guard, not a security boundary; '
            'core.hooksPath and --no-verify can bypass it.'
        )
    raise SyncwheelError(
        f'refusing unauthorized primary integration ref move(s): {detail}. '
        'Use a Syncwheel-owned rebuild or a governed worktree. '
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


def entrypoint_behavior_table():
    return _cli.entrypoint_behavior_table(command_handlers(), (
        SyncwheelRevisionBackend.ensure_stack_owned,
        reconcile_governed_worktrees,
        prune_governed_worktree_stale_locks,
    ))


def command_behavior_table():
    return _cli.command_behavior_table(command_handlers())


def attach_command_behaviors(parser):
    return _cli.attach_command_behaviors(parser, command_behavior_table())


def command_behavior(args):
    try:
        return command_behavior_table()[args.func]
    except (AttributeError, KeyError) as exc:
        raise SyncwheelError('command mutation behavior is not registered') from exc


def mutation_rule_requested(rule, args):
    if rule == 'never':
        return False
    if rule == 'always':
        return True
    if rule == 'stack-set':
        return bool(getattr(args, 'apply', False)) if (
            getattr(args, 'base', None) or getattr(args, 'adopt_tip', None)
        ) else True
    if rule == 'apply':
        return bool(getattr(args, 'apply', False))
    if rule == 'execute':
        return not bool(getattr(args, 'dry_run', False))
    if rule == 'update-manifest':
        return bool(getattr(args, 'update_manifest', False))
    if rule == 'init':
        return not bool(getattr(args, 'stdout', False))
    if rule == 'internal':
        return False
    if rule == 'mode':
        return bool(getattr(args, 'mode', None))
    if rule == 'mode-or-clear':
        return bool(getattr(args, 'mode', None) or getattr(args, 'clear', False))
    if rule == 'profile-selection':
        return bool(getattr(args, 'personal', None) or getattr(args, 'shared', False))
    if rule == 'schedule-apply':
        return bool(
            getattr(args, 'apply', False)
            and getattr(args, 'schedule_command', None) in {'install', 'remove'}
        )
    if rule == 'git-passthrough':
        return git_passthrough_mutation_requested(getattr(args, 'git_args', []))
    raise SyncwheelError(f'unknown command mutation rule: {rule}')


def manifest_mutation_requested(args):
    return mutation_rule_requested(command_behavior(args)['manifestMutates'], args)


def syncwheel_mutation_requested(args):
    """Classify repository mutations from the parser's exhaustive command table."""
    return mutation_rule_requested(command_behavior(args)['mutates'], args)


def primary_guard_remedy_requested(args):
    return command_behavior(args)['primaryGuardRemedy']


# Git's own options before the command name that take their value as the next
# argument. Anything else that starts with a dash is a flag or carries `=value`.
GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset({
    '-c', '-C', '--git-dir', '--work-tree', '--namespace', '--config-env',
    '--super-prefix', '--attr-source',
})
# Options that point Git at another repository, worktree or program directory.
GIT_REDIRECTING_GLOBAL_OPTIONS = frozenset({
    '-C', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--super-prefix',
})
# Configuration a passthrough may not override, since it would rename commands
# or move the hooks and worktree the guard relies on.
GIT_GUARDED_CONFIG_PREFIXES = (
    'alias.', 'core.hookspath', 'core.worktree', 'core.bare', 'include.', 'includeif.',
)
GIT_READ_ONLY_COMMANDS = frozenset({
    'blame', 'cat-file', 'check-attr', 'check-ignore', 'cherry', 'count-objects',
    'describe', 'diff', 'diff-files', 'diff-index', 'diff-tree', 'for-each-ref',
    'fsck', 'grep', 'help', 'log', 'ls-files', 'ls-remote', 'ls-tree', 'merge-base',
    'name-rev', 'range-diff', 'rev-list', 'rev-parse', 'shortlog', 'show',
    'show-ref', 'status', 'var', 'version', 'whatchanged',
})
GIT_READ_ONLY_SUBCOMMANDS = {
    'worktree': frozenset({'list'}),
    'stash': frozenset({'list', 'show'}),
    'reflog': frozenset({'show'}),
}
GIT_INTEGRATION_REF_COMMANDS = frozenset({
    'commit', 'reset', 'merge', 'rebase', 'cherry-pick', 'revert', 'am',
})


def split_git_passthrough(git_args):
    """Split passthrough arguments into Git's global options, command and the rest."""
    options = []
    index = 0
    while index < len(git_args):
        part = git_args[index]
        if not part.startswith('-'):
            break
        name, has_value, value = part.partition('=')
        if part.startswith(('-c', '-C')) and len(part) > 2:
            name, value = part[:2], part[2:]
        elif name in GIT_GLOBAL_OPTIONS_WITH_VALUE and not has_value:
            value = git_args[index + 1] if index + 1 < len(git_args) else ''
            index += 1
        options.append((name, value))
        index += 1
    command = git_args[index] if index < len(git_args) else None
    return options, command, list(git_args[index + 1:])


def git_positionals(arguments):
    return [part for part in arguments if not part.startswith('-')]


def symbolic_ref_reads_only(rest):
    if any(part in {'-d', '--delete', '-m'} for part in rest):
        return False
    return len(git_positionals(rest)) <= 1


def git_branch_reads_only(rest):
    return not rest or rest == ['--show-current'] or (
        rest[0] in {'--list', '-l'} and all(not part.startswith('-') for part in rest[1:])
    )


def git_config_reads_only(rest):
    arities = {'--get': (1, 2), '--get-all': (1, 2), '--get-regexp': (1, 2),
               '--get-urlmatch': (2, 2), '--list': (0, 0), '-l': (0, 0)}
    if not rest or rest[0] not in arities:
        return False
    low, high = arities[rest[0]]
    return low <= len(rest) - 1 <= high and all(not part.startswith('-') for part in rest[1:])


def git_fetch_updates_head(rest):
    # Git accepts combined short options and unique long abbreviations. Values
    # of other options and arguments after -- are not options themselves.
    value_options = ('--upload-pack', '--jobs', '--depth', '--shallow-since',
                     '--shallow-exclude', '--deepen', '--refmap', '--server-option',
                     '--negotiation-tip', '--filter')
    consume_value = False
    for part in rest:
        if consume_value:
            consume_value = False
            continue
        if part == '--':
            break
        if part.startswith('--'):
            name, equals, _ = part.partition('=')
            if len(name) > 2 and '--update-head-ok'.startswith(name):
                return True
            consume_value = not equals and any(option.startswith(name) for option in value_options)
        elif part.startswith('-'):
            for index, option in enumerate(part[1:], 1):
                if option == 'u':
                    return True
                if option in {'o', 'j'}:
                    consume_value = index == len(part) - 1
                    break
    return False


def git_passthrough_mutation_requested(git_args):
    """Fail closed for Git passthroughs except its unambiguous read-only verbs."""
    options, command, rest = split_git_passthrough(git_args)
    if any(name in GIT_REDIRECTING_GLOBAL_OPTIONS or name in {'-c', '--config-env'}
           for name, _ in options):
        return True
    if command in GIT_READ_ONLY_COMMANDS:
        return False
    if command == 'symbolic-ref':
        return not symbolic_ref_reads_only(rest)
    if command == 'branch':
        return not git_branch_reads_only(rest)
    if command == 'config':
        return not git_config_reads_only(rest)
    if command in GIT_READ_ONLY_SUBCOMMANDS:
        # The subcommand must come first: `stash -m list` is a stash push.
        return not rest or rest[0] not in GIT_READ_ONLY_SUBCOMMANDS[command]
    return True


def int_git_passthrough_refusal(worktree, git_args, branch):
    """Why `int git` must not run these arguments, or None.

    `int git` authorizes moves of the integration ref for one process, so it
    only runs Git commands that act on the checked-out integration branch in
    its own worktree. Direct ref writes, HEAD rewrites, aliases and options that
    point Git elsewhere would turn that authorization into a way around the
    guard.
    """
    options, command, rest = split_git_passthrough(git_args)
    prefix = 'int git runs Git on the integration branch in its own worktree'
    for name, value in options:
        if name in GIT_REDIRECTING_GLOBAL_OPTIONS:
            return f'{prefix}; drop {name}, which points Git at another repository or directory'
        if name in {'-c', '--config-env'}:
            key = value.split('=', 1)[0].lower()
            if key.startswith(GIT_GUARDED_CONFIG_PREFIXES):
                return f'{prefix}; it does not accept `{name} {value}`, which overrides {key}'
    if command is None:
        return None
    if command == 'update-ref':
        return (
            f'{prefix}; update-ref moves refs directly. Rebuild the integration branch '
            'with `syncwheel int rebuild`, or move it with a Git command such as reset '
            'or cherry-pick'
        )
    if command == 'branch' and not git_branch_reads_only(rest):
        return f'{prefix}; branch writes can rename or remove protected refs; only branch listing is supported'
    if command == 'config' and not git_config_reads_only(rest):
        return f'{prefix}; config writes can disable the guard; only explicit config reads are supported'
    if command == 'fetch' and git_fetch_updates_head(rest):
        return (
            f'{prefix}; fetch --update-head-ok writes the checked-out branch without '
            'updating its index. Fetch into a remote-tracking ref and merge or reset from it'
        )
    if command == 'symbolic-ref' and not symbolic_ref_reads_only(rest):
        return (
            f'{prefix}; symbolic-ref would repoint HEAD away from {branch} without a checkout. '
            'Open a lane with `syncwheel worktree open` to work on another branch'
        )
    alias = run(
        ['git', 'config', '--get', f'alias.{command}'], cwd=worktree, check=False,
    )
    if alias.returncode == 0:
        return (
            f'{prefix}; `{command}` is a Git alias for `{alias.stdout.strip()}`, which it '
            'does not expand. Spell the Git command out'
        )
    return None


def interrupted_git_operation(repo_root):
    git_dir = Path(git(repo_root, 'rev-parse', '--absolute-git-dir').stdout.strip())
    if (git_dir / 'rebase-merge').exists() or (git_dir / 'rebase-apply' / 'rebasing').exists():
        return 'rebase'
    if (git_dir / 'rebase-apply').exists():
        return 'am'
    if (git_dir / 'CHERRY_PICK_HEAD').exists():
        return 'cherry-pick'
    if (git_dir / 'REVERT_HEAD').exists():
        return 'revert'
    # Between picks the sequencer has no *_HEAD marker, only its todo list.
    todo = git_dir / 'sequencer' / 'todo'
    if todo.exists():
        steps = [line.split()[0] for line in todo.read_text().splitlines() if line.strip()]
        return {'pick': 'cherry-pick', 'p': 'cherry-pick', 'revert': 'revert'}.get(
            steps[0] if steps else None
        )
    if (git_dir / 'MERGE_HEAD').exists():
        return 'merge'
    return None


def git_resume_requested(git_args, operation):
    return (
        operation is not None
        and len(git_args) == 2
        and git_args[0] == operation
        and git_args[1] in {'--continue', '--abort', '--skip', '--quit'}
    )


def require_stack_sync_metadata_paths(repo_root, manifest_path):
    # External manifests keep their ledger beside the selected file.
    owner = repo_root if manifest_path.is_relative_to(repo_root) else Path(manifest_path.anchor)
    require_private_tracking_path(owner, manifest_path)
    ledger = _manifest_storage.ledger_root(repo_root, manifest_path)
    events = _manifest_storage.ledger_events_dir(repo_root, manifest_path)
    require_private_tracking_path(owner, events, directory=True)
    for path in (*events.glob('*.jsonl'), ledger / 'ledger.lock',
                 _manifest_storage.ledger_checkpoint_path(repo_root, manifest_path)):
        require_private_tracking_path(owner, path)


def resolve_stack_sync_selection(args):
    repo_value = args.repo or os.environ.get(ENV_REPO)
    checkout = resolve_repo_root(repo_value)
    manifest_path = Path(os.path.abspath(
        resolve_manifest_path(checkout, repo_value, args.manifest, args.personal)
    ))
    primary = primary_authority_manifest_path(checkout).parent.parent
    for root in dict.fromkeys((checkout, primary)):
        if manifest_path.is_relative_to(root):
            require_stack_sync_metadata_paths(root, manifest_path)
            return root, manifest_path
    raise SyncwheelError('stack sync requires a manifest in the current or primary checkout')


def refuse_stack_sync_control_recovery(repo_root, manifest_path, operation='stack sync'):
    shared_path = primary_authority_manifest_path(repo_root)
    for root, path in dict.fromkeys(((repo_root, manifest_path), (shared_path.parent.parent, shared_path))):
        require_stack_sync_metadata_paths(root, path)
        pending = pending_control_manifest_intents(load_ledger_events(root, path))
        if pending:
            intent = pending[0]['payload']
            if explicit_control_recovery_intent(intent):
                selection = shlex.join(manifest_path_remedy_options(root, path))
                raise SyncwheelError(
                    f'{_manifest_storage.explicit_control_recovery_refusal()}. '
                    f'Use syncwheel manifest recover-control --from-backup BACKUP {selection}'
                )
            selection = shlex.join(manifest_path_remedy_options(root, path))
            raise SyncwheelError(
                'control manifest recovery is pending; from a clean primary checkout run '
                f'syncwheel reconcile --apply {selection} before {operation}'
            )


def uses_metadata_primary_scope(args, repo_root, manifest_path, manifest):
    if command_behavior(args)['primaryScope'] != 'metadata':
        return False
    if args.func == command_stack_sync:
        return True
    if args.func == command_repo_authority_set:
        return (
            _manifest_storage.is_personal_manifest_path(repo_root, manifest_path)
            and manifest_path.is_file() and not manifest_path.is_symlink()
            and not git_path_is_tracked(repo_root, manifest_path)
        )
    if args.func in {command_repo_pr_merge_policy_set, command_repo_pr_merge_policy_clear}:
        return manifest_path == repo_root / '.syncwheel' / 'manifest.json'
    if coordination_is_active(manifest) or _manifest_storage.is_external_manifest_path(repo_root, manifest_path):
        return False
    if args.func == command_stack_archive:
        return True
    lifecycle = _stack_lifecycle(repo_root)
    if lifecycle.pending_stack_close_operation(manifest_path, args.stack):
        return False
    reason = args.reason or lifecycle.abandoned_close_reason(manifest_path, args.stack)
    return ordinary_empty_draft_close(manifest, stack_map(manifest).get(args.stack), args, reason)


def dirty_primary_control_manifest_refusal(primary_root):
    preview = shlex.join([
        'syncwheel', 'manifest', 'recover-stack', 'STACK',
        '--repo', str(primary_root), '--reason', 'REASON',
    ])
    return ('primary checkout control manifest is dirty; '
            f'if it contains one native stack declaration, preview with {preview}')


def require_metadata_primary_index(primary_root):
    if git(primary_root, 'ls-files', '-u').stdout.strip():
        raise SyncwheelError('primary checkout has index conflicts')
    staged = git(primary_root, 'diff', '--cached', '--name-only', '-z').stdout.split('\0')
    if any(staged):
        raise SyncwheelError('primary checkout has unrelated staged paths: '
                             + ', '.join(sorted(set(staged) - {''})))
    if git(primary_root, 'diff', '--quiet', '--', '.syncwheel/manifest.json',
           check=False, env={'GIT_OPTIONAL_LOCKS': '0'}).returncode != 0:
        raise SyncwheelError(dirty_primary_control_manifest_refusal(primary_root))


def require_private_metadata_paths(args, repo_root, manifest_path):
    paths = [
        _manifest_storage.ledger_root(repo_root, manifest_path) / 'ledger.lock',
        _manifest_storage.ledger_checkpoint_path(repo_root, manifest_path),
        _manifest_storage.ledger_events_dir(repo_root, manifest_path) / '000001.jsonl',
        _manifest_storage.next_ledger_segment_path(repo_root, manifest_path, create_directory=False),
    ]
    if args.func in {command_repo_authority_set, command_repo_tracking_set}:
        paths.append(manifest_path)
    elif args.func in {command_repo_pr_merge_policy_set, command_repo_pr_merge_policy_clear}:
        paths.append(checked_repo_profile_path(repo_root))
    try:
        require_ignored_ledger_paths(repo_root, paths)
    except SyncwheelError as exc:
        raise SyncwheelError(
            'metadata update requires untracked, already ignored private metadata. '
            'Review the repository exclusions and retry'
        ) from exc


def primary_checkout_preflight(args):
    if not hasattr(args, 'repo') or args.func in {
        command_hooks_guard,
        command_hooks_worktree_guard,
        command_hooks_ref_guard,
    }:
        return
    repo_root, selected_path = resolve_command_manifest_selection(args)
    if args.func == command_stack_sync:
        primary_root = primary_authority_manifest_path(repo_root).parent.parent
        require_metadata_primary_index(primary_root)
        if repo_root != primary_root:
            require_metadata_primary_index(repo_root)
        args._metadata_primary_root = primary_root
        args._metadata_preserve_primary = True
        return
    tracking_selection = getattr(args, '_tracking_selection', None)
    if tracking_selection is not None:
        primary_root = tracking_selection[3]
        manifest_path = selected_path
    else:
        worktrees = get_worktrees(repo_root)
        primary_root = Path(worktrees[0]['path']).resolve() if worktrees else repo_root
        manifest_path = (
            selected_path if args.func == command_stack_capture_integration
            else resolve_manifest_path(
                primary_root, str(primary_root), getattr(args, 'manifest', None), getattr(args, 'personal', None)
            )
        )
    manifest, _ = _manifest_storage.load_manifest(primary_root, manifest_path)
    metadata_scope = False
    if command_behavior(args)['primaryScope'] == 'metadata' and syncwheel_mutation_requested(args):
        selected, _ = _manifest_storage.load_manifest(repo_root, selected_path)
        if selected:
            metadata_scope = uses_metadata_primary_scope(args, repo_root, selected_path, selected)
            if manifest is None:
                manifest, manifest_path = selected, selected_path
    if manifest is None:
        return
    # Journal repositories deliberately turn tracked working-tree changes into
    # snapshots. Their journal commands own that separate lifecycle, so the
    # shared integration checkout guard does not apply there.
    if manifest.get('repository_mode') == 'journal':
        return
    if metadata_scope:
        args._metadata_primary_root = primary_root
        args._metadata_preserve_primary = bool(primary_checkout_dirty_entries(primary_root))
    emit_primary_checkout_dirty_warnings(
        primary_root, manifest, json_mode=bool(getattr(args, 'json', False))
    )
    if (
        syncwheel_mutation_requested(args)
        and not primary_guard_remedy_requested(args)
        and not managed_push_guard_policy(primary_root, manifest, manifest_path)['disabled']
    ):
        # A resume owns its destination's index, which can be a linked checkout.
        # Resolve existing registrations only; this exception must not create a
        # checkout or let an unrelated mutation bypass the dirty-primary guard.
        if args.func == command_int_git and any(
            git_resume_requested(args.git_args, operation)
            for operation in ('rebase', 'am', 'cherry-pick', 'revert', 'merge')
        ):
            target = resolve_git_worktree(primary_root, manifest['integration']['branch'], manifest)
            if git_resume_requested(args.git_args, interrupted_git_operation(target)):
                return
        # These commands own manifest/ref metadata or an explicit absorb patch;
        # they do not rewrite unrelated primary product paths. Commands that
        # rebuild or align the integration checkout retain the global guard.
        scoped_primary_commands = {
            command_stack_create,
            command_stack_add,
            command_stack_set,
            command_stack_promote,
            command_stack_demote,
            command_stack_absorb,
            command_worktree_release,
        }
        bounded_publication_commands = {
            command_stack_push,
            command_stack_rebuild,
            command_int_push,
            command_coordination_compose,
            command_stack_merge_pr,
        }
        if args.func in bounded_publication_commands:
            if git(primary_root, 'ls-files', '-u').stdout.strip() or git(
                primary_root, 'diff', '--cached', '--quiet', check=False
            ).returncode != 0:
                raise SyncwheelError('primary checkout has index changes or conflicts')
            if git(primary_root, 'diff', '--quiet', '--', '.syncwheel/manifest.json',
                   check=False, env={'GIT_OPTIONAL_LOCKS': '0'}).returncode != 0:
                raise SyncwheelError(dirty_primary_control_manifest_refusal(primary_root))
            return
        if metadata_scope:
            require_metadata_primary_index(primary_root)
            return
        if args.func in scoped_primary_commands:
            if git(primary_root, 'ls-files', '-u').stdout.strip():
                raise SyncwheelError('primary checkout has index conflicts')
            staged = set(item for item in git(
                primary_root, 'diff', '--cached', '--name-only', '-z'
            ).stdout.split('\0') if item)
            if args.func == command_stack_absorb and args.staged:
                selected = set(item for item in git(
                    primary_root, 'diff', '--cached', '--name-only', '-z',
                    *(['--', *args.paths] if args.paths else []),
                ).stdout.split('\0') if item)
                staged -= selected
            if staged:
                raise SyncwheelError('primary checkout has unrelated staged paths: '
                                     + ', '.join(sorted(staged)))
            if git(primary_root, 'diff', '--quiet', '--', '.syncwheel/manifest.json',
                   check=False, env={'GIT_OPTIONAL_LOCKS': '0'}).returncode != 0:
                raise SyncwheelError(dirty_primary_control_manifest_refusal(primary_root))
            return
        selected, _ = _manifest_storage.load_manifest(repo_root, selected_path)
        stack_id = getattr(args, 'stack', None)
        if isinstance(stack_id, list):
            stack_id = stack_id[0] if len(stack_id) == 1 else None
        require_clean_primary_checkout(
            primary_root, selected if selected else manifest,
            selected_path if selected else manifest_path, stack_id=stack_id,
        )


def default_hook_convergence_requested(args):
    return False


def converge_default_repository_hooks(args):
    if not default_hook_convergence_requested(args):
        return None
    repo_root, manifest_path = resolve_command_manifest_selection(args)
    if not manifest_path.exists():
        return None
    manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if manifest is None:
        return None
    return ensure_managed_repository_hooks(repo_root, manifest, manifest_path)


def governed_worktree_mutation_requested(args):
    """Return whether this command checks governed lane health before mutating.

    Such a command finishes cleanups that an explicit capture or release
    already started, and the riskiest ones then refuse while a lane needs
    recovery. It never selects an active lane for removal: an expired lease
    is information, not consent. Removing an active lane belongs to
    ``worktree release <lane>`` and ``gc --apply``.

    Integration operations update the dedicated integration ref or its
    publication state, while stack creation claims a new stack id and branch.
    Governed lanes use independent syncwheel/lane/* refs, so an expired or
    dirty unrelated lane cannot conflict with these operations. Keep
    surfacing diagnostics as warnings and leave recovery to explicit
    release/gc.
    """
    if args.func in {
        command_int_align_remote,
        command_int_push,
        command_int_rebuild,
        command_stack_create,
        command_stack_add,
        command_stack_sync,
    }:
        return False
    always_mutating = {
        command_worktree_lock, command_worktree_unlock,
        command_sync, command_publish,
        command_stack_absorb, command_stack_add, command_stack_capture_integration,
        command_stack_close, command_stack_create,
        command_stack_demote, command_stack_promote,
        command_stack_resolve_integration, command_stack_set,
    }
    apply_gated = {
        command_reconcile,
        command_resume,
        command_stack_classify_integration,
        command_stack_land,
    }
    if args.func in always_mutating:
        return True
    if args.func in apply_gated:
        return bool(getattr(args, 'apply', False))
    if args.func in {command_stack_git, command_int_git}:
        return bool(getattr(args, 'auto_worktree', False) or getattr(args, 'worktree', None))
    return False


def governed_worktree_preflight(args):
    if not hasattr(args, 'repo'):
        return
    repo_root, manifest_path = resolve_command_manifest_selection(args)
    manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if manifest is None:
        return
    if args.func in {command_stack_sync, command_stack_absorb}:
        if require_stack(manifest, args.stack).get('adopted_history'):
            raise SyncwheelError('stack adoption changed after observation; inspect the selected manifest and retry')
    if args.func == command_stack_set and getattr(args, 'preserve_worktrees', None) is not None and (
        getattr(args, 'base', None) or getattr(args, 'adopt_tip', None) or getattr(args, 'published', False)
    ):
        raise SyncwheelError('--preserve-worktrees cannot be combined with adoption or --published')
    if args.func == command_stack_set and (getattr(args, 'base', None) or getattr(args, 'adopt_tip', None)):
        # Adoption has an exact plan and never cleans up lanes.
        return
    if args.func == command_stack_set and getattr(args, 'preserve_worktrees', None) is not None:
        if not args.preserve_worktrees.strip():
            raise SyncwheelError('--preserve-worktrees requires a non-empty reason')
        # Stack set only changes the selected manifest and its ledger. This
        # explicit mode must not clean up either related or unrelated lanes.
        emit_governed_worktree_warnings(repo_root, manifest, json_mode=bool(getattr(args, 'json', False)), manifest_path=manifest_path)
        return
    if args.func in {command_worktree_open, command_worktree_list}:
        # Open checks its own target and registry under the lock, and list is
        # the detailed view of the same diagnostics. Recovery of unrelated
        # lanes belongs to explicit release and gc commands.
        return
    emit_governed_worktree_warnings(repo_root, manifest, json_mode=bool(getattr(args, 'json', False)), manifest_path=manifest_path)
    if args.func == command_stack_create:
        requested_branch = (
            f'syncwheel/draft/{safe_ref_segment(args.stack)}'
            if args.draft
            else args.branch or f'pr/{safe_ref_segment(args.stack)}'
        )
        conflicts = [
            lane for lane in governed_worktree_diagnostics(repo_root, manifest)['lanes']
            if lane.get('state') != 'reaped'
            and (
                lane.get('target') == args.stack
                or lane.get('branch') == requested_branch
            )
        ]
        if conflicts:
            labels = ', '.join(
                lane.get('id') or lane.get('branch') or lane.get('path')
                for lane in conflicts
            )
            raise SyncwheelError(
                'governed worktree conflicts with the new stack id or branch: '
                + labels
            )
    stack_scoped_without_global_reaping = {
        command_stack_archive,
        command_stack_close,
        command_stack_promote,
        command_stack_push,
        command_stack_rebuild,
        command_stack_resolve_integration,
    }
    scoped_stacks = None
    scoped_stack_label = 'the selected stack'
    if args.func in stack_scoped_without_global_reaping:
        scoped_stacks = {args.stack}
        scoped_stack_label = 'this stack'
    elif args.func in {command_reconcile, command_resume} and getattr(args, 'stack', None):
        scoped_stacks = set(args.stack)
    if args.func == command_stack_push and getattr(args, 'allow_equivalent_candidate', False):
        if getattr(args, 'retain_captured_worktrees', None) is None:
            raise SyncwheelError('--allow-equivalent-candidate requires --retain-captured-worktrees REASON')
        if args.force_with_lease or getattr(args, 'git_args', []):
            raise SyncwheelError('equivalent candidate delivery refuses force and passthrough push options')
        stack = require_stack(manifest, args.stack)
        remote = stack_push_remote(manifest, stack, args.remote)
        if coordination_is_active(manifest):
            coordinated_push_remote(args, coordination_config(manifest))
            remote = coordination_config(manifest)['remote']
        args._preserved_candidate_remote = remote
    if args.func in {command_stack_push, command_stack_capture_integration} and getattr(
        args, 'retain_captured_worktrees', None
    ) is not None:
        if not args.retain_captured_worktrees.strip():
            raise SyncwheelError('--retain-captured-worktrees requires a non-empty reason')
        proposed = retained_capture_manifest(repo_root, manifest, args)
        args._retained_captured_worktree_snapshot = retained_captured_worktree_snapshot(
            repo_root, proposed, args.stack, capture=args.func == command_stack_capture_integration,
            candidate_remote=getattr(args, '_preserved_candidate_remote', None),
        )
        return
    if args.func == command_stack_push:
        # Publication only reads lanes. A clean lane at the already declared
        # exact source tip does not need capture or cleanup, even on handoff.
        args._active_publication_snapshot = retained_captured_worktree_snapshot(
            repo_root, manifest, args.stack, active_only=True
        )
        return
    if args.func == command_stack_capture_integration:
        return
    if scoped_stacks:
        registry, _ = load_governed_worktree_registry(repo_root)
        blockers = [
            lane for lane in registry['lanes']
            if lane.get('target') in scoped_stacks and lane.get('state') != 'reaped'
            # Intact lane whose tip is in a local recovery ref; only its remote
            # save is pending, which never blocks the stack.
            and lane.get('pending_reason') != 'archive_push_failed'
        ]
        if blockers:
            # Name each lane's health and remedy: the warnings above may have
            # been collapsed into a summary line on a busy repository.
            health = {
                lane['id']: lane
                for lane in governed_worktree_diagnostics(repo_root, manifest)['lanes']
                if lane.get('id')
            }

            def describe(lane):
                found = health.get(lane['id']) or {}
                code = found.get('code') or lane.get('pending_reason') or lane.get('state')
                remedy = found.get('remedy')
                return f"{lane['id']} [{code}]" + (f': {remedy}' if remedy else '')

            raise SyncwheelError(
                f'governed worktree recovery is required before updating {scoped_stack_label}: '
                + '; '.join(describe(lane) for lane in blockers)
            )
        return
    # A lane whose remote save failed is intact (branch and folder kept), so
    # it never blocks an unrelated mutation; its own release or gc retries it.
    tolerated = {'archive_push_failed'}
    dangerous = {
        command_stack_rebuild, command_stack_push, command_int_push, command_reconcile,
        command_resume, command_sync, command_publish, command_stack_land, command_stack_close,
    }
    if args.func == command_stack_add:
        # Capture below handles active lanes whose commits become owned by
        # this stack. Only finish pending cleanups for this stack; unrelated
        # retained copies belong to explicit release/gc.
        candidate_ids = {
            lane['id'] for lane in governed_worktree_pending_cleanup_candidates(repo_root, manifest)
            if lane.get('target') == args.stack
            and lane.get('code') != 'archive_push_failed'
        }
        if not candidate_ids:
            return
        cleanup = reconcile_governed_worktrees(
            repo_root, manifest, manifest_path, candidate_ids=candidate_ids,
            protect_foreign_lanes=True,
        )
    elif governed_worktree_mutation_requested(args):
        # Record moved lane paths and finish cleanups already started, but
        # never select an active lane: removal needs release or gc. A lane
        # whose remote save failed is intact and waits for its own retry, so
        # an unreachable remote never blocks unrelated mutations.
        candidate_ids = {
            lane['id'] for lane in governed_worktree_pending_cleanup_candidates(repo_root, manifest)
            if lane.get('code') != 'archive_push_failed'
        }
        protect_foreign = args.func in {
            command_stack_capture_integration, command_stack_classify_integration,
        }
        options = {'protect_foreign_lanes': True} if protect_foreign else {}
        cleanup = reconcile_governed_worktrees(
            repo_root, manifest, manifest_path, candidate_ids=candidate_ids,
            repair_all_paths=True, **options,
        )
        # A lane whose cleanup could not take its Git worktree lock (someone
        # holds it with their own lock) is intact: the cleanup stopped before
        # touching anything. Leave it to its own release or gc instead of
        # stopping every other mutation in the repo. Whole-repository
        # publication still stops on it.
        if args.func not in dangerous:
            tolerated.add('lane_in_use')
    else:
        return
    for item in cleanup['failures']:
        if item.get('code') == 'lane_in_use' and 'lane_in_use' in tolerated:
            print(
                f"warning: governed worktree lane {item['id']} could not be locked for cleanup "
                '(another Git worktree lock holds it, or it is checked out elsewhere); it is left '
                'intact for `syncwheel worktree release` or `syncwheel gc --apply`',
                file=sys.stderr,
            )
    failures = [item for item in cleanup['failures'] if item.get('code') not in tolerated]
    if failures:
        details = ', '.join(
            f"{item['id']} [{item['code']}]" for item in failures
        )
        raise SyncwheelError(
            'governed worktree recovery is required before this mutation: '
            + details
            + '; cleanup failed closed, retry the named remedy '
            '(`syncwheel worktree list --problems` shows each lane and its remedy)'
        )
    if args.func not in dangerous:
        return
    # These commands rebuild stack branches, and sync/publish can gc the
    # worktrees of closed stacks, so a lane that needs recovery blocks them
    # when it works on a stack they can rebuild (one still declared in the
    # manifest) or when its folder is checked out on a branch other than its
    # own lane branch. Any other lane, with no target or a closed or undeclared
    # stack, is reported and left to its own release or gc. A lane folder that
    # was both moved and switched to another branch cannot be told apart from
    # a deleted one, so it is not covered here.
    open_stacks = {stack.get('id') for stack in manifest.get('stacks', [])}
    checked_out = {
        str(Path(item['path']).resolve(strict=False)): item.get('branch') or ''
        for item in get_worktrees(repo_root) if item.get('path')
    }

    def lane_blocks(lane):
        if lane.get('target') in open_stacks:
            return True
        path = lane.get('path')
        branch = checked_out.get(str(Path(path).resolve(strict=False))) if path else None
        return bool(branch) and branch != lane.get('branch')

    needs_recovery = [
        lane for lane in governed_worktree_diagnostics(repo_root, manifest)['lanes']
        if lane['code'] in {
            'dirty', 'outside_root', 'unregistered_worktree', 'unavailable',
            'invalid_lease', 'current_directory', 'branch_delete_failed',
            'branch_advanced', 'locked', 'worktree_remove_failed', 'reaping',
            'ledger_pending', 'recovery_ref_moved', 'lane_in_use',
            'registration_mismatch', 'path_reappeared',
        }
    ]
    blocked = [lane for lane in needs_recovery if lane_blocks(lane)]
    passed = [lane for lane in needs_recovery if not lane_blocks(lane)]
    if len(passed) > GOVERNED_WORKTREE_WARNING_DETAIL_LIMIT:
        print(
            f'warning: {len(passed)} governed worktree lanes need recovery but do not block this '
            'command: they have no target stack, or their stack is not in the manifest; '
            '`syncwheel worktree list --problems` shows them',
            file=sys.stderr,
        )
        passed = []
    for lane in passed:
        label = lane['id'] or lane['branch'] or lane['path']
        target = lane.get('target')
        why = f'its stack {target!r} is not in the manifest' if target else 'it has no target stack'
        print(
            f"warning: governed worktree {label} needs recovery ({lane['code']}) but does not "
            f'block this command: {why}',
            file=sys.stderr,
        )
    if blocked:
        # Name the reason and remedy here: the warnings above may have been
        # collapsed into a summary line on a busy repository.
        details = '; '.join(
            f"{item['id'] or item['branch'] or item['path']} [{item['code']}]: {item.get('remedy') or 'see worktree list'}"
            for item in blocked
        )
        raise SyncwheelError(
            'governed worktree recovery is required before this mutation: ' + details
        )


def coordination_publication_lock_requested(args):
    """Commands whose whole publication cycle must exclude every other one here."""
    if not hasattr(args, 'repo'):
        return False
    always_mutating = {
        command_stack_create, command_stack_promote, command_stack_demote,
        command_stack_close, command_stack_sync, command_stack_add, command_stack_capture_integration,
    }
    dry_run_gated = {
        command_stack_push, command_int_push, command_stack_rebuild,
    }
    apply_gated = {
        command_reconcile, command_resume, command_sync, command_publish,
        command_channel_publish, command_channel_close, command_stack_archive,
        command_coordination_claims_backfill, command_coordination_repair,
        command_coordination_compose,
    }
    if args.func in always_mutating:
        return True
    if args.func in dry_run_gated:
        return not bool(getattr(args, 'dry_run', False))
    if args.func in apply_gated:
        return bool(getattr(args, 'apply', False))
    return False


@contextlib.contextmanager
def coordination_publication_lock_for_command(args):
    if not coordination_publication_lock_requested(args):
        yield None
        return
    repo_root, manifest_path = resolve_command_manifest_selection(args)
    options = {}
    if args.func in {command_stack_sync, command_stack_capture_integration}:
        operation = 'stack sync' if args.func == command_stack_sync else 'stack capture-integration'
        selection = shlex.join(manifest_path_remedy_options(repo_root, manifest_path))
        if args.func == command_stack_capture_integration:
            _stack_lifecycle(repo_root).refuse_pending_stack_sync_promotion(manifest_path, selection)
        refuse_stack_sync_control_recovery(repo_root, manifest_path, operation)
        if args.func == command_stack_sync:
            _stack_lifecycle(repo_root).refuse_pending_stack_sync_promotion(manifest_path, selection)
        options['stale_lock_remedy'] = (
            f'from a clean primary checkout run syncwheel reconcile --apply {selection}'
        )
    manifest, _ = (
        _manifest_storage.load_manifest(repo_root, manifest_path) if manifest_path.exists() else (None, None)
    )
    if manifest is None or not coordination_is_active(manifest):
        yield None
        return
    with coordination_publication_lock(repo_root, **options) as owner:
        yield owner


CONTROL_MANIFEST_RECOVERY_ENTRYPOINTS = {
    command_stack_push,
    command_int_rebuild,
    command_int_push,
}


def recover_control_manifest_before_command(args):
    """Run FC4 recovery before an entrypoint can require its manifest source."""
    if not hasattr(args, 'repo') or bool(getattr(args, 'dry_run', False)):
        return None
    if args.func == command_manifest_recover_control:
        # Only this command's fresh exact plan may authorize a quarantined
        # continuation. A preview remains read-only, including its ledger.
        return None
    entrypoint = args.func in CONTROL_MANIFEST_RECOVERY_ENTRYPOINTS
    if not entrypoint and not manifest_mutation_requested(args):
        # Every other manifest writer must still settle a pending intent before
        # it replaces the source the interrupted operation was persisting.
        return None
    repo_root, manifest_path = resolve_command_manifest_selection(args)
    if args.func in {command_stack_sync, command_stack_capture_integration}:
        operation = 'stack sync' if args.func == command_stack_sync else 'stack capture-integration'
        refuse_stack_sync_control_recovery(repo_root, manifest_path, operation)
        return None
    if getattr(args, '_tracking_selection', None) is not None:
        validate_selected_primary_authority_path(repo_root, manifest_path)
    manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
    if args.func == command_int_rebuild:
        require_int_rebuild_reason(args, repo_root, manifest, manifest_path)
    explicit_rebuild = args.func == command_int_rebuild and bool(getattr(args, 'reason', None))
    if not explicit_rebuild and skip_implicit_control_recovery_on_dirty_primary(repo_root, manifest_path):
        # An explicit rebuild can resume its own frozen integration lease,
        # where a ref update may have preceded checkout alignment.
        args._control_manifest_recovery_state = {'recovered': False, 'control_commit': None}
        args._skip_implicit_control_recovery = True
        return manifest
    recovery_state = {}
    recovered = recover_incomplete_control_manifest_persistence(
        repo_root,
        manifest_path,
        manifest,
        allow_new_operation=explicit_rebuild,
        recovery_state=recovery_state,
        intents_only=not entrypoint,
    )
    args._control_manifest_recovery_state = recovery_state
    return recovered


def stack_adoption_preflight(args):
    if not hasattr(args, 'repo') or not syncwheel_mutation_requested(args):
        return
    from syncwheel_stack_adoption import pending_adoptions
    root, path = resolve_command_manifest_selection(args)
    pending = pending_adoptions(load_ledger_events(root, path))
    if not pending:
        return
    if (args.func == command_stack_set and getattr(args, 'adopt_tip', None)
            and getattr(args, 'plan_digest', None) in {p['planDigest'] for p in pending}):
        return
    plan = pending[0]
    raise SyncwheelError(
        f"interrupted stack adoption: resume stack set {plan['stack']} --base {plan['base']} "
        f"--adopt-tip {plan['tip']} --plan-digest {plan['planDigest']} --apply"
    )


def execute_parsed_command(args):
    stack_absorb_preflight(args)
    recover_control_manifest_before_command(args)
    if args.command in JOURNAL_FORBIDDEN_COMMANDS and hasattr(args, 'repo'):
        repo_root, manifest_path = resolve_command_manifest_selection(args)
        manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
        if manifest and manifest.get('repository_mode') == 'journal':
            raise SyncwheelError(
                f'{args.command} is forbidden for repository_mode="journal"; use journal commands'
            )
    guarded_publishers = {
        command_stack_push, command_int_push, command_journal_publish,
        command_journal_pull, command_journal_recover, command_channel_publish, command_reconcile, command_resume,
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
            manifest, manifest_path = require_manifest(
                repo_root, args.repo, getattr(args, 'manifest', None), getattr(args, 'personal', None)
            )
            ensure_managed_repository_hooks(repo_root, manifest, manifest_path)
    return args.func(args)


def lane_stack_push_manifest_refusal(args):
    """Refuse a lane push whose control manifest differs from the primary's.

    With coordination active, stack push and int push persist their loaded
    manifest onto the integration branch before the publication guards run, so
    a lane holding an older manifest would overwrite the shared control state.
    """
    commands = {command_stack_push: 'stack push', command_int_push: 'int push'}
    command = commands.get(args.func)
    if command is None or not hasattr(args, 'repo'):
        return None
    repo_root = resolve_repo_root(args.repo).resolve()
    worktrees = get_worktrees(repo_root)
    if not worktrees or not worktrees[0].get('path'):
        return None
    primary_root = Path(worktrees[0]['path']).resolve()
    if primary_root == repo_root:
        return None
    manifest_path = resolve_manifest_path(
        repo_root, args.repo, getattr(args, 'manifest', None), getattr(args, 'personal', None)
    )
    manifest, _ = load_manifest(repo_root, manifest_path)
    # Compare against the primary's own default manifest: an explicit --manifest
    # or alias would otherwise load the same file on both sides.
    primary_manifest, _ = load_manifest(primary_root, primary_root / '.syncwheel' / 'manifest.json')
    if not any(item and coordination_is_active(item) for item in (manifest, primary_manifest)):
        return None
    if manifest and primary_manifest and manifest_digest(manifest) == manifest_digest(primary_manifest):
        return None
    return (
        f'{command} from worktree {repo_root} would publish a control manifest that differs '
        f'from the primary checkout {primary_root}; run syncwheel {command} from the primary checkout'
    )


def route_stack_add_from_governed_lane(args):
    """Use shared control state while resolving revision specs in the author lane."""
    if args.func != command_stack_add or args.manifest or args.personal:
        return
    repo_root = resolve_repo_root(args.repo)
    worktrees = get_worktrees(repo_root)
    if not worktrees or Path(worktrees[0]['path']).resolve() == repo_root.resolve():
        return
    registry, _ = load_governed_worktree_registry(repo_root)
    if not any(Path(lane['path']).resolve() == repo_root.resolve() for lane in registry['lanes']):
        return
    # Explicit selectors and personal profiles keep their existing semantics.
    primary_root = Path(worktrees[0]['path']).resolve()
    if resolve_personal(repo_root) or resolve_personal(primary_root):
        return
    args.specs = [commit for spec in args.specs for commit in commit_list_for_spec(repo_root, spec)]
    args.repo = str(primary_root)


def main():
    clear_commit_memo()
    parser = build_parser()
    raw_args = sys.argv[1:]
    passthrough = []
    if '--' in raw_args:
        marker = raw_args.index('--')
        passthrough = raw_args[marker + 1:]
        raw_args = raw_args[:marker]
    args, extras = parser.parse_known_args(raw_args)
    if extras:
        # argparse leaves specs after `stack set <id> --published` unparsed
        if getattr(args, 'func', None) is not command_stack_set or any(
            extra.startswith('-') for extra in extras
        ):
            parser.error('unrecognized arguments: ' + ' '.join(extras))
        args.specs = [*args.specs, *extras]
    args.git_args = passthrough
    if args.func == command_stack_absorb:
        args.paths.extend(passthrough)
        args.git_args = []
    # Syncwheel owns the managed branches, so its own child Git processes are
    # allowed to rewind them. The guard exists to stop every other caller.
    guard_callback = args.func in {
        command_hooks_guard,
        command_hooks_worktree_guard,
        command_hooks_ref_guard,
    }
    global SYNCWHEEL_OWNS_REF_MOVES, SYNCWHEEL_REF_AUTH_REPO
    desk_release = args.func == command_stack_release_desk
    retire_copy = args.func == command_worktree_retire_copy
    recover_stack = args.func == command_manifest_recover_stack
    adopted_metadata = False
    # Retire-copy owns its explicit, locally journaled writes. Standard
    # startup reconciliation, hooks, and implicit ref authorizations stay off.
    readonly_diagnostics = (
        args.func == command_worktree_list
        or retire_copy or recover_stack
        or (args.func in {command_status, command_check} and not args.fetch)
        or (desk_release and not args.apply)
    )
    try:
        if args.func in {command_stack_sync, command_stack_absorb}:
            adopted_metadata = True
            observation = _READONLY_DIAGNOSTICS.set(True)
            try:
                result = observed_adopted_stack_command(args)
            finally:
                _READONLY_DIAGNOSTICS.reset(observation)
            if result is not None:
                return result
            adopted_metadata = False
        if desk_release:
            args.repo = args.repo or os.environ.get(ENV_REPO)
        if readonly_diagnostics or desk_release:
            diagnostic_scope = _READONLY_DIAGNOSTICS.set(readonly_diagnostics)
            authorization_scope = _EXPLICIT_REF_AUTHORIZATION.set((desk_release or recover_stack) and args.apply)
            try:
                return args.func(args)
            finally:
                _EXPLICIT_REF_AUTHORIZATION.reset(authorization_scope)
                _READONLY_DIAGNOSTICS.reset(diagnostic_scope)
        if args.func in {command_repo_tracking_set, command_repo_tracking_status}:
            args.repo = args.repo or os.environ.get(ENV_REPO)
            if args.func == command_repo_tracking_set:
                args._tracking_selection = tracking_set_selection(args)
                if args._tracking_selection[0] == 'personal':
                    return command_repo_tracking_set(args)
                args.repo = str(args._tracking_selection[1])
        if args.func == command_stack_sync:
            args._stack_sync_selection = resolve_stack_sync_selection(args)
        if args.func == command_stack_capture_integration:
            capture_root, capture_path = resolve_command_manifest_selection(args)
            capture_path = Path(os.path.abspath(capture_path))
            args._stack_capture_selection = (capture_root, capture_path)
            require_stack_sync_metadata_paths(capture_root, capture_path)
        if args.func == command_stack_absorb:
            args._stack_absorb_selection = resolve_command_manifest_selection(args)
            stack_absorb_preflight(args)
        if not guard_callback and args.func != command_stack_sync:
            SYNCWHEEL_OWNS_REF_MOVES = True
            if hasattr(args, 'repo'):
                SYNCWHEEL_REF_AUTH_REPO = resolve_repo_root(args.repo)
        if args.func in {
            command_status, command_repo_tracking_status,
            command_repo_authority_status, command_repo_authority_set,
            command_stack_merge_pr, command_int_rebuild,
        }:
            repo_root = SYNCWHEEL_REF_AUTH_REPO
            manifest_path = resolve_manifest_path(repo_root, args.repo, args.manifest, args.personal)
            validate_selected_primary_authority_path(repo_root, manifest_path)
        stack_adoption_preflight(args)
        route_stack_add_from_governed_lane(args)
        lane_push_refusal = lane_stack_push_manifest_refusal(args)
        if lane_push_refusal:
            raise SyncwheelError(lane_push_refusal)
        # A guard callback runs inside a Git process that is mid-operation, so it
        # stays a decision about the incoming refs and skips the workspace-wide
        # preflights: those inspect other worktrees and must not run there.
        if args.command != 'revision-provider' and not guard_callback:
            primary_checkout_preflight(args)
            if args.func not in {command_stack_sync, command_stack_capture_integration}:
                maybe_handle_startup_update_policy(args)
            governed_worktree_preflight(args)
            if args.func != command_stack_capture_integration:
                converge_default_repository_hooks(args)
        with coordination_publication_lock_for_command(args) as publication_owner:
            if manifest_mutation_requested(args) and hasattr(args, 'repo'):
                repo_root, manifest_path = resolve_command_manifest_selection(args)
                if args.func not in {command_init, command_stack_capture_integration} and manifest_path.exists():
                    existing_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
                    if existing_manifest:
                        ensure_managed_repository_hooks(repo_root, existing_manifest, manifest_path)
                with manifest_write_transaction(
                    repo_root, manifest_path, 'manifest-command',
                    commit_tracked_changes=(args.func != command_stack_capture_integration
                                            and not getattr(args, '_metadata_preserve_primary', False)),
                ):
                    primary_root = getattr(args, '_metadata_primary_root', None)
                    if primary_root is not None:
                        selected, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
                        if not selected or not uses_metadata_primary_scope(args, repo_root, manifest_path, selected):
                            raise SyncwheelError('metadata update scope changed before apply; review and retry')
                        require_metadata_primary_index(primary_root)
                        if args.func == command_stack_sync:
                            if coordination_is_active(selected) and publication_owner is None:
                                raise SyncwheelError('stack sync coordination changed before apply; retry')
                            require_stack_sync_metadata_paths(repo_root, manifest_path)
                            if repo_root != primary_root:
                                require_metadata_primary_index(repo_root)
                        if not args._metadata_preserve_primary and primary_checkout_dirty_entries(primary_root):
                            raise SyncwheelError('primary checkout changed before metadata apply; review and retry')
                        require_private_metadata_paths(args, repo_root, manifest_path)
                        transaction = _manifest_storage.active_manifest_write_transaction(manifest_path)
                        transaction['preserveGitignore'] = True
                    return execute_parsed_command(args)
            return execute_parsed_command(args)
    except SyncwheelError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    finally:
        if not readonly_diagnostics and not desk_release and not adopted_metadata:
            if SYNCWHEEL_REF_AUTH_REPO:
                clear_ref_authorizations(SYNCWHEEL_REF_AUTH_REPO)
            SYNCWHEEL_REF_AUTH_REPO = None


class _JournalRepository:
    """Adapt journal I/O to the current, patchable application operations."""

    def __init__(self, root):
        self.root = root

    def command(self, repo_root, *args, **kwargs):
        return git(repo_root, *args, **kwargs)

    def output_bytes(self, repo_root, *args, ok_codes=(0,)):
        return git_bytes(repo_root, *args, ok_codes=ok_codes)

    def execute(self, command, **kwargs):
        return run(command, **kwargs)

    def binary(self, command, **kwargs):
        return subprocess.run(command, **kwargs)

    def current_branch(self, repo_root):
        return get_current_branch(repo_root)

    def tip(self, repo_root, ref):
        return ref_tip(repo_root, ref)

    def common_dir(self, repo_root):
        return git_common_dir(repo_root)

    def identity(self, repo_root, command):
        return with_git_identity(repo_root, command)

    def authorized_push(self, repo_root, command, remote, refs, **kwargs):
        return run_authorized_push(repo_root, command, remote, refs, **kwargs)

    def environment(self, **kwargs):
        return managed_process_env(**kwargs)

    def write_json(self, path, payload):
        return atomic_write_private_json(path, payload)

    def read_file(self, path, max_file_bytes):
        return journal_read_admitted_file(path, max_file_bytes)

    def fingerprint(self, path):
        return journal_file_fingerprint(path)

def command_handlers():
    """Bind parser handlers at call time, preserving facade patch points."""
    return {
        'command_channel_add': command_channel_add,
        'command_channel_apply': command_channel_apply,
        'command_channel_close': command_channel_close,
        'command_channel_contract': command_channel_contract,
        'command_channel_create': command_channel_create,
        'command_channel_diff': command_channel_diff,
        'command_channel_list': command_channel_list,
        'command_channel_operation_list': command_channel_operation_list,
        'command_channel_operation_show': command_channel_operation_show,
        'command_channel_plan': command_channel_plan,
        'command_channel_promote': command_channel_promote,
        'command_channel_publish': command_channel_publish,
        'command_channel_receipt_show': command_channel_receipt_show,
        'command_channel_reconcile_outcome': command_channel_reconcile_outcome,
        'command_channel_refresh': command_channel_refresh,
        'command_channel_remove': command_channel_remove,
        'command_channel_replace': command_channel_replace,
        'command_channel_resolve': command_channel_resolve,
        'command_channel_show': command_channel_show,
        'command_check': command_check,
        'command_coordination_claims_backfill': command_coordination_claims_backfill,
        'command_coordination_compose': command_coordination_compose,
        'command_coordination_disable': command_coordination_disable,
        'command_coordination_init': command_coordination_init,
        'command_coordination_provenance_reset': command_coordination_provenance_reset,
        'command_coordination_provenance_supersede': command_coordination_provenance_supersede,
        'command_coordination_repair': command_coordination_repair,
        'command_gc': command_gc,
        'command_handoff': command_handoff,
        'command_hooks_guard': command_hooks_guard,
        'command_hooks_install': command_hooks_install,
        'command_hooks_ref_guard': command_hooks_ref_guard,
        'command_hooks_remove': command_hooks_remove,
        'command_hooks_status': command_hooks_status,
        'command_hooks_worktree_guard': command_hooks_worktree_guard,
        'command_init': command_init,
        'command_int_align_remote': command_int_align_remote,
        'command_int_git': command_int_git,
        'command_int_push': command_int_push,
        'command_int_rebuild': command_int_rebuild,
        'command_int_show': command_int_show,
        'command_int_sync_status': command_int_sync_status,
        'command_journal_publish': command_journal_publish,
        'command_journal_pull': command_journal_pull,
        'command_journal_recover': command_journal_recover,
        'command_journal_schedule': command_journal_schedule,
        'command_journal_snapshot': command_journal_snapshot,
        'command_journal_status': command_journal_status,
        'command_ledger_show': command_ledger_show,
        'command_manifest_compare': command_manifest_compare,
        'command_manifest_recover_control': command_manifest_recover_control,
        'command_manifest_recover_stack': command_manifest_recover_stack,
        'command_manifest_require_integration': command_manifest_require_integration,
        'command_plan': command_plan,
        'command_publish': command_publish,
        'command_reconcile': command_reconcile,
        'command_replay_mode': command_replay_mode,
        'command_repo_add': command_repo_add,
        'command_repo_authority_set': command_repo_authority_set,
        'command_repo_authority_status': command_repo_authority_status,
        'command_repo_ls': command_repo_ls,
        'command_repo_mode_set': command_repo_mode_set,
        'command_repo_pr_merge_policy_clear': command_repo_pr_merge_policy_clear,
        'command_repo_pr_merge_policy_set': command_repo_pr_merge_policy_set,
        'command_repo_pr_merge_policy_status': command_repo_pr_merge_policy_status,
        'command_repo_rm': command_repo_rm,
        'command_repo_set_manifest': command_repo_set_manifest,
        'command_repo_tracking_set': command_repo_tracking_set,
        'command_repo_tracking_status': command_repo_tracking_status,
        'command_resume': command_resume,
        'command_revision_provider': command_revision_provider,
        'command_revision_provider_resolve_prepared': command_revision_provider_resolve_prepared,
        'command_self_check_update': command_self_check_update,
        'command_self_install_hooks': command_self_install_hooks,
        'command_self_mode': command_self_mode,
        'command_self_status': command_self_status,
        'command_self_update': command_self_update,
        'command_stack_absorb': command_stack_absorb,
        'command_stack_add': command_stack_add,
        'command_stack_archive': command_stack_archive,
        'command_stack_release_desk': command_stack_release_desk,
        'command_stack_capture_integration': command_stack_capture_integration,
        'command_stack_classify_integration': command_stack_classify_integration,
        'command_stack_close': command_stack_close,
        'command_stack_create': command_stack_create,
        'command_stack_demote': command_stack_demote,
        'command_stack_git': command_stack_git,
        'command_stack_land': command_stack_land,
        'command_stack_list': command_stack_list,
        'command_stack_merge_pr': command_stack_merge_pr,
        'command_stack_promote': command_stack_promote,
        'command_stack_push': command_stack_push,
        'command_stack_rebuild': command_stack_rebuild,
        'command_stack_resolve_integration': command_stack_resolve_integration,
        'command_stack_set': command_stack_set,
        'command_stack_show': command_stack_show,
        'command_stack_sync': command_stack_sync,
        'command_status': command_status,
        'command_sync': command_sync,
        'command_use': command_use,
        'command_validate': command_validate,
        'command_worktree_list': command_worktree_list,
        'command_worktree_lock': command_worktree_lock,
        'command_worktree_open': command_worktree_open,
        'command_worktree_release': command_worktree_release,
        'command_worktree_retire_copy': command_worktree_retire_copy,
        'command_worktree_unlock': command_worktree_unlock,
    }

add_rebuild_args = _cli.add_rebuild_args

add_push_args = _cli.add_push_args

add_git_args = _cli.add_git_args

add_reconcile_args = _cli.add_reconcile_args

command_parser_nodes = _cli.command_parser_nodes


class _ReplayRepository:
    """Bind replay I/O and safety queries to current application operations."""

    def __init__(self, root):
        self.root = root

    def git(self, repo_root, *args, **kwargs):
        return git(repo_root, *args, **kwargs)

    def run(self, command, **kwargs):
        return run(command, **kwargs)

    def shell(self, command, **kwargs):
        return subprocess.run(command, **kwargs)

    def commit_identity_env(self, repo_root, commit):
        return commit_identity_env(repo_root, commit)

    def commit_full_sha(self, repo_root, ref):
        return commit_full_sha(repo_root, ref)

    def commit_short_sha(self, repo_root, ref):
        return commit_short_sha(repo_root, ref)

    def branch_contains(self, repo_root, branch, commit):
        return branch_contains(repo_root, branch, commit)

    def branch_exists(self, repo_root, branch):
        return branch_exists(repo_root, branch)

    def ref_tip(self, repo_root, ref):
        return ref_tip(repo_root, ref)

    def ref_tree(self, repo_root, ref):
        return ref_tree(repo_root, ref)

    def commit_changed_files(self, repo_root, commit, limit=None):
        return commit_changed_files(repo_root, commit, limit)

    def get_current_branch(self, repo_root):
        return get_current_branch(repo_root)

    def load_repo_profile(self, repo_root):
        return load_repo_profile(repo_root)

    def find_worktree_for_branch(self, repo_root, branch):
        return find_worktree_for_branch(repo_root, branch)

    def with_git_identity(self, repo_root, command):
        return with_git_identity(repo_root, command)

    def managed_process_env(self, extra=None, authorize=True):
        return managed_process_env(extra, authorize=authorize)

    def merge_stack_resolution_commits(self, repo_root, stack, stack_ref):
        return merge_stack_resolution_commits(repo_root, stack, stack_ref)

    def stack_content_absorbed_at(self, repo_root, stack, tip):
        return stack_content_absorbed_at(repo_root, stack, tip)

    def checkout_reset_destination_lease(self, repo_root, branch, **kwargs):
        return checkout_reset_destination_lease(repo_root, branch, **kwargs)

    def checkout_source_lease(self, repo_root, manifest_path):
        return checkout_source_lease(repo_root, manifest_path)


def _replay_engine(repo_root):
    repository = _ReplayRepository(repo_root)
    return ReplayEngine(repository, repository, repository)


class _GovernedWorktreeRepository:
    """Adapt only the named worktree transport, storage and event operations."""

    def __init__(self, root):
        self.root = root

    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def run(self, *args, **kwargs):
        return run(*args, **kwargs)

    def git_common_dir(self, *args, **kwargs):
        return git_common_dir(*args, **kwargs)

    def get_worktrees(self, *args, **kwargs):
        return get_worktrees(*args, **kwargs)

    def find_worktree_record_for_branch(self, *args, **kwargs):
        return find_worktree_record_for_branch(*args, **kwargs)

    def local_worktree_status(self, *args, **kwargs):
        return local_worktree_status(*args, **kwargs)

    def ref_tip(self, *args, **kwargs):
        return ref_tip(*args, **kwargs)

    def branch_exists(self, *args, **kwargs):
        return branch_exists(*args, **kwargs)

    def commit_full_sha(self, *args, **kwargs):
        return commit_full_sha(*args, **kwargs)

    def rev_list(self, *args, **kwargs):
        return rev_list(*args, **kwargs)

    def governed_worktree_owner(self):
        return governed_worktree_owner()

    def append_ledger_event(self, *args, **kwargs):
        return append_ledger_event(*args, **kwargs)

    def ledger_events_dir(self, *args, **kwargs):
        return _manifest_storage.ledger_events_dir(*args, **kwargs)

    def load_ledger_events(self, *args, **kwargs):
        return _manifest_storage.load_ledger_events(*args, **kwargs)

    def recover_ledger_tail(self, *args, **kwargs):
        return _manifest_storage.recover_ledger_tail(*args, **kwargs)

    def load_json_file(self, *args, **kwargs):
        return load_json_file(*args, **kwargs)

    def _write_all(self, *args, **kwargs):
        return _write_all(*args, **kwargs)

    def fsync_directory_path(self, *args, **kwargs):
        return fsync_directory_path(*args, **kwargs)

    def exclusive_metadata_lock(self, *args, **kwargs):
        return exclusive_metadata_lock(*args, **kwargs)

    def coordination_publication_lock_path(self, *args, **kwargs):
        return coordination_publication_lock_path(*args, **kwargs)

    def governed_lane_remedy(self, *args, **kwargs):
        return governed_lane_remedy(*args, **kwargs)

    def governed_worktree_registry_io_checkpoint(self, *args, **kwargs):
        return governed_worktree_registry_io_checkpoint(*args, **kwargs)

    def governed_worktree_cleanup_checkpoint(self, *args, **kwargs):
        return governed_worktree_cleanup_checkpoint(*args, **kwargs)


    def delete_governed_worktree_branch_with_anchor(self, *args, **kwargs):
        return delete_governed_worktree_branch_with_anchor(*args, **kwargs)

    def ensure_governed_worktree_recovery_ref(self, *args, **kwargs):
        return ensure_governed_worktree_recovery_ref(*args, **kwargs)

    def archive_governed_worktree_tip(self, *args, **kwargs):
        return archive_governed_worktree_tip(*args, **kwargs)

    def governed_worktree_cleanup_candidates(self, *args, **kwargs):
        return governed_worktree_cleanup_candidates(*args, **kwargs)

    def append_governed_worktree_cleanup_event(self, *args, **kwargs):
        return append_governed_worktree_cleanup_event(*args, **kwargs)


def _governed_worktrees(repo_root):
    repository = _GovernedWorktreeRepository(repo_root)
    return GovernedWorktrees(repository, repository, repository, repository)


class _CoordinationStateRepository:
    """Bind state observations and schema inputs to the supported facade seams."""

    def __init__(self, root):
        self.root = root

    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def commit_exists(self, *args, **kwargs):
        return commit_exists(*args, **kwargs)

    def manifest_from_tree(self, *args, **kwargs):
        return manifest_from_tree(*args, **kwargs)

    def integration_manifest_path(self, *args, **kwargs):
        return _manifest_storage.integration_manifest_path(*args, **kwargs)

    def manifest_digest(self, *args, **kwargs):
        return manifest_digest(*args, **kwargs)

    def coordination_manifest_snapshot(self, *args, **kwargs):
        return coordination_manifest_snapshot(*args, **kwargs)

    def validate_coordination_state(self, *args, **kwargs):
        return validate_coordination_state(*args, **kwargs)

    def remote_ref_tips(self, *args, **kwargs):
        return remote_ref_tips(*args, **kwargs)

    def coordination_state_from_commit(self, *args, **kwargs):
        return coordination_state_from_commit(*args, **kwargs)

    def coordination_state_control_manifest(self, *args, **kwargs):
        return coordination_state_control_manifest(*args, **kwargs)

    def coordination_state_manifest_digest_classification(self, *args, **kwargs):
        return coordination_state_manifest_digest_classification(*args, **kwargs)

    def read_remote_coordination_states(self, *args, **kwargs):
        return read_remote_coordination_states(*args, **kwargs)


def _coordination_state_reader(repo_root):
    repository = _CoordinationStateRepository(repo_root)
    return CoordinationStateReader(repository, repository, repository)


class _PublicationIntentRepository:
    """Adapt publication intent I/O and replaceable evidence sources."""

    def __init__(self, root):
        self.root = root

    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def commit_exists(self, *args, **kwargs):
        return commit_exists(*args, **kwargs)

    def branch_exists(self, *args, **kwargs):
        return branch_exists(*args, **kwargs)

    def ref_tip(self, *args, **kwargs):
        return ref_tip(*args, **kwargs)

    def load_ledger_events(self, *args, **kwargs):
        return _manifest_storage.load_ledger_events(*args, **kwargs)

    def append_ledger_event(self, *args, **kwargs):
        return append_ledger_event(*args, **kwargs)

    def read_remote_coordination_state(self, *args, **kwargs):
        return read_remote_coordination_state(*args, **kwargs)

    def remote_ref_tips(self, *args, **kwargs):
        return remote_ref_tips(*args, **kwargs)

    def coordination_publication_lock(self, *args, **kwargs):
        return coordination_publication_lock(*args, **kwargs)

    def coordination_publication_owner(self, *args, **kwargs):
        return coordination_publication_owner(*args, **kwargs)

    def coordination_operation_manifest_digest(self, *args, **kwargs):
        return coordination_operation_manifest_digest(*args, **kwargs)

    def pending_coordination_publications(self, *args, **kwargs):
        return pending_coordination_publications(*args, **kwargs)

    def coordinated_operation_landed(self, *args, **kwargs):
        return coordinated_operation_landed(*args, **kwargs)


def _publication_intents(repo_root):
    repository = _PublicationIntentRepository(repo_root)
    return PublicationIntents(repository, repository, repository, repository, repository)


class _PublicationGit:
    def __init__(self, root):
        self.root = root

    @property
    def version(self):
        return VERSION

    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def run(self, *args, **kwargs):
        return run(*args, **kwargs)

    def branch_exists(self, *args, **kwargs):
        return branch_exists(*args, **kwargs)

    def commit_exists(self, *args, **kwargs):
        return commit_exists(*args, **kwargs)

    def commit_full_sha(self, *args, **kwargs):
        return commit_full_sha(*args, **kwargs)

    def ref_tip(self, *args, **kwargs):
        return ref_tip(*args, **kwargs)

    def ref_tree(self, *args, **kwargs):
        return ref_tree(*args, **kwargs)

    def get_worktrees(self, *args, **kwargs):
        return get_worktrees(*args, **kwargs)

    def git_common_dir(self, *args, **kwargs):
        return git_common_dir(*args, **kwargs)

    def local_worktree_status(self, *args, **kwargs):
        return local_worktree_status(*args, **kwargs)

    def is_valid_coordination_branch_ref(self, *args, **kwargs):
        return is_valid_coordination_branch_ref(*args, **kwargs)

    def installation_id(self, *args, **kwargs):
        return installation_id(*args, **kwargs)

    def governed_worktree_process_start_time(self, *args, **kwargs):
        return governed_worktree_process_start_time(*args, **kwargs)

    def run_authorized_push(self, *args, **kwargs):
        return run_authorized_push(*args, **kwargs)

    def require_new_managed_refs_absent(self, *args, **kwargs):
        return require_new_managed_refs_absent(*args, **kwargs)


class _PublicationStorage:
    def __init__(self, root):
        self.root = root

    def load_repo_profile(self, *args, **kwargs):
        return load_repo_profile(*args, **kwargs)

    def save_repo_profile(self, *args, **kwargs):
        return save_repo_profile(*args, **kwargs)

    def load_manifest(self, *args, **kwargs):
        return _manifest_storage.load_manifest(*args, **kwargs)

    def manifest_from_tree(self, *args, **kwargs):
        return manifest_from_tree(*args, **kwargs)

    def integration_manifest_path(self, *args, **kwargs):
        return _manifest_storage.integration_manifest_path(*args, **kwargs)

    def exclusive_metadata_lock(self, *args, **kwargs):
        return exclusive_metadata_lock(*args, **kwargs)

    def recover_incomplete_control_manifest_persistence(self, *args, **kwargs):
        return recover_incomplete_control_manifest_persistence(*args, **kwargs)

    def restore_control_manifest_after_integration_rebuild(self, *args, **kwargs):
        return restore_control_manifest_after_integration_rebuild(*args, **kwargs)

    def save_manifest_with_ledger(self, *args, **kwargs):
        return save_manifest_with_ledger(*args, **kwargs)


class _PublicationProjections:
    def __init__(self, root):
        self.root = root

    def derived_provenance_records(self, *args, **kwargs):
        return derived_provenance_records(*args, **kwargs)

    def integration_control_matches_selected(self, *args, **kwargs):
        return integration_control_matches_selected(*args, **kwargs)

    def integration_sync_report(self, *args, **kwargs):
        return integration_sync_report(*args, **kwargs)

    def stack_reconcile_report(self, *args, **kwargs):
        return stack_reconcile_report(*args, **kwargs)

    def require_selected_integration_control(self, *args, **kwargs):
        return require_selected_integration_control(*args, **kwargs)


class _PublicationLedger:
    def __init__(self, root):
        self.root = root

    def append_ledger_event(self, *args, **kwargs):
        return append_ledger_event(*args, **kwargs)

    def load_ledger_events(self, *args, **kwargs):
        return _manifest_storage.load_ledger_events(*args, **kwargs)

    def latest_channel_event(self, *args, **kwargs):
        return latest_channel_event(*args, **kwargs)


def _publication_coordinator(repo_root):
    return PublicationCoordinator(
        _PublicationGit(repo_root), _PublicationStorage(repo_root),
        _PublicationProjections(repo_root), _PublicationLedger(repo_root),
        _coordination_state_reader(repo_root), _publication_intents(repo_root),
        _replay_engine(repo_root),
    )





class _ProvenanceRepository:
    def branch_contains(self, *args, **kwargs):
        return branch_contains(*args, **kwargs)

    def commit_changed_files(self, *args, **kwargs):
        return commit_changed_files(*args, **kwargs)

    def commit_exists(self, *args, **kwargs):
        return commit_exists(*args, **kwargs)

    def commit_full_sha(self, *args, **kwargs):
        return commit_full_sha(*args, **kwargs)

    def commit_parent_count(self, *args, **kwargs):
        return commit_parent_count(*args, **kwargs)

    def commit_path_blob(self, *args, **kwargs):
        return commit_path_blob(*args, **kwargs)

    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def git_common_dir(self, *args, **kwargs):
        return git_common_dir(*args, **kwargs)

    def parsed_commit_trailers(self, *args, **kwargs):
        return parsed_commit_trailers(*args, **kwargs)

    def ref_tip(self, *args, **kwargs):
        return ref_tip(*args, **kwargs)

    def tree_path_entry(self, *args, **kwargs):
        return tree_path_entry(*args, **kwargs)


class _ProvenanceState:
    def coordination_profile(self, *args, **kwargs):
        return coordination_profile(*args, **kwargs)

    def coordination_state_from_commit(self, *args, **kwargs):
        return coordination_state_from_commit(*args, **kwargs)


def _provenance_store():
    return ProvenanceStore(_ProvenanceRepository(), _ProvenanceState())



class _ProjectionRepository:
    def commit_full_sha(self, *args, **kwargs):
        return commit_full_sha(*args, **kwargs)

    def find_worktree_for_branch(self, *args, **kwargs):
        return find_worktree_for_branch(*args, **kwargs)

    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def journal_status_entries(self, *args, **kwargs):
        return journal_status_entries(*args, **kwargs)

    def managed_process_env(self, *args, **kwargs):
        return managed_process_env(*args, **kwargs)

    def ref_tip(self, *args, **kwargs):
        return ref_tip(*args, **kwargs)

    def ref_tree(self, *args, **kwargs):
        return ref_tree(*args, **kwargs)


class _ProjectionCoordination:
    def coordination_state_control_manifest(self, *args, **kwargs):
        return coordination_state_control_manifest(*args, **kwargs)

    def read_remote_coordination_state(self, *args, **kwargs):
        return read_remote_coordination_state(*args, **kwargs)

    def remote_ref_tips(self, *args, **kwargs):
        return remote_ref_tips(*args, **kwargs)

    def verify_coordination_state_manifest_digest(self, *args, **kwargs):
        return verify_coordination_state_manifest_digest(*args, **kwargs)


class _ProjectionReplay:
    def materialize_integration_projection(self, *args, **kwargs):
        return materialize_integration_projection(*args, **kwargs)


class _ProjectionControl:
    def manifest_from_tree(self, *args, **kwargs):
        return manifest_from_tree(*args, **kwargs)


class _ProjectionIgnorePolicy:
    def split_syncwheel_managed_gitignore(self, *args, **kwargs):
        return split_syncwheel_managed_gitignore(*args, **kwargs)


def _projection_service():
    return ProjectionService(
        _ProjectionRepository(), _ProjectionCoordination(), _ProjectionReplay(),
        _ProjectionControl(), _ProjectionIgnorePolicy(),
    )



class _IntegrationHistoryRepository:
    def branch_contains(self, *args, **kwargs):
        return branch_contains(*args, **kwargs)

    def commit_changed_files(self, *args, **kwargs):
        return commit_changed_files(*args, **kwargs)

    def commit_exists(self, *args, **kwargs):
        return commit_exists(*args, **kwargs)

    def commit_first_parent(self, *args, **kwargs):
        return commit_first_parent(*args, **kwargs)

    def commit_full_sha(self, *args, **kwargs):
        return commit_full_sha(*args, **kwargs)

    def commit_parent_count(self, *args, **kwargs):
        return commit_parent_count(*args, **kwargs)

    def commit_patch_id(self, *args, **kwargs):
        return commit_patch_id(*args, **kwargs)

    def commit_patch_ids(self, *args, **kwargs):
        return commit_patch_ids(*args, **kwargs)

    def find_worktree_for_branch(self, *args, **kwargs):
        return find_worktree_for_branch(*args, **kwargs)

    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def parsed_commit_trailers(self, *args, **kwargs):
        return parsed_commit_trailers(*args, **kwargs)

    def patch_ids_reachable_from_ref(self, *args, **kwargs):
        return patch_ids_reachable_from_ref(*args, **kwargs)

    def ref_tip(self, *args, **kwargs):
        return ref_tip(*args, **kwargs)

    def ref_tree(self, *args, **kwargs):
        return ref_tree(*args, **kwargs)

    def replay_commit_env(self, *args, **kwargs):
        return replay_commit_env(*args, **kwargs)

    def rev_list(self, *args, **kwargs):
        return rev_list(*args, **kwargs)


class _IntegrationHistoryCoordination:
    def coordination_state_chain_contains(self, *args, **kwargs):
        return coordination_state_chain_contains(*args, **kwargs)

    def coordination_state_control_manifest(self, *args, **kwargs):
        return coordination_state_control_manifest(*args, **kwargs)

    def coordination_state_from_commit(self, *args, **kwargs):
        return coordination_state_from_commit(*args, **kwargs)

    def coordination_state_manifest_digest_classification(self, *args, **kwargs):
        return coordination_state_manifest_digest_classification(*args, **kwargs)

    def is_valid_coordination_branch_ref(self, *args, **kwargs):
        return is_valid_coordination_branch_ref(*args, **kwargs)

    def verify_coordination_state_manifest_digest(self, *args, **kwargs):
        return verify_coordination_state_manifest_digest(*args, **kwargs)


class _IntegrationHistoryReplay:
    def deterministic_stack_projection(self, *args, **kwargs):
        return deterministic_stack_projection(*args, **kwargs)

    def materialize_integration_replay(self, *args, **kwargs):
        return materialize_integration_replay(*args, **kwargs)


class _IntegrationHistoryIgnorePolicy:
    def split_historical_syncwheel_managed_gitignore(self, *args, **kwargs):
        return split_historical_syncwheel_managed_gitignore(*args, **kwargs)

    def split_syncwheel_managed_gitignore(self, *args, **kwargs):
        return split_syncwheel_managed_gitignore(*args, **kwargs)


def _integration_history():
    return IntegrationHistory(
        _IntegrationHistoryRepository(), _IntegrationHistoryCoordination(),
        _IntegrationHistoryReplay(), _IntegrationHistoryIgnorePolicy(),
        control=_manifest_store(), provenance=_provenance_store(),
        projection=_projection_service(),
    )


class _ManifestRepository:
    def control_manifest_commit_env(self, *args, **kwargs):
        return control_manifest_commit_env(*args, **kwargs)

    def find_worktree_for_branch(self, *args, **kwargs):
        return find_worktree_for_branch(*args, **kwargs)

    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def git_common_dir(self, *args, **kwargs):
        return git_common_dir(*args, **kwargs)

    def git_identity(self, *args, **kwargs):
        return git_identity(*args, **kwargs)

    def ref_tip(self, *args, **kwargs):
        return ref_tip(*args, **kwargs)

    def ref_tree(self, *args, **kwargs):
        return ref_tree(*args, **kwargs)

    def run(self, *args, **kwargs):
        return run(*args, **kwargs)


class _ManifestProjection:
    def apply_integration_reconciliation(self, *args, **kwargs):
        return apply_integration_reconciliation(*args, **kwargs)

    def checkout_path_fingerprint(self, *args, **kwargs):
        return checkout_path_fingerprint(*args, **kwargs)

    def checkout_source_lease(self, *args, **kwargs):
        return checkout_source_lease(*args, **kwargs)


class _ManifestWorkspace:
    def commit_git_tracked_manifest(self, *args, **kwargs):
        return commit_git_tracked_manifest(*args, **kwargs)

    def ensure_syncwheel_metadata_excluded(self, *args, **kwargs):
        return ensure_syncwheel_metadata_excluded(*args, **kwargs)


class _ManifestLifecycle:
    def require_current_stack_create_operation(self, *args, **kwargs):
        return require_current_stack_create_operation(*args, **kwargs)


def _manifest_store():
    return ManifestStore(
        _ManifestRepository(), _ManifestProjection(),
        _ManifestWorkspace(), _ManifestLifecycle(),
    )


class _StackRepository:
    def __init__(self, root):
        self.root = root

    def legacy_stack_push_lease(self, *args, **kwargs):
        return legacy_stack_push_lease(*args, **kwargs)

    def forced_push_requested(self, *args, **kwargs):
        return forced_push_requested(*args, **kwargs)

    def without_force_push_args(self, *args, **kwargs):
        return without_force_push_args(*args, **kwargs)

    def explicit_push_leases(self, *args, **kwargs):
        return explicit_push_leases(*args, **kwargs)

    def branch_contains(self, *args, **kwargs):
        return branch_contains(*args, **kwargs)

    def branch_exists(self, *args, **kwargs):
        return branch_exists(*args, **kwargs)

    def commit_exists(self, *args, **kwargs):
        return commit_exists(*args, **kwargs)

    def commit_first_parent(self, *args, **kwargs):
        return commit_first_parent(*args, **kwargs)

    def commit_full_sha(self, *args, **kwargs):
        return commit_full_sha(*args, **kwargs)

    def commit_list_for_spec(self, *args, **kwargs):
        return commit_list_for_spec(*args, **kwargs)

    def commit_short_sha(self, *args, **kwargs):
        return commit_short_sha(*args, **kwargs)

    def fetch_observed_delivery_tip(self, *args, **kwargs):
        return fetch_observed_delivery_tip(*args, **kwargs)

    def find_worktree_for_branch(self, *args, **kwargs):
        return find_worktree_for_branch(*args, **kwargs)

    def get_current_branch(self, *args, **kwargs):
        return get_current_branch(*args, **kwargs)

    def get_worktrees(self, *args, **kwargs):
        return get_worktrees(*args, **kwargs)

    def binary_git(self, path, *args, input_data=None, index=None, recovery_identity=False):
        env = {'GIT_OPTIONAL_LOCKS': '0'}
        if index is not None:
            env['GIT_INDEX_FILE'] = str(index)
        if recovery_identity:
            for role in ('AUTHOR', 'COMMITTER'):
                env.update({f'GIT_{role}_NAME': 'Syncwheel',
                            f'GIT_{role}_EMAIL': 'syncwheel@example.invalid',
                            f'GIT_{role}_DATE': '2000-01-01T00:00:00+00:00'})
        result = subprocess.run(
            ['git', '-c', 'commit.gpgsign=false', *args], cwd=path, input=input_data,
            env=managed_process_env(env, authorize=False), capture_output=True,
        )
        if result.returncode:
            raise SyncwheelError(result.stderr.decode(errors='replace').strip() or 'desk snapshot Git command failed')
        return result.stdout

    def write_desk_intent(self, path, payload):
        common = git_common_dir(self.root)
        require_private_tracking_path(common, path)
        atomic_write_private_json(path, payload, indent=2)
        fsync_directory_path(path.parent)
        fsync_directory_path(path.parent.parent)

    def publish_desk_recovery_refs(self, operations):
        with ref_move_authorization(self.root) as nonce:
            self.capture_ref_transaction(operations, {MANAGED_REF_MOVE_AUTH_ENV: nonce})

    def capture_ref_transaction(self, operations, authorization=None, *, checked_out_ref=None,
                                check_prepared=None, fsync_refs=False):
        commands = ['start']
        for operation in operations:
            commands.extend(('option no-deref', operation))
        # Old-OID checks on some Git versions accept symbolic references to
        # that same OID. Inspect ref kinds after Git has acquired its locks.
        options = ['-c', 'core.fsync=reference'] if fsync_refs else []
        with tempfile.TemporaryFile(mode='w+t') as errors, subprocess.Popen(
            ['git', *options, 'update-ref', '--stdin'],
            cwd=self.root, env=managed_process_env(authorization),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True,
        ) as process:
            def close_transaction():
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

            def require_response(expected):
                if process.stdout.readline().strip() != expected:
                    close_transaction()
                    errors.seek(0)
                    raise SyncwheelError(errors.read().strip() or f'missing Git response: {expected}')

            try:
                process.stdin.write('\n'.join([*commands, 'prepare', '']))
                process.stdin.flush()
                require_response('start: ok')
                require_response('prepare: ok')
                for operation in operations:
                    action, ref, *_ = operation.split()
                    if git(self.root, 'symbolic-ref', '--quiet', ref, check=False).returncode == 0:
                        raise SyncwheelError(f'capture ref became symbolic before commit: {ref}')
                    if action != 'verify' and ref != checked_out_ref and find_worktree_for_branch(
                        self.root, ref.removeprefix('refs/heads/'),
                    ):
                        raise SyncwheelError(f'capture ref is checked out before commit: {ref}')
                if check_prepared is not None:
                    check_prepared()
                process.stdin.write('commit\n')
                process.stdin.flush()
                require_response('commit: ok')
            except BaseException:
                # EOF aborts a prepared transaction without installing refs.
                close_transaction()
                raise

    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def governed_worktree_owner(self, *args, **kwargs):
        return governed_worktree_owner(*args, **kwargs)

    def is_valid_coordination_branch_ref(self, *args, **kwargs):
        return is_valid_coordination_branch_ref(*args, **kwargs)

    def ref_tip(self, *args, **kwargs):
        return ref_tip(*args, **kwargs)

    def ref_tree(self, *args, **kwargs):
        return ref_tree(*args, **kwargs)

    def resolve_git_worktree(self, *args, **kwargs):
        return resolve_git_worktree(*args, **kwargs)

    def rev_list(self, *args, **kwargs):
        return rev_list(*args, **kwargs)

    def run(self, *args, **kwargs):
        return run(*args, **kwargs)

    def run_authorized_push(self, *args, **kwargs):
        return run_authorized_push(*args, **kwargs)

    def with_git_identity(self, *args, **kwargs):
        return with_git_identity(*args, **kwargs)

class _StackControl:
    def apply_coordination_snapshot(self, *args, **kwargs):
        return apply_coordination_snapshot(*args, **kwargs)

    def ensure_syncwheel_metadata_excluded(self, *args, **kwargs):
        return ensure_syncwheel_metadata_excluded(*args, **kwargs)

    def ensure_syncwheel_worktree_root_excluded(self, *args, **kwargs):
        return ensure_syncwheel_worktree_root_excluded(*args, **kwargs)

    def is_external_manifest_path(self, *args, **kwargs):
        return _manifest_storage.is_external_manifest_path(*args, **kwargs)

    def manifest_write_transaction(self, *args, **kwargs):
        return manifest_write_transaction(*args, **kwargs)

    def recover_incomplete_control_manifest_persistence(self, *args, **kwargs):
        return recover_incomplete_control_manifest_persistence(*args, **kwargs)

    def require_locked_manifest_observation(self, *args, **kwargs):
        return _manifest_storage.require_locked_manifest_observation(*args, **kwargs)

    def require_manifest(self, *args, **kwargs):
        return require_manifest(*args, **kwargs)

    def require_manifest_transaction_current(self, *args, **kwargs):
        return _manifest_storage.require_manifest_transaction_current(*args, **kwargs)

    def restore_control_manifest_after_integration_rebuild(self, *args, **kwargs):
        return restore_control_manifest_after_integration_rebuild(*args, **kwargs)

    def save_manifest(self, *args, **kwargs):
        return _manifest_storage.save_manifest(*args, **kwargs)

    def prepare_capture_metadata(self, *args, **kwargs):
        return _manifest_storage.prepare_capture_metadata(*args, **kwargs)

    def save_manifest_with_ledger(self, *args, **kwargs):
        return save_manifest_with_ledger(*args, **kwargs)

class _StackLedger:
    def append_ledger_event(self, *args, **kwargs):
        return append_ledger_event(*args, **kwargs)

    def load_ledger_events(self, *args, **kwargs):
        return _manifest_storage.load_ledger_events(*args, **kwargs)

class _StackProjections:
    def register_created_desk(self, repo_root, manifest_path, stack, path):
        return _desk_lifecycle(repo_root).register_created(manifest_path, stack, path)

    def archive_desk_remedy(self, repo_root, manifest_path, stack, worktree):
        primary = primary_authority_manifest_path(repo_root).parent.parent
        selected = manifest_path or primary / '.syncwheel/manifest.json'
        desks = _desk_lifecycle(repo_root)
        pending = desks.pending_release(selected, stack)
        args = ['syncwheel', 'stack', 'release-desk', stack['id']]
        if pending:
            args.extend(('--reason', pending['reason']))
            if pending['legacy']:
                args.extend(('--legacy-worktree', pending['path']))
            args.extend(('--confirm-writers-stopped', '--plan-digest', pending['planDigest'], '--apply'))
        elif worktree:
            worktree = Path(worktree).resolve()
            if worktree == primary:
                return 'preserve the primary checkout and move the stack authoring work to a separate desk'
            registry, _ = _governed_worktrees(repo_root).load_governed_worktree_registry()
            desks.require_no_lane(registry, stack, worktree, governed_worktree_admin_dir_for_path(repo_root, worktree))
            args.extend(('--reason', 'Release the stack desk'))
            if not any(item['path'] == str(Path(worktree).resolve()) and item['stack'] == stack['id']
                       and item['manifest'] == str(selected.resolve()) for item in desks.desks()):
                args.extend(('--legacy-worktree', str(worktree)))
        else:
            return None
        args.extend(manifest_path_remedy_options(repo_root, selected))
        action = 'confirm that writers are stopped and resume' if pending else 'inspect ownership and preview'
        return f'{action} with {shlex.join(args)}'

    def checkout_reset_destination_lease(self, *args, **kwargs):
        return checkout_reset_destination_lease(*args, **kwargs)

    def capture_integration_desk(self, *args, **kwargs):
        return capture_integration_desk(*args, **kwargs)

    def ensure_in_place_target(self, *args, **kwargs):
        return ensure_in_place_target(*args, **kwargs)

    def ensure_non_in_place_target_clean(self, *args, **kwargs):
        return ensure_non_in_place_target_clean(*args, **kwargs)

    def reconcile_worktree_path(self, *args, **kwargs):
        return reconcile_worktree_path(*args, **kwargs)

    def resolve_stack_rebuild_location(self, *args, **kwargs):
        return resolve_stack_rebuild_location(*args, **kwargs)

    def stack_content_absorbed_at(self, *args, **kwargs):
        return stack_content_absorbed_at(*args, **kwargs)

    def stack_reconcile_report(self, *args, **kwargs):
        return stack_reconcile_report(*args, **kwargs)

    def verify_retained_captured_worktrees(self, *args, **kwargs):
        return verify_retained_captured_worktrees(*args, **kwargs)

class _StackContinuation:
    def command_stack_close(self, *args, **kwargs):
        return command_stack_close(*args, **kwargs)


def _desk_lifecycle(repo_root):
    return DeskLifecycle(_StackRepository(repo_root), _governed_worktrees(repo_root))


def _stack_lifecycle(repo_root):
    return StackLifecycle(
        _StackRepository(repo_root), _StackControl(), _StackLedger(),
        _StackProjections(), _StackContinuation(),
        state=_coordination_state_reader(repo_root),
        intents=_publication_intents(repo_root),
        publication=_publication_coordinator(repo_root),
        replay=_replay_engine(repo_root), worktrees=_governed_worktrees(repo_root),
    )


class _RevisionRepository:
    def git(self, *args, **kwargs):
        return git(*args, **kwargs)

    def run(self, *args, **kwargs):
        return run(*args, **kwargs)

    def managed_process_env(self, *args, **kwargs):
        return managed_process_env(*args, **kwargs)

    def with_git_identity(self, *args, **kwargs):
        return with_git_identity(*args, **kwargs)

    def git_common_dir(self, *args, **kwargs):
        return git_common_dir(*args, **kwargs)

    def get_current_branch(self, *args, **kwargs):
        return get_current_branch(*args, **kwargs)

    def get_worktrees(self, *args, **kwargs):
        return get_worktrees(*args, **kwargs)

    def branch_exists(self, *args, **kwargs):
        return branch_exists(*args, **kwargs)

    def commit_full_sha(self, *args, **kwargs):
        return commit_full_sha(*args, **kwargs)

    def ref_tip(self, *args, **kwargs):
        return ref_tip(*args, **kwargs)

    def ref_tree(self, *args, **kwargs):
        return ref_tree(*args, **kwargs)

    def rev_list(self, *args, **kwargs):
        return rev_list(*args, **kwargs)

    def is_manifest_only_commit(self, *args, **kwargs):
        return is_manifest_only_commit(*args, **kwargs)


class _RevisionManifestStore:
    def require_manifest(self, *args, **kwargs):
        return require_manifest(*args, **kwargs)

    def save_manifest(self, *args, **kwargs):
        return _manifest_storage.save_manifest(*args, **kwargs)

    def manifest_write_transaction(self, *args, **kwargs):
        return manifest_write_transaction(*args, **kwargs)

    def require_manifest_transaction_current(self, *args, **kwargs):
        return _manifest_storage.require_manifest_transaction_current(*args, **kwargs)

    def validate_manifest(self, *args, **kwargs):
        return validate_manifest(*args, **kwargs)

    def missing_declared_stacks_message(self, *args, **kwargs):
        return missing_declared_stacks_message(*args, **kwargs)

    def handoff_misalignment_detail(self, *args, **kwargs):
        return handoff_misalignment_detail(*args, **kwargs)


class _RevisionLedger:
    def append_ledger_event(self, *args, **kwargs):
        return append_ledger_event(*args, **kwargs)

    def load_ledger_events(self, *args, **kwargs):
        return _manifest_storage.load_ledger_events(*args, **kwargs)

    def recover_ledger_tail(self, *args, **kwargs):
        return _manifest_storage.recover_ledger_tail(*args, **kwargs)

    def write_ledger_checkpoint(self, *args, **kwargs):
        return _manifest_storage.write_ledger_checkpoint(*args, **kwargs)

    def historically_abandoned_unmapped_commits(self, *args, **kwargs):
        return historically_abandoned_unmapped_commits(*args, **kwargs)

    def historically_reconciled_unmapped_commits(self, *args, **kwargs):
        return historically_reconciled_unmapped_commits(*args, **kwargs)


class _RevisionGuards:
    def active_hooks_dir(self, *args, **kwargs):
        return active_hooks_dir(*args, **kwargs)

    def managed_push_guard_policy(self, *args, **kwargs):
        return managed_push_guard_policy(*args, **kwargs)

    def authorize_ref_move(self, *args, **kwargs):
        return authorize_ref_move(*args, **kwargs)

    def ref_auth_dir(self, *args, **kwargs):
        return ref_auth_dir(*args, **kwargs)

    def ref_move_authorization(self, *args, **kwargs):
        return ref_move_authorization(*args, **kwargs)


class _RevisionProvenance:
    def is_derived_projection_commit(self, *args, **kwargs):
        return is_derived_projection_commit(*args, **kwargs)

    def record_common_derived_provenance(self, *args, **kwargs):
        return record_common_derived_provenance(*args, **kwargs)

    def resolve_common_derived_provenance(self, *args, **kwargs):
        return resolve_common_derived_provenance(*args, **kwargs)


class _RevisionReplayFactory:
    def for_repository(self, root):
        return _replay_engine(root)


class _RevisionStateFactory:
    def for_repository(self, root):
        return _coordination_state_reader(root)


class _RevisionCoordinationFactory:
    def for_repository(self, root):
        return _publication_coordinator(root)


if __name__ == '__main__':
    raise SystemExit(main())
