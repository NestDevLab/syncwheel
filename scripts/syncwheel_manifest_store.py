"""Durable manifest and lifecycle-ledger storage.

Transaction locks, write leases, fsync, control commits and crash recovery share
one owner. Repository, workspace tracking, projection and stack lifecycle effects
are explicit capabilities; caller-supplied repository paths remain explicit.
"""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import sys
import tempfile
import threading
import time
import uuid
from typing import Protocol

try:
    import fcntl
except ImportError:
    fcntl = None

from syncwheel_contracts import (
    iso_utc_now,
)
from syncwheel_errors import (
    ControlManifestAlignmentDrift,
    IntegrationReconciliationLeaseDrift,
    ManifestDurabilityError,
    MetadataRollbackIncomplete,
    SyncwheelError,
)
from syncwheel_files import (
    _write_all,
)
from syncwheel_ledger import (
    COORDINATION_PUBLICATION_TERMINAL_EVENT_TYPES,
    LEDGER_SCHEMA_VERSION,
    manifest_event_payload,
    reduce_ledger_state,
)
from syncwheel_manifest import (
    DEFAULT_SYNCWHEEL_WORKTREE_ROOT,
    SYNCWHEEL_TRACKING_LOCAL_ONLY,
    canonical_json_digest,
    control_manifest_stays_untracked,
    manifest_digest,
    normalize_manifest_data,
    normalize_syncwheel_tracking,
    normalize_syncwheel_worktree_root,
    stack_map,
)
from syncwheel_replay import (
    quoted,
)

ENV_TEST_CONTROL_MANIFEST_SIGKILL = 'SYNCWHEEL_TEST_CONTROL_MANIFEST_SIGKILL'

ENV_TEST_CONTROL_MANIFEST_PAUSE = 'SYNCWHEEL_TEST_CONTROL_MANIFEST_PAUSE'

ENV_TEST_CONTROL_MANIFEST_READY = 'SYNCWHEEL_TEST_CONTROL_MANIFEST_READY'

ENV_TEST_CONTROL_MANIFEST_RELEASE = 'SYNCWHEEL_TEST_CONTROL_MANIFEST_RELEASE'

CONTROL_MANIFEST_IDENTITY_VERSION = 2

CONTROL_MANIFEST_EXPLICIT_RECOVERY_IDENTITY_VERSION = 3

CONTROL_MANIFEST_LEGACY_IDENTITY_VERSION = 1

CONTROL_MANIFEST_IDENTITY_VERSIONS = (
    CONTROL_MANIFEST_IDENTITY_VERSION, CONTROL_MANIFEST_LEGACY_IDENTITY_VERSION,
    CONTROL_MANIFEST_EXPLICIT_RECOVERY_IDENTITY_VERSION,
)

LEDGER_SEGMENT_MAX_EVENTS = 256

_CHANNEL_MUTATION_LOCK_STATE = threading.local()

_MANIFEST_WRITE_TRANSACTION_STATE = threading.local()

_CONTROL_MANIFEST_LOCK_STATE = threading.local()

_LEDGER_FSYNC = os.fsync


class ManifestRepository(Protocol):
    def control_manifest_commit_env(self, *args, **kwargs): ...
    def find_worktree_for_branch(self, *args, **kwargs): ...
    def git(self, *args, **kwargs): ...
    def git_common_dir(self, *args, **kwargs): ...
    def git_identity(self, *args, **kwargs): ...
    def ref_tip(self, *args, **kwargs): ...
    def ref_tree(self, *args, **kwargs): ...
    def run(self, *args, **kwargs): ...


class ManifestProjection(Protocol):
    def apply_integration_reconciliation(self, *args, **kwargs): ...
    def checkout_path_fingerprint(self, *args, **kwargs): ...
    def checkout_source_lease(self, *args, **kwargs): ...


class ManifestWorkspace(Protocol):
    def commit_git_tracked_manifest(self, *args, **kwargs): ...
    def ensure_syncwheel_metadata_excluded(self, *args, **kwargs): ...


class ManifestLifecycle(Protocol):
    def require_current_stack_create_operation(self, *args, **kwargs): ...


def load_manifest(repo_root, manifest_path=None):
    path = Path(manifest_path) if manifest_path else repo_root / '.syncwheel' / 'manifest.json'
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, encoding='utf-8') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SyncwheelError(f'manifest path is not a regular file: {path}')
            data = json.load(stream)
    except FileNotFoundError:
        return None, path
    except json.JSONDecodeError as exc:
        raise SyncwheelError(f'invalid manifest JSON: {exc}') from exc
    return normalize_manifest_data(data, path), path


