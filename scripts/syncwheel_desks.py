"""Ownership and recoverable removal of persistent stack replay desks."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Protocol

from syncwheel_errors import SyncwheelError
from syncwheel_manifest import canonical_json_digest, manifest_digest, safe_ref_segment


class DeskRepository(Protocol):
    root: Path

    def git(self, *args, **kwargs): ...
    def get_worktrees(self, *args, **kwargs): ...
    def ref_tip(self, *args, **kwargs): ...
    def publish_desk_recovery_refs(self, operations): ...
    def binary_git(self, path, *args, **kwargs): ...
    def write_desk_intent(self, path, payload): ...


class DeskLifecycle:
    def __init__(self, repo, worktrees):
        self.repo = repo
        self.worktrees = worktrees

    def desks(self):
        registry, _ = self.worktrees.load_governed_worktree_registry()
        return registry.get('desks', [])

    def identity(self, path, stack):
        root = self.repo.root
        records = self.repo.get_worktrees(root)
        matches = [record for record in records
                   if record.get('branch') == stack['branch']]
        if (not records or not records[0].get('path') or Path(records[0]['path']).resolve() == path
                or len(matches) != 1 or Path(matches[0]['path']).resolve() != path):
            raise SyncwheelError('desk requires the exact linked checkout of the selected stack branch')
        if matches[0].get('locked') or matches[0].get('prunable'):
            raise SyncwheelError('desk checkout is locked or has a stale Git registration')
        if Path.cwd().resolve().is_relative_to(path):
            raise SyncwheelError('run desk release from outside the checkout being released')
        admin = self.worktrees.governed_worktree_admin_dir_for_path(path)
        actual = Path(self.repo.git(path, 'rev-parse', '--absolute-git-dir').stdout.strip()).resolve()
        if (not admin or admin != actual or (path / '.git').is_symlink()
                or not (path / '.git').is_file()):
            raise SyncwheelError('desk Git administration identity could not be verified')
        source_ref = f"refs/heads/{stack['branch']}"
        tip = self.repo.ref_tip(root, source_ref)
        if matches[0].get('HEAD') != tip or not tip:
            raise SyncwheelError('desk source tip changed while reading its registration')
        if self.repo.git(root, 'symbolic-ref', '--quiet', source_ref, check=False).returncode == 0:
            raise SyncwheelError('desk requires a direct stack branch')
        return admin, tip

    def require_no_lane(self, registry, stack, path, admin=None):
        active = [lane for lane in registry['lanes']
                  if lane.get('state') != 'reaped' or lane.get('pending_reason')]
        for lane in active:
            registered_path = Path(lane['path']).resolve()
            recorded_admin = lane.get('cleanup_admin_dir') or lane.get('admin_dir')
            if (registered_path == path or lane.get('branch') == stack['branch']
                    or lane.get('target') == stack['id']
                    or (admin and recorded_admin and Path(recorded_admin).resolve() == admin)):
                raise SyncwheelError(
                    f"governed worktree lane {lane['id']} overlaps the desk; resolve it through worktree list/release"
                )
            if admin and not recorded_admin and not self.worktrees.governed_worktree_admin_dir_for_path(
                registered_path
            ):
                records = self.repo.get_worktrees(self.repo.root)
                if not any(record.get('branch') == lane['branch'] for record in records):
                    raise SyncwheelError(
                        f"cannot prove desk is distinct from governed lane {lane['id']}; inspect syncwheel worktree list"
                    )

    def register_created(self, manifest_path, stack, path):
        with self.worktrees.governed_worktree_registry_lock():
            registry, _ = self.worktrees.load_governed_worktree_registry()
            persist = self.worktrees.governed_worktree_registry_cas_persister(registry)
            path = Path(path).resolve()
            self.require_no_lane(registry, stack, path)
            admin, _ = self.identity(path, stack)
            self.require_no_lane(registry, stack, path, admin)
            records = registry.setdefault('desks', [])
            record = {'stack': stack['id'], 'branch': stack['branch'], 'path': str(path),
                      'manifest': str(manifest_path.resolve()), 'admin': str(admin),
                      'identity': list(file_identity(admin)), 'checkoutIdentity': list(file_identity(path)),
                      'kind': 'replay-desk'}
            if any(item['path'] == str(path) or item['admin'] == str(admin) for item in records):
                raise SyncwheelError('desk registration overlaps an existing desk record')
            records.append(record)
            persist()

    def select(self, registry, manifest_path, stack, legacy):
        records = registry.get('desks', [])
        matching = [item for item in records if item['stack'] == stack['id']
                    and item['manifest'] == str(manifest_path.resolve())]
        if legacy is None and len(matching) != 1:
            raise SyncwheelError('no uniquely registered desk; inspect the linked checkout and preview with --legacy-worktree PATH --reason REASON')
        if legacy is not None and matching:
            raise SyncwheelError('a registered desk exists for this stack; omit --legacy-worktree')
        path = Path(legacy).expanduser().absolute() if legacy is not None else Path(matching[0]['path'])
        if path.resolve() != path:
            raise SyncwheelError('desk path must name the exact checkout without symbolic aliases')
        self.require_no_lane(registry, stack, path)
        if legacy is not None and any(item['path'] == str(path) or item['branch'] == stack['branch']
                                     for item in records):
            raise SyncwheelError('legacy checkout overlaps another registered desk')
        record = matching[0] if matching else None
        if record:
            if not (self.check_identity(path, record['checkoutIdentity'], directory=True)
                    and self.check_identity(Path(record['admin']), record['identity'], directory=True)):
                raise SyncwheelError('registered desk checkout or Git administration is missing')
        return path, record

    def observe(self, path, stack):
        admin, source_tip = self.identity(path, stack)
        for marker in ('index.lock', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                       'rebase-apply', 'rebase-merge', 'sequencer', 'BISECT_LOG'):
            if (admin / marker).exists():
                raise SyncwheelError(f'desk has an interrupted Git operation ({marker}); finish or abort it before release')
        if self.repo.git(path, 'rev-parse', '--shared-index-path').stdout.strip():
            raise SyncwheelError('desk release cannot preserve a split index')
        if self.repo.binary_git(path, 'ls-files', '-u', '-z'):
            raise SyncwheelError('desk index has unresolved conflicts')
        if self.repo.binary_git(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z'):
            raise SyncwheelError('desk has ignored files; preserve or relocate them before release')
        if b'160000 ' in self.repo.binary_git(path, 'ls-files', '--stage', '-z'):
            raise SyncwheelError('desk release cannot preserve a submodule checkout')
        raw_index, index_mode, _ = read_regular(admin / 'index')
        entries, content = self.scan_checkout(path)
        untracked = [os.fsdecode(part) for part in self.repo.binary_git(
            path, 'ls-files', '--others', '--exclude-standard', '-z',
        ).split(b'\0') if part]
        return {'admin': str(admin), 'identity': list(file_identity(admin)),
                'checkoutIdentity': list(file_identity(path)), 'sourceTip': source_tip,
                'indexSha256': hashlib.sha256(raw_index).hexdigest(), 'indexMode': index_mode,
                'rootMode': stat.S_IMODE(path.stat(follow_symlinks=False).st_mode), 'files': entries,
                'untracked': sorted(untracked)}, raw_index, content

    def scan_checkout(self, path):
        entries = {}
        content = {}
        require_supported_metadata(path)
        for directory, folders, names in os.walk(path, followlinks=False, onerror=raise_walk_error):
            base = Path(directory)
            for name in [*folders, *names]:
                item = base / name
                if item == path / '.git':
                    if name in folders:
                        folders.remove(name)
                    continue
                if name.casefold() == '.git':
                    raise SyncwheelError('desk contains a nested Git checkout')
                relative = item.relative_to(path).as_posix()
                require_supported_metadata(item)
                observed = item.lstat()
                mode = stat.S_IMODE(observed.st_mode)
                if stat.S_ISDIR(observed.st_mode):
                    entries[relative] = {'type': 'directory', 'mode': mode}
                    continue
                if stat.S_ISLNK(observed.st_mode):
                    payload = os.fsencode(os.readlink(item))
                    kind = 'symlink'
                    if name in folders:
                        folders.remove(name)
                elif stat.S_ISREG(observed.st_mode):
                    payload, mode, _ = read_regular(item)
                    kind = 'file'
                else:
                    raise SyncwheelError(f'desk contains unsupported special data: {relative}')
                entries[relative] = {'type': kind, 'mode': mode,
                                     'sha256': hashlib.sha256(payload).hexdigest()}
                content[relative] = payload
        return entries, content

    def make_objects(self, path, snapshot, index, content):
        def write_blob(data):
            return self.repo.binary_git(path, 'hash-object', '-w', '--stdin', input_data=data).decode().strip()

        def write_commit(tree, parent, message):
            return self.repo.binary_git(
                path, 'commit-tree', tree, '-p', parent, '-m', message, recovery_identity=True,
            ).decode().strip()

        with tempfile.TemporaryDirectory(prefix='syncwheel-desk-index-') as temporary:
            staged_index = Path(temporary) / 'staged'
            staged_index.write_bytes(index)
            staged_tree = self.repo.binary_git(path, 'write-tree', index=staged_index).decode().strip()
            staged = write_commit(staged_tree, snapshot['sourceTip'], 'Syncwheel desk staged snapshot')
            working_index = Path(temporary) / 'working'
            self.repo.binary_git(path, 'read-tree', '--empty', index=working_index)
            records = []
            for name, payload in sorted(content.items()):
                entry = snapshot['files'][name]
                mode = desk_tree_mode(entry)
                records.append(f'{mode} {write_blob(payload)}\t'.encode() + os.fsencode(name) + b'\0')
            self.repo.binary_git(path, 'update-index', '-z', '--index-info',
                                 index=working_index, input_data=b''.join(records))
            working_tree = self.repo.binary_git(path, 'write-tree', index=working_index).decode().strip()
            working = write_commit(working_tree, staged, 'Syncwheel desk working snapshot')
        return {'tip': snapshot['sourceTip'], 'index': write_blob(index),
                'staged': staged, 'working': working,
                'receipt': write_blob(json.dumps(snapshot, sort_keys=True).encode())}

    def release(self, args, manifest, manifest_path, stack, require_no_writer):
        if not args.reason.strip():
            raise SyncwheelError('desk release requires a nonempty --reason')
        if args.apply:
            if not args.confirm_writers_stopped or not args.plan_digest:
                raise SyncwheelError('desk release requires --confirm-writers-stopped and the reviewed --plan-digest')
            if len(args.plan_digest) != 64 or any(char not in '0123456789abcdef' for char in args.plan_digest):
                raise SyncwheelError('invalid desk release plan digest')
            with self.worktrees.governed_worktree_registry_lock():
                require_no_writer()
                return self.release_locked(args, manifest, manifest_path, stack, require_no_writer)
        registry, _ = self.worktrees.load_governed_worktree_registry()
        path, record = self.select(registry, manifest_path, stack, args.legacy_worktree)
        snapshot, _, _ = self.observe(path, stack)
        self.require_no_lane(registry, stack, path, Path(snapshot['admin']))
        return self.plan(args, manifest, manifest_path, stack, path, record, snapshot)

    def plan(self, args, manifest, manifest_path, stack, path, record, snapshot):
        if record and any(record[key] != snapshot[key] for key in ('admin', 'identity')):
            raise SyncwheelError('registered desk identity changed')
        plan = {'status': 'planned', 'stack': stack['id'], 'path': str(path), 'sourceTip': snapshot['sourceTip'],
                'sourceRef': f"refs/heads/{stack['branch']}", 'reason': args.reason,
                'manifest': str(manifest_path.resolve()), 'manifestDigest': manifest_digest(manifest),
                'legacy': args.legacy_worktree is not None, 'desk': record, 'snapshot': snapshot}
        digest = canonical_json_digest(plan)
        prefix = f"refs/syncwheel/desk-recovery/{safe_ref_segment(stack['id'])}/{digest}"
        return {**plan, 'planDigest': digest,
                'recoveryRefs': {kind: f'{prefix}/{kind}' for kind in ('tip', 'index', 'staged', 'working', 'receipt')}}

    def intent_directory(self):
        parent = self.worktrees.governed_worktree_registry_path().parent
        directory = parent / 'desk-release'
        for path in (parent, directory):
            if os.path.lexists(path) and not stat.S_ISDIR(path.lstat().st_mode):
                raise SyncwheelError(f'desk release intent directory is redirected: {path}')
        return directory

    def pending_release(self, manifest_path, stack):
        pending = []
        for path in sorted(self.intent_directory().glob('*.json')):
            intent = read_intent(path)
            plan = intent['plan']
            if (not intent.get('released') and plan['manifest'] == str(manifest_path.resolve())
                    and plan['stack'] == stack['id'] and plan['sourceRef'] == f"refs/heads/{stack['branch']}"):
                pending.append(plan)
        if len(pending) > 1:
            raise SyncwheelError('multiple pending desk release plans; inspect their recovery receipts')
        return pending[0] if pending else None

    def check_identity(self, path, expected, *, directory):
        if path.parent.resolve() != path.parent:
            raise SyncwheelError(f'desk identity has a symbolic parent; kept {path}')
        if not os.path.lexists(path):
            return False
        value = path.lstat()
        if stat.S_ISLNK(value.st_mode):
            raise SyncwheelError(f'desk identity was replaced by a symbolic link; kept {path}')
        if (directory and not stat.S_ISDIR(value.st_mode)) or [value.st_dev, value.st_ino] != expected:
            raise SyncwheelError(f'desk identity was replaced; kept {path}')
        return True

    def release_locked(self, args, manifest, manifest_path, stack, require_no_writer):
        registry, _ = self.worktrees.load_governed_worktree_registry()
        persist = self.worktrees.governed_worktree_registry_cas_persister(registry)
        intent_path = self.intent_directory() / f'{args.plan_digest}.json'
        pending = self.pending_release(manifest_path, stack)
        if pending and pending['planDigest'] != args.plan_digest:
            raise SyncwheelError('another desk release plan is pending; retry that exact plan')
        if os.path.lexists(intent_path):
            intent = read_intent(intent_path)
            plan, objects = intent['plan'], intent['objects']
            if (plan['stack'] != stack['id'] or plan['reason'] != args.reason
                    or plan['manifest'] != str(manifest_path.resolve())
                    or plan['manifestDigest'] != manifest_digest(manifest)
                    or (args.legacy_worktree is not None) != plan['legacy']
                    or (args.legacy_worktree and str(Path(args.legacy_worktree).expanduser().absolute()) != plan['path'])):
                raise SyncwheelError('desk release retry must match the prepared request')
            path = Path(plan['path'])
            self.require_no_lane(registry, stack, path, Path(plan['snapshot']['admin']))
            if intent.get('released'):
                self.verify_refs(plan, objects)
                self.verify_objects(plan, objects)
                return {**plan, 'status': 'released'}
        else:
            path, record = self.select(registry, manifest_path, stack, args.legacy_worktree)
            snapshot, index, content = self.observe(path, stack)
            self.require_no_lane(registry, stack, path, Path(snapshot['admin']))
            plan = self.plan(args, manifest, manifest_path, stack, path, record, snapshot)
            if args.plan_digest != plan['planDigest']:
                raise SyncwheelError('desk release plan is stale; review a new preview')
            if Path(plan['snapshot']['admin']).is_relative_to(path):
                raise SyncwheelError('desk Git administration is inside the checkout')
            objects = self.make_objects(path, snapshot, index, content)
            self.verify_objects(plan, objects)
            if self.observe(path, stack)[0] != snapshot:
                raise SyncwheelError('desk changed while creating recovery snapshots')
            intent = {'plan': plan, 'objects': objects}
            self.repo.write_desk_intent(intent_path, intent)
        quarantine = path.with_name(f'.syncwheel-desk-release-{plan["planDigest"][:16]}')
        admin = Path(plan['snapshot']['admin'])
        snapshot = plan['snapshot']
        relocated = os.path.lexists(quarantine)
        current = quarantine if relocated else path
        has_admin = self.check_identity(admin, snapshot['identity'], directory=True)
        removing = intent.get('removing') is True
        if removing and not relocated and os.path.lexists(path):
            # The original path no longer belongs to this operation. Never inspect
            # or remove a checkout that appeared there after the move.
            print(f'warning: original desk path has a replacement; kept {path}', file=sys.stderr)
            current = quarantine
        has_checkout = self.check_identity(current, snapshot['checkoutIdentity'], directory=True)
        if removing:
            self.verify_refs(plan, objects)
            self.verify_objects(plan, objects)
        if has_checkout and has_admin:
            if relocated and os.path.lexists(path):
                raise SyncwheelError(f'original desk path was replaced; kept it and the relocated desk at {quarantine}')
            observed, index, content = self.observe(current, stack)
            if observed != snapshot:
                raise SyncwheelError(f'desk changed after preview; retained at {current}')
            if not removing and any(self.repo.git(
                self.repo.root, 'cat-file', '-e', oid, check=False,
            ).returncode for oid in objects.values()):
                recovered = self.make_objects(current, snapshot, index, content)
                if recovered != objects:
                    raise SyncwheelError('regenerated desk objects differ from the reviewed recovery intent')
                self.verify_objects(plan, objects)
                if self.observe(current, stack)[0] != snapshot:
                    raise SyncwheelError(f'desk changed during recovery object regeneration; retained at {current}')
            commands = [f"verify {plan['sourceRef']} {plan['sourceTip']}"]
            for kind, ref in plan['recoveryRefs'].items():
                oid = objects[kind]
                existing = self.repo.git(self.repo.root, 'show-ref', '--verify', '--hash', ref, check=False).stdout.strip()
                commands.append(f'verify {ref} {oid}' if existing else f'create {ref} {oid}')
            self.repo.publish_desk_recovery_refs(commands)
            self.verify_refs(plan, objects)
            self.verify_objects(plan, objects)
            require_no_writer()
            if self.observe(current, stack)[0] != snapshot:
                raise SyncwheelError(f'desk changed after recovery refs were published; retained at {current}')
            if current == path:
                self.repo.git(self.repo.root, 'worktree', 'move', str(path), str(quarantine))
            if os.path.lexists(path):
                raise SyncwheelError(f'original desk path was replaced; kept it and the relocated desk at {quarantine}')
            self.check_identity(quarantine, snapshot['checkoutIdentity'], directory=True)
            if self.observe(quarantine, stack)[0] != snapshot:
                raise SyncwheelError(f'desk changed after relocation; retained at {quarantine}')
            self.repo.write_desk_intent(intent_path, {**intent, 'removing': True})
            self.verify_refs(plan, objects)
            self.repo.git(self.repo.root, 'worktree', 'remove', '--force', str(quarantine))
        elif removing and has_admin:
            registrations = [record for record in self.repo.get_worktrees(self.repo.root)
                             if record.get('path') and Path(record['path']) == quarantine]
            if (len(registrations) != 1 or registrations[0].get('locked')
                    or registrations[0].get('branch') != stack['branch']
                    or self.worktrees.governed_worktree_admin_dir_for_path(quarantine) != admin):
                raise SyncwheelError('desk administrative residue changed; kept it for inspection')
            self.repo.git(self.repo.root, 'worktree', 'remove', '--force', str(quarantine))
        elif removing and has_checkout and relocated:
            remaining, _ = self.scan_checkout(quarantine)
            if any(snapshot['files'].get(name) != entry for name, entry in remaining.items()):
                raise SyncwheelError(f'partial desk removal found new data; kept {quarantine}')
            shutil.rmtree(quarantine)
        elif not removing:
            raise SyncwheelError('desk release registration or checkout changed before removal; recovery files retained')
        self.verify_refs(plan, objects)
        if os.path.lexists(quarantine) or os.path.lexists(admin):
            raise SyncwheelError('desk removal is incomplete; retry the same reviewed plan')
        record = plan['desk']
        if record and record in registry.get('desks', []):
            registry['desks'].remove(record)
            persist()
        self.repo.write_desk_intent(intent_path, {'plan': plan, 'objects': objects, 'released': True})
        return {**plan, 'status': 'released'}

    def verify_refs(self, plan, objects):
        root = self.repo.root
        for kind, ref in plan['recoveryRefs'].items():
            if self.repo.git(root, 'symbolic-ref', '--quiet', ref, check=False).returncode == 0:
                raise SyncwheelError(f'desk recovery proof became symbolic: {ref}')
            value = self.repo.git(root, 'show-ref', '--verify', '--hash', ref).stdout.strip()
            if value != objects[kind]:
                raise SyncwheelError(f'desk recovery proof changed: {ref}')


    def verify_objects(self, plan, objects):
        root = self.repo.root
        snapshot = plan['snapshot']
        read = self.repo.binary_git
        receipt = read(root, 'cat-file', 'blob', objects['receipt'])
        index = read(root, 'cat-file', 'blob', objects['index'])
        if (objects['tip'] != snapshot['sourceTip'] or json.loads(receipt) != snapshot
                or hashlib.sha256(index).hexdigest() != snapshot['indexSha256']):
            raise SyncwheelError('desk recovery receipt or raw index failed verification')
        for kind, parent in (('staged', objects['tip']), ('working', objects['staged'])):
            ancestry = read(root, 'rev-list', '--parents', '-n', '1', objects[kind]).decode().split()
            if ancestry != [objects[kind], parent]:
                raise SyncwheelError(f'desk recovery commit ancestry changed: {kind}')
        with tempfile.TemporaryDirectory(prefix='syncwheel-desk-proof-') as temporary:
            index_path = Path(temporary) / 'index'
            index_path.write_bytes(index)
            rebuilt = read(root, 'write-tree', index=index_path).decode().strip()
        staged_tree = read(root, 'rev-parse', f"{objects['staged']}^{{tree}}").decode().strip()
        if staged_tree != rebuilt:
            raise SyncwheelError('desk staged tree does not match the recovered index')
        entries = {}
        working_tree = read(root, 'ls-tree', '-r', '-z', objects['working'])
        for row in filter(None, working_tree.split(b'\0')):
            info, name = row.split(b'\t', 1)
            mode, kind, oid = info.decode().split()
            if kind != 'blob':
                raise SyncwheelError('desk working proof includes unsupported object types')
            digest = hashlib.sha256(read(root, 'cat-file', 'blob', oid)).hexdigest()
            entries[os.fsdecode(name)] = (mode, digest)
        expected = {name: (desk_tree_mode(entry), entry['sha256'])
                    for name, entry in snapshot['files'].items() if entry['type'] != 'directory'}
        if entries != expected or not set(snapshot['untracked']).issubset(entries):
            raise SyncwheelError('desk working tree does not reproduce the reviewed snapshot')


def desk_tree_mode(entry):
    if entry['type'] == 'symlink':
        return '120000'
    return '100755' if entry['mode'] & 0o100 else '100644'


def read_intent(path):
    try:
        raw, _, _ = read_regular(path)
        intent = json.loads(raw)
        plan = intent['plan']
        core = {key: value for key, value in plan.items() if key not in {'planDigest', 'recoveryRefs'}}
        digest = canonical_json_digest(core)
        kinds = ('tip', 'index', 'staged', 'working', 'receipt')
        prefix = f"refs/syncwheel/desk-recovery/{safe_ref_segment(plan['stack'])}/{digest}"
        if (plan['planDigest'] != digest or path.stem != digest
                or plan['recoveryRefs'] != {kind: f'{prefix}/{kind}' for kind in kinds}
                or set(intent['objects']) != set(kinds)
                or any(not isinstance(oid, str) or len(oid) not in (40, 64)
                       or any(ch not in '0123456789abcdef' for ch in oid)
                       for oid in intent['objects'].values())):
            raise ValueError('intent digest or object identifiers changed')
        return intent
    except (OSError, KeyError, ValueError, TypeError) as error:
        raise SyncwheelError(f'invalid desk release intent at {path}: {error}') from error


def require_supported_metadata(path):
    if hasattr(os, 'listxattr') and os.listxattr(path, follow_symlinks=False):
        raise SyncwheelError(f'desk contains extended attributes that cannot be preserved: {path}')


def raise_walk_error(error):
    raise error


def file_identity(path):
    value = path.stat(follow_symlinks=False)
    return value.st_dev, value.st_ino


def read_regular(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SyncwheelError(f'desk snapshot requires a regular file: {path}')
        payload = handle.read()
        after = os.fstat(handle.fileno())
        if (before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (
            after.st_mtime_ns, after.st_ctime_ns, after.st_size,
        ):
            raise SyncwheelError(f'desk file changed during snapshot: {path}')
        return payload, stat.S_IMODE(before.st_mode), (before.st_dev, before.st_ino)
