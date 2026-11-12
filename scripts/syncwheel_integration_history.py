"""Integration history proofs and lease-bound reconciliation.

Durable provenance, checkout projections and manifest persistence are composed
services. Historical delivery and close evidence stay separate from ref/checkout
transitions, with repository and coordination observations supplied explicitly.
"""

import contextlib
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid
from typing import Protocol

import syncwheel_manifest_store as _manifest_storage
from syncwheel_contracts import INTEGRATION_STRATEGIES
from syncwheel_manifest_store import ManifestStore
from syncwheel_provenance import ProvenanceStore
from syncwheel_projection import ProjectionService

from syncwheel_coordination import (
    coordination_tombstone_ref,
)
from syncwheel_coordination_state import (
    COORDINATION_STATE_DIGEST_FORM_ORPHANED,
)
from syncwheel_errors import (
    IntegrationReconciliationLeaseDrift,
    SyncwheelError,
)
from syncwheel_ledger import (
    manifest_stack_history_summary,
)
from syncwheel_manifest import (
    bound_merge_stack_resolution_commits,
    canonical_json_digest,
    coordination_config,
    coordination_is_active,
    integration_composition_digest,
    manifest_digest,
    normalize_manifest_data,
    stack_delivery_identity,
    stack_integration_base_commits,
    stack_integration_commits,
    stack_integration_only_commits,
    stack_map,
    syncwheel_worktree_root,
)
from syncwheel_projection import (
    checkout_path_fingerprint,
    checkout_path_observation,
    regular_checkout_path_bytes,
)
from syncwheel_provenance import (
    apply_derived_provenance_overrides,
    projected_derived_provenance_store,
)


class IntegrationHistoryRepository(Protocol):
    def branch_contains(self, *args, **kwargs): ...
    def commit_changed_files(self, *args, **kwargs): ...
    def commit_exists(self, *args, **kwargs): ...
    def commit_first_parent(self, *args, **kwargs): ...
    def commit_full_sha(self, *args, **kwargs): ...
    def commit_parent_count(self, *args, **kwargs): ...
    def commit_patch_id(self, *args, **kwargs): ...
    def commit_patch_ids(self, *args, **kwargs): ...
    def find_worktree_for_branch(self, *args, **kwargs): ...
    def git(self, *args, **kwargs): ...
    def parsed_commit_trailers(self, *args, **kwargs): ...
    def patch_ids_reachable_from_ref(self, *args, **kwargs): ...
    def ref_tip(self, *args, **kwargs): ...
    def ref_tree(self, *args, **kwargs): ...
    def replay_commit_env(self, *args, **kwargs): ...
    def rev_list(self, *args, **kwargs): ...


class IntegrationHistoryCoordination(Protocol):
    def coordination_state_chain_contains(self, *args, **kwargs): ...
    def coordination_state_control_manifest(self, *args, **kwargs): ...
    def coordination_state_from_commit(self, *args, **kwargs): ...
    def coordination_state_manifest_digest_classification(self, *args, **kwargs): ...
    def is_valid_coordination_branch_ref(self, *args, **kwargs): ...
    def verify_coordination_state_manifest_digest(self, *args, **kwargs): ...


class IntegrationHistoryReplay(Protocol):
    def deterministic_stack_projection(self, *args, **kwargs): ...
    def materialize_integration_replay(self, *args, **kwargs): ...


class IntegrationHistoryIgnorePolicy(Protocol):
    def split_historical_syncwheel_managed_gitignore(self, *args, **kwargs): ...
    def split_syncwheel_managed_gitignore(self, *args, **kwargs): ...


