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
        self.index_image_before = _image(self._index_path())

    def written(self, path, payload=None):
        observed = _image(path)
        # A writer racing the save is never adopted as our rollback preimage.
        mode = self.before[path][1] if self.before[path] is not None else observed[1]
        self.after[path] = (payload, mode) if payload is not None else observed

    def _index_path(self):
        raw = self.git(self.root, 'rev-parse', '--git-path', 'index').stdout.strip()
        path = Path(raw)
        return path if path.is_absolute() else self.root / path

    def _semantics(self, image):
        return parse_index_semantics(image[0], self.object_format)

    def commit_tracked(self, paths, command, run):
        """Git commit --only uses a private index; peers keep the real one."""
        with tempfile.NamedTemporaryFile(prefix='syncwheel-commit-index-', delete=False) as stream:
            temporary = Path(stream.name)
        temporary.unlink()
        environment = {'GIT_INDEX_FILE': str(temporary)}
        try:
            self.git(self.root, 'read-tree', 'HEAD', env=environment)
            self.git(self.root, 'add', '-f', '--', *paths, env=environment)
            changed = self.git(self.root, 'diff', '--cached', '--quiet', 'HEAD', '--', *paths,
                               check=False, env=environment)
            if changed.returncode == 0:
                return None
            if changed.returncode != 1:
                raise SyncwheelError(changed.stderr.strip() or 'could not inspect guarded metadata')
            run(command, cwd=self.root, env=environment)
            tip = self.git(self.root, 'rev-parse', 'HEAD').stdout.strip()
            for relative in paths:
                payload, _ = self.after[self.root / relative]
                expected = self.git(self.root, 'hash-object', '--stdin', input_text=payload.decode()).stdout.strip()
                actual = self.git(self.root, 'rev-parse', f'{tip}:{relative}').stdout.strip()
                if expected != actual:
                    raise SyncwheelError('metadata changed during commit; committed and working state preserved for review')
            self._align_index(paths, tip)
            return tip
        finally:
            temporary.unlink(missing_ok=True)
            Path(str(temporary) + '.lock').unlink(missing_ok=True)

    def _align_index(self, paths, tip):
        """Align only entries unchanged since admission, under Git's index lock."""
        index = self._index_path()
        lock = Path(str(index) + '.lock')
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise SyncwheelError('metadata committed; real index is busy and was preserved for review') from exc
        temporary = None
        try:
            os.close(descriptor)
            image = _image(index)
            descriptor, name = tempfile.mkstemp(prefix='syncwheel-align-index-', dir=index.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(image[0])
            environment = {'GIT_INDEX_FILE': str(temporary)}
            current = self._semantics(image)
            before = self._semantics(self.index_image_before)
            changed = False
            for relative in paths:
                raw_path = os.fsencode(relative)
                original = [entry for entry in before['entries'] if entry[0] == raw_path] if before else None
                observed = [entry for entry in current['entries'] if entry[0] == raw_path] if current else None
                if original is None or observed != original or any(entry[4] or entry[5] for entry in original):
                    continue
                entry = self.git(self.root, 'ls-tree', tip, '--', relative).stdout
                mode_object, path = entry.split('\t', 1)
                mode, _, object_id = mode_object.split()
                self.git(self.root, 'update-index', '--add', '--cacheinfo',
                         f'{mode},{object_id},{path.rstrip()}', env=environment)
                changed = True
            if changed:
                if _image(index) != image:
                    raise SyncwheelError('index changed outside its lock; committed metadata preserved for review')
                os.chmod(temporary, image[1])
                with temporary.open('rb') as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, index)
                fsync_directory_path(index.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
                Path(str(temporary) + '.lock').unlink(missing_ok=True)
            lock.unlink(missing_ok=True)

    def restore(self):
        if self.git(self.root, 'rev-parse', 'HEAD').stdout.strip() != self.head:
            return 'HEAD changed; metadata preserved for inspection (no rollback applied)'
        peers = []
        if self._semantics(_image(self._index_path())) != self._semantics(self.index_image_before):
            peers.append("index")
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
