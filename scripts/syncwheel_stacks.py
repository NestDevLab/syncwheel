"""Stack lifecycle policy and command orchestration.

Application wrappers select the repository and manifest once. Recovery, durable
close/create/promote transitions and source publication live here, composing the
coordination, replay and governed-worktree services through named interfaces.
"""

import contextlib
import copy
import hashlib
import json
import re
from pathlib import Path
import sys
import tempfile
import uuid
from typing import Protocol

from syncwheel_contracts import (
    ZERO_OBJECT_ID,
    iso_utc_now,
)
from syncwheel_coordination import (
    changed_snapshot_fields,
    coordination_claim_ref,
    coordination_integration_resolutions,
    coordination_tombstone_ref,
    stack_snapshot_map,
)
from syncwheel_errors import (
    MetadataRollbackIncomplete,
    SyncwheelError,
)
from syncwheel_ledger import (
    manifest_event_payload,
)
from syncwheel_manifest import (
    INTEGRATION_MEMBERSHIP_REQUIRED,
    MANIFEST_VERSION_CHANNELS,
    canonical_json_digest,
    coordination_config,
    coordination_is_active,
    coordination_state_ref,
    effective_worktree_root,
    manifest_digest,
    require_stack,
    resolve_worktree_root_path,
    safe_ref_segment,
    stack_integration_commits,
    stack_integration_only_commits,
    stack_map,
    validate_stack_dependency_graph,
    validate_stack_adoption,
)
from syncwheel_publication import (
    PublicationCoordinator,
    format_stack_owner,
    local_lease_is_active,
    managed_ref_names,
)
from syncwheel_publication_intents import (
    PublicationIntents,
    EXPECTED_COORDINATION_STATE_UNSET,
    VERIFIED_STACK_PUBLICATION_RECOVERY,
)
from syncwheel_replay import (
    ReplayEngine,
    backup_branch_name,
    quoted,
    replay_target,
    require_nonempty_desk_stack_rebuild,
    require_replay_success,
    syncwheel_timestamp,
)

from syncwheel_coordination_state import CoordinationStateReader
from syncwheel_worktrees import GovernedWorktrees
from syncwheel_stack_adoption import StackAdoption


def require_stack_absorb_amendable(stack, args):
    if args.amend and not stack['commits']:
        raise SyncwheelError(
            f"stack {stack['id']!r} has no commits to amend; "
            'use -N/--no-amend with -m MESSAGE to create its first commit'
        )


def ordinary_empty_draft_close(manifest, stack, args, reason):
    return bool(
        stack and stack.get('state') == 'draft' and not coordination_is_active(manifest)
        and not stack.get('commits') and not stack_integration_commits(stack)
        and not stack.get('integration_resolution') and not stack.get('adopted_history')
        and not stack.get('integration_resolved')
        and not args.delete_branch and not args.force and reason in (None, 'closed')
    )


class StackRepository(Protocol):
    root: Path
    def branch_contains(self, *args, **kwargs): ...
    def branch_exists(self, *args, **kwargs): ...
    def commit_exists(self, *args, **kwargs): ...
    def commit_first_parent(self, *args, **kwargs): ...
    def commit_full_sha(self, *args, **kwargs): ...
    def commit_list_for_spec(self, *args, **kwargs): ...
    def commit_short_sha(self, *args, **kwargs): ...
    def fetch_observed_delivery_tip(self, *args, **kwargs): ...
    def find_worktree_for_branch(self, *args, **kwargs): ...
    def get_current_branch(self, *args, **kwargs): ...
    def get_worktrees(self, *args, **kwargs): ...
    def git(self, *args, **kwargs): ...
    def capture_ref_transaction(self, operations): ...
    def governed_worktree_owner(self, *args, **kwargs): ...
    def is_valid_coordination_branch_ref(self, *args, **kwargs): ...
    def ref_tip(self, *args, **kwargs): ...
    def ref_tree(self, *args, **kwargs): ...
    def resolve_git_worktree(self, *args, **kwargs): ...
    def rev_list(self, *args, **kwargs): ...
    def run(self, *args, **kwargs): ...
    def run_authorized_push(self, *args, **kwargs): ...
    def legacy_stack_push_lease(self, *args, **kwargs): ...
    def forced_push_requested(self, *args, **kwargs): ...
    def without_force_push_args(self, *args, **kwargs): ...
    def with_git_identity(self, *args, **kwargs): ...


class StackControl(Protocol):
    def apply_coordination_snapshot(self, *args, **kwargs): ...
    def ensure_syncwheel_metadata_excluded(self, *args, **kwargs): ...
    def ensure_syncwheel_worktree_root_excluded(self, *args, **kwargs): ...
    def is_external_manifest_path(self, *args, **kwargs): ...
    def manifest_write_transaction(self, *args, **kwargs): ...
    def recover_incomplete_control_manifest_persistence(self, *args, **kwargs): ...
    def require_locked_manifest_observation(self, *args, **kwargs): ...
    def require_manifest(self, *args, **kwargs): ...
    def require_manifest_transaction_current(self, *args, **kwargs): ...
    def restore_control_manifest_after_integration_rebuild(self, *args, **kwargs): ...
    def prepare_capture_metadata(self, *args, **kwargs): ...
    def save_manifest(self, *args, **kwargs): ...
    def save_manifest_with_ledger(self, *args, **kwargs): ...


class StackLedger(Protocol):
    def append_ledger_event(self, *args, **kwargs): ...
    def load_ledger_events(self, *args, **kwargs): ...


class StackProjections(Protocol):
    def capture_integration_desk(self, *args, **kwargs): ...
    def checkout_reset_destination_lease(self, *args, **kwargs): ...
    def register_created_desk(self, *args, **kwargs): ...
    def archive_desk_remedy(self, *args, **kwargs): ...
    def ensure_in_place_target(self, *args, **kwargs): ...
    def ensure_non_in_place_target_clean(self, *args, **kwargs): ...
    def reconcile_worktree_path(self, *args, **kwargs): ...
    def resolve_stack_rebuild_location(self, *args, **kwargs): ...
    def stack_content_absorbed_at(self, *args, **kwargs): ...
    def stack_reconcile_report(self, *args, **kwargs): ...
    def verify_retained_captured_worktrees(self, *args, **kwargs): ...


class StackContinuation(Protocol):
    def command_stack_close(self, *args, **kwargs): ...


def stack_closed_payload(intent, coordination_state=None, recovered=False):
    return {
        'stack': intent['stack'],
        'branch': intent['branch'],
        'reason': intent['reason'],
        'operation_token': intent['operation_token'],
        'coordination_state': coordination_state,
        'delivery_tip': intent.get('delivery_tip'),
        **({'archive_proof': intent['archive_proof']} if intent.get('archive_proof') else {}),
        'recovered': recovered,
    }

def manifest_scope_options(args):
    options = []
    for flag, value in (
        ('--repo', args.repo), ('--manifest', args.manifest), ('--personal', args.personal),
    ):
        if value:
            options.extend([flag, value])
    return options

def stack_close_command(args):
    proof = getattr(args, '_archive_proof', None)
    if proof:
        return quoted(['syncwheel', 'stack', 'archive', args.stack, '--reason', proof['reason'],
                       '--resume-intent', proof['resumeIntent'], '--plan-digest', proof['planDigest'],
                       '--apply', *manifest_scope_options(args)])
    command = ['syncwheel', 'stack', 'close', args.stack]
    if args.reason:
        command.extend(['--reason', args.reason])
    if args.force:
        command.append('--force')
    return quoted([*command, *manifest_scope_options(args)])

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

def coordination_draft_recovery_ref(stack_id):
    return (
        f'refs/syncwheel/recovery/drafts/{safe_ref_segment(stack_id)}-'
        f'{syncwheel_timestamp()}'
    )

def draft_branch_creation_checkpoint():
    """Fault-injection seam immediately before atomic draft ref creation."""

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

def stack_push_remote(manifest, stack, remote_override=None):
    return remote_override or stack.get('publication_remote') or manifest['defaults']['publication_remote']

def channel_ids_referencing_stack(manifest, stack_id):
    return sorted(
        channel['id'] for channel in manifest.get('channels', [])
        if any(
            entry['stack'] == stack_id
            for entry in channel.get('composition', [])
        )
    )

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

def passthrough_args(values):
    return values or []

def push_args_with_options(args):
    push_args = passthrough_args(args.git_args)
    if getattr(args, 'force_with_lease', False) and '--force-with-lease' not in push_args:
        push_args = ['--force-with-lease', *push_args]
    return push_args


