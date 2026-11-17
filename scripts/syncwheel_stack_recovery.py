"""Proof and scoped checkout alignment for withdrawal of one native stack."""

import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import tempfile

from syncwheel_errors import SyncwheelError
from syncwheel_files import fsync_directory_path
from syncwheel_ledger import manifest_stack_history_summary
from syncwheel_manifest import canonical_json_digest, manifest_digest, normalize_manifest_data
from syncwheel_manifest_store import (
    canonical_manifest_file_text,
    load_ledger_events,
    pending_control_manifest_intents,
)
from syncwheel_retire_copy import (
    checked_parent,
    directory_key,
    install_bytes,
    moved_key,
    observed_file,
    opened_directory,
    rename_new,
    require,
    same_inode,
)


RECOVERY_COMMAND = 'syncwheel manifest recover-stack'
MANIFEST = '.syncwheel/manifest.json'
COMMIT_ID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')


def stack_recovery_checkpoint(stage):
    """Fault injection seam around durable withdrawal phases."""


def _unique_json(data, path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f'duplicate JSON key {key!r} in {path}')
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise SyncwheelError(f'cannot read JSON from {path}: {exc}') from exc


def normalized_manifest(data, path, *, native_source=False):
    parsed = _unique_json(data, path)
    if native_source:
        native = (json.dumps(parsed, indent=2) + '\n').encode()
        require(native == data,
                'manifest dirt does not match native serialization; preserve the edited manifest')
    normalized = normalize_manifest_data(parsed, path)
    if native_source:
        require((json.dumps(normalized, indent=2) + '\n').encode() == data,
                'manifest dirt differs from the normalized native manifest serialization')
    return normalized


def _stack(manifest, target):
    matches = [item for item in manifest['stacks'] if item['id'] == target]
    require(len(matches) <= 1, f'ambiguous stack declaration for {target!r}')
    return matches[0] if matches else None


def _without_stack(manifest, target):
    desired = copy.deepcopy(manifest)
    desired['stacks'] = [item for item in desired['stacks'] if item['id'] != target]
    desired['integration']['stacks'] = [item for item in desired['integration']['stacks'] if item != target]
    return desired


def _check_dependents(manifest, target):
    for item in manifest['stacks']:
        require(target not in item.get('depends_on', []),
                f"stack {item['id']!r} depends on {target!r}")
    for channel in manifest.get('channels', []):
        for item in channel.get('composition', []):
            require(item['stack'] != target and target not in item.get('dependsOn', []),
                    f"channel {channel['id']!r} still references {target!r}")


def _event_scope(event, stack_id):
    payload = event.get('payload') or {}
    context = payload.get('context') or {}
    return payload.get('stack', context.get('stack')) == stack_id


def _receipt_payload(event):
    return event['payload']


def _manifest_events(events):
    return [event for event in events if event['type'] in {'manifest_saved', 'manifest_initialized'}]


def _target_summary(event, stack_id):
    rows = [item for item in (_receipt_payload(event).get('stacks') or []) if item['id'] == stack_id]
    require(len(rows) <= 1, 'ambiguous target stack in the native manifest ledger')
    return rows[0] if rows else None


def _ledger_digest(events):
    return canonical_json_digest(events)


def _without_ctime(value):
    return moved_key(value)


