"""Governed worktree registry, recovery, capture and retirement.

The service owns registry CAS and lifecycle sequencing. Git observations, durable
ledger/storage operations and phase notifications cross explicit named ports.
"""

import contextlib
import copy
import datetime
from pathlib import Path
import hashlib
import json
import os
import shlex
import socket
import sys
import tempfile
import uuid
from typing import Protocol

from syncwheel_errors import SyncwheelError
from syncwheel_contracts import ZERO_OBJECT_ID, iso_utc_now, parse_coordination_timestamp, syncwheel_timestamp
from syncwheel_manifest import (
    effective_worktree_root, resolve_worktree_root_path, path_is_relative_to,
    require_stack, safe_ref_segment,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

YELLOW = '\033[33m'

RESET = '\033[0m'

GOVERNED_WORKTREE_REGISTRY_VERSION = 1

GOVERNED_WORKTREE_LOCK_TIMEOUT_SECONDS = 5

GOVERNED_WORKTREE_LOCK_STALE_SECONDS = 300

# Above this many lanes needing attention, warnings become one summary line.
GOVERNED_WORKTREE_WARNING_DETAIL_LIMIT = 3

GOVERNED_WORKTREE_TERMINAL_EVENT_TYPES = (
    'governed_worktree_released',
    'governed_worktree_reaped',
)

_REGISTRY_EXPECTED_DIGEST_UNSET = object()


def governed_worktree_registry_busy_error(metadata, stale):
    suffix = (
        ' (the lock metadata could not prove that its owner died; inspect it manually)'
        if stale else ''
    )
    return SyncwheelError(
        'governed worktree registry is busy; retry after the other local operation finishes'
        + suffix
    )


def governed_worktree_registry_payload(registry):
    return (json.dumps(registry, indent=2, sort_keys=True) + '\n').encode('utf-8')


def governed_worktree_registry_file_digest(path):
    path = Path(path)
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(payload).hexdigest()


def governed_worktree_owner_is_dead(owner):
    """Return true only for a dead PID that belongs to this host.

    Owners are advisory identifiers, so an unparseable or remote owner must never
    make a lane eligible for deletion.  A missing local process is enough to reap
    a missing lane because there is no worktree left to protect.
    """
    if not isinstance(owner, str):
        return False
    owner_and_host, separator, raw_pid = owner.rpartition(':')
    user, at, hostname = owner_and_host.rpartition('@')
    if not separator or not at or not user or hostname != socket.gethostname():
        return False
    try:
        pid = int(raw_pid)
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError, OverflowError):
        return False
    return False


def governed_worktree_lane_lease_expired(lane, now):
    expires = parse_coordination_timestamp(lane['lease_expires_at'])
    return bool(expires and expires <= now)


def automatic_lane_cleanup_hold(lane, actor, now):
    """Require a known owner and lease before automatic capture cleanup."""
    owner = lane.get('owner')
    expires = parse_coordination_timestamp(lane.get('lease_expires_at'))
    if not isinstance(owner, str) or not owner.strip() or expires is None:
        return 'lane owner or lease is unverified'
    if owner.startswith('unknown@') and owner.endswith(':0'):
        return 'lane owner is unverified'
    if not isinstance(actor, str) or not actor.strip() or (
        actor.startswith('unknown@') and actor.endswith(':0')
    ):
        return 'current lane owner is unverified'
    if owner != actor and expires > now:
        return f'owner {owner} live'
    return None


def governed_worktree_pending_remedy(manifest, lane, manifest_path=None):
    reason = shlex.quote(
        lane.get('cleanup_event_reason')
        if lane.get('cleanup_event_type') == 'governed_worktree_released'
        and lane.get('cleanup_event_reason') else 'complete captured lane cleanup'
    )
    selector = f' --manifest {shlex.quote(str(manifest_path))}' if manifest_path else ''
    return (
        f"retry with: syncwheel worktree release {shlex.quote(lane['id'])} "
        f'--reason {reason}{selector} --apply'
    )


def governed_worktree_recovery_ref(lane):
    return f'refs/syncwheel/recovery/lanes/{safe_ref_segment(lane["id"])}-{syncwheel_timestamp()}'


def governed_worktree_cleanup_lock_reason(lane):
    material = f"{lane['id']}\0{lane['created_at']}".encode('utf-8')
    token = hashlib.sha256(material).hexdigest()[:24]
    return f"syncwheel-cleanup:{safe_ref_segment(lane['id'])}:{token}"


def governed_worktree_generation_token(lane):
    token = lane.get('generation_token')
    if isinstance(token, str) and token:
        return token
    material = f"{lane['id']}\0{lane['created_at']}\0{lane['branch']}".encode('utf-8')
    return hashlib.sha256(material).hexdigest()