class IntegrationHistory:
    def __init__(self, repository: IntegrationHistoryRepository,
                 coordination: IntegrationHistoryCoordination, replay: IntegrationHistoryReplay,
                 ignore_policy: IntegrationHistoryIgnorePolicy, *, control: ManifestStore,
                 provenance: ProvenanceStore, projection: ProjectionService):
        self.repository = repository
        self.coordination = coordination
        self.replay = replay
        self.ignore_policy = ignore_policy
        self.control = control
        self.provenance = provenance
        self.projection = projection


    def integration_reconciliation_source(self, repo_root, manifest_path):
        path = Path(manifest_path)
        return {
            'head': self.repository.ref_tip(repo_root, 'HEAD'),
            'checkout': self.projection.checkout_reset_guard(repo_root),
            'manifest': checkout_path_observation(path.parent, path.name)['fingerprint'],
            'gitignore': checkout_path_fingerprint(repo_root, '.gitignore'),
        }


    def integration_reconciliation_object(self, repo_root, manifest, local_tip, replay_tip, tree, inputs, published_parent=None):
        """Keep the declared replay graph and the prior local history as distinct parents."""
        payload = {'version': 2, 'inputs': inputs, 'manifest_digest': manifest_digest(manifest)}
        if published_parent is not None:
            payload.update(version=3, published_parent=published_parent)
        proof = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        return self.repository.git(
            repo_root, 'commit-tree', tree, '-p', local_tip, '-p', replay_tip,
            *(['-p', published_parent['tip']] if published_parent is not None else []),
            '-m', 'chore: reconcile Syncwheel integration history\n\nSyncwheel-Reconciliation: ' + proof,
            env=self.repository.replay_commit_env(repo_root, replay_tip),
        ).stdout.strip()


    def verify_reconciliation_published_parent(self, repo_root, manifest, local_tip, replay_tip,
                                              published_parent, inputs, observation):
        """Qualify one frozen published history without granting product-tree ownership."""
        if (not isinstance(published_parent, dict)
                or set(published_parent) != {'tip', 'state_tip'}
                or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value)
                       for value in published_parent.values())):
            raise SyncwheelError('integration reconciliation has an invalid published parent')
        tip, state_tip = published_parent['tip'], published_parent['state_tip']
        if (local_tip == replay_tip or self.repository.branch_contains(repo_root, local_tip, tip)
                or self.repository.branch_contains(repo_root, replay_tip, tip)
                or not self.integration_divergence_is_control_only(repo_root, local_tip, tip)):
            raise SyncwheelError('integration reconciliation published parent is not qualified control divergence')
        config = coordination_config(manifest)
        state = self.coordination.coordination_state_from_commit(repo_root, state_tip, config['id'])
        ref = f"refs/heads/{manifest['integration']['branch']}"
        if state['managed_refs'].get(ref) != tip:
            raise SyncwheelError('integration reconciliation published parent differs from its frozen state')
        self.coordination.verify_coordination_state_manifest_digest(repo_root, state, config['remote'])
        self.coordination.coordination_state_chain_contains(repo_root, config['id'], state_tip, observation['state_tip'])
        pinned, _ = self.integration_reconciliation_inputs(repo_root, manifest, inputs)
        shared = self.provenance.shared_derived_provenance_records(repo_root, pinned, state)
        self.integration_reconciliation_history(
            repo_root, pinned, tip, shared, observation=observation,
            delivery_tip=inputs['refs'][0]['tip'], delivery_observation=inputs['refs'][0],
        )


    def integration_reconciliation_inputs(self, repo_root, manifest, inputs):
        expected = [('base', 'integration', manifest['integration']['base'])]
        stacks = stack_map(manifest)
        commits = []
        for stack_id in manifest['integration']['stacks']:
            stack = stacks[stack_id]
            if manifest['integration'].get('strategy', 'cherry-pick') == 'merge-stacks':
                expected.append(('merge-stack', stack_id, stack['branch']))
                commits.extend(self.repository.commit_full_sha(repo_root, value) for value in
                               (bound_merge_stack_resolution_commits(stack) or []))
            else:
                commits.extend(self.repository.commit_full_sha(repo_root, value) for value in stack_integration_base_commits(stack))
            commits.extend(self.repository.commit_full_sha(repo_root, value) for value in stack_integration_only_commits(stack))
        refs = inputs.get('refs')
        if (
            not isinstance(refs, list)
            or any(not isinstance(item, dict) for item in refs)
            or [(item.get('kind'), item.get('id'), item.get('spec')) for item in refs] != expected
            or inputs.get('commits') != commits
            or any(not isinstance(item.get('tip'), str) or not re.fullmatch(r'[0-9a-f]{40,64}', item['tip']) for item in refs)
            or any(item.get('remoteRef') and item.get('remoteTip') != item['tip'] for item in refs)
        ):
            raise SyncwheelError('integration reconciliation replay input binding is invalid or stale')
        pinned = json.loads(json.dumps(manifest))
        pinned['integration']['base'] = inputs['refs'][0]['tip']
        overrides = {item['id']: item['tip'] for item in inputs['refs'] if item['kind'] == 'merge-stack'}
        for stack in pinned['stacks']:
            for key in ('commits', 'integration_commits', 'integration_only_commits'):
                if key in stack:
                    stack[key] = [self.repository.commit_full_sha(repo_root, commit) for commit in stack[key]]
        return pinned, overrides


    def integration_reconciliation_publishing_states(self, repo_root, observation):
        if not observation:
            return
        current = observation['state_tip']
        ref = observation['integration_ref']
        config = observation['config']
        current_head = current
        while current:
            state = self.coordination.coordination_state_from_commit(repo_root, current, config['id'])
            parents = self.repository.git(repo_root, 'show', '-s', '--format=%P', current).stdout.split()
            parent = state.get('parent_state')
            if parents != ([parent] if parent else []):
                raise SyncwheelError('integration reconciliation historical state chain is invalid')
            if current == current_head:
                # The observed head is the present authority, even when it did
                # not publish the integration ref in this operation.
                self.coordination.verify_coordination_state_manifest_digest(repo_root, state, config['remote'])
            published = (state.get('changed_refs') or {}).get(ref)
            if published:
                managed_tip = (state.get('managed_refs') or {}).get(ref)
                if (
                    current != current_head
                    and isinstance(managed_tip, str)
                    and re.fullmatch(r'[0-9a-f]{40,64}', managed_tip)
                    and not self.repository.commit_exists(repo_root, managed_tip)
                    and self.repository.git(
                        repo_root, 'rev-parse', '--is-shallow-repository'
                    ).stdout.strip() == 'false'
                ):
                    # A complete repository cannot use a missing historical tip
                    # as an ancestry or byte-level witness. Older coordination
                    # states can outlive unpublished integration objects after
                    # normal Git pruning, so keep the current authority strict
                    # while ignoring only this unusable historical publisher.
                    print(
                        f'WARNING: ignoring unavailable historical coordination publisher {current}',
                        file=sys.stderr,
                    )
                    current = parent
                    continue
                classification = self.coordination.coordination_state_manifest_digest_classification(
                    repo_root, state, config['remote']
                )
                if classification['form'] == COORDINATION_STATE_DIGEST_FORM_ORPHANED:
                    print(
                        f'WARNING: ignoring orphaned historical coordination publisher {current}',
                        file=sys.stderr,
                    )
                else:
                    yield state
            current = parent


    def integration_reconciliation_published_witness(self, repo_root, manifest, tip, observation):
        for state in self.integration_reconciliation_publishing_states(repo_root, observation):
            if state['changed_refs'].get(observation['integration_ref']) == tip:
                return state
        return None


    def integration_reconciliation_historical_derived(self, repo_root, commit, tip, observation):
        for state in self.integration_reconciliation_publishing_states(repo_root, observation):
            published = state['changed_refs'].get(observation['integration_ref'])
            if not self.repository.branch_contains(repo_root, tip, published) or not self.repository.branch_contains(repo_root, published, commit):
                continue
            records = state['manifest'].get('integration', {}).get('derived_provenance') or []
            records = [record for record in records if record.get('commit') == commit]
            if not records:
                continue
            historical = self.coordination.coordination_state_control_manifest(repo_root, state, observation['config']['remote'])
            records = [record for record in records
                       if record.get('composition_digest') == integration_composition_digest(historical)]
            if records and self.provenance.is_provenance_bound_derived_projection_commit(repo_root, commit, records):
                return True
        return False


    def integration_reconciliation_proof(self, repo_root, commit, manifest_path, observation):
        try:
            return self._integration_reconciliation_proof(repo_root, commit, manifest_path, observation)
        except (KeyError, TypeError, ValueError, IndexError, SyncwheelError):
            return None


    def _integration_reconciliation_proof(self, repo_root, commit, manifest_path, observation):
        values = [value for key, value in self.repository.parsed_commit_trailers(repo_root, commit)
                  if key.casefold() == 'syncwheel-reconciliation']
        if len(values) != 1:
            return None
        try:
            proof = json.loads(values[0])
        except ValueError:
            return None
        if not isinstance(proof, dict) or proof.get('version') not in (2, 3) or not isinstance(proof.get('inputs'), dict):
            return None
        control = self.control.manifest_from_tree(repo_root, commit, _manifest_storage.integration_manifest_path(repo_root))
        parents = self.repository.git(repo_root, 'show', '-s', '--format=%P', commit).stdout.split()
        if control is None or len(parents) != (3 if proof['version'] == 3 else 2) or manifest_digest(control) != proof.get('manifest_digest'):
            return None
        inputs = proof['inputs']
        pinned, overrides = self.integration_reconciliation_inputs(repo_root, control, inputs)
        published_parent = proof.get('published_parent')
        if proof['version'] == 3:
            if not observation or not isinstance(published_parent, dict) or published_parent.get('tip') != parents[2]:
                return None
            self.verify_reconciliation_published_parent(
                repo_root, control, parents[0], parents[1], published_parent, inputs, observation,
            )
        elif published_parent is not None:
            return None
        if self.replay.materialize_integration_replay(repo_root, pinned, overrides) != parents[1]:
            return None
        ignore = self.projection.tree_path_entry(repo_root, commit, '.gitignore')
        previous_ignore = self.projection.tree_path_entry(repo_root, parents[0], '.gitignore')
        if ignore != previous_ignore or (ignore and ignore['mode'] != '100644'):
            return None
        tree = self.projection.materialize_control_manifest_projection_tree(
            repo_root, control, self.repository.ref_tree(repo_root, parents[1]),
            gitignore_bytes=self.projection.tree_path_bytes(repo_root, ignore) if ignore else None,
        )
        if (
            tree != self.repository.ref_tree(repo_root, commit)
            or self.integration_reconciliation_object(repo_root, control, *parents[:2], tree, inputs, published_parent) != commit
        ):
            return None
        events = _manifest_storage.load_control_manifest_events(repo_root, manifest_path)
        intents, receipts = _manifest_storage.control_manifest_operation_records(events, commit)
        local = any(
            event['payload'].get('version') == 2
            and event['payload'].get('inputs') == inputs
            and event['payload'].get('replay_tip') == parents[0]
            and event['payload'].get('detached_replay_tip') == parents[1]
            and event['payload'].get('published_parent') == published_parent
            and receipts.get(key, {}).get('type') == 'manifest_saved'
            and receipts.get(key, {}).get('payload', {}).get('context', {}).get('published_parent') == published_parent
            for key, event in intents.items()
        )
        witness = self.integration_reconciliation_published_witness(
            repo_root, control, commit, observation
        ) if not local else None
        if not local and witness is None:
            return None
        return {'manifest': control, 'inputs': inputs, 'parents': parents, 'witness': witness}


    def integration_reconciliation_provenance(self, manifest, store, shared):
        before = apply_derived_provenance_overrides(shared, store)
        narrowed = self.provenance.narrowed_derived_provenance_records(None, manifest, before)
        after_store = store
        for commit, paths in sorted({(item['commit'], tuple(item['paths'])) for item in narrowed}):
            projected = projected_derived_provenance_store(
                after_store, shared, list(paths), None, expected_commit=commit,
            )
            if projected is not None:
                after_store = projected
        return before, after_store, apply_derived_provenance_overrides(shared, after_store)


    def published_integration_has_no_unique_product(self, repo_root, manifest, tip, observation, delivery_tip):
        """Prove a published integration's final product bytes are already delivered.

        A squash merge can leave historical commits unreachable from main even when
        none of their product content remains unique.  This proof covers the final
        trees only; the current coordination state and ref leases are checked by
        the reconciliation caller before and after materialization.
        """
        if not observation or observation.get('status') != 'current' or not delivery_tip:
            return False
        published_tip = observation.get('published_tip')
        if not published_tip or not self.repository.branch_contains(repo_root, tip, published_tip):
            return False
        control_paths = {'.syncwheel/manifest.json', '.gitignore'}
        for candidate in (published_tip, tip):
            if set(self.projection.integration_tree_changed_paths(repo_root, candidate, delivery_tip)) - control_paths:
                return False
        source_ignore = self.projection.tree_path_entry(repo_root, delivery_tip, '.gitignore')
        root = syncwheel_worktree_root(manifest)
        for candidate in (published_tip, tip):
            candidate_ignore = self.projection.tree_path_entry(repo_root, candidate, '.gitignore')
            if candidate_ignore == source_ignore:
                continue
            if not source_ignore or not candidate_ignore or (
                source_ignore['mode'] != '100644' or candidate_ignore['mode'] != '100644'
            ):
                return False
            try:
                source_text = self.projection.tree_path_bytes(repo_root, source_ignore).decode('utf-8')
                candidate_text = self.projection.tree_path_bytes(repo_root, candidate_ignore).decode('utf-8')
            except UnicodeDecodeError:
                return False
            candidate_split = self.ignore_policy.split_syncwheel_managed_gitignore(candidate_text, root)
            if not candidate_split or candidate_split['managed'] is None:
                return False
            source_split = self.ignore_policy.split_syncwheel_managed_gitignore(source_text, root)
            if source_split:
                if source_split['unmanaged'] != candidate_split['unmanaged']:
                    return False
            elif candidate_text.replace('.syncwheel/manifests/*.local-ledger/\n', '', 1) != source_text:
                # main still carries the immediately preceding managed-block form.
                # Do not discard any other line, including user-owned ignore rules.
                return False
        return True


    def historically_delivered_integration_boundary(self, repo_root, manifest, tip, observation, delivery_tip):
        """Find a witnessed integration prefix whose final product is on main."""
        if not observation or observation.get('status') != 'current' or not delivery_tip:
            return None
        integration_ref = observation.get('integration_ref')
        if not integration_ref:
            return None
        for state in self.integration_reconciliation_publishing_states(repo_root, observation):
            candidate = (state.get('changed_refs') or {}).get(integration_ref)
            if not candidate or not self.repository.branch_contains(repo_root, tip, candidate):
                continue
            if self.published_integration_has_no_unique_product(
                repo_root, manifest, candidate,
                {'status': 'current', 'published_tip': candidate}, delivery_tip,
            ):
                return candidate
        return None


    def historical_stack_has_exact_squash_delivery(
        self, repo_root, stack, delivery_observation, defaults, coordination_remote=None,
    ):
        """Prove an old squash from its immutable source chain and delivery tree."""
        commits = stack.get('commits') or []
        delivery_observation = delivery_observation or {}
        delivery_tip = delivery_observation.get('tip')
        target_remote = (
            stack.get('target_remote')
            or defaults.get('canonical_remote')
            or coordination_remote
        )
        target_branch = stack.get('target_branch') or defaults.get('base_branch')
        if (
            not commits
            or not delivery_tip
            or delivery_observation.get('remote') != target_remote
            or delivery_observation.get('remoteRef') != f'refs/heads/{target_branch}'
            or delivery_observation.get('remoteTip') != delivery_tip
            or any(
                not self.repository.commit_exists(repo_root, commit) for commit in commits
            )
        ):
            return False
        base = self.repository.commit_first_parent(repo_root, commits[0])
        if not base or not self.repository.branch_contains(repo_root, delivery_tip, base):
            return False
        delivery_path = self.repository.git(
            repo_root, 'rev-list', '--first-parent', '--reverse', '--ancestry-path',
            f'{base}..{delivery_tip}',
        ).stdout.split()
        if not delivery_path:
            return False
        delivered = delivery_path[0]
        parents = self.repository.git(repo_root, 'show', '-s', '--format=%P', delivered).stdout.split()
        if parents != [base]:
            return False
        projection = self.replay.deterministic_stack_projection(repo_root, base, commits)
        return (
            projection.get('status') == 'projected'
            and self.repository.ref_tree(repo_root, projection['tip']) == self.repository.ref_tree(repo_root, delivered)
        )


    def historical_managed_tip_has_exact_squash_delivery(
        self, repo_root, managed_tip, stack, delivery_observation, defaults,
        coordination_remote=None,
    ):
        """Prove the complete managed first-parent product was squash-delivered."""
        delivery_tip = (delivery_observation or {}).get('tip')
        if (
            not managed_tip
            or not delivery_tip
            or not self.repository.commit_exists(repo_root, managed_tip)
            or not self.repository.commit_exists(repo_root, delivery_tip)
        ):
            return False
        bases = self.repository.git(
            repo_root, 'merge-base', '--all', managed_tip, delivery_tip,
            check=False,
        )
        if bases.returncode != 0:
            return False
        bases = bases.stdout.split()
        if len(bases) != 1:
            return False
        base = bases[0]
        first_parent_chain = self.repository.git(
            repo_root, 'rev-list', '--first-parent', managed_tip,
        ).stdout.split()
        if base not in first_parent_chain:
            return False
        commits = self.repository.git(
            repo_root, 'rev-list', '--first-parent', '--reverse',
            f'{base}..{managed_tip}',
        ).stdout.split()
        if not commits:
            return False
        managed_stack = {
            'commits': commits,
            'target_remote': stack.get('target_remote'),
            'target_branch': stack.get('target_branch'),
        }
        return self.historical_stack_has_exact_squash_delivery(
            repo_root,
            managed_stack,
            delivery_observation,
            defaults,
            coordination_remote,
        )


    def historical_rebuilt_stack_tip_is_absorbed(
        self, repo_root, old_stack, new_stack, old_tip, delivery_observation,
    ):
        """Prove a replaced source generation adds no product to the delivery tree.

        A stack may be rebuilt from a newer main before its absorbed close.  The
        newer source can be squash-delivered while the old source remains in the
        published integration history.  The old generation is owned only when the
        same stack/ref declared its exact tip and merging it into current main is
        a conflict-free no-op.
        """
        old_identity = stack_delivery_identity(old_stack)
        new_identity = stack_delivery_identity(new_stack)
        for key in ('commits', 'integration_commits', 'integration_only_commits'):
            old_identity.pop(key)
            new_identity.pop(key)
        old_commits = stack_integration_commits(old_stack)
        declared = old_stack.get('commits') or []
        delivery_tip = (delivery_observation or {}).get('tip')
        if (
            old_identity != new_identity
            or not old_commits
            or old_commits != declared
            or declared[-1] != old_tip
            or not delivery_tip
            or not self.repository.commit_exists(repo_root, old_tip)
            or not self.repository.commit_exists(repo_root, delivery_tip)
        ):
            return False
        first_parent_history = set(self.repository.git(
            repo_root, 'rev-list', '--first-parent', old_tip,
        ).stdout.split())
        if not set(declared).issubset(first_parent_history):
            return False
        old_base = self.repository.commit_first_parent(repo_root, old_commits[0])
        if not old_base or not self.repository.branch_contains(repo_root, delivery_tip, old_base):
            return False
        merged = self.repository.git(
            repo_root, 'merge-tree', '--write-tree', f'--merge-base={old_base}',
            delivery_tip, old_tip, check=False,
        )
        return merged.returncode == 0 and merged.stdout.split() == [
            self.repository.ref_tree(repo_root, delivery_tip)
        ]


    def abandoned_stack_paths_match_delivery(self, repo_root, commits, local_tip, published_tip, delivery_tip):
        """An abandoned source has no unique product path in either integration tree."""
        if (
            not commits or not published_tip or not delivery_tip
            or any(
                not self.repository.commit_exists(repo_root, commit)
                or self.repository.commit_parent_count(repo_root, commit) != 1
                for commit in commits
            )
        ):
            return False
        paths = {
            path for commit in commits
            for path in self.repository.commit_changed_files(repo_root, commit)
        } - {'.syncwheel/manifest.json'}
        return bool(paths) and all(
            self.projection.tree_path_entry(repo_root, candidate, path)
            == self.projection.tree_path_entry(repo_root, delivery_tip, path)
            for candidate in (local_tip, published_tip)
            for path in paths
        )


    def historically_closed_integration_commits(
        self, repo_root, tip, observation, candidates=None, delivery_tip=None,
        delivery_observation=None, merge_parent_candidates=None, abandonment_only=False,
        absorbed_merge_tips=None,
    ):
        """Return exact commits covered by a durable coordinated close proof.

        A squash/rebase close proves delivery when it happens, but later changes to
        the same paths make a fresh byte comparison against the current base
        impossible.  The append-only coordination chain still contains both the
        closing tombstone and its parent manifest.  Accept only commits in the
        history being reconciled whose exact SHA or stable patch ID matches what
        the parent declared.  Unrelated commits remain unexplained.
        """
        if not observation or observation.get('status') != 'current':
            return set()
        current = observation.get('state_tip')
        integration_ref = observation.get('integration_ref')
        config = observation.get('config') or {}
        if not current or not integration_ref or not config.get('id'):
            return set()
        candidates = [
            self.repository.commit_full_sha(repo_root, commit)
            for commit in (candidates if candidates is not None else self.repository.rev_list(repo_root, tip))
            if self.repository.commit_exists(repo_root, commit)
        ]
        candidate_patches = self.repository.commit_patch_ids(repo_root, candidates)
        candidate_patch_values = {value for value in candidate_patches.values() if value}
        merge_parent_candidates = {
            self.repository.commit_full_sha(repo_root, commit)
            for commit in (merge_parent_candidates or [])
            if self.repository.commit_exists(repo_root, commit)
        }
        head = current
        closed_sources = set()
        abandoned_sources = set()
        owned_merge_parents = set()
        trusted_stack_generations = {}
        state_cache = {}

        def load_state(commit):
            if commit not in state_cache:
                state_cache[commit] = self.coordination.coordination_state_from_commit(
                    repo_root, commit, config['id']
                )
            return state_cache[commit]

        def historical_state_form(state_tip, state):
            try:
                return self.coordination.coordination_state_manifest_digest_classification(
                    repo_root, state, config.get('remote')
                )['form']
            except SyncwheelError as exc:
                unavailable = (
                    str(exc) == (
                        'coordination state integration tip object is unavailable '
                        'for manifest verification'
                    )
                    and state_tip != head
                    and self.repository.git(
                        repo_root, 'rev-parse', '--is-shallow-repository'
                    ).stdout.strip() == 'false'
                )
                if not unavailable:
                    raise
                print(
                    'WARNING: ignoring unavailable historical closed-stack proof '
                    f'{state_tip}',
                    file=sys.stderr,
                )
                return None

        while current:
            state = load_state(current)
            parents = self.repository.git(repo_root, 'show', '-s', '--format=%P', current).stdout.split()
            parent = state.get('parent_state')
            if parents != ([parent] if parent else []):
                raise SyncwheelError('integration reconciliation historical state chain is invalid')
            if current == head:
                self.coordination.verify_coordination_state_manifest_digest(
                    repo_root, state, config.get('remote')
                )
            if parent and str(state.get('publication_scope') or '').startswith('close:'):
                stack_id = state['publication_scope'].split(':', 1)[1]
                parent_state = load_state(parent)
                parent_tombstones = {
                    coordination_tombstone_ref(item)
                    for item in parent_state.get('tombstones') or []
                }
                tombstones = [
                    item for item in state.get('tombstones') or []
                    if item.get('stack') == stack_id
                    and item.get('reason') in {'absorbed', 'merged', 'abandoned', 'superseded'}
                    and coordination_tombstone_ref(item) not in parent_tombstones
                ]
                parent_stacks = [
                    item for item in (parent_state.get('manifest') or {}).get('stacks') or []
                    if item.get('id') == stack_id
                ]
                child_stacks = {
                    item.get('id')
                    for item in (state.get('manifest') or {}).get('stacks') or []
                }
                if len(tombstones) == 1 and len(parent_stacks) == 1 and stack_id not in child_stacks:
                    stack = parent_stacks[0]
                    closed_ref = coordination_tombstone_ref(tombstones[0])
                    expected_ref = f"refs/heads/{stack.get('branch')}"
                    state_form = historical_state_form(current, state)
                    parent_form = historical_state_form(parent, parent_state)
                    if (
                        closed_ref == expected_ref
                        and closed_ref not in parent_tombstones
                        and state_form is not None
                        and parent_form is not None
                        and state_form != COORDINATION_STATE_DIGEST_FORM_ORPHANED
                        and parent_form != COORDINATION_STATE_DIGEST_FORM_ORPHANED
                    ):
                        declared = [
                            *stack_integration_commits(stack),
                            *stack_integration_only_commits(stack),
                        ]
                        declared_complete = all(
                            self.repository.commit_exists(repo_root, commit) for commit in declared
                        )
                        reason = tombstones[0].get('reason')
                        managed_tip = (
                            parent_state.get('managed_refs') or {}
                        ).get(expected_ref)
                        if reason == 'absorbed' and managed_tip:
                            explicit = {
                                self.repository.commit_full_sha(repo_root, commit)
                                for commit in declared
                                if self.repository.commit_exists(repo_root, commit)
                            }
                            managed_parents = (
                                self.repository.git(
                                    repo_root, 'show', '-s', '--format=%P', managed_tip
                                ).stdout.split()
                                if self.repository.commit_exists(repo_root, managed_tip)
                                else []
                            )
                            if len(managed_parents) == 2 and explicit.intersection(
                                managed_parents
                            ):
                                declared.extend(managed_parents)
                                # Preserve the proof kind: abandoned/superseded and
                                # promotion-only ownership never authorize this set.
                                if absorbed_merge_tips is not None and managed_tip in merge_parent_candidates:
                                    absorbed_merge_tips.add(managed_tip)
                            declared.append(managed_tip)
                        declared = [
                            self.repository.commit_full_sha(repo_root, commit)
                            for commit in declared if self.repository.commit_exists(repo_root, commit)
                        ]
                        if reason in {'abandoned', 'superseded'}:
                            # Neither close reason proves delivery. It only explains
                            # the exact historical commits once every path they
                            # changed is now supplied by the live delivery base in
                            # both integration trees.  Never admit patch-equivalent
                            # commits through this proof.
                            base_observation = delivery_observation or {}
                            base_tip = base_observation.get('tip')
                            defaults = (parent_state.get('manifest') or {}).get('defaults') or {}
                            target_remote = (
                                stack.get('target_remote')
                                or defaults.get('canonical_remote')
                                or config.get('remote')
                            )
                            target_branch = stack.get('target_branch') or defaults.get('base_branch')
                            published_tip = observation.get('published_tip')
                            if (
                                declared and declared_complete and base_tip
                                and managed_tip
                                and tombstones[0].get('remote_tip') in {None, managed_tip}
                                and base_tip == delivery_tip
                                and base_observation.get('remote') == target_remote
                                and base_observation.get('remoteRef') == f'refs/heads/{target_branch}'
                                and base_observation.get('remoteTip') == base_tip
                                and published_tip
                                and self.repository.branch_contains(repo_root, tip, published_tip)
                                and self.abandoned_stack_paths_match_delivery(
                                    repo_root, declared, tip, published_tip, base_tip,
                                )
                            ):
                                abandoned_sources.update(declared)
                        else:
                            declared_patches = self.repository.commit_patch_ids(repo_root, declared)
                            relevant = bool(
                                set(declared).intersection(candidates)
                                or candidate_patch_values.intersection(
                                    value for value in declared_patches.values() if value
                                )
                            )
                            if reason == 'absorbed' or (
                                relevant and self.historical_stack_has_exact_squash_delivery(
                                    repo_root,
                                    stack,
                                    delivery_observation,
                                    (parent_state.get('manifest') or {}).get('defaults') or {},
                                    config.get('remote'),
                                )
                            ):
                                closed_sources.update(declared)
                                if reason == 'absorbed':
                                    trusted_stack_generations[stack_id] = {
                                        'digest': canonical_json_digest(stack),
                                        'managed_tip': managed_tip,
                                        'exact_squash_delivery': (
                                            self.historical_managed_tip_has_exact_squash_delivery(
                                            repo_root,
                                            managed_tip,
                                            stack,
                                            delivery_observation,
                                            (parent_state.get('manifest') or {}).get('defaults') or {},
                                            config.get('remote'),
                                            )
                                        ),
                                    }
            if (
                parent
                and str(state.get('publication_scope') or '').startswith('promote:')
            ):
                stack_id = state['publication_scope'].split(':', 1)[1]
                parent_state = load_state(parent)
                parent_stacks = [
                    item for item in (parent_state.get('manifest') or {}).get('stacks') or []
                    if item.get('id') == stack_id
                ]
                child_stacks = [
                    item for item in (state.get('manifest') or {}).get('stacks') or []
                    if item.get('id') == stack_id
                ]
                parent_tombstones = {
                    coordination_tombstone_ref(item)
                    for item in parent_state.get('tombstones') or []
                }
                promoted = [
                    item for item in state.get('tombstones') or []
                    if item.get('stack') == stack_id
                    and item.get('reason') == 'promoted'
                    and coordination_tombstone_ref(item) not in parent_tombstones
                ]
                if (
                    len(child_stacks) == 1
                    and (trusted_stack_generations.get(stack_id) or {}).get('digest')
                    == canonical_json_digest(child_stacks[0])
                    and len(parent_stacks) == 1
                    and len(promoted) == 1
                ):
                    old_stack = parent_stacks[0]
                    new_stack = child_stacks[0]
                    closed_ref = coordination_tombstone_ref(promoted[0])
                    authoritative_tip = (
                        parent_state.get('managed_refs') or {}
                    ).get(closed_ref)
                    state_form = historical_state_form(current, state)
                    parent_form = historical_state_form(parent, parent_state)
                    if (
                        closed_ref == f"refs/heads/{old_stack.get('branch')}"
                        and closed_ref not in parent_tombstones
                        and promoted[0].get('remote_tip') == authoritative_tip
                        and authoritative_tip
                        and self.repository.commit_exists(repo_root, authoritative_tip)
                        and new_stack.get('branch') != old_stack.get('branch')
                        and state_form is not None
                        and parent_form is not None
                        and state_form != COORDINATION_STATE_DIGEST_FORM_ORPHANED
                        and parent_form != COORDINATION_STATE_DIGEST_FORM_ORPHANED
                    ):
                        promoted_sources = [
                            *stack_integration_commits(old_stack),
                            *stack_integration_only_commits(old_stack),
                            authoritative_tip,
                        ]
                        closed_sources.update(
                            self.repository.commit_full_sha(repo_root, commit)
                            for commit in promoted_sources
                            if commit and self.repository.commit_exists(repo_root, commit)
                        )
            if parent:
                parent_state = load_state(parent)
                scope = str(state.get('publication_scope') or '')
                for stack_id, trusted in list(trusted_stack_generations.items()):
                    if scope == f'close:{stack_id}':
                        continue
                    child_stacks = [
                        item for item in (state.get('manifest') or {}).get('stacks') or []
                        if item.get('id') == stack_id
                    ]
                    parent_stacks = [
                        item for item in (parent_state.get('manifest') or {}).get('stacks') or []
                        if item.get('id') == stack_id
                    ]
                    if (
                        len(child_stacks) != 1
                        or canonical_json_digest(child_stacks[0]) != trusted['digest']
                    ):
                        trusted_stack_generations.pop(stack_id, None)
                        continue
                    if len(parent_stacks) != 1:
                        trusted_stack_generations.pop(stack_id, None)
                        continue
                    parent_digest = canonical_json_digest(parent_stacks[0])
                    parent_ref = f"refs/heads/{parent_stacks[0].get('branch')}"
                    child_ref = f"refs/heads/{child_stacks[0].get('branch')}"
                    parent_managed_tip = (
                        parent_state.get('managed_refs') or {}
                    ).get(parent_ref)
                    child_managed_tip = (
                        state.get('managed_refs') or {}
                    ).get(child_ref)
                    if parent_digest != trusted['digest']:
                        same_delivery = (
                            scope == f'stack:{stack_id}'
                            and stack_delivery_identity(child_stacks[0])
                            == stack_delivery_identity(parent_stacks[0])
                            and child_managed_tip == parent_managed_tip
                        )
                        if scope == f'promote:{stack_id}':
                            pass
                        elif same_delivery:
                            pass
                        elif (
                            scope == f'stack:{stack_id}'
                            and child_ref == parent_ref
                            and trusted['exact_squash_delivery']
                            and child_managed_tip == trusted['managed_tip']
                            and parent_managed_tip
                            and historical_state_form(current, state)
                            not in {None, COORDINATION_STATE_DIGEST_FORM_ORPHANED}
                            and historical_state_form(parent, parent_state)
                            not in {None, COORDINATION_STATE_DIGEST_FORM_ORPHANED}
                            and self.historical_rebuilt_stack_tip_is_absorbed(
                                repo_root, parent_stacks[0], child_stacks[0],
                                parent_managed_tip, delivery_observation,
                            )
                        ):
                            closed_sources.update(
                                self.repository.commit_full_sha(repo_root, commit)
                                for commit in [
                                    *stack_integration_commits(parent_stacks[0]),
                                    parent_managed_tip,
                                ]
                                if self.repository.commit_exists(repo_root, commit)
                            )
                        elif (
                            scope == f'stack:{stack_id}'
                            and child_ref == parent_ref
                            and trusted['exact_squash_delivery']
                            and child_managed_tip == trusted['managed_tip']
                            and parent_managed_tip
                            and self.repository.commit_exists(repo_root, parent_managed_tip)
                            and parent_managed_tip in self.repository.git(
                                repo_root, 'rev-list', '--first-parent', child_managed_tip,
                            ).stdout.split()
                        ):
                            promotion_parent = parent_state.get('parent_state')
                            promotion_parent_state = (
                                load_state(promotion_parent) if promotion_parent else None
                            )
                            promotion_parents = self.repository.git(
                                repo_root, 'show', '-s', '--format=%P', parent,
                            ).stdout.split()
                            promotion_parent_stacks = [
                                item for item in (
                                    (promotion_parent_state or {}).get('manifest') or {}
                                ).get('stacks') or []
                                if item.get('id') == stack_id
                            ]
                            promotion_parent_tombstones = {
                                coordination_tombstone_ref(item)
                                for item in (
                                    (promotion_parent_state or {}).get('tombstones') or []
                                )
                            }
                            promotion_tombstones = [
                                item for item in parent_state.get('tombstones') or []
                                if item.get('stack') == stack_id
                                and item.get('reason') == 'promoted'
                                and coordination_tombstone_ref(item)
                                not in promotion_parent_tombstones
                            ]
                            promotion_owned = bool(
                                parent_state.get('publication_scope') == f'promote:{stack_id}'
                                and promotion_parent
                                and promotion_parents == [promotion_parent]
                                and len(promotion_parent_stacks) == 1
                                and len(promotion_tombstones) == 1
                                and coordination_tombstone_ref(promotion_tombstones[0])
                                == f"refs/heads/{promotion_parent_stacks[0].get('branch')}"
                                and promotion_tombstones[0].get('remote_tip')
                                == parent_managed_tip
                                and (
                                    (promotion_parent_state.get('managed_refs') or {}).get(
                                        coordination_tombstone_ref(promotion_tombstones[0])
                                    )
                                    == parent_managed_tip
                                )
                                and historical_state_form(parent, parent_state)
                                not in {None, COORDINATION_STATE_DIGEST_FORM_ORPHANED}
                                and historical_state_form(
                                    promotion_parent, promotion_parent_state
                                ) not in {None, COORDINATION_STATE_DIGEST_FORM_ORPHANED}
                            )
                            if (
                                promotion_owned
                                and parent_managed_tip in merge_parent_candidates
                            ):
                                owned_merge_parents.add(self.repository.commit_full_sha(
                                    repo_root, parent_managed_tip,
                                ))
                            trusted_stack_generations.pop(stack_id, None)
                            continue
                        else:
                            trusted_stack_generations.pop(stack_id, None)
                            continue
                    trusted_stack_generations[stack_id] = {
                        **trusted,
                        'digest': parent_digest,
                        'managed_tip': parent_managed_tip,
                    }
            current = parent
        if abandonment_only:
            return abandoned_sources.intersection(candidates)
        if not closed_sources:
            return abandoned_sources.intersection(candidates)
        source_patches = {
            value for value in self.repository.commit_patch_ids(repo_root, closed_sources).values()
            if value
        }
        closed = {
            commit for commit in candidates
            if commit in closed_sources
            or (candidate_patches.get(commit) and candidate_patches[commit] in source_patches)
        }
        closed.update(owned_merge_parents.intersection(merge_parent_candidates))
        closed.update(abandoned_sources.intersection(candidates))
        return closed


    def historically_abandoned_unmapped_commits(self, repo_root, manifest, commits):
        """Narrow a validation warning using a fresh, exact abandoned-close proof."""
        if not commits or not coordination_is_active(manifest):
            return set()
        observation = self.projection.observe_published_integration_tip(repo_root, manifest)
        if not observation or observation.get('status') != 'current':
            return set()
        delivery = self.projection.integration_projection_ref_observation(
            repo_root, 'base', 'integration', manifest['integration']['base']
        )
        return self.historically_closed_integration_commits(
            repo_root,
            self.repository.ref_tip(repo_root, manifest['integration']['branch']),
            observation,
            candidates=commits,
            delivery_tip=delivery['tip'],
            delivery_observation=delivery,
            abandonment_only=True,
        )


    def integration_only_replay_is_empty(self, repo_root, commit, head, tree):
        if self.repository.commit_parent_count(repo_root, commit) != 1 or self.repository.ref_tree(repo_root, head) != tree:
            return False
        replay = self.repository.git(repo_root, 'merge-tree', '--write-tree',
                     f'--merge-base={commit}^', head, commit, check=False)
        return replay.returncode == 0 and replay.stdout.split() == [tree]


    def historically_empty_integration_only_commits(self, repo_root, manifest, commits, manifest_path=None):
        """Local native capture authorizes only the exact retired no-op endpoint.

        The subsequent ancestry reconciliation carries this old history in its
        durable two-parent proof, which existing fresh-clone verification consumes.
        """
        wanted, retired, previous = set(commits), set(), {}
        current = None
        for event in _manifest_storage.load_ledger_events(repo_root, manifest_path):
            if event.get('type') not in {'manifest_initialized', 'manifest_saved'}:
                continue
            payload = event.get('payload') or {}
            context = payload.get('context') or {}
            summaries = {item['id']: item for item in payload.get('stacks', [])}
            proof = context.get('empty_integration_only')
            stack_id = context.get('stack')
            before, after = previous.get(stack_id), summaries.get(stack_id)
            if payload.get('reason') == 'stack_resolve_integration' and proof and before and after:
                commit, head, tree = (proof.get(key) for key in ('commit', 'head', 'tree'))
                old_only = before.get('integration_only_commits', [])
                expected = copy.deepcopy(before)
                expected['integration_only_commits'] = [value for value in old_only if value != commit]
                expected['integration_commits'] = [value for value in before['integration_commits'] if value != commit]
                if current is None:
                    # History callers also accept anonymous source summaries. They
                    # cannot bind a named retirement proof and confer no permission.
                    current = {stack['id']: stack for stack in manifest.get('stacks', [])
                               if stack.get('id')}
                selected = current.get(stack_id)
                identity = lambda summary: {key: value for key, value in summary.items()
                                            if key not in {'integration_commits', 'integration_only_commits'}}
                if (commit in wanted and commit in old_only and context.get('commits') == []
                        and expected == after and selected
                        and identity(manifest_stack_history_summary(selected)) == identity(after)
                        and commit not in stack_integration_commits(selected)
                        and self.repository.ref_tip(repo_root, selected['branch']) == context.get('source_tip')
                        and all(value and self.repository.commit_exists(repo_root, value) for value in (commit, head))
                        and self.integration_only_replay_is_empty(repo_root, commit, head, tree)):
                    retired.add(commit)
            previous = summaries
        return retired


    def historically_reconciled_unmapped_commits(self, repo_root, manifest, commits):
        """Recognize authenticated historical derived and reconciled old-parent history.

        A published derived record can remain a valid historical witness after a
        later record replaces it in current provenance. A reconciliation keeps the
        old integration as its first parent while its
        verified second parent supplies the product tree.  Only commits exclusive
        to that old parent can be explained by this proof; later foreign commits
        and commits in an unproved merge remain unmapped.
        """
        if not commits:
            return set()
        carried = self.historically_empty_integration_only_commits(repo_root, manifest, commits)
        observation = self.projection.observe_published_integration_tip(repo_root, manifest)
        if not observation or observation.get('status') != 'current':
            return carried
        tip = self.repository.ref_tip(repo_root, manifest['integration']['branch'])
        wanted = set(commits)
        for commit in sorted(wanted - carried):
            if self.integration_reconciliation_historical_derived(
                repo_root, commit, tip, observation
            ):
                carried.add(commit)
        if carried == wanted:
            return carried
        manifest_path = _manifest_storage.integration_manifest_path(repo_root)
        base = manifest['integration']['base']
        for merge in self.repository.git(repo_root, 'rev-list', '--merges', f'{base}..{tip}').stdout.splitlines():
            proof = self.integration_reconciliation_proof(
                repo_root, merge, manifest_path, observation
            )
            if not proof:
                continue
            replay = proof['parents'][1]
            for old in [proof['parents'][0], *proof['parents'][2:]]:
                carried.update(wanted.intersection(self.repository.rev_list(repo_root, f'{replay}..{old}')))
            if carried == wanted:
                break
        return carried


    def integration_control_only_transition(self, repo_root, commit, parent):
        """Prove that a commit changes only valid Syncwheel control projection."""
        changed = {
            path for path in self.repository.git(
                repo_root, 'diff', '--name-only', '--no-renames', '-z', parent, commit
            ).stdout.split('\0')
            if path
        }
        if not changed or not changed.issubset({'.syncwheel/manifest.json', '.gitignore'}):
            return False
        entry = self.projection.tree_path_entry(repo_root, commit, '.syncwheel/manifest.json')
        if not entry or entry['mode'] != '100644':
            return False
        try:
            control = json.loads(self.projection.tree_path_bytes(repo_root, entry))
        except (ValueError, UnicodeDecodeError):
            control = None
        if not isinstance(control, dict):
            return False
        if '.gitignore' not in changed:
            return True
        candidate = self.projection.tree_path_entry(repo_root, commit, '.gitignore')
        parent_entry = self.projection.tree_path_entry(repo_root, parent, '.gitignore')
        if not candidate or candidate['mode'] != '100644' or (
            parent_entry is not None and parent_entry['mode'] != '100644'
        ):
            return False
        try:
            candidate_text = self.projection.tree_path_bytes(repo_root, candidate).decode('utf-8')
            parent_text = (
                self.projection.tree_path_bytes(repo_root, parent_entry).decode('utf-8')
                if parent_entry else ''
            )
            candidate_ignore = self.ignore_policy.split_historical_syncwheel_managed_gitignore(
                candidate_text, syncwheel_worktree_root(control),
            )
            parent_ignore = self.ignore_policy.split_historical_syncwheel_managed_gitignore(
                parent_text, syncwheel_worktree_root(control),
            )
        except UnicodeDecodeError:
            return False
        return bool(
            candidate_ignore and parent_ignore
            and candidate_ignore['managed'] is not None
            and candidate_ignore['unmanaged'] == parent_ignore['unmanaged']
        )


    def integration_reconciliation_history(
        self, repo_root, manifest, tip, provenance, manifest_path=None, observation=None,
        detached_replay=False, delivery_tip=None, delivery_observation=None,
    ):
        """Refuse unexplained history before constructing a successor transaction."""
        if not detached_replay and self.published_integration_has_no_unique_product(
            repo_root, manifest, tip, observation, delivery_tip,
        ):
            return
        base = manifest['integration']['base']
        history_base = base
        if not detached_replay:
            history_base = self.historically_delivered_integration_boundary(
                repo_root, manifest, tip, observation, delivery_tip,
            ) or base
        selected_ids = manifest['integration'].get('stacks')
        selected = set(selected_ids) if selected_ids is not None else None
        declared = {
            self.repository.commit_full_sha(repo_root, commit)
            for stack in manifest['stacks']
            if selected is None or stack.get('id') in selected
            for commit in [*stack_integration_commits(stack),
                           *(stack['commits'] if stack.get('integration_resolution') else [])]
        }
        declared.update(self.historically_empty_integration_only_commits(
            repo_root, manifest, self.repository.rev_list(repo_root, f'{base}..{tip}'), manifest_path,
        ))
        patches = {self.repository.commit_patch_id(repo_root, commit) for commit in declared}
        patches.discard(None)
        patches.update(self.repository.patch_ids_reachable_from_ref(repo_root, base))
        history = self.repository.rev_list(repo_root, f'{history_base}..{tip}')
        merge_parents = {
            commit: self.repository.git(repo_root, 'show', '-s', '--format=%P', commit).stdout.split()
            for commit in history
            if self.repository.commit_parent_count(repo_root, commit) > 1
        }
        historical_candidates = list(history)
        historical_merge_parents = {
            parents[1] for parents in merge_parents.values() if len(parents) == 2
        }
        historical_candidates.extend(historical_merge_parents)
        absorbed_merge_tips = set()
        closed_commits = (
            self.historically_closed_integration_commits(
                repo_root, tip, observation, candidates=historical_candidates,
                delivery_tip=delivery_tip,
                delivery_observation=delivery_observation,
                merge_parent_candidates=historical_merge_parents,
                absorbed_merge_tips=absorbed_merge_tips,
            ) if not detached_replay else set()
        )
        proofs = {commit: proof for commit in history
                  if self.repository.commit_parent_count(repo_root, commit) > 1
                  and (proof := self.integration_reconciliation_proof(repo_root, commit, manifest_path, observation))}
        carried_history = {
            old_commit
            for proof in proofs.values()
            for parent in [proof['parents'][0], *proof['parents'][2:]]
            for old_commit in self.repository.rev_list(repo_root, f"{proof['parents'][1]}..{parent}")
        }
        replay_ranges = [(base, tip)] if detached_replay else []
        replay_ranges.extend((proof['inputs']['refs'][0]['tip'], proof['parents'][1]) for proof in proofs.values())
        replay_merges = set()
        for replay_base, replay_tip in replay_ranges:
            replay_merges.update(self.repository.git(repo_root, 'rev-list', '--first-parent', '--merges',
                                     f'{replay_base}..{replay_tip}').stdout.split())
        for commit in history:
            if commit in declared or commit in closed_commits or commit in carried_history:
                continue
            if self.repository.commit_parent_count(repo_root, commit) != 1:
                if commit in proofs or commit in replay_merges:
                    continue
                parents = merge_parents[commit]
                if len(parents) == 2 and (
                    parents[1] in declared
                    or parents[1] in absorbed_merge_tips
                    or self.repository.commit_patch_id(repo_root, parents[1]) in patches
                ):
                    # An exact auto-merge of a declared, delivered, or authenticated
                    # absorbed managed merge tip retains ownership even if the historical
                    # merge itself was not recorded in the current manifest.
                    merge = self.repository.git(
                        repo_root, 'merge-tree', '--write-tree',
                        parents[0], parents[1], check=False,
                    )
                    if merge.returncode == 0 and merge.stdout.strip() == self.repository.ref_tree(repo_root, commit):
                        continue
                if (
                    len(parents) == 2
                    and (
                        (delivery_tip and self.repository.branch_contains(repo_root, delivery_tip, parents[1]))
                        or parents[1] in closed_commits
                    )
                    and self.integration_control_only_transition(
                        repo_root, commit, parents[1]
                    )
                ):
                    continue
                raise SyncwheelError(f'integration reconciliation has unclassified merge: {commit}')
            changed = set(self.repository.commit_changed_files(repo_root, commit))
            if self.integration_control_only_transition(repo_root, commit, f'{commit}^'):
                continue
            if self.provenance.is_provenance_bound_derived_projection_commit(repo_root, commit, provenance):
                continue
            patch = self.repository.commit_patch_id(repo_root, commit)
            if patch and patch in patches:
                continue
            if observation and self.integration_reconciliation_historical_derived(repo_root, commit, tip, observation):
                continue
            product_paths = sorted(
                changed - {'.syncwheel/manifest.json', '.gitignore'}
            )
            if product_paths:
                raise SyncwheelError(
                    'published integration contains unexplained product paths: '
                    + ', '.join(product_paths)
                    + f' (unclassified history {commit})'
                )
            raise SyncwheelError(f'integration reconciliation has unclassified history: {commit}')


    def apply_integration_reconciliation(self, repo_root, manifest_path, manifest, intent):
        """Resume the same object and CAS; never rebuild or overwrite a later proposal."""
        branch = intent['integration_branch']
        local_tip = intent['replay_tip']
        final_tip = intent['expected_control_commit']
        replay_tip = intent['detached_replay_tip']
        pinned, overrides = self.integration_reconciliation_inputs(repo_root, manifest, intent['inputs'])
        ignore_entry = intent['gitignore_entry']
        ignore_bytes = self.projection.tree_path_bytes(repo_root, ignore_entry)
        source_ignore = intent['source_lease']['gitignore']
        state = self.coordination.coordination_state_from_commit(repo_root, intent['state_tip'], coordination_config(manifest)['id'])
        published_parent = intent.get('published_parent')
        if published_parent is not None:
            if published_parent != {'tip': intent['published_tip'], 'state_tip': intent['state_tip']}:
                raise SyncwheelError('integration reconciliation intent has an unbound published parent')
            observation = {
                'status': 'current',  # Qualification of the frozen state, not a new remote observation.
                'state': state, 'state_tip': intent['state_tip'],
                'config': coordination_config(manifest), 'remote_refs': intent['remote_refs'],
                'integration_ref': f"refs/heads/{branch}", 'published_tip': intent['published_tip'],
            }
            self.verify_reconciliation_published_parent(
                repo_root, manifest, local_tip, replay_tip, published_parent, intent['inputs'], observation,
            )
        shared = self.provenance.shared_derived_provenance_records(repo_root, manifest, state)
        provenance_before, store_after, provenance_after = self.integration_reconciliation_provenance(
            manifest, intent['provenance_store'], shared,
        )
        if (
            str(Path(manifest_path).absolute()) != intent['manifest_path']
            or (ignore_entry is not None and (
                ignore_entry['mode'] != '100644'
                or hashlib.sha256(ignore_bytes).hexdigest() != source_ignore.get('sha256')
            ))
            or self.replay.materialize_integration_replay(repo_root, pinned, overrides) != replay_tip
            or self.projection.materialize_control_manifest_projection_tree(
                repo_root, manifest, self.repository.ref_tree(repo_root, replay_tip),
                gitignore_bytes=ignore_bytes if ignore_entry else None,
            ) != intent['final_tree']
            or provenance_before != intent['provenance_before']
            or store_after != intent['provenance_store_after']
            or provenance_after != intent['provenance_after']
        ):
            raise SyncwheelError('integration reconciliation intent does not bind its replay and source bytes')
        parents = self.repository.git(repo_root, 'show', '-s', '--format=%P', final_tip).stdout.split()
        if (
            parents != [local_tip, replay_tip, *([published_parent['tip']] if published_parent is not None else [])]
            or self.repository.ref_tree(repo_root, final_tip) != intent['final_tree']
            or self.integration_reconciliation_object(
                repo_root, manifest, local_tip, replay_tip, intent['final_tree'], intent['inputs'], published_parent
            ) != final_tip
            or manifest_digest(manifest) != intent['expected_manifest_digest']
        ):
            raise SyncwheelError('integration reconciliation intent has an invalid expected object')
        source = self.integration_reconciliation_source(repo_root, manifest_path)
        before_source = intent['source_lease']
        current_tip = self.repository.ref_tip(repo_root, branch)
        if current_tip not in {local_tip, final_tip}:
            if not current_tip or not self.repository.branch_contains(repo_root, current_tip, final_tip):
                self.control.abandon_control_manifest_intent(
                    repo_root, manifest_path, intent, current_tip, 'ref_cas_failed'
                )
            raise SyncwheelError('integration changed during reconciliation; preserving the newer ref')
        if current_tip == local_tip:
            observed = self.projection.observe_published_integration_tip(repo_root, manifest)
            if (
                source != before_source
                or not observed or observed.get('status') != 'current'
                or observed['state_tip'] != intent['state_tip']
                or observed['remote_refs'] != intent['remote_refs']
                or self.projection.integration_projection_input_snapshot(repo_root, manifest) != intent['inputs']
                or self.provenance.load_derived_provenance_store(repo_root) != intent['provenance_store']
            ):
                raise IntegrationReconciliationLeaseDrift(
                    'integration reconciliation lease changed before CAS; review inputs and '
                    'run int rebuild --reason to start a new operation'
                )
            _manifest_storage.require_manifest_transaction_current(manifest_path)
            moved = self.repository.git(
                repo_root, 'update-ref', f'refs/heads/{branch}', final_tip, local_tip,
                check=False,
            )
            if moved.returncode != 0:
                self.control.abandon_control_manifest_intent(
                    repo_root, manifest_path, intent, self.repository.ref_tip(repo_root, branch), 'ref_cas_failed'
                )
                raise SyncwheelError('integration reconciliation CAS lost; competing work preserved')
        _manifest_storage.control_manifest_io_checkpoint('ref_updated')
        _manifest_storage.control_manifest_io_checkpoint('before_provenance_resolution')
        with self.provenance.derived_provenance_store_lock(repo_root):
            store = self.provenance.load_derived_provenance_store(repo_root)
            if store not in (intent['provenance_store'], intent['provenance_store_after']):
                raise SyncwheelError('integration reconciliation provenance changed; pending intent preserved')
            if store != intent['provenance_store_after']:
                self.provenance.save_derived_provenance_store(repo_root, intent['provenance_store_after'])
        _manifest_storage.control_manifest_io_checkpoint('provenance_resolved')
        worktree = self.repository.find_worktree_for_branch(repo_root, branch)
        if (str(Path(worktree).resolve()) if worktree else None) != intent['destination_path']:
            raise SyncwheelError('integration reconciliation checkout moved; pending intent preserved')
        if worktree is not None:
            worktree = Path(worktree)
            _manifest_storage.control_manifest_io_checkpoint('before_checkout_alignment')
            guard = self.projection.checkout_reset_guard(worktree)
            current_source = self.integration_reconciliation_source(repo_root, manifest_path)
            expected_source = dict(before_source)
            if Path(repo_root).resolve() == worktree.resolve():
                expected_source['head'] = final_tip
            already_aligned = (
                self.repository.git(worktree, 'write-tree').stdout.strip() == intent['final_tree']
                and not guard['unstaged'] and not guard['untracked']
            )
            if not already_aligned and (
                guard != intent['destination_guard'] or current_source != expected_source
            ):
                raise SyncwheelError('integration reconciliation checkout changed; pending intent preserved')
            if self.repository.ref_tip(repo_root, branch) != final_tip:
                raise SyncwheelError('integration reconciliation ref changed before checkout alignment')
            if not already_aligned:
                self.repository.git(worktree, 'read-tree', '--reset', '-u', final_tip)
            if self.repository.ref_tip(repo_root, branch) != final_tip:
                raise SyncwheelError('integration reconciliation ref changed during checkout alignment')
            transaction = _manifest_storage.active_manifest_write_transaction(manifest_path)
            if transaction is not None and Path(manifest_path) == _manifest_storage.integration_manifest_path(worktree):
                transaction['expectedDigest'] = intent['expected_manifest_digest']
        aligned_guard = self.projection.checkout_reset_guard(worktree) if worktree else None
        _manifest_storage.control_manifest_io_checkpoint('checkout_aligned')
        source_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
        if source_manifest is None or manifest_digest(source_manifest) != manifest_digest(manifest):
            raise SyncwheelError('integration reconciliation source changed; pending intent preserved')
        _manifest_storage.control_manifest_io_checkpoint('manifest_saved')
        if (
            self.repository.ref_tip(repo_root, branch) != final_tip
            or (worktree and self.projection.checkout_reset_guard(worktree) != aligned_guard)
            or self.provenance.load_derived_provenance_store(repo_root) != intent['provenance_store_after']
        ):
            raise SyncwheelError('integration reconciliation changed before terminal receipt; pending intent preserved')
        self.control.append_ledger_event(
            repo_root, 'manifest_saved',
            self.control.control_manifest_event_payload(
                repo_root, manifest_path, manifest, final_tip, 'reconciliation',
                intent['reason'], intent['command'],
                {'replay_tip': local_tip, 'version': 2, 'detached_replay_tip': replay_tip,
                 **({'published_parent': published_parent} if published_parent is not None else {})},
                operation_id=intent['operation_id'],
            ),
            manifest_path,
            idempotency_key=_manifest_storage.control_manifest_event_idempotency_key(
                manifest, local_tip, final_tip, intent['command'], intent['operation_id']
            ),
        )
        _manifest_storage.control_manifest_io_checkpoint('ledger_saved')
        return True


    def integration_divergence_is_control_only(self, repo_root, local_tip, published_tip):
        """Admit only a linear, structurally valid local control history at divergence."""
        bases = self.repository.git(repo_root, 'merge-base', '--all', local_tip, published_tip, check=False)
        if bases.returncode != 0 or len(bases.stdout.split()) != 1:
            return False
        previous = bases.stdout.strip()
        commits = self.repository.rev_list(repo_root, f'{published_tip}..{local_tip}')
        if not commits:
            return False
        for commit in commits:
            parents = self.repository.git(repo_root, 'show', '-s', '--format=%P', commit).stdout.split()
            if parents != [previous]:
                return False
            try:
                if not self.integration_control_only_transition(repo_root, commit, previous):
                    return False
                control = normalize_manifest_data(
                    self.integration_control_manifest_from_tree(repo_root, commit),
                    _manifest_storage.integration_manifest_path(repo_root),
                )
                defaults, integration = control['defaults'], control['integration']
                if (
                    isinstance(control['version'], bool)
                    or any(not isinstance(defaults.get(key), str) or not defaults[key]
                        for key in ('canonical_remote', 'publication_remote', 'base_branch', 'base_ref'))
                    or not isinstance(integration.get('base'), str) or not integration['base']
                    or integration.get('strategy') not in INTEGRATION_STRATEGIES
                    or not isinstance(integration.get('branch'), str)
                    or not self.coordination.is_valid_coordination_branch_ref(f"refs/heads/{integration['branch']}")
                    or not self.coordination.is_valid_coordination_branch_ref(f"refs/heads/{defaults['base_branch']}")
                    or not isinstance(integration['stacks'], list)
                    or not all(isinstance(key, str) and key in stack_map(control)
                               for key in integration['stacks'])
                    or len(integration['stacks']) != len(set(integration['stacks']))
                ):
                    return False
                for stack in control['stacks']:
                    if (
                        any(not isinstance(stack.get(key), str) or not stack[key]
                            for key in ('base', 'target_remote', 'target_branch', 'integration_branch'))
                        or any(not self.coordination.is_valid_coordination_branch_ref(f'refs/heads/{stack[key]}')
                               for key in ('branch', 'target_branch', 'integration_branch'))
                    ):
                        return False
            except (SyncwheelError, TypeError, ValueError, KeyError, AttributeError):
                return False
            previous = commit
        return previous == local_tip


    def reconcile_integration_ancestry(self, repo_root, manifest_path, manifest, command, reason):
        """Plan an ordinary detached replay before changing published integration history."""
        if not coordination_is_active(manifest):
            return False
        branch = manifest['integration']['branch']
        with self.control.control_manifest_branch_lock(repo_root, branch):
            # Refresh moving replay inputs before freezing their local and remote
            # observations, as the ordinary replay path does before materializing.
            self.repository.git(repo_root, 'fetch', '--all', '--prune')
            observed = self.projection.observe_published_integration_tip(repo_root, manifest)
            if observed is None:
                return False
            if observed.get('status') != 'current':
                raise SyncwheelError('integration reconciliation requires current published state')
            self.coordination.verify_coordination_state_manifest_digest(
                repo_root, observed['state'], observed['config']['remote']
            )
            reuse_preflight = self.projection.plan_published_integration_tip_reuse(
                repo_root, manifest, manifest_path, refresh_inputs=False,
            )
            if (
                reuse_preflight
                and reuse_preflight.get('status') == 'refuse'
                and (
                    reuse_preflight.get('reason', '').startswith(
                        'source .gitignore differs from the published control bytes'
                    )
                    or reuse_preflight.get('reason') == (
                        'integration replay inputs changed during published-tip proof'
                    )
                )
            ):
                raise SyncwheelError(reuse_preflight['reason'])
            local_tip = self.repository.ref_tip(repo_root, branch)
            local_successor = bool(local_tip and self.repository.branch_contains(repo_root, local_tip, observed['published_tip']))
            control_divergence = bool(
                local_tip and not local_successor
                and self.integration_divergence_is_control_only(
                    repo_root, local_tip, observed['published_tip'],
                )
            )
            if not local_successor and not control_divergence:
                raise SyncwheelError('integration reconciliation requires a local successor of published history')
            source = self.integration_reconciliation_source(repo_root, manifest_path)
            if source['manifest']['kind'] != 'file' or (
                source['gitignore']['kind'] == 'file' and source['gitignore']['mode'] & 0o111
            ):
                raise SyncwheelError('integration reconciliation requires regular control files')
            inputs = self.projection.integration_projection_input_snapshot(repo_root, manifest)
            destination = self.projection.checkout_reset_destination_lease(
                repo_root, branch,
                allowed_paths=_manifest_storage.control_manifest_source_allowance(
                    repo_root, manifest_path
                ),
            )
            store = self.provenance.load_derived_provenance_store(repo_root)
            shared = self.provenance.shared_derived_provenance_records(repo_root, manifest, observed['state'])
            provenance, store_after, provenance_after = self.integration_reconciliation_provenance(manifest, store, shared)
            pinned, overrides = self.integration_reconciliation_inputs(repo_root, manifest, inputs)
            replay_tip = self.replay.materialize_integration_replay(repo_root, pinned, overrides)
            published_parent = None
            if control_divergence and not self.repository.branch_contains(repo_root, replay_tip, observed['published_tip']):
                published_parent = {'tip': observed['published_tip'], 'state_tip': observed['state_tip']}
                try:
                    self.verify_reconciliation_published_parent(
                        repo_root, manifest, local_tip, replay_tip, published_parent, inputs, observed,
                    )
                except SyncwheelError as exc:
                    raise SyncwheelError('integration reconciliation replay does not preserve published history') from exc
            ignore = regular_checkout_path_bytes(repo_root, '.gitignore')
            if ignore is None:
                raise SyncwheelError('integration reconciliation requires regular ignore bytes')
            tree = self.projection.materialize_control_manifest_projection_tree(
                repo_root, manifest, self.repository.ref_tree(repo_root, replay_tip),
                gitignore_bytes=ignore if source['gitignore']['kind'] == 'file' else None,
            )
            self.integration_reconciliation_history(
                repo_root, manifest, local_tip, provenance, manifest_path, observed,
                delivery_tip=inputs['refs'][0]['tip'],
                delivery_observation=inputs['refs'][0],
            )
            self.integration_reconciliation_history(repo_root, pinned, replay_tip, provenance, manifest_path, observed,
                                               detached_replay=True)
            final_tip = (self.integration_reconciliation_object(repo_root, manifest, local_tip, replay_tip, tree, inputs, published_parent)
                         if replay_tip != local_tip else None)
            after = self.projection.observe_published_integration_tip(repo_root, manifest)
            if (
                source != self.integration_reconciliation_source(repo_root, manifest_path)
                or inputs != self.projection.integration_projection_input_snapshot(repo_root, manifest)
                or not after or after != observed
                or store != self.provenance.load_derived_provenance_store(repo_root)
                or local_tip != self.repository.ref_tip(repo_root, branch)
                or destination != self.projection.checkout_reset_destination_lease(
                    repo_root, branch, allowed_paths=_manifest_storage.control_manifest_source_allowance(repo_root, manifest_path)
                )
            ):
                raise SyncwheelError('integration reconciliation inputs changed during materialization')
            previous = self.integration_reconciliation_proof(repo_root, local_tip, manifest_path, observed)
            if previous and previous['parents'][1] == replay_tip and self.repository.ref_tree(repo_root, local_tip) == tree:
                return True
            if replay_tip == local_tip:
                control = self.control.materialize_control_manifest_commit(repo_root, manifest, local_tip)
                if self.repository.ref_tree(repo_root, control) != tree or store_after != store:
                    raise SyncwheelError('integration reconciliation cannot use legacy control persistence')
                return self.control.restore_control_manifest_after_integration_rebuild(
                    repo_root, manifest_path, manifest, local_tip, 'reconciliation',
                    command=command, reason=reason,
                ) or True
            intent = {
                **self.control.control_manifest_intent_payload(
                    repo_root, manifest, manifest_digest(manifest), local_tip, final_tip,
                    'reconciliation', reason, command, str(uuid.uuid4()),
                ),
                'version': 2, 'detached_replay_tip': replay_tip, 'final_tree': tree,
                **({'published_parent': published_parent} if published_parent is not None else {}),
                'state_tip': observed['state_tip'], 'published_tip': observed['published_tip'],
                'remote_refs': observed['remote_refs'], 'inputs': inputs, 'source_lease': source,
                'manifest_path': str(Path(manifest_path).absolute()),
                'destination_path': destination['worktree_path'],
                'gitignore_entry': self.projection.tree_path_entry(repo_root, tree, '.gitignore')
                    if source['gitignore']['kind'] == 'file' else None,
                'destination_guard': self.projection.checkout_reset_guard(destination['worktree_path'])
                    if destination['worktree_path'] else None,
                'provenance_store': store,
                'provenance_store_after': store_after,
                'provenance_before': provenance,
                'provenance_after': provenance_after,
            }
            self.control.append_ledger_event(
                repo_root, 'control_manifest_persistence_intent', intent, manifest_path,
                idempotency_key=_manifest_storage.control_manifest_intent_idempotency_key(intent['operation_id']),
            )
            _manifest_storage.control_manifest_io_checkpoint('intent_saved')
            return self.apply_integration_reconciliation(repo_root, manifest_path, manifest, intent)


    def integration_control_manifest_from_tree(self, repo_root, tree):
        """Read only the literal, regular Git control blob; never resolve checkout links."""
        entry = self.projection.tree_path_entry(repo_root, tree, '.syncwheel/manifest.json')
        if entry is None or entry['mode'] != '100644':
            return None
        try:
            value = json.loads(self.projection.tree_path_bytes(repo_root, entry))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SyncwheelError(f'integration control manifest is invalid at {tree}:.syncwheel/manifest.json') from exc
        return value if isinstance(value, dict) else None
