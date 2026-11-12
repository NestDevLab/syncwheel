"""Derived projection identity, durable provenance and reset policy."""

import hashlib
import re

from syncwheel_errors import SyncwheelError

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


import contextlib
import json
import os
from pathlib import Path
import shlex
import tempfile
from typing import Protocol

try:
    import fcntl
except ImportError:
    fcntl = None

from syncwheel_errors import (
    DerivedProvenanceDurabilityError,
    SyncwheelError,
)
from syncwheel_files import (
    fsync_directory_path,
)
from syncwheel_manifest import (
    DERIVED_OPERATION_ID,
    coordination_config,
    coordination_is_active,
    normalize_derived_provenance,
)

DERIVED_PROJECTION_TRAILER = 'syncwheel-derived-projection'

DERIVED_PATHS_TRAILER = 'syncwheel-derived-paths'

DERIVED_PROVENANCE_STORE_VERSION = 1

DERIVED_PROVENANCE_OVERRIDE_FIELDS = {'paths', 'base_commit', 'record'}

DERIVED_PATHS_REBUILD_REASON = 'reconcile narrowed derived paths'

DERIVED_PROVENANCE_DISCARD_REASON = (
    'discard clone-local derived provenance superseded by the coordination snapshot'
)

DERIVED_PROVENANCE_RESET_REASON = 'discard an unreadable clone-local derived provenance store'


class ProvenanceRepository(Protocol):
    def branch_contains(self, *args, **kwargs): ...
    def commit_changed_files(self, *args, **kwargs): ...
    def commit_exists(self, *args, **kwargs): ...
    def commit_full_sha(self, *args, **kwargs): ...
    def commit_parent_count(self, *args, **kwargs): ...
    def commit_path_blob(self, *args, **kwargs): ...
    def git(self, *args, **kwargs): ...
    def git_common_dir(self, *args, **kwargs): ...
    def parsed_commit_trailers(self, *args, **kwargs): ...
    def ref_tip(self, *args, **kwargs): ...
    def tree_path_entry(self, *args, **kwargs): ...


class ProvenanceState(Protocol):
    def coordination_profile(self, *args, **kwargs): ...
    def coordination_state_from_commit(self, *args, **kwargs): ...


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


def projected_derived_provenance_store(
    original_store, shared, paths, record, *, expected_commit=None, coordinated=True,
):
    """Compute the existing provenance transition without persisting it."""
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
        shared, store, coordinated=coordinated
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
        return None
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
    return normalize_derived_provenance_store(updated)


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