def governed_worktree_cleanup_key(lane):
    material = {
        'lane': lane['id'],
        'generation_token': governed_worktree_generation_token(lane),
        'operation_token': lane.get('cleanup_operation_token'),
        'tip': lane.get('cleanup_tip'),
        'recovery_ref': lane.get('recovery_ref'),
        'event_type': lane.get('cleanup_event_type'),
        'reason': lane.get('cleanup_event_reason'),
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return f'governed-worktree-cleanup:{digest}'


def prepare_governed_worktree_cleanup(lane, event_type, reason, tip=None):
    existing_type = lane.get('cleanup_event_type')
    existing_reason = lane.get('cleanup_event_reason')
    if existing_type is None:
        lane['cleanup_event_type'] = event_type
        lane['cleanup_event_reason'] = reason
    elif event_type == 'governed_worktree_released' and existing_type != event_type:
        raise SyncwheelError(
            f"governed worktree lane {lane['id']!r} is already pending as {existing_type}"
        )
    elif event_type == 'governed_worktree_released' and existing_reason != reason:
        raise SyncwheelError(
            f"governed worktree lane {lane['id']!r} must retry its original --reason "
            f'{existing_reason!r}'
        )
    if tip and not lane.get('cleanup_tip'):
        lane['cleanup_tip'] = tip
    lane.setdefault('cleanup_operation_token', uuid.uuid4().hex)
    lane.setdefault('cleanup_idempotency_key', governed_worktree_cleanup_key(lane))


def governed_worktree_cleanup_intent_payload(lane):
    return {
        'operation_token': lane['cleanup_operation_token'],
        'lane': lane['id'],
        'generation_token': governed_worktree_generation_token(lane),
        'cleanup_tip': lane.get('cleanup_tip'),
        'recovery_ref': lane.get('recovery_ref'),
        'terminal_type': lane.get('cleanup_event_type') or 'governed_worktree_reaped',
        'reason': lane.get('cleanup_event_reason') or 'expired',
        'supersedes': lane.get('cleanup_supersedes_key'),
        'lane_record': copy.deepcopy(lane),
    }


def governed_worktree_release_note_key(lane_id, terminal, reason):
    payload = terminal.get('payload') or {}
    material = {
        'lane': lane_id,
        'generation_token': payload.get('generation_token'),
        'terminal_type': terminal.get('type'),
        'terminal_key': payload.get('idempotency_key'),
        'reason': reason,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    return f'governed-worktree-release-note:{digest}'


def governed_worktree_reaped_payload(lane, reason='expired'):
    return {
        'lane': lane['id'], 'branch': lane['branch'], 'reason': reason,
        'recovery_ref': lane.get('recovery_ref'), 'target': lane.get('target'),
        'archive_ref': lane.get('archive_ref'), 'archive_remote': lane.get('archive_remote'),
        'full': lane['full'],
        'generation_token': governed_worktree_generation_token(lane),
        'operation_token': lane.get('cleanup_operation_token'),
        'cleanup_tip': lane.get('cleanup_tip'),
        'path': lane.get('path'),
    }


def completed_governed_worktree_lane(lane):
    completed = dict(lane)
    completed.pop('pending_reason', None)
    completed.pop('branch_delete_tip', None)
    completed.pop('cleanup_event_type', None)
    completed.pop('cleanup_event_reason', None)
    completed.pop('cleanup_idempotency_key', None)
    completed.pop('cleanup_operation_token', None)
    completed.pop('cleanup_supersedes_key', None)
    completed.pop('cleanup_tip', None)
    completed.pop('cleanup_admin_dir', None)
    completed.pop('cleanup_lock_reason', None)
    completed.pop('cleanup_failure', None)
    return completed


class WorktreeRepository(Protocol):
    def delete_governed_worktree_branch_with_anchor(self, repo_root, lane, tip): ...
    def ensure_governed_worktree_recovery_ref(self, repo_root, recovery_ref, tip): ...
    def governed_worktree_cleanup_candidates(self, repo_root, manifest, registry=None): ...
    def archive_governed_worktree_tip(self, repo_root, manifest, lane, tip): ...

    root: Path

    def git(self, repo_root, *args, check=True, input_text=None, env=None): ...
    def run(self, cmd, cwd=None, check=True, input_text=None, env=None, authorize=True): ...
    def git_common_dir(self, repo_root): ...
    def get_worktrees(self, repo_root): ...
    def find_worktree_record_for_branch(self, repo_root, branch): ...
    def local_worktree_status(self, path): ...
    def ref_tip(self, repo_root, ref): ...
    def branch_exists(self, repo_root, branch): ...
    def commit_full_sha(self, repo_root, ref): ...
    def rev_list(self, repo_root, rev_range): ...
    def governed_worktree_owner(self): ...


class WorktreeLedger(Protocol):
    def append_governed_worktree_cleanup_event(self, repo_root, lane, manifest_path=None): ...

    def append_ledger_event(self,
        repo_root, event_type, payload, manifest_path=None, idempotency_key=None,
    ): ...
    def ledger_events_dir(self, repo_root, manifest_path=None): ...
    def load_ledger_events(self, repo_root, manifest_path=None): ...
    def recover_ledger_tail(self, repo_root, manifest_path=None): ...


class WorktreeStorage(Protocol):
    def load_json_file(self, path, default): ...
    def _write_all(self, descriptor, payload): ...
    def fsync_directory_path(self, directory): ...
    def exclusive_metadata_lock(self,
        lock_path,
        *,
        label,
        recovery_log_path,
        timeout_seconds,
        stale_seconds,
        busy_error,
        metadata=None,
    ): ...
    def coordination_publication_lock_path(self, repo_root): ...


class WorktreeObserver(Protocol):
    def governed_lane_remedy(self, manifest, lane, *, commit_first=False): ...
    def governed_worktree_registry_io_checkpoint(self, stage): ...
    def governed_worktree_cleanup_checkpoint(self, stage): ...


class GovernedWorktrees:
    def __init__(self, repository: WorktreeRepository, ledger: WorktreeLedger,
                 storage: WorktreeStorage, observer: WorktreeObserver):
        self.repository = repository
        self.ledger = ledger
        self.storage = storage
        self.observer = observer

    def governed_worktree_registry_path(self):
        repo_root = self.repository.root
        return self.repository.git_common_dir(repo_root) / 'syncwheel' / 'governed-worktrees.json'

    def governed_worktree_lock_path(self):
        repo_root = self.repository.root
        return self.repository.git_common_dir(repo_root) / 'syncwheel' / 'governed-worktrees.lock'

    @contextlib.contextmanager
    def governed_worktree_registry_lock(self):
        """Serialize clone-local lane state and recover a provably dead owner."""
        repo_root = self.repository.root
        lock_path = self.governed_worktree_lock_path()
        with self.storage.exclusive_metadata_lock(
            lock_path,
            label='governed worktree registry',
            recovery_log_path=lock_path.with_name('governed-worktrees-lock-recovery.jsonl'),
            timeout_seconds=GOVERNED_WORKTREE_LOCK_TIMEOUT_SECONDS,
            stale_seconds=GOVERNED_WORKTREE_LOCK_STALE_SECONDS,
            busy_error=governed_worktree_registry_busy_error,
        ) as (held_path, _metadata):
            yield held_path

    def prune_governed_worktree_stale_locks(self, manifest_path=None):
        """Drop retained stale lock inodes nobody holds; the recovery log keeps the evidence."""
        repo_root = self.repository.root
        if fcntl is None:
            return []
        lock_path = self.governed_worktree_lock_path()
        directory = lock_path.parent
        candidates = []
        for owner in (lock_path, self.storage.coordination_publication_lock_path(repo_root)):
            candidates.extend(owner.parent.glob(f'{owner.name}.stale-*'))
        pruned = []
        for candidate in sorted(set(candidates)):
            try:
                descriptor = os.open(str(candidate), os.O_RDWR)
            except OSError:
                continue
            held = False
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                held = True
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    continue
                pruned.append(candidate.name)
            finally:
                if held:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
        if pruned:
            self.storage.fsync_directory_path(directory)
            self.ledger.append_ledger_event(
                repo_root,
                'governed_worktree_stale_locks_pruned',
                {'files': pruned, 'count': len(pruned)},
                manifest_path,
            )
        return pruned

    def load_governed_worktree_registry(self):
        repo_root = self.repository.root
        path = self.governed_worktree_registry_path()
        data = self.storage.load_json_file(path, {'version': GOVERNED_WORKTREE_REGISTRY_VERSION, 'lanes': []})
        if data.get('version') != GOVERNED_WORKTREE_REGISTRY_VERSION:
            raise SyncwheelError(f'unsupported governed worktree registry version: {path}')
        lanes = data.get('lanes')
        if not isinstance(lanes, list):
            raise SyncwheelError(f'governed worktree registry lanes must be a list: {path}')
        identifiers = set()
        for lane in lanes:
            if not isinstance(lane, dict):
                raise SyncwheelError(f'governed worktree registry lane must be an object: {path}')
            lane_id = lane.get('id')
            if not isinstance(lane_id, str) or not lane_id or lane_id in identifiers:
                raise SyncwheelError(f'governed worktree registry has an invalid or duplicate lane id: {path}')
            identifiers.add(lane_id)
            for key in ('owner', 'path', 'base', 'branch', 'state', 'created_at', 'lease_expires_at'):
                if not isinstance(lane.get(key), str) or not lane[key]:
                    raise SyncwheelError(f'governed worktree lane {lane_id!r} is missing {key!r}: {path}')
            if not isinstance(lane.get('full'), bool):
                raise SyncwheelError(f'governed worktree lane {lane_id!r} has invalid full mode: {path}')
        return data, path

    def save_governed_worktree_registry(
        self,
        registry,
        expected_digest=_REGISTRY_EXPECTED_DIGEST_UNSET,
    ):
        repo_root = self.repository.root
        path = self.governed_worktree_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = governed_worktree_registry_payload(registry)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        replaced = False
        try:
            self.storage._write_all(descriptor, payload)
            self.observer.governed_worktree_registry_io_checkpoint('registry_temp_written')
            os.fsync(descriptor)
            self.observer.governed_worktree_registry_io_checkpoint('registry_temp_fsynced')
            os.close(descriptor)
            descriptor = None
            if expected_digest is not _REGISTRY_EXPECTED_DIGEST_UNSET:
                current_digest = governed_worktree_registry_file_digest(path)
                if current_digest != expected_digest:
                    raise SyncwheelError(
                        'governed worktree registry changed after its decision snapshot; '
                        'refusing a stale local-state write'
                    )
            self.observer.governed_worktree_registry_io_checkpoint('registry_preimage_verified')
            os.replace(temporary, path)
            replaced = True
            self.storage.fsync_directory_path(path.parent)
            self.observer.governed_worktree_registry_io_checkpoint('registry_directory_fsynced')
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if not replaced:
                temporary.unlink(missing_ok=True)
        return path

    def governed_worktree_registry_cas_persister(self, registry):
        repo_root = self.repository.root
        path = self.governed_worktree_registry_path()
        state = {'expected_digest': governed_worktree_registry_file_digest(path)}

        def persist():
            self.save_governed_worktree_registry(
                registry,
                expected_digest=state['expected_digest'],
            )
            state['expected_digest'] = hashlib.sha256(
                governed_worktree_registry_payload(registry)
            ).hexdigest()

        return persist

    def governed_worktree_root(self, manifest):
        repo_root = self.repository.root
        worktrees = self.repository.get_worktrees(repo_root)
        primary = Path(worktrees[0]['path']).resolve() if worktrees else Path(repo_root).resolve()
        return resolve_worktree_root_path(primary, effective_worktree_root(manifest)).resolve()

    def governed_worktree_lane_status(self, manifest, lane, now=None, manifest_path=None):
        repo_root = self.repository.root
        now = now or datetime.datetime.now(datetime.timezone.utc)
        root = self.governed_worktree_root(manifest)
        registered_path = Path(lane['path']).resolve(strict=False)
        worktree = self.repository.find_worktree_record_for_branch(repo_root, lane['branch'])
        worktree_path = (
            Path(worktree['path']).resolve(strict=False)
            if worktree and worktree.get('path') else None
        )
        path_moved = bool(
            not registered_path.exists()
            and worktree_path is not None
            and worktree_path != registered_path
        )
        path = worktree_path if path_moved else registered_path
        status = {
            'id': lane['id'], 'state': lane['state'], 'path': str(path),
            'branch': lane['branch'], 'owner': lane['owner'], 'full': lane['full'],
            'target': lane.get('target'), 'lease_expires_at': lane['lease_expires_at'],
            'code': None, 'remedy': None,
        }
        if path_moved:
            status['registered_path'] = str(registered_path)
            status['path_moved'] = True
        if lane['state'] == 'reaped':
            if lane.get('pending_reason') == 'ledger_pending':
                status.update(
                    code='ledger_pending',
                    remedy=governed_worktree_pending_remedy(manifest, lane, manifest_path),
                )
            return status
        if lane['state'] == 'captured_pending_cleanup':
            status.update(
                code=lane.get('pending_reason') or 'captured_pending_cleanup',
                remedy=governed_worktree_pending_remedy(manifest, lane, manifest_path),
            )
            return status
        if (
            worktree
            and worktree.get('locked')
            and worktree.get('locked') != governed_worktree_cleanup_lock_reason(lane)
        ):
            status.update(
                code='locked',
                remedy='unlock the Git worktree before retrying cleanup',
            )
            return status
        if path.exists():
            if path_moved:
                dirty = self.repository.local_worktree_status(path)
                if dirty is None:
                    status.update(code='unavailable', remedy='restore the worktree before attempting cleanup')
                    return status
                if dirty:
                    status.update(code='dirty', remedy=self.observer.governed_lane_remedy(manifest, lane, commit_first=True))
                    return status
            if not path_is_relative_to(path, root):
                status.update(code='outside_root', remedy='inspect and recover this lane; Syncwheel will not move or remove it')
                return status
            if worktree_path is None or worktree_path != path:
                status.update(code='unregistered_worktree', remedy='inspect the path and branch; Syncwheel will not remove an unregistered worktree')
                return status
            dirty = self.repository.local_worktree_status(path)
            if dirty is None:
                status.update(code='unavailable', remedy='restore the worktree before attempting cleanup')
                return status
            if dirty:
                status.update(code='dirty', remedy=self.observer.governed_lane_remedy(manifest, lane, commit_first=True))
                return status
            if governed_worktree_lane_lease_expired(lane, now):
                status.update(code='expired', remedy=self.observer.governed_lane_remedy(manifest, lane))
                return status
        elif governed_worktree_lane_lease_expired(lane, now) or governed_worktree_owner_is_dead(lane['owner']):
            # An expired or dead owner cannot keep a missing path alive merely
            # because an old root configuration no longer contains that path.
            status.update(code='expired', remedy=self.observer.governed_lane_remedy(manifest, lane))
            return status
        elif not path_is_relative_to(path, root):
            status.update(code='outside_root', remedy='inspect and recover this lane; Syncwheel will not move or remove it')
            return status
        elif worktree_path is None or worktree_path != path:
            status.update(code='unregistered_worktree', remedy='inspect the path and branch; Syncwheel will not remove an unregistered worktree')
            return status
        expires = parse_coordination_timestamp(lane['lease_expires_at'])
        if expires is None:
            status.update(code='invalid_lease', remedy='repair the local registry before any cleanup')
        elif governed_worktree_lane_lease_expired(lane, now):
            status.update(code='expired', remedy=self.observer.governed_lane_remedy(manifest, lane))
        return status

    def governed_worktree_diagnostics(self, manifest, manifest_path=None):
        repo_root = self.repository.root
        registry, path = self.load_governed_worktree_registry()
        root = self.governed_worktree_root(manifest)
        diagnostics = [self.governed_worktree_lane_status(manifest, lane, manifest_path=manifest_path)
                       for lane in registry['lanes']]
        known_paths = {item['path'] for item in diagnostics if item.get('id')}
        for worktree in self.repository.get_worktrees(repo_root):
            worktree_path = Path(worktree['path']).resolve(strict=False)
            if worktree_path == Path(repo_root).resolve(strict=False) or not path_is_relative_to(worktree_path, root):
                continue
            if worktree.get('branch', '').startswith('syncwheel/lane/') and str(worktree_path) not in known_paths:
                diagnostics.append({
                    'id': None, 'state': 'unknown', 'path': str(worktree_path),
                    'branch': worktree.get('branch'), 'owner': None, 'full': None,
                    'target': None, 'lease_expires_at': None,
                    'code': 'unregistered_worktree',
                    'remedy': 'inspect it manually; Syncwheel will not remove an unregistered worktree',
                })
        return {'registry_path': str(path), 'lanes': diagnostics}

    def governed_worktree_warning_lines(self, manifest, manifest_path=None, lanes=None):
        if lanes is None:
            lanes = self.governed_worktree_diagnostics(manifest, manifest_path)['lanes']
        lines = []
        for lane in lanes:
            if not lane['code']:
                continue
            label = lane['id'] or lane['branch'] or lane['path']
            lines.append(
                f"governed worktree {label}: {lane['code']}; {lane['remedy']}"
            )
        return lines

    def emit_governed_worktree_warnings(self, manifest, json_mode=False, manifest_path=None, *, lanes=None):
        if lanes is None:
            lanes = self.governed_worktree_diagnostics(manifest, manifest_path)['lanes']
        lines = self.governed_worktree_warning_lines(manifest, manifest_path, lanes=lanes)
        if not lines:
            return lines
        color = '' if os.environ.get('NO_COLOR') or not sys.stderr.isatty() else YELLOW
        reset = '' if not color else RESET
        if len(lines) > GOVERNED_WORKTREE_WARNING_DETAIL_LIMIT:
            # One line per lane buries the command's own output on busy
            # repositories; summarize by health code and point at the list.
            counts = {}
            for lane in lanes:
                if lane['code']:
                    counts[lane['code']] = counts.get(lane['code'], 0) + 1
            summary = ', '.join(f'{code} {count}' for code, count in sorted(counts.items()))
            print(
                f'{color}WARNING: {len(lines)} governed worktree lanes need attention ({summary}); '
                f'`syncwheel worktree list --problems` shows each lane and its remedy{reset}',
                file=sys.stderr,
            )
            return lines
        for line in lines:
            print(f'{color}WARNING: {line}{reset}', file=sys.stderr)
        return lines

    def governed_worktree_cleanup_tip(self, lane):
        repo_root = self.repository.root
        return (
            lane.get('cleanup_tip')
            or lane.get('branch_delete_tip')
            or self.repository.ref_tip(repo_root, lane['branch'])
            or (self.repository.ref_tip(repo_root, lane['recovery_ref']) if lane.get('recovery_ref') else None)
        )

    def append_governed_worktree_cleanup_intent(self, lane, manifest_path=None):
        repo_root = self.repository.root
        return self.ledger.append_ledger_event(
            repo_root,
            'governed_worktree_cleanup_intent',
            governed_worktree_cleanup_intent_payload(lane),
            manifest_path,
            idempotency_key=lane['cleanup_idempotency_key'],
        )

    def governed_worktree_cleanup_ledger(self, manifest_path=None):
        repo_root = self.repository.root
        if not self.ledger.ledger_events_dir(repo_root, manifest_path).exists():
            return {
                'events': [],
                'intents': {},
                'pending': {},
                'successful': {},
                'superseded': set(),
            }
        self.ledger.recover_ledger_tail(repo_root, manifest_path)
        events = self.ledger.load_ledger_events(repo_root, manifest_path)
        intents = {}
        superseded = set()
        successful = {}
        for event in events:
            payload = event.get('payload') or {}
            key = payload.get('idempotency_key')
            if not isinstance(key, str) or not key:
                continue
            if event.get('type') == 'governed_worktree_cleanup_intent':
                intents[key] = event
                prior = payload.get('supersedes')
                if isinstance(prior, str) and prior:
                    superseded.add(prior)
            elif event.get('type') in {'governed_worktree_reaped', 'governed_worktree_released'}:
                successful[key] = event
        pending = {
            key: event for key, event in intents.items()
            if key not in successful and key not in superseded
        }
        return {
            'events': events,
            'intents': intents,
            'pending': pending,
            'successful': successful,
            'superseded': superseded,
        }

    def recover_governed_worktree_registry_from_ledger(
        self,
        registry,
        persist,
        manifest_path=None,
        *,
        protect_lane=None,
    ):
        repo_root = self.repository.root
        ledger = self.governed_worktree_cleanup_ledger(manifest_path)
        changed = False
        by_id = {lane['id']: lane for lane in registry['lanes']}

        for key, terminal in ledger['successful'].items():
            payload = terminal.get('payload') or {}
            lane_id = payload.get('lane')
            generation = payload.get('generation_token')
            lane = by_id.get(lane_id)
            if (
                lane is not None
                and not (protect_lane and protect_lane(lane))
                and generation
                and governed_worktree_generation_token(lane) == generation
            ):
                registry['lanes'].remove(lane)
                by_id.pop(lane_id, None)
                changed = True

        pending_by_lane = {}
        for key, intent in ledger['pending'].items():
            payload = intent.get('payload') or {}
            lane_id = payload.get('lane')
            if isinstance(lane_id, str):
                previous = pending_by_lane.get(lane_id)
                if previous is None or intent.get('seq', 0) > previous[1].get('seq', 0):
                    pending_by_lane[lane_id] = (key, intent)

        for lane_id, (key, intent) in pending_by_lane.items():
            payload = intent.get('payload') or {}
            recorded = payload.get('lane_record')
            generation = payload.get('generation_token')
            if not isinstance(recorded, dict) or not generation:
                continue
            lane = by_id.get(lane_id)
            if protect_lane and (protect_lane(recorded) or (lane is not None and protect_lane(lane))):
                continue
            if lane is None:
                restored = copy.deepcopy(recorded)
                registry['lanes'].append(restored)
                by_id[lane_id] = restored
                changed = True
                continue
            if governed_worktree_generation_token(lane) != generation:
                raise SyncwheelError(
                    f'governed worktree lane {lane_id!r} has a different generation than its '
                    'durable cleanup intent; inspect the lane before retrying cleanup'
                )
            current_key = lane.get('cleanup_idempotency_key')
            if current_key == key:
                continue
            if current_key is not None:
                if payload.get('supersedes') != current_key:
                    raise SyncwheelError(
                        f'governed worktree lane {lane_id!r} has a cleanup operation that conflicts '
                        'with its durable ledger intent'
                    )
            lane.clear()
            lane.update(copy.deepcopy(recorded))
            changed = True

        if changed:
            persist()
        return ledger

    def governed_worktree_release_terminal(self, lane_id, manifest_path=None):
        """Latest terminal any Syncwheel command wrote for this lane, release or reap."""
        repo_root = self.repository.root
        ledger = self.governed_worktree_cleanup_ledger(manifest_path)
        intent_keys = {
            key for key, event in ledger['intents'].items()
            if (event.get('payload') or {}).get('lane') == lane_id
        }
        for event in reversed(ledger['events']):
            if event.get('type') not in GOVERNED_WORKTREE_TERMINAL_EVENT_TYPES:
                continue
            payload = event.get('payload') or {}
            if (
                payload.get('lane') == lane_id
                and payload.get('idempotency_key') in intent_keys
            ):
                return event
        return None

    def append_governed_worktree_release_note(self, lane_id, reason, terminal, manifest_path=None):
        repo_root = self.repository.root
        payload = terminal.get('payload') or {}
        return self.ledger.append_ledger_event(
            repo_root,
            'governed_worktree_release_noted',
            {
                'lane': lane_id,
                'reason': reason,
                'terminal_type': terminal.get('type'),
                'terminal_seq': terminal.get('seq'),
                'terminal_reason': payload.get('reason'),
                'generation_token': payload.get('generation_token'),
                'branch': payload.get('branch'),
                'path': payload.get('path'),
                'recovery_ref': payload.get('recovery_ref'),
            },
            manifest_path,
            idempotency_key=governed_worktree_release_note_key(lane_id, terminal, reason),
        )

    def ensure_governed_worktree_recovery_ref(self, recovery_ref, tip):
        repo_root = self.repository.root
        current = self.repository.ref_tip(repo_root, recovery_ref)
        if current == tip:
            return
        if current is not None:
            raise SyncwheelError(
                f'governed worktree recovery_ref_moved: recovery ref {recovery_ref} '
                f'points to {current} instead of {tip}'
            )
        result = self.repository.git(repo_root, 'update-ref', recovery_ref, tip, ZERO_OBJECT_ID, check=False)
        if result.returncode == 0:
            return
        current = self.repository.ref_tip(repo_root, recovery_ref)
        if current == tip:
            return
        if current is not None:
            raise SyncwheelError(
                f'governed worktree recovery_ref_moved: recovery ref {recovery_ref} '
                f'points to {current} instead of {tip}'
            )
        raise SyncwheelError(
            result.stderr.strip() or result.stdout.strip()
            or f'could not create governed worktree recovery ref {recovery_ref}'
        )

    def verify_governed_worktree_recovery_ref(self, recovery_ref, tip):
        repo_root = self.repository.root
        current = self.repository.ref_tip(repo_root, recovery_ref)
        if current != tip:
            actual = current or 'missing'
            raise SyncwheelError(
                f'governed worktree recovery_ref_moved: recovery ref {recovery_ref} '
                f'points to {actual} instead of {tip}; restore the anchored tip before retrying cleanup'
            )

    def governed_worktree_record_for_path(self, path):
        repo_root = self.repository.root
        expected = Path(path).resolve(strict=False)
        for worktree in self.repository.get_worktrees(repo_root):
            if worktree.get('path') and Path(worktree['path']).resolve(strict=False) == expected:
                return worktree
        return None

    def governed_worktree_admin_dir_for_path(self, path):
        repo_root = self.repository.root
        expected_gitdir = (Path(path).resolve(strict=False) / '.git').resolve(strict=False)
        worktrees_dir = self.repository.git_common_dir(repo_root) / 'worktrees'
        if not worktrees_dir.is_dir():
            return None
        matches = []
        for admin_dir in sorted(item for item in worktrees_dir.iterdir() if item.is_dir()):
            gitdir_path = admin_dir / 'gitdir'
            try:
                raw_gitdir = gitdir_path.read_text(encoding='utf-8').strip()
            except OSError:
                continue
            candidate = Path(raw_gitdir)
            if not candidate.is_absolute():
                candidate = admin_dir / candidate
            if candidate.resolve(strict=False) == expected_gitdir:
                matches.append(admin_dir.resolve(strict=False))
        return matches[0] if len(matches) == 1 else None

    def governed_worktree_registration_matches(self, lane, path, admin_dir, lock_reason):
        repo_root = self.repository.root
        path = Path(path).resolve(strict=False)
        admin_dir = Path(admin_dir).resolve(strict=False)
        try:
            raw_gitdir = (admin_dir / 'gitdir').read_text(encoding='utf-8').strip()
            head = (admin_dir / 'HEAD').read_text(encoding='utf-8').strip()
        except OSError:
            return False
        registered_gitdir = Path(raw_gitdir)
        if not registered_gitdir.is_absolute():
            registered_gitdir = admin_dir / registered_gitdir
        if registered_gitdir.resolve(strict=False) != (path / '.git').resolve(strict=False):
            return False
        if head != f"ref: refs/heads/{lane['branch']}":
            return False
        worktree = self.governed_worktree_record_for_path(path)
        return bool(worktree and worktree.get('locked') == lock_reason)

    def lock_governed_worktree_for_cleanup(self, lane):
        repo_root = self.repository.root
        stored_admin = lane.get('cleanup_admin_dir')
        if stored_admin:
            path = Path(lane['path']).resolve(strict=False)
            worktree = self.governed_worktree_record_for_path(path)
            admin_dir = Path(stored_admin).resolve(strict=False)
            if not worktree and not admin_dir.exists() and not path.exists():
                return {
                    'path': path,
                    'admin_dir': admin_dir,
                    'lock_reason': governed_worktree_cleanup_lock_reason(lane),
                    'registration_removed': True,
                }, None
        else:
            worktree = self.repository.find_worktree_record_for_branch(repo_root, lane['branch'])
            if not worktree or not worktree.get('path'):
                return None, None
            path = Path(worktree['path']).resolve(strict=False)
            admin_dir = self.governed_worktree_admin_dir_for_path(path)
        if not worktree:
            return None, {
                'code': 'registration_mismatch',
                'remedy': 'inspect the recorded Git worktree registration before retrying cleanup',
            }
        lock_reason = governed_worktree_cleanup_lock_reason(lane)
        locked = self.repository.git(
            repo_root,
            'worktree',
            'lock',
            '--reason',
            lock_reason,
            str(path),
            check=False,
        )
        refreshed = self.governed_worktree_record_for_path(path)
        if locked.returncode != 0 and not (refreshed and refreshed.get('locked') == lock_reason):
            return None, {
                'code': 'lane_in_use',
                'remedy': 'retry after the process using the governed lane releases its Git worktree lock',
            }
        if admin_dir is None or not self.governed_worktree_registration_matches(
            lane,
            path,
            admin_dir,
            lock_reason,
        ):
            if refreshed and refreshed.get('locked') == lock_reason:
                self.repository.git(repo_root, 'worktree', 'unlock', str(path), check=False)
            return None, {
                'code': 'registration_mismatch',
                'remedy': 'inspect the recorded Git worktree registration before retrying cleanup',
            }
        return {
            'path': path,
            'admin_dir': admin_dir,
            'lock_reason': lock_reason,
        }, None

    def unlock_governed_worktree_cleanup(self, lock):
        repo_root = self.repository.root
        if not lock:
            return
        worktree = self.governed_worktree_record_for_path(lock['path'])
        if worktree and worktree.get('locked') == lock['lock_reason']:
            self.repository.git(repo_root, 'worktree', 'unlock', str(lock['path']), check=False)

    def governed_worktree_final_probe(self, path, anchored_tip, missing_at_classification):
        path = Path(path).resolve(strict=False)
        if missing_at_classification and path.exists():
            return {
                'code': 'path_reappeared',
                'remedy': 'inspect the reappeared lane path, then retry its recorded worktree release',
            }
        if not path.exists():
            return None
        untracked = self.repository.run(
            ['git', '-C', str(path), 'ls-files', '--others', '--exclude-standard', '-z'],
            check=False,
        )
        if untracked.returncode != 0:
            return {
                'code': 'unavailable',
                'remedy': 'restore the worktree before retrying cleanup',
            }
        tracked = self.repository.run(
            ['git', '-C', str(path), 'diff-index', '--quiet', anchored_tip, '--'],
            check=False,
        )
        if untracked.stdout or tracked.returncode != 0:
            return {
                'code': 'dirty',
                'remedy': 'the worktree became dirty before removal; retain or commit its changes, then retry release',
            }
        return None

    def delete_governed_worktree_branch_with_anchor(self, lane, tip):
        repo_root = self.repository.root
        recovery_ref = lane['recovery_ref']
        branch_ref = f'refs/heads/{lane["branch"]}'
        transaction = '\n'.join([
            'start',
            f'update {recovery_ref} {tip} {tip}',
            f'delete {branch_ref} {tip}',
            'prepare',
            'commit',
            '',
        ])
        deletion = self.repository.git(
            repo_root,
            'update-ref',
            '--stdin',
            check=False,
            input_text=transaction,
        )
        committed = 'commit: ok' in {line.strip() for line in deletion.stdout.splitlines()}
        if (
            deletion.returncode == 0
            and committed
            and self.repository.ref_tip(repo_root, recovery_ref) == tip
            and self.repository.ref_tip(repo_root, lane['branch']) is None
        ):
            return True, None
        recovery_tip = self.repository.ref_tip(repo_root, recovery_ref)
        branch_tip = self.repository.ref_tip(repo_root, lane['branch'])
        if recovery_tip != tip:
            actual = recovery_tip or 'missing'
            return False, {
                'code': 'recovery_ref_moved',
                'remedy': (
                    f'restore recovery ref {recovery_ref} from {actual} to anchored tip {tip}, '
                    'then retry cleanup'
                ),
            }
        if branch_tip != tip:
            return False, {
                'code': 'branch_advanced',
                'remedy': 'inspect the retained lane branch; its recovery ref is immutable',
            }
        detail = deletion.stderr.strip() or deletion.stdout.strip()
        if deletion.returncode == 0 and not committed:
            detail = 'ref transaction did not report commit: ok'
        return False, {
            'code': 'branch_delete_failed',
            'remedy': detail or 'retry cleanup',
        }

    def governed_worktree_cleanup_candidates(self, manifest, registry=None):
        repo_root = self.repository.root
        if registry is None:
            registry = self.load_governed_worktree_registry()[0]
        candidates = []
        for lane in registry['lanes']:
            status = self.governed_worktree_lane_status(manifest, lane)
            if lane['state'] == 'active' and status['code'] != 'expired':
                continue
            if lane['state'] == 'reaped' and lane.get('pending_reason') != 'ledger_pending':
                continue
            if lane['state'] not in {'active', 'captured_pending_cleanup', 'reaped'}:
                continue
            candidates.append(status)
        return candidates

    def reap_governed_worktree_lane(
        self,
        manifest,
        lane,
        persist=None,
        manifest_path=None,
        event_type='governed_worktree_reaped',
        event_reason='expired',
    ):
        repo_root = self.repository.root
        if lane['state'] == 'reaped' and lane.get('pending_reason') == 'ledger_pending':
            return True, self.governed_worktree_lane_status(manifest, lane)

        status = self.governed_worktree_lane_status(manifest, lane)
        lock, lock_failure = self.lock_governed_worktree_for_cleanup(lane)
        if lock_failure:
            return False, {**status, **lock_failure}
        self.observer.governed_worktree_cleanup_checkpoint('after_git_worktree_lock')
        path = lock['path'] if lock else Path(lane['path']).resolve(strict=False)
        if lock and Path(lane['path']).resolve(strict=False) != path:
            lane['path'] = str(path)

        def fail_before_ref_change(code, remedy):
            self.unlock_governed_worktree_cleanup(lock)
            return False, {**status, 'code': code, 'remedy': remedy}

        path_exists = path.exists()
        if path_exists and not path_is_relative_to(path, self.governed_worktree_root(manifest)):
            return fail_before_ref_change(
                'outside_root',
                'inspect and recover this lane; Syncwheel will not move or remove it',
            )
        if path_exists and not lock:
            return fail_before_ref_change(
                'unregistered_worktree',
                'inspect the path and branch; Syncwheel will not remove an unregistered worktree',
            )
        if lane['state'] == 'active' and event_type != 'governed_worktree_released':
            expired = governed_worktree_lane_lease_expired(
                lane,
                datetime.datetime.now(datetime.timezone.utc),
            )
            abandoned_missing = not path_exists and governed_worktree_owner_is_dead(lane['owner'])
            if not expired and not abandoned_missing:
                return fail_before_ref_change(status['code'], status['remedy'])
        current_tip = self.repository.ref_tip(repo_root, lane['branch'])
        post_ref_cleanup = bool(
            current_tip is None
            and lane.get('cleanup_tip')
            and lane.get('recovery_ref')
        )
        if path_exists and post_ref_cleanup:
            retry_probe = self.governed_worktree_final_probe(path, lane['cleanup_tip'], False)
            if retry_probe:
                lane['state'] = 'captured_pending_cleanup'
                lane['pending_reason'] = 'worktree_remove_failed'
                lane['cleanup_failure'] = retry_probe['code']
                if persist:
                    persist()
                return False, {**status, **retry_probe}
        elif path_exists:
            dirty = self.repository.local_worktree_status(path)
            if dirty is None:
                return fail_before_ref_change(
                    'unavailable',
                    'restore the worktree before attempting cleanup',
                )
            if dirty:
                return fail_before_ref_change(
                    'dirty',
                    self.observer.governed_lane_remedy(manifest, lane, commit_first=True),
                )
        current = self.repository.run(['git', 'rev-parse', '--show-toplevel'], check=False)
        current_path = Path(current.stdout.strip()).resolve(strict=False) if current.returncode == 0 else None
        if current_path == path:
            return fail_before_ref_change(
                'current_directory',
                'leave the lane directory, then run a Syncwheel mutation again',
            )

        branch_advanced_pending = lane.get('pending_reason') == 'branch_advanced'
        retrying_advanced = bool(
            branch_advanced_pending
            and current_tip
            and lane.get('cleanup_tip')
            and lane.get('recovery_ref')
            and lane.get('cleanup_event_type')
        )
        if branch_advanced_pending and not retrying_advanced:
            return False, {
                **status,
                'code': 'branch_advanced',
                'remedy': governed_worktree_pending_remedy(manifest, lane, manifest_path),
            }
        if retrying_advanced and current_tip:
            superseded_key = lane.get('cleanup_idempotency_key')
            superseded_event_type = lane.get('cleanup_event_type')
            lane.pop('cleanup_tip', None)
            lane.pop('branch_delete_tip', None)
            lane.pop('recovery_ref', None)
            lane.pop('cleanup_idempotency_key', None)
            lane.pop('cleanup_operation_token', None)
            # The new tip gets a new recovery ref, so it needs its own archive ref.
            for key in ('archive_ref', 'archive_remote', 'archive_skipped', 'archive_confirmed_tip'):
                lane.pop(key, None)
            if (
                event_type == 'governed_worktree_released'
                and superseded_event_type != event_type
            ):
                lane.pop('cleanup_event_type', None)
                lane.pop('cleanup_event_reason', None)
            if superseded_key:
                lane['cleanup_supersedes_key'] = superseded_key

        anchored_tip = self.governed_worktree_cleanup_tip(lane)
        if lane.get('cleanup_tip') and current_tip not in {None, lane['cleanup_tip']}:
            lane['state'] = 'captured_pending_cleanup'
            lane['pending_reason'] = 'branch_advanced'
            lane['cleanup_admin_dir'] = str(lock['admin_dir']) if lock else None
            lane['cleanup_lock_reason'] = lock['lock_reason'] if lock else None
            if persist:
                persist()
            return False, {
                **status,
                'code': 'branch_advanced',
                'remedy': governed_worktree_pending_remedy(manifest, lane, manifest_path),
            }

        lane['state'] = 'captured_pending_cleanup'
        lane['pending_reason'] = 'reaping'
        if lock:
            lane['cleanup_admin_dir'] = str(lock['admin_dir'])
            lane['cleanup_lock_reason'] = lock['lock_reason']
        if anchored_tip:
            lane['cleanup_tip'] = anchored_tip
            lane['recovery_ref'] = lane.get('recovery_ref') or governed_worktree_recovery_ref(lane)
        prepare_governed_worktree_cleanup(lane, event_type, event_reason, anchored_tip)
        self.observer.governed_worktree_cleanup_checkpoint('before_cleanup_intent')
        self.append_governed_worktree_cleanup_intent(lane, manifest_path)
        if persist:
            persist()
        self.observer.governed_worktree_cleanup_checkpoint('after_cleanup_intent')

        if anchored_tip:
            try:
                self.repository.ensure_governed_worktree_recovery_ref(repo_root, lane['recovery_ref'], anchored_tip)
                self.verify_governed_worktree_recovery_ref(lane['recovery_ref'], anchored_tip)
            except SyncwheelError:
                lane['pending_reason'] = 'recovery_ref_moved'
                if persist:
                    persist()
                raise
            self.observer.governed_worktree_cleanup_checkpoint('after_recovery_anchor')
            archive_failure = self.repository.archive_governed_worktree_tip(
                repo_root, manifest, lane, anchored_tip,
            )
            if archive_failure:
                lane['pending_reason'] = 'archive_push_failed'
                if persist:
                    persist()
                return False, {**status, **archive_failure}
            if persist:
                persist()
            self.observer.governed_worktree_cleanup_checkpoint('after_remote_archive')
            current_tip = self.repository.ref_tip(repo_root, lane['branch'])
            if current_tip is not None:
                if current_tip != anchored_tip:
                    lane['pending_reason'] = 'branch_advanced'
                    if persist:
                        persist()
                    return False, {
                        **status,
                        'code': 'branch_advanced',
                        'remedy': governed_worktree_pending_remedy(manifest, lane, manifest_path),
                    }
                deleted, delete_detail = self.repository.delete_governed_worktree_branch_with_anchor(repo_root,
                    lane,
                    anchored_tip,
                )
                if not deleted:
                    lane['pending_reason'] = delete_detail['code']
                    lane['branch_delete_tip'] = anchored_tip
                    if persist:
                        persist()
                    if delete_detail['code'] == 'branch_advanced':
                        delete_detail['remedy'] = governed_worktree_pending_remedy(manifest, lane, manifest_path)
                    return False, {**status, **delete_detail}
                self.observer.governed_worktree_cleanup_checkpoint('after_ref_transaction')
            else:
                try:
                    self.verify_governed_worktree_recovery_ref(lane['recovery_ref'], anchored_tip)
                except SyncwheelError:
                    lane['pending_reason'] = 'recovery_ref_moved'
                    if persist:
                        persist()
                    raise

        if lock and not lock.get('registration_removed') and not self.governed_worktree_registration_matches(
            lane,
            path,
            lock['admin_dir'],
            lock['lock_reason'],
        ):
            lane['pending_reason'] = 'worktree_remove_failed'
            lane['cleanup_failure'] = 'registration_mismatch'
            if persist:
                persist()
            return False, {
                **status,
                'code': 'registration_mismatch',
                'remedy': 'the locked Git worktree registration changed; inspect it before retrying release',
            }

        final_probe = self.governed_worktree_final_probe(path, anchored_tip, not path_exists)
        if final_probe:
            lane['pending_reason'] = 'worktree_remove_failed'
            lane['cleanup_failure'] = final_probe['code']
            if persist:
                persist()
            return False, {**status, **final_probe}

        if lock and not lock.get('registration_removed'):
            self.observer.governed_worktree_cleanup_checkpoint('before_worktree_remove')
            try:
                removal = self.repository.run(
                    ['git', 'worktree', 'remove', '--force', '--force', str(path)],
                    cwd=repo_root,
                    check=False,
                )
            except SyncwheelError:
                lane['pending_reason'] = 'worktree_remove_failed'
                lane['cleanup_failure'] = 'worktree_remove_failed'
                if persist:
                    persist()
                raise
            if removal.returncode != 0:
                lane['pending_reason'] = 'worktree_remove_failed'
                lane['cleanup_failure'] = 'worktree_remove_failed'
                if persist:
                    persist()
                raise SyncwheelError(
                    removal.stderr.strip() or removal.stdout.strip()
                    or f'could not remove governed worktree {path}'
                )
            self.observer.governed_worktree_cleanup_checkpoint('after_worktree_remove')
            if Path(lock['admin_dir']).exists():
                lane['pending_reason'] = 'worktree_remove_failed'
                lane['cleanup_failure'] = 'registration_mismatch'
                if persist:
                    persist()
                return False, {
                    **status,
                    'code': 'registration_mismatch',
                    'remedy': 'Git retained the targeted worktree registration; inspect it before retrying release',
                }

        lane['state'] = 'reaped'
        lane['reaped_at'] = iso_utc_now()
        lane['pending_reason'] = 'ledger_pending'
        if persist:
            persist()
        return True, self.governed_worktree_lane_status(manifest, lane)

    def append_governed_worktree_cleanup_event(self, lane, manifest_path=None):
        repo_root = self.repository.root
        event_type = lane.get('cleanup_event_type') or 'governed_worktree_reaped'
        reason = lane.get('cleanup_event_reason') or 'expired'
        key = lane.get('cleanup_idempotency_key') or governed_worktree_cleanup_key(lane)
        lane['cleanup_idempotency_key'] = key
        return self.ledger.append_ledger_event(
            repo_root,
            event_type,
            governed_worktree_reaped_payload(lane, reason),
            manifest_path,
            idempotency_key=key,
        )

    def reconcile_governed_worktrees(
        self, manifest, manifest_path=None, candidate_ids=None, repair_all_paths=False,
        *, protect_foreign_lanes=False,
    ):
        repo_root = self.repository.root
        with self.governed_worktree_registry_lock():
            self.prune_governed_worktree_stale_locks(manifest_path)
            registry, _ = self.load_governed_worktree_registry()
            persist = self.governed_worktree_registry_cas_persister(registry)
            protect_lane = self.automatic_cleanup_hold() if protect_foreign_lanes else None
            self.recover_governed_worktree_registry_from_ledger(
                registry,
                persist,
                manifest_path,
                protect_lane=protect_lane,
            )
            paths_updated = False
            for lane in registry['lanes']:
                if protect_lane and protect_lane(lane):
                    continue
                status = self.governed_worktree_lane_status(manifest, lane)
                if (
                    status.get('path_moved')
                    and (repair_all_paths or candidate_ids is None or lane['id'] in candidate_ids)
                ):
                    lane['path'] = status['path']
                    paths_updated = True
            if paths_updated:
                persist()
            if candidate_ids is None:
                candidates = self.repository.governed_worktree_cleanup_candidates(repo_root, manifest, registry)
                selected = {item['id'] for item in candidates}
            else:
                selected = set(candidate_ids)
            reaped = []
            failures = []
            for lane in list(registry['lanes']):
                if lane['id'] not in selected or (protect_lane and protect_lane(lane)):
                    continue
                completed, detail = self.reap_governed_worktree_lane(
                    manifest,
                    lane,
                    persist=persist,
                    manifest_path=manifest_path,
                )
                if not completed:
                    failures.append({
                        'id': lane['id'],
                        'code': detail.get('code') or 'lane_generation_changed',
                    })
                    continue
                try:
                    self.observer.governed_worktree_cleanup_checkpoint('before_terminal_ledger')
                    self.ledger.append_governed_worktree_cleanup_event(repo_root, lane, manifest_path)
                    self.observer.governed_worktree_cleanup_checkpoint('after_terminal_ledger')
                except Exception:
                    lane['state'] = 'reaped'
                    lane['pending_reason'] = 'ledger_pending'
                    persist()
                    raise
                registry['lanes'].remove(lane)
                persist()
                self.observer.governed_worktree_cleanup_checkpoint('after_cleanup_record_removed')
                reaped.append({'id': lane['id']})
            return {'reaped': reaped, 'failures': failures}

    def governed_worktree_commit_set(self, lane):
        repo_root = self.repository.root
        if not self.repository.branch_exists(repo_root, lane['branch']):
            return set()
        return set(self.repository.rev_list(repo_root, f"{lane['base']}..{lane['branch']}"))

    def automatic_cleanup_hold(self):
        actor = self.repository.governed_worktree_owner()
        now = datetime.datetime.now(datetime.timezone.utc)
        return lambda lane: automatic_lane_cleanup_hold(lane, actor, now)

    def capture_governed_worktrees_for_stack(self, manifest, stack_id, manifest_path=None):
        repo_root = self.repository.root
        stack = require_stack(manifest, stack_id)
        owned = {self.repository.commit_full_sha(repo_root, commit) for commit in stack['commits']}
        with self.governed_worktree_registry_lock():
            registry, _ = self.load_governed_worktree_registry()
            persist = self.governed_worktree_registry_cas_persister(registry)
            hold = self.automatic_cleanup_hold()
            self.recover_governed_worktree_registry_from_ledger(
                registry,
                persist,
                manifest_path,
                protect_lane=hold,
            )
            captured = []
            for lane in registry['lanes']:
                if lane['state'] != 'active' or lane.get('target') not in {None, stack_id}:
                    continue
                commits = self.governed_worktree_commit_set(lane)
                if not commits or not commits.issubset(owned):
                    continue
                reason = hold(lane)
                if reason:
                    print(
                        f"WARNING: governed worktree {lane['id']} captured, kept: {reason}",
                        file=sys.stderr,
                    )
                    continue
                lane['state'] = 'captured_pending_cleanup'
                lane['captured_at'] = iso_utc_now()
                reaped, detail = self.reap_governed_worktree_lane(
                    manifest,
                    lane,
                    persist=persist,
                    manifest_path=manifest_path,
                )
                if not reaped and detail.get('code') == 'archive_push_failed':
                    print(
                        f"WARNING: governed worktree {lane['id']} was captured but kept: "
                        f"{detail.get('remedy')}",
                        file=sys.stderr,
                    )
                captured.append({
                    'id': lane['id'], 'state': lane['state'], 'detail': detail['code'], 'reaped': reaped,
                })
            if captured:
                persist()
            return captured
