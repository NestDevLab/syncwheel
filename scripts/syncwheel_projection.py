"""Checkout content leases and published integration projection planning."""

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
import tempfile
from typing import Protocol

import syncwheel_manifest_store as _manifest_storage

from syncwheel_errors import (
    SyncwheelError,
)
from syncwheel_manifest import (
    bound_merge_stack_resolution_commits,
    canonical_json_digest,
    coordination_config,
    coordination_is_active,
    manifest_digest,
    stack_integration_base_commits,
    stack_integration_only_commits,
    stack_map,
    syncwheel_worktree_root,
)
from syncwheel_replay import (
    replay_target,
)


class ProjectionRepository(Protocol):
    def commit_full_sha(self, *args, **kwargs): ...
    def find_worktree_for_branch(self, *args, **kwargs): ...
    def git(self, *args, **kwargs): ...
    def journal_status_entries(self, *args, **kwargs): ...
    def managed_process_env(self, *args, **kwargs): ...
    def ref_tip(self, *args, **kwargs): ...
    def ref_tree(self, *args, **kwargs): ...


class ProjectionCoordination(Protocol):
    def coordination_state_control_manifest(self, *args, **kwargs): ...
    def read_remote_coordination_state(self, *args, **kwargs): ...
    def remote_ref_tips(self, *args, **kwargs): ...
    def verify_coordination_state_manifest_digest(self, *args, **kwargs): ...


class ProjectionReplay(Protocol):
    def materialize_integration_projection(self, *args, **kwargs): ...


class ProjectionControl(Protocol):
    def manifest_from_tree(self, *args, **kwargs): ...


class ProjectionIgnorePolicy(Protocol):
    def split_syncwheel_managed_gitignore(self, *args, **kwargs): ...


def checkout_path_observation(repo_root, relative):
    """Read one checkout path through descriptor-bound, no-follow traversal."""
    parts = Path(relative).parts
    if (
        not parts
        or Path(relative).is_absolute()
        or any(part in {'', '.', '..'} for part in parts)
    ):
        return {'fingerprint': {'kind': 'invalid'}, 'bytes': None}
    directory_flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    descriptors = []
    try:
        current = os.open(str(Path(repo_root).resolve()), directory_flags | nofollow)
        descriptors.append(current)
        for part in parts[:-1]:
            try:
                current = os.open(
                    part, directory_flags | nofollow, dir_fd=current
                )
            except FileNotFoundError:
                return {'fingerprint': {'kind': 'missing'}, 'bytes': b''}
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    return {'fingerprint': {'kind': 'invalid'}, 'bytes': None}
                raise
            descriptors.append(current)
        try:
            path_stat = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return {'fingerprint': {'kind': 'missing'}, 'bytes': b''}
        mode = stat.S_IMODE(path_stat.st_mode)
        if stat.S_ISLNK(path_stat.st_mode):
            return {
                'fingerprint': {
                    'kind': 'symlink',
                    'mode': mode,
                    'device': path_stat.st_dev,
                    'inode': path_stat.st_ino,
                },
                'bytes': None,
            }
        if not stat.S_ISREG(path_stat.st_mode):
            return {
                'fingerprint': {'kind': 'other', 'mode': mode},
                'bytes': None,
            }
        try:
            descriptor = os.open(
                parts[-1], os.O_RDONLY | nofollow, dir_fd=current
            )
        except FileNotFoundError:
            return {'fingerprint': {'kind': 'changed'}, 'bytes': None}
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                return {'fingerprint': {'kind': 'changed'}, 'bytes': None}
            raise
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (path_stat.st_dev, path_stat.st_ino)
        ):
            return {'fingerprint': {'kind': 'changed'}, 'bytes': None}
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        stable_fields = (
            'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode',
        )
        if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
            return {'fingerprint': {'kind': 'changed'}, 'bytes': None}
        payload = b''.join(chunks)
        return {
            'fingerprint': {
                'kind': 'file',
                'mode': stat.S_IMODE(after.st_mode),
                'device': after.st_dev,
                'inode': after.st_ino,
                'size': after.st_size,
                'mtimeNs': after.st_mtime_ns,
                'ctimeNs': after.st_ctime_ns,
                'sha256': hashlib.sha256(payload).hexdigest(),
            },
            'bytes': payload,
        }
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def checkout_path_fingerprint(repo_root, relative):
    return checkout_path_observation(repo_root, relative)['fingerprint']