class ProvenanceStore:
    def __init__(self, repository: ProvenanceRepository, state: ProvenanceState):
        self.repository = repository
        self.state = state


    def derived_projection_commit_paths_digest(self, repo_root, commit, paths):
        return derived_projection_paths_digest({
            path: self.repository.commit_path_blob(repo_root, commit, path)
            for path in paths
        })


    def derived_provenance_store_path(self, repo_root):
        return self.repository.git_common_dir(repo_root) / 'syncwheel' / 'derived-provenance.json'


    def derived_provenance_store_lock_path(self, repo_root):
        return self.repository.git_common_dir(repo_root) / 'syncwheel' / 'derived-provenance.lock'


    def load_derived_provenance_store(self, repo_root):
        path = self.derived_provenance_store_path(repo_root)
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


    def save_derived_provenance_store(self, repo_root, store):
        store = normalize_derived_provenance_store(store)
        path = self.derived_provenance_store_path(repo_root)
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
    def derived_provenance_store_lock(self, repo_root):
        if fcntl is None:
            raise SyncwheelError('derived provenance writes require POSIX file locking support')
        lock_path = self.derived_provenance_store_lock_path(repo_root)
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


    def local_coordination_provenance_state(self, repo_root, manifest):
        """Return the newest locally available published coordination state."""
        config = coordination_config(manifest)
        if not config or config.get('mode') != 'active-active':
            return None
        candidates = []
        remote_state_ref = f"refs/remotes/{config['remote']}/{config['state_branch']}"
        remote_tip = self.repository.ref_tip(repo_root, remote_state_ref)
        if remote_tip:
            candidates.append(remote_tip)
        _profile, local_coordination = self.state.coordination_profile(repo_root)
        seen = local_coordination.get('last_seen_state') or {}
        if seen.get('coordination_id') == config['id'] and isinstance(seen.get('state_tip'), str):
            candidates.append(seen['state_tip'])
        candidates = list(dict.fromkeys(candidates))
        available = [tip for tip in candidates if self.repository.commit_exists(repo_root, tip)]
        if not available:
            return None
        newest = available[0]
        for candidate in available[1:]:
            if self.repository.git(
                repo_root,
                'merge-base',
                '--is-ancestor',
                newest,
                candidate,
                check=False,
            ).returncode == 0:
                newest = candidate
        return self.state.coordination_state_from_commit(repo_root, newest, config['id'])


    def shared_derived_provenance_records(self, repo_root, manifest, coordination_state=None):
        """Return the active-active snapshot provenance, or an empty clone-local base."""
        active = coordination_is_active(manifest)
        state = coordination_state if active else None
        if active and state is None:
            state = self.local_coordination_provenance_state(repo_root, manifest)
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


    def derived_provenance_snapshot(self, repo_root, manifest, coordination_state=None):
        """Resolve provenance from the shared snapshot plus Git-common-dir state."""
        shared = self.shared_derived_provenance_records(
            repo_root, manifest, coordination_state
        )
        return resolve_derived_provenance_overrides(
            shared,
            self.load_derived_provenance_store(repo_root),
            coordinated=coordination_is_active(manifest),
        )


    def derived_provenance_records(self, repo_root, manifest, coordination_state=None):
        return self.derived_provenance_snapshot(repo_root, manifest, coordination_state)[0]


    def update_common_derived_provenance(
        self, repo_root,
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
        with self.derived_provenance_store_lock(repo_root):
            shared = self.shared_derived_provenance_records(
                repo_root, manifest, coordination_state
            )
            original_store = self.load_derived_provenance_store(repo_root)
            updated = projected_derived_provenance_store(
                original_store, shared, paths, record, expected_commit=expected_commit,
                coordinated=coordination_is_active(manifest),
            )
            if updated is None or updated == original_store:
                return False
            self.save_derived_provenance_store(repo_root, updated)
            return True


    def record_common_derived_provenance(
        self, repo_root, manifest, record, coordination_state=None
    ):
        normalized = normalize_derived_provenance(
            [record], label='derived provenance record'
        )[0]
        return self.update_common_derived_provenance(
            repo_root,
            manifest,
            normalized['paths'],
            normalized,
            coordination_state=coordination_state,
        )


    def resolve_common_derived_provenance(
        self, repo_root,
        manifest,
        paths,
        *,
        expected_commit=None,
        coordination_state=None,
    ):
        return self.update_common_derived_provenance(
            repo_root,
            manifest,
            sorted(paths),
            None,
            expected_commit=expected_commit,
            coordination_state=coordination_state,
        )


    def is_provenance_bound_derived_projection_commit(self, repo_root, commit, provenance):
        """Verify trailers, content, and provenance independently of the current path policy."""
        if self.repository.commit_parent_count(repo_root, commit) != 1:
            return False
        full_commit = self.repository.commit_full_sha(repo_root, commit)
        files = self.repository.commit_changed_files(repo_root, full_commit)
        if not files:
            return False
        trailers = self.repository.parsed_commit_trailers(repo_root, full_commit)
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
        content_digest = self.derived_projection_commit_paths_digest(
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


    def is_derived_projection_commit(self, repo_root, manifest, commit, provenance=None):
        """Recognize only a currently allowed, provenance-bound provider projection."""
        full_commit = self.repository.commit_full_sha(repo_root, commit)
        prefixes = manifest.get('integration', {}).get('derived_paths') or []
        files = self.repository.commit_changed_files(repo_root, full_commit)
        if not files or not prefixes or not all(
            any(path.startswith(prefix) for prefix in prefixes) for path in files
        ):
            return False
        records = (
            normalize_derived_provenance(provenance)
            if provenance is not None
            else self.derived_provenance_records(repo_root, manifest)
        )
        return self.is_provenance_bound_derived_projection_commit(
            repo_root, full_commit, records
        )


    def narrowed_derived_provenance_records(self, repo_root, manifest, provenance=None):
        """Return provenance paths excluded by the current derived-path policy."""
        prefixes = manifest.get('integration', {}).get('derived_paths') or []
        records = (
            normalize_derived_provenance(provenance)
            if provenance is not None
            else self.derived_provenance_records(repo_root, manifest)
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


    def stale_derived_projection_records(
        self, repo_root, manifest, integration_branch, provenance=None
    ):
        """Return provider paths whose recorded result is absent from integration."""
        stale = []
        records = (
            normalize_derived_provenance(provenance)
            if provenance is not None
            else self.derived_provenance_records(repo_root, manifest)
        )
        for record in records:
            reachable = self.repository.branch_contains(repo_root, integration_branch, record['commit'])
            for path in record['paths']:
                # Reconciliation preserves ancestry while replacing the product tree.
                # Reachability alone therefore no longer proves the derived result.
                if reachable and self.repository.tree_path_entry(repo_root, integration_branch, path) == self.repository.tree_path_entry(
                    repo_root, record['commit'], path
                ):
                    continue
                stale.append({**record, 'path': path})
        return sorted(stale, key=lambda item: (item['path'], item['operation_id']))
