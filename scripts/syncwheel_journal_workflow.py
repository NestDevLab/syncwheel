"""Journal snapshot, publication, pull and recovery transactions.

The workflow owns Git sequencing and file/index safety. The repository port names
its actual transport operations; it cannot access arbitrary CLI globals.
"""

import contextlib
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from typing import Protocol

from syncwheel_errors import SyncwheelError
from syncwheel_journal import admit_paths, parse_status_entries

try:
    import fcntl
except ImportError:  # pragma: no cover - unsupported platforms fail before locking
    fcntl = None


class JournalRepository(Protocol):
    """I/O needed by journal transactions, implemented by the application."""

    root: Path

    def command(self, repo_root, *args, **kwargs): ...
    def output_bytes(self, repo_root, *args, ok_codes=(0,)): ...
    def execute(self, command, **kwargs): ...
    def binary(self, command, **kwargs): ...
    def current_branch(self, repo_root): ...
    def tip(self, repo_root, ref): ...
    def common_dir(self, repo_root): ...
    def identity(self, repo_root, command): ...
    def authorized_push(self, repo_root, command, remote, refs, **kwargs): ...
    def environment(self, **kwargs): ...
    def write_json(self, path, payload): ...
    def read_file(self, path, max_file_bytes): ...
    def fingerprint(self, path): ...


