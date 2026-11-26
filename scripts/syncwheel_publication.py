"""Coordination publication, source leases, snapshots and atomic ref transitions.

State observation, durable intents and replay are composed services. Repository,
control-file storage, lifecycle ledger and projection queries are explicit ports.
"""

import contextlib
import copy
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
import uuid
from typing import Protocol

import syncwheel_selective_publication as selective
import syncwheel_coordination as snapshot_model
import syncwheel_publication_intents as _publication_policy
from syncwheel_contracts import (
    iso_utc_now,
)
from syncwheel_coordination import (
    COORDINATION_REMOTE_ROLE_CANONICAL,
    COORDINATION_REMOTE_ROLE_PUBLICATION,
    changed_snapshot_fields,
    channel_snapshot_map,
    coordination_claim_ref,
    coordination_identity_snapshot,
    coordination_snapshot_schema,
    coordination_tombstone_ref,
    merge_coordination_snapshots,
    stack_snapshot_map,
)
from syncwheel_coordination_state import (
    COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST,
    COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT,
    COORDINATION_STATE_FILE,
)
from syncwheel_errors import (
    CoordinationRemoteRejection,
    SyncwheelError,
)
from syncwheel_ledger import (
    manifest_channel_history_summary,
)
from syncwheel_manifest import (
    MANIFEST_VERSION_CHANNELS,
    canonical_json_digest,
    canonical_manifest_json,
    channel_composition_digest,
    coordination_config,
    coordination_state_ref,
    manifest_digest,
    normalize_coordination_id,
    normalize_derived_provenance,
    stack_map,
)
from syncwheel_publication_intents import (
    COORDINATION_CLAIM_FILE,
    EXPECTED_COORDINATION_STATE_UNSET,
    coordinated_publish_retry_hint,
    coordination_state_document_matches_operation,
)
from syncwheel_replay import (
    quoted,
    syncwheel_timestamp,
)


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

COORDINATION_PUBLICATION_LOCK_TIMEOUT_SECONDS = 5

COORDINATION_PUBLICATION_LOCK_STALE_SECONDS = 300

STACK_OWNER_STATE_SCAN_LIMIT = 50


_COORDINATION_PUBLICATION_LOCK_STATE = threading.local()


def coordination_manifest_remote_roles(manifest):
    """Map only portable manifest roles to local Git remote aliases."""
    defaults = manifest['defaults']
    canonical_remote = defaults['canonical_remote']
    publication_remote = defaults['publication_remote']
    roles = {canonical_remote: COORDINATION_REMOTE_ROLE_CANONICAL}
    if publication_remote != canonical_remote:
        roles[publication_remote] = COORDINATION_REMOTE_ROLE_PUBLICATION
    return roles


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


def local_lease_is_active(coordination):
    lease = coordination.get('lease') or {}
    if not isinstance(lease, dict):
        return False
    expires_at = lease.get('expires_at')
    if not isinstance(expires_at, (int, float)):
        return False
    return expires_at > time.time()


def coordination_publication_lock_holders():
    holders = getattr(_COORDINATION_PUBLICATION_LOCK_STATE, 'holders', None)
    if holders is None:
        holders = {}
        _COORDINATION_PUBLICATION_LOCK_STATE.holders = holders
    return holders


def coordination_publication_busy_error(metadata, stale):
    pid = metadata.get('pid', 'unknown')
    since = metadata.get('acquired_at', 'an unknown time')
    suffix = (
        ' (the lock metadata could not prove that its owner died; inspect it manually)'
        if stale else ''
    )
    return SyncwheelError(
        f'a coordinated publication is in flight in this clone (pid {pid}, since {since}); '
        'retry when the in-flight publication finishes' + suffix
    )


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


def format_stack_owner(summary):
    parts = []
    local = summary.get('local')
    if local:
        parts.append(
            f"last local {local['event']} by {local.get('actor') or 'unknown actor'} "
            f"at {local.get('ts')}"
        )
    published = summary.get('published')
    if published:
        parts.append(
            f"last published {published['scope']} at {published['created_at']} "
            f"from installation {published['installation_id']}"
            + (f", commit author {published['author']}" if published.get('author') else '')
        )
    return f"owner: {'; '.join(parts)}" if parts else 'owner unknown'


class PublicationRepository(Protocol):
    root: Path
    version: str
    def git(self, *args, **kwargs): ...
    def run(self, *args, **kwargs): ...
    def branch_exists(self, *args, **kwargs): ...
    def commit_exists(self, *args, **kwargs): ...
    def commit_full_sha(self, *args, **kwargs): ...
    def ref_tip(self, *args, **kwargs): ...
    def ref_tree(self, *args, **kwargs): ...
    def get_worktrees(self, *args, **kwargs): ...
    def git_common_dir(self, *args, **kwargs): ...
    def local_worktree_status(self, *args, **kwargs): ...
    def is_valid_coordination_branch_ref(self, *args, **kwargs): ...
    def installation_id(self, *args, **kwargs): ...
    def governed_worktree_process_start_time(self, *args, **kwargs): ...
    def run_authorized_push(self, *args, **kwargs): ...
    def require_new_managed_refs_absent(self, *args, **kwargs): ...


class PublicationStorage(Protocol):
    def load_repo_profile(self, *args, **kwargs): ...
    def save_repo_profile(self, *args, **kwargs): ...
    def load_manifest(self, *args, **kwargs): ...
    def manifest_from_tree(self, *args, **kwargs): ...
    def integration_manifest_path(self, *args, **kwargs): ...
    def exclusive_metadata_lock(self, *args, **kwargs): ...
    def recover_incomplete_control_manifest_persistence(self, *args, **kwargs): ...
    def restore_control_manifest_after_integration_rebuild(self, *args, **kwargs): ...
    def save_manifest_with_ledger(self, *args, **kwargs): ...


class PublicationProjections(Protocol):
    def derived_provenance_records(self, *args, **kwargs): ...
    def integration_control_matches_selected(self, *args, **kwargs): ...
    def integration_sync_report(self, *args, **kwargs): ...
    def stack_reconcile_report(self, *args, **kwargs): ...
    def require_selected_integration_control(self, *args, **kwargs): ...


class PublicationLedger(Protocol):
    def append_ledger_event(self, *args, **kwargs): ...
    def load_ledger_events(self, *args, **kwargs): ...
    def latest_channel_event(self, *args, **kwargs): ...


class PublicationStates(Protocol):
    def read_remote_coordination_state(self, config, fetch=True, local_manifest_version=None): ...
    def remote_ref_tips(self, remote, refs): ...
    def coordination_state_control_manifest(self, state, remote=None): ...
    def coordination_state_from_commit(self, commit, expected_id=None, claims_mode='advisory'): ...
    def require_exclusive_coordination_ownership(self, config, managed_refs, expected_state_refs=None, *, fetch=True): ...
    def verify_coordination_state_manifest_digest(self, state, remote=None): ...


