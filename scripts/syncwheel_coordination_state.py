"""Observe and verify immutable coordination state and ownership evidence.

Transport observations, committed-manifest inputs and replaceable state witnesses
are explicit dependencies. Publication and control-file writes live elsewhere.
"""

import json
import re
from pathlib import Path
from typing import Protocol

from syncwheel_errors import SyncwheelError
from syncwheel_manifest import (
    COORDINATION_STATE_PREFIX, MANIFEST_VERSION_CHANNELS,
    MANIFEST_VERSION_COORDINATED, coordination_state_ref,
    default_coordination_state_branch,
)

COORDINATION_STATE_FILE = '.syncwheel/coordination-state.json'

COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST = 'control-manifest-file'

COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT = 'legacy-normalized-snapshot'

COORDINATION_STATE_DIGEST_FORM_ORPHANED = 'orphaned'


class CoordinationReadRepository(Protocol):
    root: Path
    def git(self, repo_root, *args, **kwargs): ...
    def commit_exists(self, repo_root, ref): ...


class CoordinationManifestInputs(Protocol):
    def manifest_from_tree(self, repo_root, commit, path): ...
    def integration_manifest_path(self, repo_root): ...
    def manifest_digest(self, manifest): ...
    def coordination_manifest_snapshot(self, manifest, repo_root=None, coordination_state=None): ...
    def validate_coordination_state(self, state, expected_id=None, claims_mode='advisory'): ...


class CoordinationStateWitnesses(Protocol):
    """Independent observations consumed by state verification and ownership scans."""
    def remote_ref_tips(self, repo_root, remote, refs): ...
    def coordination_state_from_commit(self, repo_root, commit, expected_id=None, claims_mode='advisory'): ...
    def coordination_state_control_manifest(self, repo_root, state, remote=None): ...
    def coordination_state_manifest_digest_classification(self, repo_root, state, remote=None): ...
    def read_remote_coordination_states(self, repo_root, config, *, fetch=True): ...


