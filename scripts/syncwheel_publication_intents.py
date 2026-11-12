"""Durable coordination publication intents and exact remote landing evidence.

The service owns token/terminal lifecycle and bounded state/claim proofs. Git,
ledger, state observations and publication locking cross explicit ports.
"""

import copy
import json
from pathlib import Path
import uuid
from typing import Protocol

from syncwheel_errors import SyncwheelError, CoordinationRemoteRejection
from syncwheel_manifest import canonical_json_digest, coordination_config, require_stack
from syncwheel_coordination import (
    coordination_claim_ref, coordination_tombstone_ref,
    coordination_state_operation_manifest_digest,
)
from syncwheel_coordination_state import COORDINATION_STATE_FILE

COORDINATION_CLAIM_FILE = 'claim.json'

EXPECTED_COORDINATION_STATE_UNSET = object()

VERIFIED_STACK_PUBLICATION_RECOVERY = object()

COORDINATION_CLAIM_HISTORY_SCAN_LIMIT = 500


def coordination_scope_remedy(scope):
    scope = str(scope or '')
    prefix, separator, identifier = scope.partition(':')
    if separator and identifier:
        return {
            'stack': f'stack push {identifier}',
            'promote': f'stack promote {identifier}',
            'demote': f'stack demote {identifier}',
            'close': f'stack close {identifier} --force',
        }.get(prefix)
    if scope == 'integration':
        return 'int push'
    if scope in {'full', 'partial'}:
        return 'reconcile --apply --push'
    return None


def pending_coordination_publication_remedy(scopes):
    remedies = list(dict.fromkeys(
        remedy for remedy in (coordination_scope_remedy(scope) for scope in scopes)
        if remedy
    ))
    lines = ['another coordinated publication intent is pending: ' + ', '.join(scopes)]
    if remedies:
        lines.append('complete it first:')
        lines.extend(f'  syncwheel {remedy}' for remedy in remedies)
    else:
        lines.append('retry or reconcile that exact command first')
    return SyncwheelError('\n'.join(lines))


def coordinated_publish_retry_hint(scope):
    remedy = coordination_scope_remedy(scope)
    if not remedy:
        return '; nothing was published, rerun the interrupted command'
    return f'\nNothing was published. Retry:\n  syncwheel {remedy}'


def coordinated_publish_remote_failure(remedy):
    return SyncwheelError(
        'coordinated publish could not inspect the coordination remote; '
        f'restore remote access, then retry:\n  syncwheel {remedy}'
    )


def coordination_operation_touched_refs(operation):
    refs = list(operation.get('changed_refs') or {})
    tombstone = operation.get('tombstone')
    if tombstone:
        refs.append(coordination_tombstone_ref(tombstone))
    refs.extend(operation.get('claimed_refs') or [])
    return list(dict.fromkeys(refs))


def coordination_claim_proves_operation(claim, config, source_ref, operation):
    """One claim's share of the claim proof: identity, scope, and the ref set.

    A claim written before 0.43 records neither scope nor ref set, so it can
    never prove an operation on its own.
    """
    return (
        claim.get('coordination_id') == config['id']
        and claim.get('source_ref') == source_ref
        and claim.get('operation_token') == operation.get('operation_token')
        and claim.get('publication_scope') == operation.get('scope')
        and claim.get('changed_refs') == sorted(operation.get('changed_refs') or {})
    )


def coordination_state_document_matches_operation(state, operation):
    """The state commit declares this operation's token, scope, refs and digest."""
    if not isinstance(state, dict):
        return False
    token = operation.get('operation_token')
    if not token:
        return False
    return (
        state.get('operation_token') == token
        and state.get('publication_scope') == operation.get('scope')
        and state.get('projection_status') == operation.get('projection_status')
        and coordination_state_operation_manifest_digest(state)
        == operation.get('manifest_digest')
        and state.get('changed_refs') == dict(
            sorted((operation.get('changed_refs') or {}).items())
        )
    )


def publication_intent_owner_recovers(intent):
    """Promote finishes its own landed intent: it still owes a manifest save."""
    return str(intent.get('scope') or '').startswith('promote:')