class StackLifecycle:
    def __init__(self, repository: StackRepository, control: StackControl,
                 ledger: StackLedger, projections: StackProjections,
                 continuation: StackContinuation, *, state: CoordinationStateReader,
                 intents: PublicationIntents, publication: PublicationCoordinator,
                 replay: ReplayEngine, worktrees: GovernedWorktrees):
        self.repository = repository
        self.control = control
        self.ledger = ledger
        self.projections = projections
        self.continuation = continuation
        self.state = state
        self.intents = intents
        self.publication = publication
        self.replay = replay
        self.worktrees = worktrees


    def pending_stack_close_operation(self, manifest_path, stack_id):
        repo_root = self.repository.root
        events = self.ledger.load_ledger_events(repo_root, manifest_path)
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


    def remote_first_close_failure(self, manifest, stack_id, exc, kind='remote-first', retry=None):
        repo_root = self.repository.root
        config = coordination_config(manifest)
        if config and self.intents.coordination_remote_is_reachable(config['remote']):
            return SyncwheelError(str(exc))
        return SyncwheelError(
            f'{stack_id}: {kind} close could not inspect the coordination remote: {exc}\n'
            'Restore remote access, then retry:\n  '
            + (retry or f'syncwheel stack close {stack_id} --force')
        )


    def abandon_superseded_stack_close(self, manifest_path, pending):
        repo_root = self.repository.root
        self.ledger.append_ledger_event(
            repo_root,
            'stack_close_abandoned',
            {
                'stack': pending['stack'],
                'branch': pending.get('branch'),
                'operation_token': pending['operation_token'],
                'reason': 'close_superseded',
                'status': 'close_superseded',
                'close_reason': pending.get('reason'),
            },
            manifest_path,
        )


    def abandoned_close_reason(self, manifest_path, stack_id):
        """The reason of an abandoned close this stack never replaced with a new one."""
        repo_root = self.repository.root
        reason = None
        for event in self.ledger.load_ledger_events(repo_root, manifest_path):
            payload = event.get('payload') or {}
            if payload.get('stack') != stack_id:
                continue
            if event.get('type') == 'stack_close_abandoned':
                reason = payload.get('close_reason')
            elif event.get('type') in {'stack_close_intent', 'stack_closed'}:
                reason = None
        return reason


    def supersede_unpublished_stack_close(self, manifest_path, pending):
        """Retire an obsolete close intent in a repository without coordination."""
        repo_root = self.repository.root
        self.abandon_superseded_stack_close(manifest_path, pending)


    def stack_recreated_after_close_intent(self, manifest_path, pending):
        """Whether the stack id was declared again after this close intent was journaled."""
        repo_root = self.repository.root
        journaled = False
        for event in self.ledger.load_ledger_events(repo_root, manifest_path):
            payload = event.get('payload') or {}
            if (
                event.get('type') == 'stack_close_intent'
                and payload.get('operation_token') == pending['operation_token']
            ):
                journaled = True
            elif (
                journaled and event.get('type') == 'manifest_saved'
                and payload.get('reason') == 'stack_create'
                and (payload.get('context') or {}).get('stack') == pending['stack']
            ):
                return True
        return False


    def published_close_reached_remote(self, config, observed, pending):
        """Whether a close token is in the state history or its tombstone claim history."""
        repo_root = self.repository.root
        token = pending['operation_token']
        recorded_tip = pending.get('expected_coordination_state_tip')
        if recorded_tip and token in {
            self.intents.coordination_commit_operation_token(commit)
            for commit in self.intents.coordination_state_commits_since(observed['tip'], recorded_tip)
        }:
            return True
        closed_ref = pending['closed_ref']
        claim_ref = coordination_claim_ref(closed_ref)
        claim_tip = self.state.remote_ref_tips(config['remote'], [claim_ref])[claim_ref]
        return self.intents.coordination_claim_history_carries_token(
            config, claim_tip, closed_ref, token
        )


    def abandon_unlanded_stack_close(self, config, manifest_path, pending, observed):
        """Retire a close token that never landed; the retry writes a fresh intent.

        Refuses when the stack's published entry changed since the recorded state, so
        an old close never acts on a stack generation it did not see.
        """
        repo_root = self.repository.root
        stack_id = pending['stack']
        recorded_tip = pending.get('expected_coordination_state_tip')
        try:
            unchanged = recorded_tip is None or (
                stack_snapshot_map(self.state.coordination_state_from_commit(
                    recorded_tip, config['id'], config.get('claims', 'advisory'),
                ).get('manifest') or {}).get(stack_id)
                == stack_snapshot_map((observed.get('state') or {}).get('manifest') or {}).get(stack_id)
            )
        except SyncwheelError:
            unchanged = False
        self.abandon_superseded_stack_close(manifest_path, pending)
        if not unchanged:
            raise SyncwheelError(
                f'{stack_id}: close_superseded; its published entry changed after the '
                'interrupted close, so that close was abandoned and this retry changed '
                'neither the manifest nor the remote; review syncwheel handoff, then '
                'run the close again'
            )


    def scoped_close_publication_manifest(self, manifest, snapshot, stack_id):
        """The published snapshot without one stack; local proposals stay local."""
        publication = self.control.apply_coordination_snapshot(manifest, snapshot)
        publication['stacks'] = [
            item for item in publication['stacks'] if item['id'] != stack_id
        ]
        publication['integration']['stacks'] = [
            item for item in publication['integration'].get('stacks', [])
            if item != stack_id
        ]
        return publication


    def published_close_tombstone(self, manifest, pending):
        repo_root = self.repository.root
        config = coordination_config(manifest)
        if not config or not pending.get('remote_first'):
            return None
        closed_ref = pending.get('closed_ref')
        if not closed_ref:
            return None
        claim_ref = coordination_claim_ref(closed_ref)
        published = self.state.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version']
        )
        claim_tip = self.state.remote_ref_tips(config['remote'], [claim_ref])[claim_ref]
        state = published.get('state') or {}
        if not claim_tip or state.get('claims', {}).get(closed_ref) != claim_tip:
            return None
        claim = self.intents.fetch_coordination_claim(
            config['remote'], claim_ref, claim_tip
        )
        if (
            claim.get('coordination_id') != config['id']
            or claim.get('source_ref') != closed_ref
            or claim.get('operation_token') != pending.get('operation_token')
            or claim.get('closed') is not True
        ):
            return None
        return {'state_tip': published['tip'], 'claim_tip': claim_tip}


    def recover_pending_stack_close(self, manifest, manifest_path, pending):
        repo_root = self.repository.root
        state_tip = pending.get('coordination_state')
        if manifest_digest(manifest) != pending.get('manifest_digest_after'):
            # the manifest moved on after the close saved it: only a landed close,
            # proved on the remote, can still be terminalized
            if pending.get('remote_first') or not coordination_is_active(manifest):
                return False
            config = coordination_config(manifest)
            observed = self.state.read_remote_coordination_state(
                config, fetch=True, local_manifest_version=manifest['version']
            )
            if not self.published_close_reached_remote(config, observed, pending):
                return False
            state_tip = observed['tip']
        else:
            published = self.published_close_tombstone(manifest, pending)
            if pending.get('remote_first') and not published:
                return False
            state_tip = (published or {}).get('state_tip') or state_tip
        self.ledger.append_ledger_event(
            repo_root,
            'stack_closed',
            stack_closed_payload(pending, coordination_state=state_tip, recovered=True),
            manifest_path,
        )
        return True


    def stack_archive_writer_guard(self, manifest, stack, manifest_path=None):
        """An authoring lane or checked-out source must be released before archival."""
        repo_root = self.repository.root
        registry, _ = self.worktrees.load_governed_worktree_registry()
        if any(lane.get('state') != 'reaped' and (
            lane.get('target') == stack['id'] or lane.get('branch') == stack['branch']
        ) for lane in registry['lanes']):
            raise SyncwheelError('stack archive refuses an active writer lane; capture and release it first')
        worktree = self.repository.find_worktree_for_branch(repo_root, stack['branch'])
        remedy = self.projections.archive_desk_remedy(repo_root, manifest_path, stack, worktree)
        if remedy:
            raise SyncwheelError(f'stack archive refuses a checked-out source branch or pending desk release; {remedy}')
        _, coordination = self.publication.coordination_profile()
        if stack['id'] in (coordination.get('locks') or {}) or local_lease_is_active(coordination):
            raise SyncwheelError('stack archive refuses an active worktree lock or coordination lease')


    def build_stack_archive_plan(self, manifest, stack_id, reason, resume_intent, manifest_path=None):
        repo_root = self.repository.root
        stack = require_stack(manifest, stack_id)
        self.stack_archive_writer_guard(manifest, stack, manifest_path)
        if not reason or not reason.strip() or not resume_intent or not resume_intent.strip():
            raise SyncwheelError('stack archive requires a nonempty --reason and --resume-intent')
        if channel_ids_referencing_stack(manifest, stack_id) or any(
            stack_id in item.get('depends_on', []) for item in manifest['stacks']
        ):
            raise SyncwheelError('stack archive refuses a stack used by an active channel or dependent stack')
        source_ref = f"refs/heads/{stack['branch']}"
        source_tip = self.repository.ref_tip(repo_root, source_ref)
        if not source_tip:
            raise SyncwheelError('stack archive requires the source branch; recover it before planning')
        # Full object IDs, including resolved/only-in-integration commits, are the
        # proof endpoints. They need not be ancestors of the delivery branch.
        commits = list(dict.fromkeys(self.repository.commit_full_sha(repo_root, commit) for commit in [
            source_tip, *stack.get('commits', []), *stack_integration_commits(stack),
        ]))
        remote_refs = {}
        state_tip = None
        published_generation = None
        config = coordination_config(manifest)
        if coordination_is_active(manifest):
            state_ref = coordination_state_ref(config)
            refs = list(dict.fromkeys([
                *managed_ref_names(manifest), coordination_claim_ref(source_ref), state_ref,
            ]))
            remote_refs = self.state.remote_ref_tips(config['remote'], refs)
            state_tip = remote_refs[state_ref]
            if not state_tip or not remote_refs.get(f"refs/heads/{manifest['integration']['branch']}"):
                raise SyncwheelError('stack archive requires initialized published coordination; run handoff first')
            # A preview never fetches. If a peer object is missing, handoff/fetch is
            # an explicit prerequisite rather than a hidden write in a plan.
            state = self.state.coordination_state_from_commit(state_tip, config['id'], config.get('claims', 'advisory'))
            published_stack = stack_snapshot_map(state['manifest']).get(stack_id)
            local_stack = stack_snapshot_map(self.publication.coordination_manifest_snapshot(manifest, repo_root)).get(stack_id)
            if published_stack is not None and published_stack != local_stack:
                raise SyncwheelError('stack archive generation differs from published state; run handoff first')
            published_resolution = coordination_integration_resolutions(state['manifest']).get(stack_id, {})
            published_generation = {'stack': published_stack, 'resolution': published_resolution}
            for commit in [*published_resolution.get('integration_commits', []),
                           *published_resolution.get('integration_only_commits', [])]:
                sha = self.repository.commit_full_sha(repo_root, commit)
                if sha not in commits:
                    commits.append(sha)
            claim_tip = remote_refs.get(coordination_claim_ref(source_ref))
            if claim_tip:
                claim = self.intents.coordination_claim_from_commit(claim_tip)
                if claim.get('coordination_id') != config['id'] or claim.get('source_ref') != source_ref or claim.get('closed'):
                    raise SyncwheelError('stack archive source claim is foreign or closed')
            remote_source = remote_refs.get(source_ref)
            if state.get('managed_refs', {}).get(source_ref) != remote_source or (
                claim_tip and state.get('claims', {}).get(source_ref) != claim_tip
            ):
                raise SyncwheelError('stack archive source or claim differs from coordination state; run handoff first')
            if remote_source:
                commits = list(dict.fromkeys([*commits, self.repository.commit_full_sha(repo_root, remote_source)]))
        plan = {
            'schemaVersion': 1, 'kind': 'stackArchivePlan', 'stack': stack_id,
            'stackGeneration': copy.deepcopy(stack), 'manifestDigest': manifest_digest(manifest),
            'sourceRef': source_ref, 'sourceTip': source_tip, 'commits': commits,
            'declaredEndpoints': {spec: self.repository.commit_full_sha(repo_root, spec) for spec in [
                *stack.get('commits', []), *stack_integration_commits(stack),
            ]},
            'reason': reason, 'resumeIntent': resume_intent, 'retentionDays': 30,
            'coordinationStateTip': state_tip, 'remoteRefs': remote_refs,
            'publishedGeneration': published_generation,
            'remote': config['remote'] if coordination_is_active(manifest) else None,
        }
        digest = canonical_json_digest(plan)
        # Timestamp identifies the retained source generation; no wall-clock input
        # means repeated read-only previews have the same digest and endpoints.
        stamp = self.repository.git(repo_root, 'show', '-s', '--format=%ct', source_tip).stdout.strip()
        prefix = f"refs/archive/{stamp}/{safe_ref_segment(stack_id)}/{digest}"
        plan['archiveRefs'] = {f'{prefix}/source-tip': source_tip}
        declared_sources = {self.repository.commit_full_sha(repo_root, item) for item in stack.get('commits', [])}
        for sha in commits:
            if sha != source_tip:
                label = 'declared-commit' if sha in declared_sources else 'integration-resolved'
                plan['archiveRefs'][f'{prefix}/{label}/{sha}'] = sha
        plan['planDigest'] = canonical_json_digest(plan)
        return plan


    def verify_stack_archive_proof(self, proof, stack=None):
        repo_root = self.repository.root
        if stack is not None:
            if stack != proof['stackGeneration'] or self.repository.ref_tip(repo_root, proof['sourceRef']) != proof['sourceTip']:
                raise SyncwheelError('stack archive source generation changed; generate a new plan')
        for spec, sha in proof['declaredEndpoints'].items():
            if self.repository.commit_full_sha(repo_root, spec) != sha:
                raise SyncwheelError('stack archive declared endpoint changed; generate a new plan')
        for ref, sha in proof['archiveRefs'].items():
            if self.repository.ref_tip(repo_root, ref) != sha:
                raise SyncwheelError(f'stack archive retention proof is missing or changed: {ref}')
        # Each endpoint has a direct ref, a stronger reachability proof than an
        # ancestry walk. Keep this linear even for large historical stacks.
        retained = set(proof['archiveRefs'].values())
        for sha in proof['commits']:
            if sha not in retained:
                raise SyncwheelError(f'stack archive has no retained reachability proof for {sha}')


    def command_stack_archive(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        if args.apply and not args.plan_digest:
            raise SyncwheelError('--plan-digest is required with --apply')
        prepared = next((event['payload'] for event in reversed(self.ledger.load_ledger_events(repo_root, manifest_path))
                         if event.get('type') == 'stack_archive_prepared'
                         and event.get('payload', {}).get('planDigest') == args.plan_digest), None) if args.apply else None
        if prepared:
            if prepared['stack'] != args.stack or prepared['reason'] != args.reason or prepared['resumeIntent'] != args.resume_intent:
                raise SyncwheelError('stack archive retry does not match the prepared request')
            plan = prepared
            self.verify_stack_archive_proof(plan)
            terminal = next((event['payload'] for event in reversed(self.ledger.load_ledger_events(repo_root, manifest_path))
                             if event.get('type') == 'stack_closed'
                             and event.get('payload', {}).get('archive_proof', {}).get('planDigest') == args.plan_digest), None)
            if terminal:
                print(json.dumps(terminal, indent=2))
                return 0
            stack = stack_map(manifest).get(args.stack)
            if stack is not None:
                self.stack_archive_writer_guard(manifest, stack, manifest_path)
            self.verify_stack_archive_proof(plan, stack)
        else:
            plan = self.build_stack_archive_plan(manifest, args.stack, args.reason, args.resume_intent, manifest_path)
            if not args.apply:
                print(json.dumps(plan, indent=2))
                return 0
            if args.plan_digest != plan['planDigest']:
                raise SyncwheelError('stack archive plan is stale; generate a new preview')
            # One atomic local ref transaction: a partial failure leaves the source
            # and declaration untouched, and never overwrites a different archive.
            commands = ['start', f"verify {plan['sourceRef']} {plan['sourceTip']}"]
            for ref, sha in plan['archiveRefs'].items():
                existing = self.repository.ref_tip(repo_root, ref)
                commands.append(f'verify {ref} {sha}' if existing == sha else f'create {ref} {sha}')
            commands.extend(['prepare', 'commit'])
            self.repository.run(['git', 'update-ref', '--stdin'], cwd=repo_root, input_text='\n'.join(commands) + '\n')
            self.verify_stack_archive_proof(plan, stack_map(manifest)[args.stack])
            plan['retainedAt'] = iso_utc_now()
            self.ledger.append_ledger_event(repo_root, 'stack_archive_prepared', plan, manifest_path)
        pending = self.pending_stack_close_operation(manifest_path, args.stack)
        if pending and pending.get('archive_proof', {}).get('planDigest') != plan['planDigest']:
            previous = pending.get('archive_proof')
            if not previous or any(previous[key] != plan[key] for key in (
                'stackGeneration', 'sourceTip', 'reason', 'resumeIntent',
            )):
                raise SyncwheelError('stack archive refuses to replace another pending close request')
            if coordination_is_active(manifest):
                config = coordination_config(manifest)
                observed = self.state.read_remote_coordination_state(config, fetch=True,
                                                          local_manifest_version=manifest['version'])
                if self.published_close_reached_remote(config, observed, pending):
                    raise SyncwheelError('the earlier archive close landed; retry its original plan digest first')
                self.abandon_unlanded_stack_close(config, manifest_path, pending, observed)
            else:
                self.supersede_unpublished_stack_close(manifest_path, pending)
        close_args = copy.copy(args)
        close_args.reason = 'archived'
        close_args.force = False
        close_args.delete_branch = False
        close_args._archive_proof = plan
        return self.continuation.command_stack_close(close_args)


    def command_stack_close(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        self.complete_pending_promote_intents(
            manifest,
            manifest_path,
            skip_stacks={args.stack},
            only_stacks={args.stack},
        )
        original_manifest = copy.deepcopy(manifest)
        pending_close = self.pending_stack_close_operation(manifest_path, args.stack)
        stack = stack_map(manifest).get(args.stack)
        if stack is None:
            if pending_close:
                try:
                    if self.recover_pending_stack_close(
                        manifest, manifest_path, pending_close
                    ):
                        print(f'{args.stack}: recovered interrupted stack close')
                        return 0
                except SyncwheelError as exc:
                    raise self.remote_first_close_failure(
                        manifest, args.stack, exc, retry=stack_close_command(args),
                    ) from exc
                raise SyncwheelError(
                    f'{args.stack}: interrupted stack close found, but the manifest no longer '
                    'matches its intended result; inspect the stack_close_intent ledger event '
                    'and reconcile the manifest before retrying'
                )
            raise SyncwheelError(f'unknown stack: {args.stack}')
        branch = stack['branch']
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

        recovered_reason = (
            None if args.reason or pending_close
            else self.abandoned_close_reason(manifest_path, args.stack)
        )
        reason = (
            args.reason
            or (pending_close.get('reason') if pending_close else recovered_reason)
            or 'closed'
        )
        archive_proof = getattr(args, '_archive_proof', None)
        if reason == 'archived' and not archive_proof:
            raise SyncwheelError('use stack archive with a reviewed plan; archived is not a delivery close reason')
        if archive_proof:
            self.verify_stack_archive_proof(archive_proof, stack)
        landed_close = None
        published_observation = None
        if pending_close and coordination_is_active(manifest) and not pending_close.get('remote_first'):
            config = coordination_config(manifest)
            try:
                published_observation = self.state.read_remote_coordination_state(
                    config, fetch=True, local_manifest_version=manifest['version'],
                )
            except SyncwheelError as exc:
                raise self.remote_first_close_failure(
                    manifest, args.stack, exc,
                    kind='published', retry=stack_close_command(args),
                ) from exc
            if self.published_close_reached_remote(
                config, published_observation, pending_close
            ):
                published_entry = stack_snapshot_map(
                    (published_observation.get('state') or {}).get('manifest') or {}
                ).get(args.stack)
                local_entry = stack_snapshot_map(
                    self.publication.coordination_manifest_snapshot(manifest, repo_root)
                ).get(args.stack)
                if (
                    published_entry is not None and published_entry == local_entry
                ) or self.stack_recreated_after_close_intent(manifest_path, pending_close):
                    # this stack id was declared again after the close landed: that
                    # close is done, and this one is a new operation
                    self.ledger.append_ledger_event(
                        repo_root,
                        'stack_closed',
                        stack_closed_payload(
                            pending_close,
                            coordination_state=published_observation['tip'],
                            recovered=True,
                        ),
                        manifest_path,
                    )
                    pending_close = None
                    reason = args.reason or 'closed'
                else:
                    landed_close = published_observation
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
                    self.published_close_tombstone(
                        recovered_manifest, pending_close
                    )
                    if manifest_digest(recovered_manifest)
                    == pending_close.get('manifest_digest_after')
                    else None
                )
            except SyncwheelError as exc:
                raise self.remote_first_close_failure(
                    manifest, args.stack, exc, retry=stack_close_command(args),
                ) from exc
            if published:
                self.control.require_manifest_transaction_current(manifest_path)
                self.control.save_manifest(manifest_path, recovered_manifest)
                self.ledger.append_ledger_event(
                    repo_root,
                    'stack_closed',
                    stack_closed_payload(
                        pending_close,
                        coordination_state=published['state_tip'],
                        recovered=True,
                    ),
                    manifest_path,
                )
                self.abandon_pending_stack_creates(
                    manifest_path, args.stack, 'stack_closed'
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
                pending_remote_state = self.state.read_remote_coordination_state(
                    config, fetch=True,
                    local_manifest_version=manifest['version'],
                )
                current_refs = self.state.remote_ref_tips(
                    config['remote'], generation_refs
                )
            except SyncwheelError as exc:
                raise self.remote_first_close_failure(
                    manifest, args.stack, exc, retry=stack_close_command(args),
                ) from exc
            if current_refs != generation_refs:
                self.abandon_superseded_stack_close(
                    manifest_path, pending_close
                )
                raise SyncwheelError(
                    f'{args.stack}: close_superseded; the pending close generation is '
                    'no longer current and was abandoned; this retry changed neither '
                    'the manifest nor the remote'
                )
        delivery_tip = None
        if reason == 'absorbed' and landed_close is None:
            delivery_base = f"{stack['target_remote']}/{stack['target_branch']}"
            delivery_tip = self.repository.fetch_observed_delivery_tip(
                repo_root, stack['target_remote'], stack['target_branch']
            )
            if not self.projections.stack_content_absorbed_at(repo_root, stack, delivery_tip):
                raise SyncwheelError(
                    f"{args.stack}: cannot close as absorbed: content is not reachable from delivery base "
                    f"{delivery_base} at {delivery_tip}; rebuilding integration projection "
                    f"{manifest['integration']['branch']} "
                    'would drop it. Deliver or preserve the stack first, then use a different close reason.'
                )

        # The stack's pinned base is historical; delivery ancestry belongs to the
        # current target ref, observed and fetched for this close attempt.
        empty_draft = ordinary_empty_draft_close(manifest, stack, args, reason)
        needs_delivery_ancestry = (
            landed_close is None and not archive_proof and not empty_draft
            and (reason == 'merged' or not args.force)
        )
        ancestry_ref = (
            delivery_tip if reason == 'absorbed' else
            self.repository.fetch_observed_delivery_tip(repo_root, stack['target_remote'], stack['target_branch'])
            if needs_delivery_ancestry else
            stack.get('base') or manifest['defaults']['base_ref']
        )
        ancestry_label = (
            f"{stack['target_remote']}/{stack['target_branch']} at {ancestry_ref}"
            if reason == 'absorbed' or needs_delivery_ancestry else ancestry_ref
        )
        unmerged = []
        for sha in (stack.get('commits') or []) if landed_close is None else []:
            result = self.repository.git(repo_root, 'merge-base', '--is-ancestor', sha, ancestry_ref, check=False)
            if result.returncode != 0:
                unmerged.append(sha)

        if reason == 'absorbed' or archive_proof:
            unmerged = []
        if unmerged and not args.force:
            short = [self.repository.commit_short_sha(repo_root, sha) for sha in unmerged[:5]]
            extra = f' (and {len(unmerged) - 5} more)' if len(unmerged) > 5 else ''
            raise SyncwheelError(
                f"{args.stack}: {len(unmerged)} commit(s) are NOT yet reachable from {ancestry_label}: "
                f"{', '.join(short)}{extra}\n"
                f"For a squash/rebase delivery, retry with --reason absorbed so Syncwheel fetches "
                f"and verifies the delivered content. Use --force only for a deliberate close "
                f"without ancestry or absorption proof."
            )

        merged_note = (
            '' if unmerged or landed_close is not None or archive_proof or empty_draft
            else f' (all commits confirmed in {ancestry_label})'
        )

        # Remove from stacks list.
        manifest['stacks'] = [s for s in manifest['stacks'] if s['id'] != args.stack]
        # Remove from integration stacks list.
        if args.stack in manifest['integration'].get('stacks', []):
            manifest['integration']['stacks'] = [
                s for s in manifest['integration']['stacks'] if s != args.stack
            ]

        if not empty_draft and pending_close is None and args.reason is None and recovered_reason is None:
            reason = 'merged' if not unmerged else 'closed'
        if pending_close and pending_close.get('remote_first') and reason != pending_close.get('reason'):
            raise SyncwheelError(
                f'{args.stack}: interrupted remote-first close is bound to reason '
                f'{pending_close.get("reason")!r}; recover that operation before changing reason'
            )
        if (pending_close and not pending_close.get('remote_first')
                and not coordination_is_active(manifest) and (
            reason != pending_close.get('reason')
            or pending_close.get('manifest_digest_after') != manifest_digest(manifest)
        )):
            self.supersede_unpublished_stack_close(manifest_path, pending_close)
            pending_close = None
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
                expected_state = pending_remote_state or self.state.read_remote_coordination_state(
                    config, fetch=True,
                    local_manifest_version=manifest['version'],
                )
                published_state = expected_state.get('state') or {}
                if published_state.get('manifest'):
                    publication_manifest = self.scoped_close_publication_manifest(
                        manifest, published_state['manifest'], args.stack
                    )
                else:
                    publication_manifest = copy.deepcopy(manifest)
                    publication_manifest['stacks'] = []
                    publication_manifest['integration']['stacks'] = []
                managed = list(dict.fromkeys([
                    *managed_ref_names(publication_manifest), closed_ref,
                ]))
                claim_ref = coordination_claim_ref(closed_ref)
                expected_observation = self.state.remote_ref_tips(
                    config['remote'], [*managed, claim_ref]
                )
                remote_tip = expected_observation[closed_ref]
                claim_tip = expected_observation[claim_ref]
                if claim_tip:
                    claim = self.intents.fetch_coordination_claim(
                        config['remote'], claim_ref, claim_tip
                    )
                    if claim['coordination_id'] != config['id']:
                        raise SyncwheelError(
                            f'{closed_ref} is claimed by coordination domain '
                            f'{claim["coordination_id"]}; refusing close'
                        )
            except SyncwheelError as exc:
                raise self.remote_first_close_failure(
                    manifest, args.stack, exc, retry=stack_close_command(args),
                ) from exc
        elif coordination_is_active(manifest):
            config = coordination_config(manifest)
            expected_state = published_observation
            if expected_state is None:
                try:
                    expected_state = self.state.read_remote_coordination_state(
                        config, fetch=True,
                        local_manifest_version=manifest['version'],
                    )
                except SyncwheelError as exc:
                    raise self.remote_first_close_failure(
                        manifest, args.stack, exc,
                        kind='published', retry=stack_close_command(args),
                    ) from exc
            if landed_close is not None:
                if reason != pending_close['reason']:
                    raise SyncwheelError(
                        f"{args.stack}: the interrupted close already reached the coordination "
                        f"remote with reason {pending_close['reason']!r}; retry with "
                        f"--reason {pending_close['reason']}"
                    )
                coordination_result = {
                    'status': 'already_published', 'state_tip': expected_state['tip'],
                }
            elif pending_close:
                self.abandon_unlanded_stack_close(
                    config, manifest_path, pending_close, expected_state
                )
                pending_close = None
            if coordination_result is None and expected_state.get('state'):
                publication_manifest = self.scoped_close_publication_manifest(
                    manifest, expected_state['state']['manifest'], args.stack
                )
        if publication_manifest is not manifest:
            published_users = sorted({
                *channel_ids_referencing_stack(publication_manifest, args.stack),
                *(
                    item['id'] for item in publication_manifest['stacks']
                    if args.stack in item.get('depends_on', [])
                ),
            })
            if published_users:
                raise SyncwheelError(
                    f'stack {args.stack} is still used by published channel(s) or stack(s): '
                    + ', '.join(published_users)
                    + '; run syncwheel handoff and update them before closing'
                )
        if archive_proof and coordination_result is None and config:
            if expected_state.get('tip') != archive_proof['coordinationStateTip']:
                raise SyncwheelError('stack archive coordination state changed after the reviewed plan')
            expected_observation = archive_proof['remoteRefs']
            if self.state.remote_ref_tips(config['remote'], expected_observation) != expected_observation:
                raise SyncwheelError('stack archive refs or claims changed after the reviewed plan')
        if archive_proof:
            self.verify_stack_archive_proof(archive_proof, stack)
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
                self.publication.coordination_manifest_snapshot(publication_manifest, repo_root)
            ),
            'delivery_tip': delivery_tip,
            **({'archive_proof': archive_proof} if archive_proof else {}),
            'actor': self.repository.governed_worktree_owner(),
        }
        if pending_close is None:
            self.ledger.append_ledger_event(
                repo_root, 'stack_close_intent', close_intent, manifest_path
            )
        else:
            close_intent = pending_close
        self.control.require_manifest_transaction_current(manifest_path)
        if coordination_is_active(manifest) and coordination_result is None:
            config = config or coordination_config(manifest)
            try:
                coordination_result = self.publication.coordinated_publish(
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
                        **({'archive': {'planDigest': archive_proof['planDigest'], 'retentionDays': 30}} if archive_proof else {}),
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
                retry = (
                    '' if 'Retry:\n' in str(exc)
                    else f' Retry:\n  {stack_close_command(args)}'
                )
                raise SyncwheelError(
                    f'{exc}\nRemote-first close did not save the manifest.{retry}'
                ) from exc
        self.control.save_manifest(manifest_path, manifest)
        close_intent['coordination_state'] = (
            coordination_result.get('state_tip') if coordination_result else None
        )
        self.ledger.append_ledger_event(
            repo_root,
            'stack_closed',
            stack_closed_payload(
                close_intent,
                coordination_state=close_intent.get('coordination_state'),
            ),
            manifest_path,
        )
        self.abandon_pending_stack_creates(
            manifest_path, args.stack, 'stack_closed'
        )

        print(f"{args.stack}: closed{merged_note}")
        print(f"  branch : {branch}")
        print(f"  reason : {reason}")
        print(f"  removed from integration: {manifest['integration']['branch']}")
        if coordination_result:
            print(f"  coordination state: {coordination_result['status']}")

        if args.delete_branch:
            if self.repository.branch_exists(repo_root, branch):
                self.repository.git(repo_root, 'branch', '-d', branch, check=False)
                print(f"  local branch deleted: {branch}")
            else:
                print(f"  local branch already absent: {branch}")
        else:
            print(f"  tip: run 'git branch -d {branch}' to delete the local branch when ready")

        return 0


    def stack_create_recorded(self, manifest_path, stack_id):
        repo_root = self.repository.root
        return any(
            event.get('type') == 'manifest_saved'
            and (event.get('payload') or {}).get('reason') == 'stack_create'
            and ((event.get('payload') or {}).get('context') or {}).get('stack') == stack_id
            for event in self.ledger.load_ledger_events(repo_root, manifest_path)
        )


    def unmatched_stack_create_operations(self, manifest_path, stack_id=None):
        """Return create intents not closed by completion or explicit abandonment."""
        repo_root = self.repository.root
        events = self.ledger.load_ledger_events(repo_root, manifest_path)
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


    def pending_stack_create_operation(self, manifest_path, stack_id, branch, tip):
        """Return the newest unmatched intent token for this exact create lifecycle."""
        repo_root = self.repository.root
        for payload in self.unmatched_stack_create_operations(
            manifest_path, stack_id
        ):
            if payload.get('branch') == branch and payload.get('tip') == tip:
                return payload.get('operation_token')
        return None


    def abandon_pending_stack_creates(self, manifest_path, stack_id, reason):
        repo_root = self.repository.root
        for payload in self.unmatched_stack_create_operations(manifest_path, stack_id):
            self.ledger.append_ledger_event(
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
        self, manifest_path, stack_id, branch, tip, operation_token
    ):
        repo_root = self.repository.root
        if not operation_token:
            return
        current = self.pending_stack_create_operation(
            manifest_path, stack_id, branch, tip
        )
        if current != operation_token:
            raise SyncwheelError(
                f'{stack_id}: refusing late completion for superseded create operation '
                f'{operation_token}'
            )


    def recover_equivalent_draft_create(
        self, manifest, manifest_path, stack, operation_token
    ):
        """Adopt a completed atomic draft publication after a local persistence failure."""
        repo_root = self.repository.root
        config = coordination_config(manifest)
        if not config or config.get('mode') != 'active-active':
            return None
        source_ref = f"refs/heads/{stack['branch']}"
        local_tip = self.repository.ref_tip(repo_root, stack['branch'])
        published = self.state.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version']
        )
        state = published.get('state') or {}
        claim_ref = coordination_claim_ref(source_ref)
        observed = self.state.remote_ref_tips(
            config['remote'], [source_ref, claim_ref]
        )
        claim_tip = observed[claim_ref]
        claim = (
            self.intents.fetch_coordination_claim(config['remote'], claim_ref, claim_tip)
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
            and state.get('manifest_digest') == self.publication.coordination_manifest_digest(manifest, repo_root)
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


    def preflight_active_draft_create(self, manifest, manifest_path, stack):
        """Validate ownership and composition before creating the local source branch."""
        repo_root = self.repository.root
        config = coordination_config(manifest)
        if not config or config.get('mode') != 'active-active':
            return None
        source_ref = f"refs/heads/{stack['branch']}"
        planned_tip = self.replay.deterministic_stack_replay_tip(stack['base'], stack['commits'])
        if not planned_tip:
            raise SyncwheelError(
                f"{stack['id']}: cannot deterministically materialize draft source before coordinated publication"
            )
        planned_tip = self.repository.commit_full_sha(repo_root, planned_tip)
        expected = self.state.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version']
        )
        managed = managed_ref_names(manifest)
        claim_ref = coordination_claim_ref(source_ref)
        observed = self.state.remote_ref_tips(
            config['remote'], [*managed, claim_ref]
        )
        claim_tip = observed[claim_ref]
        if claim_tip:
            claim = self.intents.fetch_coordination_claim(
                config['remote'], claim_ref, claim_tip
            )
            if claim['coordination_id'] != config['id']:
                raise SyncwheelError(
                    f'{source_ref} is claimed by coordination domain '
                    f'{claim["coordination_id"]}; refusing publication'
                )
        coordination_state_refs = self.state.require_exclusive_coordination_ownership(
            config, managed
        )
        if observed[source_ref] is not None and observed[source_ref] != planned_tip:
            raise SyncwheelError(
                f"{stack['id']}: unowned remote draft ref {source_ref} has a different tip; "
                'refusing to replace it'
            )
        self.publication.validate_coordination_publication_base(
            manifest,
            config,
            expected,
            {source_ref: planned_tip},
            remedy_stack=stack['id'],
            creation_remedy=True,
            manifest_path=manifest_path,
        )
        self.publication.atomic_push_capability_probe(config['remote'])
        return {
            'source_ref': source_ref,
            'planned_tip': planned_tip,
            'expected_coordination_state_tip': expected['tip'],
            'expected_observed_refs': observed,
            'expected_coordination_state_refs': coordination_state_refs,
        }


    def command_stack_create(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        if args.draft and coordination_is_active(manifest):
            create_remote = coordination_config(manifest)['remote']
            if not self.intents.coordination_remote_is_reachable(create_remote):
                raise SyncwheelError(
                    'coordinated publish could not inspect the coordination remote; '
                    'restore remote access, then retry:\n  '
                    f'{draft_create_retry_command(args)}'
                )
        self.complete_pending_promote_intents(manifest, manifest_path)
        stacks = stack_map(manifest)
        if args.stack in stacks:
            existing = stacks[args.stack]
            existing_tip = self.repository.ref_tip(repo_root, existing['branch'])
            pending_operation = self.pending_stack_create_operation(
                manifest_path,
                args.stack,
                existing['branch'],
                existing_tip,
            )
            if (
                args.draft
                and existing.get('state', 'published') == 'draft'
                and self.repository.branch_exists(repo_root, existing['branch'])
                and (pending_operation or not self.stack_create_recorded(manifest_path, args.stack))
            ):
                self.ledger.append_ledger_event(
                    repo_root,
                    'manifest_saved',
                    manifest_event_payload(
                        manifest_path, manifest, 'stack_create',
                        {
                            'stack': args.stack,
                            'branch': existing['branch'],
                            'operation_token': pending_operation,
                            'recovered': True,
                            'actor': self.repository.governed_worktree_owner(),
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
            commits.extend(self.repository.commit_list_for_spec(repo_root, spec))
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
            self.control.require_manifest_transaction_current(manifest_path)
            if self.repository.branch_exists(repo_root, branch):
                existing_tip = self.repository.ref_tip(repo_root, branch)
                operation_token = self.pending_stack_create_operation(
                    manifest_path, args.stack, branch, existing_tip
                )
                coordination_result = self.recover_equivalent_draft_create(
                    manifest, manifest_path, stack, operation_token
                )
                if coordination_result is not None:
                    if operation_token is None:
                        self.abandon_pending_stack_creates(
                            manifest_path, args.stack, 'superseded_by_recovery'
                        )
                        operation_token = str(uuid.uuid4())
                        self.ledger.append_ledger_event(
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
                    self.control.save_manifest_with_ledger(
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
                            'actor': self.repository.governed_worktree_owner(),
                        },
                    )
                    self.worktrees.capture_governed_worktrees_for_stack(manifest, args.stack)
                    print(f"{args.stack}: recovered equivalent published draft create")
                    return 0
            preflight = self.preflight_active_draft_create(manifest, manifest_path, stack)
            if preflight:
                operation_token = self.pending_stack_create_operation(
                    manifest_path, args.stack, branch, preflight['planned_tip']
                )
                if operation_token is None:
                    self.abandon_pending_stack_creates(
                        manifest_path, args.stack, 'superseded_by_new_create'
                    )
                    operation_token = str(uuid.uuid4())
                    self.ledger.append_ledger_event(
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
            self.cleanup_interrupted_draft_materialization(
                stack,
                preflight['planned_tip'] if preflight else None,
                operation_token,
            )
            if self.repository.branch_exists(repo_root, branch):
                if not preflight or self.repository.ref_tip(repo_root, branch) != preflight['planned_tip']:
                    raise SyncwheelError(f'draft stack branch already exists locally: {branch}')
            else:
                self.materialize_new_stack_branch(
                    stack,
                    planned_tip=(preflight['planned_tip'] if preflight else None),
                    operation_token=operation_token,
                )
                if preflight and self.repository.ref_tip(repo_root, branch) != preflight['planned_tip']:
                    raise SyncwheelError(f"{args.stack}: materialized draft tip differs from its reviewed projection")
            if coordination_is_active(manifest) and coordination_result is None:
                try:
                    coordination_result = self.publication.coordinated_publish(
                        manifest,
                        manifest_path,
                        {f"refs/heads/{branch}": self.repository.ref_tip(repo_root, branch)},
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
        self.control.save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'stack_create',
            {
                'stack': args.stack,
                'branch': branch,
                'operation_token': operation_token,
                'coordination_state': coordination_result.get('state_tip') if coordination_result else None,
                'actor': self.repository.governed_worktree_owner(),
            },
        )
        self.worktrees.capture_governed_worktrees_for_stack(
            manifest,
            args.stack,
            manifest_path=manifest_path,
        )
        print(f"{args.stack}: created {branch} with {len(stack['commits'])} commits (state={stack['state']})")
        if coordination_result:
            print(f"  coordination state: {coordination_result['status']}")
        return 0


    def draft_materialization_worktree(self, operation_token):
        repo_root = self.repository.root
        if not operation_token:
            return None
        token = hashlib.sha256(
            f'{Path(repo_root).resolve()}:{operation_token}'.encode('utf-8')
        ).hexdigest()[:24]
        return Path(tempfile.gettempdir()) / f'syncwheel-stack-create-{token}'


    def cleanup_interrupted_draft_materialization(
        self, stack, planned_tip, operation_token
    ):
        repo_root = self.repository.root
        path = self.draft_materialization_worktree(operation_token)
        if path is None:
            return
        registrations = [
            item for item in self.repository.get_worktrees(repo_root)
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
        removed = self.repository.git(
            repo_root, 'worktree', 'remove', '--force', str(path), check=False
        )
        if removed.returncode != 0:
            raise SyncwheelError(
                f"{stack['id']}: could not remove interrupted create worktree {path}"
            )


    def materialize_new_stack_branch(
        self, stack, planned_tip=None, operation_token=None
    ):
        """Create a draft ref with create-only CAS and leave no worktree on failure."""
        repo_root = self.repository.root
        planned_tip = planned_tip or self.replay.deterministic_stack_replay_tip(
            stack['base'], stack['commits']
        )
        if not planned_tip:
            raise SyncwheelError(
                f"{stack['id']}: cannot deterministically materialize draft source branch"
            )
        planned_tip = self.repository.commit_full_sha(repo_root, planned_tip)
        branch_ref = f"refs/heads/{stack['branch']}"
        draft_branch_creation_checkpoint()
        created = self.repository.git(
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
        deterministic_worktree = self.draft_materialization_worktree(
            operation_token
        )
        temporary = (
            None if deterministic_worktree
            else tempfile.TemporaryDirectory(prefix='syncwheel-stack-create-')
        )
        worktree = deterministic_worktree or Path(temporary.name)
        try:
            try:
                self.repository.git(repo_root, 'worktree', 'add', str(worktree), stack['branch'])
                if self.repository.ref_tip(repo_root, stack['branch']) != planned_tip:
                    raise SyncwheelError(
                        f"{stack['id']}: materialized draft tip differs from its reviewed projection"
                    )
            except BaseException:
                self.repository.git(repo_root, 'worktree', 'remove', '--force', str(worktree), check=False)
                self.repository.git(repo_root, 'update-ref', '-d', branch_ref, planned_tip, check=False)
                raise
            else:
                self.repository.git(repo_root, 'worktree', 'remove', '--force', str(worktree), check=False)
        finally:
            if temporary:
                temporary.cleanup()


    def anchor_diverged_draft_branch(
        self, manifest_path, stack_id, from_branch, draft_tip
    ):
        """Keep a diverged draft tip reachable, then drop the branch the promotion replaced.

        The anchor and the deletion are one ref transaction, so the tip is never
        unreachable and a failed anchor leaves the draft branch untouched.
        """
        repo_root = self.repository.root
        recovery_ref = coordination_draft_recovery_ref(stack_id)
        transaction = '\n'.join([
            'start',
            f'create {recovery_ref} {draft_tip}',
            f'delete refs/heads/{from_branch} {draft_tip}',
            'prepare',
            'commit',
            '',
        ])
        result = self.repository.git(
            repo_root, 'update-ref', '--stdin', check=False, input_text=transaction
        )
        committed = 'commit: ok' in {line.strip() for line in result.stdout.splitlines()}
        if (
            result.returncode != 0
            or not committed
            or self.repository.ref_tip(repo_root, recovery_ref) != draft_tip
            or self.repository.branch_exists(repo_root, from_branch)
        ):
            detail = result.stderr.strip() or result.stdout.strip() or 'no detail returned'
            raise SyncwheelError(
                f'{stack_id}: could not anchor the diverged draft branch {from_branch} '
                f'under {recovery_ref}: {detail}'
            )
        self.ledger.append_ledger_event(
            repo_root,
            'coordination_draft_divergence_anchored',
            {
                'stack': stack_id,
                'branch': from_branch,
                'tip': draft_tip,
                'recovery_ref': recovery_ref,
                'reason': 'promotion_completed_with_diverged_draft',
            },
            manifest_path,
        )
        print(
            f'{stack_id}: anchored diverged draft {from_branch} at {draft_tip} under '
            f'{recovery_ref}'
        )
        return recovery_ref


    def realign_promoted_local_branches(
        self, config, stack_id, from_branch, to_branch, promoted_tip,
        manifest_path=None,
    ):
        """Restore the branch layout an interrupted promotion had already reached.

        A promotion can land long before this recovery runs, and an ordinary command
        can rematerialize the draft branch from the manifest in between. Neither is
        evidence against the recorded intent.
        """
        repo_root = self.repository.root
        if not to_branch or from_branch == to_branch:
            return
        if not self.repository.branch_exists(repo_root, to_branch):
            if self.repository.branch_exists(repo_root, from_branch):
                self.repository.git(repo_root, 'branch', '-m', from_branch, to_branch)
                return
            if not promoted_tip:
                raise SyncwheelError(
                    f'{stack_id}: pending promote intent has no published tip to restore '
                    f'{to_branch} from'
                )
            if not self.repository.commit_exists(repo_root, promoted_tip):
                self.repository.git(
                    repo_root, 'fetch', '--quiet', config['remote'],
                    f'refs/heads/{to_branch}', check=False,
                )
            if not self.repository.commit_exists(repo_root, promoted_tip):
                raise SyncwheelError(
                    f'{stack_id}: cannot restore {to_branch} at {promoted_tip} from '
                    f"{config['remote']}"
                )
            created = self.repository.git(
                repo_root, 'update-ref', f'refs/heads/{to_branch}', promoted_tip,
                ZERO_OBJECT_ID, check=False,
            )
            if created.returncode != 0:
                raise SyncwheelError(
                    f'{stack_id}: {to_branch} appeared concurrently while recovering '
                    'the promotion'
                )
            return
        if not self.repository.branch_exists(repo_root, from_branch):
            return
        draft_tip = self.repository.ref_tip(repo_root, from_branch)
        if self.repository.git(
            repo_root, 'merge-base', '--is-ancestor', draft_tip, to_branch, check=False
        ).returncode != 0:
            self.anchor_diverged_draft_branch(
                manifest_path, stack_id, from_branch, draft_tip
            )
            return
        self.repository.git(repo_root, 'update-ref', '-d', f'refs/heads/{from_branch}', draft_tip)


    def equivalent_remote_stack_promotion(
        self, manifest, stack, from_branch, to_branch,
    ):
        """Return proof when another publisher already completed this promotion."""
        repo_root = self.repository.root
        if (
            not coordination_is_active(manifest)
            or from_branch == to_branch
            or stack.get('state', 'published') != 'draft'
        ):
            return None
        config = coordination_config(manifest)
        observed = self.state.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version']
        )
        state = observed.get('state') or {}
        if not state or not self.publication.coordination_state_matches_remote(config, state):
            return None
        remote_stack = stack_snapshot_map(state.get('manifest') or {}).get(stack['id'])
        proposed = copy.deepcopy(manifest)
        proposed_stack = require_stack(proposed, stack['id'])
        proposed_stack['branch'] = to_branch
        proposed_stack['state'] = 'published'
        proposed_stack['publication'] = {'enabled': True}
        proposed_stack = stack_snapshot_map(
            self.publication.coordination_manifest_snapshot(proposed, repo_root)
        ).get(stack['id'])
        if remote_stack != proposed_stack:
            return None
        from_ref = f'refs/heads/{from_branch}'
        to_ref = f'refs/heads/{to_branch}'
        promoted_tip = (state.get('managed_refs') or {}).get(to_ref)
        from_tip = self.repository.ref_tip(repo_root, from_branch)
        to_tip = self.repository.ref_tip(repo_root, to_branch)
        if (
            not promoted_tip
            or (from_tip is not None and from_tip != promoted_tip)
            or (to_tip is not None and to_tip != promoted_tip)
            or (from_tip is None and to_tip is None)
        ):
            return None
        matching_tombstone = next((
            item for item in state.get('tombstones') or []
            if item.get('stack') == stack['id']
            and coordination_tombstone_ref(item) == from_ref
            and item.get('reason') == 'promoted'
            and item.get('remote_tip') == promoted_tip
        ), None)
        if not matching_tombstone:
            return None
        return {
            'config': config,
            'observed': observed,
            'state_tip': observed['tip'],
            'promoted_tip': promoted_tip,
        }


    def adopt_equivalent_remote_stack_promotion(
        self, manifest, manifest_path, stack, from_branch, to_branch,
    ):
        repo_root = self.repository.root
        proof = self.equivalent_remote_stack_promotion(
            manifest, stack, from_branch, to_branch
        )
        if not proof:
            return False
        pending = self.intents.pending_coordination_publication_for_scope(
            manifest_path, f"promote:{stack['id']}"
        )
        if pending and self.intents.coordinated_operation_landed(
            proof['config'],
            proof['observed'],
            pending,
            claims_fallback=True,
        ):
            return False
        self.intents.resolve_pending_coordination_publications(manifest, manifest_path)
        proof = self.equivalent_remote_stack_promotion(
            manifest, stack, from_branch, to_branch
        )
        if not proof:
            raise SyncwheelError(
                f'{stack["id"]}: published promotion changed during local adoption'
            )
        self.control.require_manifest_transaction_current(manifest_path)
        self.realign_promoted_local_branches(
            proof['config'],
            stack['id'],
            from_branch,
            to_branch,
            proof['promoted_tip'],
            manifest_path=manifest_path,
        )
        stack['branch'] = to_branch
        stack['state'] = 'published'
        stack['publication'] = {'enabled': True}
        self.control.save_manifest(manifest_path, manifest)
        self.ledger.append_ledger_event(
            repo_root,
            'stack_promoted',
            {
                'stack': stack['id'],
                'from_branch': from_branch,
                'branch': to_branch,
                'coordination_state': proof['state_tip'],
                'adopted': True,
                'actor': self.repository.governed_worktree_owner(),
            },
            manifest_path,
        )
        print(f"{stack['id']}: adopted equivalent published promotion")
        print(f'  branch: {from_branch} -> {to_branch}')
        print(f"  coordination state: {proof['state_tip']}")
        return True


    def recover_pending_stack_promote(
        self, manifest, manifest_path, stack, pending
    ):
        repo_root = self.repository.root
        rename = pending.get('rename')
        state_transition = pending.get('state_transition')
        if rename:
            to_branch = rename.get('to_branch')
            if (
                rename.get('stack') != stack['id']
                or stack['branch'] not in {rename.get('from_branch'), to_branch}
            ):
                raise SyncwheelError(
                    f"{stack['id']}: pending promote intent does not match the local stack generation"
                )
            self.realign_promoted_local_branches(
                coordination_config(manifest),
                stack['id'],
                rename.get('from_branch'),
                to_branch,
                (pending.get('changed_refs') or {}).get(f'refs/heads/{to_branch}'),
                manifest_path=manifest_path,
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
        operation = self.intents.renew_coordination_publication(
            manifest, manifest_path, pending
        )
        result = self.publication.coordinated_publish(
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
        self.control.save_manifest(manifest_path, manifest)
        self.ledger.append_ledger_event(
            repo_root,
            'stack_promoted',
            {
                'stack': stack['id'],
                'from_branch': rename.get('from_branch') if rename else stack['branch'],
                'branch': stack['branch'],
                'coordination_state': result.get('state_tip'),
                'recovered': True,
                'actor': self.repository.governed_worktree_owner(),
            },
            manifest_path,
        )
        self.intents.complete_coordination_publication(manifest_path, operation, result)
        print(f"{stack['id']}: recovered interrupted draft promotion")
        return 0


    def complete_pending_promote_intents(
        self, manifest, manifest_path, *, apply=True, skip_stacks=(),
        only_stacks=None,
    ):
        """Finish promotions that landed before their manifest was saved.

        Only the manifest is behind the published state, and any command that
        rebuilds managed branches from it would resurrect the draft the promotion
        already replaced. Reporting the remedy mutates nothing, so it must not take
        the publication lock and must not be refused by a live publication.
        """
        repo_root = self.repository.root
        if not coordination_is_active(manifest):
            return
        if not any(
            str(intent.get('scope') or '').startswith('promote:')
            for intent in self.intents.pending_coordination_publications(manifest_path)
        ):
            return
        guard = (
            self.publication.coordination_publication_lock() if apply else contextlib.nullcontext()
        )
        with guard:
            self.resolve_pending_promote_intents(
                manifest,
                manifest_path,
                apply=apply,
                skip_stacks=skip_stacks,
                only_stacks=only_stacks,
            )


    def resolve_pending_promote_intents(
        self, manifest, manifest_path, *, apply=True, skip_stacks=(),
        only_stacks=None,
    ):
        repo_root = self.repository.root
        selected = None if only_stacks is None else set(only_stacks)
        pending = [
            intent for intent in self.intents.pending_coordination_publications(manifest_path)
            if str(intent.get('scope') or '').startswith('promote:')
            and str(intent.get('scope'))[len('promote:'):] not in set(skip_stacks)
            and (
                selected is None
                or str(intent.get('scope'))[len('promote:'):] in selected
            )
        ]
        if not pending:
            return
        config = coordination_config(manifest)
        observed = self.state.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version']
        )
        for intent in pending:
            stack_id = str(intent.get('scope'))[len('promote:'):]
            stack = next(
                (item for item in manifest['stacks'] if item['id'] == stack_id), None
            )
            if stack is None or not self.intents.coordinated_operation_landed(
                config, observed, intent, claims_fallback=True
            ):
                continue
            if not apply:
                print(
                    f'{stack_id}: a landed promotion is still pending; complete it with:'
                    f'\n  syncwheel stack promote {stack_id}'
                )
                continue
            self.recover_pending_stack_promote(
                manifest, manifest_path, stack, intent
            )


    def publish_updated_draft_before_promotion(self, manifest, manifest_path, stack):
        """Publish pending draft edits without widening the rename permission."""
        repo_root = self.repository.root
        if not coordination_is_active(manifest):
            return
        config = coordination_config(manifest)
        observed = self.state.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version']
        )
        state = observed.get('state') or {}
        remote_stack = stack_snapshot_map(state.get('manifest') or {}).get(stack['id'])
        local_stack = stack_snapshot_map(self.publication.coordination_manifest_snapshot(manifest, repo_root))[stack['id']]
        if remote_stack == local_stack:
            return
        # Only an existing draft on the same ref may take this preliminary step.
        # Foreign promotions and ownership changes remain the rename guard's job.
        if not remote_stack or remote_stack.get('state') != 'draft' or remote_stack['branch'] != stack['branch']:
            return
        # Refresh only appended/replaced commit selections here. Other semantic
        # edits still require explicit publication before requesting promotion.
        if ({key: value for key, value in remote_stack.items() if key != 'commits'}
                != {key: value for key, value in local_stack.items() if key != 'commits'}):
            return
        changed_refs = {f"refs/heads/{stack['branch']}": self.repository.ref_tip(repo_root, stack['branch'])}
        operation = self.intents.begin_coordination_publication(
            manifest, manifest_path, changed_refs, f"stack:{stack['id']}", 'partial',
        )
        result = self.publication.coordinated_publish(
            manifest, manifest_path, operation['changed_refs'],
            f"stack:{stack['id']}", 'partial',
            expected_coordination_state_tip=operation['expected_coordination_state_tip'],
            operation_token=operation['operation_token'],
        )
        self.intents.complete_coordination_publication(manifest_path, operation, result)


    def command_stack_promote(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        self.complete_pending_promote_intents(
            manifest,
            manifest_path,
            skip_stacks={args.stack},
            only_stacks={args.stack},
        )
        stack = require_stack(manifest, args.stack)
        from_branch = stack['branch']
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
        if stack.get('state', 'published') == 'draft':
            try:
                if self.adopt_equivalent_remote_stack_promotion(
                    manifest, manifest_path, stack, from_branch, to_branch
                ):
                    return 0
            except SyncwheelError as exc:
                failure = self.intents.coordinated_publish_command_failure(
                    coordination_config(manifest) if coordination_is_active(manifest) else None,
                    exc,
                    f'stack promote {args.stack}',
                )
                if failure is None:
                    raise
                raise failure from exc
        pending_promotion = self.intents.pending_coordination_publication_for_scope(
            manifest_path, f'promote:{args.stack}'
        )
        if pending_promotion:
            try:
                return self.recover_pending_stack_promote(
                    manifest, manifest_path, stack, pending_promotion
                )
            except SyncwheelError as exc:
                failure = self.intents.coordinated_publish_command_failure(
                    coordination_config(manifest) if coordination_is_active(manifest) else None,
                    exc,
                    f'stack promote {args.stack}',
                )
                if failure is None:
                    raise
                raise failure from exc
        if stack.get('state', 'published') != 'draft':
            raise SyncwheelError(f"{args.stack}: promote requires state draft (found {stack.get('state', 'published')})")
        if not self.repository.branch_exists(repo_root, from_branch):
            raise SyncwheelError(f"{args.stack}: cannot promote draft without a materialized branch: {from_branch}")
        if to_branch != from_branch:
            if self.repository.branch_exists(repo_root, to_branch):
                raise SyncwheelError(f'cannot promote onto an existing local branch: {to_branch}')

        previous_worktree_path = None
        worktree_root = effective_worktree_root(manifest)
        if worktree_root:
            safe = from_branch.replace('/', '-').replace('\\', '-')
            previous_worktree_path = resolve_worktree_root_path(repo_root, worktree_root) / safe

        renamed = False
        coordination_result = None
        publication_operation = None
        try:
            self.publish_updated_draft_before_promotion(manifest, manifest_path, stack)
            stack['branch'] = to_branch
            stack['state'] = 'published'
            stack['publication'] = {'enabled': True}
            self.control.require_manifest_transaction_current(manifest_path)
            if to_branch != from_branch:
                self.repository.run(['git', 'branch', '-m', from_branch, to_branch], cwd=repo_root)
                renamed = True

            if coordination_is_active(manifest):
                config = coordination_config(manifest)
                if to_branch != from_branch:
                    from_ref = f'refs/heads/{from_branch}'
                    from_ref_tip = self.state.remote_ref_tips(config['remote'], [from_ref])[from_ref]
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
                    publication_operation = self.intents.begin_coordination_publication(
                        manifest,
                        manifest_path,
                        {f'refs/heads/{to_branch}': self.repository.ref_tip(repo_root, to_branch)},
                        f'promote:{args.stack}',
                        'partial',
                        tombstone=tombstone,
                        rename=rename,
                    )
                    coordination_result = self.publication.coordinated_publish(
                        manifest,
                        manifest_path,
                        {f'refs/heads/{to_branch}': self.repository.ref_tip(repo_root, to_branch)},
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
                    publication_operation = self.intents.begin_coordination_publication(
                        manifest,
                        manifest_path,
                        {},
                        f'promote:{args.stack}',
                        'partial',
                        state_transition=state_transition,
                    )
                    coordination_result = self.publication.coordinated_publish(
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
        except Exception as exc:
            if renamed and self.repository.branch_exists(repo_root, to_branch) and not self.repository.branch_exists(repo_root, from_branch):
                self.repository.git(repo_root, 'branch', '-m', to_branch, from_branch, check=False)
            if isinstance(exc, SyncwheelError):
                failure = self.intents.coordinated_publish_command_failure(
                    coordination_config(manifest) if coordination_is_active(manifest) else None,
                    exc,
                    f'stack promote {args.stack}',
                )
                if failure is not None:
                    raise failure from exc
            raise

        self.control.save_manifest(manifest_path, manifest)
        self.ledger.append_ledger_event(
            repo_root,
            'stack_promoted',
            {
                'stack': args.stack,
                'from_branch': from_branch,
                'branch': to_branch,
                'coordination_state': coordination_result.get('state_tip') if coordination_result else None,
                'actor': self.repository.governed_worktree_owner(),
            },
            manifest_path,
        )
        if publication_operation:
            self.intents.complete_coordination_publication(
                manifest_path, publication_operation, coordination_result
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


    def command_stack_demote(self, args, manifest, manifest_path):
        repo_root = self.repository.root
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
        self.control.require_manifest_transaction_current(manifest_path)
        if coordination_is_active(manifest):
            state_transition = {
                'stack': args.stack,
                'from_state': 'published',
                'to_state': 'draft',
            }
            publication_operation = self.intents.begin_coordination_publication(
                manifest,
                manifest_path,
                {},
                f'demote:{args.stack}',
                'partial',
                state_transition=state_transition,
            )
            coordination_result = self.publication.coordinated_publish(
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
        self.control.save_manifest(manifest_path, manifest)
        self.ledger.append_ledger_event(
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
            self.intents.complete_coordination_publication(
                manifest_path, publication_operation, coordination_result
            )
        print(f'{args.stack}: demoted published -> draft')
        print(f"  branch: {stack['branch']} (unchanged)")
        if coordination_result:
            print(f"  coordination state: {coordination_result['status']}")
        return 0


    def refuse_pending_stack_sync_promotion(self, manifest_path, remedy_selection):
        for intent in self.intents.pending_coordination_publications(manifest_path):
            scope = str(intent.get('scope') or '')
            if scope.startswith('promote:'):
                pending_stack = scope[len('promote:'):]
                raise SyncwheelError(
                    'pending stack promotion; complete it with '
                    f'syncwheel stack promote {quoted([pending_stack])} {remedy_selection}'
                )

    def consistent_stack_adoption(self, stack, remedy_selection):
        adopted = validate_stack_adoption(stack)
        if not adopted:
            return None
        root = self.repository.root
        ref = f"refs/heads/{stack['branch']}"
        offline = {'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': '', 'GIT_OPTIONAL_LOCKS': '0'}

        def observe(*args, allowed=(0,)):
            result = self.repository.git(root, *args, check=False, env=offline)
            if result.returncode not in allowed:
                raise SyncwheelError(
                    f'cannot verify adopted history: Git {args[0]} failed; inspect repository objects before retrying'
                )
            return result

        stale = (
            observe('symbolic-ref', '--quiet', ref, allowed=(0, 1)).returncode == 0
            or observe('rev-parse', '--verify', '--quiet', ref, allowed=(0, 1)).stdout.strip() != adopted['tip']
            or any(observe('cat-file', '-t', oid).stdout.strip() != 'commit'
                   for oid in (adopted['base'], adopted['tip']))
        )
        if not stale and adopted['tip'] != adopted['base']:
            parents = observe('rev-list', '--parents', '-n', '1', adopted['tip']).stdout.split()[1:]
            contained = [parent for parent in parents
                         if observe('merge-base', '--is-ancestor', parent, adopted['base'], allowed=(0, 1)).returncode == 0]
            stale = contained != [adopted['base']]
        if stale:
            raise SyncwheelError(
                'adopted history is stale; use explicit '
                f"syncwheel stack set {quoted([stack['id']])} --base BASE --adopt-tip TIP {remedy_selection}"
            )
        return adopted

    def require_stack_absorb_allowed(self, stack, args, remedy_selection):
        if self.consistent_stack_adoption(stack, remedy_selection):
            mode = ['--staged'] if args.staged else []
            paths = ['--', *args.paths] if args.paths else []
            remedy = quoted(['syncwheel', 'stack', 'absorb', 'DRAFT', *mode, '-N', '-m', 'MESSAGE'])
            raise SyncwheelError(
                'stack is adopted; cannot absorb into its pinned history. Patch remains unchanged. '
                'Use an existing non-adopted draft; primary ownership and staged-path guards still apply. '
                'Replace DRAFT and MESSAGE in: '
                f'{remedy} {remedy_selection} {quoted(paths)}'
            )
        require_stack_absorb_amendable(stack, args)

    def observe_adopted_stack_sync(self, args, stack, remedy_selection):
        if not self.consistent_stack_adoption(stack, remedy_selection):
            return None
        print(f'{args.stack}: stack is adopted; nothing to sync. Next: int rebuild / push')
        print(f'  syncwheel int rebuild --dry-run {remedy_selection}')
        print(f'  syncwheel stack push {quoted([args.stack])} --dry-run {remedy_selection}')
        return 0

    def command_stack_sync(self, args, manifest, manifest_path, remedy_selection):
        repo_root = self.repository.root
        stack = require_stack(manifest, args.stack)
        self.refuse_pending_stack_sync_promotion(manifest_path, remedy_selection)
        observed = self.observe_adopted_stack_sync(args, stack, remedy_selection)
        if observed is not None:
            return observed
        commits = self.repository.rev_list(
            repo_root, f"{stack['base']}..{stack['branch']}", offline=True,
        )
        stack['commits'] = commits
        self.control.save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'stack_sync',
            {'stack': args.stack, 'branch': stack['branch']},
        )
        print(f"{args.stack}: synced {len(commits)} commits from {stack['branch']}")
        return 0


    def require_empty_stack_absorb_base(self, stack):
        if stack['commits']:
            return None
        repo_root = self.repository.root
        branch_ref = f"refs/heads/{stack['branch']}"
        base = self.repository.commit_full_sha(repo_root, stack['base'])
        tip = self.repository.ref_tip(repo_root, branch_ref)
        symbolic = self.repository.git(repo_root, 'symbolic-ref', '--quiet', branch_ref, check=False)
        if symbolic.returncode == 0 or tip not in (None, base):
            raise SyncwheelError(
                f"empty stack {stack['id']!r} branch does not match its base as a direct ref "
                f"({stack['branch']!r}); use a new draft for this patch and classify the existing history explicitly"
            )
        return base, tip


    def command_stack_absorb(self, args, manifest, manifest_path, remedy_selection=''):
        repo_root = self.repository.root
        stack = require_stack(manifest, args.stack)
        self.require_stack_absorb_allowed(stack, args, remedy_selection)
        empty_start = self.require_empty_stack_absorb_base(stack)
        integration_branch = stack.get('integration_branch') or manifest['integration']['branch']
        current_branch = self.repository.get_current_branch(repo_root)
        if current_branch != integration_branch and not args.force:
            raise SyncwheelError(
                f"stack absorb expects the integration branch {integration_branch!r}; "
                f"current branch is {current_branch!r}. Pass --force to override."
            )

        pathspec = args.paths or []
        separator = ['--', *pathspec] if pathspec else []
        if not args.staged:
            staged = self.repository.git(repo_root, 'diff', '--cached', '--quiet', '--', *pathspec, check=False)
            if staged.returncode == 1:
                raise SyncwheelError('staged changes exist; pass --staged or unstage them before absorbing unstaged changes')

        diff_args = ['diff', '--binary']
        if args.staged:
            diff_args.append('--cached')
        diff_args.extend(separator)
        patch = self.repository.git(repo_root, *diff_args).stdout
        if not patch.strip():
            source = 'staged changes' if args.staged else 'working tree changes'
            raise SyncwheelError(f'no {source} to absorb')

        self.control.require_manifest_transaction_current(manifest_path)
        stack_worktree = self.resolve_stack_absorb_location(manifest_path, manifest, stack, args)
        if empty_start is not None and self.repository.ref_tip(stack_worktree, 'HEAD') != empty_start[0]:
            raise SyncwheelError('empty stack worktree no longer matches its base; preserve it and review the branch')
        # The patch and commit touch only selected paths. Unrelated unstaged work
        # in the stack worktree remains outside this operation, but the real index
        # and patch paths must be clean before `git apply --index`.
        if self.repository.git(stack_worktree, 'ls-files', '-u').stdout.strip() or self.repository.git(
            stack_worktree, 'diff', '--cached', '--quiet', check=False
        ).returncode != 0:
            raise SyncwheelError(f'{stack_worktree} has index changes or conflicts')
        patch_paths = set(item for item in self.repository.git(
            repo_root, 'diff', *(['--cached'] if args.staged else []),
            '--name-only', '--no-renames', '-z', *separator,
        ).stdout.split('\0') if item)
        target_dirty = set(item for item in self.repository.git(
            stack_worktree, 'diff', '--name-only', '-z'
        ).stdout.split('\0') if item)
        target_dirty.update(item for item in self.repository.git(
            stack_worktree, 'ls-files', '--others', '--exclude-standard', '-z'
        ).stdout.split('\0') if item)
        overlap = sorted(patch_paths & target_dirty)
        if overlap:
            raise SyncwheelError('stack absorb target patch paths are dirty: '
                                 + ', '.join(overlap))
        apply_patch = self.repository.run(['git', '-C', str(stack_worktree), 'apply', '--index'], input_text=patch, check=False)
        if apply_patch.returncode != 0:
            raise SyncwheelError(apply_patch.stderr.strip() or apply_patch.stdout.strip() or 'failed to apply patch to stack worktree')

        if args.amend:
            self.repository.run(self.repository.with_git_identity(stack_worktree, ['git', 'commit', '--amend', '--no-edit']), cwd=stack_worktree)
        else:
            message = args.message or f"chore: absorb integration changes into {args.stack}"
            self.repository.run(self.repository.with_git_identity(stack_worktree, ['git', 'commit', '-m', message]), cwd=stack_worktree)

        reverse_args = ['apply', '--reverse']
        if args.staged:
            self.repository.git(repo_root, *reverse_args, '--cached', input_text=patch)
        self.repository.git(repo_root, *reverse_args, input_text=patch)

        stack['commits'] = self.repository.rev_list(repo_root, f"{stack['base']}..{stack['branch']}")
        self.control.save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'stack_absorb',
            {'stack': args.stack, 'branch': stack['branch']},
        )
        print(f"{args.stack}: absorbed changes into {stack['branch']} and synced {len(stack['commits'])} commits")
        return 0


    def resolve_stack_absorb_location(self, manifest_path, manifest, stack, args):
        repo_root = self.repository.root
        branch = stack['branch']
        empty_start = self.require_empty_stack_absorb_base(stack)

        def add_worktree(path):
            options = [str(path), branch]
            if empty_start is not None and empty_start[1] is None:
                options = ['-b', branch, str(path), empty_start[0]]
            self.repository.run(['git', 'worktree', 'add', *options], cwd=repo_root)

        existing = self.repository.find_worktree_for_branch(repo_root, branch)
        if args.worktree:
            path = Path(args.worktree).expanduser().resolve()
            if existing and existing != path:
                raise SyncwheelError(
                    f"branch {branch!r} already has a worktree at {existing}; "
                    'reuse that worktree or pass its path with --worktree'
                )
            if not existing:
                add_worktree(path)
            return path
        if existing:
            return existing
        path = self.projections.reconcile_worktree_path(repo_root, branch, effective_worktree_root(manifest, args.worktree_root))
        worktree_root = effective_worktree_root(manifest, args.worktree_root)
        if self.control.is_external_manifest_path(repo_root, manifest_path):
            self.control.ensure_syncwheel_worktree_root_excluded(repo_root, worktree_root)
        else:
            self.control.ensure_syncwheel_metadata_excluded(repo_root, manifest.get('syncwheel_tracking'), worktree_root)
        add_worktree(path)
        return path


    def command_stack_set(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        if getattr(args, 'base', None) or getattr(args, 'adopt_tip', None):
            return StackAdoption(self).execute_stack_adoption(args, manifest, manifest_path)
        if getattr(args, 'plan_digest', None) or getattr(args, 'apply', False):
            raise SyncwheelError('--plan-digest and --apply require --base and --adopt-tip')
        stack = require_stack(manifest, args.stack)
        target_branch = getattr(args, 'target_branch', None)
        if args.published:
            if target_branch is not None:
                raise SyncwheelError('stack set --published cannot be combined with --target-branch')
            if args.specs:
                raise SyncwheelError('stack set --published takes no commit specs')
            return self.reset_stack_to_published(manifest, manifest_path, stack, args)
        if not args.specs and target_branch is None:
            raise SyncwheelError('stack set requires commit specs, --target-branch, or --published')
        if target_branch is not None:
            if (target_branch.startswith('-')
                    or not self.repository.is_valid_coordination_branch_ref(f'refs/heads/{target_branch}')):
                raise SyncwheelError('stack set --target-branch requires a valid branch name')
            if target_branch in (stack['branch'], stack.get('integration_branch'),
                                 manifest['integration']['branch']):
                raise SyncwheelError('stack delivery target cannot be its source or integration branch')
        previous_target = stack['target_branch']
        if args.specs:
            commits = []
            for spec in args.specs:
                commits.extend(self.repository.commit_list_for_spec(repo_root, spec))
            stack['commits'] = list(dict.fromkeys(commits))
            stack.pop('adopted_history', None)
        if target_branch is not None:
            stack['target_branch'] = target_branch
        context = {'stack': args.stack, 'branch': stack['branch'], 'actor': self.repository.governed_worktree_owner()}
        if target_branch is not None:
            context.update(previous_target_branch=previous_target, target_branch=target_branch)
        self.control.save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'stack_set',
            context,
        )
        print(f"{args.stack}: set {len(stack['commits'])} commits")
        return 0


    def reset_stack_to_published(self, manifest, manifest_path, stack, args):
        repo_root = self.repository.root
        if not coordination_is_active(manifest):
            raise SyncwheelError('stack set --published requires active-active coordination')
        config = coordination_config(manifest)
        observed = self.state.read_remote_coordination_state(
            config, fetch=True, local_manifest_version=manifest['version'],
        )
        snapshot = (observed.get('state') or {}).get('manifest') or {}
        if stack['id'] not in stack_snapshot_map(snapshot):
            raise SyncwheelError(
                f"{stack['id']}: stack is not published, so there is no published entry to reset to"
            )
        local_entry = stack_snapshot_map(self.publication.coordination_manifest_snapshot(manifest, repo_root))[stack['id']]
        published_entry = stack_snapshot_map(snapshot)[stack['id']]
        other_fields = [
            field for field in changed_snapshot_fields(published_entry, local_entry)
            if field != 'commits'
        ]
        if other_fields:
            raise SyncwheelError(
                f"{stack['id']}: local entry differs from the published one in "
                f"{', '.join(other_fields)}; stack set --published only resets commits"
            )
        missing = [
            commit for commit in published_entry['commits'] if not self.repository.commit_exists(repo_root, commit)
        ]
        if missing:
            self.repository.git(repo_root, 'fetch', '--quiet', config['remote'], check=False)
            missing = [commit for commit in missing if not self.repository.commit_exists(repo_root, commit)]
        if missing:
            owner = format_stack_owner(self.publication.stack_owner_summary(
                manifest_path, config, stack['id'], state_tip=observed['tip']
            ))
            raise SyncwheelError(
                f"{stack['id']}: published commit(s) {', '.join(missing)} are not reachable "
                f"from {config['remote']}; the owner must publish them ({owner})"
            )
        previous = list(stack['commits'])
        stack['commits'] = list(published_entry['commits'])
        self.control.save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'stack_set',
            {
                'stack': stack['id'],
                'branch': stack['branch'],
                'published_state': observed['tip'],
                'previous_commits': previous,
                'actor': self.repository.governed_worktree_owner(),
            },
        )
        restore = quoted([
            'syncwheel', 'stack', 'set', stack['id'],
            *(previous or [f"{stack['base']}..{stack['base']}"]),
            *manifest_scope_options(args),
        ])
        print(f"{stack['id']}: reset to the published entry ({len(stack['commits'])} commits)")
        print(f'Restore the previous local commits with:\n  {restore}')
        return 0


    def command_stack_resolve_integration(self, args, manifest, manifest_path):
        """Record conflict-resolved commits that already materialize on integration."""
        repo_root = self.repository.root
        stack = require_stack(manifest, args.stack)
        if getattr(args, 'worktree', None):
            return self.projections.capture_integration_desk(repo_root, manifest, manifest_path, stack, args)
        integration_branch = manifest['integration']['branch']
        if args.empty and args.specs:
            raise SyncwheelError('use either --empty or resolved integration commit specs, not both')
        if not args.empty and not args.specs:
            raise SyncwheelError('provide resolved integration commit specs or pass --empty explicitly')
        commits = []
        for spec in args.specs:
            commits.extend(self.repository.commit_list_for_spec(repo_root, spec))
        commits = list(dict.fromkeys(commits))
        for commit in commits:
            if not self.repository.branch_contains(repo_root, integration_branch, commit):
                raise SyncwheelError(
                    f"resolved integration commit is not on {integration_branch}: {commit}"
                )
        stack['integration_commits'] = commits
        stack.pop('integration_resolution', None)
        self.control.save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'stack_resolve_integration',
            {'stack': args.stack, 'integration_branch': integration_branch, 'commits': commits},
        )
        print(f"{args.stack}: recorded {len(commits)} resolved integration commits")
        return 0


    def build_stack_classify_integration_plan(self, manifest, manifest_path, stack_id, specs):
        """Plan manifest-only ownership of commits already present on integration."""
        repo_root = self.repository.root
        stack = require_stack(manifest, stack_id)
        integration_branch = manifest['integration']['branch']
        if stack_id not in manifest['integration'].get('stacks', []):
            raise SyncwheelError(
                f'stack is not included in {integration_branch}: {stack_id}'
            )
        if not self.repository.branch_exists(repo_root, integration_branch):
            raise SyncwheelError(f'integration branch does not exist: {integration_branch}')
        commits = []
        for spec in specs:
            commits.extend(self.repository.commit_list_for_spec(repo_root, spec))
        commits = list(dict.fromkeys(self.repository.commit_full_sha(repo_root, commit) for commit in commits))
        if not commits:
            raise SyncwheelError('provide at least one integration commit to classify')
        for commit in commits:
            if not self.repository.branch_contains(repo_root, integration_branch, commit):
                raise SyncwheelError(
                    f'integration-only commit is not on {integration_branch}: {commit}'
                )
        owners = {}
        for candidate in manifest['stacks']:
            for commit in stack_integration_commits(candidate):
                if self.repository.commit_exists(repo_root, commit):
                    owners.setdefault(self.repository.commit_full_sha(repo_root, commit), candidate['id'])
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


    def command_stack_classify_integration(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        plan, proposed = self.build_stack_classify_integration_plan(
            manifest, manifest_path, args.stack, args.specs
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
        with self.control.manifest_write_transaction(
            repo_root, manifest_path, f'stack-classify-integration-{args.stack}'
        ):
            current = self.control.require_locked_manifest_observation(repo_root, manifest_path, plan)
            current_plan, proposed = self.build_stack_classify_integration_plan(
                current, manifest_path, args.stack, args.specs
            )
            if current_plan['planDigest'] != args.plan_digest:
                raise SyncwheelError(
                    'integration classification plan is stale after revalidation; generate a new preview'
                )
            self.control.save_manifest_with_ledger(
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


    def validate_stack_update(self, manifest, stack, previous_commits):
        repo_root = self.repository.root
        validate_stack_adoption(stack)
        report = self.projections.stack_reconcile_report(repo_root, manifest, stack)
        if report.get('projection_error'):
            stack['commits'] = previous_commits
            detail = report['projection_error']
            raise SyncwheelError(
                f"stack {stack['id']} projection failed after adding commits; "
                f"the stack branch cannot be rebuilt cleanly from the manifest:\n{detail}"
            )


    def validate_integration_first_base(self, manifest, added_commits):
        repo_root = self.repository.root
        if not added_commits:
            return
        integration = manifest['integration']
        integration_branch = integration['branch']
        if self.repository.get_current_branch(repo_root) != integration_branch:
            return
        first_added = added_commits[0]
        if not self.repository.branch_contains(repo_root, integration_branch, first_added):
            return
        parent = self.repository.commit_first_parent(repo_root, first_added)
        if not parent:
            return
        expected_tree = self.replay.materialize_integration_projection(manifest)
        parent_tree = self.repository.ref_tree(repo_root, parent)
        if parent_tree != expected_tree:
            raise SyncwheelError(
                f"cannot add {first_added} from integration branch {integration_branch!r}: "
                "the commit was not created on top of the current manifest projection. "
                "Run `syncwheel reconcile` and apply the required integration rebuild before "
                "creating or adding more integration-first commits."
            )


    def command_stack_add(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        self.complete_pending_promote_intents(
            manifest, manifest_path, only_stacks={args.stack},
        )
        stack = require_stack(manifest, args.stack)
        previous_commits = list(stack['commits'])
        commits = list(previous_commits)
        previous_full_shas = {self.repository.commit_full_sha(repo_root, commit) for commit in previous_commits if self.repository.commit_exists(repo_root, commit)}
        added_commits = []
        for spec in args.specs:
            for commit in self.repository.commit_list_for_spec(repo_root, spec):
                commits.append(commit)
                if self.repository.commit_full_sha(repo_root, commit) not in previous_full_shas:
                    added_commits.append(commit)
        self.validate_integration_first_base(manifest, added_commits)
        stack['commits'] = list(dict.fromkeys(commits))
        self.validate_stack_update(manifest, stack, previous_commits)
        self.control.save_manifest_with_ledger(
            repo_root,
            manifest_path,
            manifest,
            'stack_add',
            {
                'stack': args.stack,
                'branch': stack['branch'],
                'added_commits': added_commits,
                'actor': self.repository.governed_worktree_owner(),
            },
        )
        self.worktrees.capture_governed_worktrees_for_stack(
            manifest,
            args.stack,
            manifest_path=manifest_path,
        )
        print(f"{args.stack}: now has {len(stack['commits'])} commits")
        return 0


    def rebuild_stack_from_manifest(
        self,
        manifest,
        manifest_path,
        stack,
        *,
        dry_run,
        mode,
        worktree,
    ):
        """Rebuild one stack through the shared replay executor and ledger path."""
        repo_root = self.repository.root
        adopted = validate_stack_adoption(stack)
        if adopted:
            current = self.repository.ref_tip(repo_root, stack['branch'])
            if current != adopted['tip']:
                raise SyncwheelError('adopted stack tip changed; use an explicit stack set adoption')
            print(f"{stack['id']}: preserving adopted tip {current}")
            return {'status': 'ok', 'before_tip': current, 'after_tip': current, 'mode': mode}
        require_nonempty_desk_stack_rebuild(stack, mode)
        if not dry_run and mode == 'in-place':
            self.projections.ensure_in_place_target(
                repo_root, stack['branch'], manifest, stack['id'], manifest_path=manifest_path,
            )
        if not dry_run and mode in ('ephemeral', 'plumbing'):
            self.projections.ensure_non_in_place_target_clean(
                repo_root,
                stack['branch'],
                self.repository.find_worktree_for_branch(repo_root, stack['branch']),
            )
        if not dry_run and mode == 'desk':
            self.projections.ensure_non_in_place_target_clean(repo_root, stack['branch'], worktree)
        plan = self.replay.replay_plan(manifest, replay_target(stack=stack, worktree=worktree), mode)
        if not dry_run and mode == 'desk':
            plan['register_created_desk'] = lambda path: self.projections.register_created_desk(
                repo_root, manifest_path, stack, path,
            )
        result = self.replay.execute_replay(plan, not dry_run)
        require_replay_success(result)
        if not dry_run:
            self.ledger.append_ledger_event(
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


    def observe_capture_inputs(self, manifest, stack, specs):
        repo_root = self.repository.root
        integration = manifest['integration']
        siblings = [require_stack(manifest, stack_id) for stack_id in integration['stacks']]
        revisions = {integration['base'], stack['base']}
        refs = {f"refs/heads/{integration['branch']}", f"refs/heads/{stack['branch']}"}
        for item in siblings:
            for key in ('commits', 'integration_commits', 'integration_only_commits'):
                revisions.update(item.get(key) or ())
            if integration.get('strategy') == 'merge-stacks':
                refs.add(f"refs/heads/{item['branch']}")
        revisions.update(stack['commits'])
        resolved = {value: self.repository.commit_full_sha(repo_root, value)
                    for value in sorted(revisions)}
        spec_endpoints = {endpoint for spec in specs for endpoint in re.split(r'\.\.\.?', spec)}
        # Include endpoints and peeled/ancestry expressions in the ref lease.
        ref_names = {re.split(r'[~^]', value, maxsplit=1)[0]
                     for value in revisions | spec_endpoints if value}
        refs.update(
            ref for value in ref_names
            if (ref := self.repository.git(
                repo_root, 'rev-parse', '--symbolic-full-name', '--verify', '--end-of-options', value, check=False,
            ).stdout.strip()).startswith('refs/')
        )
        direct = {}
        for ref in sorted(refs):
            self.repository.git(repo_root, 'check-ref-format', ref)
            if self.repository.git(repo_root, 'symbolic-ref', '--quiet', ref, check=False).returncode == 0:
                raise SyncwheelError(f'stack capture-integration requires a direct ref: {ref}')
            direct[ref] = self.repository.git(repo_root, 'show-ref', '--verify', '--hash', ref,
                                              check=False).stdout.strip() or None
        requested = [
            self.repository.commit_full_sha(repo_root, commit)
            for spec in specs
            for commit in self.repository.commit_list_for_spec(repo_root, spec)
        ]
        return resolved, direct, requested

    def publish_capture_tip(self, branch, before, tip, direct):
        ref = f'refs/heads/{branch}'
        zero = '0' * len(tip)
        operations = []
        for dependency, oid in sorted(direct.items()):
            if dependency != ref:
                operations.append(f'verify {dependency} {oid or zero}')
        if before == tip:
            operations.append(f'verify {ref} {tip}')
            backup = None
        else:
            backup = f'refs/heads/{backup_branch_name(branch, syncwheel_timestamp())}' if before else None
            if backup:
                operations.append(f'create {backup} {before}')
            operations.append(f'update {ref} {tip} {before or zero}')
        self.repository.capture_ref_transaction(operations)
        return backup

    def rollback_capture_tip(self, branch, before, tip, backup):
        ref = f'refs/heads/{branch}'
        operations = []
        operations.append(f'update {ref} {before} {tip}' if before else f'delete {ref} {tip}')
        if backup:
            operations.append(f'delete {backup} {before}')
        self.repository.capture_ref_transaction(operations)

    def command_stack_capture_integration(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        self.projections.verify_retained_captured_worktrees(repo_root, manifest, args)
        stack = require_stack(manifest, args.stack)

        if validate_stack_adoption(stack):
            raise SyncwheelError('adopted history requires explicit stack set --base BASE --adopt-tip TIP')
        original = copy.deepcopy(manifest)
        inputs = self.observe_capture_inputs(original, stack, args.specs)
        resolved, direct, captured_commits = inputs
        branch = stack['branch']
        before = direct[f'refs/heads/{branch}']
        if branch == manifest['integration']['branch']:
            raise SyncwheelError('capture requires a stack branch distinct from integration')
        destination = self.projections.checkout_reset_destination_lease(repo_root, branch)

        before_commits = [resolved[value] for value in stack['commits']]
        declared = list(dict.fromkeys([*before_commits, *captured_commits]))
        added_commits = [commit for commit in declared if commit not in before_commits]
        self.validate_integration_first_base(manifest, added_commits)
        stack['commits'] = declared
        proposed = copy.deepcopy(manifest)
        proposed['integration']['base'] = resolved[manifest['integration']['base']]
        projected_stack = require_stack(proposed, args.stack)
        projected_stack['base'] = resolved[stack['base']]
        try:
            tip = self.replay.materialize_capture_tip(projected_stack)
        except SyncwheelError as exc:
            raise SyncwheelError(f"stack {args.stack} projection failed after adding commits:\n{exc}") from exc
        overrides = {args.stack: tip}
        for stack_id in proposed['integration']['stacks']:
            item = require_stack(proposed, stack_id)
            for key in ('integration_commits', 'integration_only_commits'):
                if item.get(key):
                    item[key] = [resolved[value] for value in item[key]]
            if item is not projected_stack:
                item['commits'] = [resolved[value] for value in item['commits']]
                if proposed['integration'].get('strategy') == 'merge-stacks':
                    observed = direct[f"refs/heads/{item['branch']}"]
                    if not observed:
                        raise SyncwheelError(f'integration projection: stack {stack_id} source ref is missing')
                    overrides[stack_id] = observed
        try:
            self.replay.materialize_integration_projection(proposed, overrides)
        except SyncwheelError as exc:
            raise SyncwheelError(f'proposed integration projection failed before capture:\n{exc}') from exc

        rebuild = {
            'stack': args.stack, 'branch': branch, 'base': stack['base'],
            'integration_branch': stack.get('integration_branch'),
            'before_tip': before, 'after_tip': tip, 'replay_mode': 'ephemeral',
        }
        context = {
            'stack': args.stack, 'branch': branch, 'added_commits': added_commits,
            'actor': self.repository.governed_worktree_owner(),
        }
        path = destination['worktree_path']
        if path and before != tip:
            raise SyncwheelError(
                f'capture would move stack branch {branch!r} checked out at {path}; '
                'inspect and preserve that checkout before retrying with the stack branch unchecked out'
            )

        def require_current_capture_inputs():
            if inputs != self.observe_capture_inputs(original, require_stack(original, args.stack), args.specs):
                raise SyncwheelError('capture projection inputs changed; inspect the refs and retry')
            if destination != self.projections.checkout_reset_destination_lease(repo_root, branch):
                raise SyncwheelError('capture checkout changed after projection; inspect it and retry')
            retained = self.projections.verify_retained_captured_worktrees(repo_root, manifest, args)
            if any(item['tip'] != tip for item in retained):
                raise SyncwheelError('proposed capture tip differs from a retained lane; inspect the lane before retrying')

        require_current_capture_inputs()
        backup = None
        installed = False
        try:
            with self.control.prepare_capture_metadata(repo_root, manifest_path, manifest, rebuild, context):
                require_current_capture_inputs()
                backup = self.publish_capture_tip(branch, before, tip, direct)
                installed = before != tip
        except (OSError, SyncwheelError) as failure:
            if installed:
                # Preserve the new source and backup if metadata rollback itself
                # encountered a foreign write and the surviving recipe is unknown.
                if isinstance(failure, MetadataRollbackIncomplete):
                    raise SyncwheelError(
                        f'{failure}; capture source and backup kept for inspection: {branch}, {backup}'
                    ) from failure
                try:
                    self.rollback_capture_tip(branch, before, tip, backup)
                except SyncwheelError as error:
                    raise SyncwheelError(
                        f'{failure}; capture ref rollback refused ({error}); inspect {branch} and {backup}'
                    ) from failure
            if isinstance(failure, OSError):
                raise SyncwheelError(f'stack capture-integration write failed: {failure}') from failure
            raise
        if getattr(args, 'retain_captured_worktrees', None) is None:
            try:
                self.worktrees.capture_governed_worktrees_for_stack(
                    manifest, args.stack, manifest_path=manifest_path,
                )
            except (SyncwheelError, OSError) as exc:
                print(f'warning: stack capture-integration applied; lane cleanup failed: {exc}',
                      file=sys.stderr)
        print(f"{args.stack}: captured {len(added_commits)} integration commit(s)")
        return 0


    def command_stack_rebuild(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        self.complete_pending_promote_intents(
            manifest,
            manifest_path,
            apply=not args.dry_run,
            only_stacks={args.stack},
        )
        stack = require_stack(manifest, args.stack)
        mode, worktree = self.replay.select_replay_mode(
            manifest,
            args,
            stack['branch'],
            self.projections.resolve_stack_rebuild_location(repo_root, manifest, stack, args),
        )
        self.rebuild_stack_from_manifest(
            manifest,
            manifest_path,
            stack,
            dry_run=args.dry_run,
            mode=mode,
            worktree=worktree,
        )
        return 0


    def command_stack_push(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        stack = require_stack(manifest, args.stack)
        adopted = validate_stack_adoption(stack)
        if adopted and self.repository.ref_tip(repo_root, stack['branch']) != adopted['tip']:
            raise SyncwheelError('adopted stack source changed; inspect and adopt the exact intended tip')
        retained = self.projections.verify_retained_captured_worktrees(repo_root, manifest, args)
        for item in retained:
            print(f"preserving captured worktree {item['lane']['id']}: {item['tip']}; "
                  f"{(args.retain_captured_worktrees or 'active publication source').strip()}")
        delivery = [item['candidate_delivery'] for item in retained if 'candidate_delivery' in item]
        if delivery and not args.dry_run:
            self.ledger.append_ledger_event(
                repo_root, 'preserved_candidate_delivery_prepared',
                {'stack': stack['id'], 'reason': args.retain_captured_worktrees.strip(),
                 'proofs': delivery, 'actor': self.repository.governed_worktree_owner()}, manifest_path,
            )
        self.complete_pending_promote_intents(
            manifest,
            manifest_path,
            apply=not args.dry_run,
            only_stacks={args.stack},
        )
        if coordination_is_active(manifest):
            recovery_state = getattr(args, '_control_manifest_recovery_state', None)
            if recovery_state is None:
                recovery_state = {}
            if not args.dry_run and not hasattr(args, '_control_manifest_recovery_state'):
                manifest = self.control.recover_incomplete_control_manifest_persistence(
                    repo_root,
                    manifest_path,
                    manifest,
                    recovery_state=recovery_state,
                )
            stack = require_stack(manifest, args.stack)
            refusal = draft_push_refusal(
                manifest, stack, stack_push_remote(manifest, stack, args.remote)
            )
            if refusal:
                raise SyncwheelError(refusal)
            config = coordination_config(manifest)
            coordinated_push_remote(args, config)
            if not args.dry_run:
                replay_tip = self.repository.ref_tip(repo_root, manifest['integration']['branch'])
                self.control.restore_control_manifest_after_integration_rebuild(
                    repo_root,
                    manifest_path,
                    manifest,
                    replay_tip,
                    'control',
                    reason=f"publish {stack['id']} control manifest",
                    command='syncwheel stack push',
                )
            # Control persistence is local housekeeping, not publication authority
            # over the integration projection or any other stack.
            changed_refs = {
                f"refs/heads/{stack['branch']}": (adopted['tip'] if adopted else
                    retained[0]['target_tip'] if retained else self.repository.ref_tip(repo_root, stack['branch']))
            }
            try:
                publication_operation = (
                    None if args.dry_run else self.intents.begin_coordination_publication(
                        manifest,
                        manifest_path,
                        changed_refs,
                        f"stack:{stack['id']}",
                        'partial',
                        identity_changed_refs=changed_refs,
                    )
                )
                if publication_operation:
                    changed_refs = publication_operation['changed_refs']
                self.projections.verify_retained_captured_worktrees(repo_root, manifest, args)
                result = (publication_operation or {}).get(VERIFIED_STACK_PUBLICATION_RECOVERY)
                if result is None:
                    result = self.publication.coordinated_publish(
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
                failure = self.intents.coordinated_publish_command_failure(
                    config, exc, f"stack push {stack['id']}"
                )
                if failure is None:
                    raise
                raise failure from exc
            if not args.dry_run:
                self.ledger.append_ledger_event(
                    repo_root,
                    'stack_pushed',
                    {
                        'stack': stack['id'],
                        'branch': stack['branch'],
                        'remote': config['remote'],
                        'tip': self.repository.ref_tip(repo_root, stack['branch']),
                        'coordination_state': result.get('state_tip'),
                        'coordination_status': result['status'],
                        'operation_token': publication_operation['operation_token'],
                        'recovered': bool(result.get('recovered')),
                        **({'preserved_candidate_delivery': delivery} if delivery else {}),
                        'actor': self.repository.governed_worktree_owner(),
                    },
                    manifest_path,
                )
                self.intents.complete_coordination_publication(
                    manifest_path, publication_operation, result
                )
            return 0
        stack = require_stack(manifest, args.stack)
        refusal = draft_push_refusal(manifest, stack, stack_push_remote(manifest, stack, args.remote))
        if refusal:
            raise SyncwheelError(refusal)
        remote = stack_push_remote(manifest, stack, args.remote)
        push_args = push_args_with_options(args)
        retained = self.projections.verify_retained_captured_worktrees(repo_root, manifest, args)
        publication_tip = adopted['tip'] if adopted else (retained[0]['target_tip'] if retained else None)
        refspec = (f"{publication_tip}:refs/heads/{stack['branch']}"
                   if publication_tip else stack['branch'])
        command = ['git', 'push', *push_args, remote, refspec]
        if delivery:
            # An exact lease is a CAS, not authority to replace history: the
            # frozen proof above already requires remote_tip <= candidate.
            # Ref deletion or any concurrent advance must fail atomically.
            proof = delivery[0]
            command = ['git', 'push',
                       f"--force-with-lease={proof['remote_ref']}:{proof['remote_tip']}",
                       remote, refspec]
        if args.dry_run:
            print(quoted(command))
            return 0
        if self.repository.forced_push_requested(push_args):
            local_tip = publication_tip or self.repository.ref_tip(repo_root, stack['branch'])
            leases = self.repository.legacy_stack_push_lease(
                repo_root, stack, remote, local_tip, push_args, manifest_path,
            )
            command = ['git', 'push', *leases, *self.repository.without_force_push_args(push_args), remote, refspec]
        self.repository.run_authorized_push(
            repo_root, command, remote, [f"refs/heads/{stack['branch']}"], manifest=manifest,
        )
        print(quoted(command))
        self.ledger.append_ledger_event(
            repo_root,
            'stack_pushed',
            {
                'stack': stack['id'],
                'branch': stack['branch'],
                'remote': remote,
                'tip': publication_tip or self.repository.ref_tip(repo_root, stack['branch']),
                **({'preserved_candidate_delivery': delivery} if delivery else {}),
                'actor': self.repository.governed_worktree_owner(),
            },
            manifest_path,
        )
        return 0


    def command_stack_git(self, args, manifest, manifest_path):
        repo_root = self.repository.root
        stack = require_stack(manifest, args.stack)
        worktree = self.repository.resolve_git_worktree(
            repo_root, stack['branch'], manifest, args.worktree, args.auto_worktree
        )
        git_args = passthrough_args(args.git_args)
        if not git_args:
            raise SyncwheelError('stack git requires git arguments after --')
        result = self.repository.run(['git', *git_args], cwd=worktree, check=False)
        if result.stdout:
            print(result.stdout, end='')
        if result.stderr:
            print(result.stderr, end='', file=sys.stderr)
        return result.returncode