def regular_checkout_path_bytes(repo_root, relative):
    observed = checkout_path_observation(repo_root, relative)
    if observed['fingerprint']['kind'] == 'missing':
        return b''
    if observed['fingerprint']['kind'] != 'file':
        return None
    return observed['bytes']


def published_integration_tip_reuse_fallback(observation, reason, path_proof=None):
    return {
        'status': 'fallback',
        'reason': reason,
        'stateTip': observation['state_tip'],
        'publishedTip': observation['published_tip'],
        'pathProof': path_proof or [],
    }


def published_integration_tip_reuse_refusal(observation, reason):
    return {
        'status': 'refuse',
        'reason': reason,
        'stateTip': observation.get('state_tip'),
        'publishedTip': observation.get('published_tip'),
    }


def published_integration_replay_target(
    manifest, plan, worktree, reset_destination_lease,
):
    """Replay the declared projection from an exactly leased published tip."""
    integration = copy.deepcopy(manifest['integration'])
    integration['base'] = plan['publishedTip']
    stack_ref_overrides = {
        item['id']: item['tip']
        for item in plan['replayInputs']['refs']
        if item.get('kind') == 'merge-stack'
    }
    target = replay_target(
        integration=integration,
        worktree=worktree,
        skip_contained=True,
        stack_ref_overrides=stack_ref_overrides,
        expected_tip=plan['publishedTip'],
    )
    target['source_reset_lease'] = {
        'manifest_path': plan['sourceLease']['manifest_path'],
        'reset_guard_digest': plan['sourceLease']['reset_guard_digest'],
    }
    target['reset_destination_lease'] = copy.deepcopy(reset_destination_lease)
    return target