def stack_publication_source_refs(manifest, scope):
    """A stack publisher owns its branch, never the integration projection."""
    if not str(scope).startswith('stack:'):
        return None
    stack = require_stack(manifest, scope.partition(':')[2])
    if stack['branch'] == manifest['integration']['branch']:
        raise SyncwheelError('stack publication cannot target the integration branch; use syncwheel int push')
    return {f"refs/heads/{stack['branch']}"}


def require_stack_publication_refs(manifest, scope, changed_refs):
    allowed = stack_publication_source_refs(manifest, scope)
    unexpected = set(changed_refs) - allowed if allowed is not None else set()
    if unexpected:
        raise SyncwheelError(
            'stack publication cannot publish unrelated source refs: '
            + ', '.join(sorted(unexpected))
            + '; publish integration explicitly with syncwheel int push'
        )


class PublicationIntentRepository(Protocol):
    root: Path
    def git(self, repo_root, *args, **kwargs): ...
    def commit_exists(self, repo_root, ref): ...
    def branch_exists(self, repo_root, branch): ...
    def ref_tip(self, repo_root, ref): ...


class PublicationIntentLedger(Protocol):
    def load_ledger_events(self, repo_root, manifest_path=None): ...
    def append_ledger_event(self, repo_root, event_type, payload, manifest_path=None, **kwargs): ...


class PublicationIntentStates(Protocol):
    def read_remote_coordination_state(self, repo_root, config, fetch=True, local_manifest_version=None): ...
    def remote_ref_tips(self, repo_root, remote, refs): ...


class PublicationIntentTransactions(Protocol):
    def coordination_publication_lock(self, repo_root): ...
    def coordination_publication_owner(self, repo_root): ...
    def coordination_operation_manifest_digest(self, manifest, repo_root=None): ...


class PublicationIntentEvidence(Protocol):
    def pending_coordination_publications(self, repo_root, manifest_path=None): ...
    def coordinated_operation_landed(self, repo_root, config, observed, operation, *, claims_fallback=False): ...