class JournalWorkflow:
    def __init__(self, repository: JournalRepository):
        self.repository = repository

    def status_entries(self):
        result = self.repository.command(
            self.repository.root, 'status', '--porcelain=v1', '-z',
            '--untracked-files=all', '--ignore-submodules=all', '--no-renames',
        )
        return parse_status_entries(result.stdout)

    def real_index_clean(self):
        return self.repository.command(
            self.repository.root, 'diff', '--cached', '--quiet', 'HEAD', '--',
            check=False,
        ).returncode == 0

    def admission(self, journal, paths=None):
        return admit_paths(
            self.repository.root, journal, self.status_entries(), paths,
            read_file=self.repository.read_file,
        )

    @contextlib.contextmanager
    def lock(self):
        repo_root = self.repository.root
        if fcntl is None:
            raise SyncwheelError(f'journal snapshot locking is unsupported on {sys.platform}')
        git_dir = Path(self.repository.command(repo_root, 'rev-parse', '--absolute-git-dir').stdout.strip())
        lock_path = git_dir / 'syncwheel-journal.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a+') as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SyncwheelError(f'journal snapshot already running: {lock_path}') from exc
            yield lock_path

    @contextlib.contextmanager
    def index_lock(self):
        repo_root = self.repository.root
        git_dir = Path(self.repository.command(repo_root, 'rev-parse', '--absolute-git-dir').stdout.strip())
        index_path = git_dir / 'index'
        lock_path = git_dir / 'index.lock'
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise SyncwheelError(f'journal could not lock real index: {lock_path}') from exc
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                if index_path.exists():
                    handle.write(index_path.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            yield index_path, lock_path
        finally:
            lock_path.unlink(missing_ok=True)

    def snapshot(self, manifest, apply=False, paths=None):
        repo_root = self.repository.root
        journal = manifest['journal']
        branch = journal['branch']
        if self.repository.current_branch(repo_root) != branch:
            raise SyncwheelError(f'journal branch must be checked out: expected {branch!r}')
        if paths is not None:
            for path in paths:
                if not isinstance(path, str) or path.startswith('/') or '\\' in path or any(
                    part in {'', '.', '..'} for part in path.split('/')
                ) or path.split('/')[0] == '.git':
                    raise SyncwheelError('snapshot --path requires an exact repo-relative file path')
            if self.repository.command(repo_root, 'ls-files', '-u').stdout.strip():
                raise SyncwheelError('journal snapshot requires a conflict-free index')
        elif not self.real_index_clean():
            raise SyncwheelError('journal snapshot requires a clean real index')
        parent = self.repository.command(repo_root, 'rev-parse', 'HEAD').stdout.strip()
        admitted, excluded, rejected, fingerprints, blobs = self.admission(journal, paths=paths)
        result = {
            'mode': 'apply' if apply else 'plan',
            'branch': branch,
            'parent': parent,
            'admitted': admitted,
            'excluded': excluded,
            'rejected': rejected,
            'commit': None,
            'changed': False,
        }
        if rejected:
            reasons = ', '.join(f"{item['path']}: {item['reason']}" for item in rejected)
            raise SyncwheelError(f'journal snapshot rejected content: {reasons}')
        if not apply or not admitted:
            return result
        with self.lock(), self.index_lock() as (real_index_path, real_index_lock):
            if self.repository.command(repo_root, 'rev-parse', 'HEAD').stdout.strip() != parent:
                raise SyncwheelError('journal HEAD changed during snapshot; retry')
            if paths is None and not self.real_index_clean():
                raise SyncwheelError('journal real index changed during snapshot; retry')
            with tempfile.NamedTemporaryFile(prefix='syncwheel-journal-index-', delete=False) as handle:
                index_path = Path(handle.name)
            index_path.unlink(missing_ok=True)
            env = {'GIT_INDEX_FILE': str(index_path)}
            try:
                self.repository.command(repo_root, 'read-tree', parent, env=env)
                for entry in admitted:
                    path = entry['path']
                    blob = blobs.get(path)
                    if blob is None:
                        self.repository.command(repo_root, 'update-index', '--force-remove', '--', path, env=env)
                        continue
                    with tempfile.NamedTemporaryFile(prefix='syncwheel-journal-blob-', delete=False) as blob_file:
                        blob_path = Path(blob_file.name)
                        blob_file.write(blob['content'])
                    try:
                        object_id = self.repository.command(repo_root, 'hash-object', '-w', str(blob_path)).stdout.strip()
                    finally:
                        blob_path.unlink(missing_ok=True)
                    self.repository.command(
                        repo_root, 'update-index', '--add', '--cacheinfo',
                        f"{blob['mode']},{object_id},{path}", env=env,
                    )
                tree = self.repository.command(repo_root, 'write-tree', env=env).stdout.strip()
                parent_tree = self.repository.command(repo_root, 'rev-parse', f'{parent}^{{tree}}').stdout.strip()
                if tree == parent_tree:
                    return result
                for path, before in fingerprints.items():
                    target = repo_root / path
                    if before is None:
                        stable = not target.exists()
                    else:
                        try:
                            stable = target.exists() and self.repository.fingerprint(target) == before
                        except (OSError, SyncwheelError):
                            stable = False
                    if not stable:
                        raise SyncwheelError(f'journal content changed during snapshot: {path}; retry')
                if self.repository.command(repo_root, 'rev-parse', 'HEAD').stdout.strip() != parent:
                    raise SyncwheelError('journal HEAD changed during snapshot; retry')
                timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
                message = f'journal: snapshot {timestamp}'
                commit_cmd = self.repository.identity(
                    repo_root, ['git', 'commit-tree', tree, '-p', parent, '-m', message]
                )
                commit = self.repository.execute(commit_cmd, cwd=repo_root).stdout.strip()
                update = self.repository.command(repo_root, 'update-ref', f'refs/heads/{branch}', commit, parent, check=False)
                if update.returncode != 0:
                    raise SyncwheelError('journal HEAD changed before commit publication; retry')
                try:
                    if paths is None:
                        self.repository.command(repo_root, 'read-tree', commit, env={'GIT_INDEX_FILE': str(real_index_lock)})
                    else:
                        for entry in admitted:
                            relative = entry['path']
                            value = blobs.get(relative)
                            if value is None:
                                self.repository.command(repo_root, 'update-index', '--force-remove', '--', relative,
                                    env={'GIT_INDEX_FILE': str(real_index_lock)})
                            else:
                                object_id = self.repository.command(repo_root, 'rev-parse', f'{commit}:{relative}').stdout.strip()
                                self.repository.command(repo_root, 'update-index', '--add', '--cacheinfo',
                                    f"{value['mode']},{object_id},{relative}",
                                    env={'GIT_INDEX_FILE': str(real_index_lock)})
                    os.replace(real_index_lock, real_index_path)
                except Exception as exc:
                    rollback = self.repository.command(
                        repo_root, 'update-ref', f'refs/heads/{branch}', parent, commit, check=False
                    )
                    if rollback.returncode != 0:
                        raise SyncwheelError(
                            'journal index realignment failed and branch rollback also failed; STOP'
                        ) from exc
                    raise SyncwheelError('journal index realignment failed; branch update rolled back') from exc
                result.update({'commit': commit, 'changed': True})
                return result
            finally:
                index_path.unlink(missing_ok=True)

    def remote_tip(self, remote, branch):
        repo_root = self.repository.root
        result = self.repository.command(repo_root, 'ls-remote', '--heads', remote, f'refs/heads/{branch}', check=False)
        if result.returncode != 0:
            raise SyncwheelError(result.stderr.strip() or f'could not observe journal remote {remote}')
        line = result.stdout.strip()
        return line.split()[0] if line else None

    def unroll_snapshot(self, branch, commit, parent):
        """Turn an unpublished snapshot commit back into working-tree changes."""
        repo_root = self.repository.root
        with self.lock():
            moved = self.repository.command(repo_root, 'update-ref', f'refs/heads/{branch}', parent, commit, check=False)
            if moved.returncode != 0:
                raise SyncwheelError(
                    'journal publish lease lost and the local snapshot could not be unrolled; STOP'
                )
            self.repository.command(repo_root, 'read-tree', parent)

    def publish(self, manifest, apply=False, committed_only=False):
        repo_root = self.repository.root
        journal = manifest['journal']
        branch, remote = journal['branch'], journal['remote']
        # Another clone may have published since the last run; catching up first
        # keeps two writers from wedging each other on remote-ahead.
        pull = self.pull(manifest, apply=apply)
        expected_parent = pull['remote_tip']
        empty_snapshot = {'mode': 'apply' if apply else 'plan', 'branch': branch,
                          'parent': self.repository.tip(repo_root, 'HEAD'), 'admitted': [],
                          'excluded': [], 'rejected': [], 'commit': None, 'changed': False}
        if not apply:
            payload = empty_snapshot if committed_only else self.snapshot(manifest, apply=False)
            payload.update({'remote': remote, 'expected_remote_tip': expected_parent, 'pull': pull})
            return payload
        local_head = self.repository.command(repo_root, 'rev-parse', 'HEAD').stdout.strip()
        if expected_parent and self.repository.command(
            repo_root, 'merge-base', '--is-ancestor', expected_parent, local_head, check=False
        ).returncode != 0:
            raise SyncwheelError('journal local branch diverged from the expected remote parent; STOP')
        snapshot = empty_snapshot if committed_only else self.snapshot(manifest, apply=True)
        tip = snapshot['commit'] or local_head
        if tip != expected_parent:
            refspec = f'{tip}:refs/heads/{branch}'
            lease = f'--force-with-lease=refs/heads/{branch}:{expected_parent or ""}'
            pushed = self.repository.authorized_push(
                repo_root,
                ['git', 'push', '--porcelain', lease, remote, refspec],
                remote,
                [f'refs/heads/{branch}'],
                check=False,
                manifest=manifest,
            )
            if pushed.returncode != 0:
                if snapshot['commit']:
                    self.unroll_snapshot(branch, snapshot['commit'], snapshot['parent'])
                raise SyncwheelError(
                    'journal publish lease lost; local changes are back in the working tree. '
                    'Retry journal publish --apply, which pulls first'
                )
        snapshot.update({
            'remote': remote, 'expected_remote_tip': expected_parent,
            'published_tip': tip, 'pull': pull,
        })
        return snapshot

    def pull_blob_matches(self, path, entry):
        """Return True when the working-tree path is byte-identical to a tree entry."""
        repo_root = self.repository.root
        absolute = repo_root / path
        if entry is None:
            return not os.path.lexists(absolute)
        mode, object_id = entry
        if not os.path.lexists(absolute):
            return False
        if mode == '120000':
            if not absolute.is_symlink():
                return False
            content = os.readlink(absolute).encode()
        elif mode in {'100644', '100755'}:
            if absolute.is_symlink() or not absolute.is_file():
                return False
            if (mode == '100755') != bool(absolute.stat().st_mode & 0o111):
                return False
            content = absolute.read_bytes()
        else:
            return False
        return content == self.repository.output_bytes(repo_root, 'cat-file', 'blob', object_id)

    def pull_tree_entries(self, commit, paths):
        repo_root = self.repository.root
        if not paths:
            return {}
        listed = self.repository.output_bytes(repo_root, '--literal-pathspecs', 'ls-tree', '-z', commit, '--', *paths)
        entries = {}
        for record in listed.split(b'\0'):
            if not record:
                continue
            meta, name = record.split(b'\t', 1)
            mode, _kind, object_id = meta.decode().split()
            entries[name.decode()] = (mode, object_id)
        return {path: entries.get(path) for path in paths}

    def pull_index_entries(self):
        """Map each path whose index entry differs from HEAD to its (mode, object) or None."""
        repo_root = self.repository.root
        changed = self.repository.output_bytes(repo_root, 'diff', '--cached', '--name-only', '--no-renames', '-z', 'HEAD')
        paths = [name.decode() for name in changed.split(b'\0') if name]
        if not paths:
            return {}
        listed = self.repository.output_bytes(repo_root, '--literal-pathspecs', 'ls-files', '--stage', '-z', '--', *paths)
        entries = {path: None for path in paths}
        for record in listed.split(b'\0'):
            if not record:
                continue
            meta, name = record.split(b'\t', 1)
            mode, object_id, stage = meta.decode().split()
            if stage != '0':
                raise SyncwheelError(f'journal pull refuses an unmerged index entry: {name.decode()}')
            entries[name.decode()] = (mode, object_id)
        return entries

    def pull_plan(self, manifest, fetch=True):
        repo_root = self.repository.root
        journal = manifest['journal']
        branch, remote = journal['branch'], journal['remote']
        current = self.repository.current_branch(repo_root)
        if current != branch:
            raise SyncwheelError(f'journal branch must be checked out: expected {branch!r}, found {current!r}')
        tracking_ref = f'refs/remotes/{remote}/{branch}'
        head = self.repository.command(repo_root, 'rev-parse', 'HEAD').stdout.strip()
        plan = {
            'mode': 'plan', 'branch': branch, 'remote': remote, 'head': head,
            'remote_tip': None, 'relation': None, 'identical_dirty': [],
            'kept_local': [], 'conflicts': [], 'changed': False,
        }
        if fetch:
            if self.remote_tip(remote, branch) is None:
                plan['relation'] = 'remote-missing'
                return plan
            self.repository.command(repo_root, 'fetch', '--quiet', remote, f'+refs/heads/{branch}:{tracking_ref}')
        remote_tip = self.repository.tip(repo_root, tracking_ref)
        plan['remote_tip'] = remote_tip
        if not remote_tip:
            plan['relation'] = 'remote-missing'
            return plan
        if head == remote_tip:
            plan['relation'] = 'aligned'
            return plan
        if self.repository.command(repo_root, 'merge-base', '--is-ancestor', remote_tip, head, check=False).returncode == 0:
            plan['relation'] = 'ahead'
            return plan
        if self.repository.command(repo_root, 'merge-base', '--is-ancestor', head, remote_tip, check=False).returncode != 0:
            raise SyncwheelError(
                f'journal local branch {branch} diverged from {remote}/{branch}; '
                'STOP without merge, reset, rebase, or force'
            )
        plan['relation'] = 'behind'
        upstream = {
            name.decode()
            for name in self.repository.output_bytes(
                repo_root, 'diff', '--name-only', '--no-renames', '-z', head, remote_tip
            ).split(b'\0')
            if name
        }
        dirty = [entry['path'] for entry in self.status_entries()]
        touched = [path for path in dirty if path in upstream]
        targets = self.pull_tree_entries(remote_tip, touched)
        staged = self.pull_index_entries()
        for path in sorted(dirty):
            if path not in upstream:
                # A local file where upstream now needs a directory (or the reverse)
                # cannot survive the fast-forward either.
                prefix = path + '/'
                if any(other.startswith(prefix) or path.startswith(other + '/') for other in upstream):
                    plan['conflicts'].append(path)
                else:
                    plan['kept_local'].append(path)
            elif self.pull_blob_matches(path, targets[path]) and (
                path not in staged or staged[path] == targets[path]
            ):
                plan['identical_dirty'].append(path)
            else:
                plan['conflicts'].append(path)
        return plan

    def conflict_error(self, conflicts):
        return SyncwheelError(
            'journal pull refuses local changes that differ from the remote tip: '
            + ', '.join(conflicts)
            + '. Resolve with: syncwheel journal pull --apply --park-conflicts'
        )

    def regular_file(self, path):
        return os.path.lexists(path) and not os.path.islink(path) and os.path.isfile(path)

    def park_conflicts(self, plan):
        """Save each conflicting local version outside the worktree and restore the path to HEAD."""
        repo_root = self.repository.root
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        directory = (
            self.repository.common_dir(repo_root) / 'syncwheel' / 'journal-conflicts'
            / f"{stamp}-{plan['remote_tip'][:12]}"
        )
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        paths = plan['conflicts']
        in_head = self.pull_tree_entries(plan['head'], paths)
        entries = []
        for path in paths:
            source = repo_root / path
            ours = None
            if os.path.lexists(source):
                if not self.regular_file(source):
                    raise SyncwheelError(f'journal conflict on a non-regular path needs manual resolution: {path}')
                ours = directory / 'ours' / path
                ours.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, ours)
            entries.append({'path': path, 'ours': str(ours) if ours else None})
        tracked = [path for path in paths if in_head[path]]
        if tracked:
            self.repository.command(repo_root, '--literal-pathspecs', 'restore', '--source=HEAD', '--staged', '--worktree', '--', *tracked)
        for path in paths:
            if in_head[path]:
                continue
            self.repository.command(repo_root, '--literal-pathspecs', 'rm', '-q', '--cached', '--ignore-unmatch', '--', path)
            if os.path.lexists(repo_root / path):
                (repo_root / path).unlink()
        return {'directory': directory, 'base': plan['head'], 'theirs': plan['remote_tip'], 'entries': entries}

    def merge_parked(self, parked):
        """Three-way merge each parked file; clean results land in the worktree, the rest stay parked."""
        repo_root = self.repository.root
        directory = parked['directory']
        paths = [entry['path'] for entry in parked['entries']]
        bases = self.pull_tree_entries(parked['base'], paths)
        for entry in parked['entries']:
            path = entry['path']
            target = repo_root / path
            entry['theirs'] = f"{parked['theirs']}:{path}"
            entry['base'] = f"{parked['base']}:{path}"
            if entry['ours'] is None or bases[path] is None or not self.regular_file(target):
                entry['status'] = 'conflict'
                entry['reason'] = 'deleted or added on one side; no common base to merge'
                continue
            base_file = directory / 'base' / path
            base_file.parent.mkdir(parents=True, exist_ok=True)
            base_file.write_bytes(self.repository.output_bytes(repo_root, 'cat-file', 'blob', bases[path][1]))
            theirs_file = directory / 'theirs' / path
            theirs_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, theirs_file)
            result = self.repository.binary(
                ['git', 'merge-file', '-p', '-L', 'local', '-L', 'base', '-L', 'remote',
                 entry['ours'], str(base_file), str(theirs_file)],
                cwd=repo_root, capture_output=True, env=self.repository.environment(authorize=False),
            )
            if result.returncode == 0:
                target.write_bytes(result.stdout)
                entry['status'] = 'merged'
            else:
                conflict_file = directory / 'conflict' / path
                conflict_file.parent.mkdir(parents=True, exist_ok=True)
                conflict_file.write_bytes(result.stdout)
                entry['status'] = 'conflict'
                entry['conflict'] = str(conflict_file)
                entry['reason'] = 'overlapping edits' if result.returncode > 0 else 'binary or unmergeable content'
        self.repository.write_json(directory / 'conflicts.json', {
            'base': parked['base'], 'theirs': parked['theirs'], 'entries': parked['entries'],
        })
        return parked['entries']

    def pull(self, manifest, apply=False, park_conflicts=False):
        repo_root = self.repository.root
        plan = self.pull_plan(manifest)
        if plan['conflicts'] and not (apply and park_conflicts):
            raise self.conflict_error(plan['conflicts'])
        if not apply or plan['relation'] != 'behind':
            plan['mode'] = 'apply' if apply else 'plan'
            return plan
        head, remote_tip = plan['head'], plan['remote_tip']
        parked = None
        with self.lock():
            # Re-run the whole proof under the snapshot lock so a concurrent
            # snapshot or edit cannot slip in between the check and the move.
            locked = self.pull_plan(manifest, fetch=False)
            if (locked['head'], locked['remote_tip'], locked['relation']) != (head, remote_tip, 'behind'):
                raise SyncwheelError('journal state changed during pull; retry')
            if locked['conflicts']:
                if not park_conflicts:
                    raise self.conflict_error(locked['conflicts'])
                parked = self.park_conflicts(locked)
                locked = self.pull_plan(manifest, fetch=False)
                if locked['conflicts']:
                    raise SyncwheelError(
                        f"journal conflicts remain after parking in {parked['directory']}; STOP"
                    )
            already_staged = self.pull_index_entries()
            staged = [path for path in locked['identical_dirty'] if path not in already_staged]
            if staged:
                # Staging the byte-identical paths makes the index agree with the
                # target tree, so git's own fast-forward accepts them and still
                # refuses anything that changed after the proof.
                self.repository.command(repo_root, '--literal-pathspecs', 'add', '-A', '--', *staged)
            merged = self.repository.command(repo_root, 'merge', '--ff-only', '--no-edit', remote_tip, check=False)
            if merged.returncode != 0:
                if staged:
                    self.repository.command(repo_root, '--literal-pathspecs', 'reset', '-q', 'HEAD', '--', *staged, check=False)
                parked_note = f"; local versions are parked in {parked['directory']}" if parked else ''
                raise SyncwheelError(
                    'journal fast-forward failed; local branch left unchanged'
                    + parked_note + ': '
                    + (merged.stderr.strip() or merged.stdout.strip())
                )
        updated = self.repository.command(repo_root, 'rev-parse', 'HEAD').stdout.strip()
        if updated != remote_tip:
            raise SyncwheelError(f'journal fast-forward landed on {updated}, expected {remote_tip}; STOP')
        changed_paths = self.repository.command(repo_root, 'diff', '--name-only', head, remote_tip).stdout.splitlines()
        locked.update({
            'mode': 'apply', 'changed': True, 'head': updated, 'previous_head': head,
            'changed_paths': changed_paths, 'conflicts': plan['conflicts'],
        })
        if parked:
            entries = self.merge_parked(parked)
            locked['parked'] = {'directory': str(parked['directory']), 'entries': entries}
            locked['resolution_required'] = any(entry['status'] == 'conflict' for entry in entries)
        return locked

    def recover_clean_paths(self, paths):
        repo_root = self.repository.root
        status = self.repository.command(repo_root, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                     '--ignore-submodules=none', '--no-renames').stdout
        dirty = [entry[3:] for entry in status.split('\0') if entry]
        dirty.extend(path.decode() for path in self.repository.output_bytes(
            repo_root, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z'
        ).split(b'\0') if path)
        conflicts = sorted({path for path in dirty if any(
            path == touched or path.startswith(touched + '/') or touched.startswith(path + '/')
            for touched in paths
        )})
        if conflicts:
            raise SyncwheelError('journal recover refuses local changes on replay paths: ' + ', '.join(conflicts))

    def recover_move(self, branch, before, after):
        """Align the checkout without discarding edits, then CAS through the owned ref guard."""
        repo_root = self.repository.root
        if self.repository.current_branch(repo_root) != branch or self.repository.tip(repo_root, 'HEAD') != before:
            raise SyncwheelError('journal branch changed during recovery; STOP')
        # Two-tree checkout refuses dirty overlaps and untracked obstructions itself.
        self.repository.command(repo_root, 'read-tree', '-m', '-u', before, after)
        moved = self.repository.command(repo_root, 'update-ref', f'refs/heads/{branch}', after, before, check=False)
        if moved.returncode:
            self.repository.command(repo_root, 'read-tree', '-m', '-u', after, before)
            raise SyncwheelError('journal recovery ref lease lost; checkout restored: ' + moved.stderr.strip())

    def recover(self, manifest, apply=False, rollback=None, manifest_path=None):
        repo_root = self.repository.root
        journal = manifest['journal']
        branch, remote = journal['branch'], journal['remote']
        if self.repository.current_branch(repo_root) != branch:
            raise SyncwheelError('journal recovery requires the journal branch checked out')
        if rollback:
            receipt = json.loads(Path(rollback).read_text())
            if receipt.get('branch') != branch or receipt.get('operation') != 'journal-recover':
                raise SyncwheelError('receipt does not describe recovery of this journal branch')
            before, after = receipt['after'], receipt['before']
            if self.repository.tip(repo_root, 'HEAD') != before:
                raise SyncwheelError('journal rollback requires the exact recovered head; STOP')
            paths = [path.decode() for path in self.repository.output_bytes(repo_root, 'diff', '--name-only', '--no-renames', '-z', before, after).split(b'\0') if path]
            self.recover_clean_paths(paths)
            if apply:
                with self.lock():
                    self.recover_clean_paths(paths)
                    self.recover_move(branch, before, after)
            return {'mode': 'apply' if apply else 'plan', 'operation': 'rollback',
                    'before': before, 'after': after, 'changed': apply}
        # Fetch all advertised remote refs, including tags and other branches: a local
        # snapshot published on another ref must never be rewritten.
        self.repository.command(repo_root, 'fetch', '--quiet', '--all', '--tags')
        published_tips = []
        for name in self.repository.command(repo_root, 'remote').stdout.splitlines():
            advertised = self.repository.command(repo_root, 'ls-remote', '--refs', name).stdout.splitlines()
            refs = [line.split()[1] for line in advertised]
            if refs:
                self.repository.command(repo_root, 'fetch', '--quiet', '--no-tags', name, *refs)
            for line in advertised:
                object_id = line.split()[0]
                peeled = self.repository.command(repo_root, 'rev-parse', '--verify', object_id + '^{commit}', check=False)
                if peeled.returncode == 0:
                    published_tips.append((line.split()[1], peeled.stdout.strip()))
        tip = self.remote_tip(remote, branch)
        if tip is None:
            raise SyncwheelError('journal recovery requires an existing remote branch')
        head = self.repository.tip(repo_root, 'HEAD')
        ahead, behind = map(int, self.repository.command(repo_root, 'rev-list', '--left-right', '--count', f'{head}...{tip}').stdout.split())
        relation = 'diverged' if ahead and behind else 'ahead-only' if ahead else 'behind-only' if behind else 'in-sync'
        plan = {'mode': 'apply' if apply else 'plan', 'branch': branch, 'remote': remote,
                'before': head, 'remote_tip': tip, 'relation': relation,
                'ahead': ahead, 'behind': behind, 'changed': False, 'replayed': {}}
        if relation == 'behind-only':
            plan['pull'] = self.pull(manifest, apply=apply)
            plan['changed'] = plan['pull']['changed']
            return plan
        if relation != 'diverged':
            if ahead:
                plan['next'] = 'syncwheel journal publish --committed-only'
            return plan
        commits = self.repository.command(repo_root, 'rev-list', '--reverse', '--topo-order', f'{tip}..{head}').stdout.splitlines()
        remote_refs = self.repository.command(repo_root, 'for-each-ref', '--format=%(refname)', 'refs/remotes/').stdout.splitlines()
        published_tips.extend((ref, ref) for ref in remote_refs)
        for commit in commits:
            if len(self.repository.command(repo_root, 'rev-list', '--parents', '-n', '1', commit).stdout.split()) != 2:
                raise SyncwheelError(f'journal recovery requires single-parent snapshots: {commit}')
            for remote_ref, remote_commit in published_tips:
                if self.repository.command(repo_root, 'merge-base', '--is-ancestor', commit, remote_commit, check=False).returncode == 0:
                    raise SyncwheelError(f'journal recovery refuses already-published commit {commit} on {remote_ref}')
        paths = {path.decode() for path in self.repository.output_bytes(repo_root, 'diff', '--name-only', '--no-renames', '-z', head, tip).split(b'\0') if path}
        for commit in commits:
            paths.update(path.decode() for path in self.repository.output_bytes(repo_root, 'diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-z', '-r', commit).split(b'\0') if path)
        self.recover_clean_paths(paths)
        if not apply:
            plan['commits'] = commits
            return plan
        with self.lock():
            if self.repository.tip(repo_root, 'HEAD') != head:
                raise SyncwheelError('journal head changed during recovery; STOP')
            self.recover_clean_paths(paths)
            directory = self.repository.common_dir(repo_root) / 'syncwheel' / 'journal-recover'
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='replay-', dir=directory) as temporary:
                worktree = Path(temporary) / 'worktree'
                self.repository.command(repo_root, 'worktree', 'add', '--quiet', '--detach', str(worktree), tip)
                try:
                    parent = tip
                    for commit in commits:
                        picked = self.repository.command(worktree, 'cherry-pick', '--no-commit', commit, check=False)
                        if picked.returncode:
                            conflicts = [path.decode() for path in self.repository.output_bytes(worktree, 'diff', '--name-only', '--diff-filter=U', '-z').split(b'\0') if path]
                            raise SyncwheelError(f'journal recovery conflict at {commit}: '
                                                 + ', '.join(conflicts) + '; ' + picked.stderr.strip())
                        tree = self.repository.command(worktree, 'write-tree').stdout.strip()
                        # Rebuild the commit from its raw headers/message to preserve
                        # author, committer, timestamps, encoding and message bytes.
                        raw = self.repository.output_bytes(repo_root, 'cat-file', 'commit', commit)
                        headers, message = raw.split(b'\n\n', 1)
                        kept = []
                        skip = False
                        for line in headers.split(b'\n'):
                            if line.startswith(b' '):
                                if not skip:
                                    kept.append(line)
                                continue
                            skip = line.startswith((b'tree ', b'parent ', b'gpgsig ', b'gpgsig-sha256 ', b'mergetag '))
                            if not skip:
                                kept.append(line)
                        content = (f'tree {tree}\nparent {parent}\n'.encode()
                                   + b'\n'.join(kept) + b'\n\n' + message)
                        result = self.repository.binary(['git', 'hash-object', '-t', 'commit', '-w', '--stdin'],
                                                cwd=repo_root, input=content, capture_output=True,
                                                env=self.repository.environment())
                        if result.returncode:
                            raise SyncwheelError(result.stderr.decode(errors='replace'))
                        parent = result.stdout.decode().strip()
                        plan['replayed'][commit] = parent
                        self.repository.command(worktree, 'reset', '--quiet', '--hard', parent)
                    self.recover_clean_paths(paths)
                    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    receipt_path = directory / f'{head}-{parent}.json'
                    receipt = {'operation': 'journal-recover', 'branch': branch, 'before': head,
                               'after': parent, 'remote_tip': tip, 'replayed': plan['replayed'],
                               'timestamp': stamp, 'status': 'prepared',
                               'rollback': 'syncwheel journal recover --rollback '
                                           + shlex.quote(str(receipt_path)) + ' -r ' + shlex.quote(str(repo_root))
                                           + (' -M ' + shlex.quote(str(manifest_path)) if manifest_path else '') + ' --apply'}
                    self.repository.write_json(receipt_path, receipt)
                    self.recover_move(branch, head, parent)
                    receipt['status'] = 'complete'
                    self.repository.write_json(receipt_path, receipt)
                    plan.update({'after': parent, 'changed': True, 'receipt': str(receipt_path),
                                 'next': 'syncwheel journal publish --committed-only'})
                finally:
                    # Only the isolated replay is disposable, including conflict state.
                    self.repository.command(repo_root, 'worktree', 'remove', '--force', str(worktree))
        return plan