def save_manifest(path, manifest):
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = None
    if path.exists() or path.is_symlink():
        existing = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(existing.st_mode):
            raise SyncwheelError(f'manifest path is not a regular file: {path}')
        existing_mode = stat.S_IMODE(existing.st_mode)
    payload = (json.dumps(manifest, indent=2) + '\n').encode('utf-8')
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{path.name}.tmp-', dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    replaced = False
    try:
        if existing_mode is not None:
            os.fchmod(descriptor, existing_mode)
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        require_manifest_transaction_current(path)
        os.replace(temporary_path, path)
        replaced = True
        transaction = active_manifest_write_transaction(path)
        if transaction is not None:
            persisted, _ = load_manifest(transaction['repoRoot'], path)
            transaction['expectedDigest'] = (
                manifest_digest(persisted) if persisted is not None else None
            )
            transaction['manifestWritten'] = True
        directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
        try:
            directory_fd = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise ManifestDurabilityError(
                f'manifest replace completed but parent directory durability check failed; '
                f'outcome is unknown: {path}'
            ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not replaced:
            temporary_path.unlink(missing_ok=True)


def external_ledger_root(manifest_path):
    path = Path(manifest_path).expanduser()
    stem = path.stem
    if stem.endswith('-manifest'):
        trimmed = stem[:-len('-manifest')]
        stem = trimmed or stem
    return path.parent / f'{stem}-ledger'


def is_external_manifest_path(repo_root, manifest_path):
    if manifest_path is None:
        return False
    repo_root_path = Path(repo_root).expanduser().resolve(strict=False)
    manifest_root = Path(manifest_path).expanduser().resolve(strict=False)
    try:
        manifest_root.relative_to(repo_root_path)
        return False
    except ValueError:
        return True


def is_personal_manifest_path(repo_root, manifest_path):
    if manifest_path is None:
        return False
    repo_root_path = Path(repo_root).expanduser().resolve(strict=False)
    manifest_root = Path(manifest_path).expanduser().resolve(strict=False)
    try:
        relative = manifest_root.relative_to(repo_root_path)
    except ValueError:
        return False
    return (
        relative.parent == Path('.syncwheel/manifests')
        and relative.name.endswith('.local.json')
    )


def ledger_root(repo_root, manifest_path=None):
    if manifest_path is None:
        return repo_root / '.syncwheel' / 'ledger'
    repo_root_path = Path(repo_root).expanduser().resolve(strict=False)
    manifest_root = Path(manifest_path).expanduser().resolve(strict=False)
    if is_external_manifest_path(repo_root_path, manifest_root) or is_personal_manifest_path(
        repo_root_path, manifest_root
    ):
        return external_ledger_root(manifest_path)
    return repo_root / '.syncwheel' / 'ledger'


def ledger_events_dir(repo_root, manifest_path=None):
    return ledger_root(repo_root, manifest_path) / 'events'


def ledger_checkpoints_dir(repo_root, manifest_path=None):
    return ledger_root(repo_root, manifest_path) / 'checkpoints'


def active_manifest_write_transaction(manifest_path):
    transactions = getattr(_MANIFEST_WRITE_TRANSACTION_STATE, 'transactions', {})
    return transactions.get(str(Path(manifest_path).resolve(strict=False)))


def active_manifest_write_transaction_for_repo(repo_root):
    transactions = getattr(_MANIFEST_WRITE_TRANSACTION_STATE, 'transactions', {})
    resolved_root = Path(repo_root).resolve()
    matches = [
        transaction for transaction in transactions.values()
        if Path(transaction['repoRoot']).resolve() == resolved_root
    ]
    return matches[0] if len(matches) == 1 else None


def require_manifest_transaction_current(manifest_path):
    transaction = active_manifest_write_transaction(manifest_path)
    if transaction is None:
        return
    current, _ = load_manifest(transaction['repoRoot'], manifest_path)
    current_digest = manifest_digest(current) if current is not None else None
    if current_digest != transaction['expectedDigest']:
        raise SyncwheelError(
            'manifest changed outside the active transaction; refusing stale write or side effect'
        )


def require_locked_manifest_observation(repo_root, manifest_path, plan):
    current, _ = load_manifest(repo_root, manifest_path)
    if not current or manifest_digest(current) != plan.get('manifestDigestBefore'):
        raise SyncwheelError(
            'channel plan is stale under mutation lock; manifest changed before apply'
        )
    return current


def channel_mutation_checkpoint():
    """Fault-injection seam immediately before an authoritative channel mutation."""
    return None


def ledger_checkpoint_path(repo_root, manifest_path=None):
    return ledger_checkpoints_dir(repo_root, manifest_path) / 'latest.json'


def ledger_io_checkpoint(stage):
    """Fault-injection seam around physically durable ledger writes."""
    return None


def ledger_fsync_directory_path(directory):
    descriptor = os.open(str(directory), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        _LEDGER_FSYNC(descriptor)
    finally:
        os.close(descriptor)


def ensure_directory_durable(directory):
    directory = Path(directory)
    missing = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    directory.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        ledger_fsync_directory_path(created.parent)
        ledger_fsync_directory_path(created)


@contextlib.contextmanager
def ledger_write_lock(repo_root, manifest_path=None):
    if fcntl is None:
        raise SyncwheelError('ledger writes require POSIX file locking support')
    root = ledger_root(repo_root, manifest_path)
    ensure_directory_durable(root)
    lock_path = root / 'ledger.lock'
    existed = lock_path.exists()
    with lock_path.open('a+b') as handle:
        if not existed:
            handle.flush()
            _LEDGER_FSYNC(handle.fileno())
            ledger_fsync_directory_path(root)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _repair_incomplete_ledger_tail(path):
    """Repair only the final unterminated JSONL frame.

    A complete JSON value without its delimiter is finished by appending the
    delimiter. An invalid unterminated suffix is the only data we truncate;
    newline-terminated corruption is never rewritten or hidden.
    """
    path = Path(path)
    if not path.exists():
        return 'unchanged'
    payload = path.read_bytes()
    if not payload or payload.endswith(b'\n'):
        return 'unchanged'
    boundary = payload.rfind(b'\n') + 1
    tail = payload[boundary:]
    try:
        parsed = json.loads(tail.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        with path.open('r+b') as handle:
            handle.truncate(boundary)
            handle.flush()
            _LEDGER_FSYNC(handle.fileno())
        ledger_fsync_directory_path(path.parent)
        return 'truncated-incomplete-tail'
    if not isinstance(parsed, dict):
        raise SyncwheelError(
            f'invalid unterminated ledger event in {path}: expected a JSON object'
        )
    with path.open('ab') as handle:
        handle.write(b'\n')
        handle.flush()
        _LEDGER_FSYNC(handle.fileno())
    ledger_fsync_directory_path(path.parent)
    return 'completed-delimiter'


def _recover_ledger_tail_unlocked(repo_root, manifest_path=None):
    directory = ledger_events_dir(repo_root, manifest_path)
    if not directory.exists():
        return 'unchanged'
    segments = sorted(directory.glob('*.jsonl'))
    if not segments:
        return 'unchanged'
    return _repair_incomplete_ledger_tail(segments[-1])


def recover_ledger_tail(repo_root, manifest_path=None):
    with ledger_write_lock(repo_root, manifest_path):
        return _recover_ledger_tail_unlocked(repo_root, manifest_path)


def load_repaired_ledger_events(repo_root, manifest_path=None):
    """Read ledger events only after repairing the durable tail under lock."""
    with ledger_write_lock(repo_root, manifest_path):
        _recover_ledger_tail_unlocked(repo_root, manifest_path)
        return load_ledger_events(repo_root, manifest_path)


def load_control_manifest_events(repo_root, manifest_path=None):
    """Avoid creating ledger metadata when there is no control history to read."""
    directory = ledger_events_dir(repo_root, manifest_path)
    if not directory.exists() or not any(directory.glob('*.jsonl')):
        return []
    return load_repaired_ledger_events(repo_root, manifest_path)


def canonical_manifest_file_text(manifest):
    """Return deterministic, human-readable bytes for manifest-bearing commits."""
    return json.dumps(manifest, indent=2, sort_keys=True) + '\n'


def load_ledger_events(repo_root, manifest_path=None):
    directory = ledger_events_dir(repo_root, manifest_path)
    if not directory.exists():
        return []
    events = []
    for path in sorted(directory.glob('*.jsonl')):
        payload = path.read_bytes()
        if payload and not payload.endswith(b'\n'):
            raise SyncwheelError(
                f'incomplete ledger tail in {path}; recover it before reading'
            )
        for line_number, raw_line in enumerate(payload.split(b'\n')[:-1], start=1):
            if not raw_line.strip():
                continue
            try:
                data = json.loads(raw_line.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SyncwheelError(
                    f'invalid ledger event in {path}:{line_number}: {exc}'
                ) from exc
            if not isinstance(data, dict):
                raise SyncwheelError(
                    f'invalid ledger event in {path}:{line_number}: expected object'
                )
            events.append(data)
    return events


def load_ledger_state(repo_root, manifest_path=None):
    reduced = reduce_ledger_state(load_ledger_events(repo_root, manifest_path))
    path = ledger_checkpoint_path(repo_root, manifest_path)
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data == reduced:
            return data
    return reduced


def _write_ledger_checkpoint_unlocked(repo_root, state, manifest_path=None):
    path = ledger_checkpoint_path(repo_root, manifest_path)
    ensure_directory_durable(path.parent)
    encoded = (json.dumps(state, indent=2, sort_keys=True) + '\n').encode('utf-8')
    descriptor, temporary_name = tempfile.mkstemp(
        prefix='.latest.', suffix='.tmp', dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        midpoint = max(1, len(encoded) // 2)
        _write_all(descriptor, encoded[:midpoint])
        ledger_io_checkpoint('checkpoint_payload_half_written')
        _write_all(descriptor, encoded[midpoint:])
        ledger_io_checkpoint('checkpoint_payload_written')
        _LEDGER_FSYNC(descriptor)
        ledger_io_checkpoint('checkpoint_temp_fsynced')
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        ledger_fsync_directory_path(path.parent)
        ledger_io_checkpoint('checkpoint_replaced')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def private_tracking_file_image(path):
    """Observe data and its file identity without following redirects."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise SyncwheelError(f'private tracking requires a regular, unlinked file: {path}')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            return (observed.st_dev, observed.st_ino, stat.S_IMODE(observed.st_mode), stream.read())
    finally:
        os.close(descriptor)


def remove_private_tracking_temporaries(temporary_paths, *, applied, operation):
    for path in reversed(temporary_paths):
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            outcome = 'applied' if applied else 'interrupted'
            print(
                f'warning: {operation} {outcome}; could not remove temporary file {path}: {error}',
                file=sys.stderr,
            )


@contextlib.contextmanager
def private_tracking_temporaries(operation='personal tracking'):
    temporary_paths = []
    try:
        yield temporary_paths
    except BaseException:
        remove_private_tracking_temporaries(temporary_paths, applied=False, operation=operation)
        raise
    else:
        remove_private_tracking_temporaries(temporary_paths, applied=True, operation=operation)


def stage_private_tracking_file(temporary_paths, path, mode, payload, *, append_at=None):
    ensure_directory_durable(path.parent)
    descriptor, name = tempfile.mkstemp(prefix='.tracking-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    temporary_paths.append(temporary)
    with os.fdopen(descriptor, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        if append_at is None:
            stream.write(payload)
        else:
            stream.write(payload[:append_at])
            stream.flush()
            ledger_io_checkpoint('event_payload_half_written')
            stream.write(payload[append_at:])
        stream.flush()
        _LEDGER_FSYNC(stream.fileno())
    return temporary, private_tracking_file_image(temporary)


def publish_private_tracking_files(writes, temporary_paths, operation='personal tracking'):
    """Publish staged bytes and undo only the files this transaction still owns."""
    installed = []
    try:
        for path, before, staged, after, rollback, phase in writes:
            if private_tracking_file_image(path) != before:
                raise SyncwheelError(f'{operation} metadata changed before write: {path}')
            os.replace(staged, path)
            installed.append((path, after, rollback))
            if phase != 'event_fsynced':
                ledger_io_checkpoint(phase)
            ledger_fsync_directory_path(path.parent)
            if phase == 'event_fsynced':
                ledger_io_checkpoint(phase)
    except BaseException as failure:
        rollback_errors = []
        for path, after, rollback in reversed(installed):
            restored = False
            try:
                if private_tracking_file_image(path) != after:
                    raise SyncwheelError('file changed after write')
                if rollback is None:
                    path.unlink()
                else:
                    os.replace(rollback, path)
                restored = True
                ledger_fsync_directory_path(path.parent)
            except (OSError, SyncwheelError) as error:
                detail = f'{path}: {error}'
                if rollback is not None and not restored:
                    temporary_paths.remove(rollback)
                    detail += f'; original image retained at {rollback}'
                rollback_errors.append(detail)
        if rollback_errors:
            raise MetadataRollbackIncomplete(
                f'{operation} write failed ({failure}); rollback incomplete: '
                + ', '.join(rollback_errors)
            ) from failure
        raise



@contextlib.contextmanager
def prepare_capture_metadata(repo_root, manifest_path, manifest, rebuild, context):
    operation = 'stack capture-integration'
    transaction = active_manifest_write_transaction(manifest_path)
    if transaction is None:
        raise SyncwheelError('stack capture-integration requires a manifest transaction')
    with ledger_write_lock(repo_root, manifest_path), private_tracking_temporaries(operation) as temporary_paths:
        require_manifest_transaction_current(manifest_path)
        events = load_ledger_events(repo_root, manifest_path)
        if pending_control_manifest_intents(events):
            raise SyncwheelError('control manifest recovery became pending before capture; run capture again for the selected recovery command')
        state = reduce_ledger_state(events)
        payload = manifest_event_payload(manifest_path, manifest, 'stack_capture_integration', context)
        segment = next_ledger_segment_path(repo_root, manifest_path, create_directory=False)
        segments = {}
        for kind, data in (('stack_rebuilt', rebuild), ('manifest_saved', payload)):
            if segment not in segments:
                before = private_tracking_file_image(segment)
                segments[segment] = (before, before[3] if before else b'')
            before, records = segments[segment]
            event = {
                'schema_version': LEDGER_SCHEMA_VERSION,
                'seq': state['last_seq'] + 1,
                'ts': iso_utc_now(),
                'type': kind,
                'payload': data,
            }
            events.append(event)
            state = reduce_ledger_state(events)
            records += (json.dumps(event, sort_keys=True) + '\n').encode('utf-8')
            segments[segment] = (before, records)
            if records.count(b'\n') >= LEDGER_SEGMENT_MAX_EVENTS:
                segment = segment.with_name(f'{int(segment.stem) + 1:06d}.jsonl')
        before_manifest = private_tracking_file_image(manifest_path)
        if before_manifest is None:
            raise SyncwheelError('capture manifest disappeared before apply')
        checkpoint = ledger_checkpoint_path(repo_root, manifest_path)
        plan = [
            (manifest_path, before_manifest, (json.dumps(manifest, indent=2) + '\n').encode(),
             'capture_manifest_replaced'),
            *((path, before, records, 'event_fsynced')
              for path, (before, records) in segments.items()),
            (checkpoint, private_tracking_file_image(checkpoint),
             (json.dumps(state, indent=2, sort_keys=True) + '\n').encode(), 'checkpoint_replaced'),
        ]
        writes = []
        for path, before, contents, phase in plan:
            mode = before[2] if before else 0o600
            staged, after = stage_private_tracking_file(temporary_paths, path, mode, contents)
            rollback = stage_private_tracking_file(temporary_paths, path, mode, before[3])[0] if before else None
            writes.append((path, before, staged, after, rollback, phase))
        require_manifest_transaction_current(manifest_path)
        after_digest = manifest_digest(normalize_manifest_data(manifest, manifest_path))
        yield
        publish_private_tracking_files(writes, temporary_paths, operation)
        transaction['expectedDigest'] = after_digest
        transaction['manifestWritten'] = True


def write_ledger_checkpoint(repo_root, state, manifest_path=None):
    with ledger_write_lock(repo_root, manifest_path):
        _recover_ledger_tail_unlocked(repo_root, manifest_path)
        authoritative = reduce_ledger_state(
            load_ledger_events(repo_root, manifest_path)
        )
        # The append-only event log is authoritative if the caller raced and
        # supplied an older derived checkpoint state.
        _write_ledger_checkpoint_unlocked(repo_root, authoritative, manifest_path)


def next_ledger_segment_path(repo_root, manifest_path=None, *, create_directory=True):
    directory = ledger_events_dir(repo_root, manifest_path)
    if create_directory:
        ensure_directory_durable(directory)
    segments = sorted(directory.glob('*.jsonl'))
    if not segments:
        return directory / '000001.jsonl'
    current = segments[-1]
    with current.open() as handle:
        line_count = sum(1 for _ in handle)
    if line_count < LEDGER_SEGMENT_MAX_EVENTS:
        return current
    next_index = int(current.stem) + 1
    return directory / f'{next_index:06d}.jsonl'


def refuse_duplicate_terminal_publication_event(state, event_type, payload):
    """Keep exactly one terminal event per operation token.

    The refusal belongs to the write: a ledger that already carries two must
    stay readable, or the command an operator would inspect it with is the one
    that fails.
    """
    if event_type not in COORDINATION_PUBLICATION_TERMINAL_EVENT_TYPES:
        return
    token = (payload or {}).get('operation_token')
    if not token:
        return
    record = (state.get('coordination_publications') or {}).get(token) or {}
    if not record.get('terminal'):
        return
    raise SyncwheelError(
        'coordinated publication ledger refuses a second terminal event for '
        f'operation token {token}: {record["terminal"]} at seq '
        f'{record.get("terminal_seq")} already stands\n'
        'inspect the recorded terminal with:\n  syncwheel ledger show'
    )


def control_manifest_difference(manifest, observed):
    """Describe every material control-manifest difference for a safe retry."""
    expected_stacks = list((manifest or {}).get('stacks') or [])
    observed_stacks = list((observed or {}).get('stacks') or [])
    expected_ids_in_order = [stack.get('id') for stack in expected_stacks]
    observed_ids_in_order = [stack.get('id') for stack in observed_stacks]
    expected_ids = set(expected_ids_in_order)
    observed_ids = set(observed_ids_in_order)
    parts = []
    missing = sorted(expected_ids - observed_ids)
    unexpected = sorted(observed_ids - expected_ids)
    if missing:
        parts.append('missing stacks: ' + ', '.join(missing))
    if unexpected:
        parts.append('unexpected stacks: ' + ', '.join(unexpected))
    if expected_ids_in_order != observed_ids_in_order:
        parts.append('stack order differs')
    expected_by_id = stack_map(manifest or {})
    observed_by_id = stack_map(observed or {})
    for stack_id in sorted(expected_ids & observed_ids):
        expected = expected_by_id[stack_id]
        actual = observed_by_id[stack_id]
        for field, label in (('base', 'base'), ('branch', 'branch'), ('commits', 'commits')):
            if expected.get(field) != actual.get(field):
                parts.append(f'{stack_id} {label} differs')
        ignored = {'id', 'base', 'branch', 'commits'}
        expected_config = {key: value for key, value in expected.items() if key not in ignored}
        observed_config = {key: value for key, value in actual.items() if key not in ignored}
        if expected_config != observed_config:
            parts.append(f'{stack_id} configuration differs')
    if (manifest or {}).get('integration') != (observed or {}).get('integration'):
        parts.append('integration configuration differs')
    return '; '.join(parts) or 'manifest definitions differ'


def preflight_control_manifest_digest(repo_root, manifest_path, manifest):
    """Fail closed if the on-disk manifest changed after command observation."""
    observed, _ = load_manifest(repo_root, manifest_path)
    expected_digest = manifest_digest(manifest)
    observed_digest = manifest_digest(observed) if observed is not None else None
    if observed_digest == expected_digest:
        return
    manifest_path = Path(manifest_path)
    expected_payload = canonical_manifest_file_text(manifest)
    remedy = (
        'expected=$(mktemp "${TMPDIR:-/tmp}/syncwheel-expected-manifest.XXXXXX.json") '
        f'&& printf %s {shlex.quote(expected_payload)} > "$expected" '
        f'&& diff -u {shlex.quote(str(manifest_path))} "$expected"; '
        'printf "expected manifest: %s\\n" "$expected"'
    )
    raise SyncwheelError(
        'control manifest differs before integration rebuild: '
        f'expected digest {expected_digest}, found {observed_digest}; '
        + control_manifest_difference(manifest, observed)
        + f'. Restore the control manifest before retrying: {remedy}'
    )


def integration_manifest_path(repo_root):
    """The manifest path carried by every integration projection."""
    return Path(repo_root) / '.syncwheel' / 'manifest.json'


def control_manifest_intent_idempotency_key(operation_id):
    return f'control-manifest-intent:{operation_id}'


def control_manifest_event_idempotency_key(
    manifest, replay_tip, control_commit, command, operation_id,
):
    identity = {
        'operation_id': operation_id,
        'integration_branch': manifest['integration']['branch'],
        'command': command,
        'parent': replay_tip,
        'control_commit': control_commit,
    }
    return 'control-manifest:' + canonical_json_digest(identity)


def control_manifest_operation_records(events, control_commit):
    intents = {}
    receipts = {}
    for event in events:
        payload = event.get('payload') or {}
        operation_id = payload.get('operation_id')
        if not operation_id:
            continue
        if (
            event.get('type') == 'control_manifest_persistence_intent'
            and payload.get('expected_control_commit') == control_commit
        ):
            if operation_id in intents:
                raise SyncwheelError(
                    f'control manifest ledger contains duplicate intents for {operation_id}'
                )
            intents[operation_id] = event
        if (
            event.get('type') in {
                'manifest_saved', 'control_manifest_persistence_abandoned',
            }
            and (
                payload.get('control_commit') == control_commit
                or payload.get('expected_control_commit') == control_commit
            )
        ):
            if operation_id in receipts:
                raise SyncwheelError(
                    f'control manifest ledger contains duplicate terminal records for {operation_id}'
                )
            receipts[operation_id] = event
    return intents, receipts


def pending_control_manifest_intents(events, branch=None):
    """Return intents without a receipt or explicit abandonment, independent of ref state."""
    intents = {}
    terminal = {}
    for event in events:
        payload = event.get('payload') or {}
        operation_id = payload.get('operation_id')
        if not operation_id:
            continue
        if event.get('type') == 'control_manifest_persistence_intent':
            if operation_id in intents:
                raise SyncwheelError(
                    f'control manifest ledger contains duplicate intents for {operation_id}'
                )
            intents[operation_id] = event
        elif event.get('type') in {
            'manifest_saved', 'control_manifest_persistence_abandoned',
        }:
            if operation_id in terminal:
                raise SyncwheelError(
                    f'control manifest ledger contains duplicate terminal records for {operation_id}'
                )
            terminal[operation_id] = event
    pending = [
        event for operation_id, event in intents.items()
        if operation_id not in terminal
        and (
            branch is None
            or (event.get('payload') or {}).get('integration_branch') == branch
        )
    ]
    return sorted(pending, key=lambda event: event.get('seq', 0))


def control_manifest_operation_intent(
    events, control_commit, replay_tip, expected_digest, branch, operation_id=None,
):
    intents, receipts = control_manifest_operation_records(events, control_commit)
    matching = []
    for candidate_id, event in intents.items():
        payload = event['payload']
        if operation_id is not None and candidate_id != operation_id:
            continue
        if (
            payload.get('replay_tip') == replay_tip
            and payload.get('expected_manifest_digest') == expected_digest
            and payload.get('integration_branch') == branch
        ):
            matching.append(event)
    pending = [
        event for event in matching
        if event['payload']['operation_id'] not in receipts
    ]
    if len(pending) > 1:
        raise SyncwheelError(
            f'control manifest ledger contains ambiguous incomplete intents for {control_commit}'
        )
    if operation_id is not None and not matching:
        raise SyncwheelError(
            f'control manifest ledger is missing intent {operation_id}'
        )
    return (pending[0] if pending else None), matching, receipts


def control_manifest_io_checkpoint(stage):
    """Fault-injection seam for the control-manifest commit transaction."""
    if os.environ.get(ENV_TEST_CONTROL_MANIFEST_SIGKILL) == stage:
        os.kill(os.getpid(), signal.SIGKILL)
    if os.environ.get(ENV_TEST_CONTROL_MANIFEST_PAUSE) == stage:
        ready = os.environ.get(ENV_TEST_CONTROL_MANIFEST_READY)
        release = os.environ.get(ENV_TEST_CONTROL_MANIFEST_RELEASE)
        if not ready or not release:
            raise SyncwheelError(
                'control-manifest pause checkpoint requires ready and release paths'
            )
        ready_path = Path(ready)
        ready_path.write_text(stage + '\n')
        deadline = time.monotonic() + 20
        release_path = Path(release)
        while not release_path.exists():
            if time.monotonic() >= deadline:
                raise SyncwheelError(
                    f'control-manifest pause checkpoint timed out at {stage}'
                )
            time.sleep(0.01)
    return None


def control_manifest_relative_path(repo_root):
    return integration_manifest_path(repo_root).relative_to(
        Path(repo_root).resolve()
    ).as_posix()


def control_manifest_source_allowance(repo_root, manifest_path):
    """The integration control manifest is a rebuild input, never blocking dirt."""
    if manifest_path is None or is_external_manifest_path(repo_root, manifest_path):
        return set()
    relative = control_manifest_relative_path(repo_root)
    try:
        actual = Path(manifest_path).resolve(strict=False).relative_to(
            Path(repo_root).resolve()
        ).as_posix()
    except ValueError:
        return set()
    if actual != relative:
        return set()
    return {relative, Path(relative).parent.as_posix() + '/'}


def restore_guarded_control_manifests(guarded):
    """Put back only the guarded sources the replay removed."""
    for path, payload in guarded or ():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def control_manifest_obstruction_refusal(obstruction, command):
    return (
        f"integration checkout {obstruction['worktree']} carries changes the "
        'control-manifest alignment must not overwrite ('
        + ', '.join(obstruction['display'])
        + '); the integration ref was not moved. Commit, stash or discard them '
        f'there, then rerun: {command}'
    )


def control_manifest_obstruction_warning(obstruction, command):
    return (
        'the control manifest was persisted, but integration checkout '
        f"{obstruction['worktree']} still carries uncommitted changes ("
        + ', '.join(obstruction['display'])
        + '); only the control manifest was aligned there. Commit, stash or '
        f'discard them, then rerun: {command}'
    )


def control_manifest_divergence_remedy(repo_root, manifest_path):
    command = ['syncwheel', 'int', 'rebuild']
    if is_external_manifest_path(repo_root, manifest_path):
        command.extend(['--manifest', str(manifest_path)])
    command.extend([
        '--reason', 'adopt reviewed control manifest proposal',
    ])
    return quoted(command)


def explicit_control_recovery_intent(intent):
    # Also quarantine older, unscoped recover-control intents. They must not
    # silently regain the ordinary metadata writer on a newer client either.
    return (intent.get('command') == 'syncwheel manifest recover-control'
            or intent.get('control_identity') == CONTROL_MANIFEST_EXPLICIT_RECOVERY_IDENTITY_VERSION
            or intent.get('recovery_policy') == 'explicit-plan-only')


def explicit_control_recovery_refusal():
    return ('interrupted explicit control recovery is quarantined; requested command '
            'was not executed. Preserve the checkout and obtain a fresh '
            'manifest recover-control --from-backup BACKUP plan before explicit apply')


def pending_explicit_control_recoveries(repo_root, manifest_path):
    # An alternate/personal control source cannot hide a canonical operation
    # that protects this repository's integration checkout. De-duplicate ledger
    # locations, not caller-selected candidate objects or operation identities.
    seen = set()
    explicit = []
    for source in (integration_manifest_path(repo_root), manifest_path):
        location = ledger_root(repo_root, source).resolve()
        if location in seen:
            continue
        seen.add(location)
        explicit.extend(event['payload'] for event in pending_control_manifest_intents(
            load_control_manifest_events(repo_root, source)
        ) if explicit_control_recovery_intent(event['payload']))
    return explicit


def repo_relative_path(repo_root, path):
    try:
        return Path(path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return None


def status_line_paths(line):
    if len(line) < 4:
        return set()
    payload = line[3:]
    if ' -> ' in payload:
        return {part.strip('"') for part in payload.split(' -> ')}
    return {payload.strip('"')}


def manifest_policy_from_file(manifest_path):
    try:
        data = json.loads(Path(manifest_path).read_text())
    except (OSError, json.JSONDecodeError):
        return None, DEFAULT_SYNCWHEEL_WORKTREE_ROOT
    if not isinstance(data, dict):
        return None, DEFAULT_SYNCWHEEL_WORKTREE_ROOT
    tracking = normalize_syncwheel_tracking(data.get('syncwheel_tracking'), str(manifest_path))
    worktree_root = normalize_syncwheel_worktree_root(data.get('syncwheel_worktree_root'), str(manifest_path))
    return tracking, worktree_root


class ManifestStore:
    def __init__(self, repository: ManifestRepository, projection: ManifestProjection,
                 workspace: ManifestWorkspace, lifecycle: ManifestLifecycle):
        self.repository = repository
        self.projection = projection
        self.workspace = workspace
        self.lifecycle = lifecycle


    def control_manifest_lock_path(self, repo_root, branch):
        """Return the clone-local lock shared by every manifest source for a branch."""
        common_path = self.repository.git_common_dir(repo_root)
        lock_key = hashlib.sha256(
            f'{common_path}\0{branch}'.encode('utf-8')
        ).hexdigest()[:32]
        return common_path / 'syncwheel-locks' / f'control-manifest-{lock_key}.lock'


    @contextlib.contextmanager
    def control_manifest_branch_lock(self, repo_root, branch):
        """Serialize control persistence by repository and integration branch."""
        if fcntl is None:
            raise SyncwheelError('control manifest persistence requires POSIX file locking support')
        lock_path = self.control_manifest_lock_path(repo_root, branch)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        identity = str(lock_path)
        held = getattr(_CONTROL_MANIFEST_LOCK_STATE, 'held', None)
        if held is None:
            held = {}
            _CONTROL_MANIFEST_LOCK_STATE.held = held
        if identity in held:
            held[identity] += 1
            try:
                yield lock_path
            finally:
                held[identity] -= 1
            return
        with lock_path.open('a+') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[identity] = 1
            try:
                yield lock_path
            finally:
                held.pop(identity, None)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


    @contextlib.contextmanager
    def channel_mutation_lock(self, repo_root, manifest_path, channel_id):
        if fcntl is None:
            raise SyncwheelError('channel mutations require POSIX file locking support')
        if is_external_manifest_path(repo_root, manifest_path):
            lock_dir = ledger_root(repo_root, manifest_path) / 'locks'
        else:
            common_dir = self.repository.git(repo_root, 'rev-parse', '--git-common-dir').stdout.strip()
            common_path = Path(common_dir)
            if not common_path.is_absolute():
                common_path = repo_root / common_path
            lock_dir = common_path.resolve(strict=False) / 'syncwheel-locks'
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_key = hashlib.sha256(
            str(Path(manifest_path).resolve(strict=False)).encode('utf-8')
        ).hexdigest()[:32]
        lock_path = lock_dir / f'manifest-{lock_key}.lock'
        held = getattr(_CHANNEL_MUTATION_LOCK_STATE, 'held', None)
        if held is None:
            held = {}
            _CHANNEL_MUTATION_LOCK_STATE.held = held
        lock_identity = str(lock_path.resolve(strict=False))
        if lock_identity in held:
            held[lock_identity] += 1
            try:
                yield lock_path
            finally:
                held[lock_identity] -= 1
            return
        with lock_path.open('a+') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            held[lock_identity] = 1
            try:
                yield lock_path
            finally:
                held.pop(lock_identity, None)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


    @contextlib.contextmanager
    def manifest_write_transaction(
        self, repo_root, manifest_path, owner='manifest-command', *, commit_tracked_changes=True,
    ):
        identity = str(Path(manifest_path).resolve(strict=False))
        transactions = getattr(_MANIFEST_WRITE_TRANSACTION_STATE, 'transactions', None)
        if transactions is None:
            transactions = {}
            _MANIFEST_WRITE_TRANSACTION_STATE.transactions = transactions
        existing = transactions.get(identity)
        if existing is not None:
            existing['depth'] += 1
            try:
                yield
            finally:
                existing['depth'] -= 1
            return
        with self.channel_mutation_lock(repo_root, manifest_path, owner):
            observed, _ = load_manifest(repo_root, manifest_path)
            transaction = {
                'repoRoot': repo_root,
                'expectedDigest': manifest_digest(observed) if observed is not None else None,
                'manifestWritten': False,
                'gitignoreWritten': False,
                'depth': 1,
            }
            transactions[identity] = transaction
            try:
                yield
                if commit_tracked_changes and (
                    transaction['manifestWritten'] or transaction['gitignoreWritten']
                ):
                    self.workspace.commit_git_tracked_manifest(
                        repo_root,
                        manifest_path,
                        include_manifest=transaction['manifestWritten'],
                        include_gitignore=transaction['gitignoreWritten'],
                    )
            finally:
                transactions.pop(identity, None)


    def require_ignored_ledger_paths(self, repo_root, paths):
        """Verify frozen recovery metadata without rewriting exclusion files."""
        relatives = [repo_relative_path(repo_root, path) for path in paths]
        if not all(relatives) or self.repository.git(repo_root, 'ls-files', '--stage', '--', *relatives).stdout:
            raise SyncwheelError('control recovery requires untracked, already ignored ledger metadata')
        ignored = self.repository.git(repo_root, 'check-ignore', '--no-index', '-z', '--stdin',
                      input_text='\0'.join(relatives) + '\0', check=False)
        if ignored.returncode != 0 or set(ignored.stdout.strip('\0').split('\0')) != set(relatives):
            raise SyncwheelError('control recovery requires untracked, already ignored ledger metadata')


    def append_ledger_event(
        self, repo_root, event_type, payload, manifest_path=None, idempotency_key=None,
    ):
        transaction = active_manifest_write_transaction_for_repo(repo_root)
        preserve_ignore = transaction is not None and transaction.get('preserveGitignore') is True
        if preserve_ignore:
            self.require_ignored_ledger_paths(repo_root, [
                ledger_root(repo_root, manifest_path) / 'ledger.lock',
                ledger_checkpoint_path(repo_root, manifest_path),
                ledger_events_dir(repo_root, manifest_path) / '000001.jsonl',
            ])
        require_manifest_transaction_current(
            manifest_path or repo_root / '.syncwheel' / 'manifest.json'
        )
        with ledger_write_lock(repo_root, manifest_path):
            _recover_ledger_tail_unlocked(repo_root, manifest_path)
            events = load_ledger_events(repo_root, manifest_path)
            if idempotency_key is not None:
                payload = dict(payload)
                payload['idempotency_key'] = idempotency_key
                matching = [
                    event for event in events
                    if event.get('type') == event_type
                    and (
                        event.get('idempotency_key') == idempotency_key
                        or (event.get('payload') or {}).get('idempotency_key')
                        == idempotency_key
                    )
                ]
                if matching:
                    if len(matching) != 1:
                        raise SyncwheelError(
                            f'ledger idempotency collision: {idempotency_key}'
                        )
                    return matching[0]
            current = reduce_ledger_state(events)
            refuse_duplicate_terminal_publication_event(current, event_type, payload)
            event = {
                'schema_version': LEDGER_SCHEMA_VERSION,
                'seq': current['last_seq'] + 1,
                'ts': iso_utc_now(),
                'type': event_type,
                'payload': payload,
            }
            if idempotency_key is not None:
                event['idempotency_key'] = idempotency_key
            path = next_ledger_segment_path(repo_root, manifest_path)
            if preserve_ignore:
                self.require_ignored_ledger_paths(repo_root, [path])
            existed = path.exists()
            encoded = json.dumps(event, sort_keys=True).encode('utf-8')
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                midpoint = max(1, len(encoded) // 2)
                _write_all(descriptor, encoded[:midpoint])
                ledger_io_checkpoint('event_payload_half_written')
                _write_all(descriptor, encoded[midpoint:])
                ledger_io_checkpoint('event_payload_written')
                _write_all(descriptor, b'\n')
                ledger_io_checkpoint('event_record_written')
                _LEDGER_FSYNC(descriptor)
                if not existed:
                    ledger_fsync_directory_path(path.parent)
                ledger_io_checkpoint('event_fsynced')
            finally:
                os.close(descriptor)
            state = reduce_ledger_state(load_ledger_events(repo_root, manifest_path))
            _write_ledger_checkpoint_unlocked(repo_root, state, manifest_path)
            return event


    def save_personal_tracking(self, repo_root, manifest_path, manifest, context):
        """Save a private tracking declaration and its ledger event as one update."""
        transaction = active_manifest_write_transaction(manifest_path)
        if transaction is None or not transaction.get('preserveGitignore'):
            raise SyncwheelError('personal tracking requires a private manifest transaction')
        applied = False
        try:
            with ledger_write_lock(repo_root, manifest_path), private_tracking_temporaries() as temporary_paths:
                require_manifest_transaction_current(manifest_path)
                events = load_ledger_events(repo_root, manifest_path)
                current = reduce_ledger_state(events)
                event = {
                    'schema_version': LEDGER_SCHEMA_VERSION,
                    'seq': current['last_seq'] + 1,
                    'ts': iso_utc_now(),
                    'type': 'manifest_saved',
                    'payload': manifest_event_payload(manifest_path, manifest, 'repo_tracking_set', context),
                }
                state = reduce_ledger_state([*events, event])
                segment = next_ledger_segment_path(repo_root, manifest_path, create_directory=False)
                checkpoint = ledger_checkpoint_path(repo_root, manifest_path)
                self.require_ignored_ledger_paths(repo_root, [manifest_path, segment, checkpoint])
                before_manifest = private_tracking_file_image(manifest_path)
                before_segment = private_tracking_file_image(segment)
                before_checkpoint = private_tracking_file_image(checkpoint)
                if before_manifest is None:
                    raise SyncwheelError('personal manifest disappeared before tracking update')
                existing_segment = before_segment[3] if before_segment else b''
                record = (json.dumps(event, sort_keys=True) + '\n').encode('utf-8')
                plan = (
                    (manifest_path, before_manifest, (json.dumps(manifest, indent=2) + '\n').encode('utf-8'),
                     None, 'personal_tracking_manifest_replaced'),
                    (segment, before_segment, existing_segment + record,
                     len(existing_segment) + max(1, len(record) // 2), 'event_fsynced'),
                    (checkpoint, before_checkpoint, (json.dumps(state, indent=2, sort_keys=True) + '\n').encode('utf-8'),
                     None, 'checkpoint_replaced'),
                )
                after_digest = manifest_digest(manifest)
                writes = []
                for path, before, payload, append_at, phase in plan:
                    mode = before[2] if before else 0o600
                    staged, after = stage_private_tracking_file(temporary_paths, path, mode, payload, append_at=append_at)
                    rollback = stage_private_tracking_file(temporary_paths, path, mode, before[3])[0] if before else None
                    writes.append((path, before, staged, after, rollback, phase))
                require_manifest_transaction_current(manifest_path)
                publish_private_tracking_files(writes, temporary_paths)
                applied = True
                transaction['expectedDigest'] = after_digest
                transaction['manifestWritten'] = True
        except OSError as error:
            if not applied:
                raise SyncwheelError(f'personal tracking write failed: {error}') from error
            print(f'warning: personal tracking applied; finalization failed: {error}', file=sys.stderr)

    def save_manifest_with_ledger(self, repo_root, manifest_path, manifest, reason, context=None, event_type='manifest_saved'):
        if reason == 'stack_create' and context and context.get('operation_token'):
            stack = stack_map(manifest).get(context.get('stack'))
            if stack:
                self.lifecycle.require_current_stack_create_operation(
                    repo_root,
                    manifest_path,
                    stack['id'],
                    stack['branch'],
                    self.repository.ref_tip(repo_root, stack['branch']),
                    context['operation_token'],
                )
        save_manifest(manifest_path, manifest)
        self.append_ledger_event(repo_root, event_type, manifest_event_payload(manifest_path, manifest, reason, context), manifest_path)


    def acknowledge_in_place_manifest_replay(self, repo_root, manifest_path, replay_tip):
        """Accept only the manifest state written by a completed in-place replay."""
        transaction = active_manifest_write_transaction(manifest_path)
        if transaction is None or is_external_manifest_path(repo_root, manifest_path):
            return
        try:
            relative_path = Path(manifest_path).resolve(strict=False).relative_to(
                Path(repo_root).resolve()
            ).as_posix()
        except ValueError as exc:
            raise SyncwheelError(
                f'in-place replay cannot verify manifest outside repository: {manifest_path}'
            ) from exc
        expected_result = self.repository.git(repo_root, 'show', f'{replay_tip}:{relative_path}', check=False)
        if expected_result.returncode == 0:
            try:
                expected_manifest = json.loads(expected_result.stdout)
            except json.JSONDecodeError as exc:
                raise SyncwheelError(
                    f'in-place replay produced an invalid manifest at {replay_tip}:{relative_path}'
                ) from exc
            expected_digest = manifest_digest(expected_manifest)
        else:
            expected_digest = None
        observed_manifest, _ = load_manifest(repo_root, manifest_path)
        observed_normalized_digest = (
            manifest_digest(observed_manifest) if observed_manifest is not None else None
        )
        if observed_manifest is None:
            observed_digest = None
        else:
            try:
                observed_digest = manifest_digest(json.loads(Path(manifest_path).read_text()))
            except (OSError, json.JSONDecodeError) as exc:
                raise SyncwheelError(
                    f'in-place replay left an unreadable manifest: {manifest_path}'
                ) from exc
        preserved_untracked_digest = None
        if expected_digest is None and observed_normalized_digest == transaction['expectedDigest']:
            preserved_untracked_digest = observed_digest
        if observed_digest not in {expected_digest, preserved_untracked_digest}:
            raise SyncwheelError(
                'manifest changed unexpectedly during in-place replay; refusing stale write or side effect'
            )
        transaction['expectedDigest'] = observed_normalized_digest


    def manifest_from_tree(self, repo_root, commit, path):
        relative = Path(path).resolve(strict=False).relative_to(Path(repo_root).resolve()).as_posix()
        result = self.repository.git(repo_root, 'show', f'{commit}:{relative}', check=False)
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SyncwheelError(f'integration control manifest is invalid at {commit}:{relative}') from exc


    def materialize_control_manifest_commit(
        self, repo_root, manifest, parent, identity_version=CONTROL_MANIFEST_IDENTITY_VERSION,
    ):
        """Build the manifest-only control commit without touching the real index.

        Its identity is fixed and its timestamps inherit the deterministic replay
        parent.  Therefore parent + manifest bytes always yields the same object.
        """
        target = integration_manifest_path(repo_root)
        relative = target.relative_to(Path(repo_root)).as_posix()
        payload = canonical_manifest_file_text(manifest)
        with tempfile.NamedTemporaryFile(prefix='syncwheel-control-index-', delete=False) as handle:
            index_path = Path(handle.name)
        index_path.unlink(missing_ok=True)
        environment = {'GIT_INDEX_FILE': str(index_path)}
        try:
            self.repository.git(repo_root, 'read-tree', parent, env=environment)
            blob = self.repository.git(repo_root, 'hash-object', '-w', '--stdin', input_text=payload).stdout.strip()
            self.repository.git(
                repo_root, 'update-index', '--add', '--cacheinfo',
                f'100644,{blob},{relative}', env=environment,
            )
            tree = self.repository.git(repo_root, 'write-tree', env=environment).stdout.strip()
            if tree == self.repository.ref_tree(repo_root, parent):
                return parent
            commit = self.repository.git(
                repo_root, 'commit-tree', tree, '-p', parent,
                # A different object makes older consumers fail their deterministic
                # object check instead of resuming this explicit-plan-only operation.
                '-m', ('chore: restore guarded Syncwheel control manifest'
                       if identity_version == CONTROL_MANIFEST_EXPLICIT_RECOVERY_IDENTITY_VERSION
                       else 'chore: restore Syncwheel control manifest'),
                env=self.repository.control_manifest_commit_env(repo_root, parent, identity_version),
            ).stdout.strip()
        finally:
            index_path.unlink(missing_ok=True)
        committed = self.manifest_from_tree(repo_root, commit, target)
        if manifest_digest(committed) != manifest_digest(manifest):
            raise SyncwheelError(
                'control manifest digest differs in the object prepared for integration publication'
            )
        return commit


    def control_manifest_commit_identity_version(self, repo_root, manifest, parent, commit):
        """Return the identity formula that produced this control object, or None."""
        for version in CONTROL_MANIFEST_IDENTITY_VERSIONS:
            if self.materialize_control_manifest_commit(
                repo_root, manifest, parent, version,
            ) == commit:
                return version
        return None


    def control_manifest_actor(self, repo_root):
        name, email = self.repository.git_identity(repo_root)
        if not name or not email:
            name, email = 'Syncwheel', 'syncwheel@example.com'
        return f'{name} <{email}>'


    def control_manifest_event_payload(
        self, repo_root, manifest_path, manifest, control_commit, replay_mode, reason, command, context,
        operation_id=None,
    ):
        payload = manifest_event_payload(
            manifest_path, manifest, reason,
            {
                **(context or {}),
                'replay_mode': replay_mode,
                'control_commit': control_commit,
                'control_manifest_digest': manifest_digest(manifest),
            },
        )
        payload.update({
            'actor': self.control_manifest_actor(repo_root),
            'reason': reason,
            'command': command,
            'control_commit': control_commit,
        })
        if operation_id is not None:
            payload['operation_id'] = operation_id
        return payload


    def control_manifest_intent_payload(
        self, repo_root, manifest, source_digest, replay_tip, control_commit,
        replay_mode, reason, command, operation_id,
        identity_version=CONTROL_MANIFEST_IDENTITY_VERSION,
    ):
        return {
            'operation_id': operation_id,
            'integration_branch': manifest['integration']['branch'],
            'source_manifest_digest': source_digest,
            'expected_manifest_digest': manifest_digest(manifest),
            'replay_tip': replay_tip,
            'expected_control_commit': control_commit,
            'control_identity': identity_version,
            **({'recovery_policy': 'explicit-plan-only'}
               if identity_version == CONTROL_MANIFEST_EXPLICIT_RECOVERY_IDENTITY_VERSION else {}),
            'replay_mode': replay_mode,
            'actor': self.control_manifest_actor(repo_root),
            'reason': reason,
            'command': command,
        }


    def abandon_control_manifest_intent(
        self, repo_root, manifest_path, intent, observed_tip, outcome,
    ):
        payload = {
            'operation_id': intent['operation_id'],
            'integration_branch': intent['integration_branch'],
            'replay_tip': intent['replay_tip'],
            'expected_control_commit': intent['expected_control_commit'],
            'observed_tip': observed_tip,
            'outcome': outcome,
            'actor': self.control_manifest_actor(repo_root),
            'reason': intent['reason'],
            'command': intent['command'],
        }
        self.append_ledger_event(
            repo_root,
            'control_manifest_persistence_abandoned',
            payload,
            manifest_path,
            idempotency_key=f"control-manifest-abandoned:{intent['operation_id']}",
        )
        return payload


    def staged_control_manifest_migration(self, repo_root):
        """Whether an untracking migration of the control manifest is still staged."""
        relative = control_manifest_relative_path(repo_root)
        staged = self.repository.git(
            repo_root, 'diff', '--cached', '--name-only', '--diff-filter=D', '--', relative,
            check=False,
        )
        return staged.returncode == 0 and bool(staged.stdout.strip())


    def control_manifest_owns_the_blocking_dirt(self, repo_root):
        """Whether the blocking dirt is Syncwheel's own metadata, not product work."""
        result = self.repository.run(['git', '-C', str(repo_root), 'status', '--porcelain'], check=False)
        if result.returncode != 0:
            return False
        relative = control_manifest_relative_path(repo_root)
        paths = set()
        for line in result.stdout.splitlines():
            paths |= status_line_paths(line)
        return relative in paths and all(
            path == '.gitignore' or path.startswith('.syncwheel/') for path in paths
        )


    def local_only_control_manifest_untrack_step(self, repo_root, manifest):
        """The command that keeps a local-only manifest out of a rebuild's reset."""
        if not control_manifest_stays_untracked(manifest):
            return None
        checkout = self.repository.find_worktree_for_branch(repo_root, manifest['integration']['branch'])
        if checkout is None:
            return None
        relative = control_manifest_relative_path(repo_root)
        listed = self.repository.git(checkout, 'ls-files', '-z', '--', relative, check=False)
        if listed.returncode != 0 or not listed.stdout.strip('\0'):
            return None
        location = [] if Path(checkout) == Path(repo_root) else ['-C', str(checkout)]
        # --force: the entry may differ from both HEAD and the worktree, which
        # git rm refuses to discard without it.
        return ['git', *location, 'rm', '--cached', '--force', '-q', '--', relative]


    def untrack_local_only_control_manifest(self, repo_root, manifest):
        """Drop the control manifest from the integration index, keeping the file."""
        step = self.local_only_control_manifest_untrack_step(repo_root, manifest)
        if step is None:
            return False
        removed = self.repository.run(step, cwd=repo_root, check=False)
        if removed.returncode != 0:
            raise SyncwheelError(
                'cannot untrack the local-only control manifest: '
                + (removed.stderr.strip() or removed.stdout.strip() or 'git rm failed')
            )
        return True


    def local_only_control_manifest_guard(self, repo_root, manifest, manifest_path):
        """Keep a local-only manifest file across the reset a rebuild performs.

        The file is tracked only because an earlier release committed it, so the
        reset would delete it. Untrack it where the integration branch is checked
        out, and remember the bytes for every source path the reset can reach.
        """
        if not control_manifest_stays_untracked(manifest):
            return []
        checkout = self.repository.find_worktree_for_branch(repo_root, manifest['integration']['branch'])
        candidates = [integration_manifest_path(repo_root)]
        if checkout is not None:
            candidates.append(integration_manifest_path(checkout))
        if manifest_path is not None:
            candidates.append(Path(manifest_path))
        guarded = []
        seen = set()
        for path in candidates:
            # Guard the path the command names, never a symlink's target.
            key = str(path)
            if key in seen or path.is_symlink():
                continue
            seen.add(key)
            try:
                guarded.append((path, path.read_bytes()))
            except OSError:
                continue
        self.untrack_local_only_control_manifest(repo_root, manifest)
        return guarded


    def control_manifest_checkout_obstruction(
        self, repo_root, integration_worktree, replay_tip, control_commit, expected_digest,
    ):
        """Name the checkout content a control-manifest alignment must not overwrite."""
        integration_worktree = Path(integration_worktree).resolve()
        status = self.repository.git(
            repo_root, '-C', str(integration_worktree), 'status', '--porcelain',
            '--untracked-files=all',
        ).stdout.splitlines()
        if not status:
            return None
        expected_path = control_manifest_relative_path(repo_root)
        material_status = [
            line for line in status
            if not line.startswith('?? ') or line[3:] == expected_path
        ]
        changed_paths = {line[3:] for line in material_status if len(line) >= 4}
        index_tree = self.repository.git(
            repo_root, '-C', str(integration_worktree), 'write-tree'
        ).stdout.strip()
        worktree_manifest_path = integration_worktree / expected_path
        try:
            worktree_raw_manifest = json.loads(worktree_manifest_path.read_text())
            worktree_manifest, _ = load_manifest(
                integration_worktree, worktree_manifest_path
            )
        except FileNotFoundError:
            worktree_raw_manifest = None
            worktree_manifest = None
        except (OSError, json.JSONDecodeError, SyncwheelError) as exc:
            raise SyncwheelError(
                'interrupted control-manifest checkout contains an unreadable manifest'
            ) from exc
        worktree_digests = {
            manifest_digest(candidate)
            for candidate in (worktree_raw_manifest, worktree_manifest)
            if candidate is not None
        }
        replayed_manifest = self.manifest_from_tree(
            repo_root, replay_tip, integration_manifest_path(repo_root)
        )
        replayed_digest = (
            manifest_digest(replayed_manifest) if replayed_manifest is not None else None
        )
        allowed_index_trees = {
            self.repository.ref_tree(repo_root, replay_tip),
            self.repository.ref_tree(repo_root, control_commit),
        }
        allowed_worktree_digests = {expected_digest}
        if replayed_digest is not None:
            allowed_worktree_digests.add(replayed_digest)
        worktree_manifest_is_allowed = (
            bool(worktree_digests & allowed_worktree_digests)
            or (not worktree_digests and replayed_digest is None)
        )
        blocking = sorted(changed_paths - {expected_path})
        index_diverged = index_tree not in allowed_index_trees
        manifest_diverged = not worktree_manifest_is_allowed
        if not blocking and not index_diverged and not manifest_diverged:
            return None
        return {
            'worktree': str(integration_worktree),
            'paths': blocking,
            'display': blocking or [expected_path],
            'index_diverged': index_diverged,
            'manifest_diverged': manifest_diverged,
        }


    def align_control_manifest_worktree(
        self, repo_root, manifest, replay_tip, control_commit, expected_digest,
        obstruction_state=None, preserve_ignore=False,
    ):
        """Align index/worktree to the control tree without ever moving its branch ref."""
        branch = manifest['integration']['branch']
        integration_ref = f'refs/heads/{branch}'
        integration_worktree = self.repository.find_worktree_for_branch(repo_root, branch)
        if not integration_worktree:
            return None
        integration_worktree = Path(integration_worktree).resolve()
        control_manifest_io_checkpoint('before_checkout_alignment')
        observed_tip = self.repository.ref_tip(repo_root, integration_ref)
        if observed_tip != control_commit:
            advanced = bool(
                observed_tip
                and self.repository.git(
                    repo_root, 'merge-base', '--is-ancestor', control_commit, observed_tip,
                    check=False,
                ).returncode == 0
            )
            relation = 'advanced beyond' if advanced else 'changed away from'
            raise ControlManifestAlignmentDrift(
                f'integration ref {relation} interrupted control commit '
                f'{control_commit}; checkout alignment stopped without moving the ref '
                f'(observed {observed_tip or "absent"})',
                observed_tip,
            )
        obstruction = self.control_manifest_checkout_obstruction(
            repo_root, integration_worktree, replay_tip, control_commit, expected_digest,
        )
        manifest_file_aligned = False
        if obstruction is None and not preserve_ignore:
            # read-tree updates only this worktree's index and files. Unlike reset it
            # cannot move the checked-out branch ref and therefore cannot undo a CAS.
            self.repository.git(
                repo_root, '-C', str(integration_worktree),
                'read-tree', '--reset', '-u', control_commit,
            )
            manifest_file_aligned = True
        else:
            # The ref already carries the control commit, so the operation must end
            # rather than stall: align the control manifest alone and leave every
            # other path in the checkout to its owner. A checkout carrying a
            # different manifest proposal keeps that file too.
            if obstruction is None or not obstruction['manifest_diverged']:
                self.repository.git(
                    repo_root, '-C', str(integration_worktree),
                    'checkout', control_commit, '--',
                    control_manifest_relative_path(repo_root),
                )
                manifest_file_aligned = True
            if obstruction_state is not None and obstruction is not None:
                obstruction_state.update(obstruction)
        observed_after = self.repository.ref_tip(repo_root, integration_ref)
        if observed_after != control_commit:
            raise ControlManifestAlignmentDrift(
                'integration ref changed during interrupted control-manifest checkout '
                f'alignment; the ref was not moved (observed {observed_after or "absent"})',
                observed_after,
            )
        aligned = self.manifest_from_tree(
            repo_root, control_commit, integration_manifest_path(repo_root)
        )
        if manifest_digest(aligned) != expected_digest:
            raise SyncwheelError('integration checkout did not align to the control manifest')
        source_path = Path(integration_worktree) / integration_manifest_path(
            repo_root
        ).relative_to(Path(repo_root).resolve())
        transaction = active_manifest_write_transaction(source_path)
        if transaction is not None and manifest_file_aligned:
            # A kept foreign manifest never gets rewritten here; only claim the
            # baseline when the worktree file was actually made to match it.
            transaction['expectedDigest'] = expected_digest
        return integration_worktree


    def capture_checkout_source_lease(self, repo_root, manifest_path, target):
        """Expose one exact post-restore source lease without changing bool results."""
        if target is None:
            return
        observed = self.projection.checkout_source_lease(repo_root, manifest_path)
        if observed is None:
            raise SyncwheelError(
                'cannot capture the source checkout lease after control restoration'
            )
        target.clear()
        target.update(observed)


    def persist_local_only_control_manifest(
        self, repo_root, manifest_path, manifest, replay_tip, replay_mode, reason, command,
        operation_id, persist_source, source_lease_out,
    ):
        """Record the rebuilt integration tip without putting the manifest in it.

        The receipt names the replay tip as the control commit, so every consumer
        of the ledger sees that this rebuild has no separate control object.
        """
        expected_digest = manifest_digest(manifest)
        try:
            source_file_manifest = json.loads(Path(manifest_path).read_text())
        except FileNotFoundError:
            source_file_manifest = None
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncwheelError(f'control manifest source is unreadable: {manifest_path}') from exc
        source_file_digest = (
            manifest_digest(source_file_manifest) if source_file_manifest is not None else None
        )
        saved = persist_source and source_file_digest != expected_digest
        if saved:
            require_manifest_transaction_current(manifest_path)
            save_manifest(manifest_path, manifest)
        control_manifest_io_checkpoint('manifest_saved')
        if not saved and any(
            event.get('type') == 'manifest_saved'
            and (event.get('payload') or {}).get('control_commit') == replay_tip
            and ((event.get('payload') or {}).get('context') or {}).get(
                'control_manifest_digest'
            ) == expected_digest
            for event in load_control_manifest_events(repo_root, manifest_path)
        ):
            self.capture_checkout_source_lease(repo_root, manifest_path, source_lease_out)
            return False
        operation_id = operation_id or str(uuid.uuid4())
        payload = self.control_manifest_event_payload(
            repo_root, manifest_path, manifest, replay_tip, replay_mode, reason, command,
            {'replay_tip': replay_tip, 'syncwheel_tracking': SYNCWHEEL_TRACKING_LOCAL_ONLY},
            operation_id=operation_id,
        )
        self.append_ledger_event(
            repo_root,
            'manifest_saved',
            payload,
            manifest_path,
            idempotency_key=control_manifest_event_idempotency_key(
                manifest, replay_tip, replay_tip, command, operation_id
            ),
        )
        control_manifest_io_checkpoint('ledger_saved')
        self.capture_checkout_source_lease(repo_root, manifest_path, source_lease_out)
        return saved


    def restore_control_manifest_after_integration_rebuild(
        self, repo_root, manifest_path, manifest, replay_tip, replay_mode,
        reason='restore_control_manifest_after_integration_rebuild', command='reconcile',
        operation_id=None, persist_source=True, source_lease_out=None,
        identity_version=CONTROL_MANIFEST_IDENTITY_VERSION,
        preserved_ignore=None, pre_cas_check=None,
    ):
        branch = manifest['integration']['branch']
        with self.control_manifest_branch_lock(repo_root, branch):
            # Check every explicit pending operation BEFORE deriving an object from
            # caller defaults (or taking the local-only/no-op paths). A different
            # identity/parent must not make the existing intent invisible.
            explicit = pending_explicit_control_recoveries(repo_root, manifest_path)
            if explicit:
                transaction = active_manifest_write_transaction(manifest_path)
                intent = explicit[0]
                if (len(explicit) != 1 or transaction is None
                        or transaction.get('explicitControlRecoveryOperation') != intent['operation_id']
                        or transaction.get('preserveGitignore') is not True
                        or command != 'syncwheel manifest recover-control'
                        or operation_id != intent['operation_id']
                        or branch != intent['integration_branch']
                        or replay_tip != intent['replay_tip']
                        or identity_version != intent.get('control_identity', CONTROL_MANIFEST_LEGACY_IDENTITY_VERSION)
                        or manifest_digest(manifest) != intent['expected_manifest_digest']
                        or preserved_ignore is None or pre_cas_check is None
                        or not persist_source):
                    raise SyncwheelError(explicit_control_recovery_refusal())
            if control_manifest_stays_untracked(manifest):
                return self.persist_local_only_control_manifest(
                    repo_root,
                    manifest_path,
                    manifest,
                    replay_tip,
                    replay_mode,
                    reason,
                    command,
                    operation_id,
                    persist_source,
                    source_lease_out,
                )
            return self._restore_control_manifest_after_integration_rebuild_locked(
                repo_root,
                manifest_path,
                manifest,
                replay_tip,
                replay_mode,
                reason=reason,
                command=command,
                operation_id=operation_id,
                persist_source=persist_source,
                source_lease_out=source_lease_out,
                identity_version=identity_version,
                preserved_ignore=preserved_ignore,
                pre_cas_check=pre_cas_check,
            )


    def _restore_control_manifest_after_integration_rebuild_locked(
        self, repo_root, manifest_path, manifest, replay_tip, replay_mode,
        reason='restore_control_manifest_after_integration_rebuild', command='reconcile',
        operation_id=None, persist_source=True, source_lease_out=None,
        identity_version=CONTROL_MANIFEST_IDENTITY_VERSION,
        preserved_ignore=None, pre_cas_check=None,
    ):
        """Publish an isolated manifest-only commit above a rebuilt integration tip.

        The object is built and verified before the integration ref is compared and
        moved. Only after the CAS and checkout alignment succeed do we save the
        source (including an external manifest) and append its receipt.
        """
        target = integration_manifest_path(repo_root)
        observed = self.manifest_from_tree(repo_root, replay_tip, target)
        expected_digest = manifest_digest(manifest)
        observed_digest = manifest_digest(observed) if observed is not None else None
        integration_ref = f"refs/heads/{manifest['integration']['branch']}"
        current_tip = self.repository.ref_tip(repo_root, integration_ref)
        control_commit = self.materialize_control_manifest_commit(
            repo_root, manifest, replay_tip, identity_version,
        )
        # Semantic equality does not make differently formatted Git blobs equal.
        # Canonicalize through the same durable transaction before relying on exact
        # tree equivalence after another publisher wins a coordination race.
        if observed_digest == expected_digest and current_tip == replay_tip and control_commit == replay_tip:
            self.capture_checkout_source_lease(repo_root, manifest_path, source_lease_out)
            return False
        transaction = active_manifest_write_transaction(manifest_path)
        if transaction is not None:
            replayed_source, _ = load_manifest(repo_root, manifest_path)
            replayed_source_digest = (
                manifest_digest(replayed_source) if replayed_source is not None else None
            )
            # An ephemeral or desk replay may reset the checkout that carries the
            # integration branch. It is the one known in-command transition that
            # may replace the source before the control CAS. Record that observed
            # replay digest in the intent; every other change still fails closed.
            if replayed_source_digest != transaction['expectedDigest']:
                # Tree JSON may omit defaults that load_manifest normalizes. Compare
                # the actual replay bytes through the same verified checkpoint used
                # for in-place replay, rather than comparing raw and normalized data.
                self.acknowledge_in_place_manifest_replay(repo_root, manifest_path, replay_tip)
        events = load_control_manifest_events(repo_root, manifest_path)
        pending_intent, matching_intents, receipts = control_manifest_operation_intent(
            events,
            control_commit,
            replay_tip,
            expected_digest,
            manifest['integration']['branch'],
            operation_id=operation_id,
        )
        if pending_intent is not None and explicit_control_recovery_intent(pending_intent['payload']):
            transaction = active_manifest_write_transaction(manifest_path)
            if (transaction is None or transaction.get('explicitControlRecoveryOperation')
                    != pending_intent['payload']['operation_id']):
                raise SyncwheelError(explicit_control_recovery_refusal())
        if current_tip == control_commit and pending_intent is None:
            # A completed local operation is an idempotent retry. A deterministic
            # control commit with no local intent belongs to some other operation
            # and must never be claimed as this clone's persistence receipt.
            self.capture_checkout_source_lease(repo_root, manifest_path, source_lease_out)
            return False
        if current_tip != control_commit:
            # FC3: probe the checkout the alignment will touch before the intent and
            # the CAS, so unrelated work there can never leave an advanced ref behind.
            integration_worktree = self.repository.find_worktree_for_branch(
                repo_root, manifest['integration']['branch']
            )
            obstruction = (
                self.control_manifest_checkout_obstruction(
                    repo_root, integration_worktree, replay_tip, control_commit,
                    expected_digest,
                )
                if integration_worktree else None
            )
            ignore_is_preserved = bool(
                obstruction is not None
                and obstruction['paths'] == ['.gitignore']
                and not obstruction['index_diverged']
                and not obstruction['manifest_diverged']
                and preserved_ignore is not None
                and self.projection.checkout_path_fingerprint(integration_worktree, '.gitignore') == preserved_ignore
            )
            if obstruction is not None and obstruction['paths'] and not ignore_is_preserved:
                raise SyncwheelError(control_manifest_obstruction_refusal(
                    obstruction,
                    pending_intent['payload']['command'] if pending_intent else command,
                ))
        if pending_intent is not None:
            intent = pending_intent['payload']
            operation_id = intent['operation_id']
            replay_mode = intent['replay_mode']
            reason = intent['reason']
            command = intent['command']
            source_manifest, _ = load_manifest(repo_root, manifest_path)
            source_digest = (
                manifest_digest(source_manifest) if source_manifest is not None else None
            )
            try:
                source_file_manifest = json.loads(Path(manifest_path).read_text())
            except FileNotFoundError:
                source_file_manifest = None
            except (OSError, json.JSONDecodeError) as exc:
                raise SyncwheelError(
                    f'control manifest source is unreadable: {manifest_path}'
                ) from exc
            source_file_digest = (
                manifest_digest(source_file_manifest)
                if source_file_manifest is not None else None
            )
            if {source_digest, source_file_digest}.isdisjoint({
                intent.get('source_manifest_digest'),
                intent.get('expected_manifest_digest'),
            }):
                raise SyncwheelError(
                    'control manifest changed after the interrupted persistence intent; '
                    'refusing to replace the newer local proposal. Review the proposal, then run: '
                    + control_manifest_divergence_remedy(repo_root, manifest_path)
                )
        else:
            if current_tip != replay_tip:
                raise SyncwheelError(
                    'integration ref changed before control manifest publication; '
                    'rerun the rebuild from a fresh plan'
                )
            operation_id = str(uuid.uuid4())
            transaction = active_manifest_write_transaction(manifest_path)
            source_digest = (
                transaction.get('expectedDigest') if transaction is not None else None
            )
            intent = self.control_manifest_intent_payload(
                repo_root,
                manifest,
                source_digest,
                replay_tip,
                control_commit,
                replay_mode,
                reason,
                command,
                operation_id,
                identity_version=identity_version,
            )
            self.append_ledger_event(
                repo_root,
                'control_manifest_persistence_intent',
                intent,
                manifest_path,
                idempotency_key=control_manifest_intent_idempotency_key(operation_id),
            )
            control_manifest_io_checkpoint('intent_saved')
        # The object has already been checked above.  This one-shot CAS protects a
        # concurrent integration writer without ever involving hooks or the index.
        if pre_cas_check is not None:
            try:
                pre_cas_check()
            except SyncwheelError:
                self.abandon_control_manifest_intent(
                    repo_root, manifest_path, intent, self.repository.ref_tip(repo_root, integration_ref),
                    'recovery_source_changed_before_cas',
                )
                raise
        require_manifest_transaction_current(manifest_path)
        if current_tip == replay_tip:
            moved = self.repository.git(
                repo_root, 'update-ref', integration_ref, control_commit, replay_tip,
                check=False,
            )
            if moved.returncode != 0:
                current_tip = self.repository.ref_tip(repo_root, integration_ref)
                if current_tip != control_commit:
                    self.abandon_control_manifest_intent(
                        repo_root,
                        manifest_path,
                        intent,
                        current_tip,
                        'ref_cas_failed',
                    )
                    raise SyncwheelError(
                        'control manifest persistence intent was abandoned because the '
                        'integration ref changed before its CAS; rerun the rebuild from a fresh plan'
                    )
        elif current_tip != control_commit:
            self.abandon_control_manifest_intent(
                repo_root,
                manifest_path,
                intent,
                current_tip,
                'ref_changed_before_alignment',
            )
            raise SyncwheelError(
                'control manifest persistence intent was abandoned because the integration '
                'ref changed before checkout alignment; the ref was not moved. '
                'Rerun the rebuild from a fresh plan'
            )
        control_manifest_io_checkpoint('ref_updated')
        alignment_obstruction = {}
        try:
            integration_worktree = self.align_control_manifest_worktree(
                repo_root, manifest, replay_tip, control_commit, expected_digest,
                obstruction_state=alignment_obstruction,
                preserve_ignore=preserved_ignore is not None,
            )
        except ControlManifestAlignmentDrift as exc:
            self.abandon_control_manifest_intent(
                repo_root,
                manifest_path,
                intent,
                exc.observed_tip,
                'checkout_alignment_ref_drift',
            )
            raise
        control_manifest_io_checkpoint('checkout_aligned')
        try:
            source_file_manifest = json.loads(Path(manifest_path).read_text())
        except FileNotFoundError:
            source_file_manifest = None
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncwheelError(f'control manifest source is unreadable: {manifest_path}') from exc
        source_file_digest = (
            manifest_digest(source_file_manifest) if source_file_manifest is not None else None
        )
        if persist_source and source_file_digest != expected_digest:
            save_manifest(manifest_path, manifest)
        control_manifest_io_checkpoint('manifest_saved')
        payload = self.control_manifest_event_payload(
            repo_root, manifest_path, manifest, control_commit, replay_mode, reason, command,
            {
                'replay_tip': replay_tip,
                'replayed_manifest_digest': observed_digest,
                'difference': control_manifest_difference(manifest, observed),
            },
            operation_id=operation_id,
        )
        self.append_ledger_event(
            repo_root,
            'manifest_saved',
            payload,
            manifest_path,
            idempotency_key=control_manifest_event_idempotency_key(
                manifest, replay_tip, control_commit, command, operation_id
            ),
        )
        control_manifest_io_checkpoint('ledger_saved')
        if alignment_obstruction:
            print(
                'warning: '
                + control_manifest_obstruction_warning(alignment_obstruction, command),
                file=sys.stderr,
            )
        self.capture_checkout_source_lease(repo_root, manifest_path, source_lease_out)
        return True


    def resolve_superseded_control_manifest_intent(
        self, repo_root, manifest_path, manifest, intent, branch, allow_new_operation,
    ):
        """Close an interrupted intent whose source already carries a newer proposal."""
        source_manifest, _ = load_manifest(repo_root, manifest_path)
        try:
            source_file_manifest = json.loads(Path(manifest_path).read_text())
        except FileNotFoundError:
            source_file_manifest = None
        except (OSError, json.JSONDecodeError) as exc:
            raise SyncwheelError(
                f'control manifest source is unreadable: {manifest_path}'
            ) from exc
        if source_manifest is None and source_file_manifest is None:
            return None
        source_digest = (
            manifest_digest(source_manifest) if source_manifest is not None else None
        )
        source_file_digest = (
            manifest_digest(source_file_manifest)
            if source_file_manifest is not None else None
        )
        if not {source_digest, source_file_digest}.isdisjoint({
            intent.get('source_manifest_digest'),
            intent.get('expected_manifest_digest'),
        }):
            return None
        control_commit = intent['expected_control_commit']
        replay_tip = intent.get('replay_tip')
        current_tip = self.repository.ref_tip(repo_root, f'refs/heads/{branch}')
        proposal = source_manifest or manifest
        if current_tip == control_commit:
            # The CAS already landed, so the operation owes only its terminal
            # record. Write it against the proposal the source now holds, so the
            # newer declarations are never replaced by the interrupted one.
            payload = self.control_manifest_event_payload(
                repo_root, manifest_path, proposal, control_commit,
                intent.get('replay_mode'), intent['reason'], intent['command'],
                {
                    'replay_tip': replay_tip,
                    'persisted_manifest_digest': intent.get('expected_manifest_digest'),
                    'source_superseded': True,
                },
                operation_id=intent['operation_id'],
            )
            self.append_ledger_event(
                repo_root,
                'manifest_saved',
                payload,
                manifest_path,
                idempotency_key=control_manifest_event_idempotency_key(
                    proposal, replay_tip, control_commit, intent['command'],
                    intent['operation_id'],
                ),
            )
            return proposal
        if allow_new_operation:
            self.abandon_control_manifest_intent(
                repo_root, manifest_path, intent, current_tip, 'superseded_local_proposal',
            )
            return proposal
        raise SyncwheelError(
            'control manifest changed after the interrupted persistence intent; '
            'refusing to replace the newer local proposal. Review the proposal, then run: '
            + control_manifest_divergence_remedy(repo_root, manifest_path)
        )


    def recover_incomplete_control_manifest_persistence(
        self, repo_root, manifest_path, manifest, allow_new_operation=False,
        recovery_state=None, intents_only=False,
    ):
        """Recover ledger intent before relying on the ref subject or source file."""
        if recovery_state is not None:
            recovery_state.update({'recovered': False, 'control_commit': None})

        # FC4: the durable intent is authoritative even while the branch still
        # points at the ordinary replay tip and even when replay removed the source.
        events = load_control_manifest_events(repo_root, manifest_path)
        branch = ((manifest or {}).get('integration') or {}).get('branch')
        if pending_explicit_control_recoveries(repo_root, manifest_path):
            raise SyncwheelError(explicit_control_recovery_refusal())
        all_pending = pending_control_manifest_intents(events)
        pending = (
            all_pending
            if len(all_pending) <= 1 or not branch
            else [
                event for event in all_pending
                if (event.get('payload') or {}).get('integration_branch') == branch
            ]
        )
        if len(pending) > 1:
            raise SyncwheelError(
                'control manifest ledger contains ambiguous incomplete intents for '
                + (branch or str(manifest_path))
            )
        pending_intent = pending[0] if pending else None
        if pending_intent is None and intents_only:
            return manifest
        if pending_intent is not None:
            branch = pending_intent['payload'].get('integration_branch')
        if not branch:
            symbolic = self.repository.git(
                repo_root, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False
            )
            branch = symbolic.stdout.strip() or None
        if not branch:
            return manifest
        if pending_intent is None and not self.repository.ref_tip(repo_root, branch):
            return manifest

        with self.control_manifest_branch_lock(repo_root, branch):
            events = load_control_manifest_events(repo_root, manifest_path)
            if pending_intent is not None:
                operation_id = pending_intent['payload']['operation_id']
                pending = [
                    event for event in pending_control_manifest_intents(events, branch=branch)
                    if event['payload']['operation_id'] == operation_id
                ]
                if not pending:
                    current, _ = load_manifest(repo_root, manifest_path)
                    return current or manifest
                intent = pending[0]['payload']
                # A scoped recovery grant never survives as permission for an
                # unrelated command. This refusal precedes alignment and ledger writes.
                if explicit_control_recovery_intent(intent):
                    raise SyncwheelError(explicit_control_recovery_refusal())
                if intent.get('version') == 2:
                    committed = self.manifest_from_tree(
                        repo_root, intent['expected_control_commit'], integration_manifest_path(repo_root)
                    )
                    if committed is None:
                        raise SyncwheelError('integration reconciliation control manifest is missing')
                    try:
                        self.projection.apply_integration_reconciliation(repo_root, manifest_path, committed, intent)
                    except IntegrationReconciliationLeaseDrift:
                        # Explicit re-planning may retire only an operation whose CAS
                        # never landed. Post-CAS drift always keeps its pending intent.
                        if not allow_new_operation or self.repository.ref_tip(repo_root, branch) != intent['replay_tip']:
                            raise
                        current, _ = load_manifest(repo_root, manifest_path)
                        if current is None:
                            raise
                        self.abandon_control_manifest_intent(
                            repo_root, manifest_path, intent, intent['replay_tip'], 'superseded_before_cas'
                        )
                        return current
                    if recovery_state is not None:
                        recovery_state.update({'recovered': True, 'control_commit': intent['expected_control_commit']})
                    return committed
                control_commit = intent.get('expected_control_commit')
                replay_tip = intent.get('replay_tip')
                identity_version = intent.get(
                    'control_identity', CONTROL_MANIFEST_LEGACY_IDENTITY_VERSION,
                )
                parents = self.repository.git(
                    repo_root, 'show', '-s', '--format=%P', control_commit, check=False
                ).stdout.split()
                committed = self.manifest_from_tree(
                    repo_root, control_commit, integration_manifest_path(repo_root)
                )
                if (
                    len(parents) != 1
                    or parents[0] != replay_tip
                    or committed is None
                    or (committed.get('integration') or {}).get('branch') != branch
                    or manifest_digest(committed) != intent.get('expected_manifest_digest')
                    or self.materialize_control_manifest_commit(
                        repo_root, committed, replay_tip, identity_version,
                    ) != control_commit
                ):
                    raise SyncwheelError(
                        f'control manifest intent {intent["operation_id"]} references an '
                        'invalid expected control object; refusing recovery'
                    )
                if control_manifest_stays_untracked(committed):
                    self.abandon_control_manifest_intent(
                        repo_root, manifest_path, intent, self.repository.ref_tip(repo_root, branch),
                        'local_only_control_manifest',
                    )
                    return committed
                superseded = self.resolve_superseded_control_manifest_intent(
                    repo_root, manifest_path, manifest, intent, branch, allow_new_operation,
                )
                if superseded is not None:
                    if recovery_state is not None:
                        recovery_state.update({
                            'recovered': True,
                            'control_commit': control_commit,
                        })
                    return superseded
                self.restore_control_manifest_after_integration_rebuild(
                    repo_root,
                    manifest_path,
                    committed,
                    replay_tip,
                    'recovery',
                    reason=intent['reason'],
                    command=intent['command'],
                    operation_id=intent['operation_id'],
                    identity_version=identity_version,
                )
                if recovery_state is not None:
                    recovery_state.update({
                        'recovered': True,
                        'control_commit': control_commit,
                    })
                return committed

            source_manifest, _ = load_manifest(repo_root, manifest_path)
            manifest = source_manifest or manifest
            control_commit = self.repository.ref_tip(repo_root, branch)
            if not control_commit:
                return manifest
            subject = self.repository.git(
                repo_root, 'show', '-s', '--format=%s', control_commit, check=False
            ).stdout.strip()
            if subject != 'chore: restore Syncwheel control manifest':
                return manifest
            parents = self.repository.git(
                repo_root, 'show', '-s', '--format=%P', control_commit, check=False
            ).stdout.split()
            if len(parents) != 1:
                return manifest
            replay_tip = parents[0]
            committed = self.manifest_from_tree(
                repo_root, control_commit, integration_manifest_path(repo_root)
            )
            if committed is None or (committed.get('integration') or {}).get('branch') != branch:
                return manifest
            if self.control_manifest_commit_identity_version(
                repo_root, committed, replay_tip, control_commit,
            ) is None:
                return manifest
            committed_digest = manifest_digest(committed)

            # A receipt for this exact control object proves that its local
            # persistence operation is already complete. A different source is
            # therefore a subsequent local proposal, not incomplete recovery.
            # Do not use any historical source digest as proof for an unreceipted
            # control object: only its current intent can authorize recovery.
            _intents, terminal = control_manifest_operation_records(
                events, control_commit
            )
            if any(
                event.get('type') == 'manifest_saved'
                for event in terminal.values()
            ):
                return manifest

            # A checked-out control tip can repair an absent in-repository source
            # without claiming a foreign operation or writing an external proposal.
            if manifest is None:
                internal = integration_manifest_path(repo_root).resolve(strict=False)
                if Path(manifest_path).resolve(strict=False) == internal:
                    if control_manifest_stays_untracked(committed):
                        # The same bytes, without letting a checkout alignment put a
                        # local-only manifest back into the index.
                        save_manifest(manifest_path, committed)
                    else:
                        self.align_control_manifest_worktree(
                            repo_root, committed, replay_tip, control_commit, committed_digest
                        )
                    return committed
                return manifest

            source_digest = manifest_digest(manifest)
            if source_digest != committed_digest:
                if allow_new_operation:
                    return manifest
                raise SyncwheelError(
                    'control manifest divergence has no local persistence intent; '
                    'refusing to replace the local proposal with a control commit '
                    'that may belong to another clone. Review the proposal, then run: '
                    + control_manifest_divergence_remedy(repo_root, manifest_path)
                )
            return manifest