class PublicationIntents:
    def __init__(self, repository: PublicationIntentRepository,
                 ledger: PublicationIntentLedger, states: PublicationIntentStates,
                 transactions: PublicationIntentTransactions,
                 evidence: PublicationIntentEvidence):
        self.repository = repository
        self.ledger = ledger
        self.states = states
        self.transactions = transactions
        self.evidence = evidence

    def coordination_publication_identity(
        self,
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
        """Build the operation identity and its fingerprint.

        ``identity_changed_refs``, when given, replaces ``changed_refs`` in the
        fingerprint only: a control-manifest refresh that housekeeping may or may
        not need this attempt is not part of what the operation itself declares
        it will change, so it must not make the fingerprint differ between a
        first attempt and its retry. The full ``changed_refs`` is still what gets
        recorded on the intent (see ``begin_coordination_publication``); the
        landing proof's L3 keeps comparing that recorded value, unaffected by
        this narrower fingerprint slice.
        """
        repo_root = self.repository.root
        published_manifest = publication_manifest or manifest
        stable_tombstone = copy.deepcopy(tombstone) if tombstone else None
        if stable_tombstone:
            stable_tombstone.pop('closed_at', None)
        fingerprint_refs = (
            changed_refs if identity_changed_refs is None else identity_changed_refs
        )
        identity = {
            'coordination_id': coordination_config(manifest)['id'],
            'scope': scope,
            'projection_status': projection_status,
            'manifest_digest': self.transactions.coordination_operation_manifest_digest(
                published_manifest, repo_root
            ),
            'changed_refs': dict(sorted(changed_refs.items())),
            'tombstone': stable_tombstone,
            'rename': rename,
            'state_transition': state_transition,
        }
        fingerprint_identity = {
            **identity,
            'changed_refs': dict(sorted(fingerprint_refs.items())),
        }
        return identity, canonical_json_digest(fingerprint_identity)

    def pending_coordination_publications(self, manifest_path):
        repo_root = self.repository.root
        events = self.ledger.load_ledger_events(repo_root, manifest_path)
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

    def pending_coordination_publication(self, manifest_path, fingerprint):
        repo_root = self.repository.root
        pending = self.evidence.pending_coordination_publications(repo_root, manifest_path)
        matching = [item for item in pending if item.get('fingerprint') == fingerprint]
        if matching:
            return matching[-1]
        if pending:
            raise pending_coordination_publication_remedy(
                sorted({str(item.get('scope')) for item in pending})
            )
        return None

    def pending_coordination_publication_for_scope(
        self, manifest_path, scope
    ):
        repo_root = self.repository.root
        matching = [
            item for item in self.evidence.pending_coordination_publications(repo_root, manifest_path)
            if item.get('scope') == scope
        ]
        if len(matching) > 1:
            raise SyncwheelError(
                f'multiple coordinated publication intents are pending for {scope}; '
                'inspect the ledger before retrying'
            )
        return matching[-1] if matching else None

    def coordination_remote_is_reachable(self, remote):
        repo_root = self.repository.root
        return self.repository.git(
            repo_root, 'ls-remote', '--quiet', remote, 'HEAD', check=False
        ).returncode == 0

    def coordinated_publish_command_failure(self, config, exc, remedy):
        """Turn a raw publication failure into an operator error, or None to reraise."""
        repo_root = self.repository.root
        if isinstance(exc, CoordinationRemoteRejection):
            return SyncwheelError(
                f'{exc}\nNothing was published. Resolve the rejection on the remote, '
                f'then retry:\n  syncwheel {remedy}'
            )
        if not config or self.coordination_remote_is_reachable(config['remote']):
            return None
        return coordinated_publish_remote_failure(remedy)

    def coordination_state_commits_since(self, tip, since_tip):
        repo_root = self.repository.root
        if not tip:
            return []
        if not since_tip:
            rev_range = tip
        elif self.repository.git(
            repo_root, 'merge-base', '--is-ancestor', since_tip, tip, check=False
        ).returncode == 0:
            rev_range = f'{since_tip}..{tip}'
        else:
            return []
        result = self.repository.git(repo_root, 'rev-list', rev_range, check=False)
        if result.returncode != 0:
            return []
        return result.stdout.split()

    def coordination_state_document_from_commit(self, commit):
        repo_root = self.repository.root
        result = self.repository.git(
            repo_root, 'show', f'{commit}:{COORDINATION_STATE_FILE}', check=False
        )
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def coordination_commit_operation_token(self, commit):
        repo_root = self.repository.root
        token = (self.coordination_state_document_from_commit(commit) or {}).get(
            'operation_token'
        )
        return token if isinstance(token, str) else None

    def coordination_claim_history_matches(
        self, config, claim_tip, source_ref, predicate
    ):
        """Whether any claim in the ancestry of a claim tip satisfies ``predicate``.

        A later publication of the same source ref parents its claim on this one, so
        an older claim stays reachable even when the claim tip has moved on. Only
        the most recent COORDINATION_CLAIM_HISTORY_SCAN_LIMIT claims are read.
        """
        repo_root = self.repository.root
        if not claim_tip:
            return False
        if not self.repository.commit_exists(repo_root, claim_tip):
            self.repository.git(
                repo_root, 'fetch', '--quiet', config['remote'],
                coordination_claim_ref(source_ref), check=False,
            )
        if not self.repository.commit_exists(repo_root, claim_tip):
            return False
        listed = self.repository.git(
            repo_root, 'rev-list',
            f'--max-count={COORDINATION_CLAIM_HISTORY_SCAN_LIMIT}', claim_tip,
            check=False,
        )
        if listed.returncode != 0:
            return False
        for commit in listed.stdout.split():
            try:
                claim = self.coordination_claim_from_commit(commit)
            except SyncwheelError:
                continue
            if predicate(claim):
                return True
        return False

    def coordination_claim_history_carries_token(
        self, config, claim_tip, source_ref, operation_token
    ):
        repo_root = self.repository.root
        if not operation_token:
            return False
        return self.coordination_claim_history_matches(
            config,
            claim_tip,
            source_ref,
            lambda claim: (
                claim.get('coordination_id') == config['id']
                and claim.get('source_ref') == source_ref
                and claim.get('operation_token') == operation_token
            ),
        )

    def coordination_state_claims_carry_operation(self, config, state, operation):
        """Every touched source ref carries this operation's token in its claim history."""
        repo_root = self.repository.root
        claims = state.get('claims') or {}
        for source_ref in coordination_operation_touched_refs(operation):
            if not self.coordination_claim_history_carries_token(
                config,
                claims.get(source_ref),
                source_ref,
                operation.get('operation_token'),
            ):
                return False
        return True

    def coordination_remote_claims_prove_operation(self, config, operation):
        """Claim-side landing evidence for a state chain that no longer carries it.

        Every changed ref, and no other, must carry a claim declaring this exact
        operation: its token, its scope and its whole ref set. A claim that carries
        the token under another scope or another ref set proves a different
        operation, so it proves nothing about this one.
        """
        repo_root = self.repository.root
        changed = sorted(operation.get('changed_refs') or {})
        if not changed or not operation.get('operation_token'):
            return False
        claim_refs = {ref: coordination_claim_ref(ref) for ref in changed}
        observed = self.states.remote_ref_tips(repo_root, config['remote'], claim_refs.values())
        for source_ref, claim_ref in claim_refs.items():
            if not self.coordination_claim_history_matches(
                config,
                observed.get(claim_ref),
                source_ref,
                lambda claim, source_ref=source_ref: coordination_claim_proves_operation(
                    claim, config, source_ref, operation
                ),
            ):
                return False
        return True

    def coordinated_operation_landing_evidence(
        self, config, observed, operation, *, claims_fallback=False
    ):
        """Prove that this exact operation reached the remote, and say how.

        The state proof, the only form a fresh publication may use, needs a state
        commit inside the recorded window declaring this operation's token, scope,
        changed refs, projection status and manifest digest, with the claim of every
        touched source ref carrying the same token. Ref tips are not proof: another
        clone can legitimately move a published ref, or push the same tip without
        this operation ever landing.

        The claim proof, available to the recovery paths only, needs every changed
        ref to carry a claim declaring this operation's token, scope and whole ref
        set. It has no window, which is why it survives a state ref rewritten
        backwards, and it excludes the manifest digest, which is not recomputable
        from a claim.
        """
        repo_root = self.repository.root
        if not operation.get('operation_token'):
            return None
        tip = (observed or {}).get('tip')
        expected_tip = operation.get('expected_coordination_state_tip')
        if tip and expected_tip:
            for commit in self.coordination_state_commits_since(tip, expected_tip):
                document = self.coordination_state_document_from_commit(commit)
                if coordination_state_document_matches_operation(
                    document, operation
                ) and self.coordination_state_claims_carry_operation(
                    config, document, operation
                ):
                    return 'state'
        if claims_fallback and self.coordination_remote_claims_prove_operation(
            config, operation
        ):
            return 'claims'
        return None

    def coordinated_operation_landed(
        self, config, observed, operation, *, claims_fallback=False
    ):
        repo_root = self.repository.root
        return self.coordinated_operation_landing_evidence(
            config, observed, operation, claims_fallback=claims_fallback
        ) is not None

    def abandoned_publication_rename_is_restorable(self, config, intent):
        """A rename is undone only when the remote proves the promotion never landed.

        The destination ref present on the remote, or a claim carrying this token,
        is evidence of a landed operation whose branch layout must stand.
        """
        repo_root = self.repository.root
        rename = intent.get('rename') or {}
        to_branch = rename.get('to_branch')
        if not to_branch:
            return False
        destination_ref = f'refs/heads/{to_branch}'
        observed = self.states.remote_ref_tips(
            repo_root,
            config['remote'],
            [destination_ref, coordination_claim_ref(destination_ref)],
        )
        if observed.get(destination_ref):
            return False
        return not self.coordination_claim_history_carries_token(
            config,
            observed.get(coordination_claim_ref(destination_ref)),
            destination_ref,
            intent.get('operation_token'),
        )

    def restore_abandoned_publication_rename(self, config, intent):
        repo_root = self.repository.root
        rename = intent.get('rename') or {}
        from_branch = rename.get('from_branch')
        to_branch = rename.get('to_branch')
        if not from_branch or not to_branch or from_branch == to_branch:
            return
        if self.repository.branch_exists(repo_root, from_branch) or not self.repository.branch_exists(repo_root, to_branch):
            return
        if not self.abandoned_publication_rename_is_restorable(config, intent):
            return
        self.repository.git(repo_root, 'branch', '-m', to_branch, from_branch, check=False)

    def resolve_pending_coordination_publications(
        self, manifest, manifest_path, *, adopt_tokens=()
    ):
        """Terminalize publish intents no live process in this clone still owns.

        The publication lock, not the intent payload, decides what is in flight: a
        process that holds it runs no other publication cycle in this clone, and a
        dead owner released it by kernel action. An intent whose operation is proved
        landed completes as ``already_published``; one that never reached the remote
        is abandoned, because it can never satisfy its own leases again and leaving
        it pending freezes every later publication. Tokens in ``adopt_tokens``
        belong to the running command, which finishes or renews them itself.
        """
        repo_root = self.repository.root
        config = coordination_config(manifest)
        if not config:
            return None
        if not self.evidence.pending_coordination_publications(repo_root, manifest_path):
            return None
        with self.transactions.coordination_publication_lock(repo_root):
            pending = self.evidence.pending_coordination_publications(repo_root, manifest_path)
            if not pending:
                return None
            observed = self.states.read_remote_coordination_state(
                repo_root, config, fetch=True, local_manifest_version=manifest['version']
            )
            for intent in pending:
                token = intent.get('operation_token')
                expected_tip = intent.get('expected_coordination_state_tip')
                if self.evidence.coordinated_operation_landed(
                    repo_root, config, observed, intent, claims_fallback=True
                ):
                    if token in adopt_tokens or publication_intent_owner_recovers(intent):
                        continue
                    self.ledger.append_ledger_event(
                        repo_root,
                        'coordination_publish_completed',
                        {
                            'operation_token': token,
                            'fingerprint': intent.get('fingerprint'),
                            'scope': intent.get('scope'),
                            'coordination_state': observed['tip'],
                            'coordination_status': 'already_published',
                            'recovered': True,
                            'legacy_intent': not expected_tip,
                        },
                        manifest_path,
                    )
                    continue
                if not expected_tip:
                    # A pre-0.43 intent records no reviewed tip, so nothing about it
                    # was overtaken; it is replaced, not superseded.
                    reason = 'not_landed'
                elif observed['tip'] != expected_tip:
                    reason = 'superseded'
                elif token in adopt_tokens:
                    continue
                else:
                    reason = 'not_landed'
                self.restore_abandoned_publication_rename(config, intent)
                self.ledger.append_ledger_event(
                    repo_root,
                    'coordination_publish_abandoned',
                    {
                        'operation_token': token,
                        'fingerprint': intent.get('fingerprint'),
                        'scope': intent.get('scope'),
                        'reason': reason,
                        'status': reason,
                        'coordination_state': observed['tip'],
                        'owner': intent.get('owner'),
                        'legacy_intent': not intent.get('owner'),
                    },
                    manifest_path,
                )
            return observed

    def renew_coordination_publication(self, manifest, manifest_path, pending):
        with self.transactions.coordination_publication_lock(self.repository.root):
            return self._renew_coordination_publication_locked(manifest, manifest_path, pending)

    def _renew_coordination_publication_locked(self, manifest, manifest_path, pending):
        """Give a pending intent a reviewable plan again.

        A recorded state tip that has been overtaken can never satisfy its own
        leases, so the intent is abandoned and replaced by an equivalent one
        observed against the current state. An intent whose result already landed
        keeps its token so the publish recovers instead of republishing.
        """
        repo_root = self.repository.root
        config = coordination_config(manifest)
        observed = self.states.read_remote_coordination_state(
            repo_root, config, fetch=True, local_manifest_version=manifest['version']
        )
        if observed['tip'] == pending.get('expected_coordination_state_tip'):
            return pending
        if self.evidence.coordinated_operation_landed(
            repo_root, config, observed, pending, claims_fallback=True
        ):
            return pending
        self.ledger.append_ledger_event(
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
            'owner': self.transactions.coordination_publication_owner(repo_root),
        }
        self.ledger.append_ledger_event(
            repo_root, 'coordination_publish_intent', payload, manifest_path
        )
        return payload

    def pending_coordination_publication_after_resolution(
        self, manifest, manifest_path, fingerprint
    ):
        repo_root = self.repository.root
        adopt = {
            intent.get('operation_token')
            for intent in self.evidence.pending_coordination_publications(repo_root, manifest_path)
            if intent.get('fingerprint') == fingerprint
        }
        self.resolve_pending_coordination_publications(
            manifest, manifest_path, adopt_tokens=adopt
        )
        return self.pending_coordination_publication(manifest_path, fingerprint)

    def begin_coordination_publication(
        self,
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
        repo_root = self.repository.root
        config = coordination_config(manifest)
        identity, fingerprint = self.coordination_publication_identity(
            manifest,
            changed_refs,
            scope,
            projection_status,
            tombstone=tombstone,
            rename=rename,
            state_transition=state_transition,
            publication_manifest=publication_manifest,
            identity_changed_refs=identity_changed_refs,
        )
        with self.transactions.coordination_publication_lock(repo_root):
            existing = self.pending_coordination_publication_after_resolution(
                manifest, manifest_path, fingerprint
            )
            if existing:
                allowed = stack_publication_source_refs(manifest, scope)
                extra = set(existing['changed_refs']) - allowed if allowed is not None else set()
                if not extra:
                    return {**existing, 'retry': True}
                observed = self.states.read_remote_coordination_state(
                    repo_root, config, fetch=True, local_manifest_version=manifest['version']
                )
                if self.evidence.coordinated_operation_landed(
                    repo_root, config, observed, existing, claims_fallback=True
                ):
                    # This in-memory result is bound to the exact pending intent.
                    # It never enters a fresh publication's state-only proof path.
                    return {
                        **existing, 'retry': True,
                        VERIFIED_STACK_PUBLICATION_RECOVERY: {
                            'status': 'already_published',
                            'state_tip': observed['tip'],
                            'recovered': True,
                        },
                    }
                self.ledger.append_ledger_event(
                    repo_root, 'coordination_publish_abandoned',
                    {
                        'operation_token': existing['operation_token'],
                        'fingerprint': existing['fingerprint'],
                        'scope': existing['scope'],
                        'reason': 'not_landed',
                        'status': 'not_landed',
                        'coordination_state': observed['tip'],
                        'owner': existing.get('owner'),
                    },
                    manifest_path,
                )
            require_stack_publication_refs(manifest, scope, changed_refs)
            observed = self.states.read_remote_coordination_state(
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
                'owner': self.transactions.coordination_publication_owner(repo_root),
            }
            self.ledger.append_ledger_event(
                repo_root, 'coordination_publish_intent', payload, manifest_path
            )
            return {**payload, 'retry': False}

    def complete_coordination_publication(
        self, manifest_path, operation, result
    ):
        repo_root = self.repository.root
        self.ledger.append_ledger_event(
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

    def coordination_claim_from_commit(self, commit):
        repo_root = self.repository.root
        result = self.repository.git(repo_root, 'show', f'{commit}:{COORDINATION_CLAIM_FILE}', check=False)
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
        if 'publication_scope' in claim and not isinstance(claim['publication_scope'], str):
            raise SyncwheelError(f'invalid coordination claim publication scope at {commit}')
        if 'changed_refs' in claim and (
            not isinstance(claim['changed_refs'], list)
            or any(not isinstance(value, str) for value in claim['changed_refs'])
        ):
            raise SyncwheelError(f'invalid coordination claim changed refs at {commit}')
        if 'closed' in claim and not isinstance(claim['closed'], bool):
            raise SyncwheelError(f'invalid coordination claim closed flag at {commit}')
        return claim

    def fetch_coordination_claim(self, remote, claim_ref, tip):
        repo_root = self.repository.root
        if not tip:
            return None
        result = self.repository.git(repo_root, 'fetch', '--quiet', remote, claim_ref, check=False)
        if result.returncode != 0 or self.repository.ref_tip(repo_root, 'FETCH_HEAD') != tip:
            raise SyncwheelError(f'coordination claim changed while fetching: {claim_ref}')
        return self.coordination_claim_from_commit('FETCH_HEAD')
