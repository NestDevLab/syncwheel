"""Compensate refused guarded recipe commits without discarding peer writes."""

import os
from pathlib import Path
import stat
import tempfile

from syncwheel_errors import SyncwheelError
from syncwheel_files import fsync_directory_path
from syncwheel_index import parse_index_semantics


def _image(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise SyncwheelError(f'guarded metadata requires a regular file: {path}')
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


class GuardedMetadataRollback:
    def __init__(self, root, manifest_path, git):
        self.root = root
        self.git = git
        self.head = git(root, 'rev-parse', 'HEAD').stdout.strip()
        self.object_format = git(root, 'rev-parse', '--show-object-format').stdout.strip()
        self.before = {path: _image(path) for path in (manifest_path, root / '.gitignore')}
        self.after = {}
        self.index_before = {}
        self.index_after = {}
        self.index_image_before = None
        self.index_image_after = None

    def written(self, path, payload=None):
        observed = _image(path)
        # A writer racing the save is never adopted as our rollback preimage.
        mode = self.before[path][1] if self.before[path] is not None else observed[1]
        self.after[path] = (payload, mode) if payload is not None else observed

    def _entries(self, paths, env=None):
        return {path: self.git(self.root, 'ls-files', '--stage', '--', path, env=env).stdout
                for path in paths}

    def _index_path(self):
        raw = self.git(self.root, 'rev-parse', '--git-path', 'index').stdout.strip()
        path = Path(raw)
        return path if path.is_absolute() else self.root / path

    def _snapshot_entries(self, paths, image):
        with tempfile.NamedTemporaryFile(prefix='syncwheel-observe-index-') as stream:
            stream.write(image[0])
            stream.flush()
            return self._entries(paths, env={'GIT_INDEX_FILE': stream.name})

    def _semantics(self, image):
        return parse_index_semantics(image[0], self.object_format)

    def observe_index(self, paths):
        self.index_image_before = _image(self._index_path())
        self.index_before = self._snapshot_entries(paths, self.index_image_before)

    def observe_staged_index(self, paths):
        self.index_image_after = _image(self._index_path())
        observed = self._snapshot_entries(paths, self.index_image_after)
        self.index_after = dict(observed)
        before = self._semantics(self.index_image_before)
        after = self._semantics(self.index_image_after)
        self.before_semantics = before
        self.staged_semantics = after
        owned = {os.fsencode(path) for path in paths}
        # Do not adopt an unrelated edit that raced git add and observation.
        self.exclusive_index_delta = bool(before and after and
            [entry for entry in before['entries'] if entry[0] not in owned] ==
            [entry for entry in after['entries'] if entry[0] not in owned] and
            before['extensions'] == after['extensions'])
        for relative in paths:
            path = self.root / relative
            payload, mode = self.after[path]
            blob = self.git(self.root, 'hash-object', '--stdin', input_text=payload.decode()).stdout.strip()
            expected = f"{100755 if mode & 0o111 else 100644} {blob} 0\t{relative}\n"
            self.index_after[relative] = expected
            if observed[relative] != expected:
                # A peer staged this path before observation; do not adopt it.
                self.index_image_after = None

    def _restore_index(self):
        if not self.index_after:
            return []
        index = self._index_path()
        lock = Path(str(index) + '.lock')
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise SyncwheelError('index is busy; guarded rollback preserved it for inspection') from exc
        temporary = None
        try:
            os.close(descriptor)
            image = _image(index)
            descriptor, name = tempfile.mkstemp(prefix='syncwheel-rollback-index-', dir=index.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(self.index_image_before[0] if image == self.index_image_after and self.exclusive_index_delta else image[0])
            if image == self.index_image_after and self.exclusive_index_delta:
                os.chmod(temporary, self.index_image_before[1])
                with temporary.open('rb') as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, index)
                fsync_directory_path(index.parent)
                return []
            environment = {'GIT_INDEX_FILE': str(temporary)}
            current = self._entries(self.index_after, env=environment)
            peers = []
            current_semantics = self._semantics(image)
            for path, expected in self.index_after.items():
                owned = os.fsencode(path)
                entries = lambda parsed: [entry for entry in parsed['entries'] if entry[0] == owned] if parsed else None
                original = entries(self.before_semantics)
                flags_changed = (entries(current_semantics) != entries(self.staged_semantics)
                                 or original is None or any(entry[4] or entry[5] for entry in original))
                if current[path] != expected or flags_changed:
                    peers.append('index:' + path)
                    continue
                # Remove any stages, then put back the exact original entry.
                object_id = self.git(self.root, 'hash-object', '--stdin', input_text='').stdout.strip()
                payload = f"0 {'0' * len(object_id)}\t{path}\n" + self.index_before[path]
                self.git(self.root, 'update-index', '--index-info', input_text=payload, env=environment)
            if _image(index) != image:
                raise SyncwheelError('index changed outside its lock; guarded rollback preserved it')
            os.chmod(temporary, image[1])
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, index)
            fsync_directory_path(index.parent)
            return peers
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
                Path(str(temporary) + '.lock').unlink(missing_ok=True)
            lock.unlink(missing_ok=True)

    def restore(self):
        if self.git(self.root, 'rev-parse', 'HEAD').stdout.strip() != self.head:
            return 'HEAD changed; metadata preserved for inspection (no rollback applied)'
        peers = self._restore_index()
        for path, expected in self.after.items():
            if _image(path) != expected:
                peers.append('file:' + path.relative_to(self.root).as_posix())
                continue
            before = self.before[path]
            if before is None:
                path.unlink()
                fsync_directory_path(path.parent)
                continue
            descriptor, name = tempfile.mkstemp(prefix=f'.{path.name}.rollback-', dir=path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(before[0])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, before[1])
                if _image(path) != expected:
                    peers.append('file:' + path.relative_to(self.root).as_posix())
                    continue
                os.replace(temporary, path)
                fsync_directory_path(path.parent)
            finally:
                temporary.unlink(missing_ok=True)
        if peers:
            return 'guarded metadata rollback preserved concurrent edits; review before retry: ' + ', '.join(peers)
        return 'guarded metadata restored; retry with the same reviewed source and manifest'