class CoordinationStateReader:
    def __init__(self, repository: CoordinationReadRepository,
                 metadata: CoordinationManifestInputs,
                 evidence: CoordinationStateWitnesses):
        self.repository = repository
        self.metadata = metadata
        self.evidence = evidence

    def coordination_state_control_manifest(self, state, remote=None):
        repo_root = self.repository.root
        snapshot = state.get('manifest') or {}
        integration = snapshot.get('integration') or {}
        branch = integration.get('branch')
        if not isinstance(branch, str) or not branch:
            raise SyncwheelError('coordination state manifest has no integration branch')
        integration_ref = f'refs/heads/{branch}'
        tip = (state.get('managed_refs') or {}).get(integration_ref)
        if not isinstance(tip, str) or not re.fullmatch(r'[0-9a-f]{40}', tip):
            raise SyncwheelError('coordination state has no exact integration tip')
        available = self.repository.commit_exists(repo_root, tip)
        if not available and remote:
            fetched = self.repository.git(repo_root, 'fetch', '--quiet', remote, integration_ref, check=False)
            available = fetched.returncode == 0 and self.repository.commit_exists(repo_root, tip)
        if not available:
            raise SyncwheelError(
                'coordination state integration tip object is unavailable for manifest verification'
            )
        committed = self.metadata.manifest_from_tree(
            repo_root, tip, self.metadata.integration_manifest_path(repo_root)
        )
        if committed is None:
            raise SyncwheelError(
                'coordination state integration tip has no control manifest'
            )
        return committed

    def coordination_state_manifest_digest(self, state, remote=None):
        repo_root = self.repository.root
        return self.metadata.manifest_digest(
            self.evidence.coordination_state_control_manifest(repo_root, state, remote)
        )

    def coordination_state_legacy_manifest_digest(self, control_manifest, state):
        """Return the digest form published before 0.42.2: the normalized snapshot."""
        repo_root = self.repository.root
        try:
            snapshot = self.metadata.coordination_manifest_snapshot(control_manifest, repo_root, state)
        except SyncwheelError:
            return None
        return self.metadata.manifest_digest(snapshot)

    def coordination_state_manifest_digest_classification(self, state, remote=None):
        """Classify a state's manifest_digest, including the unrecognized (orphaned) case.

        Unreadable control-manifest evidence (missing tip, missing manifest file)
        is a distinct failure and is not caught here: it always propagates so a
        caller can never mistake it for a healable, merely-unrecognized digest.
        """
        repo_root = self.repository.root
        control_manifest = self.evidence.coordination_state_control_manifest(repo_root, state, remote)
        control_digest = self.metadata.manifest_digest(control_manifest)
        recorded = state.get('manifest_digest')
        if recorded == control_digest:
            form = COORDINATION_STATE_DIGEST_FORM_CONTROL_MANIFEST
        elif recorded == self.coordination_state_legacy_manifest_digest(
            control_manifest, state
        ):
            form = COORDINATION_STATE_DIGEST_FORM_LEGACY_SNAPSHOT
        else:
            form = COORDINATION_STATE_DIGEST_FORM_ORPHANED
        return {
            'form': form,
            'recorded_digest': recorded,
            'control_manifest_digest': control_digest,
        }

    def classify_coordination_state_manifest_digest(self, state, remote=None):
        repo_root = self.repository.root
        classification = self.evidence.coordination_state_manifest_digest_classification(repo_root, state, remote)
        if classification['form'] == COORDINATION_STATE_DIGEST_FORM_ORPHANED:
            raise SyncwheelError(
                'coordination state manifest_digest does not match the control manifest '
                'on its integration tip'
            )
        return classification

    def coordination_state_manifest_digest_form(self, state, remote=None):
        """Classify without failing closed, for callers that only report the form."""
        repo_root = self.repository.root
        try:
            return self.classify_coordination_state_manifest_digest(state, remote)
        except SyncwheelError:
            return None

    def verify_coordination_state_manifest_digest(self, state, remote=None):
        repo_root = self.repository.root
        return self.classify_coordination_state_manifest_digest(state, remote)

    def coordination_state_from_commit(
        self, commit, expected_id=None, claims_mode='advisory'
    ):
        repo_root = self.repository.root
        result = self.repository.git(repo_root, 'show', f'{commit}:{COORDINATION_STATE_FILE}', check=False)
        if result.returncode != 0:
            raise SyncwheelError(
                result.stderr.strip()
                or f'coordination state commit {commit} does not contain {COORDINATION_STATE_FILE}'
            )
        try:
            state = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SyncwheelError(f'invalid coordination state JSON at {commit}: {exc}') from exc
        return self.metadata.validate_coordination_state(state, expected_id, claims_mode)

    def read_remote_coordination_state(self, config, fetch=True, local_manifest_version=None):
        repo_root = self.repository.root
        state_ref = coordination_state_ref(config)
        tip = self.evidence.remote_ref_tips(repo_root, config['remote'], [state_ref])[state_ref]
        if not tip:
            return {'tip': None, 'state': None}
        if fetch:
            result = self.repository.git(repo_root, 'fetch', '--quiet', config['remote'], state_ref, check=False)
            if result.returncode != 0:
                raise SyncwheelError(
                    result.stderr.strip() or result.stdout.strip() or 'failed to fetch coordination state'
                )
            # FETCH_HEAD is clone-global and another fetch may replace it before
            # the read. Keep the state paired with the remote tip we observed.
            commit = tip
        else:
            commit = tip
        state = self.evidence.coordination_state_from_commit(
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

    def read_remote_coordination_states(self, config, *, fetch=True):
        repo_root = self.repository.root
        result = self.repository.git(
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
            if fetch:
                fetched = self.repository.git(repo_root, 'fetch', '--quiet', config['remote'], ref, check=False)
                if fetched.returncode != 0:
                    raise SyncwheelError(fetched.stderr.strip() or f'failed to fetch coordination state {ref}')
            state = self.evidence.coordination_state_from_commit(repo_root, tip)
            expected_ref = f"refs/heads/{default_coordination_state_branch(state['coordination_id'])}"
            if ref != expected_ref:
                raise SyncwheelError(
                    f'coordination state branch mismatch: {ref} declares {state["coordination_id"]!r}'
                )
            states.append({'ref': ref, 'tip': tip, 'state': state})
        return states

    def coordination_ownership_conflicts(self, config, managed_refs, states=None):
        repo_root = self.repository.root
        claimed = set(managed_refs)
        conflicts = []
        for item in (states if states is not None else self.evidence.read_remote_coordination_states(repo_root, config)):
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
        self,
        config,
        managed_refs,
        expected_state_refs=None,
        *, fetch=True,
    ):
        repo_root = self.repository.root
        states = self.evidence.read_remote_coordination_states(repo_root, config, fetch=fetch)
        observed_state_refs = {item['ref']: item['tip'] for item in states}
        if expected_state_refs is not None and observed_state_refs != expected_state_refs:
            raise SyncwheelError(
                'coordinated publish STOP: coordination state refs changed after the reviewed plan'
            )
        conflicts = self.coordination_ownership_conflicts(
            config, managed_refs, states=states
        )
        if conflicts:
            details = '; '.join(
                f"{item['coordination_id']}: {', '.join(item['refs'])}" for item in conflicts
            )
            raise SyncwheelError(
                'managed refs are already owned by another coordination domain: ' + details
            )
        return observed_state_refs

    def remote_ref_tips(self, remote, refs):
        repo_root = self.repository.root
        refs = list(dict.fromkeys(refs))
        output = {ref: None for ref in refs}
        if not refs:
            return output
        result = self.repository.git(repo_root, 'ls-remote', '--heads', remote, *refs, check=False)
        if result.returncode != 0:
            raise SyncwheelError(result.stderr.strip() or result.stdout.strip() or f'cannot inspect remote {remote}')
        for line in result.stdout.splitlines():
            sha, separator, ref = line.partition('\t')
            if separator and ref in output:
                output[ref] = sha.strip()
        return output