class ProjectionService:
    def __init__(self, repository: ProjectionRepository, coordination: ProjectionCoordination,
                 replay: ProjectionReplay, control: ProjectionControl,
                 ignore_policy: ProjectionIgnorePolicy):
        self.repository = repository
        self.coordination = coordination
        self.replay = replay
        self.control = control
        self.ignore_policy = ignore_policy


    def checkout_reset_guard(self, repo_root):
        """Bind a hard-reset decision to the index and checkout-relative dirt."""
        repo_root = Path(repo_root).resolve()
        index = self.repository.git(repo_root, 'ls-files', '--stage', '-z')
        unstaged_paths = sorted({
            path for path in self.repository.git(
                repo_root, 'diff-files', '--name-only', '-z'
            ).stdout.split('\0')
            if path
        })
        untracked_paths = sorted({
            path for path in self.repository.git(
                repo_root, 'ls-files', '--others', '--exclude-standard', '-z'
            ).stdout.split('\0')
            if path
        })
        return {
            'index_sha256': hashlib.sha256(index.stdout.encode('utf-8')).hexdigest(),
            'unstaged': {
                path: checkout_path_fingerprint(repo_root, path)
                for path in unstaged_paths
            },
            'untracked': {
                path: checkout_path_fingerprint(repo_root, path)
                for path in untracked_paths
            },
        }


    def checkout_reset_destination_lease(
        self, repo_root, branch, status_base=None, allowed_paths=None,
        allowed_path_prefixes=None,
    ):
        """Return one clean-validated observation of the integration reset target."""
        worktree = self.repository.find_worktree_for_branch(repo_root, branch)
        branch_ref = f'refs/heads/{branch}'
        allowed_paths = sorted(set(allowed_paths or ()))
        allowed_path_prefixes = sorted({
            str(prefix).replace('\\', '/').rstrip('/') + '/'
            for prefix in (allowed_path_prefixes or ())
        })
        empty_status = {'staged': [], 'unstaged': [], 'untracked': []}
        if worktree is None:
            return {
                'branch_ref': branch_ref,
                'worktree_path': None,
                'status_base': None,
                'status': empty_status,
                'allowed_paths': allowed_paths,
                'allowed_path_prefixes': allowed_path_prefixes,
                'reset_guard_digest': None,
            }
        worktree = Path(worktree).resolve()
        observed_branch_ref = self.repository.git(
            worktree, 'symbolic-ref', '--quiet', 'HEAD', check=False
        ).stdout.strip()
        if observed_branch_ref != branch_ref:
            raise SyncwheelError(
                'integration reset destination changed during lease capture; '
                'refusing to overwrite it'
            )
        status_base = status_base or self.repository.ref_tip(worktree, 'HEAD')
        before_guard = self.checkout_reset_guard(worktree)
        staged_paths = sorted({
            path for path in self.repository.git(
                worktree, 'diff', '--cached', '--name-only', '--no-renames', '-z',
                status_base, '--',
            ).stdout.split('\0')
            if path
        })
        status = {
            'staged': staged_paths,
            'unstaged': sorted(before_guard['unstaged']),
            'untracked': sorted(before_guard['untracked']),
        }
        changed_paths = sorted({
            path for paths in status.values() for path in paths
        })
        unexpected_paths = [
            path for path in changed_paths
            if path not in allowed_paths and not any(
                path.startswith(prefix) for prefix in allowed_path_prefixes
            )
        ]
        after_guard = self.checkout_reset_guard(worktree)
        final_branch_ref = self.repository.git(
            worktree, 'symbolic-ref', '--quiet', 'HEAD', check=False
        ).stdout.strip()
        if before_guard != after_guard or final_branch_ref != observed_branch_ref:
            raise SyncwheelError(
                'integration reset destination changed during lease capture; '
                'refusing to overwrite it'
            )
        if unexpected_paths:
            raise SyncwheelError(
                f'integration reset destination {worktree} is not clean at lease capture; '
                'refusing to overwrite it: ' + ', '.join(unexpected_paths)
            )
        return {
            'branch_ref': observed_branch_ref,
            'worktree_path': str(worktree),
            'status_base': status_base,
            'status': status,
            'allowed_paths': allowed_paths,
            'allowed_path_prefixes': allowed_path_prefixes,
            'reset_guard_digest': canonical_json_digest(after_guard),
        }


    def checkout_source_lease(self, repo_root, manifest_path):
        """Bind a reuse decision to HEAD, index, dirt, and its control source bytes."""
        repo_root = Path(repo_root).resolve()
        try:
            manifest_relative = Path(manifest_path).resolve(strict=False).relative_to(
                repo_root
            ).as_posix()
        except ValueError:
            return None
        status = self.repository.journal_status_entries(repo_root)
        paths = sorted({entry['path'] for entry in status} | {
            manifest_relative,
            '.gitignore',
        })
        reset_guard = self.checkout_reset_guard(repo_root)
        evidence = {
            'head': self.repository.ref_tip(repo_root, 'HEAD'),
            'index_sha256': reset_guard['index_sha256'],
            'status': status,
            'paths': {
                path: checkout_path_fingerprint(repo_root, path)
                for path in paths
            },
        }
        return {
            'digest': canonical_json_digest(evidence),
            'manifest_path': manifest_relative,
            'manifest': evidence['paths'][manifest_relative],
            'gitignore': evidence['paths']['.gitignore'],
            'reset_guard_digest': canonical_json_digest(reset_guard),
        }


    def tree_path_entry(self, repo_root, tree, relative):
        listing = self.repository.git(repo_root, 'ls-tree', '-z', tree, '--', relative, check=False)
        if listing.returncode != 0:
            raise SyncwheelError(f'cannot inspect integration tree path: {relative}')
        entries = [entry for entry in listing.stdout.split('\0') if entry]
        if not entries:
            return None
        if len(entries) != 1:
            raise SyncwheelError(f'ambiguous integration tree path: {relative}')
        metadata, separator, listed_path = entries[0].partition('\t')
        if not separator or listed_path != relative:
            raise SyncwheelError(f'ambiguous integration tree path: {relative}')
        mode, object_type, object_id = metadata.split(' ', 2)
        if object_type != 'blob' or not re.fullmatch(r'[0-9a-f]{40,64}', object_id):
            raise SyncwheelError(f'integration tree path is not a blob: {relative}')
        return {'mode': mode, 'blob': object_id}


    def tree_path_bytes(self, repo_root, entry):
        if entry is None:
            return b''
        result = subprocess.run(
            ['git', 'cat-file', 'blob', entry['blob']],
            cwd=repo_root,
            capture_output=True,
            env=self.repository.managed_process_env(),
        )
        if result.returncode != 0:
            raise SyncwheelError(f"cannot read integration tree blob: {entry['blob']}")
        return result.stdout


    def integration_tree_changed_paths(self, repo_root, left, right):
        return sorted({
            path for path in self.repository.git(
                repo_root,
                'diff-tree', '--no-commit-id', '--name-only', '-r', '-z',
                left, right,
            ).stdout.split('\0')
            if path
        })


    def materialize_control_manifest_projection_tree(
        self, repo_root, manifest, replay_tree, gitignore_bytes=None, manifest_bytes=None,
    ):
        """Overlay exact local control bytes on a projected tree without moving a ref."""
        relative = _manifest_storage.control_manifest_relative_path(repo_root)
        with tempfile.NamedTemporaryFile(
            prefix='syncwheel-control-projection-index-', delete=False
        ) as handle:
            index_path = Path(handle.name)
        index_path.unlink(missing_ok=True)
        environment = {'GIT_INDEX_FILE': str(index_path)}
        try:
            self.repository.git(repo_root, 'read-tree', replay_tree, env=environment)
            manifest_payload = (
                _manifest_storage.canonical_manifest_file_text(manifest).encode('utf-8')
                if manifest_bytes is None else manifest_bytes
            )
            result = subprocess.run(
                ['git', 'hash-object', '-w', '--stdin'],
                cwd=repo_root,
                input=manifest_payload,
                capture_output=True,
                env=self.repository.managed_process_env(),
            )
            if result.returncode != 0:
                raise SyncwheelError('cannot materialize the exact control manifest blob')
            blob = result.stdout.decode().strip()
            self.repository.git(
                repo_root,
                'update-index', '--add', '--cacheinfo',
                f'100644,{blob},{relative}',
                env=environment,
            )
            if gitignore_bytes is not None:
                result = subprocess.run(
                    ['git', 'hash-object', '-w', '--stdin'],
                    cwd=repo_root,
                    input=gitignore_bytes,
                    capture_output=True,
                    env=self.repository.managed_process_env(),
                )
                if result.returncode != 0:
                    raise SyncwheelError(
                        'cannot materialize the exact control .gitignore blob'
                    )
                gitignore_blob = result.stdout.decode().strip()
                self.repository.git(
                    repo_root,
                    'update-index', '--add', '--cacheinfo',
                    f'100644,{gitignore_blob},.gitignore',
                    env=environment,
                )
            return self.repository.git(repo_root, 'write-tree', env=environment).stdout.strip()
        finally:
            index_path.unlink(missing_ok=True)


    def observe_published_integration_tip(self, repo_root, manifest, *, fetch=True):
        config = coordination_config(manifest)
        if not config or config.get('mode') != 'active-active':
            return None
        remote = self.coordination.read_remote_coordination_state(
            repo_root,
            config,
            fetch=fetch,
            local_manifest_version=manifest['version'],
        )
        if not remote['tip'] or not remote['state']:
            return None
        if fetch and self.repository.ref_tip(repo_root, 'FETCH_HEAD') != remote['tip']:
            return {'status': 'stale'}
        state = remote['state']
        expected_refs = dict(state.get('managed_refs') or {})
        observed_refs = self.coordination.remote_ref_tips(repo_root, config['remote'], expected_refs)
        if observed_refs != expected_refs:
            return {'status': 'stale'}
        integration_ref = f"refs/heads/{manifest['integration']['branch']}"
        published_tip = expected_refs.get(integration_ref)
        if not published_tip:
            return None
        return {
            'status': 'current',
            'config': config,
            'state': state,
            'state_tip': remote['tip'],
            'state_digest': canonical_json_digest(state),
            'remote_refs': observed_refs,
            'integration_ref': integration_ref,
            'published_tip': published_tip,
        }


    def integration_projection_ref_observation(self, repo_root, kind, identifier, spec):
        tip = self.repository.ref_tip(repo_root, spec)
        if not tip:
            raise SyncwheelError(f'cannot resolve {kind} replay input: {spec}')
        symbolic = self.repository.git(
            repo_root, 'rev-parse', '--verify', '--symbolic-full-name', spec,
            check=False,
        ).stdout.strip()
        observed = {
            'kind': kind,
            'id': identifier,
            'spec': spec,
            'symbolicRef': symbolic or None,
            'tip': tip,
        }
        if symbolic.startswith('refs/remotes/'):
            remotes = sorted(
                (line.strip() for line in self.repository.git(repo_root, 'remote').stdout.splitlines()),
                key=len,
                reverse=True,
            )
            for remote in remotes:
                prefix = f'refs/remotes/{remote}/'
                if not symbolic.startswith(prefix):
                    continue
                remote_ref = f"refs/heads/{symbolic[len(prefix):]}"
                observed.update({
                    'remote': remote,
                    'remoteRef': remote_ref,
                    'remoteTip': self.coordination.remote_ref_tips(
                        repo_root, remote, [remote_ref]
                    )[remote_ref],
                })
                break
        return observed


    def integration_projection_input_snapshot(self, repo_root, manifest):
        """Resolve every moving ref and immutable commit consumed by projection."""
        integration = manifest['integration']
        refs = [self.integration_projection_ref_observation(
            repo_root, 'base', 'integration', integration['base']
        )]
        stacks = stack_map(manifest)
        immutable_commits = []
        for stack_id in integration['stacks']:
            stack = stacks[stack_id]
            if integration.get('strategy', 'cherry-pick') == 'merge-stacks':
                refs.append(self.integration_projection_ref_observation(
                    repo_root, 'merge-stack', stack_id, stack['branch']
                ))
                immutable_commits.extend(
                    self.repository.commit_full_sha(repo_root, value) for value in
                    (bound_merge_stack_resolution_commits(stack) or [])
                )
            else:
                immutable_commits.extend(
                    self.repository.commit_full_sha(repo_root, commit)
                    for commit in stack_integration_base_commits(stack)
                )
            immutable_commits.extend(
                self.repository.commit_full_sha(repo_root, commit)
                for commit in stack_integration_only_commits(stack)
            )
        return {
            'refs': refs,
            'commits': immutable_commits,
        }


    def plan_published_integration_tip_reuse(
        self, repo_root, manifest, manifest_path, *, refresh_inputs=True,
    ):
        """Prove that retaining an already-published integration tip loses no bytes."""
        if not coordination_is_active(manifest):
            return None
        if refresh_inputs:
            self.repository.git(repo_root, 'fetch', '--all', '--prune')
        source_lease = self.checkout_source_lease(repo_root, manifest_path)
        if source_lease is None:
            return None
        observation = self.observe_published_integration_tip(
            repo_root, manifest, fetch=refresh_inputs
        )
        if observation is None:
            return None
        if observation['status'] == 'stale':
            return published_integration_tip_reuse_refusal(
                observation,
                'published coordination state or managed refs changed during reuse planning',
            )
        if source_lease['manifest']['kind'] != 'file':
            return published_integration_tip_reuse_refusal(
                observation,
                'source control manifest must remain one regular no-follow file',
            )
        branch = manifest['integration']['branch']
        if self.repository.ref_tip(repo_root, branch) != observation['published_tip']:
            return None
        try:
            self.coordination.verify_coordination_state_manifest_digest(
                repo_root, observation['state'], observation['config']['remote']
            )
            published_manifest = self.coordination.coordination_state_control_manifest(
                repo_root, observation['state'], observation['config']['remote']
            )
        except SyncwheelError as exc:
            return published_integration_tip_reuse_refusal(
                observation,
                f'published integration control proof is invalid: {exc}',
            )
        expected_manifest_digest = manifest_digest(manifest)
        if manifest_digest(published_manifest) != expected_manifest_digest:
            return None

        try:
            replay_inputs = self.integration_projection_input_snapshot(repo_root, manifest)
        except SyncwheelError as exc:
            return published_integration_tip_reuse_fallback(
                observation, f'integration replay inputs are unavailable: {exc}'
            )
        remote_input_drift = [
            item for item in replay_inputs['refs']
            if item.get('remoteRef') and item.get('remoteTip') != item['tip']
        ]
        if remote_input_drift:
            return published_integration_tip_reuse_refusal(
                observation,
                'integration replay input differs from its remote ref: '
                + ', '.join(item['spec'] for item in remote_input_drift),
            )

        source_gitignore_bytes = regular_checkout_path_bytes(repo_root, '.gitignore')
        if source_gitignore_bytes is None:
            return published_integration_tip_reuse_refusal(
                observation,
                'source .gitignore differs from the published control bytes: not a regular file',
            )
        try:
            source_gitignore_text = source_gitignore_bytes.decode('utf-8')
        except UnicodeDecodeError:
            source_gitignore = None
        else:
            source_gitignore = self.ignore_policy.split_syncwheel_managed_gitignore(
                source_gitignore_text, syncwheel_worktree_root(manifest)
            )
        if source_gitignore is None:
            return published_integration_tip_reuse_refusal(
                observation,
                'source .gitignore differs from the published control bytes: '
                'managed block is malformed or duplicated',
            )

        published_tip = observation['published_tip']
        published_tree = self.repository.ref_tree(repo_root, published_tip)
        published_manifest_entry = self.tree_path_entry(
            repo_root, published_tree, source_lease['manifest_path']
        )
        published_manifest_bytes = self.tree_path_bytes(
            repo_root, published_manifest_entry
        )
        replay_tree = self.replay.materialize_integration_projection(repo_root, manifest)
        projected_tree = self.materialize_control_manifest_projection_tree(
            repo_root,
            manifest,
            replay_tree,
            gitignore_bytes=(
                source_gitignore_bytes
                if source_lease['gitignore']['kind'] == 'file'
                else None
            ),
            manifest_bytes=published_manifest_bytes,
        )
        try:
            replay_inputs_after = self.integration_projection_input_snapshot(
                repo_root, manifest
            )
        except SyncwheelError:
            replay_inputs_after = None
        if replay_inputs_after != replay_inputs:
            return published_integration_tip_reuse_refusal(
                observation,
                'integration replay inputs changed during published-tip proof',
            )
        changed_paths = self.integration_tree_changed_paths(
            repo_root, published_tree, projected_tree
        )
        path_proof = [
            {
                'path': path,
                'published': self.tree_path_entry(repo_root, published_tree, path),
                'projected': self.tree_path_entry(repo_root, projected_tree, path),
            }
            for path in changed_paths
        ]
        allowed_paths = {source_lease['manifest_path'], '.gitignore'}

        published_gitignore_entry = self.tree_path_entry(
            repo_root, published_tree, '.gitignore'
        )
        published_gitignore_bytes = self.tree_path_bytes(
            repo_root, published_gitignore_entry
        )
        if (
            source_gitignore['unmanaged']
            and source_gitignore_bytes != published_gitignore_bytes
        ):
            return published_integration_tip_reuse_refusal(
                observation,
                'source .gitignore differs from the published control bytes: '
                'unreviewed surrounding bytes are present',
            )

        unexpected = sorted(set(changed_paths) - allowed_paths)
        if unexpected:
            base_inputs = [
                item for item in replay_inputs['refs']
                if item.get('kind') == 'base' and item.get('id') == 'integration'
            ]
            if len(base_inputs) != 1:
                return published_integration_tip_reuse_refusal(
                    observation,
                    'integration replay input proof has no unique base ref',
                )
            base_tree = self.repository.ref_tree(repo_root, base_inputs[0]['tip'])
            unsafe_product_paths = []
            for proof in path_proof:
                if proof['path'] not in unexpected:
                    continue
                proof['base'] = self.tree_path_entry(repo_root, base_tree, proof['path'])
                # A declared replay may add, modify, or delete a base path. It may
                # not replace a byte that exists only on the published partial tip:
                # that would silently discard an unmapped contribution.
                if proof['published'] != proof['base']:
                    unsafe_product_paths.append(proof['path'])
            if unsafe_product_paths:
                return {
                    **published_integration_tip_reuse_refusal(
                        observation,
                        'published integration contains unexplained product paths: '
                        + ', '.join(unsafe_product_paths),
                    ),
                    'pathProof': path_proof,
                }

        if source_lease['manifest_path'] in changed_paths:
            projected_manifest_entry = self.tree_path_entry(
                repo_root, projected_tree, source_lease['manifest_path']
            )
            projected_manifest = self.control.manifest_from_tree(
                repo_root, projected_tree, manifest_path
            )
            if (
                not published_manifest_entry
                or published_manifest_entry['mode'] != '100644'
                or not projected_manifest_entry
                or projected_manifest_entry['mode'] != '100644'
                or projected_manifest is None
                or manifest_digest(projected_manifest) != expected_manifest_digest
            ):
                return published_integration_tip_reuse_fallback(
                    observation,
                    'projected control manifest is not semantically identical',
                    path_proof,
                )

        projected_gitignore_entry = self.tree_path_entry(
            repo_root, projected_tree, '.gitignore'
        )
        projected_gitignore_bytes = self.tree_path_bytes(
            repo_root, projected_gitignore_entry
        )
        source_gitignore_fingerprint = source_lease['gitignore']
        gitignore_modes_match = (
            (
                published_gitignore_entry is None
                and source_gitignore_fingerprint['kind'] == 'missing'
            )
            or (
                published_gitignore_entry is not None
                and published_gitignore_entry['mode'] == '100644'
                and source_gitignore_fingerprint['kind'] == 'file'
                and not source_gitignore_fingerprint['mode'] & 0o111
            )
        ) and (
            projected_gitignore_entry is None
            or projected_gitignore_entry['mode'] == '100644'
        )
        if not gitignore_modes_match:
            return published_integration_tip_reuse_fallback(
                observation,
                'source, published, and projected .gitignore modes differ',
                path_proof,
            )
        if '.gitignore' in changed_paths:
            try:
                published_gitignore_text = published_gitignore_bytes.decode('utf-8')
                projected_gitignore_text = projected_gitignore_bytes.decode('utf-8')
            except UnicodeDecodeError:
                return published_integration_tip_reuse_fallback(
                    observation,
                    'published or projected .gitignore is not UTF-8 control text',
                    path_proof,
                )
            published_gitignore = self.ignore_policy.split_syncwheel_managed_gitignore(
                published_gitignore_text, syncwheel_worktree_root(manifest)
            )
            projected_gitignore = self.ignore_policy.split_syncwheel_managed_gitignore(
                projected_gitignore_text, syncwheel_worktree_root(manifest)
            )
            if (
                published_gitignore is None
                or projected_gitignore is None
                or published_gitignore['unmanaged'] != projected_gitignore['unmanaged']
            ):
                return published_integration_tip_reuse_fallback(
                    observation,
                    'published .gitignore differs outside its one managed block',
                    path_proof,
                )

        if source_gitignore_bytes != published_gitignore_bytes:
            return published_integration_tip_reuse_fallback(
                observation,
                'source .gitignore does not exactly match the published control bytes',
                path_proof,
            )

        current_source_lease = self.checkout_source_lease(repo_root, manifest_path)
        if (
            current_source_lease is None
            or current_source_lease['digest'] != source_lease['digest']
        ):
            return published_integration_tip_reuse_refusal(
                observation,
                'source checkout changed during published integration reuse planning',
            )
        return {
            'status': 'replay' if unexpected else 'retain',
            'stateTip': observation['state_tip'],
            'stateDigest': observation['state_digest'],
            'remoteRefs': observation['remote_refs'],
            'integrationRef': observation['integration_ref'],
            'publishedTip': published_tip,
            'publishedTree': published_tree,
            'projectedTree': projected_tree,
            'manifestDigest': expected_manifest_digest,
            'sourceLease': source_lease,
            'replayInputs': replay_inputs,
            'changedPaths': changed_paths,
            'replayProductPaths': unexpected,
            'pathProof': path_proof,
        }


    def published_integration_tip_reuse_is_current(
        self, repo_root, manifest, manifest_path, plan
    ):
        """Recheck every source, state, and ref lease immediately before retention."""
        before_source = self.checkout_source_lease(repo_root, manifest_path)
        if before_source is None or before_source['digest'] != plan['sourceLease']['digest']:
            return False
        if self.repository.ref_tip(repo_root, manifest['integration']['branch']) != plan['publishedTip']:
            return False
        try:
            if self.integration_projection_input_snapshot(repo_root, manifest) != plan['replayInputs']:
                return False
        except SyncwheelError:
            return False
        observed = self.observe_published_integration_tip(repo_root, manifest)
        if observed is None or observed.get('status') != 'current':
            return False
        if (
            observed['state_tip'] != plan['stateTip']
            or observed['state_digest'] != plan['stateDigest']
            or observed['remote_refs'] != plan['remoteRefs']
            or observed['published_tip'] != plan['publishedTip']
        ):
            return False
        observed_manifest, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
        if (
            observed_manifest is None
            or manifest_digest(observed_manifest) != plan['manifestDigest']
        ):
            return False
        after_source = self.checkout_source_lease(repo_root, manifest_path)
        return bool(
            after_source
            and after_source['digest'] == plan['sourceLease']['digest']
            and self.repository.ref_tip(repo_root, manifest['integration']['branch'])
            == plan['publishedTip']
        )


    def published_integration_replay_is_current(
        self, repo_root, manifest, manifest_path, plan, expected_source_lease,
    ):
        """Verify the replay result and every remote/input lease before publication."""
        before_source = self.checkout_source_lease(repo_root, manifest_path)
        if (
            before_source is None
            or before_source['digest'] != expected_source_lease.get('digest')
        ):
            return False
        try:
            before_inputs = self.integration_projection_input_snapshot(repo_root, manifest)
        except SyncwheelError:
            return False
        if before_inputs != plan['replayInputs']:
            return False
        observed = self.observe_published_integration_tip(repo_root, manifest)
        if observed is None or observed.get('status') != 'current':
            return False
        try:
            after_inputs = self.integration_projection_input_snapshot(repo_root, manifest)
        except SyncwheelError:
            return False
        if (
            after_inputs != plan['replayInputs']
            or observed['state_tip'] != plan['stateTip']
            or observed['state_digest'] != plan['stateDigest']
            or observed['remote_refs'] != plan['remoteRefs']
            or observed['published_tip'] != plan['publishedTip']
        ):
            return False
        branch = manifest['integration']['branch']
        local_tip = self.repository.ref_tip(repo_root, branch)
        if not local_tip:
            return False
        if self.repository.git(
            repo_root,
            'merge-base', '--is-ancestor', plan['publishedTip'], local_tip,
            check=False,
        ).returncode != 0:
            return False
        local_tree = self.repository.ref_tree(repo_root, local_tip)
        if local_tree != plan['projectedTree']:
            return False
        committed = self.control.manifest_from_tree(
            repo_root, local_tip, _manifest_storage.integration_manifest_path(repo_root)
        )
        source, _ = _manifest_storage.load_manifest(repo_root, manifest_path)
        if (
            committed is None
            or source is None
            or manifest_digest(committed) != plan['manifestDigest']
            or manifest_digest(source) != plan['manifestDigest']
            or self.tree_path_entry(repo_root, local_tree, '.gitignore')
            != self.tree_path_entry(repo_root, plan['projectedTree'], '.gitignore')
        ):
            return False
        after_source = self.checkout_source_lease(repo_root, manifest_path)
        return bool(
            after_source
            and after_source['digest'] == expected_source_lease.get('digest')
        )


    def unpublished_local_integration_control_tip(self, repo_root, manifest, manifest_path=None):
        """Return only a receipt-backed control tip with the exact projected tree."""
        observation = self.observe_published_integration_tip(repo_root, manifest)
        if observation is None:
            return None
        if observation['status'] == 'stale':
            raise SyncwheelError(
                'published coordination state or managed refs changed while '
                'checking the local integration control tip'
            )
        branch = manifest['integration']['branch']
        local_tip = self.repository.ref_tip(repo_root, branch)
        if not local_tip or local_tip == observation['published_tip']:
            return None
        committed = self.control.manifest_from_tree(
            repo_root, local_tip, _manifest_storage.integration_manifest_path(repo_root)
        )
        if committed is None or manifest_digest(committed) != manifest_digest(manifest):
            return None
        events = _manifest_storage.load_control_manifest_events(repo_root, manifest_path)
        intents, receipts = _manifest_storage.control_manifest_operation_records(events, local_tip)
        completed = sorted(set(intents) & set(receipts))
        if len(completed) != 1:
            return None
        operation_id = completed[0]
        intent = intents[operation_id]['payload']
        receipt = receipts[operation_id]['payload']
        expected_digest = manifest_digest(manifest)
        parent = self.repository.git(repo_root, 'rev-parse', f'{local_tip}^', check=False)
        replay_tip = parent.stdout.strip() if parent.returncode == 0 else None
        if (
            intent.get('integration_branch') != branch
            or intent.get('expected_control_commit') != local_tip
            or intent.get('expected_manifest_digest') != expected_digest
            or intent.get('replay_tip') != replay_tip
            or receipt.get('control_commit') != local_tip
            or receipt.get('manifest_hash') != expected_digest
            or (receipt.get('context') or {}).get('control_manifest_digest')
            != expected_digest
            or (receipt.get('context') or {}).get('replay_tip') != replay_tip
        ):
            return None
        try:
            replay_inputs = self.integration_projection_input_snapshot(repo_root, manifest)
        except SyncwheelError:
            return None
        if any(
            item.get('remoteRef') and item.get('remoteTip') != item['tip']
            for item in replay_inputs['refs']
        ):
            return None
        replay_tree = self.replay.materialize_integration_projection(repo_root, manifest)
        source_gitignore = checkout_path_observation(repo_root, '.gitignore')
        source_gitignore_kind = source_gitignore['fingerprint']['kind']
        if source_gitignore_kind not in {'file', 'missing'}:
            return None
        source_gitignore_bytes = source_gitignore['bytes']
        projected_tree = self.materialize_control_manifest_projection_tree(
            repo_root,
            manifest,
            replay_tree,
            gitignore_bytes=(
                source_gitignore_bytes
                if source_gitignore_kind == 'file'
                else None
            ),
        )
        try:
            replay_inputs_after = self.integration_projection_input_snapshot(
                repo_root, manifest
            )
        except SyncwheelError:
            return None
        if replay_inputs_after != replay_inputs or self.repository.ref_tree(repo_root, local_tip) != projected_tree:
            return None
        return local_tip
