"""In-process revision backend for exact-path, crash-safe product/control commits.

The wire protocol stays separate. Git, manifest control, ledger, guards and
provenance are explicit capabilities; replay and coordination use composed
per-repository domain services. No CLI namespace is imported.
"""

import contextlib
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Protocol

from syncwheel_replay import ReplayEngine
from syncwheel_coordination_state import CoordinationStateReader
from syncwheel_publication import PublicationCoordinator

from syncwheel_contracts import (
    MANAGED_REF_MOVE_AUTH_ENV,
    iso_utc_now,
)
from syncwheel_errors import (
    SyncwheelError,
)
from syncwheel_files import (
    _write_all,
    fsync_directory_path,
)
from syncwheel_index import (
    INDEX_OBJECT_FORMATS,
    IndexLease,
    IndexObservation,
)
from syncwheel_journal import (
    journal_path_matches,
    journal_path_sensitive,
    journal_secret_reason,
)
from syncwheel_ledger import (
    manifest_event_payload,
    manifest_stack_history_summary,
    reduce_ledger_state,
)
from syncwheel_manifest import (
    MANIFEST_VERSION_CHANNELS,
    SYNCWHEEL_TRACKING_GIT_TRACKED,
    canonical_json_digest,
    coordination_config,
    coordination_is_active,
    integration_composition_digest,
    manifest_digest,
    stack_map,
    validate_stack_dependency_graph,
)
from syncwheel_provenance import (
    derived_projection_paths_digest,
)
from syncwheel_publication import (
    local_lease_is_active,
    managed_ref_names,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class RevisionRepository(Protocol):
    def git(self, *args, **kwargs): ...
    def run(self, *args, **kwargs): ...
    def managed_process_env(self, *args, **kwargs): ...
    def with_git_identity(self, *args, **kwargs): ...
    def git_common_dir(self, *args, **kwargs): ...
    def get_current_branch(self, *args, **kwargs): ...
    def get_worktrees(self, *args, **kwargs): ...
    def branch_exists(self, *args, **kwargs): ...
    def commit_full_sha(self, *args, **kwargs): ...
    def ref_tip(self, *args, **kwargs): ...
    def ref_tree(self, *args, **kwargs): ...
    def rev_list(self, *args, **kwargs): ...
    def is_manifest_only_commit(self, *args, **kwargs): ...


class RevisionManifestStore(Protocol):
    def require_manifest(self, *args, **kwargs): ...
    def save_manifest(self, *args, **kwargs): ...
    def manifest_write_transaction(self, *args, **kwargs): ...
    def require_manifest_transaction_current(self, *args, **kwargs): ...
    def validate_manifest(self, *args, **kwargs): ...
    def missing_declared_stacks_message(self, *args, **kwargs): ...
    def handoff_misalignment_detail(self, *args, **kwargs): ...


class RevisionLedger(Protocol):
    def append_ledger_event(self, *args, **kwargs): ...
    def load_ledger_events(self, *args, **kwargs): ...
    def recover_ledger_tail(self, *args, **kwargs): ...
    def write_ledger_checkpoint(self, *args, **kwargs): ...
    def historically_abandoned_unmapped_commits(self, *args, **kwargs): ...
    def historically_reconciled_unmapped_commits(self, *args, **kwargs): ...


class RevisionGuards(Protocol):
    def active_hooks_dir(self, *args, **kwargs): ...
    def managed_push_guard_policy(self, *args, **kwargs): ...
    def authorize_ref_move(self, *args, **kwargs): ...
    def ref_auth_dir(self, *args, **kwargs): ...
    def ref_move_authorization(self, *args, **kwargs): ...


class RevisionProvenance(Protocol):
    def is_derived_projection_commit(self, *args, **kwargs): ...
    def record_common_derived_provenance(self, *args, **kwargs): ...
    def resolve_common_derived_provenance(self, *args, **kwargs): ...


class RevisionReplayFactory(Protocol):
    def for_repository(self, root) -> ReplayEngine: ...


class RevisionStateFactory(Protocol):
    def for_repository(self, root) -> CoordinationStateReader: ...


class RevisionCoordinationFactory(Protocol):
    def for_repository(self, root) -> PublicationCoordinator: ...


class SyncwheelRevisionBackend:
    """In-process facade for the Agentwheel revision-provider protocol."""

    MANIFEST_PRODUCT_PATH = '.syncwheel/manifest.json'
    INDEX_LEASE_KEYS = {
        'baseline': ('baselineIndexSha256', 'baselineIndexSemantic'),
        'product': ('productIndexSha256', 'productIndexSemantic'),
        'control': ('controlIndexSha256', 'controlIndexSemantic'),
    }
    SEMANTIC_INDEX_MATCHES_KEY = 'indexLeaseSemanticMatches'
    SEMANTIC_INDEX_MATCH_FIELDS = frozenset(
        {'stage', 'oldSha256', 'newSha256', 'semanticDigest', 'at'}
    )
    INDEX_LOCK_WAIT_SECONDS = 2.0

    def __init__(self, provider_module, *, repository: RevisionRepository,
                 manifest_store: RevisionManifestStore, ledger: RevisionLedger,
                 guards: RevisionGuards, provenance: RevisionProvenance,
                 replay_factory: RevisionReplayFactory,
                 state_factory: RevisionStateFactory,
                 coordination_factory: RevisionCoordinationFactory):
        self.provider = provider_module
        self.repository = repository
        self.manifest_store = manifest_store
        self.ledger = ledger
        self.guards = guards
        self.provenance = provenance
        self.replay_factory = replay_factory
        self.state_factory = state_factory
        self.coordination_factory = coordination_factory

    def _fail(self, message):
        raise self.provider.RevisionProviderError(message)

    def _repo_root(self, request):
        supplied = Path(request.repository_root)
        resolved = supplied.resolve(strict=False)
        if str(resolved) != request.repository_root:
            self._fail('repositoryRoot must be a canonical absolute path')
        result = self.repository.git(resolved, 'rev-parse', '--show-toplevel', check=False)
        if result.returncode != 0:
            self._fail(f'repositoryRoot is not a Git worktree: {resolved}')
        top = Path(result.stdout.strip()).resolve()
        if top != resolved:
            self._fail(f'repositoryRoot must name the worktree root: {top}')
        return resolved

    def _manifest(self, repo_root):
        try:
            return self.manifest_store.require_manifest(repo_root, str(repo_root), None, None)
        except SyncwheelError as exc:
            self._fail(str(exc))

    def _read_product_path(self, repo_root, relative):
        """Read a regular file through descriptor-bound, no-follow traversal."""
        directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        nofollow = getattr(os, 'O_NOFOLLOW', 0)
        descriptors = []
        try:
            current = os.open(str(repo_root), directory_flags | nofollow)
            descriptors.append(current)
            parts = relative.split('/')
            for part in parts[:-1]:
                try:
                    current = os.open(
                        part, directory_flags | nofollow, dir_fd=current
                    )
                except FileNotFoundError:
                    return None
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                        self._fail(
                            f'product path parent must not be a symbolic link: {relative}'
                        )
                    raise
                descriptors.append(current)
            try:
                # A FIFO must never block before fstat can reject its type.
                descriptor = os.open(
                    parts[-1], os.O_RDONLY | nofollow | os.O_NONBLOCK,
                    dir_fd=current,
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    self._fail(f'product path must not be a symbolic link: {relative}')
                raise
            descriptors.append(descriptor)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                self._fail(f'product path must be a regular file or absent: {relative}')
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            fingerprint_before = (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                before.st_ctime_ns, stat.S_IMODE(before.st_mode),
            )
            fingerprint_after = (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns, stat.S_IMODE(after.st_mode),
            )
            if fingerprint_before != fingerprint_after:
                self._fail(f'product path changed while it was being read: {relative}')
            payload = b''.join(chunks)
            return {
                'sha256': hashlib.sha256(payload).hexdigest(),
                'bytes': payload,
                'mode': '100755' if after.st_mode & 0o111 else '100644',
                'fingerprint': list(fingerprint_after),
            }
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _file_sha256(self, repo_root, relative):
        observed = self._read_product_path(repo_root, relative)
        return observed['sha256'] if observed is not None else None

    def _hash_blob_bytes(self, repo_root, payload):
        result = subprocess.run(
            ['git', 'hash-object', '-w', '--stdin'],
            cwd=repo_root,
            input=payload,
            capture_output=True,
        )
        if result.returncode != 0:
            self._fail(
                'could not persist provider blob object: '
                + result.stderr.decode('utf-8', errors='replace').strip()
            )
        return result.stdout.decode().strip()

    def _capture_path_objects(self, repo_root, request, paths=None):
        expected = {
            item.path: item.after_sha256 for item in request.paths
        }
        selected = paths or [item.path for item in request.paths]
        objects = {}
        for relative in selected:
            observed = self._read_product_path(repo_root, relative)
            declared = expected.get(relative)
            actual = observed['sha256'] if observed is not None else None
            if relative in expected and actual != declared:
                self._fail(
                    f'after SHA-256 mismatch for {relative}: '
                    f'expected {declared!r}, found {actual!r}'
                )
            if observed is None:
                objects[relative] = {
                    'sha256': None, 'blob': None, 'mode': None,
                }
            else:
                objects[relative] = {
                    'sha256': observed['sha256'],
                    'blob': self._hash_blob_bytes(repo_root, observed['bytes']),
                    'mode': observed['mode'],
                }
        return objects

    def _validate_hashes(self, repo_root, request, stage):
        attribute = 'before_sha256' if stage == 'before' else 'after_sha256'
        for item in request.paths:
            expected = getattr(item, attribute)
            actual = self._file_sha256(repo_root, item.path)
            if actual != expected:
                self._fail(
                    f'{stage} SHA-256 mismatch for {item.path}: '
                    f'expected {expected!r}, found {actual!r}'
                )

    def _head_file_sha256(self, repo_root, head, relative):
        listing = self.repository.git(repo_root, 'ls-tree', '-z', head, '--', relative, check=False)
        if listing.returncode != 0:
            self._fail(f'could not inspect {relative} at expectedHead {head}')
        entries = [entry for entry in listing.stdout.split('\0') if entry]
        if not entries:
            return None
        if len(entries) != 1:
            self._fail(f'ambiguous tree entry for product path: {relative}')
        entry = entries[0]
        metadata, separator, listed_path = entry.partition('\t')
        if not separator or listed_path != relative:
            self._fail(f'ambiguous tree entry for product path: {relative}')
        mode, object_type, object_id = metadata.split(' ', 2)
        if object_type != 'blob' or mode not in {'100644', '100755'}:
            self._fail(f'product path is not a regular file at expectedHead: {relative}')
        result = subprocess.run(
            ['git', 'cat-file', 'blob', object_id],
            cwd=repo_root,
            capture_output=True,
        )
        if result.returncode != 0:
            self._fail(f'could not read {relative} at expectedHead {head}')
        return hashlib.sha256(result.stdout).hexdigest()

    def _validate_before_hashes_at_head(self, repo_root, request):
        for item in request.paths:
            actual = self._head_file_sha256(repo_root, request.expected_head, item.path)
            if actual != item.before_sha256:
                self._fail(
                    f'before SHA-256 mismatch for {item.path} at expectedHead: '
                    f'expected {item.before_sha256!r}, found {actual!r}'
                )

    def _index_conflicts(self, repo_root):
        return bool(self.repository.git(repo_root, 'ls-files', '-u').stdout.strip())

    def _index_is_clean(self, repo_root):
        return self.repository.git(
            repo_root, 'diff', '--cached', '--quiet', check=False,
            env={'GIT_OPTIONAL_LOCKS': '0'},
        ).returncode == 0

    @contextlib.contextmanager
    def _private_index_copy(self, index_path, payload):
        """Git env that reads payload from a throwaway copy of the index.

        Kept beside the real index so a split index still finds its shared
        index. Git locks and rewrites only the copy.
        """
        descriptor, temporary_name = tempfile.mkstemp(
            prefix='index.syncwheel-observation-', dir=index_path.parent,
        )
        observed_index = Path(temporary_name)
        try:
            with os.fdopen(descriptor, 'wb') as copied:
                copied.write(payload)
            copied_bytes, _ = self._read_regular_file(observed_index, 'observational Git index')
            if copied_bytes != payload:
                self._fail('observational Git index copy differs from the real index')
            yield {
                'GIT_INDEX_FILE': str(observed_index),
                'GIT_OPTIONAL_LOCKS': '0',
            }
        finally:
            observed_index.unlink(missing_ok=True)
            Path(f'{temporary_name}.lock').unlink(missing_ok=True)

    def _index_tree(self, repo_root, observed=None):
        """Tree of the observed real-index bytes, never taking the real index.lock."""
        index_path = self._index_path(repo_root)
        if observed is None:
            payload, _ = self._read_regular_file(index_path, 'Git index')
        else:
            payload = observed.payload
        with self._private_index_copy(index_path, payload) as environment:
            return self.repository.git(repo_root, 'write-tree', env=environment).stdout.strip()

    def _dirty_paths(self, repo_root, request=None):
        paths = set()
        real_index = self._index_path(repo_root)
        index_bytes, _ = self._read_regular_file(real_index, 'Git index')
        observed = IndexObservation(index_bytes, self._index_object_format(repo_root))
        with self._private_index_copy(real_index, index_bytes) as environment:
            for arguments in (
                ('diff', '--name-only', '-z'),
                ('ls-files', '--others', '--exclude-standard', '-z'),
            ):
                output = self.repository.git(repo_root, *arguments, env=environment).stdout
                paths.update(item for item in output.split('\0') if item)
            current = self._index_observation(repo_root)
            # no request means no journal for the audit trail, so stay byte-strict
            candidates = [(
                'dirty-path observation',
                observed if request is not None else IndexLease(observed.sha256, None),
            )]
            if self._matching_index_lease(request, current, candidates) is None:
                self._fail('real Git index changed during dirty-path observation')
        return paths

    def _dirty_snapshot(self, repo_root, paths):
        snapshot = {}
        for path in sorted(paths):
            directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
            nofollow = getattr(os, 'O_NOFOLLOW', 0)
            descriptors = []
            try:
                current = os.open(str(repo_root), directory_flags | nofollow)
                descriptors.append(current)
                parts = path.split('/')
                for part in parts[:-1]:
                    current = os.open(
                        part, directory_flags | nofollow, dir_fd=current
                    )
                    descriptors.append(current)
                try:
                    before = os.stat(
                        parts[-1], dir_fd=current, follow_symlinks=False
                    )
                except FileNotFoundError:
                    snapshot[path] = None
                    continue
                if stat.S_ISREG(before.st_mode):
                    observed = self._read_product_path(repo_root, path)
                    snapshot[path] = (
                        None if observed is None else
                        ['regular', observed['sha256'], observed['mode']]
                    )
                    continue
                target = (
                    os.readlink(parts[-1], dir_fd=current)
                    if stat.S_ISLNK(before.st_mode) else None
                )
                after = os.stat(
                    parts[-1], dir_fd=current, follow_symlinks=False
                )
                fingerprint = lambda value: (
                    value.st_mode, value.st_dev, value.st_ino, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns,
                )
                if fingerprint(before) != fingerprint(after):
                    self._fail(
                        f'unowned dirty path changed while inspected: {path}'
                    )
                snapshot[path] = [
                    'symlink' if target is not None else 'special',
                    target, *fingerprint(after),
                ]
            finally:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)
        return snapshot

    def _assert_unowned_dirty_unchanged(self, repo_root, request, *, allowed_outside=()):
        allowed = {item.path for item in request.paths} | set(allowed_outside)
        outside = self._dirty_paths(repo_root, request) - allowed
        journal = self.load_journal(request)
        baseline = (journal or {}).get('baselineUnownedDirty')
        if baseline is None:
            return self._dirty_snapshot(repo_root, outside)
        actual = self._dirty_snapshot(repo_root, outside)
        if actual != baseline:
            changed = sorted(set(actual) ^ set(baseline) | {
                path for path in set(actual) & set(baseline)
                if actual[path] != baseline[path]
            })
            self._fail('unowned dirty paths changed during revision operation: '
                       + ', '.join(changed))
        return actual

    def _ensure_clean(self, repo_root):
        if self._index_conflicts(repo_root):
            self._fail('revision provider requires a conflict-free index')
        status = self.repository.git(
            repo_root,
            'status',
            '--porcelain',
            '--untracked-files=all',
            env={'GIT_OPTIONAL_LOCKS': '0'},
        ).stdout
        if status.strip():
            self._fail('revision provider preflight requires a completely clean worktree and index')

    def _ensure_after_scope(self, repo_root, request, *, allowed_outside=()):
        if self._index_conflicts(repo_root):
            self._fail('revision provider refuses an index with conflicts')
        if not self._index_is_clean(repo_root):
            self._fail('revision provider refuses pre-staged changes')
        self._validate_hashes(repo_root, request, 'after')
        self._assert_unowned_dirty_unchanged(
            repo_root, request, allowed_outside=allowed_outside
        )
        for item in request.paths:
            tracked = self.repository.git(
                repo_root, 'ls-files', '--error-unmatch', '--', item.path, check=False
            ).returncode == 0
            ignored = self.repository.git(repo_root, 'check-ignore', '-q', '--', item.path, check=False).returncode == 0
            if not tracked and ignored and item.after_sha256 is not None:
                self._fail(f'revision provider refuses an ignored product path: {item.path}')

    def _worktrees(self, repo_root):
        return sorted(str(Path(item['path']).resolve()) for item in self.repository.get_worktrees(repo_root))

    def _worktree_porcelain(self, repo_root):
        return self.repository.git(repo_root, 'worktree', 'list', '--porcelain').stdout

    def _direct_ref_observation(self, name, object_oid):
        return {
            'name': name,
            'kind': 'direct',
            'objectOid': object_oid,
            'symbolicTarget': None,
        }

    def _symbolic_ref_result(self, repo_root, name):
        return self.repository.git(
            repo_root,
            'symbolic-ref',
            '--quiet',
            '--no-recurse',
            name,
            check=False,
        )

    def _observe_ref(self, repo_root, name, *, allow_missing=True):
        if self.repository.run(['git', 'check-ref-format', name], check=False).returncode != 0:
            self._fail(f'cannot inspect invalid full ref name: {name!r}')
        symbolic = self._symbolic_ref_result(repo_root, name)
        if symbolic.returncode == 0:
            kind = 'symbolic'
            symbolic_target = symbolic.stdout.strip()
            if (
                not symbolic_target
                or self.repository.run(
                    ['git', 'check-ref-format', symbolic_target], check=False
                ).returncode != 0
            ):
                self._fail(
                    f'symbolic ref {name!r} has an invalid immediate target: '
                    f'{symbolic_target!r}'
                )
        elif symbolic.returncode == 1:
            kind = 'direct'
            symbolic_target = None
        else:
            detail = symbolic.stderr.strip() or symbolic.stdout.strip()
            suffix = f': {detail}' if detail else ''
            self._fail(
                f'could not inspect ref kind for {name!r} '
                f'(symbolic-ref exit {symbolic.returncode}){suffix}'
            )

        resolved = self.repository.git(
            repo_root,
            'show-ref',
            '--verify',
            '--hash',
            '--',
            name,
            check=False,
        )
        object_oid = resolved.stdout.strip() if resolved.returncode == 0 else ''
        if not re.fullmatch(r'[0-9a-f]{40}', object_oid):
            if kind == 'direct' and allow_missing:
                return None
            detail = resolved.stderr.strip() or resolved.stdout.strip()
            suffix = f': {detail}' if detail else ''
            self._fail(
                f'could not resolve {kind} ref {name!r} to a full object OID'
                f'{suffix}'
            )
        return {
            'name': name,
            'kind': kind,
            'objectOid': object_oid,
            'symbolicTarget': symbolic_target,
        }

    def _refs_snapshot(self, repo_root, *prefixes):
        result = self.repository.git(
            repo_root,
            'for-each-ref',
            '--format=%(refname)',
            *prefixes,
        )
        names = sorted(line for line in result.stdout.splitlines() if line)
        if len(names) != len(set(names)):
            self._fail('Git returned duplicate ref names while taking a snapshot')
        return {
            name: self._observe_ref(repo_root, name, allow_missing=False)
            for name in names
        }

    def _remote_refs(self, repo_root):
        return self._refs_snapshot(repo_root, 'refs/remotes/')

    def _managed_local_refs(self, repo_root, manifest):
        return {
            ref: self._observe_ref(repo_root, ref)
            for ref in sorted(managed_ref_names(manifest))
        }

    def _all_refs(self, repo_root):
        return self._refs_snapshot(repo_root, 'refs/')

    def _index_path(self, repo_root):
        raw = self.repository.git(repo_root, 'rev-parse', '--git-path', 'index').stdout.strip()
        path = Path(raw)
        if not path.is_absolute():
            path = repo_root / path
        return Path(os.path.abspath(path))

    def _read_regular_file(self, path, label):
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            self._fail(f'{label} does not exist: {path}')
        except OSError as exc:
            self._fail(f'could not open {label} without following links: {path}: {exc}')
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                self._fail(f'{label} is not a regular file: {path}')
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            return b''.join(chunks), metadata
        finally:
            os.close(descriptor)

    def _index_sha256(self, repo_root):
        path = self._index_path(repo_root)
        payload, _ = self._read_regular_file(path, 'Git index')
        return hashlib.sha256(payload).hexdigest()

    def _index_object_format(self, repo_root):
        formats = self.__dict__.setdefault('_index_object_formats', {})
        key = str(repo_root)
        if key not in formats:
            result = self.repository.git(
                repo_root, 'rev-parse', '--show-object-format', check=False,
                env={'GIT_OPTIONAL_LOCKS': '0'},
            )
            value = result.stdout.strip() if result.returncode == 0 else ''
            formats[key] = value if value in INDEX_OBJECT_FORMATS else None
        return formats[key]

    def _index_observation(self, repo_root):
        payload, _ = self._read_regular_file(self._index_path(repo_root), 'Git index')
        return IndexObservation(payload, self._index_object_format(repo_root))

    def _journal_index_lease(self, journal, name):
        sha_key, semantic_key = self.INDEX_LEASE_KEYS[name]
        semantic = journal.get(semantic_key)
        if semantic is not None and (
            not isinstance(semantic, str) or not self.provider.HEX_64.fullmatch(semantic)
        ):
            self._fail(f'operation journal has an invalid {semantic_key}')
        self._journal_semantic_index_matches(journal)
        return IndexLease(journal.get(sha_key), semantic)

    @staticmethod
    def _journal_has_semantic_leases(journal):
        # journals written before semantic leases existed keep byte-exact leases
        return 'baselineIndexSemantic' in journal

    def _journal_semantic_index_matches(self, journal):
        key = self.SEMANTIC_INDEX_MATCHES_KEY
        if key not in journal:
            return []
        recorded = journal[key]
        hex_64 = self.provider.HEX_64
        if not isinstance(recorded, list) or not all(
            isinstance(item, dict)
            and set(item) == self.SEMANTIC_INDEX_MATCH_FIELDS
            and isinstance(item['stage'], str) and item['stage']
            and isinstance(item['at'], str) and item['at']
            and all(
                isinstance(item[field], str) and hex_64.fullmatch(item[field])
                for field in ('oldSha256', 'newSha256', 'semanticDigest')
            )
            for item in recorded
        ):
            self._fail(f'operation journal has an invalid {key}')
        return list(recorded)

    def _matching_index_lease(self, request, observed, candidates):
        """Return the position of the first (stage, lease) the observed index holds.

        Byte equality wins. Otherwise a lease holds only when both semantic
        digests exist and agree, and that acceptance is kept as journal audit.
        """
        for position, (_, lease) in enumerate(candidates):
            if observed.sha256 == lease.sha256:
                return position
        for position, (stage, lease) in enumerate(candidates):
            expected = lease.semantic
            if expected is None or observed.semantic != expected:
                continue
            self._record_semantic_index_match(
                request, stage, lease.sha256, observed.sha256, expected
            )
            return position
        return None

    def _record_semantic_index_match(self, request, stage, old_sha, new_sha, semantic):
        pending = self.__dict__.setdefault('_semantic_index_matches', [])
        record = {
            'operationId': request.operation_id,
            'stage': stage,
            'oldSha256': old_sha,
            'newSha256': new_sha,
            'semanticDigest': semantic,
            'at': iso_utc_now(),
        }
        if not any(self._same_semantic_index_match(item, record) for item in pending):
            pending.append(record)

    @staticmethod
    def _same_semantic_index_match(first, second):
        fields = ('stage', 'oldSha256', 'newSha256', 'semanticDigest')
        return all(first.get(field) == second.get(field) for field in fields)

    def _merge_semantic_index_matches(self, request, journal):
        # callers save their own journal copies, so every save re-adds all
        # matches accepted in this process
        recorded = self._journal_semantic_index_matches(journal)
        pending = [
            item for item in self.__dict__.get('_semantic_index_matches', [])
            if item['operationId'] == request.operation_id
        ]
        if not pending:
            return
        for item in pending:
            entry = {key: value for key, value in item.items() if key != 'operationId'}
            if not any(
                self._same_semantic_index_match(existing, entry) for existing in recorded
            ):
                recorded.append(entry)
        journal[self.SEMANTIC_INDEX_MATCHES_KEY] = recorded

    def aligned_index_semantic(self, request, journal, commit):
        """Semantic lease for the index align_index wrote for commit, if any.

        Journals written before semantic leases existed stay byte-strict.
        """
        if not self._journal_has_semantic_leases(journal):
            return None
        return self.__dict__.get('_aligned_index_semantics', {}).get(commit)

    def reprepare_index_lease(self, request, journal):
        """Recover one pre-effect index lease only after a fresh full preflight."""
        repo_root = self._repo_root(request)
        old_sha = journal.get('baselineIndexSha256')
        if not isinstance(old_sha, str) or not self.provider.HEX_64.fullmatch(old_sha):
            self._fail('prepared journal has an invalid baseline index SHA-256')
        baseline = self._journal_index_lease(journal, 'baseline')
        index_lock = Path(f'{self._index_path(repo_root)}.lock')
        deadline = time.monotonic() + self.INDEX_LOCK_WAIT_SECONDS
        while True:
            current = self._index_observation(repo_root)
            # stat-only refresh stages the same content, no renewal needed
            if self._matching_index_lease(
                request, current, [('recovery preflight', baseline)]
            ) is not None:
                return
            if not self._index_lock_present(index_lock):
                break
            if not self._wait_for_foreign_index_lock(index_lock, deadline):
                self._fail(f'Git index is locked by another writer: {index_lock}')
        current_sha = current.sha256
        if request.action != 'recover' or journal.get('phase') != 'prepared':
            self._fail('index lease changed outside recoverable prepared preflight')
        if 'indexLeaseReprepared' in journal:
            self._fail('index lease changed again after one-shot recovery')
        initial = {
            'projectionRoute': None, 'derivedPaths': None,
            'derivedPathsDigest': None, 'derivedContentDigest': None,
            'productIndexSha256': None, 'controlIndexSha256': None,
            'indexAlignments': {}, 'candidateProductCommitSha': None,
            'candidateProductTreeSha': None, 'productPathObjects': None,
            'candidateDraftCommitSha': None, 'candidateDraftTreeSha': None,
            'productHooksValidated': False, 'draftRefOwned': False,
            'productCommitSha': None, 'draftStackId': None, 'draftBranch': None,
            'candidateControlCommitSha': None, 'candidateControlTreeSha': None,
            'controlPathObjects': None, 'controlHooksValidated': False,
            'manifestReplaced': False, 'ledgerAppended': False,
            'controlCommitSha': None, 'published': False, 'expiration': None,
        }
        if any(key not in journal or journal[key] != value
               for key, value in initial.items()):
            self._fail('index lease changed after revision effects began')
        if 'publicationState' in journal:
            self._fail('prepared journal contains unexpected publication evidence')
        if (
            journal.get('request') != request.intent_json()
            or journal.get('expectedHead') != request.expected_head
            or journal.get('resultingHead') != request.expected_head
        ):
            self._fail('prepared request or HEAD lease changed')
        if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
            self._fail('index lease recovery requires a clean, conflict-free index')
        observation = self.preflight(request)
        if self._index_sha256(repo_root) != current_sha:
            self._fail('index changed during fresh recovery preflight')
        leases = {
            'observedManifestDigest': 'manifestDigest',
            'manifestDigest': 'manifestDigest',
            'baselineWorktrees': 'worktrees',
            'baselineRemoteRefs': 'remoteRefs',
            'managedLocalRefs': 'managedLocalRefs',
            'refTransactionRefs': 'refTransactionRefs',
            'integrationBranch': 'integrationBranch',
            'baseRef': 'baseRef',
            'baseRefFullName': 'baseRefFullName',
            'baseRefSha': 'baseRefSha',
            'baseRefObjectSha': 'baseRefObjectSha',
            'baseRefObservation': 'baseRefObservation',
            'projectionBaseSha': 'projectionBaseSha',
            'projectionBaseKind': 'projectionBaseKind',
            'integrationCompositionDigest': 'integrationCompositionDigest',
            'baselineUnownedDirty': 'unownedDirty',
            'baselineStalePaths': 'stalePaths',
            'unmappedIntegrationCommits': 'unmappedIntegrationCommits',
            'coordination': 'coordination',
        }
        changed = [
            key for key, observed in leases.items()
            if key not in journal or journal[key] != observation.get(observed)
        ]
        known = set(initial) | set(leases) | {
            'schemaVersion', 'providerId', 'operationId', 'planDigest',
            'request', 'phase', 'expectedHead', 'resultingHead',
            'baselineIndexSha256',
        }
        optional = {'baselineIndexSemantic', self.SEMANTIC_INDEX_MATCHES_KEY}
        if not known <= set(journal) <= known | optional:
            self._fail('prepared journal has missing or unexpected evidence')
        if changed or observation['head'] != request.expected_head:
            self._fail('fresh recovery preflight changed prepared leases: '
                       + ', '.join(changed or ['head']))
        if self._index_sha256(repo_root) != current_sha:
            self._fail('index changed before recovery lease could be journaled')
        journal['indexLeaseReprepared'] = {
            'oldSha256': old_sha,
            'newSha256': current_sha,
            'at': iso_utc_now(),
        }
        journal['baselineIndexSha256'] = current_sha
        if self._journal_has_semantic_leases(journal):
            journal['baselineIndexSemantic'] = current.semantic
        self.save_journal(request, journal)
        self.checkpoint('index_lease_reprepared')

    def _resolve_base_ref(self, repo_root, manifest):
        base_ref = manifest['defaults']['base_ref']
        if re.fullmatch(r'[0-9a-f]{40}', base_ref):
            try:
                base_sha = self.repository.commit_full_sha(repo_root, base_ref)
            except SyncwheelError as exc:
                self._fail(
                    f'defaults.base_ref full SHA does not name a commit: '
                    f'{base_ref!r}: {exc}'
                )
            if base_sha != base_ref:
                self._fail(
                    'defaults.base_ref full SHA must identify a commit directly, '
                    'not an annotated tag or another peeled object'
                )
            return base_ref, None, base_sha, None, None

        if re.fullmatch(r'[0-9A-Fa-f]{4,40}', base_ref):
            self._fail(
                'defaults.base_ref must not be an abbreviated commit SHA or a '
                'non-lowercase full SHA; '
                'use the exact lowercase 40-hex commit SHA'
            )

        if base_ref.startswith('refs/'):
            candidates = [base_ref]
        elif base_ref.startswith(('heads/', 'remotes/', 'tags/')):
            candidates = [f'refs/{base_ref}']
        else:
            candidates = [
                f'refs/heads/{base_ref}',
                f'refs/remotes/{base_ref}',
                f'refs/tags/{base_ref}',
            ]

        resolved = []
        for candidate in candidates:
            if self.repository.run(['git', 'check-ref-format', candidate], check=False).returncode != 0:
                continue
            observation = self._observe_ref(repo_root, candidate)
            if observation is None:
                continue
            if observation['kind'] == 'symbolic':
                self._fail(
                    f'defaults.base_ref {base_ref!r} resolves to symbolic ref '
                    f'{candidate!r} -> {observation["symbolicTarget"]!r}; '
                    'revision-provider v1 requires a direct object ref'
                )
            resolved.append((candidate, observation))

        if not resolved:
            self._fail(
                f'defaults.base_ref {base_ref!r} must be a direct resolvable Git ref '
                'or an exact lowercase 40-hex commit SHA; revision expressions are '
                'not accepted'
            )
        if len(resolved) != 1:
            self._fail(
                f'defaults.base_ref {base_ref!r} is ambiguous between direct refs: '
                + ', '.join(candidate for candidate, _ in resolved)
                + '; use one full refs/... name'
            )
        full_name, base_ref_observation = resolved[0]
        base_ref_object_sha = base_ref_observation['objectOid']
        try:
            base_sha = self.repository.commit_full_sha(repo_root, full_name)
        except SyncwheelError as exc:
            self._fail(f'could not resolve defaults.base_ref {base_ref!r}: {exc}')
        return (
            base_ref,
            full_name,
            base_sha,
            base_ref_object_sha,
            base_ref_observation,
        )

    def _assert_base_ref_is_not_managed(self, manifest, base_ref, full_name):
        if full_name is None:
            return
        managed = set(managed_ref_names(manifest))
        if full_name in managed:
            self._fail(
                f'defaults.base_ref {base_ref!r} resolves to managed ref '
                f'{full_name!r}; a revision-provider base must not alias an '
                'integration, stack, or channel branch'
            )

    def _fresh_coordination_handoff(self, repo_root, manifest, manifest_path=None, *, fetch=True):
        if not coordination_is_active(manifest):
            return {'mode': 'disabled', 'stateTip': None, 'manifestDigest': None}
        config = coordination_config(manifest)
        try:
            _, local_coordination = self.coordination_factory.for_repository(repo_root).coordination_profile()
            pending = local_coordination.get('pending_merge')
            if isinstance(pending, dict) and pending.get('coordination_id') == config['id']:
                self._fail(
                    'active-active handoff has a pending coordination merge; resolve it first'
                )
            if local_coordination.get('locks'):
                self._fail('active-active handoff has local stack/worktree locks')
            if local_lease_is_active(local_coordination):
                self._fail('active-active handoff has an active local publication lease')
            remote = self.state_factory.for_repository(repo_root).read_remote_coordination_state(
                config,
                fetch=fetch,
                local_manifest_version=manifest['version'],
            )
            state = remote.get('state')
            if not remote.get('tip') or not state:
                self._fail(
                    'active-active handoff requires an initialized published coordination state'
                )
            current_refs = managed_ref_names(manifest)
            self.state_factory.for_repository(repo_root).require_exclusive_coordination_ownership(config, current_refs, fetch=fetch)
            local_digest = self.coordination_factory.for_repository(repo_root).coordination_manifest_digest(manifest, repo_root)
            if state.get('manifest_digest') != local_digest:
                self._fail(
                    'active-active handoff manifest is not aligned with fresh coordination state'
                    + self.manifest_store.handoff_misalignment_detail(
                        repo_root, manifest_path, manifest, config, remote
                    )
                )
            if not self.coordination_factory.for_repository(repo_root).coordination_state_matches_remote(config, state):
                self._fail(
                    'active-active handoff coordination state does not match fresh remote refs'
                )
            drifted_local = []
            integration_ref = f"refs/heads/{manifest['integration']['branch']}"
            for ref in current_refs:
                expected_tip = state.get('managed_refs', {}).get(ref)
                local_tip = self.repository.ref_tip(repo_root, ref)
                control_only_ahead = False
                if ref == integration_ref and expected_tip and local_tip:
                    ancestor = self.repository.git(
                        repo_root,
                        'merge-base',
                        '--is-ancestor',
                        expected_tip,
                        local_tip,
                        check=False,
                    )
                    if ancestor.returncode == 0:
                        ahead = self.repository.rev_list(repo_root, f'{expected_tip}..{local_tip}')
                        control_only_ahead = bool(ahead) and all(
                            self.repository.is_manifest_only_commit(repo_root, commit) for commit in ahead
                        )
                if not expected_tip or (local_tip != expected_tip and not control_only_ahead):
                    drifted_local.append(
                        f'{ref} (local {local_tip or "missing"}, state {expected_tip or "missing"})'
                    )
            if drifted_local:
                self._fail(
                    'active-active handoff local managed refs are not aligned: '
                    + '; '.join(drifted_local)
                )
            return {
                'mode': 'active-active',
                'stateTip': remote['tip'],
                'manifestDigest': state['manifest_digest'],
            }
        except SyncwheelError as exc:
            self._fail(f'active-active handoff gate failed: {exc}')

    def for_request(self, request):
        root = self._repo_root(request)
        manifest, _ = self._manifest(root)
        if manifest.get('repository_mode') == 'journal':
            return JournalRevisionBackend(self.provider, repository=self.repository, manifest_store=self.manifest_store, ledger=self.ledger, guards=self.guards, provenance=self.provenance, replay_factory=self.replay_factory, state_factory=self.state_factory, coordination_factory=self.coordination_factory)
        return self

    def _validate_repository(self, repo_root, request, *, require_clean):
        manifest, manifest_path = self._manifest(repo_root)
        if manifest.get('repository_mode') != 'delivery':
            self._fail('revision provider requires repository_mode="delivery"')
        if manifest.get('syncwheel_tracking') != SYNCWHEEL_TRACKING_GIT_TRACKED:
            self._fail('revision provider requires syncwheel_tracking="git-tracked"')
        tracked_manifest = self.repository.git(
            repo_root, 'ls-files', '--error-unmatch', '--', self.MANIFEST_PRODUCT_PATH,
            check=False,
        )
        if tracked_manifest.returncode != 0:
            self._fail('revision provider requires a tracked .syncwheel/manifest.json')
        branch = self.repository.get_current_branch(repo_root)
        integration_branch = manifest['integration']['branch']
        if branch != integration_branch:
            self._fail(
                f'revision provider requires the integration checkout: '
                f'expected {integration_branch!r}, found {branch!r}'
            )
        head = self.repository.ref_tip(repo_root, 'HEAD')
        if head != request.expected_head:
            self._fail(
                f'integration HEAD changed: expected {request.expected_head}, found {head}'
            )
        digest = manifest_digest(manifest)
        if (
            request.expected_manifest_digest is not None
            and digest != request.expected_manifest_digest
        ):
            self._fail(
                'manifest digest changed: '
                f'expected {request.expected_manifest_digest}, found {digest}'
            )
        hooks = self.guards.managed_push_guard_policy(repo_root, manifest)
        if hooks.get('required') and not hooks.get('ready') and not hooks.get('disabled'):
            self._fail(
                'managed repository guards are not ready; install or explicitly disable them '
                'before revision-provider preflight'
            )
        (
            base_ref,
            base_ref_full_name,
            base_ref_sha,
            base_ref_object_sha,
            base_ref_observation,
        ) = self._resolve_base_ref(repo_root, manifest)
        self._assert_base_ref_is_not_managed(
            manifest, base_ref, base_ref_full_name
        )
        validation = self.manifest_store.validate_manifest(repo_root, manifest)
        stale_paths = {
            item['path']
            for item in validation['details']['integration'].get(
                'derived_projection_stale'
            ) or []
        }
        declared_paths = {item.path for item in request.paths}
        repairs_stale_paths = bool(stale_paths) and (
            request.action == 'check' or bool(stale_paths & declared_paths)
        )
        blocking_errors = [
            error for error in validation['errors']
            if not (
                repairs_stale_paths
                and error.startswith('derived-projection-stale:')
            )
        ]
        if blocking_errors:
            self._fail('Syncwheel validation failed: ' + '; '.join(blocking_errors))
        missing_declared = [
            item for item in validation['details']['stacks']
            if item['id'] in manifest['integration'].get('stacks', [])
            and item.get('missing_from_integration')
        ]
        if missing_declared:
            self._fail(
                'integration declared stack(s) are missing from integration: '
                + self.manifest_store.missing_declared_stacks_message(
                    repo_root,
                    manifest_path,
                    manifest,
                    {item['id']: item['missing_from_integration'] for item in missing_declared},
                )
            )
        unmapped = list(validation['details']['integration'].get('unmapped_commits') or [])
        if unmapped:
            abandoned = self.ledger.historically_abandoned_unmapped_commits(repo_root, manifest, unmapped)
            reconciled = self.ledger.historically_reconciled_unmapped_commits(
                repo_root, manifest, unmapped
            )
            unmapped = [
                commit for commit in unmapped
                if commit not in abandoned and commit not in reconciled
            ]
            if unmapped:
                self._fail(
                    'integration already contains unmapped commits: ' + ', '.join(unmapped)
                )
        if require_clean:
            if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
                self._fail('revision provider preflight requires a clean, conflict-free index')
            forbidden = self._dirty_paths(repo_root, request) & (
                {item.path for item in request.paths} | {self.MANIFEST_PRODUCT_PATH}
            )
            if forbidden:
                self._fail('revision provider preflight has dirty owned paths: '
                           + ', '.join(sorted(forbidden)))
            self._validate_hashes(repo_root, request, 'before')
        coordination = self._fresh_coordination_handoff(repo_root, manifest, manifest_path)
        remote_refs = self._remote_refs(repo_root)
        managed_local_refs = self._managed_local_refs(repo_root, manifest)
        ref_transaction_refs = dict(managed_local_refs)
        ref_transaction_refs.update(remote_refs)
        if base_ref_full_name:
            ref_transaction_refs[base_ref_full_name] = base_ref_observation
        integration_ref = f'refs/heads/{integration_branch}'
        ref_transaction_refs[integration_ref] = self._direct_ref_observation(
            integration_ref, head
        )
        ref_transaction_refs[f'refs/heads/{request.draft_branch}'] = None
        ref_transaction_refs = self._expand_symbolic_target_leases(
            repo_root, ref_transaction_refs
        )
        worktrees = self._worktrees(repo_root)
        composition_digest = integration_composition_digest(manifest)
        index = self._index_observation(repo_root)
        return {
            'repoRoot': repo_root,
            'manifest': manifest,
            'manifestPath': manifest_path,
            'manifestDigest': digest,
            'head': head,
            'integrationBranch': integration_branch,
            'worktrees': worktrees,
            'remoteRefs': remote_refs,
            'managedLocalRefs': managed_local_refs,
            'refTransactionRefs': ref_transaction_refs,
            'baseRef': base_ref,
            'baseRefFullName': base_ref_full_name,
            'baseRefSha': base_ref_sha,
            'baseRefObjectSha': base_ref_object_sha,
            'baseRefObservation': base_ref_observation,
            'projectionBaseSha': base_ref_sha,
            'projectionBaseKind': 'manifest-base',
            'integrationCompositionDigest': composition_digest,
            'indexSha256': index.sha256,
            'indexSemantic': index.semantic,
            'unmappedIntegrationCommits': unmapped,
            'stalePaths': sorted(stale_paths),
            'coordination': coordination,
        }

    def _journal_directory(self, request):
        repo_root = self._repo_root(request)
        return self.repository.git_common_dir(repo_root) / 'syncwheel' / 'revision-provider'

    def _journal_path(self, request):
        return self._journal_directory(request) / f'{request.operation_id}.json'

    @contextlib.contextmanager
    def operation_lock(self, request):
        if fcntl is None:
            self._fail(f'revision-provider locking is unsupported on {sys.platform}')
        directory = self._journal_directory(request).parent
        directory.mkdir(parents=True, exist_ok=True)
        lock_path = directory / 'revision-provider.lock'
        with lock_path.open('a+') as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._fail(f'another revision-provider operation holds {lock_path}')
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load_journal(self, request):
        path = self._journal_path(request)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            self._fail(f'invalid revision-provider journal {path}: {exc}')
        if not isinstance(payload, dict):
            self._fail(f'invalid revision-provider journal root: {path}')
        return payload

    def _fsync_directory(self, directory):
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def save_journal(self, request, journal):
        self._merge_semantic_index_matches(request, journal)
        path = self._journal_path(request)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(journal, indent=2, sort_keys=True) + '\n'
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{request.operation_id}.', suffix='.tmp', dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'w') as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def delete_journal(self, request):
        path = self._journal_path(request)
        path.unlink(missing_ok=True)
        if path.parent.exists():
            self._fsync_directory(path.parent)

    def _derived_paths_digest(self, paths):
        return canonical_json_digest(list(paths or []))

    def _manifest_lease_digest(self, journal):
        if journal.get('manifestReplaced'):
            return journal.get('manifestDigest')
        return journal.get('observedManifestDigest')

    def _pending_operation_manifest_matches(self, request, journal, manifest):
        """Recognize the provider's own manifest write before its journal catches up."""
        if (
            journal.get('projectionRoute') not in {'manifest-base', 'resolved-manifest-base'}
            or journal.get('phase') != 'product_committed'
            or not journal.get('productCommitSha')
        ):
            return False
        existing = stack_map(manifest).get(request.draft_stack_id)
        if existing != self._desired_stack(request, journal, manifest):
            return False
        stripped = self._manifest_without_operation_stack(manifest, request)
        return manifest_digest(stripped) == journal.get('observedManifestDigest')

    def _expiration_digest_pair(self, request, journal, manifest):
        if journal.get('projectionRoute') == 'derived':
            observed_composition = journal.get('integrationCompositionDigest')
            current_composition = integration_composition_digest(manifest)
            if observed_composition != current_composition:
                return observed_composition, current_composition
            observed_paths = journal.get('derivedPathsDigest')
            current_paths = self._derived_paths_digest(
                manifest['integration'].get('derived_paths') or []
            )
            return observed_paths, current_paths
        return self._manifest_lease_digest(journal), manifest_digest(manifest)

    def _expire_if_operation_lease_changed(self, request, journal, manifest):
        if journal.get('phase') in {'verified', 'expired'}:
            return
        route = journal.get('projectionRoute')
        if route == 'derived':
            current_composition = integration_composition_digest(manifest)
            if current_composition != journal.get('integrationCompositionDigest'):
                self.expire_manifest_invalidated(
                    request,
                    journal,
                    'integration composition changed while derived projection was pending',
                )
            current_paths = list(manifest['integration'].get('derived_paths') or [])
            if (
                current_paths != list(journal.get('derivedPaths') or [])
                or self._derived_paths_digest(current_paths)
                != journal.get('derivedPathsDigest')
            ):
                self.expire_manifest_invalidated(
                    request,
                    journal,
                    'integration.derived_paths changed while derived projection was pending',
                )
            return
        current_digest = manifest_digest(manifest)
        if current_digest == self._manifest_lease_digest(journal):
            return
        if self._pending_operation_manifest_matches(
            request, journal, manifest
        ):
            return
        reason = (
            'manifest changed after preflight'
            if route is None
            else 'manifest changed while revision-provider operation was pending'
        )
        self.expire_manifest_invalidated(request, journal, reason)

    def expire_manifest_invalidated(self, request, journal, reason):
        """Terminally expire a receipt whose manifest lease can no longer recover.

        This deliberately changes only local provider state.  The caller must
        start a new Agentwheel update, which obtains a fresh manifest lease.
        """
        remedy = 'run a new Agentwheel update'
        repo_root = self._repo_root(request)
        manifest, manifest_path = self._manifest(repo_root)
        observed_digest, current_digest = self._expiration_digest_pair(
            request, journal, manifest
        )
        expiration = journal.get('expiration') or {
            'reason': reason,
            'remedy': remedy,
            'observedDigest': observed_digest,
            'currentDigest': current_digest,
            'decidedAt': iso_utc_now(),
        }
        event_payload = {
            'operation_id': request.operation_id,
            'reason': expiration['reason'],
            'remedy': expiration['remedy'],
            'observed_digest': expiration['observedDigest'],
            'current_digest': expiration['currentDigest'],
            'decided_at': expiration['decidedAt'],
        }
        if journal.get('phase') != 'expired':
            journal['phase'] = 'expired'
            journal['expiration'] = expiration
            self.save_journal(request, journal)
            self.checkpoint('receipt_expired')
        self.ledger.recover_ledger_tail(repo_root, manifest_path)
        matches = [
            event for event in self.ledger.load_ledger_events(repo_root, manifest_path)
            if event.get('type') == 'revision_provider_expired'
            and (event.get('payload') or {}).get('operation_id') == request.operation_id
        ]
        if matches and (len(matches) != 1 or matches[0].get('payload') != event_payload):
            self._fail(
                f'ledger collision while expiring revision-provider operation '
                f'{request.operation_id}'
            )
        if not matches:
            self.ledger.append_ledger_event(
                repo_root, 'revision_provider_expired', event_payload, manifest_path
            )
            self.checkpoint('expiration_ledger_event_written')
        self._fail(
            f'operation {request.operation_id} expired: {expiration["reason"]}; {expiration["remedy"]}'
        )

    def check(self, request):
        observation = self._validate_repository(
            self._repo_root(request), request, require_clean=True
        )
        stacks = stack_map(observation['manifest'])
        if request.draft_stack_id in stacks:
            self._fail(f'draft stack id already exists: {request.draft_stack_id}')
        if any(
            stack['branch'] == request.draft_branch
            for stack in observation['manifest']['stacks']
        ) or self.repository.branch_exists(observation['repoRoot'], request.draft_branch):
            self._fail(f'draft branch already exists: {request.draft_branch}')
        return observation

    def preflight(self, request):
        observation = self._validate_repository(
            self._repo_root(request), request, require_clean=False
        )
        self._validate_before_hashes_at_head(observation['repoRoot'], request)
        self._ensure_after_scope(observation['repoRoot'], request)
        observation['unownedDirty'] = self._assert_unowned_dirty_unchanged(
            observation['repoRoot'], request
        )
        stacks = stack_map(observation['manifest'])
        if request.draft_stack_id in stacks:
            self._fail(f'draft stack id already exists: {request.draft_stack_id}')
        if any(
            stack['branch'] == request.draft_branch
            for stack in observation['manifest']['stacks']
        ) or self.repository.branch_exists(observation['repoRoot'], request.draft_branch):
            self._fail(f'draft branch already exists: {request.draft_branch}')
        return observation

    def verify_after_paths(self, request):
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        if self.repository.get_current_branch(repo_root) != manifest['integration']['branch']:
            self._fail('integration branch changed after preflight')
        if self.repository.ref_tip(repo_root, 'HEAD') != request.expected_head:
            self._fail('integration HEAD changed after preflight')
        journal = self.load_journal(request)
        if journal:
            self._expire_if_operation_lease_changed(request, journal, manifest)
            expected_refs = dict(journal['managedLocalRefs'])
            expected_refs.update(journal['baselineRemoteRefs'])
            if journal.get('baseRefFullName'):
                expected_refs[journal['baseRefFullName']] = journal[
                    'baseRefObservation'
                ]
            expected_refs[f'refs/heads/{request.draft_branch}'] = None
            self._assert_ref_leases(repo_root, expected_refs)
            self._assert_index_lease(
                repo_root, request, journal, 'baseline', 'preflight'
            )
        self._ensure_after_scope(repo_root, request)

    def _prepare_exact_commit(self, repo_root, parent, path_objects, message):
        descriptor, index_name = tempfile.mkstemp(prefix='syncwheel-revision-index-')
        os.close(descriptor)
        index_path = Path(index_name)
        index_path.unlink(missing_ok=True)
        environment = {'GIT_INDEX_FILE': str(index_path)}
        try:
            self.repository.git(repo_root, 'read-tree', parent, env=environment)
            for relative, entry in sorted(path_objects.items()):
                if entry['blob'] is None:
                    self.repository.git(
                        repo_root, 'update-index', '--force-remove', '--', relative,
                        env=environment, check=False,
                    )
                    continue
                self.repository.git(
                    repo_root,
                    'update-index',
                    '--add',
                    '--cacheinfo',
                    f"{entry['mode']},{entry['blob']},{relative}",
                    env=environment,
                )
            tree = self.repository.git(repo_root, 'write-tree', env=environment).stdout.strip()
            parent_tree = self.repository.ref_tree(repo_root, parent)
            if tree == parent_tree:
                return None
            command = self.repository.with_git_identity(
                repo_root,
                ['git', 'commit-tree', tree, '-p', parent, '-F', '-'],
            )
            commit = self.repository.run(command, cwd=repo_root, input_text=message).stdout.strip()
            changed = set(
                item
                for item in self.repository.git(
                    repo_root,
                    'diff-tree',
                    '--no-commit-id',
                    '--name-only',
                    '-r',
                    '-z',
                    parent,
                    commit,
                ).stdout.split('\0')
                if item
            )
            if not changed or changed != set(path_objects):
                self._fail('prepared commit escaped the exact path allowlist')
            return {'commit': commit, 'tree': tree, 'pathObjects': path_objects}
        finally:
            index_path.unlink(missing_ok=True)

    def prepare_product_commit(self, request, message):
        repo_root = self._repo_root(request)
        self._ensure_after_scope(repo_root, request)
        path_objects = self._capture_path_objects(repo_root, request)
        prepared = self._prepare_exact_commit(
            repo_root,
            request.expected_head,
            path_objects,
            message,
        )
        if prepared is None:
            return None
        return {
            'candidateProductCommitSha': prepared['commit'],
            'candidateProductTreeSha': prepared['tree'],
            'productPathObjects': prepared['pathObjects'],
        }

    def _projection_reproduces_product_blobs(self, repo_root, projection, path_objects):
        if projection.get('status') != 'projected':
            return False
        for path, expected in path_objects.items():
            actual = self._tree_entry(repo_root, projection['tip'], path)
            actual_blob = actual['blob'] if actual is not None else None
            if actual_blob != expected['blob']:
                return False
        return True

    def _journal_product_path_objects(self, request, journal):
        path_objects = journal.get('productPathObjects')
        expected_paths = sorted(item.path for item in request.paths)
        valid = (
            isinstance(path_objects, dict)
            and sorted(path_objects) == expected_paths
            and bool(path_objects)
        )
        if valid:
            for value in path_objects.values():
                if not isinstance(value, dict) or set(value) != {
                    'sha256', 'blob', 'mode'
                }:
                    valid = False
                    break
                sha256 = value.get('sha256')
                blob = value.get('blob')
                mode = value.get('mode')
                deletion = sha256 is None and blob is None and mode is None
                regular = (
                    isinstance(sha256, str)
                    and re.fullmatch(r'[0-9a-f]{64}', sha256)
                    and isinstance(blob, str)
                    and re.fullmatch(r'[0-9a-f]{40,64}', blob)
                    and mode in {'100644', '100755'}
                )
                if not deletion and not regular:
                    valid = False
                    break
        if not valid:
            self._fail(
                'journaled productPathObjects is missing or invalid; release the '
                'prepared operation, restore the declared paths if needed, then run '
                'a new Agentwheel update'
            )
        return path_objects

    def _prepared_resolution_binding(self, request, journal, resolution, reason, leases):
        return {
            'repositoryRoot': request.repository_root,
            'operationId': request.operation_id,
            'intentDigest': request.plan_digest,
            'candidateProductCommitSha': journal.get('candidateProductCommitSha'),
            'candidateProductTreeSha': journal.get('candidateProductTreeSha'),
            'baseRefSha': journal.get('baseRefSha'),
            'resolutionCommitSha': resolution,
            'resolutionTreeSha': self.repository.ref_tree(self._repo_root(request), resolution),
            'productPathObjects': self._journal_product_path_objects(request, journal),
            'reason': reason,
            'leases': leases,
        }

    def _prepared_resolution_audit(self, request, witness, *, complete=False):
        root = self._repo_root(request)
        _, manifest_path = self._manifest(root)
        if complete:
            self.ledger.recover_ledger_tail(root, manifest_path)
        matches = [
            event for event in self.ledger.load_ledger_events(root, manifest_path)
            if event.get('type') == 'revision_provider_prepared_resolution'
            and (event.get('payload') or {}).get('binding', {}).get('operationId')
            == request.operation_id
        ]
        if matches and (len(matches) != 1 or matches[0].get('payload') != witness):
            self._fail('prepared resolution audit collision')
        if not matches:
            if complete is False:
                self._fail('prepared resolution audit is incomplete; retry resolve-prepared')
            if complete:
                self.ledger.append_ledger_event(
                    root, 'revision_provider_prepared_resolution', witness, manifest_path
                )
                self.checkpoint('prepared_resolution_audited')

    def _prepared_resolution_projection(self, request, journal, *, complete_audit=False):
        """One immutable proof shared by preparation and every draft verifier."""
        witness = journal.get('preparedResolution')
        if not isinstance(witness, dict) or set(witness) != {'binding', 'planDigest'}:
            self._fail('prepared resolution witness is missing or invalid')
        binding = witness['binding']
        if not isinstance(binding, dict):
            self._fail('prepared resolution binding is invalid')
        resolution = binding.get('resolutionCommitSha')
        reason = binding.get('reason')
        if (not isinstance(resolution, str)
                or not self.provider.HEX_40.fullmatch(resolution)
                or not isinstance(reason, str) or not reason.strip()
                or not isinstance(binding.get('leases'), dict)):
            self._fail('prepared resolution identity is invalid')
        expected = self._prepared_resolution_binding(
            request, journal, resolution, reason, binding['leases']
        )
        if binding != expected or canonical_json_digest(binding) != witness['planDigest']:
            self._fail('prepared resolution witness changed')
        parents = self.repository.git(self._repo_root(request), 'rev-list', '--parents', '-n', '1',
                      resolution, check=False)
        if parents.returncode or parents.stdout.split() != [resolution, journal['baseRefSha']]:
            self._fail('prepared resolution requires exactly one parent equal to baseRefSha')
        self._verify_candidate_object(
            self._repo_root(request), resolution, binding['resolutionTreeSha'],
            journal['baseRefSha'], binding['productPathObjects'],
        )
        if self.replay_factory.for_repository(self._repo_root(request)).deterministic_stack_replay_tip(journal['baseRefSha'], [resolution]) != resolution:
            self._fail('prepared resolution replay does not reproduce its exact commit tip')
        if complete_audit is not None:
            self._prepared_resolution_audit(request, witness, complete=complete_audit)
        return {
            'projectionRoute': 'resolved-manifest-base',
            'candidateDraftCommitSha': resolution,
            'candidateDraftTreeSha': binding['resolutionTreeSha'],
        }

    def _prepared_resolution_leases(self, request, journal):
        """Observe pending-product leases without expiring or advancing anything."""
        root = self._repo_root(request)
        manifest, _ = self._manifest(root)
        self.provider._assert_matching_journal(request, journal)
        if (manifest.get('repository_mode') != 'delivery'
                or manifest.get('syncwheel_tracking') != SYNCWHEEL_TRACKING_GIT_TRACKED
                or journal.get('repositoryMode') == 'journal'
                or journal.get('phase') != 'prepared' or request.no_commit
                or journal.get('operationId') != request.operation_id
                or journal.get('projectionRoute') is not None
                or not journal.get('candidateProductCommitSha')
                or journal.get('request') != request.intent_json()
                or journal.get('expectedHead') != request.expected_head
                or journal.get('resultingHead') != request.expected_head):
            self._fail('resolve-prepared requires an unchanged prepared delivery operation with no route')
        if not all(isinstance(journal.get(key), str) and self.provider.HEX_40.fullmatch(journal[key])
                   for key in ('candidateProductCommitSha', 'candidateProductTreeSha', 'baseRefSha')):
            self._fail('prepared resolution requires immutable full product/base object IDs')
        if any(journal.get(key) for key in (
                'candidateDraftCommitSha', 'candidateDraftTreeSha', 'draftRefOwned',
                'productCommitSha', 'draftStackId', 'draftBranch',
                'candidateControlCommitSha', 'candidateControlTreeSha',
                'controlPathObjects', 'controlHooksValidated', 'manifestReplaced',
                'ledgerAppended', 'controlCommitSha', 'published', 'expiration',
                'publicationState')):
            self._fail('resolve-prepared refuses an operation with revision effects')
        if (manifest_digest(manifest) != journal.get('observedManifestDigest')
                or manifest_digest(manifest) != journal.get('manifestDigest')
                or self.repository.get_current_branch(root) != journal.get('integrationBranch')
                or manifest['integration']['branch'] != journal.get('integrationBranch')
                or self.repository.ref_tip(root, 'HEAD') != request.expected_head):
            self._fail('prepared resolution manifest, branch or HEAD lease changed')
        hooks = self.guards.managed_push_guard_policy(root, manifest)
        if hooks.get('required') and not hooks.get('ready') and not hooks.get('disabled'):
            self._fail('managed repository guards are not ready')
        if self.repository.git(root, 'ls-files', '--error-unmatch', '--', self.MANIFEST_PRODUCT_PATH,
               check=False).returncode:
            self._fail('prepared resolution requires a tracked manifest')
        base = self._resolve_base_ref(root, manifest)
        if base != tuple(journal.get(key) for key in (
                'baseRef', 'baseRefFullName', 'baseRefSha',
                'baseRefObjectSha', 'baseRefObservation')):
            self._fail('prepared resolution base lease changed')
        self._assert_base_ref_is_not_managed(manifest, base[0], base[1])
        if (journal.get('projectionBaseSha') != base[2]
                or journal.get('projectionBaseKind') != 'manifest-base'
                or journal.get('integrationCompositionDigest') != integration_composition_digest(manifest)):
            self._fail('prepared resolution projection inputs changed')
        # Read fresh remote state without fetching or updating any refs.
        coordination = self._fresh_coordination_handoff(root, manifest, fetch=False)
        if coordination != journal.get('coordination'):
            self._fail('prepared resolution coordination lease changed')
        refs = self._journal_ref_transaction_refs(request, journal)
        self._assert_no_ref_transaction_locks(root, refs)
        self._assert_ref_leases(root, refs)
        if (self._worktrees(root) != journal.get('baselineWorktrees')
                or self._remote_refs(root) != journal.get('baselineRemoteRefs')
                or self._managed_local_refs(root, manifest) != journal.get('managedLocalRefs')):
            self._fail('prepared resolution worktree or ref membership lease changed')
        self._assert_index_lease(root, request, journal, 'baseline', 'prepared resolution')
        self._validate_before_hashes_at_head(root, request)
        self._ensure_after_scope(root, request)
        objects = self._journal_product_path_objects(request, journal)
        if any(objects[item.path]['sha256'] != item.after_sha256 for item in request.paths):
            self._fail('prepared product path hashes differ from the operation intent')
        self._verify_candidate_object(
            root, journal['candidateProductCommitSha'], journal['candidateProductTreeSha'],
            request.expected_head, objects,
        )
        self._verify_worktree_objects(root, objects)
        index = self._index_observation(root)
        return {
            'manifestDigest': manifest_digest(manifest),
            'head': request.expected_head,
            'refs': refs,
            'worktrees': journal['baselineWorktrees'],
            'indexSha256': index.sha256,
            'indexSemantic': index.semantic,
            'unownedDirty': self._assert_unowned_dirty_unchanged(root, request),
            'coordination': coordination,
        }

    def resolve_prepared(self, request, resolution, reason, *, apply=False, plan_digest=None):
        # Caller holds the same operation lock as finalize/recover. No generic
        # dirty-worktree exemption: only the original exact pending paths pass.
        if (not isinstance(resolution, str) or not self.provider.HEX_40.fullmatch(resolution)
                or not isinstance(reason, str) or not reason.strip()
                or len(reason) > 4096 or self.provider.REASON_CONTROL_CHARACTER.search(reason)):
            self._fail('resolve-prepared requires a full commit OID and nonempty reason')
        journal = self.load_journal(request)
        if journal is None:
            self._fail('resolve-prepared requires an existing prepared journal')
        leases = self._prepared_resolution_leases(request, journal)
        binding = self._prepared_resolution_binding(request, journal, resolution, reason, leases)
        witness = {'binding': binding, 'planDigest': canonical_json_digest(binding)}
        previous = journal.get('preparedResolution')
        if previous is not None and previous != witness:
            self._fail('a different prepared resolution is already bound to this operation')
        candidate = dict(journal, preparedResolution=witness)
        # One proof validates the object in plans and throughout recovery.
        self._prepared_resolution_projection(request, candidate, complete_audit=None)
        if previous is None:
            self._prepared_resolution_audit(request, witness, complete=None)
        if apply:
            if plan_digest != witness['planDigest']:
                self._fail('prepared resolution plan digest changed; review a fresh plan')
            # A final fresh lease observation is a CAS immediately before the
            # durable decision. Ref publication still uses its own atomic CAS.
            if self._prepared_resolution_leases(request, journal) != leases:
                self._fail('prepared resolution leases changed during apply')
            if previous is None:
                self.save_journal(request, candidate)
                self.checkpoint('prepared_resolution_written')
            self._prepared_resolution_projection(request, candidate, complete_audit=True)
        return {'operationId': request.operation_id, 'planDigest': witness['planDigest'],
                'resolution': binding, 'applied': apply,
                'nextAction': 'recover the same operation; resolution does not finalize or publish'}

    def prepare_draft_projection(self, request, journal):
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        if manifest_digest(manifest) != journal['observedManifestDigest']:
            self.expire_manifest_invalidated(
                request,
                journal,
                'manifest changed before draft object preparation',
            )
        if journal.get('preparedResolution') is not None:
            return self._prepared_resolution_projection(request, journal, complete_audit=True)
        projection = self.replay_factory.for_repository(repo_root).deterministic_stack_projection(
            journal['baseRefSha'],
            [journal['candidateProductCommitSha']],
        )
        reproduces_product = self._projection_reproduces_product_blobs(
            repo_root,
            projection,
            journal['productPathObjects'],
        )
        if reproduces_product:
            return {
                'projectionRoute': 'manifest-base',
                'candidateDraftCommitSha': projection['tip'],
                'candidateDraftTreeSha': projection['tree'],
            }
        prefixes = manifest['integration'].get('derived_paths') or []
        paths = sorted(journal['productPathObjects'])
        if not prefixes or not all(any(path.startswith(prefix) for prefix in prefixes) for path in paths):
            conflicts = ', '.join(projection.get('paths') or []) or 'unknown path(s)'
            self._fail(
                'product commit has a conflicting draft projection; conflicts: '
                f'{conflicts}; base {projection.get("base", journal["baseRefSha"])}; '
                'derived route refuses paths outside integration.derived_paths'
            )
        content_digest = derived_projection_paths_digest({
            path: journal['productPathObjects'][path]['blob']
            for path in paths
        })
        prepared = self._prepare_exact_commit(
            repo_root,
            request.expected_head,
            journal['productPathObjects'],
            self.provider.product_commit_message(request)
            + f'Syncwheel-Derived-Projection: {request.operation_id}\n'
            + f'Syncwheel-Derived-Paths: {content_digest}\n',
        )
        if prepared is None:
            self._fail('derived projection unexpectedly has no product delta')
        return {
            'projectionRoute': 'derived',
            'candidateProductCommitSha': prepared['commit'],
            'candidateProductTreeSha': prepared['tree'],
            'integrationCompositionDigest': integration_composition_digest(manifest),
            'derivedPaths': list(prefixes),
            'derivedPathsDigest': self._derived_paths_digest(prefixes),
            'derivedContentDigest': content_digest,
        }

    def verify_projection_route(self, request, journal):
        """Recompute the persisted route without replacing its candidate objects."""
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        self._expire_if_operation_lease_changed(request, journal, manifest)
        path_objects = self._journal_product_path_objects(request, journal)
        if journal.get('preparedResolution') is not None or journal.get('projectionRoute') == 'resolved-manifest-base':
            expected = self._prepared_resolution_projection(request, journal)
            if any(journal.get(key) != value for key, value in expected.items()):
                self._fail('journaled explicit projection route changed')
            return
        projection = self.replay_factory.for_repository(repo_root).deterministic_stack_projection(
            journal['baseRefSha'],
            [journal['candidateProductCommitSha']],
        )
        reproduces_product = self._projection_reproduces_product_blobs(
            repo_root,
            projection,
            path_objects,
        )
        recomputed_route = 'manifest-base' if reproduces_product else 'derived'
        if recomputed_route != journal.get('projectionRoute'):
            self._fail(
                'journaled projection route no longer matches its immutable candidate'
            )
        if recomputed_route == 'manifest-base':
            if (
                projection.get('tip') != journal.get('candidateDraftCommitSha')
                or projection.get('tree') != journal.get('candidateDraftTreeSha')
            ):
                self._fail('journaled draft projection object changed')
            return
        if journal.get('candidateDraftCommitSha') is not None:
            self._fail('journaled derived route unexpectedly owns a draft candidate')
        paths = sorted(path_objects)
        content_digest = derived_projection_paths_digest({
            path: path_objects[path]['blob']
            for path in paths
        })
        if content_digest != journal.get('derivedContentDigest'):
            self._fail('journaled derived path/content digest changed')
        candidate_provenance = [{
            'operation_id': request.operation_id,
            'commit': journal['candidateProductCommitSha'],
            'paths': paths,
            'paths_digest': content_digest,
            'composition_digest': journal['integrationCompositionDigest'],
        }]
        if not self.provenance.is_derived_projection_commit(
            repo_root,
            manifest,
            journal['candidateProductCommitSha'],
            provenance=candidate_provenance,
        ):
            self._fail(
                'journaled derived candidate is missing its path or trailer ownership proof'
            )

    def current_head(self, request):
        return self.repository.ref_tip(self._repo_root(request), 'HEAD')

    def _tree_entry(self, repo_root, commit, relative):
        listing = self.repository.git(repo_root, 'ls-tree', '-z', commit, '--', relative, check=False)
        if listing.returncode != 0:
            self._fail(f'could not inspect candidate tree path: {relative}')
        entries = [entry for entry in listing.stdout.split('\0') if entry]
        if not entries:
            return None
        if len(entries) != 1:
            self._fail(f'ambiguous candidate tree entry: {relative}')
        entry = entries[0]
        metadata, separator, listed_path = entry.partition('\t')
        if not separator or listed_path != relative:
            self._fail(f'ambiguous candidate tree entry: {relative}')
        mode, object_type, object_id = metadata.split(' ', 2)
        if object_type != 'blob' or mode not in {'100644', '100755'}:
            self._fail(f'candidate tree path is not a regular blob: {relative}')
        return {'mode': mode, 'blob': object_id}

    def _verify_candidate_object(
        self, repo_root, commit, tree, parent, path_objects
    ):
        if self.repository.ref_tip(repo_root, f'{commit}^') != parent:
            self._fail('journaled candidate parent changed or is unavailable')
        if self.repository.ref_tree(repo_root, commit) != tree:
            self._fail('journaled candidate tree does not match its object id')
        changed = {
            item for item in self.repository.git(
                repo_root, 'diff-tree', '--no-commit-id', '--name-only', '-r', '-z',
                parent, commit,
            ).stdout.split('\0') if item
        }
        if changed != set(path_objects):
            self._fail('journaled candidate tree escaped the exact path allowlist')
        for relative, expected in path_objects.items():
            actual = self._tree_entry(repo_root, commit, relative)
            if expected['blob'] is None:
                if actual is not None:
                    self._fail(f'candidate did not delete declared path: {relative}')
                continue
            if actual != {'mode': expected['mode'], 'blob': expected['blob']}:
                self._fail(f'candidate blob or mode lease failed for {relative}')
            result = subprocess.run(
                ['git', 'cat-file', 'blob', expected['blob']],
                cwd=repo_root,
                capture_output=True,
            )
            if result.returncode != 0:
                self._fail(f'candidate blob is unavailable for {relative}')
            if hashlib.sha256(result.stdout).hexdigest() != expected['sha256']:
                self._fail(f'candidate blob bytes do not match declared hash for {relative}')

    def _verify_worktree_objects(self, repo_root, path_objects):
        for relative, expected in path_objects.items():
            observed = self._read_product_path(repo_root, relative)
            if expected['blob'] is None:
                if observed is not None:
                    self._fail(f'declared deletion was replaced before ref update: {relative}')
                continue
            if observed is None:
                self._fail(f'declared file disappeared before ref update: {relative}')
            if (
                observed['sha256'] != expected['sha256']
                or observed['mode'] != expected['mode']
            ):
                self._fail(f'worktree bytes or mode changed before ref update: {relative}')

    def _expected_managed_refs(self, request, journal, integration_tip, *, draft_owned):
        expected = dict(journal['managedLocalRefs'])
        expected.update(journal['baselineRemoteRefs'])
        if journal.get('baseRefFullName'):
            expected[journal['baseRefFullName']] = journal['baseRefObservation']
        manifest, _ = self._manifest(self._repo_root(request))
        integration_ref = f"refs/heads/{manifest['integration']['branch']}"
        expected[integration_ref] = self._direct_ref_observation(
            integration_ref, integration_tip
        )
        draft_ref = f'refs/heads/{request.draft_branch}'
        expected[draft_ref] = (
            self._direct_ref_observation(
                draft_ref, journal['candidateDraftCommitSha']
            )
            if draft_owned
            else None
        )
        return expected

    def _assert_operation_worktree(self, repo_root, request, journal, kind):
        manifest, _ = self._manifest(repo_root)
        if kind == 'product':
            self._expire_if_operation_lease_changed(
                request, journal, manifest
            )
            self._ensure_after_scope(
                repo_root,
                request,
                allowed_outside=(
                    (self.MANIFEST_PRODUCT_PATH,)
                    if journal.get('projectionRoute') == 'derived'
                    else ()
                ),
            )
            return
        if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
            self._fail('control ref update requires a clean, conflict-free index')
        dirty = self._dirty_paths(repo_root, request)
        if self.MANIFEST_PRODUCT_PATH not in dirty:
            self._fail(
                'control ref update requires .syncwheel/manifest.json; found: '
                + ', '.join(sorted(dirty))
            )
        self._assert_unowned_dirty_unchanged(
            repo_root, request, allowed_outside=(self.MANIFEST_PRODUCT_PATH,)
        )
        self._expire_if_operation_lease_changed(request, journal, manifest)

    def _assert_ref_leases(self, repo_root, expected):
        drift = []
        for ref, observation in sorted(expected.items()):
            actual = self._observe_ref(repo_root, ref)
            if actual != observation:
                drift.append(
                    f'{ref} (expected {observation or "missing"}, '
                    f'found {actual or "missing"})'
                )
        if drift:
            self._fail('managed local ref lease was lost: ' + '; '.join(drift))

    def _assert_index_lease(self, repo_root, request, journal, lease_name, stage):
        lease = self._journal_index_lease(journal, lease_name)
        if not isinstance(lease.sha256, str) or not self.provider.HEX_64.fullmatch(
            lease.sha256
        ):
            self._fail(f'{stage} index lease is missing from the operation journal')
        observed = self._index_observation(repo_root)
        if self._matching_index_lease(request, observed, [(stage, lease)]) is None:
            self._fail(
                f'real Git index lease was lost before {stage}: '
                f'expected {lease.sha256}, found {observed.sha256}'
            )

    def _expand_symbolic_target_leases(self, repo_root, expected):
        expanded = dict(expected)
        pending = [
            observation
            for observation in expanded.values()
            if observation is not None and observation['kind'] == 'symbolic'
        ]
        visited = set()
        while pending:
            symbolic = pending.pop()
            name = symbolic['name']
            if name in visited:
                continue
            visited.add(name)
            target = symbolic['symbolicTarget']
            target_observation = expanded.get(target)
            if target not in expanded:
                target_observation = self._observe_ref(
                    repo_root, target, allow_missing=False
                )
                expanded[target] = target_observation
            if target_observation is None:
                self._fail(
                    f'symbolic ref lease target disappeared: {name!r} -> {target!r}'
                )
            if target_observation['objectOid'] != symbolic['objectOid']:
                self._fail(
                    f'symbolic ref lease object differs from its immediate target: '
                    f'{name!r} -> {target!r}'
                )
            if target_observation['kind'] == 'symbolic':
                pending.append(target_observation)
        return expanded

    def _journal_ref_transaction_refs(self, request, journal):
        raw = journal.get('refTransactionRefs')
        if not isinstance(raw, dict) or not raw:
            self._fail(
                'operation journal has no complete ref-transaction observation set'
            )
        integration_branch = journal.get('integrationBranch')
        if (
            not isinstance(integration_branch, str)
            or self.repository.run(
                ['git', 'check-ref-format', '--branch', integration_branch],
                check=False,
            ).returncode != 0
        ):
            self._fail('operation journal has an invalid integration branch')

        required = {
            f'refs/heads/{integration_branch}',
            f'refs/heads/{request.draft_branch}',
        }
        base_ref = journal.get('baseRefFullName')
        if base_ref is not None:
            if not isinstance(base_ref, str):
                self._fail('operation journal has an invalid base ref name')
            required.add(base_ref)
        for field in ('managedLocalRefs', 'baselineRemoteRefs'):
            source = journal.get(field)
            if not isinstance(source, dict):
                self._fail(f'operation journal has an invalid {field} snapshot')
            required.update(source)
            for name, observation in source.items():
                if name not in raw or raw[name] != observation:
                    self._fail(
                        f'operation journal ref-transaction snapshot omitted or '
                        f'changed {name!r} from {field}'
                    )
        if base_ref is not None and raw.get(base_ref) != journal.get(
            'baseRefObservation'
        ):
            self._fail(
                'operation journal ref-transaction snapshot changed its base ref'
            )
        missing = sorted(required.difference(raw))
        if missing:
            self._fail(
                'operation journal ref-transaction snapshot omitted required refs: '
                + ', '.join(missing)
            )

        observations = {}
        fields = {'name', 'kind', 'objectOid', 'symbolicTarget'}
        for name, observation in raw.items():
            if (
                not isinstance(name, str)
                or self.repository.run(['git', 'check-ref-format', name], check=False).returncode != 0
            ):
                self._fail(
                    f'operation journal contains an invalid ref-transaction name: '
                    f'{name!r}'
                )
            if observation is None:
                observations[name] = None
                continue
            if not isinstance(observation, dict) or set(observation) != fields:
                self._fail(
                    f'operation journal contains an invalid typed ref observation '
                    f'for {name!r}'
                )
            kind = observation.get('kind')
            target = observation.get('symbolicTarget')
            if (
                observation.get('name') != name
                or kind not in {'direct', 'symbolic'}
                or not re.fullmatch(r'[0-9a-f]{40}', observation.get('objectOid') or '')
            ):
                self._fail(
                    f'operation journal contains an invalid typed ref observation '
                    f'for {name!r}'
                )
            if kind == 'direct' and target is not None:
                self._fail(
                    f'operation journal direct ref {name!r} has a symbolic target'
                )
            if kind == 'symbolic' and (
                not isinstance(target, str)
                or self.repository.run(['git', 'check-ref-format', target], check=False).returncode != 0
            ):
                self._fail(
                    f'operation journal symbolic ref {name!r} has an invalid target'
                )
            observations[name] = observation

        for name, observation in observations.items():
            if observation is None or observation['kind'] != 'symbolic':
                continue
            target = observation['symbolicTarget']
            target_observation = observations.get(target)
            if target not in observations or target_observation is None:
                self._fail(
                    f'operation journal symbolic ref {name!r} omitted its typed '
                    f'referent {target!r}'
                )
            if target_observation['objectOid'] != observation['objectOid']:
                self._fail(
                    f'operation journal symbolic ref {name!r} differs from its '
                    f'referent {target!r}'
                )
        return observations

    def verify_recovery_gate(self, request, journal):
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        self._expire_if_operation_lease_changed(request, journal, manifest)
        expected = self._journal_ref_transaction_refs(request, journal)
        self._assert_no_ref_transaction_locks(repo_root, expected)

    def _ref_transaction_lock_paths(self, repo_root, expected):
        paths = []
        for ref in sorted(expected):
            raw = self.repository.git(
                repo_root, 'rev-parse', '--git-path', f'{ref}.lock'
            ).stdout.strip()
            path = Path(raw)
            if not path.is_absolute():
                path = repo_root / path
            paths.append(Path(os.path.abspath(path)))
        packed_raw = self.repository.git(
            repo_root, 'rev-parse', '--git-path', 'packed-refs.lock'
        ).stdout.strip()
        packed = Path(packed_raw)
        if not packed.is_absolute():
            packed = repo_root / packed
        paths.append(Path(os.path.abspath(packed)))
        head_raw = self.repository.git(
            repo_root, 'rev-parse', '--git-path', 'HEAD.lock'
        ).stdout.strip()
        head = Path(head_raw)
        if not head.is_absolute():
            head = repo_root / head
        paths.append(Path(os.path.abspath(head)))
        return sorted(set(paths), key=str)

    def _assert_no_ref_transaction_locks(self, repo_root, expected):
        existing = [
            path
            for path in self._ref_transaction_lock_paths(repo_root, expected)
            if path.exists() or path.is_symlink()
        ]
        if existing:
            self._fail(
                'Git ref transaction lock ownership cannot be proven after an '
                'interrupted writer; automatic cleanup is forbidden. Verify no '
                'Git writer is active, inspect and remove only these exact lock '
                'paths manually, then retry recover: '
                + ', '.join(str(path) for path in existing)
            )

    def _cas_ref_with_leases(self, repo_root, target_ref, new_tip, expected):
        expected = self._expand_symbolic_target_leases(repo_root, expected)
        target_observation = expected[target_ref]
        if target_observation is not None and target_observation['kind'] != 'direct':
            self._fail(f'compare-and-swap target must be a direct ref: {target_ref}')
        self._assert_ref_leases(repo_root, expected)
        self._assert_no_ref_transaction_locks(repo_root, expected)
        commands = []
        for ref, observation in sorted(expected.items()):
            old = (
                observation['objectOid']
                if observation is not None
                else ('0' * 40)
            )
            commands.append('option no-deref')
            if ref == target_ref:
                commands.append(f'update {ref} {new_tip} {old}')
            else:
                commands.append(f'verify {ref} {old}')

        authorization = self.guards.authorize_ref_move(repo_root)
        try:
            process = subprocess.Popen(
                ['git', 'update-ref', '--stdin'],
                cwd=repo_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self.repository.managed_process_env(
                    {MANAGED_REF_MOVE_AUTH_ENV: authorization},
                    authorize=False,
                ),
            )
        except BaseException:
            (self.guards.ref_auth_dir(repo_root) / authorization).unlink(missing_ok=True)
            raise
        prepared = False
        try:
            process.stdin.write('start\n')
            process.stdin.flush()
            start_response = process.stdout.readline().strip()
            if start_response != 'start: ok':
                stdout, stderr = self._abort_ref_transaction(process)
                detail = stderr.strip() or stdout.strip() or start_response
                self._fail(f'managed ref transaction could not start: {detail}')

            process.stdin.write('\n'.join([*commands, 'prepare', '']))
            process.stdin.flush()
            prepare_response = process.stdout.readline().strip()
            if prepare_response != 'prepare: ok':
                stdout, stderr = self._abort_ref_transaction(process)
                detail = stderr.strip() or stdout.strip() or prepare_response
                self._fail(f'managed ref transaction could not prepare: {detail}')
            prepared = True

            self.checkpoint('ref_transaction_prepared')
            self._assert_ref_leases(repo_root, expected)

            process.stdin.write('commit\n')
            process.stdin.flush()
            process.stdin.close()
            stdout = process.stdout.read()
            stderr = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
            returncode = process.wait()
            prepared = False
            if returncode != 0 or stdout.strip() != 'commit: ok':
                detail = stderr.strip() or stdout.strip()
                self._fail(
                    'managed ref compare-and-swap failed during commit: ' + detail
                )
        except BaseException:
            if prepared or process.poll() is None:
                self._abort_ref_transaction(process)
            raise
        finally:
            (self.guards.ref_auth_dir(repo_root) / authorization).unlink(missing_ok=True)

    def _abort_ref_transaction(self, process):
        if process.poll() is None and process.stdin and not process.stdin.closed:
            try:
                process.stdin.write('abort\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        stdout = process.stdout.read() if process.stdout else ''
        stderr = process.stderr.read() if process.stderr else ''
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
        process.wait()
        return stdout, stderr

    def _verify_draft_candidate(self, repo_root, request, journal):
        if journal.get('preparedResolution') is not None or journal.get('projectionRoute') == 'resolved-manifest-base':
            expected = self._prepared_resolution_projection(request, journal)
            if any(journal.get(key) != value for key, value in expected.items()):
                self._fail('journaled explicit draft candidate changed')
            return
        projection = self.replay_factory.for_repository(repo_root).deterministic_stack_projection(
            journal.get('projectionBaseSha', journal['baseRefSha']),
            [journal['candidateProductCommitSha']],
        )
        if projection.get('status') != 'projected':
            self._fail('draft projection is no longer a non-empty conflict-free object')
        if (
            projection['tip'] != journal['candidateDraftCommitSha']
            or projection['tree'] != journal['candidateDraftTreeSha']
        ):
            self._fail('journaled draft projection object changed')

    def ensure_draft_ref_owned(self, request, journal):
        repo_root = self._repo_root(request)
        self.verify_recovery_gate(request, journal)
        if self.repository.ref_tip(repo_root, 'HEAD') != request.expected_head:
            self._fail('integration advanced before draft ownership')
        self._assert_operation_worktree(repo_root, request, journal, 'product')
        self._verify_candidate_object(
            repo_root,
            journal['candidateProductCommitSha'],
            journal['candidateProductTreeSha'],
            request.expected_head,
            journal['productPathObjects'],
        )
        self._verify_worktree_objects(repo_root, journal['productPathObjects'])
        self._verify_draft_candidate(repo_root, request, journal)
        self._assert_index_lease(
            repo_root, request, journal, 'baseline', 'draft ownership'
        )
        expected = self._expected_managed_refs(
            request, journal, request.expected_head, draft_owned=False
        )
        draft_ref = f'refs/heads/{request.draft_branch}'
        actual = self._observe_ref(repo_root, draft_ref)
        actual_oid = actual['objectOid'] if actual is not None else None
        if actual_oid == journal['candidateDraftCommitSha']:
            expected[draft_ref] = self._direct_ref_observation(
                draft_ref, journal['candidateDraftCommitSha']
            )
            self._assert_ref_leases(repo_root, expected)
            self.verify_recovery_gate(request, journal)
            return
        if actual_oid is not None:
            self._fail(f'draft branch collision: expected absent, found {actual_oid}')
        self._cas_ref_with_leases(
            repo_root, draft_ref, journal['candidateDraftCommitSha'], expected
        )
        self.verify_recovery_gate(request, journal)
        self.checkpoint('draft_ref_cas')

    def _run_commit_validation_hooks(self, repo_root, commit):
        hooks_dir, _ = self.guards.active_hooks_dir(repo_root)
        prepare_commit_message = hooks_dir / 'prepare-commit-msg'
        if prepare_commit_message.is_file() and os.access(prepare_commit_message, os.X_OK):
            self._fail(
                'executable prepare-commit-msg hooks are unsupported by the deterministic '
                'revision provider; disable the hook or commit outside this provider'
            )
        descriptor, index_name = tempfile.mkstemp(prefix='syncwheel-hook-index-')
        os.close(descriptor)
        index_path = Path(index_name)
        index_path.unlink(missing_ok=True)
        message_descriptor, message_name = tempfile.mkstemp(
            prefix='syncwheel-commit-message-', suffix='.txt'
        )
        message_path = Path(message_name)
        message = self.repository.git(repo_root, 'show', '-s', '--format=%B', commit).stdout
        try:
            with os.fdopen(message_descriptor, 'w') as handle:
                handle.write(message)
                handle.flush()
                os.fsync(handle.fileno())
            with self.guards.ref_move_authorization(repo_root) as authorization:
                environment = self.repository.managed_process_env(
                    {
                        'GIT_INDEX_FILE': str(index_path),
                        'SYNCWHEEL_REVISION_PROVIDER_COMMIT': commit,
                        MANAGED_REF_MOVE_AUTH_ENV: authorization,
                    },
                    authorize=False,
                )
                self.repository.git(repo_root, 'read-tree', commit, env={'GIT_INDEX_FILE': str(index_path)})
                for hook_name, arguments in (
                    ('pre-commit', []),
                    ('commit-msg', [str(message_path)]),
                ):
                    hook = hooks_dir / hook_name
                    if not hook.is_file() or not os.access(hook, os.X_OK):
                        continue
                    result = subprocess.run(
                        [str(hook), *arguments],
                        cwd=repo_root,
                        text=True,
                        capture_output=True,
                        env=environment,
                    )
                    if result.returncode != 0:
                        detail = (result.stderr.strip() or result.stdout.strip())[:2000]
                        suffix = f': {detail}' if detail else ''
                        self._fail(
                            f'{hook_name} hook rejected prepared commit '
                            f'(exit {result.returncode}){suffix}'
                        )
                hook_tree = self.repository.git(
                    repo_root,
                    'write-tree',
                    env={'GIT_INDEX_FILE': str(index_path)},
                ).stdout.strip()
                if hook_tree != self.repository.ref_tree(repo_root, commit):
                    self._fail('commit hook modified the deterministic provider index')
                if message_path.read_text() != message:
                    self._fail('commit-msg hook modified the deterministic provider message')
        finally:
            index_path.unlink(missing_ok=True)
            message_path.unlink(missing_ok=True)

    def _hook_repository_snapshot(self, repo_root, path_objects):
        observed_paths = {}
        for relative in path_objects:
            observed = self._read_product_path(repo_root, relative)
            observed_paths[relative] = None if observed is None else {
                'sha256': observed['sha256'], 'mode': observed['mode'],
            }
        index = self._index_observation(repo_root)
        return {
            'allRefs': self._all_refs(repo_root),
            'headSymbolic': self.repository.git(
                repo_root, 'symbolic-ref', '--quiet', 'HEAD', check=False
            ).stdout.strip() or None,
            'headObject': self.repository.ref_tip(repo_root, 'HEAD'),
            'worktreePorcelain': self._worktree_porcelain(repo_root),
            'index': index,
            'indexTree': self._index_tree(repo_root, index),
            'status': self.repository.git(
                repo_root,
                'status',
                '--porcelain=v1',
                '-z',
                '--untracked-files=all',
                env={'GIT_OPTIONAL_LOCKS': '0'},
            ).stdout,
            'paths': observed_paths,
        }

    def validate_prepared_commit(self, request, journal, kind):
        repo_root = self._repo_root(request)
        if kind == 'product':
            commit = journal['candidateProductCommitSha']
            tree = journal['candidateProductTreeSha']
            parent = request.expected_head
            path_objects = journal['productPathObjects']
            integration_tip = request.expected_head
            draft_owned = False
        elif kind == 'control':
            commit = journal['candidateControlCommitSha']
            tree = journal['candidateControlTreeSha']
            parent = journal['productCommitSha']
            path_objects = journal['controlPathObjects']
            integration_tip = journal['productCommitSha']
            draft_owned = True
        else:
            self._fail(f'unknown prepared commit kind: {kind}')
        self._assert_operation_worktree(repo_root, request, journal, kind)
        self._verify_candidate_object(
            repo_root, commit, tree, parent, path_objects
        )
        self._verify_worktree_objects(repo_root, path_objects)
        if kind == 'product' and journal.get('projectionRoute') != 'derived':
            self._verify_draft_candidate(repo_root, request, journal)
        expected_refs = self._expected_managed_refs(
            request, journal, integration_tip, draft_owned=draft_owned
        )
        self._assert_ref_leases(repo_root, expected_refs)
        lease_name = 'baseline' if kind == 'product' else 'product'
        if not self._journal_index_lease(journal, lease_name).sha256:
            self._fail(f'{kind} index lease is missing from the operation journal')
        self._assert_index_lease(
            repo_root, request, journal, lease_name, f'{kind} hook validation'
        )
        before = self._hook_repository_snapshot(repo_root, path_objects)
        rejection = None
        try:
            self._run_commit_validation_hooks(repo_root, commit)
        except self.provider.RevisionProviderError as exc:
            rejection = exc
        after = self._hook_repository_snapshot(repo_root, path_objects)
        index_before = before.pop('index')
        index_after = after.pop('index')
        if after != before or self._matching_index_lease(
            request, index_after, [(f'{kind} hook snapshot', index_before)]
        ) is None:
            self._fail(
                'commit hook changed repository state; no subsequent managed ref was moved'
            )
        if rejection is not None:
            raise rejection
        self._verify_candidate_object(
            repo_root, commit, tree, parent, path_objects
        )
        self._verify_worktree_objects(repo_root, path_objects)
        self._assert_ref_leases(repo_root, expected_refs)

    def publish_prepared_commit(self, request, commit, expected_parent):
        repo_root = self._repo_root(request)
        journal = self.load_journal(request)
        if journal is None:
            self._fail('prepared commit publication requires a journal')
        self.verify_recovery_gate(request, journal)
        parent = self.repository.ref_tip(repo_root, f'{commit}^')
        if parent != expected_parent:
            self._fail(
                f'prepared commit parent mismatch: expected {expected_parent}, found {parent}'
            )
        current_head = self.repository.ref_tip(repo_root, 'HEAD')
        if current_head not in {expected_parent, commit}:
            self._fail('integration HEAD changed before compare-and-swap publication')
        index_tree = self._index_tree(repo_root)
        if index_tree not in {self.repository.ref_tree(repo_root, expected_parent), self.repository.ref_tree(repo_root, commit)}:
            self._fail('real index changed before compare-and-swap publication')
        manifest, _ = self._manifest(repo_root)
        branch = manifest['integration']['branch']
        if self.repository.get_current_branch(repo_root) != branch:
            self._fail('primary checkout left the integration branch')
        if expected_parent == request.expected_head:
            derived = journal.get('projectionRoute') == 'derived'
            lease_name = 'baseline'
            if not journal.get('productHooksValidated'):
                self._fail('product hooks were not durably validated before draft ownership')
            if not derived and self.repository.ref_tip(repo_root, request.draft_branch) != journal.get(
                'candidateDraftCommitSha'
            ):
                self._fail('draft ownership is missing before integration publication')
            if current_head == expected_parent:
                self._assert_operation_worktree(
                    repo_root, request, journal, 'product'
                )
            self._verify_candidate_object(
                repo_root, commit, journal['candidateProductTreeSha'], expected_parent,
                journal['productPathObjects'],
            )
            self._verify_worktree_objects(repo_root, journal['productPathObjects'])
            if not derived:
                self._verify_draft_candidate(repo_root, request, journal)
            expected_refs = self._expected_managed_refs(
                request, journal, expected_parent, draft_owned=not derived
            )
        else:
            lease_name = 'product'
            if not journal.get('productIndexSha256'):
                self._fail('product index lease is missing before control publication')
            if not journal.get('controlHooksValidated'):
                self._fail('control hooks were not durably validated before publication')
            if current_head == expected_parent:
                self._assert_operation_worktree(
                    repo_root, request, journal, 'control'
                )
            self._verify_candidate_object(
                repo_root, commit, journal['candidateControlTreeSha'], expected_parent,
                journal['controlPathObjects'],
            )
            self._verify_worktree_objects(repo_root, journal['controlPathObjects'])
            expected_refs = self._expected_managed_refs(
                request, journal, expected_parent, draft_owned=True
            )
        integration_ref = f'refs/heads/{branch}'
        if current_head == commit:
            expected_refs[integration_ref] = self._direct_ref_observation(
                integration_ref, commit
            )
            self._assert_ref_leases(repo_root, expected_refs)
            self.verify_recovery_gate(request, journal)
            return commit
        self._assert_index_lease(
            repo_root, request, journal, lease_name, 'integration publication'
        )
        self._assert_ref_leases(repo_root, expected_refs)
        self._cas_ref_with_leases(repo_root, integration_ref, commit, expected_refs)
        self.verify_recovery_gate(request, journal)
        self.checkpoint(
            'integration_product_cas'
            if expected_parent == request.expected_head
            else 'integration_control_cas'
        )
        return commit

    def _deterministic_index(self, repo_root, commit, *, allowed_worktree_paths=()):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix='syncwheel-revision-aligned-index-'
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        temporary.unlink(missing_ok=True)
        environment = {'GIT_INDEX_FILE': str(temporary)}
        try:
            self.repository.git(repo_root, 'read-tree', commit, env=environment)
            refreshed = self.repository.git(
                repo_root,
                'update-index',
                '--refresh',
                check=False,
                env=environment,
            )
            if refreshed.returncode != 0:
                mismatched = {
                    item
                    for item in self.repository.git(
                        repo_root,
                        'diff-files',
                        '--name-only',
                        '-z',
                        env=environment,
                    ).stdout.split('\0')
                    if item
                }
                if mismatched - set(allowed_worktree_paths):
                    self._fail(
                        'working tree does not match the commit during index preparation: '
                        + (refreshed.stderr.strip() or refreshed.stdout.strip())
                    )
            tree = self.repository.git(repo_root, 'write-tree', env=environment).stdout.strip()
            expected_tree = self.repository.ref_tree(repo_root, commit)
            if tree != expected_tree:
                self._fail('deterministic replacement index does not match the commit tree')
            payload = temporary.read_bytes()
            return payload, hashlib.sha256(payload).hexdigest()
        finally:
            temporary.unlink(missing_ok=True)

    def _index_alignment_directory(self, request):
        return self._journal_directory(request) / 'index-alignment'

    def _ensure_index_alignment_directory(self, request):
        directory = self._index_alignment_directory(request)
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            self._fsync_directory(directory.parent)
        self._fsync_directory(directory)
        return directory

    def _index_alignment_record(
        self, request, journal, kind, commit, expected_sha, desired_sha, *, replace=False
    ):
        filename = (
            f'{request.operation_id}-{kind}-{desired_sha}.index'
        )
        record = {
            'schemaVersion': 1,
            'kind': kind,
            'commitSha': commit,
            'expectedSha256': expected_sha,
            'desiredSha256': desired_sha,
            'backingFile': filename,
        }
        alignments = journal.get('indexAlignments')
        if alignments is None:
            alignments = {}
        if not isinstance(alignments, dict):
            self._fail('operation journal has invalid index-alignment ownership state')
        existing = alignments.get(kind)
        if existing is not None and existing != record and not replace:
            self._fail(f'{kind} index-alignment ownership record changed')
        if existing != record:
            updated = copy.deepcopy(journal)
            updated_alignments = dict(alignments)
            updated_alignments[kind] = record
            updated['indexAlignments'] = updated_alignments
            self.save_journal(request, updated)
            self.checkpoint(f'{kind}_index_alignment_prepared')
        return record

    def _resumed_index_alignment(
        self, request, repo_root, journal, kind, commit, expected_sha, payload, sha
    ):
        """Return (payload, sha, replace_record) for a rebuild that differs only in stat data.

        Journals without semantic leases stay byte-exact.
        """
        alignments = journal.get('indexAlignments')
        existing = alignments.get(kind) if isinstance(alignments, dict) else None
        if (
            not isinstance(existing, dict)
            or existing.get('desiredSha256') == sha
            or not self._journal_has_semantic_leases(journal)
        ):
            return payload, sha, False
        recorded = existing.get('desiredSha256')
        if not isinstance(recorded, str) or not self.provider.HEX_64.fullmatch(recorded):
            return payload, sha, False
        filename = f'{request.operation_id}-{kind}-{recorded}.index'
        if existing != {
            'schemaVersion': 1, 'kind': kind, 'commitSha': commit,
            'expectedSha256': expected_sha, 'desiredSha256': recorded,
            'backingFile': filename,
        }:
            return payload, sha, False
        object_format = self._index_object_format(repo_root)
        fresh = IndexObservation(payload, object_format)
        if fresh.semantic is None:
            return payload, sha, False
        stage = f'{kind} alignment replay'
        backing = self._index_alignment_directory(request) / filename
        if not os.path.lexists(backing):
            # backing already removed, so the old record owns no payload and no index.lock
            self._record_semantic_index_match(request, stage, recorded, sha, fresh.semantic)
            return payload, sha, True
        journaled_payload, _ = self._read_regular_file(
            backing, 'journaled index-alignment backing file'
        )
        journaled = IndexObservation(journaled_payload, object_format)
        if journaled.sha256 != recorded or self._matching_index_lease(
            request, fresh, [(stage, IndexLease(recorded, journaled.semantic))]
        ) is None:
            self._fail('journaled index-alignment backing payload is not deterministic')
        return journaled_payload, recorded, False

    def _prepare_index_backing(self, request, record, desired_payload, mode):
        directory = self._ensure_index_alignment_directory(request)
        backing = directory / record['backingFile']
        if backing.parent != directory:
            self._fail('index-alignment backing path escaped its ownership directory')
        try:
            existing_payload, metadata = self._read_regular_file(
                backing, 'journaled index-alignment backing file'
            )
        except self.provider.RevisionProviderError as exc:
            if backing.exists() or backing.is_symlink():
                raise
            descriptor = None
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, 'O_NOFOLLOW', 0)
            )
            try:
                descriptor = os.open(backing, flags, mode)
                os.fchmod(descriptor, mode)
                _write_all(descriptor, desired_payload)
                os.fsync(descriptor)
            except FileExistsError:
                self._fail(
                    f'index-alignment backing file appeared concurrently: {backing}'
                )
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            self._fsync_directory(directory)
            existing_payload, metadata = self._read_regular_file(
                backing, 'journaled index-alignment backing file'
            )
        if hashlib.sha256(existing_payload).hexdigest() != record['desiredSha256']:
            self._fail('journaled index-alignment backing bytes changed')
        if existing_payload != desired_payload:
            self._fail('journaled index-alignment backing payload is not deterministic')
        if stat.S_IMODE(metadata.st_mode) != mode:
            self._fail('journaled index-alignment backing mode changed')
        self.checkpoint(f"{record['kind']}_index_backing_fsynced")
        return backing

    def _same_regular_inode(self, first, second):
        try:
            first_stat = os.lstat(first)
            second_stat = os.lstat(second)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(first_stat.st_mode)
            and stat.S_ISREG(second_stat.st_mode)
            and first_stat.st_dev == second_stat.st_dev
            and first_stat.st_ino == second_stat.st_ino
        )

    def _remove_owned_index_lock(self, lock_path, backing):
        if not self._same_regular_inode(lock_path, backing):
            return False
        os.unlink(lock_path)
        self._fsync_directory(lock_path.parent)
        return True

    def _remove_index_backing(self, backing):
        try:
            metadata = os.lstat(backing)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(metadata.st_mode):
            self._fail(f'index-alignment backing is no longer regular: {backing}')
        os.unlink(backing)
        self._fsync_directory(backing.parent)

    def _index_lock_present(self, lock_path):
        return lock_path.exists() or lock_path.is_symlink()

    def _wait_for_foreign_index_lock(self, lock_path, deadline):
        """Poll until another process drops index.lock, False once the deadline passes.

        A plain `git status` holds index.lock briefly while refreshing stat data,
        so only a lock outliving the wait is an error.
        """
        delay = 0.005
        while self._index_lock_present(lock_path):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.1)
        return True

    def recover_owned_index_lock(self, request, journal):
        index_path = self._index_path(self._repo_root(request))
        lock_path = Path(f'{index_path}.lock')
        if not self._index_lock_present(lock_path):
            return
        alignments = journal.get('indexAlignments')
        records = []
        for kind, record in (alignments.items() if isinstance(alignments, dict) else ()):
            if kind not in {'product', 'control'} or not isinstance(record, dict):
                self._fail('operation journal has invalid index-alignment ownership state')
            commit = record.get('commitSha')
            expected_sha = record.get('expectedSha256')
            desired_sha = record.get('desiredSha256')
            expected_filename = f'{request.operation_id}-{kind}-{desired_sha}.index'
            if (
                not isinstance(commit, str)
                or not self.provider.HEX_40.fullmatch(commit)
                or not isinstance(expected_sha, str)
                or not self.provider.HEX_64.fullmatch(expected_sha)
                or not isinstance(desired_sha, str)
                or not self.provider.HEX_64.fullmatch(desired_sha)
                or record.get('backingFile') != expected_filename
            ):
                self._fail('operation journal has invalid index-alignment ownership state')
            backing = self._index_alignment_directory(request) / expected_filename
            records.append((kind, record, backing))
        owned = [item for item in records if self._same_regular_inode(lock_path, item[2])]
        if not owned:
            deadline = time.monotonic() + self.INDEX_LOCK_WAIT_SECONDS
            if self._wait_for_foreign_index_lock(lock_path, deadline):
                return
            self._fail(f'Git index is locked by another writer: {lock_path}')
        if len(owned) != 1:
            self._fail(f'Git index is locked by another writer: {lock_path}')
        kind, record, backing = owned[0]
        repo_root = self._repo_root(request)
        if self.repository.ref_tip(repo_root, 'HEAD') != record['commitSha']:
            self._fail(
                f'journaled {kind} index.lock is not aligned with current HEAD; '
                'automatic recovery is unsafe'
            )
        current = self._index_observation(repo_root)
        predecessor = self._journal_index_lease(
            journal, 'baseline' if kind == 'product' else 'product'
        )
        backing_payload, _ = self._read_regular_file(
            backing, 'journaled index-alignment backing file'
        )
        desired = IndexObservation(backing_payload, self._index_object_format(repo_root))
        stage = f'{kind} index.lock recovery'
        candidates = [
            (stage, IndexLease(
                record['expectedSha256'],
                predecessor.semantic
                if predecessor.sha256 == record['expectedSha256'] else None,
            )),
            (stage, IndexLease(
                record['desiredSha256'],
                desired.semantic
                if self._journal_has_semantic_leases(journal)
                and desired.sha256 == record['desiredSha256'] else None,
            )),
        ]
        if self._matching_index_lease(request, current, candidates) is None:
            self._fail(
                f'real Git index lease was lost while recovering {kind} index.lock'
            )
        if not self._remove_owned_index_lock(lock_path, backing):
            self._fail('journaled index.lock ownership changed during recovery')
        self.checkpoint(f'{kind}_index_lock_recovered')

    def _take_alignment_lock(self, lock_path, backing, already_aligned, reassert):
        """Link backing as index.lock unless the index is already aligned.

        Returns (already_aligned, lock_owned). Foreign locks get at most
        INDEX_LOCK_WAIT_SECONDS in total, and the lease is asserted again after
        every wait because the lock holder may have rewritten the index.
        """
        deadline = time.monotonic() + self.INDEX_LOCK_WAIT_SECONDS
        while True:
            if already_aligned:
                if (
                    not self._index_lock_present(lock_path)
                    or self._remove_owned_index_lock(lock_path, backing)
                ):
                    return True, False
            else:
                try:
                    os.link(backing, lock_path, follow_symlinks=False)
                    return False, True
                except FileExistsError:
                    if self._same_regular_inode(lock_path, backing):
                        return False, True
            if not self._wait_for_foreign_index_lock(lock_path, deadline):
                self._fail(f'Git index is locked by another writer: {lock_path}')
            already_aligned = reassert()

    def align_index(self, request, commit):
        repo_root = self._repo_root(request)
        if self.repository.ref_tip(repo_root, 'HEAD') != commit:
            self._fail('cannot align index to a non-current revision')
        journal = self.load_journal(request)
        if journal is None:
            self._fail('index alignment requires an operation journal')
        if commit == journal.get('candidateProductCommitSha'):
            kind = 'product'
            predecessor = self._journal_index_lease(journal, 'baseline')
        elif commit == journal.get('candidateControlCommitSha'):
            kind = 'control'
            predecessor = self._journal_index_lease(journal, 'product')
        else:
            self._fail('index alignment commit is not owned by this operation')
        expected_sha = predecessor.sha256
        if not expected_sha:
            self._fail(f'{kind} index alignment has no predecessor lease')

        desired_payload, desired_sha = self._deterministic_index(
            repo_root,
            commit,
            allowed_worktree_paths=(
                (self.MANIFEST_PRODUCT_PATH,)
                if kind == 'product' and journal.get('projectionRoute') == 'derived'
                else ()
            ),
        )
        desired_payload, desired_sha, replace_record = self._resumed_index_alignment(
            request, repo_root, journal, kind, commit, expected_sha,
            desired_payload, desired_sha,
        )
        desired = IndexObservation(desired_payload, self._index_object_format(repo_root))
        self.__dict__.setdefault('_aligned_index_semantics', {})[commit] = desired.semantic
        leased = (f'{kind} alignment', predecessor)
        aligned = (f'{kind} alignment result', IndexLease(
            desired_sha,
            desired.semantic if self._journal_has_semantic_leases(journal) else None,
        ))

        def assert_leased_or_aligned():
            observed = self._index_observation(repo_root)
            matched = self._matching_index_lease(request, observed, [leased, aligned])
            if matched is None:
                self._fail(
                    f'real Git index lease was lost before {kind} alignment: '
                    f'expected {expected_sha}, found {observed.sha256}'
                )
            return matched == 1

        assert_leased_or_aligned()
        self.checkpoint(f'before_{kind}_index_lock')
        index_path = self._index_path(repo_root)
        lock_path = Path(f'{index_path}.lock')
        try:
            _, index_metadata = self._read_regular_file(index_path, 'Git index')
            mode = stat.S_IMODE(index_metadata.st_mode)
        except FileNotFoundError:
            self._fail(f'revision provider requires an existing Git index: {index_path}')

        already_aligned = assert_leased_or_aligned()
        record = self._index_alignment_record(
            request, journal, kind, commit, expected_sha, desired_sha,
            replace=replace_record,
        )
        backing = self._prepare_index_backing(
            request, record, desired_payload, mode
        )

        lock_owned = False
        try:
            already_aligned, lock_owned = self._take_alignment_lock(
                lock_path, backing, already_aligned, assert_leased_or_aligned
            )
            if not already_aligned:
                self._fsync_directory(lock_path.parent)
                if not self._same_regular_inode(lock_path, backing):
                    self._fail('journaled index.lock ownership could not be proven')
                self.checkpoint(f'{kind}_index_lock_owned')
                locked = self._index_observation(repo_root)
                if self._matching_index_lease(request, locked, [leased]) is None:
                    self._fail(
                        f'real Git index lease was lost before {kind} alignment: '
                        f'expected {expected_sha}, found {locked.sha256}'
                    )
                final = self._index_observation(repo_root)
                if self._matching_index_lease(request, final, [leased]) is None:
                    self._fail(
                        f'real Git index changed while {kind} alignment held index.lock'
                    )
                os.replace(lock_path, index_path)
                lock_owned = False
                fsync_directory_path(index_path.parent)
                self.checkpoint(f'{kind}_index_cas')
        except BaseException:
            if lock_owned:
                self._remove_owned_index_lock(lock_path, backing)
            raise
        finally:
            if lock_owned:
                self._remove_owned_index_lock(lock_path, backing)
        if already_aligned:
            self._fsync_directory(index_path.parent)
            self._remove_index_backing(backing)
            if self._index_tree(repo_root) != self.repository.ref_tree(repo_root, commit):
                self._fail('aligned index hash does not produce the current commit tree')
            return desired_sha
        self._remove_index_backing(backing)
        retained = self._index_observation(repo_root)
        if self._matching_index_lease(request, retained, [aligned]) is None:
            self._fail(f'real Git index did not retain the {kind} replacement bytes')
        if self._index_tree(repo_root, retained) != self.repository.ref_tree(repo_root, commit):
            self._fail(f'real Git index did not align to the {kind} commit tree')
        return desired_sha

    def _desired_stack(self, request, journal, manifest):
        desired = {
            'id': request.draft_stack_id,
            'branch': request.draft_branch,
            'base': journal.get('projectionBaseSha', journal['baseRefSha']),
            'target_remote': manifest['defaults']['canonical_remote'],
            'target_branch': manifest['defaults']['base_branch'],
            'integration_branch': manifest['integration']['branch'],
            'commits': [journal['productCommitSha']],
            'state': 'draft',
            'publication': {'enabled': False},
            'meta': {
                'purpose': f'Own Agentwheel operation {request.operation_id}',
                'agentwheel_operation_id': request.operation_id,
                'agentwheel_plan_digest': request.plan_digest,
            },
        }
        if journal.get('projectionRoute') == 'resolved-manifest-base':
            self._verify_draft_candidate(self._repo_root(request), request, journal)
            desired['commits'] = [journal['candidateDraftCommitSha']]
            desired['integration_commits'] = [journal['productCommitSha']]
            desired['integration_resolution'] = {'source_tip': journal['candidateDraftCommitSha']}
        if journal.get('projectionBaseKind') == 'integration-tip':
            desired['meta']['revision_provider_projection_base'] = {
                'kind': 'integration-tip',
                'sha': journal['projectionBaseSha'],
            }
        return desired

    def _manifest_without_operation_stack(self, manifest, request):
        stripped = copy.deepcopy(manifest)
        stripped['stacks'] = [
            stack for stack in stripped['stacks']
            if stack['id'] != request.draft_stack_id
        ]
        stripped['integration']['stacks'] = [
            stack_id for stack_id in stripped['integration'].get('stacks', [])
            if stack_id != request.draft_stack_id
        ]
        return stripped

    def _assert_draft_branch(self, repo_root, desired, journal, request):
        journal_tip = self.repository.ref_tip(repo_root, desired['branch'])
        if journal.get('projectionRoute') == 'resolved-manifest-base':
            self._verify_draft_candidate(repo_root, request, journal)
            expected_tip = journal['candidateDraftCommitSha']
        else:
            expected_tip = self.replay_factory.for_repository(repo_root).deterministic_stack_replay_tip(
                journal.get('projectionBaseSha', journal['baseRefSha']),
                desired['commits'],
            )
        if not expected_tip:
            self._fail('owned draft projection is no longer reproducible')
        if journal_tip != expected_tip:
            self._fail(
                f'draft branch collision: expected {expected_tip}, found {journal_tip}'
            )

    def ensure_stack_owned(self, request, journal):
        repo_root = self._repo_root(request)
        if self.repository.ref_tip(repo_root, 'HEAD') != journal['productCommitSha']:
            self._fail('product commit is not the current integration HEAD')
        self._assert_ref_leases(
            repo_root,
            self._expected_managed_refs(
                request, journal, journal['productCommitSha'], draft_owned=True
            ),
        )
        manifest, manifest_path = self._manifest(repo_root)
        with self.manifest_store.manifest_write_transaction(
            repo_root,
            manifest_path,
            'revision-provider',
            commit_tracked_changes=False,
        ):
            manifest, _ = self._manifest(repo_root)
            desired = self._desired_stack(request, journal, manifest)
            existing = stack_map(manifest).get(request.draft_stack_id)
            if existing is not None:
                if existing != desired:
                    self._fail(f'draft stack collision: {request.draft_stack_id}')
                stripped = self._manifest_without_operation_stack(manifest, request)
                if manifest_digest(stripped) != journal['observedManifestDigest']:
                    self.expire_manifest_invalidated(
                        request,
                        journal,
                        'manifest contains changes beyond this operation draft stack',
                    )
                if not self.repository.branch_exists(repo_root, request.draft_branch):
                    self._fail('owned draft stack branch is missing')
                if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
                    self._fail('manifest recovery requires a clean, conflict-free index')
                self._assert_unowned_dirty_unchanged(
                    repo_root, request, allowed_outside=(self.MANIFEST_PRODUCT_PATH,)
                )
                self._assert_draft_branch(repo_root, desired, journal, request)
                context = {
                    'operation_id': request.operation_id,
                    'plan_digest': request.plan_digest,
                    'stack': request.draft_stack_id,
                    'branch': request.draft_branch,
                    'product_commit': journal['productCommitSha'],
                    'paths': sorted(journal['productPathObjects']),
                }
                if not journal.get('manifestReplaced'):
                    journal['manifestDigest'] = manifest_digest(manifest)
                    journal['manifestReplaced'] = True
                    self.save_journal(request, journal)
                    self.checkpoint('manifest_replaced')
                self._ensure_ownership_ledger_event(
                    repo_root, manifest_path, manifest, context, request, journal
                )
                return {'manifestDigest': manifest_digest(manifest)}

            if manifest_digest(manifest) != journal['observedManifestDigest']:
                self.expire_manifest_invalidated(
                    request, journal, 'manifest changed before draft ownership'
                )
            self._ensure_after_scope(repo_root, request)
            if any(stack['branch'] == request.draft_branch for stack in manifest['stacks']):
                self._fail(f'draft branch is owned by another stack: {request.draft_branch}')
            if self.repository.ref_tip(repo_root, request.draft_branch) != journal.get(
                'candidateDraftCommitSha'
            ):
                self._fail('draft ref ownership must be proven before manifest ownership')
            self._assert_draft_branch(repo_root, desired, journal, request)
            manifest['stacks'].append(desired)
            if request.draft_stack_id not in manifest['integration']['stacks']:
                manifest['integration']['stacks'].append(request.draft_stack_id)
            validate_stack_dependency_graph(
                manifest['stacks'],
                require_declared_dependencies=manifest['version'] == MANIFEST_VERSION_CHANNELS,
            )
            desired_digest = manifest_digest(manifest)
            context = {
                'operation_id': request.operation_id,
                'plan_digest': request.plan_digest,
                'stack': request.draft_stack_id,
                'branch': request.draft_branch,
                'product_commit': journal['productCommitSha'],
                'paths': sorted(journal['productPathObjects']),
            }
            self.manifest_store.require_manifest_transaction_current(manifest_path)
            self.manifest_store.save_manifest(manifest_path, manifest)
            self.checkpoint('manifest_replace_written')
            journal['manifestDigest'] = desired_digest
            journal['manifestReplaced'] = True
            self.save_journal(request, journal)
            self.checkpoint('manifest_replaced')
            self._ensure_ownership_ledger_event(
                repo_root, manifest_path, manifest, context, request, journal
            )
            persisted, _ = self._manifest(repo_root)
            return {'manifestDigest': manifest_digest(persisted)}

    def _ensure_ownership_ledger_event(
        self, repo_root, manifest_path, manifest, context, request, journal
    ):
        self.ledger.recover_ledger_tail(repo_root, manifest_path)
        desired_payload = manifest_event_payload(
            manifest_path, manifest, 'revision_provider_stack_ownership', context
        )
        matching = []
        for event in self.ledger.load_ledger_events(repo_root, manifest_path):
            if event.get('type') != 'manifest_saved':
                continue
            payload = event.get('payload') or {}
            event_context = payload.get('context') or {}
            if event_context.get('operation_id') == request.operation_id:
                matching.append(event)
        if matching:
            if len(matching) != 1 or matching[0].get('payload') != desired_payload:
                self._fail(
                    f'ledger collision for revision-provider operation {request.operation_id}'
                )
        self.provenance.resolve_common_derived_provenance(
            repo_root, manifest, sorted(context['paths'])
        )
        if not matching:
            self.ledger.append_ledger_event(
                repo_root, 'manifest_saved', desired_payload, manifest_path
            )
            self.checkpoint('ledger_event_written')
        events = self.ledger.load_ledger_events(repo_root, manifest_path)
        self.ledger.write_ledger_checkpoint(repo_root, reduce_ledger_state(events), manifest_path)
        if not journal.get('ledgerAppended'):
            journal['ledgerAppended'] = True
            self.save_journal(request, journal)
            self.checkpoint('ledger_appended')

    def prepare_control_commit(self, request, message):
        repo_root = self._repo_root(request)
        journal = self.load_journal(request)
        if not journal or self.repository.ref_tip(repo_root, 'HEAD') != journal.get('productCommitSha'):
            self._fail('control commit requires the product commit at integration HEAD')
        if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
            self._fail('control commit requires a clean, conflict-free index')
        dirty = self._dirty_paths(repo_root, request)
        if self.MANIFEST_PRODUCT_PATH not in dirty:
            self._fail(
                'control commit requires .syncwheel/manifest.json; found: '
                + ', '.join(sorted(dirty))
            )
        self._assert_unowned_dirty_unchanged(
            repo_root, request, allowed_outside=(self.MANIFEST_PRODUCT_PATH,)
        )
        path_objects = self._capture_path_objects(
            repo_root, request, paths=[self.MANIFEST_PRODUCT_PATH]
        )
        prepared = self._prepare_exact_commit(
            repo_root,
            journal['productCommitSha'],
            path_objects,
            message,
        )
        if prepared is None:
            return None
        return {
            'candidateControlCommitSha': prepared['commit'],
            'candidateControlTreeSha': prepared['tree'],
            'controlPathObjects': prepared['pathObjects'],
        }

    def _verify_invariants(self, repo_root, request, journal, expected_head):
        self.verify_recovery_gate(request, journal)
        if self.repository.ref_tip(repo_root, 'HEAD') != expected_head:
            self._fail('integration HEAD does not match the operation receipt')
        if journal.get('projectionRoute') == 'derived':
            if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
                self._fail('derived revision-provider operation left index changes behind')
            self._assert_unowned_dirty_unchanged(
                repo_root, request, allowed_outside=(self.MANIFEST_PRODUCT_PATH,)
            )
        else:
            if self._index_conflicts(repo_root) or not self._index_is_clean(repo_root):
                self._fail('revision-provider operation left index changes behind')
            self._assert_unowned_dirty_unchanged(repo_root, request)
        manifest, _ = self._manifest(repo_root)
        digest = manifest_digest(manifest)
        if (
            journal.get('projectionRoute') != 'derived'
            and digest != journal['manifestDigest']
        ):
            self.expire_manifest_invalidated(
                request,
                journal,
                'manifest digest changed before terminal verification',
            )
        validation = self.manifest_store.validate_manifest(repo_root, manifest)
        stale_after = {
            item['path'] for item in validation['details']['integration'].get(
                'derived_projection_stale'
            ) or []
        }
        stale_before = set(journal.get('baselineStalePaths') or [])
        repaired = stale_before - stale_after
        scoped_repair = bool(repaired) and repaired <= {
            item.path for item in request.paths
        } and stale_after < stale_before
        blocking_errors = [
            error for error in validation['errors']
            if not (
                scoped_repair and error.startswith('derived-projection-stale:')
            )
        ]
        if blocking_errors:
            self._fail('post-operation Syncwheel validation failed: ' + '; '.join(blocking_errors))
        unmapped = list(validation['details']['integration'].get('unmapped_commits') or [])
        if unmapped:
            abandoned = self.ledger.historically_abandoned_unmapped_commits(repo_root, manifest, unmapped)
            reconciled = self.ledger.historically_reconciled_unmapped_commits(
                repo_root, manifest, unmapped
            )
            unmapped = [
                commit for commit in unmapped
                if commit not in abandoned and commit not in reconciled
            ]
            if unmapped:
                self._fail('operation left unmapped integration commits: ' + ', '.join(unmapped))
        if self._worktrees(repo_root) != journal['baselineWorktrees']:
            self._fail('operation leaked or removed a Git worktree')
        if self._remote_refs(repo_root) != journal['baselineRemoteRefs']:
            self._fail('operation changed a remote-tracking ref; publication is forbidden')
        if journal.get('productCommitSha'):
            expected_refs = self._expected_managed_refs(
                request, journal, expected_head,
                draft_owned=journal.get('projectionRoute') != 'derived',
            )
        else:
            expected_refs = self._expected_managed_refs(
                request, journal, expected_head, draft_owned=False
            )
        self._assert_ref_leases(repo_root, expected_refs)
        if not journal.get('productCommitSha'):
            lease_name = 'baseline'
        elif journal.get('controlIndexSha256'):
            lease_name = 'control'
        else:
            lease_name = 'product'
        if not self._journal_index_lease(journal, lease_name).sha256:
            self._fail('terminal index lease is missing from the operation journal')
        self._assert_index_lease(
            repo_root, request, journal, lease_name, 'terminal verification'
        )
        self.verify_recovery_gate(request, journal)
        return {
            'resultingHead': expected_head,
            'manifestDigest': digest,
            'unmappedIntegrationCommits': unmapped,
        }

    def verify_final(self, request, journal):
        repo_root = self._repo_root(request)
        manifest, _ = self._manifest(repo_root)
        desired = self._desired_stack(request, journal, manifest)
        if stack_map(manifest).get(request.draft_stack_id) != desired:
            self._fail('operation draft stack ownership is missing or changed')
        if not self.repository.branch_exists(repo_root, request.draft_branch):
            self._fail('operation draft branch is missing')
        self._assert_draft_branch(repo_root, desired, journal, request)
        if self.repository.ref_tip(repo_root, request.draft_branch) != journal.get(
            'candidateDraftCommitSha'
        ):
            self._fail('operation draft branch differs from its journaled object')
        return self._verify_invariants(
            repo_root, request, journal, journal['controlCommitSha']
        )

    def verify_derived_final(self, request, journal):
        repo_root = self._repo_root(request)
        manifest, manifest_path = self._manifest(repo_root)
        self._expire_if_operation_lease_changed(request, journal, manifest)
        if self.repository.branch_exists(repo_root, request.draft_branch) or request.draft_stack_id in stack_map(manifest):
            self._fail('derived projection must not create a draft ref or stack')
        journal['manifestDigest'] = manifest_digest(manifest)
        payload = {
            'operation_id': request.operation_id,
            'commit': journal['productCommitSha'],
            'paths': sorted(journal['productPathObjects']),
            'paths_digest': journal['derivedContentDigest'],
            'composition_digest': journal['integrationCompositionDigest'],
        }
        matching = [
            event for event in self.ledger.load_ledger_events(repo_root, manifest_path)
            if event.get('type') == 'revision_provider_derived_commit'
            and (event.get('payload') or {}).get('operation_id') == request.operation_id
        ]
        if matching and (len(matching) != 1 or matching[0].get('payload') != payload):
            self._fail(f'ledger collision for derived revision-provider operation {request.operation_id}')
        self.provenance.record_common_derived_provenance(repo_root, manifest, payload)
        if not matching:
            self.ledger.append_ledger_event(repo_root, 'revision_provider_derived_commit', payload, manifest_path)
        return self._verify_invariants(
            repo_root, request, journal, journal['productCommitSha']
        )

    def verify_no_repository_delta(self, request, journal):
        repo_root = self._repo_root(request)
        self._assert_unowned_dirty_unchanged(repo_root, request)
        journal['manifestDigest'] = journal['observedManifestDigest']
        return self._verify_invariants(repo_root, request, journal, request.expected_head)

    def verify_release(self, request, journal):
        repo_root = self._repo_root(request)
        observation = self._validate_repository(repo_root, request, require_clean=False)
        if observation['manifestDigest'] != journal['observedManifestDigest']:
            self._fail('cannot release after manifest mutation')
        self._ensure_after_scope(repo_root, request)
        manifest = observation['manifest']
        if request.draft_stack_id in stack_map(manifest) or self.repository.branch_exists(
            repo_root, request.draft_branch
        ):
            self._fail('cannot release after draft ownership or ref mutation')
        expected_refs = self._expected_managed_refs(
            request, journal, request.expected_head, draft_owned=False
        )
        self._assert_ref_leases(repo_root, expected_refs)
        self._assert_index_lease(
            repo_root, request, journal, 'baseline', 'operation release'
        )

    def checkpoint(self, phase):
        """Fault-injection seam; production intentionally performs no action."""
        return None


