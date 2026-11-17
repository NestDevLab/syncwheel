"""Proof and recoverable cleanup of a governed lane's already-declared copy."""

import contextlib
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import tarfile
import tempfile

from syncwheel_errors import SyncwheelError
from syncwheel_files import fsync_directory_path


def retire_copy_checkpoint(stage):
    """Fault injection seam for durable phase boundaries."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def require(condition, detail):
    if not condition:
        raise SyncwheelError(detail)


def stat_key(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid]


def directory_key(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode), f'expected an original directory: {path}')
    return stat_key(info)


def observed_file(path, *, allow_link=False):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None, None
    require(not set_metadata(path), f'unsupported extended attributes: {path}')
    require(allow_link or before.st_nlink == 1, f'unsupported hardlinked file: {path}')
    if stat.S_ISLNK(before.st_mode):
        payload = os.fsencode(os.readlink(path))
    else:
        require(stat.S_ISREG(before.st_mode), f'unsupported filesystem entry: {path}')
        require(not before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX),
                f'unsupported special file mode: {path}')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as file:
            require(os.fstat(file.fileno()) == before, f'file changed while opening: {path}')
            payload = file.read()
    after = path.lstat()
    require(stat_key(before) == stat_key(after)
            and before.st_mtime_ns == after.st_mtime_ns
            and before.st_ctime_ns == after.st_ctime_ns
            and before.st_size == after.st_size
            and before.st_nlink == after.st_nlink, f'file changed while reading: {path}')
    return {'stat': stat_key(before), 'mtime': before.st_mtime_ns, 'ctime': before.st_ctime_ns,
            'size': before.st_size, 'sha256': digest(payload)}, payload


def set_metadata(path):
    return os.listxattr(path, follow_symlinks=False) if hasattr(os, 'listxattr') else ()


def moved_key(entry):
    if entry is None:
        return None
    # A rename changes ctime, while a hardlink used for restoration also changes ctime.
    return {key: value for key, value in entry.items() if key != 'ctime'}


def path_names(payload):
    return {os.fsdecode(item) for item in payload.split(b'\0') if item}


def safe_name(name):
    parts = name.split('/')
    require(all(part and part not in ('.', '..') and part.casefold() != '.git'
                for part in parts), f'unsupported Git path: {name!r}')
    return parts


@contextlib.contextmanager
def checked_parent(root, name, keys=None):
    parts = safe_name(name)
    dirs = [root]
    for part in parts[:-1]:
        dirs.append(dirs[-1] / part)
    observed = [directory_key(path) for path in dirs]
    if keys is not None:
        require(observed == keys, f'parent directory identity changed: {name!r}')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        require(stat_key(os.fstat(fd)) == observed[0], f'checkout identity changed: {root}')
        for part, key in zip(parts[:-1], observed[1:]):
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            require(stat_key(os.fstat(fd)) == key, f'parent directory identity changed: {name!r}')
        require(not set_metadata(Path(f'/proc/self/fd/{fd}/.')),
                f'unsupported extended attributes on parent of {name!r}')
        yield fd, parts[-1], observed
    finally:
        os.close(fd)


def install_bytes(path, data, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as file:
        file.write(data)
        file.flush()
        os.fchmod(file.fileno(), mode)
        os.fsync(file.fileno())


def rename_new(source_fd, source, target_fd, target):
    """Move an original without replacing even a concurrently created destination."""
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    require(renameat2 is not None, 'retire-copy needs filesystem support for no-replace rename')
    result = renameat2(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1)
    if result:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), target)


def remedy(lane_id, *, commit=None, reason=None, plan_digest=None, selection=()):
    command = ['syncwheel', 'worktree', 'retire-copy', lane_id, *selection]
    if commit:
        command.extend(('--commit', commit))
    command.extend(('--reason', reason or 'retire the already-adopted copy'))
    if plan_digest:
        command.extend(('--apply', '--plan-digest', plan_digest, '--confirm-writers-stopped'))
    return shlex.join(command)


class RetireCopy:
    def __init__(self, *, env, common, lane_id, reason, requested_commit, selection, identity,
                 lock_metadata, stale_lock_reason):
        self.env = {**env, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_LAZY_FETCH': '1',
                    'GIT_ALLOW_PROTOCOL': '', 'GIT_NO_REPLACE_OBJECTS': '1'}
        self.env.pop('GIT_ATTR_SOURCE', None)
        self.common = common
        self.lane_id = lane_id
        self.reason = reason
        self.requested_commit = requested_commit
        self.selection = selection
        self.identity = identity
        self.lock_metadata = lock_metadata
        self.stale_lock_reason = stale_lock_reason
        self.store = common / 'syncwheel' / 'retire-copy'

    def git(self, path, *argv, data=None, index=None, missing=False):
        env = self.env.copy()
        options = ['-c', 'core.fsmonitor=false', '-c', 'core.splitIndex=false']
        if index is not None:
            env['GIT_INDEX_FILE'] = str(index)
            # Git can run post-index-change even for a temporary index built
            # solely to verify the recovery artifacts. Keep that private.
            options.extend(('-c', 'core.hooksPath=/dev/null'))
        result = subprocess.run(
            ['git', *options, *argv],
            cwd=path, env=env, input=data, capture_output=True,
        )
        if missing and result.returncode == 1:
            return b''
        require(result.returncode == 0, result.stderr.decode(errors='replace').strip()
                or f'Git {argv[0]} failed (exit {result.returncode})')
        return result.stdout

    def head_map(self, path, commit):
        result = {}
        for row in self.git(path, 'ls-tree', '-rz', commit).split(b'\0'):
            if row:
                header, name = row.split(b'\t', 1)
                mode, kind, oid = header.decode().split()
                require(kind == 'blob' and mode in {'100644', '100755', '120000'},
                        'retire-copy refuses submodules or unsupported tree entries')
                name = os.fsdecode(name)
                require(name not in result, f'duplicate Git path: {name!r}')
                result[name] = [mode, oid]
        return result

    def index_map(self, path):
        result = {}
        for row in self.git(path, 'ls-files', '--stage', '-z').split(b'\0'):
            if row:
                header, name = row.split(b'\t', 1)
                mode, oid, stage = header.decode().split()
                require(stage == '0', 'retire-copy refuses a conflicted index')
                require(mode in {'100644', '100755', '120000'},
                        'retire-copy refuses submodules or unsupported index entries')
                name = os.fsdecode(name)
                require(name not in result, f'duplicate Git path: {name!r}')
                result[name] = [mode, oid]
        data = self.git(path, 'ls-files', '--debug', '-z')
        while data:
            _, _, rest = data.partition(b'\0')
            parsed = re.match(rb'  ctime:[^\n]*\n  mtime:[^\n]*\n  dev:[^\n]*\n  uid:[^\n]*\n  size:[^\n]*\tflags: ([0-9a-f]+)\n', rest)
            require(parsed is not None and int(parsed[1], 16) == 0,
                    'retire-copy refuses sparse, assume-unchanged, or intent-to-add index entries')
            data = rest[parsed.end():]
        return result

    def verify_layout(self, path, admin, own_lock=None):
        require(Path('/proc/self/fd').is_dir()
                and getattr(ctypes.CDLL(None), 'renameat2', None) is not None,
                'retire-copy requires a platform with descriptor-relative no-replace file moves')
        require(not set_metadata(path) and not set_metadata(admin),
                'unsupported extended attributes on lane or Git administration')
        for marker in ('HEAD.lock', 'MERGE_HEAD', 'MERGE_MSG', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                       'rebase-apply', 'rebase-merge', 'sequencer', 'BISECT_LOG', 'AUTO_MERGE'):
            require(not os.path.lexists(admin / marker), f'interrupted Git operation ({marker})')
        lock = admin / 'index.lock'
        if os.path.lexists(lock):
            require(own_lock and same_inode(lock, own_lock), 'Git index is locked by another writer')
        require(not self.git(path, 'rev-parse', '--shared-index-path').strip(),
                'retire-copy refuses a split index')
        for key, harmless in (('core.sparseCheckout', {b'', b'false', b'no', b'off', b'0'}),
                              ('core.autocrlf', {b'', b'false', b'no', b'off', b'0'}),
                              ('core.symlinks', {b'', b'true', b'yes', b'on', b'1'})):
            value = self.git(path, 'config', '--get', key, missing=True).strip().lower()
            require(value in harmless, f'retire-copy refuses unsupported Git configuration {key}')

        def walk_error(error):
            raise error

        for here, dirs, files in os.walk(path, followlinks=False, onerror=walk_error):
            for name in dirs + files:
                if name.casefold() == '.git':
                    require(Path(here) == path and name == '.git' and name not in dirs,
                            f'nested Git checkout: {here}')

    def verify_attributes(self, path, names):
        if not names:
            return
        attributes = self.git(
            path, 'check-attr', '--all', '-z', '--stdin',
            data=b''.join(os.fsencode(name) + b'\0' for name in sorted(names)),
        ).split(b'\0')[:-1]
        require(len(attributes) % 3 == 0, 'could not verify Git attributes')
        conversions = {b'filter', b'working-tree-encoding', b'text', b'eol', b'ident', b'crlf'}
        for name, attr in zip(attributes[::3], attributes[1::3]):
            require(attr not in conversions,
                    f'unsupported Git filter or conversion attribute {os.fsdecode(attr)}: {os.fsdecode(name)!r}')

    def inspect_work(self, path, names, format_name):
        entries = {}
        for name in sorted(names):
            with checked_parent(path, name) as (fd, leaf, parents):
                item = Path(f'/proc/self/fd/{fd}') / leaf
                found, payload = observed_file(item)
            git_entry = None
            if found is not None:
                oid = hashlib.new(format_name, f'blob {len(payload)}\0'.encode() + payload).hexdigest()
                git_entry = [git_mode(found['stat'][2]), oid]
            entries[name] = (git_entry, found, parents)
        return entries

    def snapshot(self, bound, *, own_lock=None, skip=()):
        path, admin = Path(bound['path']), Path(bound['admin'])
        self.verify_layout(path, admin, own_lock)
        index_before, raw_index = observed_file(admin / 'index', allow_link=own_lock is not None)
        require(index_before is not None, 'Git index is missing')
        indexed, head = self.index_map(path), self.head_map(path, bound['head'])
        untracked = path_names(self.git(path, 'ls-files', '--others', '--exclude-standard', '-z'))
        require(not self.git(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z'),
                'retire-copy refuses ignored files; preserve them before retiring the lane copy')
        names = set(indexed) | set(head) | untracked
        for name in names:
            safe_name(name)
        self.verify_attributes(path, names)
        format_name = self.git(path, 'rev-parse', '--show-object-format').decode().strip()
        work = self.inspect_work(path, names - set(skip), format_name)
        require(observed_file(admin / 'index', allow_link=own_lock is not None)[0] == index_before,
                'Git index changed during observation')
        return head, indexed, untracked, work, index_before, raw_index

    def plan(self):
        bound = self.identity()
        head, indexed, untracked, work, index_before, _ = self.snapshot(bound)
        evidence = {}
        for name, (entry, snapshot, parents) in work.items():
            original, staged = head.get(name), indexed.get(name)
            if original != staged or staged != entry:
                require(name.rsplit('/', 1)[-1] != '.gitattributes',
                        'retire-copy refuses to change Git attribute files')
                frozen = self.git(Path(bound['path']), 'cat-file', 'blob', original[1]) if original else None
                evidence[name] = {'head': original, 'index': staged, 'work': entry,
                                  'headSha256': digest(frozen) if frozen is not None else None,
                                  'original': snapshot, 'parents': parents,
                                  'untracked': name in untracked}
        require(evidence, 'the governed lane has no dirty copy to retire')
        requested = bound['requestedCommit']
        candidates = [requested] if requested else bound['declaredHistory']
        matches = []
        for commit in dict.fromkeys(candidates):
            comparison = self.head_map(Path(bound['path']), commit)
            if all((info['head'] == info['index'] or info['index'] == comparison.get(name))
                   and (info['index'] == info['work'] or info['work'] == comparison.get(name))
                   for name, info in evidence.items()):
                matches.append(commit)
        require(matches, 'no single declared commit matches every dirty index and working-tree layer')
        require(len(matches) == 1,
                'multiple declared commits match this copy; preview again with --commit REV')
        require(self.identity() == bound, 'lane identity or selected manifest changed during preview')
        plan = {'schemaVersion': 1, 'reason': self.reason, 'bound': bound,
                'chosenCommit': matches[0], 'indexBefore': index_before, 'pathEvidence': evidence}
        return plan, digest(encode(plan))

    def prepare(self, plan, token, destination):
        self.private_store(create=True)
        path, admin = Path(plan['bound']['path']), Path(plan['bound']['admin'])
        parents = plan['pathEvidence']
        with tempfile.TemporaryDirectory(prefix='.prepared-', dir=self.store) as tmp:
            stage = Path(tmp)
            # The final directory is moved as one unit only after every artifact is verified.
            recovery = stage / 'recovery'
            recovery.mkdir(mode=0o700)
            require(recovery.stat().st_dev == path.stat().st_dev == admin.stat().st_dev
                    and all(item['parents'][-1][0] == recovery.stat().st_dev for item in parents.values()),
                    'retire-copy needs same-filesystem recovery, worktree, and Git index paths')
            metadata, raw = observed_file(admin / 'index')
            require(metadata == plan['indexBefore'], 'Git index changed before recovery')
            install_bytes(recovery / 'index', raw)
            retire_copy_checkpoint('recovery-prepared')
            require((recovery / 'index').read_bytes() == raw, 'original index recovery bytes did not verify')
            staged = stage / 'staged'
            staged.write_bytes(raw)
            staged_tree = self.git(path, 'write-tree', index=staged).decode().strip()
            work_index = stage / 'working'
            work_index.write_bytes(raw)

            records = []
            prepared = {}
            files = {}
            tar = io.BytesIO()
            with tarfile.open(fileobj=tar, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for number, (name, info) in enumerate(sorted(parents.items())):
                    with checked_parent(path, name, info['parents']) as (fd, leaf, _):
                        current, payload = observed_file(Path(f'/proc/self/fd/{fd}') / leaf)
                    require(current == info['original'], f'worktree path changed before recovery: {name!r}')
                    if info['untracked']:
                        member = tarfile.TarInfo(name)
                        member.mode = stat.S_IMODE(current['stat'][2])
                        member.uid, member.gid = current['stat'][3:]
                        member.mtime = current['mtime'] // 1_000_000_000
                        if info['work'][0] == '120000':
                            member.type = tarfile.SYMTYPE
                            member.linkname = os.fsdecode(payload)
                            archive.addfile(member)
                        else:
                            member.size = len(payload)
                            archive.addfile(member, io.BytesIO(payload))
                        files[name] = (current, payload)
                    elif info['index'] != info['work']:
                        if info['work'] is None:
                            zero = '0' * len(plan['bound']['head'])
                            spec = f'0 {zero}'
                        else:
                            mode, oid = info['work']
                            actual = self.git(path, 'hash-object', '-w', '--stdin', data=payload).decode().strip()
                            require(actual == oid, f'worktree blob changed before recovery: {name!r}')
                            spec = f'{mode} {oid}'
                        records.append(spec.encode() + b'\t' + os.fsencode(name) + b'\0')
                    if info['head']:
                        mode, oid = info['head']
                        clean = self.git(path, 'cat-file', 'blob', oid)
                        require(digest(clean) == info['headSha256'], f'frozen HEAD file changed: {name!r}')
                        output = recovery / f'clean-{number}'
                        if mode == '120000':
                            require(clean and b'\0' not in clean, f'unsupported symbolic link target: {name!r}')
                            output.symlink_to(os.fsdecode(clean))
                        else:
                            install_bytes(output, clean, 0o755 if mode == '100755' else 0o644)
                        prepared[name] = moved_key(observed_file(output)[0])
            if records:
                self.git(path, 'update-index', '-z', '--index-info', data=b''.join(records), index=work_index)
            working_tree = self.git(path, 'write-tree', index=work_index).decode().strip()
            anchor = plan['bound']['head']
            checksums = {}
            for label, base, target in (('staged', anchor, staged_tree),
                                        ('unstaged', staged_tree, working_tree)):
                patch = self.git(path, 'diff', '--binary', '--full-index', '--no-ext-diff',
                                 '--no-textconv', '--no-renames', base, target, '--')
                output = recovery / f'{label}.patch'
                install_bytes(output, patch)
                verified = stage / f'verify-{label}'
                self.git(path, 'read-tree', base, index=verified)
                if patch:
                    self.git(path, 'apply', '--cached', '--binary', '--whitespace=nowarn',
                             '-', data=output.read_bytes(), index=verified)
                require(self.git(path, 'write-tree', index=verified).decode().strip() == target,
                        f'{label} recovery patch did not reproduce the original tree')
                checksums[output.name] = digest(patch)
            archive_path = recovery / 'untracked.tar'
            install_bytes(archive_path, tar.getvalue())
            with tarfile.open(archive_path, 'r') as archive:
                require(set(archive.getnames()) == set(files) and len(archive.getnames()) == len(files),
                        'untracked archive member list did not verify')
                for member in archive:
                    metadata, expected = files[member.name]
                    stream = archive.extractfile(member) if member.isfile() else None
                    actual = stream.read() if stream else os.fsencode(member.linkname)
                    require((member.isfile() or member.issym())
                            and member.mode == stat.S_IMODE(metadata['stat'][2])
                            and [member.uid, member.gid] == metadata['stat'][3:]
                            and actual == expected, f'untracked recovery entry did not verify: {member.name!r}')
            checksums[archive_path.name] = digest(tar.getvalue())
            checksums['index'] = digest(raw)
            clean_index = stage / 'index.clean'
            self.git(path, 'read-tree', anchor, index=clean_index)
            require(self.git(path, 'write-tree', index=clean_index) == self.git(
                path, 'rev-parse', f'{anchor}^{{tree}}'), 'clean index does not reproduce the frozen HEAD')
            install_bytes(recovery / 'index.clean', clean_index.read_bytes(),
                          stat.S_IMODE(plan['indexBefore']['stat'][2]))
            clean_fingerprint, _ = observed_file(recovery / 'index.clean')
            intent = {'plan': plan, 'planDigest': token, 'cleanIndex': moved_key(clean_fingerprint),
                      'checksums': checksums, 'prepared': prepared}
            install_bytes(recovery / 'receipt.json', encode(intent))
            fsync_directory_path(recovery)
            fsync_directory_path(stage)
            with opened_directory(stage) as source, opened_directory(self.store) as target:
                rename_new(source, recovery.name, target, destination.name)
                os.fsync(source)
                os.fsync(target)
        self.pin_commits(path, plan, token)
        retire_copy_checkpoint('recovery-verified')

    def pin_commits(self, path, plan, token):
        for label, tip in (('head', plan['bound']['head']), ('declared', plan['chosenCommit'])):
            ref = f'refs/syncwheel/recovery/retire-copy/{token}/{label}'
            require(not self.git(path, 'symbolic-ref', '--quiet', ref, missing=True),
                    f'recovery ref is symbolic: {ref}')
            current = self.git(path, 'rev-parse', '--verify', '--quiet', ref, missing=True).strip().decode()
            if not current:
                self.git(path, '-c', 'core.fsync=reference', 'update-ref', '--no-deref',
                         ref, tip, '0' * len(tip))
            else:
                require(current == tip, f'recovery ref drift: {ref}')

    def private_store(self, *, create=False):
        for part in (self.store.parent, self.store):
            if create and not os.path.lexists(part):
                part.mkdir(mode=0o700)
                fsync_directory_path(part.parent)
            if os.path.lexists(part):
                directory_key(part)
        if self.store.exists():
            require(self.store.stat().st_mode & 0o077 == 0,
                    'retire-copy recovery directory is not private')

    def load_intent(self, token, destination):
        self.private_store()
        directory_key(destination)
        require(destination.stat().st_mode & 0o077 == 0, 'retire-copy recovery is not private')
        try:
            _, raw = observed_file(destination / 'receipt.json')
            intent = json.loads(raw)
            plan = intent['plan']
            valid = (intent['planDigest'] == token == digest(encode(plan))
                     and self.reason == plan['reason'] and self.identity() == plan['bound'])
            require(valid, 'recovery plan digest or lane identity changed; retain the recovery')
            require(set(intent['checksums']) == {'index', 'staged.patch', 'unstaged.patch', 'untracked.tar'},
                    'invalid recovery artifact list')
            for name, checksum in intent['checksums'].items():
                observed, _ = observed_file(destination / name)
                require(observed and observed['sha256'] == checksum
                        and (name != 'index' or checksum == plan['indexBefore']['sha256']),
                        f'recovery artifact changed: {name}')
            return intent
        except (KeyError, ValueError, TypeError) as exc:
            raise SyncwheelError(f'invalid retire-copy recovery intent: {exc}') from exc

    def verify_resume(self, intent, dest, own_lock):
        plan = intent['plan']
        require(self.identity() == plan['bound'], 'lane identity or selected manifest changed before cleanup')
        path = Path(plan['bound']['path'])
        paths = plan['pathEvidence']
        head, indexed, _, work, index_info, _ = self.snapshot(plan['bound'], own_lock=own_lock, skip=paths)
        clean = moved_key(index_info) == intent['cleanIndex']
        require(clean or index_info == plan['indexBefore'], 'Git index changed since the reviewed plan')
        for name, (entry, _, _) in work.items():
            require(head.get(name) == indexed.get(name) == entry,
                    f'new dirty path since the reviewed plan: {name!r}')
        for number, (name, evidence) in enumerate(sorted(paths.items())):
            with checked_parent(path, name, evidence['parents']) as (fd, leaf, _):
                state = self.path_state(dest, number, name, evidence, intent, fd, leaf)
            require(not clean or state == 'clean', 'the installed index has a dirty worktree path')
        return clean

    def path_state(self, dest, number, name, evidence, intent, fd, leaf):
        clean = intent['prepared'].get(name)
        head = evidence['head']
        require((head is None and clean is None) or (
            head is not None and clean is not None
            and clean['sha256'] == evidence['headSha256']
            and head[0] == git_mode(clean['stat'][2])
        ), f'frozen HEAD proof changed: {name!r}')
        prepared, _ = observed_file(dest / f'clean-{number}')
        require(prepared is None or moved_key(prepared) == clean, f'prepared HEAD file changed: {name!r}')
        old, _ = observed_file(dest / f'original-{number}')
        present, _ = observed_file(Path(f'/proc/self/fd/{fd}') / leaf)
        original = evidence['original']
        if old is None and original is not None and present == original:
            require(prepared is not None or clean is None, f'prepared HEAD file missing: {name!r}')
            return 'original'
        require(moved_key(old) == moved_key(original),
                f'original quarantine proof changed: {name!r}')
        if clean is not None and moved_key(present) == clean and prepared is None:
            return 'clean'
        if present is None:
            require(prepared is not None or clean is None, f'prepared HEAD file missing: {name!r}')
            return 'clean' if clean is None else 'quarantined'
        raise SyncwheelError(f'new or changed file at {name!r}; recovery and replacement retained')

    def clean_paths(self, intent, dest, own_lock):
        plan = intent['plan']
        path = Path(plan['bound']['path'])
        with opened_directory(dest) as recover_fd:
            for number, (name, evidence) in enumerate(sorted(plan['pathEvidence'].items())):
                require(self.identity() == plan['bound'], 'lane identity or selected manifest changed during cleanup')
                with checked_parent(path, name, evidence['parents']) as (fd, leaf, _):
                    state = self.path_state(dest, number, name, evidence, intent, fd, leaf)
                    if state == 'original':
                        quarantine = f'original-{number}'
                        rename_new(fd, leaf, recover_fd, quarantine)
                        os.fsync(fd)
                        os.fsync(recover_fd)
                        if moved_key(observed_file(dest / quarantine)[0]) != moved_key(evidence['original']):
                            raise SyncwheelError(f'file changed while moving {name!r}; inspect both paths in recovery')
                        retire_copy_checkpoint('path-quarantined')
                    if state != 'clean' and evidence['head'] is not None:
                        # Validate the parent again after quarantine or interruption and never overwrite a newcomer.
                        with checked_parent(path, name, evidence['parents']) as (restore_fd, restore_leaf, _):
                            require(self.path_state(dest, number, name, evidence, intent, restore_fd,
                                                    restore_leaf) == 'quarantined',
                                    f'path changed before restoring HEAD: {name!r}')
                            rename_new(recover_fd, f'clean-{number}', restore_fd, restore_leaf)
                            os.fsync(restore_fd)
                            os.fsync(recover_fd)
                        retire_copy_checkpoint('path-restored')
        self.verify_resume(intent, dest, own_lock)
        for number, (name, evidence) in enumerate(sorted(plan['pathEvidence'].items())):
            with checked_parent(path, name, evidence['parents']) as (fd, leaf, _):
                require(self.path_state(dest, number, name, evidence, intent, fd, leaf) == 'clean',
                        f'path was not successfully cleaned: {name!r}')

    def take_index_lock(self, token, dest, intent, backing, lock):
        ownership = dest / 'index-owner.json'
        if os.path.lexists(lock):
            require(same_inode(backing, lock), 'Git index is locked by another writer')
            _, payload = observed_file(ownership)
            try:
                previous = json.loads(payload)
                valid = (previous['planDigest'] == token and previous['host'] == socket.gethostname()
                         and previous['indexIdentity'] == intent['cleanIndex']['stat'][:2]
                         and type(previous['pid']) is int and previous['pid'] > 0
                         and isinstance(previous['process_start_time'], str)
                         and self.stale_lock_reason(previous) in {'pid_not_alive', 'process_zombie'})
            except (KeyError, ValueError, TypeError):
                valid = False
            require(valid, 'cannot prove the interrupted index lock belongs to a stopped retire-copy writer')
        owner = self.lock_metadata()
        require(owner['process_start_time'], 'cannot prove the current index-lock writer identity')
        owner.update(planDigest=token, host=socket.gethostname(),
                     indexIdentity=intent['cleanIndex']['stat'][:2])
        with tempfile.TemporaryDirectory(prefix='.index-owner-', dir=dest) as temporary:
            updated = Path(temporary) / 'owner'
            install_bytes(updated, encode(owner))
            os.replace(updated, ownership)
            fsync_directory_path(dest)
        if not os.path.lexists(lock):
            os.link(backing, lock, follow_symlinks=False)
            fsync_directory_path(lock.parent)
        require(same_inode(backing, lock), 'Git index is locked by another writer')

    def apply(self, token):
        require(re.fullmatch(r'[0-9a-f]{64}', token or '') is not None, 'apply requires the reviewed --plan-digest')
        destination = self.store / token
        self.private_store()
        try:
            if not os.path.lexists(destination):
                plan, current = self.plan()
                require(current == token, 'retire-copy plan drift; inspect a fresh preview')
                self.prepare(plan, token, destination)
            intent = self.load_intent(token, destination)
            plan = intent['plan']
            path, admin = Path(plan['bound']['path']), Path(plan['bound']['admin'])
            self.pin_commits(path, plan, token)
            backing = destination / 'index.clean'
            lock = admin / 'index.lock'
            index = admin / 'index'
            clean_index, _ = observed_file(backing, allow_link=True)
            already_installed = self.verify_resume(intent, destination, backing)
            require((already_installed and clean_index is None)
                    or moved_key(clean_index) == intent['cleanIndex'], 'prepared clean index changed')
            if not already_installed:
                self.take_index_lock(token, destination, intent, backing, lock)
                try:
                    require(same_inode(backing, lock), 'Git index lock identity changed')
                    require(not self.verify_resume(intent, destination, backing), 'Git index changed before cleanup')
                    self.clean_paths(intent, destination, backing)
                    require(same_inode(backing, lock)
                            and observed_file(index)[0] == plan['indexBefore'],
                            'Git index or its lock changed before installation')
                    os.replace(lock, index)
                    fsync_directory_path(admin)
                    retire_copy_checkpoint('index-installed')
                finally:
                    if same_inode(backing, lock):
                        lock.unlink()
                        fsync_directory_path(admin)
            require(self.verify_resume(intent, destination, backing), 'clean index did not verify')
            if same_inode(backing, index):
                backing.unlink()
                fsync_directory_path(destination)
            return self.output(plan, token, True)
        except (OSError, SyncwheelError, tarfile.TarError) as exc:
            if not os.path.lexists(destination):
                if isinstance(exc, tarfile.TarError):
                    raise SyncwheelError(f'could not verify the untracked recovery archive: {exc}') from exc
                raise
            command = remedy(self.lane_id, commit=self.requested_commit, reason=self.reason,
                             plan_digest=token, selection=self.selection)
            raise SyncwheelError(f'{exc}; recovery retained at {destination}; inspect and resume with: {command}') from exc

    def output(self, plan, token, applied=False):
        bound = plan['bound']
        result = {'applied': applied, 'planDigest': token, 'lane': self.lane_id,
                  'chosenCommit': plan['chosenCommit'], 'head': bound['head'], 'stack': bound['stack'],
                  'stackTip': bound['stackTip'], 'pathEvidence': plan['pathEvidence'],
                  'recoveryLocation': str(self.store / token)}
        if applied and bound['head'] != bound['stackTip']:
            result['nextCommands'] = [
                shlex.join(['syncwheel', 'worktree', 'release', self.lane_id, *self.selection,
                            '--reason', self.reason]),
                shlex.join(['syncwheel', 'stack', 'push', bound['stack'], *self.selection, '--dry-run']),
            ]
        return result


def require_no_visible_lane_users(path):
    # Explicit writer confirmation covers unnamed tools and processes outside
    # our visibility; a visible process using the lane is contrary evidence.
    for process in Path('/proc').iterdir():
        if not process.name.isdecimal():
            continue
        try:
            cwd = Path(os.readlink(process / 'cwd'))
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            continue
        if cwd.is_relative_to(path):
            raise SyncwheelError(f'process {process.name} is using the lane; stop its writers first')


def git_mode(mode):
    return '120000' if stat.S_ISLNK(mode) else ('100755' if mode & stat.S_IXUSR else '100644')


@contextlib.contextmanager
def opened_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        yield fd
    finally:
        os.close(fd)


def same_inode(first, second):
    try:
        a, b = first.lstat(), second.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(a.st_mode) and stat.S_ISREG(b.st_mode) and stat_key(a)[:2] == stat_key(b)[:2]