class PublicationIntentService(Protocol):
    def coordination_state_document_from_commit(self, commit): ...
    def coordinated_operation_landed(self, config, observed, operation, *, claims_fallback=False): ...
    def fetch_coordination_claim(self, remote, claim_ref, tip): ...


class PublicationReplay(Protocol):
    def deterministic_stack_replay_tip(self, base, commits): ...
    def backup_branch_command(self, branch, timestamp): ...


class PublicationCoordinator:
    def __init__(self, repository: PublicationRepository, storage: PublicationStorage,
                 projections: PublicationProjections, ledger: PublicationLedger,
                 states: PublicationStates, intents: PublicationIntentService,
                 replay: PublicationReplay):
        self.repository = repository
        self.storage = storage
        self.projections = projections
        self.ledger = ledger
        self.states = states
        self.intents = intents
        self.replay = replay

    def _apply_snapshot(self, manifest, snapshot):
        return snapshot_model.apply_coordination_snapshot(
            manifest, snapshot,
            branch_is_valid=self.repository.is_valid_coordination_branch_ref,
        )

    def _validate_state(self, state, expected_id=None, claims_mode='advisory'):
        return snapshot_model.validate_coordination_state(
            state, expected_id, claims_mode,
            branch_is_valid=self.repository.is_valid_coordination_branch_ref,
        )

    def public_coordination_remote_ref(self, role, branch, path):
        ref = f'refs/heads/{branch}' if isinstance(branch, str) else None
        if not self.repository.is_valid_coordination_branch_ref(ref):
            raise SyncwheelError(f'{path} has an invalid remote branch ref')
        return {
            'kind': 'remote-ref',
            'role': role,
            'ref': ref,
        }

    def public_coordination_ref(self, value, remote_roles, repo_root=None, path='manifest ref'):
        """Project a local ref into a portable public ref without collapsing remote identity."""
        if not isinstance(value, str):
            return value
        if value.startswith('refs/remotes/'):
            for remote in sorted(remote_roles, key=len, reverse=True):
                remote_tracking_prefix = f'refs/remotes/{remote}/'
                if value.startswith(remote_tracking_prefix):
                    return self.public_coordination_remote_ref(
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
                return self.public_coordination_remote_ref(
                    remote_roles[remote], value[len(remote_prefix):], path
                )
        if value.startswith('refs/') or '/' not in value:
            return value
        if repo_root is not None:
            configured_remotes = {
                line.strip()
                for line in self.repository.git(repo_root, 'remote').stdout.splitlines()
                if line.strip()
            }
            for remote in sorted(configured_remotes - set(remote_roles), key=len, reverse=True):
                if value.startswith(f'{remote}/'):
                    raise SyncwheelError(
                        f'{path} uses an unrecognized local remote alias: {value!r}; '
                        'use the canonical or publication remote role instead'
                    )
            if self.repository.branch_exists(repo_root, value):
                return f'refs/heads/{value}'
        raise SyncwheelError(
            f'{path} may contain an unrecognized local remote alias: {value!r}; '
            'use the canonical or publication remote role, or an explicit refs/heads/... local branch'
        )

    def coordination_manifest_snapshot(self, manifest, repo_root=None, coordination_state=None):
        """Return the public, topology-only projection stored in remote coordination state."""
        defaults = manifest['defaults']
        remote_roles = coordination_manifest_remote_roles(manifest)
        snapshot = {
            'version': manifest['version'],
            'defaults': {
                'base_branch': defaults['base_branch'],
                'base_ref': self.public_coordination_ref(
                    defaults['base_ref'], remote_roles, repo_root, 'defaults.base_ref'
                ),
            },
            'integration': {
                'branch': manifest['integration']['branch'],
                'base': self.public_coordination_ref(
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
                self.projections.derived_provenance_records(repo_root, manifest, coordination_state)
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
                'base': self.public_coordination_ref(
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
            if stack.get('adopted_history'):
                snapshot_stack['adopted_history'] = dict(stack['adopted_history'])
            resolution = {key: copy.deepcopy(stack[key])
                          for key in ('integration_commits', 'integration_only_commits', 'integration_resolution')
                          if key in stack}
            if stack.get('integration_resolution'):
                # Resolution is integration-owned state, not a source-branch edit.
                # int push carries both the replay objects and these bindings atomically.
                snapshot['integration'].setdefault('resolutions', {})[stack['id']] = resolution
            snapshot['stacks'].append(snapshot_stack)
        if manifest.get('version') == MANIFEST_VERSION_CHANNELS:
            snapshot['channels'] = []
            for channel in manifest.get('channels', []):
                snapshot_channel = manifest_channel_history_summary(channel)
                snapshot_channel.pop('remote', None)
                snapshot_channel['base'] = self.public_coordination_ref(
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

    def coordination_manifest_digest(self, manifest, repo_root=None):
        if repo_root is None:
            return manifest_digest(manifest)
        integration = manifest.get('integration') or {}
        branch = integration.get('branch')
        if not branch:
            raise SyncwheelError('manifest has no integration branch for its control digest')
        tip = self.repository.ref_tip(repo_root, branch)
        if not tip:
            raise SyncwheelError(f'integration branch is missing for control digest: {branch}')
        committed = self.storage.manifest_from_tree(
            repo_root, tip, self.storage.integration_manifest_path(repo_root)
        )
        if committed is None:
            # Read-only planning may precede the first coordinated publication.
            # The apply path materializes these exact bytes on the integration tip
            # before it builds or publishes coordination state.
            return manifest_digest(manifest)
        return manifest_digest(committed)

    def coordination_operation_manifest_digest(self, manifest, repo_root=None):
        return canonical_json_digest(
            coordination_identity_snapshot(
                self.coordination_manifest_snapshot(manifest, repo_root)
            )
        )

    def coordination_state_managed_manifest_digest(self, manifest, managed, integration_ref):
        """Digest the control manifest committed at the ref tip the state itself records."""
        repo_root = self.repository.root
        tip = managed.get(integration_ref)
        if not tip:
            return manifest_digest(manifest)
        committed = self.storage.manifest_from_tree(repo_root, tip, self.storage.integration_manifest_path(repo_root))
        return manifest_digest(committed) if committed is not None else manifest_digest(manifest)

    def build_coordination_state(
        self, manifest, config, previous, observed_refs, changed_refs, scope,
        projection_status, installation, tombstone=None, claim_commits=None,
        operation_token=None, refresh_manifest_digest=False,
    ):
        repo_root = self.repository.root
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
        snapshot = self.coordination_manifest_snapshot(
            manifest,
            repo_root,
            previous_state,
        )
        integration_ref = f"refs/heads/{manifest['integration']['branch']}"
        if (
            integration_ref in changed_refs
            or previous_state is None
            or previous_state.get('manifest_digest') is None
            or refresh_manifest_digest
        ):
            published_manifest_digest = self.coordination_state_managed_manifest_digest(
                manifest, managed, integration_ref
            )
        else:
            published_manifest_digest = previous_state['manifest_digest']
        return {
            'schema_version': coordination_snapshot_schema(snapshot),
            'coordination_id': config['id'],
            'publication_id': str(uuid.uuid4()),
            'parent_state': previous.get('tip') if previous else None,
            'created_at': iso_utc_now(),
            'syncwheel_version': self.repository.version,
            'installation_id': installation,
            'manifest': snapshot,
            'manifest_digest': published_manifest_digest,
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

    def create_coordination_state_commit(self, state, parent_tip=None):
        repo_root = self.repository.root
        encoded = json.dumps(state, indent=2, sort_keys=True) + '\n'
        blob = self.repository.run(['git', 'hash-object', '-w', '--stdin'], cwd=repo_root, input_text=encoded).stdout.strip()
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
                self.repository.git(repo_root, 'read-tree', f'{parent_tip}^{{tree}}', env=environment)
            else:
                self.repository.git(repo_root, 'read-tree', '--empty', env=environment)
            self.repository.git(
                repo_root,
                'update-index',
                '--add',
                '--cacheinfo',
                f'100644,{blob},{COORDINATION_STATE_FILE}',
                env=environment,
            )
            tree = self.repository.git(repo_root, 'write-tree', env=environment).stdout.strip()
            # Coordination state is public transport. Never inherit a maintainer's
            # Git identity into these append-only remote commits.
            command = ['git', *COORDINATION_GIT_IDENTITY_CONFIG, 'commit-tree', tree]
            if parent_tip:
                command.extend(['-p', parent_tip])
            command.extend(['-m', f"syncwheel coordination: {state['publication_scope']}"])
            return self.repository.run(command, cwd=repo_root).stdout.strip()
        finally:
            try:
                os.unlink(index_path)
            except OSError:
                pass

    def create_coordination_claim_commit(
        self,
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
        repo_root = self.repository.root
        claim = {
            'coordination_id': normalize_coordination_id(coordination_id),
            'source_ref': source_ref,
            'operation_token': operation_token,
            'claimed_at': iso_utc_now(),
            'syncwheel_version': self.repository.version,
            'publication_scope': publication_scope or '',
            'changed_refs': sorted(changed_refs),
        }
        if closed:
            claim['closed'] = True
            claim['reason'] = reason or 'closed'
        encoded = json.dumps(claim, indent=2, sort_keys=True) + '\n'
        blob = self.repository.run(
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
                self.repository.git(repo_root, 'read-tree', f'{parent_tip}^{{tree}}', env=environment)
            else:
                self.repository.git(repo_root, 'read-tree', '--empty', env=environment)
            self.repository.git(
                repo_root, 'update-index', '--add', '--cacheinfo',
                f'100644,{blob},{COORDINATION_CLAIM_FILE}', env=environment,
            )
            tree = self.repository.git(repo_root, 'write-tree', env=environment).stdout.strip()
            command = ['git', *COORDINATION_GIT_IDENTITY_CONFIG, 'commit-tree', tree]
            if parent_tip:
                command.extend(['-p', parent_tip])
            command.extend(['-m', f'syncwheel claim: {source_ref}'])
            return self.repository.run(command, cwd=repo_root).stdout.strip()
        finally:
            try:
                os.unlink(index_path)
            except OSError:
                pass

    def atomic_push_capability_probe(self, remote):
        repo_root = self.repository.root
        probe_tip = self.repository.ref_tip(repo_root, 'HEAD')
        if not probe_tip:
            probe_tip = self.repository.git(repo_root, 'rev-list', '--all', '-n', '1', check=False).stdout.strip()
        if not probe_tip:
            raise SyncwheelError('cannot probe atomic push capability without a local HEAD commit')
        probe_ref = f"refs/heads/syncwheel-probe/{uuid.uuid4().hex}"
        result = self.repository.git(
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

    def acquire_local_coordination_lease(self, config, installation):
        repo_root = self.repository.root
        profile, coordination = self.coordination_profile()
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
        self.storage.save_repo_profile(repo_root, profile)
        return token

    def release_local_coordination_lease(self, token):
        repo_root = self.repository.root
        profile, coordination = self.coordination_profile()
        lease = coordination.get('lease') or {}
        if lease.get('token') == token:
            coordination.pop('lease', None)
            profile['coordination'] = coordination
            self.storage.save_repo_profile(repo_root, profile)

    def record_pending_coordination_merge(self, config, expected, latest, manifest):
        repo_root = self.repository.root
        profile, coordination = self.coordination_profile()
        coordination['pending_merge'] = {
            'coordination_id': config['id'],
            'base_state': expected.get('tip'),
            'remote_state': latest.get('tip'),
            'local_manifest_digest': manifest_digest(manifest),
            'created_at': iso_utc_now(),
        }
        profile['coordination'] = coordination
        self.storage.save_repo_profile(repo_root, profile)

    def clear_pending_coordination_merge(self, config):
        repo_root = self.repository.root
        profile, coordination = self.coordination_profile()
        pending = coordination.get('pending_merge')
        if isinstance(pending, dict) and pending.get('coordination_id') == config['id']:
            coordination.pop('pending_merge', None)
            profile['coordination'] = coordination
            self.storage.save_repo_profile(repo_root, profile)

    def record_coordination_state_seen(self, config, state_tip):
        repo_root = self.repository.root
        profile, coordination = self.coordination_profile()
        coordination['last_seen_state'] = {
            'coordination_id': config['id'],
            'state_tip': state_tip,
            'seen_at': iso_utc_now(),
        }
        profile['coordination'] = coordination
        self.storage.save_repo_profile(repo_root, profile)

    def coordination_branch_worktrees(self, branch):
        repo_root = self.repository.root
        return [
            Path(item['path'])
            for item in self.repository.get_worktrees(repo_root)
            if item.get('branch') == branch
        ]

    def align_equivalent_coordination_refs(self, config, state, changed_refs):
        """Align safe, tree-equivalent local refs after another device won the lease."""
        repo_root = self.repository.root
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
            local_tip = self.repository.ref_tip(repo_root, branch)
            if local_tip == target_tip:
                continue
            if not local_tip:
                raise SyncwheelError(f'cannot align missing local managed branch: {branch}')
            fetched = self.repository.git(repo_root, 'fetch', '--quiet', config['remote'], ref, check=False)
            if fetched.returncode != 0 or self.repository.ref_tip(repo_root, 'FETCH_HEAD') != target_tip:
                raise SyncwheelError(f'cannot fetch the expected remote coordination tip for {branch}')
            if self.repository.ref_tree(repo_root, branch) != self.repository.ref_tree(repo_root, target_tip):
                raise SyncwheelError(
                    f'equivalent coordination state has a different tree for {branch}; manual review is required'
                )
            worktrees = self.coordination_branch_worktrees(branch)
            for worktree in worktrees:
                status = self.repository.local_worktree_status(worktree)
                if status is None or status:
                    raise SyncwheelError(
                        f'cannot align equivalent coordination state because {worktree} is dirty'
                    )
            backup = self.replay.backup_branch_command(branch, syncwheel_timestamp())
            if backup:
                self.repository.run(backup, cwd=repo_root)
            if worktrees:
                for worktree in worktrees:
                    self.repository.run(['git', '-C', str(worktree), 'reset', '--hard', target_tip])
            else:
                self.repository.git(repo_root, 'update-ref', ref, target_tip, local_tip)
            aligned.append({'branch': branch, 'from': local_tip, 'to': target_tip})
        return aligned

    def classify_coordination_race(self, manifest, config, expected, changed_refs, projection_status):
        repo_root = self.repository.root
        latest = self.states.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version']
        )
        desired_snapshot = self.coordination_manifest_snapshot(manifest, repo_root)
        if latest['state']:
            state = latest['state']
            if (
                state.get('manifest') == desired_snapshot
                and state.get('manifest_digest') == self.coordination_manifest_digest(
                    manifest, repo_root
                )
                and state.get('projection_status') == projection_status
                and self.coordination_state_matches_remote(config, state)
            ):
                return {'status': 'equivalent', 'latest': latest}
        base = expected.get('state', {}).get('manifest') if expected.get('state') else None
        if base and latest['state']:
            merged = merge_coordination_snapshots(base, desired_snapshot, latest['state']['manifest'])
            if merged['status'] == 'mergeable':
                return {'status': 'mergeable', 'latest': latest, **merged}
            return {'status': 'conflict', 'latest': latest, **merged}
        return {'status': 'conflict', 'latest': latest, 'reason': 'state_changed_without_a_merge_base'}

    def fetch_coordination_ref_tip(self, config, ref, expected_tip):
        repo_root = self.repository.root
        fetched = self.repository.git(repo_root, 'fetch', '--quiet', config['remote'], ref, check=False)
        if fetched.returncode != 0 or self.repository.ref_tip(repo_root, 'FETCH_HEAD') != expected_tip:
            raise SyncwheelError(f'cannot fetch the expected remote coordination tip for {ref}')
        return expected_tip

    def coordination_ref_is_safe_successor(self, config, ref, remote_tip, candidate_tip):
        repo_root = self.repository.root
        candidate_tip = self.repository.ref_tip(repo_root, candidate_tip)
        if candidate_tip == remote_tip:
            return True
        self.fetch_coordination_ref_tip(config, ref, remote_tip)
        if self.repository.ref_tree(repo_root, candidate_tip) == self.repository.ref_tree(repo_root, remote_tip):
            return True
        return self.repository.git(repo_root, 'merge-base', '--is-ancestor', remote_tip, candidate_tip, check=False).returncode == 0

    def coordination_stack_ref_is_exact_rebase(
        self,
        manifest,
        remote_snapshot,
        stack_id,
        remote_tip,
        candidate_tip,
    ):
        """Accept only a replay-proven rebase of an already published stack ref."""
        repo_root = self.repository.root
        previous_manifest = self._apply_snapshot(manifest, remote_snapshot)
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
        if any(not self.repository.commit_exists(repo_root, commit) for commit in previous_commits):
            return False
        if self.repository.commit_full_sha(repo_root, previous_commits[-1]) != remote_tip:
            return False
        candidate_tip = self.repository.ref_tip(repo_root, candidate_tip)
        if not candidate_tip or self.repository.commit_full_sha(repo_root, local_commits[-1]) != candidate_tip:
            return False
        expected_tip = self.replay.deterministic_stack_replay_tip(
            local_stack['base'],
            previous_commits,
        )
        return expected_tip == candidate_tip

    def integration_partial_stack_adoption_allowed(
        self, remote_integration, local_integration, added, changed_stack_refs, tombstone=None
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

    def validate_coordination_changed_ref_successors(
        self,
        manifest,
        config,
        state,
        remote_snapshot,
        local_snapshot,
        changed_refs,
    ):
        """Validate every changed ref before topology-equivalent success paths."""
        repo_root = self.repository.root
        remote_stacks = stack_snapshot_map(remote_snapshot)
        local_stacks = stack_snapshot_map(local_snapshot)
        for stack_id in sorted(set(remote_stacks) & set(local_stacks)):
            remote_stack = remote_stacks[stack_id]
            local_stack = local_stacks[stack_id]
            if remote_stack['branch'] != local_stack['branch']:
                continue
            ref = f"refs/heads/{local_stack['branch']}"
            if ref not in changed_refs:
                continue
            candidate_tip = changed_refs[ref]
            remote_tip = state.get('managed_refs', {}).get(ref)
            safe_successor = not remote_tip or self.coordination_ref_is_safe_successor(
                config,
                ref,
                remote_tip,
                candidate_tip,
            )
            exact_rebase = (
                remote_tip
                and not safe_successor
                and self.coordination_stack_ref_is_exact_rebase(
                    manifest,
                    remote_snapshot,
                    stack_id,
                    remote_tip,
                    candidate_tip,
                )
            )
            if not safe_successor and not exact_rebase:
                raise SyncwheelError(
                    f'{stack_id}: local branch is not a safe successor of the published managed ref; '
                    'run handoff and resolve the overlapping stack change'
                )

        integration_ref = f"refs/heads/{manifest['integration']['branch']}"
        remote_tip = state.get('managed_refs', {}).get(integration_ref)
        if (
            integration_ref in changed_refs
            and remote_tip
            and not self.coordination_ref_is_safe_successor(
                config,
                integration_ref,
                remote_tip,
                changed_refs[integration_ref],
            )
        ):
            raise SyncwheelError(
                'local integration branch is not a safe successor of the published integration ref; '
                'run handoff and resolve the overlap'
            )

    def validate_coordination_publication_base(
        self,
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
        ref_validation_scope=None,
    ):
        """Fail closed when a stale manifest would erase or overwrite published state."""
        repo_root = self.repository.root
        state = expected.get('state') if expected else None
        if not state:
            return None
        if not self.coordination_state_matches_ref_scope(config, state, ref_validation_scope):
            raise SyncwheelError(
                'published coordination state no longer matches its managed remote refs; run handoff and resolve manually'
            )
        digest_form = self.states.verify_coordination_state_manifest_digest(
            state, config['remote']
        )
        remote_snapshot = state['manifest']
        local_snapshot = self.coordination_manifest_snapshot(manifest, repo_root)
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
        # Report an erased declaration before ancestry drift; every admissible
        # publication still passes the unchanged successor guard before any return.
        self.validate_coordination_changed_ref_successors(
            manifest,
            config,
            state,
            remote_snapshot,
            local_snapshot,
            changed_refs,
        )
        if remote_snapshot == local_snapshot:
            return digest_form

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
                    f'; to publish only {remedy_stack} and keep the other local stacks unpublished, run:\n  '
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
                    owner = format_stack_owner(self.stack_owner_summary(
                        manifest_path, config, stack_id,
                        state_tip=expected.get('tip'),
                    ))
                    advice = (
                        f'drop the local change with syncwheel stack set {stack_id} --published'
                        if self.stack_reset_is_safe(remote_stack, local_stack)
                        else 'the owner must publish or reset it; run syncwheel handoff for details'
                    )
                    raise SyncwheelError(
                        f'{stack_id}: local manifest differs from published state without '
                        f'publishing its managed branch ({owner}); {advice}'
                    )
                continue

        integration_ref = f"refs/heads/{manifest['integration']['branch']}"
        remote_integration = remote_snapshot.get('integration')
        local_integration = local_snapshot.get('integration')
        if remote_integration != local_integration:
            partial_stack_adoption = self.integration_partial_stack_adoption_allowed(
                remote_integration, local_integration, added, changed_stack_refs, tombstone
            )
            if partial_stack_adoption:
                return
            if not tombstone and integration_ref not in changed_refs:
                raise SyncwheelError(
                    'local integration configuration differs from published state without publishing integration'
                )
            return digest_form
        return digest_form

    def coordination_publication_lock_path(self):
        repo_root = self.repository.root
        return self.repository.git_common_dir(repo_root) / 'syncwheel' / 'coordination-publication.lock'

    @contextlib.contextmanager
    def coordination_publication_lock(self, *, stale_lock_remedy=None):
        """Serialize the whole intent, push, local remainder and terminal-event cycle.

        Held across a network push, so a contender refuses instead of waiting it out.
        Re-entrant inside one process: the outermost holder owns the release.
        """
        repo_root = self.repository.root
        lock_path = self.coordination_publication_lock_path()
        key = str(lock_path)
        holders = coordination_publication_lock_holders()
        holder = holders.get(key)
        if holder is not None:
            holder['depth'] += 1
            try:
                yield holder['metadata']
            finally:
                holder['depth'] -= 1
            return
        with self.storage.exclusive_metadata_lock(
            lock_path,
            label='coordination publication',
            recovery_log_path=lock_path.with_name('coordination-publication-lock-recovery.jsonl'),
            timeout_seconds=COORDINATION_PUBLICATION_LOCK_TIMEOUT_SECONDS,
            stale_seconds=COORDINATION_PUBLICATION_LOCK_STALE_SECONDS,
            busy_error=coordination_publication_busy_error,
            stale_lock_remedy=stale_lock_remedy,
        ) as (_held_path, metadata):
            holders[key] = {'depth': 1, 'metadata': metadata}
            try:
                yield metadata
            finally:
                holders.pop(key, None)

    def coordination_publication_owner(self):
        """Name the process that recorded an intent; the lock, not this, is the authority."""
        repo_root = self.repository.root
        holder = coordination_publication_lock_holders().get(
            str(self.coordination_publication_lock_path())
        )
        metadata = (holder or {}).get('metadata') or {}
        pid = metadata.get('pid') or os.getpid()
        return {
            'installation_id': self.repository.installation_id(create=True),
            'host': socket.gethostname(),
            'pid': pid,
            'process_start_time': (
                metadata.get('process_start_time')
                or self.repository.governed_worktree_process_start_time(pid)
            ),
            'lock_token': metadata.get('token'),
            'recorded_at': iso_utc_now(),
        }

    def coordinated_publication_matches_remote(
        self,
        config,
        state,
        changed_refs,
        touched_refs,
        scope,
        projection_status,
        manifest_digest_value,
        operation_token,
        ref_validation_scope=None,
    ):
        repo_root = self.repository.root
        operation = {
            'operation_token': operation_token,
            'scope': scope,
            'projection_status': projection_status,
            'manifest_digest': manifest_digest_value,
            'changed_refs': changed_refs,
        }
        if not coordination_state_document_matches_operation(state, operation):
            return False
        if not self.coordination_state_matches_ref_scope(config, state, ref_validation_scope):
            return False
        claim_refs = {source_ref: coordination_claim_ref(source_ref) for source_ref in touched_refs}
        observed = self.states.remote_ref_tips(config['remote'], claim_refs.values())
        for source_ref, claim_ref in claim_refs.items():
            claim_tip = observed[claim_ref]
            if not claim_tip or state.get('claims', {}).get(source_ref) != claim_tip:
                return False
            claim = self.intents.fetch_coordination_claim(
                config['remote'], claim_ref, claim_tip
            )
            if (
                claim.get('coordination_id') != config['id']
                or claim.get('source_ref') != source_ref
                or claim.get('operation_token') != operation_token
            ):
                return False
        return True

    def coordinated_publish_cycle(
        self,
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
        adopt_remote_tips=None,
        ref_validation_scope=None,
    ):
        repo_root = self.repository.root
        config = coordination_config(manifest)
        if not config or config.get('mode') != 'active-active':
            raise SyncwheelError('coordinated publish requires an active-active manifest version 2 coordination block')
        if config['remote'] != manifest['defaults']['publication_remote']:
            raise SyncwheelError('coordination.remote must match defaults.publication_remote')
        recovery_state = {}
        if not dry_run:
            source_manifest, _ = self.storage.load_manifest(repo_root, manifest_path)
            if source_manifest is not None:
                source_digest_before = manifest_digest(source_manifest)
                recovered_source = self.storage.recover_incomplete_control_manifest_persistence(
                    repo_root,
                    manifest_path,
                    source_manifest,
                    recovery_state=recovery_state,
                    intents_only=True,
                )
                if recovery_state.get('recovered'):
                    if manifest_digest(manifest) != source_digest_before:
                        raise SyncwheelError(
                            'coordinated publish candidate was invalidated by control-manifest '
                            'recovery; rebuild the publication plan'
                        )
                    manifest = recovered_source
        changed_refs = dict(changed_refs)
        stack_scope = _publication_policy.stack_publication_source_refs(manifest, scope) is not None
        publication_manifest = publication_manifest or manifest
        integration_ref = f"refs/heads/{manifest['integration']['branch']}"
        if recovery_state.get('recovered') and not stack_scope:
            changed_refs[integration_ref] = self.repository.ref_tip(
                repo_root, manifest['integration']['branch']
            )
        integration_tip = self.repository.ref_tip(repo_root, manifest['integration']['branch'])
        # A13: the trigger consults the published state, not only the local tree.
        # A clone whose integration branch is merely stale (never synced, not
        # diverged) must not re-bootstrap over a control manifest another clone
        # already published; that expands claim_refs/observation_refs after this
        # plan was reviewed and trips the "managed refs changed" guard below.
        expected = self.states.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version']
        )
        integration_ref_published = (
            (expected.get('state') or {}).get('managed_refs') or {}
        ).get(integration_ref) is not None
        if (
            not dry_run
            and not stack_scope
            and integration_tip
            and not integration_ref_published
            and not self.projections.integration_control_matches_selected(repo_root, manifest, integration_tip)
        ):
            self.storage.restore_control_manifest_after_integration_rebuild(
                repo_root,
                manifest_path,
                manifest,
                integration_tip,
                'control',
                reason='bootstrap coordinated control manifest',
                command='syncwheel coordinated publish',
                persist_source=False,
            )
            changed_refs[integration_ref] = self.repository.ref_tip(
                repo_root, manifest['integration']['branch']
            )
        selective.require_compose_ref_validation_scope(
            ref_validation_scope, publication_manifest,
            self.coordination_manifest_snapshot(publication_manifest, repo_root),
            expected.get('state'), scope)
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
        desired_manifest_digest = self.coordination_operation_manifest_digest(
            publication_manifest, repo_root
        )
        if (
            operation_token
            and expected.get('state')
            and self.coordinated_publication_matches_remote(
                config,
                expected['state'],
                changed_refs,
                touched_refs,
                scope,
                projection_status,
                desired_manifest_digest,
                operation_token,
                ref_validation_scope=ref_validation_scope,
            )
        ):
            report_advisory_unclaimed_owned_refs(config, expected['state'])
            print('coordinated publish: recovered the exact published operation intent')
            return {
                'status': 'recovered',
                'state_tip': expected['tip'],
                'recovered': True,
            }
        if operation_token and self.intents.coordinated_operation_landed(
            config,
            expected,
            {
                'operation_token': operation_token,
                'scope': scope,
                'projection_status': projection_status,
                'manifest_digest': desired_manifest_digest,
                'changed_refs': changed_refs,
                'tombstone': tombstone,
                'expected_coordination_state_tip': (
                    None
                    if expected_coordination_state_tip is EXPECTED_COORDINATION_STATE_UNSET
                    else expected_coordination_state_tip
                ),
            },
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
        _publication_policy.require_stack_publication_refs(manifest, scope, changed_refs)
        if (
            expected_coordination_state_tip is not EXPECTED_COORDINATION_STATE_UNSET
            and expected['tip'] != expected_coordination_state_tip
        ):
            raise SyncwheelError(
                'coordinated publish STOP: remote state changed after the reviewed plan'
                + coordinated_publish_retry_hint(scope)
            )
        self.states.require_exclusive_coordination_ownership(
            config,
            selective.required_ref_values(dict.fromkeys(managed), ref_validation_scope),
            expected_state_refs=expected_coordination_state_refs,
        )
        claim_refs = {ref: coordination_claim_ref(ref) for ref in touched_refs}
        observation_refs = list(dict.fromkeys([*managed, *claim_refs.values()]))
        full_observation = self.states.remote_ref_tips(config['remote'], observation_refs)
        if (full_observation.get(integration_ref) is None and integration_ref not in changed_refs
                and (ref_validation_scope is None or integration_ref not in ref_validation_scope['preservedRefs'])):
            if stack_scope:
                raise SyncwheelError(
                    'stack publication requires an existing published integration ref; '
                    'bootstrap it explicitly with syncwheel int push'
                )
            if not integration_tip:
                raise SyncwheelError(
                    'cannot bootstrap coordination state without a local integration tip'
                )
            if self.storage.manifest_from_tree(
                repo_root, integration_tip, self.storage.integration_manifest_path(repo_root)
            ) is None:
                raise SyncwheelError(
                    'cannot bootstrap coordination state before the integration tip '
                    'contains .syncwheel/manifest.json'
                )
            # A state cannot bind a control-manifest digest to an absent tip.  The
            # first coordinated publication therefore carries the already-local
            # integration tip in the same atomic push, and the ref it carries owes
            # a claim like every other touched source ref.
            changed_refs[integration_ref] = integration_tip
            touched_refs = list(dict.fromkeys([*touched_refs, integration_ref]))
            claim_refs[integration_ref] = coordination_claim_ref(integration_ref)
            observation_refs = list(dict.fromkeys([*managed, *claim_refs.values()]))
            full_observation = self.states.remote_ref_tips(config['remote'], observation_refs)
        observed_refs = {ref: full_observation.get(ref) for ref in managed}
        if stack_scope and expected.get('state') is None:
            try:
                self.states.coordination_state_control_manifest(
                    {'manifest': self.coordination_manifest_snapshot(manifest, repo_root),
                     'managed_refs': observed_refs},
                    config['remote'],
                )
            except SyncwheelError as exc:
                raise SyncwheelError(
                    'stack publication requires published integration control metadata; '
                    'bootstrap it explicitly with syncwheel int push'
                ) from exc
        observed_claims = {
            source_ref: full_observation.get(claim_ref)
            for source_ref, claim_ref in claim_refs.items()
        }
        if expected_observed_refs is not None:
            planned_observations = {
                ref: expected_observed_refs.get(ref) for ref in observation_refs
            }
            if selective.required_ref_values(full_observation, ref_validation_scope) != selective.required_ref_values(planned_observations, ref_validation_scope):
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
        self.repository.require_new_managed_refs_absent(
            config, expected.get('state'), changed_refs, observed_refs,
            repo_root, adopt_remote_tips,
            integration_ref=integration_ref,
        )
        for source_ref, claim_tip in observed_claims.items():
            if not claim_tip:
                continue
            claim = self.intents.fetch_coordination_claim(
                config['remote'], claim_refs[source_ref], claim_tip
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
        base_digest_form = self.validate_coordination_publication_base(
            publication_manifest,
            config,
            expected,
            changed_refs,
            tombstone=tombstone,
            rename=rename,
            state_transition=state_transition,
            remedy_stack=remedy_stack,
            creation_remedy=creation_remedy,
            manifest_path=manifest_path,
            ref_validation_scope=ref_validation_scope,
        )
        observed_refs = selective.required_ref_values(observed_refs, ref_validation_scope)
        for ref, sha in changed_refs.items():
            if not sha:
                raise SyncwheelError(f'cannot publish an empty managed ref: {ref}')
        if integration_ref in changed_refs:
            self.projections.require_selected_integration_control(repo_root, manifest, changed_refs[integration_ref])
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
        installation = self.repository.installation_id(create=True)
        claim_commits = {}
        for source_ref, claim_ref in claim_refs.items():
            claim_commits[source_ref] = self.create_coordination_claim_commit(
                source_ref,
                config['id'],
                operation_token,
                observed_claims[source_ref],
                closed=bool(tombstone and source_ref == coordination_tombstone_ref(tombstone)),
                reason=(tombstone or {}).get('reason'),
                publication_scope=scope,
                changed_refs=changed_refs,
            )
        state = self.build_coordination_state(
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
            refresh_manifest_digest=bool(
                base_digest_form
                and base_digest_form['form']
                == COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT
            ),
        )
        self._validate_state(state, config['id'], config.get('claims', 'advisory'))
        state_commit = self.create_coordination_state_commit(state, expected['tip'])
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
        _publication_policy.require_stack_publication_refs(manifest, scope, changed_refs)
        refspecs = [f'{changed_refs[ref]}:{ref}' for ref in sorted(changed_refs)]
        refspecs.extend(
            f'{claim_commits[source_ref]}:{claim_refs[source_ref]}'
            for source_ref in sorted(claim_refs)
        )
        refspecs.append(f'{state_commit}:{state_ref}')
        token = self.acquire_local_coordination_lease(config, installation)
        try:
            if not preflight_complete:
                self.atomic_push_capability_probe(config['remote'])
            command = ['git', 'push', '--atomic', *lease_args, config['remote'], *refspecs]
            result = self.repository.run_authorized_push(
                repo_root, command, config['remote'],
                [*changed_refs, *claim_refs.values(), state_ref], check=False,
                manifest=manifest,
            )
            if result.returncode != 0:
                latest_claims = self.states.remote_ref_tips(
                    config['remote'], claim_refs.values()
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
                leased_now = self.states.remote_ref_tips(
                    config['remote'], list(expected_tips)
                )
                if leased_now == expected_tips:
                    raise CoordinationRemoteRejection(
                        'the coordination remote refused the atomic publication: '
                        + (
                            result.stderr.strip()
                            or result.stdout.strip()
                            or 'no detail returned'
                        )
                    )
                race = self.classify_coordination_race(
                    manifest,
                    config,
                    expected,
                    changed_refs,
                    projection_status,
                )
                if race['status'] == 'equivalent':
                    aligned = self.align_equivalent_coordination_refs(
                        config,
                        race['latest']['state'],
                        changed_refs,
                    )
                    self.clear_pending_coordination_merge(config)
                    self.record_coordination_state_seen(config, race['latest']['tip'])
                    if aligned:
                        self.ledger.append_ledger_event(
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
                    self.record_pending_coordination_merge(config, expected, race['latest'], manifest)
                    raise SyncwheelError(
                        'coordinated publish stopped after a lease loss; disjoint stack changes are mergeable. '
                        'Review handoff, then rerun the full lifecycle with publish --accept-merge.'
                    )
                detail = result.stderr.strip() or result.stdout.strip() or 'no detail returned'
                raise SyncwheelError(
                    f"coordinated publish stopped after a lease loss or remote rejection ({race.get('reason', 'conflict')}): {detail}"
                )
            self.clear_pending_coordination_merge(config)
            self.record_coordination_state_seen(config, state_commit)
            report_advisory_unclaimed_owned_refs(config, state)
            print(quoted(command))
            published = {'status': 'published', 'state_tip': state_commit}
            if base_digest_form:
                published['base_state_digest_form'] = base_digest_form['form']
            if (
                base_digest_form
                and base_digest_form['form']
                == COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT
            ):
                self.record_coordination_state_digest_migration(
                    manifest_path,
                    config,
                    base_digest_form,
                    expected['tip'],
                    state_commit,
                    state['manifest_digest'],
                )
                published['migrated_manifest_digest'] = state['manifest_digest']
                print(
                    'coordinated publish: migrated the legacy coordination state '
                    'manifest_digest to the control manifest form'
                )
            return published
        finally:
            self.release_local_coordination_lease(token)

    def local_manifest_projection_is_convergent(self, manifest, manifest_path=None):
        repo_root = self.repository.root
        try:
            for stack in manifest['stacks']:
                if not self.repository.branch_exists(repo_root, stack['branch']):
                    return False
                if self.projections.stack_reconcile_report(repo_root, manifest, stack).get('local_matches_projection') is not True:
                    return False
            integration = manifest['integration']
            if not self.repository.branch_exists(repo_root, integration['branch']):
                return False
            report = self.projections.integration_sync_report(repo_root, manifest)
            if (report.get('local_matches_product_projection') is not True
                    or report.get('local_control_manifest_matches_selected') is not True):
                return False
            for channel in manifest.get('channels', []):
                tip = self.repository.ref_tip(repo_root, channel['branch'])
                applied = self.ledger.latest_channel_event(
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
        self, manifest, manifest_path, *, persist=True, expected_digest=None
    ):
        repo_root = self.repository.root
        config = coordination_config(manifest)
        if not config or config.get('mode') != 'active-active':
            raise SyncwheelError('--accept-merge requires an active-active coordination manifest')
        profile, coordination = self.coordination_profile()
        pending = coordination.get('pending_merge')
        if not isinstance(pending, dict) or pending.get('coordination_id') != config['id']:
            raise SyncwheelError('there is no pending mergeable coordinated publication for this manifest')
        if pending.get('local_manifest_digest') != manifest_digest(manifest):
            raise SyncwheelError('the local manifest changed after the mergeable conflict; run handoff and resolve again')
        latest = self.states.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version']
        )
        if not latest['state'] or latest['tip'] != pending.get('remote_state'):
            raise SyncwheelError('remote coordination state changed after the mergeable conflict; run handoff and retry')
        base_tip = pending.get('base_state')
        if not base_tip:
            raise SyncwheelError('the mergeable conflict has no shared coordination-state ancestor')
        base = self.states.coordination_state_from_commit(base_tip, config['id'])
        merged = merge_coordination_snapshots(
            base['manifest'],
            self.coordination_manifest_snapshot(manifest, repo_root),
            latest['state']['manifest'],
        )
        if merged['status'] != 'mergeable':
            raise SyncwheelError(
                'the previous mergeable coordination conflict is no longer safe to merge: '
                + merged.get('reason', 'unknown')
            )
        updated = self._apply_snapshot(manifest, merged['merged'])
        if expected_digest is not None and manifest_digest(updated) != expected_digest:
            raise SyncwheelError(
                'the accepted coordination merge changed after reconciliation planning; '
                'run handoff and retry'
            )
        if persist:
            self.storage.save_manifest_with_ledger(
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

    def require_sha1_repository(self, operation):
        repo_root = self.repository.root
        object_format = self.repository.git(repo_root, 'rev-parse', '--show-object-format').stdout.strip()
        if object_format != 'sha1':
            raise SyncwheelError(f'{operation} supports only SHA-1 repositories; found {object_format}')

    def coordination_state_matches_ref_scope(self, config, state, scope):
        if scope is None:
            return self.coordination_state_matches_remote(config, state)
        expected = selective.required_ref_values(state.get('managed_refs') or {}, scope)
        observed = self.states.remote_ref_tips(config['remote'], expected)
        return observed == expected

    def coordination_state_matches_remote(self, config, state):
        repo_root = self.repository.root
        expected = state.get('managed_refs') or {}
        observed = self.states.remote_ref_tips(config['remote'], expected)
        return all(observed.get(ref) == tip for ref, tip in expected.items())

    def coordination_profile(self):
        repo_root = self.repository.root
        profile = self.storage.load_repo_profile(repo_root)
        coordination = profile.get('coordination')
        if coordination is None:
            coordination = {}
        if not isinstance(coordination, dict):
            raise SyncwheelError('syncwheel profile coordination state must be an object')
        profile['coordination'] = coordination
        return profile, coordination

    def published_stack_owner(self, stack_id, state_tip):
        repo_root = self.repository.root
        scopes = {
            f'{kind}:{stack_id}' for kind in ('create', 'stack', 'promote', 'demote', 'close')
        }
        commit = state_tip
        for _ in range(STACK_OWNER_STATE_SCAN_LIMIT):
            document = self.intents.coordination_state_document_from_commit(commit) if commit else None
            if document is None:
                return None
            stack = stack_snapshot_map(document.get('manifest') or {}).get(stack_id)
            ref = f"refs/heads/{stack['branch']}" if stack else None
            changed = document.get('changed_refs') or {}
            if document.get('publication_scope') in scopes or ref in changed:
                commits = (stack or {}).get('commits') or []
                last = commits[-1] if commits else None
                author = (
                    self.repository.git(repo_root, 'show', '-s', '--format=%an <%ae>', last, check=False).stdout.strip()
                    if last and self.repository.commit_exists(repo_root, last) else ''
                )
                return {
                    'state': commit,
                    'scope': document.get('publication_scope'),
                    'created_at': document.get('created_at'),
                    'installation_id': document.get('installation_id'),
                    'author': author or None,
                }
            commit = document.get('parent_state')
        return None

    def stack_owner_summary(self, manifest_path, config, stack_id, state_tip=None):
        """Last local ledger event about a stack, plus the newest published state touching it."""
        repo_root = self.repository.root
        summary = {'stack': stack_id, 'local': None, 'published': None}
        try:
            events = self.ledger.load_ledger_events(repo_root, manifest_path)
        except SyncwheelError:
            events = []
        for event in reversed(events):
            payload = event.get('payload') or {}
            context = payload.get('context') or {}
            if event.get('type') == 'manifest_saved' and context.get('stack') == stack_id:
                summary['local'] = {
                    'event': payload.get('reason'), 'ts': event.get('ts'), 'actor': context.get('actor'),
                }
                break
            if (
                event.get('type') in {'stack_pushed', 'stack_promoted', 'stack_close_intent'}
                and payload.get('stack') == stack_id
            ):
                summary['local'] = {
                    'event': event['type'], 'ts': event.get('ts'), 'actor': payload.get('actor'),
                }
                break
        if config and config.get('mode') == 'active-active':
            try:
                if state_tip is None:
                    state_tip = self.states.read_remote_coordination_state(config, fetch=True)['tip']
                summary['published'] = self.published_stack_owner(stack_id, state_tip)
            except SyncwheelError:
                pass
        return summary

    def stack_reset_is_safe(self, published_stack, local_stack):
        repo_root = self.repository.root
        return changed_snapshot_fields(published_stack, local_stack) == ['commits'] and all(
            self.repository.commit_exists(repo_root, commit) for commit in published_stack.get('commits') or []
        )

    def coordination_state_chain_contains(self, coordination_id, base_tip, latest_tip):
        """Prove both Git ancestry and every declared append-only state link."""
        repo_root = self.repository.root
        if self.repository.git(repo_root, 'merge-base', '--is-ancestor', base_tip, latest_tip, check=False).returncode != 0:
            raise SyncwheelError('coordination compose known base state is not an ancestor of remote state')
        current = latest_tip
        while current != base_tip:
            state = self.states.coordination_state_from_commit(current, coordination_id)
            declared_parent = state.get('parent_state')
            git_parent_result = self.repository.git(repo_root, 'rev-parse', f'{current}^', check=False)
            git_parent = git_parent_result.stdout.strip() if git_parent_result.returncode == 0 else None
            if not declared_parent or declared_parent != git_parent:
                raise SyncwheelError('coordination compose state chain is not append-only')
            current = declared_parent
        self.states.coordination_state_from_commit(base_tip, coordination_id)
        return True

    def record_coordination_state_digest_migration(
        self, manifest_path, config, digest_form, parent_state, state_tip, digest,
    ):
        repo_root = self.repository.root
        return self.ledger.append_ledger_event(
            repo_root,
            'coordination_state_digest_migrated',
            {
                'coordination_id': config['id'],
                'parent_state': parent_state,
                'state_tip': state_tip,
                'from_form': digest_form['form'],
                'from_digest': digest_form['recorded_digest'],
                'to_form': COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST,
                'to_digest': digest,
            },
            manifest_path,
        )


    def coordinated_publish(self, *args, **kwargs):
        """Run one publication cycle under this clone's single-writer lock.

        A dry run inspects and prints only, so it never takes the lock and is never
        refused by a live publication.
        """
        repo_root = self.repository.root
        dry_run = kwargs.get('dry_run') if 'dry_run' in kwargs else (
            len(args) > 5 and bool(args[5])
        )
        if dry_run:
            return self.coordinated_publish_cycle(*args, **kwargs)
        with self.coordination_publication_lock():
            return self.coordinated_publish_cycle(*args, **kwargs)