class StackRecovery:
    def __init__(self, ports, root, manifest_path, common, stack_id, reason):
        self.ports = ports
        self.root = root
        self.manifest_path = manifest_path
        self.common = common
        self.stack_id = stack_id
        self.reason = reason
        self.store = common / 'syncwheel' / 'recover-stack'

    def events(self):
        self.ports.require_ledger_paths()
        events = load_ledger_events(self.root, self.manifest_path)
        require(all(event.get('seq') == index for index, event in enumerate(events, 1)),
                'ambiguous or discontinuous native ledger history')
        return events

    def history(self, manifest, head, stack, events, head_stack, source_tip):
        stack_id, branch = self.stack_id, stack['branch']
        saves = _manifest_events(events)
        require(saves, 'no native manifest ledger evidence')
        latest = saves[-1]
        payload = _receipt_payload(latest)
        context = payload.get('context') or {}
        require(latest['type'] == 'manifest_saved' and payload.get('reason') in {'stack_create', 'stack_sync'}
                and context.get('stack') == stack_id and context.get('branch') == branch
                and payload.get('manifest_hash') == manifest_digest(manifest)
                and Path(payload.get('manifest_path', '')).resolve() == self.manifest_path
                and _target_summary(latest, stack_id) == manifest_stack_history_summary(stack),
                'manifest dirt is not the latest native create or sync for this stack')
        boundary = max((event['seq'] for event in events
                        if _event_scope(event, stack_id)
                        and (event['type'] == 'stack_closed'
                             or (event['type'] == 'manifest_saved'
                                 and _receipt_payload(event).get('reason') == 'stack_withdraw'))),
                       default=0)
        scoped = [event for event in events if event['seq'] > boundary]
        creates = [event for event in scoped
                   if event['type'] == 'manifest_saved'
                   and _receipt_payload(event).get('reason') == 'stack_create'
                   and _event_scope(event, stack_id)]
        require(len(creates) == 1 and creates[0]['seq'] <= latest['seq'],
                'the target stack does not have a unique native create generation')
        create = creates[0]
        lifecycle = [event for event in scoped if event['seq'] >= create['seq'] and _event_scope(event, stack_id)]
        rejected = {
            'stack_pushed', 'stack_promoted', 'stack_archive_prepared', 'stack_archived',
            'stack_land_started', 'stack_land_prepared', 'stack_land_receipt',
            'github_pr_merge_started', 'github_pr_merge_prepared', 'github_pr_merge_receipt',
            'stack_tip_adoption_prepared', 'stack_tip_adopted',
        }
        require(not any(event['type'] in rejected for event in lifecycle),
                'native ledger shows publication, adoption, or another stack disposition')
        self.ports.require_unpublished_tracking(branch)
        create_row = _target_summary(create, stack_id)
        require(create_row and create_row.get('branch') == branch,
                'the native create record does not identify this source branch')
        token = (_receipt_payload(create).get('context') or {}).get('operation_token')
        if token:
            create_intents = [event for event in scoped if event['type'] == 'stack_create_intent'
                              and (_receipt_payload(event).get('operation_token') == token)]
            require(len(create_intents) == 1 and create_intents[0]['seq'] < create['seq']
                    and _event_scope(create_intents[0], stack_id)
                    and _receipt_payload(create_intents[0]).get('branch') == branch,
                    'the native create receipt has no exact matching intent')
        else:
            create_intents = []
        if head_stack:
            require(payload['reason'] == 'stack_sync'
                    and {k: v for k, v in stack.items() if k != 'commits'}
                    == {k: v for k, v in head_stack.items() if k != 'commits'},
                    'the dirty stack changes fields outside its native sync commit list')
            earlier = [event for event in saves if create['seq'] <= event['seq'] < latest['seq']
                       and _receipt_payload(event).get('manifest_hash') == manifest_digest(head)
                       and _target_summary(event, stack_id) == manifest_stack_history_summary(head_stack)]
            require(earlier, 'HEAD declaration is missing an earlier exact native ledger save')
            prior_head = earlier[-1]['seq']
        else:
            prior_head = None
        require(not stack.get('adopted_history') and not (head_stack or {}).get('adopted_history'),
                'recover-stack cannot prove an adopted generation from the native stack ledger')

        candidates = {source_tip}
        if create_intents:
            candidates.add(_receipt_payload(create_intents[0]).get('tip'))
        generations = []
        for event in saves:
            if not create['seq'] <= event['seq'] <= latest['seq']:
                continue
            row = _target_summary(event, stack_id)
            require(row is not None and row['branch'] == branch
                    and Path(_receipt_payload(event).get('manifest_path', '')).resolve() == self.manifest_path,
                    'stack membership or branch changed across the recovered lifecycle')
            commits = set()
            for field in ('commits', 'integration_commits', 'integration_only_commits'):
                revisions = row.get(field) or []
                require(isinstance(revisions, list), 'invalid commit history in native manifest ledger')
                commits.update(revisions)
            candidates.update(commits)
            generations.append(event['seq'])
        rebuilds = [event for event in scoped if event['type'] == 'stack_rebuilt'
                    and event['seq'] > create['seq'] and _event_scope(event, stack_id)]
        for event in rebuilds:
            rebuild = _receipt_payload(event)
            require(rebuild.get('branch') == branch and rebuild.get('after_tip'),
                    'native rebuild does not identify the target source branch and tip')
            candidates.add(rebuild['after_tip'])
            if rebuild.get('before_tip'):
                candidates.add(rebuild['before_tip'])
        base = self.ports.commit(stack['base'])
        if payload['reason'] == 'stack_sync':
            expected = self.ports.git('rev-list', '--reverse', f'{base}..{source_tip}').splitlines()
            requested = [self.ports.full_declared_commit(oid) for oid in stack['commits']]
            require(requested == expected and (expected or source_tip == base),
                    'source ref no longer matches the native stack sync history')
            require(not rebuilds or rebuilds[-1]['seq'] < latest['seq'],
                    'target source branch was rebuilt after the latest manifest sync')
        else:
            projected = (_receipt_payload(rebuilds[-1])['after_tip'] if rebuilds else
                         _receipt_payload(create_intents[0])['tip'] if create_intents else
                         stack['commits'][-1] if stack['commits'] else base)
            require(self.ports.full_declared_commit(projected) == source_tip,
                    'source ref no longer matches the native stack create or rebuild history')
        commits = sorted({self.ports.full_declared_commit(oid) for oid in candidates})
        return commits, {
            'create': create['seq'], 'latestSave': latest['seq'], 'headSave': prior_head,
            'generations': generations, 'rebuilds': [event['seq'] for event in rebuilds],
        }

    def plan(self, events):
        require(not pending_control_manifest_intents(events),
                'another control manifest intent is pending; inspect its native recovery first')
        ledger_segments = self.ports.ledger_segments()
        initial = self.observe()
        require(initial['status'] == [f' M {MANIFEST}'],
                'recover-stack requires the canonical manifest as the only unstaged checkout change')
        require(initial['indexEntriesDigest'] == self.ports.tree_entries_digest(initial['head']),
                'recover-stack requires an index that exactly matches HEAD')
        before_fp, source = observed_file(self.manifest_path)
        require(before_fp == initial['manifest'], 'manifest changed during withdrawal proof')
        head = normalized_manifest(self.ports.head_manifest(initial['head']), self.manifest_path)
        dirty = normalized_manifest(source, self.manifest_path, native_source=True)
        self.ports.require_mode(head, dirty, initial['branch'])
        self.ports.unresolved(self.stack_id, events)
        stack = _stack(dirty, self.stack_id)
        require(stack, f'unknown stack {self.stack_id!r} in the native dirty manifest')
        source_ref = f"refs/heads/{stack['branch']}"
        require(source_ref != initial['ref'], 'the stack source ref is also the primary integration ref')
        source_tip = self.ports.direct_ref(source_ref)
        require(source_tip, 'the native stack source ref is missing')
        _check_dependents(dirty, self.stack_id)
        desired = _without_stack(dirty, self.stack_id)
        require(desired == _without_stack(head, self.stack_id),
                'unrelated manifest fields differ between HEAD and the native dirty manifest')
        commits, evidence = self.history(dirty, head, stack, events, _stack(head, self.stack_id), source_tip)
        require(self.observe() == initial and self.ports.direct_ref(source_ref) == source_tip
                and self.events() == events and self.ports.ledger_segments() == ledger_segments,
                'withdrawal proof inputs changed during inspection')
        return {
            'schemaVersion': 1, 'kind': 'stackManifestRecoveryPlan', 'stack': self.stack_id,
            'reason': self.reason, 'manifestPath': str(self.manifest_path), 'beforeHead': initial['head'],
            'sourceRef': source_ref, 'sourceTip': source_tip, 'preservedCommits': commits,
            'manifestDigestBefore': manifest_digest(dirty), 'manifestDigestAfter': manifest_digest(desired),
            'ledgerEvidence': evidence, 'ledgerDigest': _ledger_digest(events),
            'ledgerCount': len(events), 'ledgerSegments': ledger_segments,
            'observation': initial,
        }, desired

    def observe(self, own_index_lock=None):
        return self.ports.observe(own_index_lock)

    def anchors(self, plan, token):
        prefix = f"refs/syncwheel/recovery/stack-withdraw/{self.stack_id}/{token}"
        return {
            **{f'{prefix}/commits/{oid}': oid for oid in plan['preservedCommits']},
            f'{prefix}/source': plan['sourceTip'],
        }

    def output(self, plan, token, *, applied=False, control_commit=None, pending=False):
        return {
            **plan, 'planDigest': token, 'anchorRefs': self.anchors(plan, token),
            'recoveryLocation': str(self.store / token), 'applied': applied,
            'pending': pending, 'controlCommit': control_commit,
        }

    def retry(self, token):
        return shlex.join([*RECOVERY_COMMAND.split(), self.stack_id, '--repo', str(self.root),
                           '--reason', self.reason, '--plan-digest', token, '--apply'])

    def select_intent(self, events, token):
        pending = pending_control_manifest_intents(events)
        require(len(pending) <= 1, 'unrelated or ambiguous control manifest intent is pending')
        if pending:
            event = pending[0]
            data = _receipt_payload(event)
            scope = data.get('stack_withdrawal') or {}
            require(data.get('command') == RECOVERY_COMMAND and scope.get('stack') == self.stack_id,
                    'unrelated control manifest intent is pending')
            require(data.get('reason') == 'stack_withdraw' and scope.get('reason') == self.reason,
                    'pending withdrawal requires the original --reason')
            require(token is None or data.get('operation_id') == token,
                    'pending withdrawal requires the original --plan-digest')
            return event
        if token:
            matching = [event for event in events if event['type'] == 'control_manifest_persistence_intent'
                        and _receipt_payload(event).get('operation_id') == token
                        and _receipt_payload(event).get('command') == RECOVERY_COMMAND]
            require(len(matching) <= 1, 'ambiguous recovered stack intent')
            if matching:
                return matching[0]
        return None

    def complete_receipt(self, events, token):
        matched = [event for event in events if event['type'] == 'manifest_saved'
                   and _receipt_payload(event).get('operation_id') == token
                   and _receipt_payload(event).get('command') == RECOVERY_COMMAND]
        require(len(matched) <= 1, 'ambiguous terminal stack withdrawal receipt')
        return matched[0] if matched else None

    def durable_directory(self, token, *, create=False):
        for directory in (self.store.parent, self.store):
            if create and not os.path.lexists(directory):
                directory.mkdir(mode=0o700)
                fsync_directory_path(directory.parent)
            if os.path.lexists(directory):
                directory_key(directory)
        if self.store.exists():
            require(self.store.stat().st_mode & 0o077 == 0, 'stack withdrawal recovery is not private')
        destination = self.store / token
        if os.path.lexists(destination):
            directory_key(destination)
            require(destination.stat().st_mode & 0o077 == 0, 'stack withdrawal operation is not private')
        return destination

    def prepare(self, plan, desired, token, control_commit):
        destination = self.durable_directory(token, create=True)
        if destination.exists():
            prepared = self.read_prepared(destination, plan, control_commit)
            require(self.observe() == plan['observation'] and not self.ports.pending_index_lock(),
                    'prepared withdrawal source changed before its durable intent')
            return prepared
        old_index, backup = observed_file(self.ports.index)
        require(old_index == plan['observation']['index'], 'index changed before recovery preparation')
        with tempfile.TemporaryDirectory(prefix='.stack-withdraw-', dir=self.store) as td:
            sandbox = Path(td)
            ready = sandbox / 'ready'
            ready.mkdir(mode=0o700)
            require(ready.stat().st_dev == self.ports.index.parent.stat().st_dev
                    == self.manifest_path.parent.stat().st_dev,
                    'stack withdrawal requires one filesystem for recovery, manifest, and index')
            index_temp = sandbox / 'prepared-index'
            self.ports.build_index(control_commit, index_temp)
            install_bytes(ready / 'index.clean', index_temp.read_bytes(),
                          stat.S_IMODE(old_index['stat'][2]))
            install_bytes(ready / 'index.original', backup)
            payload = canonical_manifest_file_text(desired).encode()
            require(payload == self.ports.head_manifest(control_commit),
                    'prepared withdrawal manifest differs from the native control commit')
            install_bytes(ready / 'manifest.clean', payload,
                          stat.S_IMODE(plan['observation']['manifest']['stat'][2]))
            expected_index, _ = observed_file(ready / 'index.clean')
            expected_manifest, _ = observed_file(ready / 'manifest.clean')
            record = {'planDigest': token, 'plan': plan, 'controlCommit': control_commit,
                      'cleanIndex': _without_ctime(expected_index),
                      'cleanManifest': _without_ctime(expected_manifest)}
            install_bytes(ready / 'prepared.json', (json.dumps(record, sort_keys=True) + '\n').encode())
            fsync_directory_path(ready)
            fsync_directory_path(sandbox)
            with opened_directory(sandbox) as source, opened_directory(self.store) as target:
                rename_new(source, ready.name, target, destination.name)
                os.fsync(source)
                os.fsync(target)
        return self.read_prepared(destination, plan, control_commit)

    def read_prepared(self, dest, plan, control_commit):
        _, raw = observed_file(dest / 'prepared.json')
        require(raw is not None, 'missing durable prepared stack withdrawal record')
        prepared = _unique_json(raw, 'prepared stack withdrawal record')
        require(prepared['plan'] == plan and prepared['planDigest'] == canonical_json_digest(plan)
                and prepared['controlCommit'] == control_commit, 'prepared withdrawal plan or control commit changed')
        original, _ = observed_file(dest / 'index.original')
        require(original and original['sha256'] == plan['observation']['index']['sha256'],
                'stored original index failed recovery verification')
        require(prepared['cleanManifest']['sha256'] == hashlib.sha256(
            self.ports.head_manifest(control_commit)).hexdigest(), 'stored clean manifest does not match control commit')
        return prepared

    def manifest_state(self, dest, prepared, fd, leaf):
        plan = prepared['plan']
        original = plan['observation']['manifest']
        clean = prepared['cleanManifest']
        visible, _ = observed_file(Path(f'/proc/self/fd/{fd}') / leaf)
        parked, _ = observed_file(dest / 'manifest.original')
        replacement, _ = observed_file(dest / 'manifest.clean')
        require(replacement is None or _without_ctime(replacement) == clean,
                'prepared manifest changed before checkout alignment')
        if parked is None and visible == original and replacement is not None:
            return 'original'
        require(_without_ctime(parked) == _without_ctime(original),
                'original manifest quarantine proof changed')
        if visible is None and replacement is not None:
            return 'quarantined'
        if replacement is None and _without_ctime(visible) == clean:
            return 'clean'
        raise SyncwheelError('new or changed manifest found during recovery; preserve it and inspect recovery')

    def phase(self, prepared, dest):
        planned = prepared['plan']['observation']
        observed = self.observe(dest / 'index.clean')
        require(all(observed[name] == planned[name] for name in ('branch', 'ref', 'commonIdentity',
                    'rootIdentity', 'registry', 'manifestParents')), 'primary or lane registration changed during withdrawal')
        require(not any(item[3:] != MANIFEST for item in observed['status']),
                'unrelated checkout changes appeared during withdrawal')
        with checked_parent(self.root, MANIFEST, planned['manifestParents']) as (fd, leaf, _):
            state = self.manifest_state(dest, prepared, fd, leaf)
        index_state = ('original' if observed['index'] == planned['index'] else
                       'clean' if _without_ctime(observed['index']) == prepared['cleanIndex'] else 'foreign')
        require(index_state != 'foreign', 'index changed since the reviewed withdrawal plan')
        control = prepared['controlCommit']
        before = planned['head']
        require(observed['head'] in {before, control}, 'primary HEAD changed outside the withdrawal')
        if observed['head'] != control:
            require(state == 'original' and index_state == 'original'
                    and observed['status'] == planned['status'], 'checkout changed before the control ref CAS')
        if index_state == 'clean':
            require(state == 'clean' and observed['indexEntriesDigest'] == self.ports.tree_entries_digest(control)
                    and not observed['status'], 'withdrawal index or manifest has not aligned to the control commit')
        else:
            require(observed['indexEntriesDigest'] == planned['indexEntriesDigest'], 'original index entries changed')
        require(self.ports.direct_ref(prepared['plan']['sourceRef']) == prepared['plan']['sourceTip'],
                'stack source ref changed during withdrawal')
        self.ports.require_unpublished_tracking(prepared['plan']['sourceRef'].removeprefix('refs/heads/'))
        return observed['head'], index_state, state

    def take_index_lock(self, dest, prepared):
        backing = dest / 'index.clean'
        lock = self.ports.index.with_name('index.lock')
        ownership = dest / 'index-owner.json'
        if os.path.lexists(lock):
            require(same_inode(backing, lock), 'Git index is locked by another writer')
            _, raw = observed_file(ownership)
            previous = _unique_json(raw, 'withdrawal index lock owner') if raw else None
            require(previous and previous.get('planDigest') == prepared['planDigest']
                    and previous.get('host') == socket.gethostname()
                    and previous.get('indexIdentity') == prepared['cleanIndex']['stat'][:2]
                    and previous.get('process_start_time')
                    and self.ports.stale_lock_reason(previous) in {'pid_not_alive', 'process_zombie'},
                    'cannot prove the interrupted index lock belongs to a stopped withdrawal writer')
        owner = self.ports.lock_metadata()
        require(owner['process_start_time'], 'cannot prove the withdrawal index writer identity')
        owner.update(planDigest=prepared['planDigest'], host=socket.gethostname(),
                     indexIdentity=prepared['cleanIndex']['stat'][:2])
        self.ports.write_private_json(ownership, owner)
        if not os.path.lexists(lock):
            os.link(backing, lock, follow_symlinks=False)
            fsync_directory_path(lock.parent)
        require(same_inode(backing, lock), 'Git index lock changed while claiming it')
        return lock, backing

    def synchronize(self, plan, prepared, dest, *, publish):
        control = prepared['controlCommit']
        token = prepared['planDigest']
        head, index_state, _ = self.phase(prepared, dest)
        anchors = self.anchors(plan, token)
        if index_state == 'clean':
            self.ports.verify_anchors(anchors)
            backing = dest / 'index.clean'
            if same_inode(backing, self.ports.index):
                backing.unlink()
                fsync_directory_path(dest)
            return
        lock, backing = self.take_index_lock(dest, prepared)
        try:
            require(_without_ctime(observed_file(backing, allow_link=True)[0]) == prepared['cleanIndex'],
                    'prepared clean index changed')
            current_head, current_index, _ = self.phase(prepared, dest)
            require(current_head == head and current_index == 'original',
                    'primary ref or index changed after taking the index lock')
            if head != control:
                require(publish, 'pending withdrawal has no authorized control ref transaction')
                self.ports.publish_refs(plan, control, anchors, lambda: self.phase(prepared, dest))
                stack_recovery_checkpoint('refs-updated')
            self.ports.verify_anchors(anchors)
            require(self.phase(prepared, dest)[0] == control, 'control ref CAS did not take effect')
            parents = plan['observation']['manifestParents']
            with opened_directory(dest) as safe:
                with checked_parent(self.root, MANIFEST, parents) as (parent, leaf, _):
                    state = self.manifest_state(dest, prepared, parent, leaf)
                    if state == 'original':
                        rename_new(parent, leaf, safe, 'manifest.original')
                        os.fsync(parent)
                        os.fsync(safe)
                        require(self.manifest_state(dest, prepared, parent, leaf) == 'quarantined',
                                'manifest changed while moving it to recovery')
                        stack_recovery_checkpoint('manifest-quarantined')
                with checked_parent(self.root, MANIFEST, parents) as (parent, leaf, _):
                    if self.manifest_state(dest, prepared, parent, leaf) == 'quarantined':
                        rename_new(safe, 'manifest.clean', parent, leaf)
                        os.fsync(parent)
                        os.fsync(safe)
                        stack_recovery_checkpoint('manifest-aligned')
            require(self.phase(prepared, dest) == (control, 'original', 'clean')
                    and same_inode(backing, lock), 'manifest or index changed before index alignment')
            os.replace(lock, self.ports.index)
            fsync_directory_path(lock.parent)
            stack_recovery_checkpoint('index-aligned')
            require(self.phase(prepared, dest) == (control, 'clean', 'clean'),
                    'primary checkout failed final withdrawal alignment')
        finally:
            if same_inode(backing, lock):
                lock.unlink()
                fsync_directory_path(lock.parent)
        if same_inode(backing, self.ports.index):
            backing.unlink()
            fsync_directory_path(dest)

    def checkpoint(self, stage):
        stack_recovery_checkpoint(stage)

    def require_fresh_observation(self, plan):
        require(self.observe() == plan['observation']
                and self.ports.direct_ref(plan['sourceRef']) == plan['sourceTip'],
                'stack withdrawal source changed before its durable control intent')
        self.ports.require_unpublished_tracking(plan['sourceRef'].removeprefix('refs/heads/'))
        self.require_ledger(plan, self.events(), canonical_json_digest(plan))

    def require_ledger(self, plan, events, token):
        count = plan['ledgerCount']
        require(len(events) >= count and _ledger_digest(events[:count]) == plan['ledgerDigest'],
                'native ledger prefix changed since the reviewed withdrawal')
        suffix = events[count:]
        require(len(suffix) <= 2, 'unrelated native ledger events followed the withdrawal plan')
        for event, kind in zip(suffix, ('control_manifest_persistence_intent', 'manifest_saved')):
            payload = _receipt_payload(event)
            require(event['type'] == kind and payload.get('operation_id') == token
                    and payload.get('command') == RECOVERY_COMMAND
                    and payload.get('reason') == 'stack_withdraw',
                    'unrelated native ledger event followed the withdrawal plan')
        additions = b''
        baseline = plan['ledgerSegments']
        current = self.ports.ledger_segments(raw=True)
        for name, entry in baseline.items():
            image = current.get(name)
            require(image is not None and len(image) >= entry['size']
                    and hashlib.sha256(image[:entry['size']]).hexdigest() == entry['sha256'],
                    f'native ledger segment changed since preview: {name}')
        for name, image in sorted(current.items()):
            additions += image[baseline.get(name, {}).get('size', 0):]
        expected = b''.join((json.dumps(event, sort_keys=True) + '\n').encode() for event in suffix)
        require(additions == expected, 'native ledger bytes outside the reviewed withdrawal changed')

    def plan_from_intent(self, intent, token):
        data = _receipt_payload(intent)
        scope = data['stack_withdrawal']
        plan = scope['plan']
        desired = scope['desired']
        require(data['operation_id'] == token == canonical_json_digest(plan)
                and scope['stack'] == plan['stack'] == self.stack_id
                and scope['reason'] == plan['reason'] == self.reason
                and plan['manifestPath'] == str(self.manifest_path)
                and data['integration_branch'] == plan['observation']['branch']
                and data['replay_tip'] == plan['beforeHead']
                and data['source_manifest_digest'] == plan['manifestDigestBefore']
                and data['expected_manifest_digest'] == plan['manifestDigestAfter'] == manifest_digest(desired)
                and _stack(desired, self.stack_id) is None,
                'persisted withdrawal plan does not match the requested recovery')
        return plan, desired

    def prepared_from_intent(self, intent, plan, token):
        payload = _receipt_payload(intent)
        prepared = self.read_prepared(self.durable_directory(token), plan, payload['expected_control_commit'])
        require(canonical_json_digest(prepared) == payload['stack_withdrawal']['preparedDigest'],
                'prepared withdrawal fingerprint no longer matches its native intent')
        return prepared

    def verify_completed(self, plan, prepared, terminal):
        require(terminal is not None, 'missing completed withdrawal ledger receipt')
        payload = _receipt_payload(terminal)
        require(payload.get('control_commit') == prepared['controlCommit']
                and payload.get('manifest_hash') == plan['manifestDigestAfter']
                and (payload.get('context') or {}).get('plan_digest') == prepared['planDigest'],
                'terminal withdrawal ledger receipt does not match the reviewed plan')
        self.require_ledger(plan, self.events(), prepared['planDigest'])
        self.ports.verify_anchors(self.anchors(plan, prepared['planDigest']))
        observed = self.observe()
        require(observed['head'] == prepared['controlCommit']
                and observed['indexEntriesDigest'] == self.ports.tree_entries_digest(prepared['controlCommit'])
                and (observed['manifest'] or {}).get('sha256') == prepared['cleanManifest']['sha256']
                and not observed['status']
                and self.ports.direct_ref(plan['sourceRef']) == plan['sourceTip'],
                'completed withdrawal checkout or source has changed; inspect without retrying writes')

    def execute_stack_withdrawal(self, *, apply, token):
        require(bool(self.reason), 'manifest recover-stack requires a nonempty --reason')
        if apply:
            require(re.fullmatch(r'[0-9a-f]{64}', token or '') is not None,
                    'manifest recover-stack --apply requires the exact reviewed --plan-digest')
        else:
            require(token is None, '--plan-digest requires --apply')
        events = self.events()
        intent = self.select_intent(events, token)
        if intent is None:
            plan, desired = self.plan(events)
            current = canonical_json_digest(plan)
            require(not apply or token == current, 'stack withdrawal plan changed; obtain a fresh preview')
            token = current
        else:
            token = _receipt_payload(intent)['operation_id']
            plan, desired = self.plan_from_intent(intent, token)
            self.require_ledger(plan, events, token)
            prepared = self.prepared_from_intent(intent, plan, token)
            if self.complete_receipt(events, token) is None:
                self.phase(prepared, self.durable_directory(token))
        if not apply:
            return self.output(plan, token, pending=intent is not None,
                               control_commit=_receipt_payload(intent)['expected_control_commit'] if intent else None)
        completed = self.complete_receipt(events, token)
        if completed is not None:
            self.verify_completed(plan, prepared, completed)
            self.ports.refresh_checkpoint(self, plan, token)
            return self.output(plan, token, applied=True, control_commit=prepared['controlCommit'])
        try:
            control_commit = self.ports.persist_stack_withdrawal(self, plan, desired, token, intent)
        except (SyncwheelError, OSError) as exc:
            if self.durable_directory(token).exists():
                raise SyncwheelError(
                    f'{exc}; withdrawal recovery retained at {self.store / token}; '
                    f'inspect and resume with: {self.retry(token)}'
                ) from exc
            raise
        return self.output(plan, token, applied=True, control_commit=control_commit)