class JournalRevisionBackend(SyncwheelRevisionBackend):
    """Local exact-path revisioning; publication is a separate journal operation."""

    def _head_policy(self, repo_root, request, head):
        config_path = '.agentwheel/config.json'
        # Read policy from the operation's trusted starting revision. A dirty
        # config or a later policy change cannot silently widen the lease.
        raw = self.repository.git(repo_root, 'show', f'{request.expected_head}:{config_path}', check=False)
        policy = 'exact'
        if raw.returncode == 0:
            try:
                config = json.loads(raw.stdout)
                policy = config.get('mutationPolicy', {}).get('revisioning', {}).get('provider', {}).get('headLease', 'exact')
            except (ValueError, AttributeError):
                self._fail('invalid revision-provider HEAD lease configuration')
        if policy not in {'exact', 'path-scoped'}:
            self._fail('invalid revision-provider headLease')
        if head == request.expected_head:
            return policy
        if policy != 'path-scoped' or not request.paths or request.no_commit or self.repository.git(
            repo_root, 'merge-base', '--is-ancestor', request.expected_head, head,
            check=False,
        ).returncode != 0:
            self._fail('journal HEAD changed outside the allowed HEAD lease')
        protected = {item.path for item in request.paths} | {
            config_path, self.MANIFEST_PRODUCT_PATH,
        }
        for commit in self.repository.rev_list(repo_root, f'{request.expected_head}..{head}'):
            changed = set(self.repository.git(repo_root, 'diff-tree', '--root', '-m',
                              '--no-commit-id', '--name-only', '-r', '-z', commit).stdout.split('\0'))
            if changed & protected:
                self._fail('journal HEAD lease overlaps operation or provider configuration paths')
        return policy

    def _observe_journal(self, request, *, before=False):
        root = self._repo_root(request)
        manifest, path = self._manifest(root)
        if manifest.get('repository_mode') != 'journal':
            self._fail('journal repository mode changed')
        if manifest.get('syncwheel_tracking') != SYNCWHEEL_TRACKING_GIT_TRACKED:
            self._fail('revision provider requires git-tracked metadata')
        if self.repository.git(root, 'ls-files', '--error-unmatch', '--', self.MANIFEST_PRODUCT_PATH,
               check=False).returncode:
            self._fail('revision provider requires a tracked manifest')
        branch = manifest['journal']['branch']
        if self.repository.get_current_branch(root) != branch:
            self._fail('revision provider requires the journal branch checkout')
        hooks = self.guards.managed_push_guard_policy(root, manifest)
        if hooks.get('required') and not hooks.get('ready') and not hooks.get('disabled'):
            self._fail('managed repository guards are not ready')
        if self._index_conflicts(root) or not self._index_is_clean(root):
            self._fail('journal provider requires a clean, conflict-free index')
        head = self.repository.ref_tip(root, 'HEAD')
        self._head_policy(root, request, head)
        digest = manifest_digest(manifest)
        if request.expected_manifest_digest and digest != request.expected_manifest_digest:
            self._fail('manifest digest changed')
        if self.MANIFEST_PRODUCT_PATH in self._dirty_paths(root, request):
            self._fail('journal manifest has uncommitted changes')
        if before:
            self._validate_hashes(root, request, 'before')
        else:
            self._validate_before_hashes_at_head(root, request)
            self._validate_hashes(root, request, 'after')
        index = self._index_observation(root)
        return {'baselineIndexSha256': index.sha256, 'baselineIndexSemantic': index.semantic,
                'indexAlignments': {}, 'repositoryMode': 'journal', 'manifestDigest': digest,
                'observedManifestDigest': digest, 'integrationBranch': branch,
                'operationParentHead': head, 'resultingHead': head,
                'unmappedIntegrationCommits': []}

    def check(self, request):
        return self._observe_journal(request, before=True)

    def _admit_paths(self, root, manifest, request):
        paths = {item.path for item in request.paths}
        journal = manifest['journal']
        for path in paths:
            if (not journal_path_matches(path, journal['include'])
                    or journal_path_matches(path, journal['exclude'])
                    or journal_path_sensitive(path)):
                self._fail('journal admission refuses operation path: ' + path)
        for path in paths:
            # A declared file is an input even when Git ignores it or hides it
            # from status. Read it through the provider's no-follow path reader.
            value = self._read_product_path(root, path)
            if value is None:
                continue
            if self.repository.git(root, 'check-ignore', '-q', '--', path, check=False).returncode == 0:
                self._fail('journal admission refuses ignored operation path: ' + path)
            if len(value['bytes']) > journal['max_file_bytes']:
                self._fail('journal admission rejected oversized content: ' + path)
            secret = journal_secret_reason(value['bytes'])
            if secret:
                self._fail('journal admission rejected high-confidence secret: ' + path)
        self._validate_hashes(root, request, 'after')

    def preflight(self, request):
        observation = self._observe_journal(request)
        root = self._repo_root(request)
        manifest, _ = self._manifest(root)
        self._admit_paths(root, manifest, request)
        return observation

    def verify_recovery_gate(self, request, journal):
        root = self._repo_root(request)
        manifest, _ = self._manifest(root)
        if manifest.get('repository_mode') != 'journal' or manifest_digest(manifest) != journal['observedManifestDigest']:
            self._fail('journal manifest changed after preflight')
        if self.repository.get_current_branch(root) != journal['integrationBranch']:
            self._fail('journal branch changed after preflight')
        current = self.repository.ref_tip(root, 'HEAD')
        allowed = {journal['operationParentHead'], journal.get('candidateProductCommitSha')}
        if current not in allowed:
            # Before object preparation a disjoint writer may advance HEAD.
            if journal['phase'] != 'prepared':
                self._fail('journal HEAD changed after commit preparation')
            if journal.get('candidateProductCommitSha'):
                raise self.provider.JournalHeadAdvanced('journal HEAD changed after commit preparation')
            self._head_policy(root, request, current)
            self._adopt_advanced_head(root, journal, current)
            self.save_journal(request, journal)
        hooks = self.guards.managed_push_guard_policy(root, manifest)
        if hooks.get('required') and not hooks.get('ready') and not hooks.get('disabled'):
            self._fail('managed repository guards are not ready')
        self._assert_no_ref_transaction_locks(root, {
            f"refs/heads/{journal['integrationBranch']}": None,
        })

    def _adopt_advanced_head(self, root, journal, current):
        if not self._index_is_clean(root):
            self._fail('journal HEAD advancement requires a clean index')
        journal['operationParentHead'] = current
        index = self._index_observation(root)
        journal.update(baselineIndexSha256=index.sha256, baselineIndexSemantic=index.semantic)

    def rebuild_stale_candidate(self, request, journal):
        candidate = journal.get('candidateProductCommitSha')
        if journal['phase'] != 'prepared' or not candidate:
            return
        root = self._repo_root(request)
        current = self.repository.ref_tip(root, 'HEAD')
        if current in {journal['operationParentHead'], candidate}:
            return
        self._head_policy(root, request, current)
        # the rebuilt commit must carry the bytes the operation already verified
        self._verify_worktree_objects(root, journal['productPathObjects'])
        self._adopt_advanced_head(root, journal, current)
        journal.update(candidateProductCommitSha=None, candidateProductTreeSha=None,
                       productPathObjects=None, productHooksValidated=False)
        self.save_journal(request, journal)

    def verify_after_paths(self, request):
        self._observe_journal(request)

    def prepare_product_commit(self, request, message):
        root = self._repo_root(request)
        journal = self.load_journal(request)
        manifest, _ = self._manifest(root)
        self._admit_paths(root, manifest, request)
        objects = self._capture_path_objects(root, request)
        prepared = self._prepare_exact_commit(root, journal['operationParentHead'], objects, message)
        if prepared is None:
            return None
        return {'candidateProductCommitSha': prepared['commit'],
                'candidateProductTreeSha': prepared['tree'], 'productPathObjects': objects}

    def validate_prepared_commit(self, request, journal, kind):
        root = self._repo_root(request)
        self.verify_recovery_gate(request, journal)
        self._verify_candidate_object(root, journal['candidateProductCommitSha'],
                                      journal['candidateProductTreeSha'],
                                      journal['operationParentHead'], journal['productPathObjects'])
        self._verify_worktree_objects(root, journal['productPathObjects'])
        before = self._hook_repository_snapshot(root, journal['productPathObjects'])
        self._run_commit_validation_hooks(root, journal['candidateProductCommitSha'])
        after = self._hook_repository_snapshot(root, journal['productPathObjects'])
        before_index = before.pop('index')
        after_index = after.pop('index')
        if before != after or before_index.semantic != after_index.semantic:
            self._fail('commit hook changed repository state')

    def publish_prepared_commit(self, request, commit, expected_parent):
        root = self._repo_root(request)
        journal = self.load_journal(request)
        self.verify_recovery_gate(request, journal)
        self._verify_worktree_objects(root, journal['productPathObjects'])
        current = self.repository.ref_tip(root, 'HEAD')
        if current == commit:
            return commit
        if current != expected_parent:
            raise self.provider.JournalHeadAdvanced('journal publication HEAD lease lost')
        if not self._index_is_clean(root):
            self._fail('journal publication index lease lost')
        self._assert_index_lease(root, request, journal, 'baseline', 'journal publication')
        ref = f"refs/heads/{journal['integrationBranch']}"
        self._cas_ref_with_leases(root, ref, commit,
                                 {ref: self._direct_ref_observation(ref, expected_parent)})
        self.checkpoint('integration_product_cas')
        return commit

    def _deterministic_index(self, repo_root, commit, *, allowed_worktree_paths=()):
        request = self._journal_alignment_request
        journal = self.load_journal(request)
        observed = self._index_observation(repo_root)
        # Preserve the real index's unrelated entries and extensions; only
        # operation entries are replaced. The inherited writer supplies durable
        # backing files, semantic leases, CAS, and proven owned-lock recovery.
        with self._private_index_copy(self._index_path(repo_root), observed.payload) as env:
            for path, entry in sorted(journal['productPathObjects'].items()):
                if entry['blob'] is None:
                    self.repository.git(repo_root, 'update-index', '--force-remove', '--', path, env=env)
                else:
                    self.repository.git(repo_root, 'update-index', '--add', '--cacheinfo',
                        f"{entry['mode']},{entry['blob']},{path}", env=env)
            payload = Path(env['GIT_INDEX_FILE']).read_bytes()
            if self.repository.git(repo_root, 'write-tree', env=env).stdout.strip() != self.repository.ref_tree(repo_root, commit):
                self._fail('journal replacement index does not match its exact-path commit')
            return payload, hashlib.sha256(payload).hexdigest()

    def align_index(self, request, commit):
        self._journal_alignment_request = request
        try:
            result = super().align_index(request, commit)
        finally:
            del self._journal_alignment_request
        self.checkpoint('integration_product_committed')
        return result

    def verify_journal_final(self, request, journal):
        self.verify_recovery_gate(request, journal)
        root = self._repo_root(request)
        self._verify_worktree_objects(root, journal['productPathObjects'])
        if not self._index_is_clean(root):
            self._fail('journal final index is not aligned')

    def verify_release(self, request, journal):
        self.verify_recovery_gate(request, journal)
        if journal.get('candidateProductCommitSha') and self.repository.ref_tip(
            self._repo_root(request), 'HEAD'
        ) == journal['candidateProductCommitSha']:
            self._fail('cannot release a published product commit; recover it instead')
        self._observe_journal(request)
