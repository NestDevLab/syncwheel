"""Durable reservations for reviewed cooperative test jobs.

The caller supplies the existing JSON CAS store, repository observation and
native service manager. This module never acquires a publication/manifest lock,
launches a command implicitly during recovery, or treats unknown as idle.
"""

import copy
import hashlib
import json
import re
import time
import uuid

from syncwheel_contracts import CHANNEL_PLAN_SCHEMA_VERSION
from syncwheel_errors import SyncwheelError
from syncwheel_manifest import (
    canonical_json_digest, channel_composition_digest, channel_pin_digest,
    manifest_digest, normalize_manifest_data,
)
from syncwheel_ledger import manifest_channel_history_summary


def document_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def derive_channel_request(inputs, workspace, *, read_manifest_blob,
                           validate_channel, observe_tools):
    """Derive facts from an immutable control blob and the existing channel plan.

    This proves object/content provenance, not a forge approval or authorship.
    Observations about moving refs, external checks and people stay reported.
    """
    if not isinstance(inputs, dict) or set(inputs) - {
        'channel_plan', 'manifest_blob', 'argv', 'environment', 'observations'
    }:
        raise SyncwheelError('test request accepts only channel_plan, manifest_blob, argv, environment and observations')
    plan = inputs.get('channel_plan')
    if not isinstance(plan, dict) or plan.get('kind') != 'channelPlan' or (type(plan.get('schemaVersion')) is not int or plan.get('schemaVersion') != CHANNEL_PLAN_SCHEMA_VERSION):
        raise SyncwheelError('test request needs the existing channel plan schema')
    if plan.get('operation') != 'apply':
        raise SyncwheelError('test request needs a local channel materialization plan')
    body = {key: value for key, value in plan.items() if key not in {'planDigest', 'operationId'}}
    if canonical_json_digest(body) != plan.get('planDigest'):
        raise SyncwheelError('reviewed channel plan digest does not match its payload')
    blob = inputs.get('manifest_blob')
    if not isinstance(blob, str) or not re.fullmatch('[0-9a-f]{40}', blob):
        raise SyncwheelError('test request needs an immutable manifest blob object id')
    raw = read_manifest_blob(blob)
    try:
        manifest = normalize_manifest_data(json.loads(raw), '<test-slot-manifest-blob>')
    except (ValueError, UnicodeDecodeError) as error:
        raise SyncwheelError('test manifest blob is not valid JSON') from error
    if manifest_digest(manifest) != plan.get('manifestDigest'):
        raise SyncwheelError('channel plan does not match its immutable control manifest')
    channels = [channel for channel in manifest.get('channels', []) if channel['id'] == plan.get('channel')]
    if len(channels) != 1:
        raise SyncwheelError('channel plan does not name one channel in its control manifest')
    channel = channels[0]
    expected = {'branch': channel['branch'], 'base': channel['base'],
                'baseRevision': channel['baseRevision'], 'composition': channel['composition'],
                'pinDigest': channel_pin_digest(channel),
                'compositionDigest': channel_composition_digest(channel)}
    if any(plan.get(key) != value for key, value in expected.items()):
        raise SyncwheelError('channel plan pins differ from the verified manifest channel')
    before = plan.get('before')
    if not isinstance(before, dict) or before.get('channel') != manifest_channel_history_summary(channel):
        raise SyncwheelError('channel plan snapshot differs from its verified channel recipe')
    validate_channel(manifest, channel)
    argv = inputs.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and '\0' not in arg for arg in argv):
        raise SyncwheelError('test argv must be a nonempty literal string list')
    environment = inputs.get('environment', {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and key and '=' not in key and '\0' not in key
        and isinstance(value, str) and '\0' not in value
        for key, value in environment.items()
    ):
        raise SyncwheelError('test environment must contain literal string names and values')
    forbidden = [key for key in environment if key.startswith(('GIT_', 'LD_', 'PYTHON'))
                 or key in {'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'}]
    if forbidden:
        raise SyncwheelError('test environment cannot select Git, loader, Python or manager namespaces')
    tools = observe_tools(argv)
    request = {'workspace': copy.deepcopy(workspace), 'plan_digest': plan['planDigest'],
               'composition_digest': expected['compositionDigest'], 'base': channel['baseRevision'],
               'sources': copy.deepcopy(channel['composition']), 'candidate': workspace['head'],
               'tree': workspace['tree'], 'argv': list(argv), 'environment': dict(environment),
               'tools': tools,
               'verified_provenance': {'manifest_blob': blob, 'manifest_digest': manifest_digest(manifest),
                                       'channel': copy.deepcopy(channel), 'plan_digest_checked': True},
               'reported_observations': {'caller': copy.deepcopy(inputs.get('observations', {})),
                   'channel_plan': copy.deepcopy(plan),
                   'origin_or_review_approval_verified': False}}
    return request


def terminal_observation(binding, observation, *, cgroup_exists, cgroup_populated):
    """Qualify one retained invocation, including an explicitly retired cgroup."""
    required = ('unit', 'invocation', 'configuration', 'cgroup')
    if not all(binding.get(key) for key in required):
        return None
    if any(observation.get(key) != binding[key] for key in required[:3]):
        return None
    if observation.get('job') != '' or observation.get('main_pid') != 0:
        return None
    outcome = observation.get('result')
    state = (observation.get('active'), observation.get('substate'))
    if outcome == 'success':
        code, status = observation.get('main_code'), observation.get('main_status')
        normal_exit = code == 1 and status == 0
        signaled = code in (2, 3) and type(status) is int and 0 < status <= 64
        if state != ('active', 'exited') or not (normal_exit or signaled):
            return None
        # systemd treats some termination signals as clean service outcomes.
        # They prove cessation once the cgroup is empty, never a passing test.
        if signaled:
            outcome = 'signal'
    elif state != ('failed', 'failed') or not outcome or observation.get('main_code') is None or observation.get('main_status') is None:
        return None
    current_cgroup = observation.get('cgroup')
    if current_cgroup:
        if current_cgroup != binding['cgroup'] or cgroup_exists is not True or cgroup_populated is not False:
            return None
    elif current_cgroup != '' or cgroup_exists is not False or cgroup_populated is not None:
        # Retirement is accepted only for the exact path durably bound while
        # alive, and only alongside the same retained terminal invocation.
        return None
    return {'outcome': outcome, 'passed': outcome == 'success',
            'main_code': observation['main_code'], 'main_status': observation['main_status'],
            'retired_cgroup': current_cgroup == ''}


class CooperativeTestSlot:
    """One physical workspace, one reservation; short JSON transactions only.

    store.load() returns a JsonSnapshot. store.save(snapshot) uses the canonical
    exact-byte CAS writer and cooperative lock. observe_workspace() must return
    the canonical admin identity, physical directory identity and exact inputs.
    """

    def __init__(self, store, observe_workspace):
        self.store = store
        self.observe_workspace = observe_workspace

    def reserve(self, request):
        snapshot = self.store.load()
        if snapshot.get('active') is not None:
            raise SyncwheelError('test slot is occupied; reconcile its recorded operation')
        workspace = self.observe_workspace()
        if not workspace.get('clean'):
            raise SyncwheelError('test workspace must be clean before reservation')
        if request.get('workspace') != workspace:
            raise SyncwheelError('test workspace or candidate changed before reservation')
        for field in ('plan_digest', 'composition_digest', 'base', 'candidate', 'tree', 'argv', 'tools'):
            if not request.get(field):
                raise SyncwheelError(f'test request is missing {field}')
        if not isinstance(request.get('sources'), list):
            raise SyncwheelError('test request sources must be an explicit list, possibly empty')
        if request['candidate'] != workspace.get('head') or request['tree'] != workspace.get('tree'):
            raise SyncwheelError('test request does not describe the exact workspace candidate')
        argv = request['argv']
        if not isinstance(argv, list) or not all(isinstance(arg, str) and '\0' not in arg for arg in argv):
            raise SyncwheelError('test argv must be a nonempty literal string list')
        operation = {'nonce': uuid.uuid4().hex, 'state': 'reserved',
                     'reserved_monotonic': time.monotonic(),
                     'request': copy.deepcopy(request), 'request_digest': document_digest(request)}
        snapshot['active'] = operation
        self.store.save(snapshot)
        return copy.deepcopy(operation)

    def _operation(self, snapshot, nonce):
        operation = snapshot.get('active')
        if not operation or operation.get('nonce') != nonce:
            raise SyncwheelError('test slot reservation changed; refusing stale operation')
        return operation

    def starting(self, nonce, unit, configuration, *, verification=None):
        snapshot = self.store.load()
        operation = self._operation(snapshot, nonce)
        if operation['state'] != 'reserved':
            raise SyncwheelError('test job was already dispatched or is uncertain; observe without redispatch')
        if operation['request']['workspace'] != self.observe_workspace():
            raise SyncwheelError('test workspace changed before dispatch')
        if not unit.endswith(nonce + '.service') or not configuration:
            raise SyncwheelError('test unit must bind the reservation nonce and exact configuration')
        operation.update(state='starting', unit=unit, configuration=copy.deepcopy(configuration),
                         starting_monotonic=time.monotonic(),
                         verification=copy.deepcopy(verification))
        self.store.save(snapshot)
        return copy.deepcopy(operation)

    def bind(self, nonce, observation):
        snapshot = self.store.load()
        operation = self._operation(snapshot, nonce)
        if operation['state'] != 'starting':
            raise SyncwheelError('test job is not awaiting its initial native binding')
        if observation.get('unit') != operation['unit'] or observation.get('configuration') != operation['configuration']:
            raise SyncwheelError('native test unit does not match reviewed dispatch')
        if not observation.get('invocation') or not observation.get('cgroup'):
            raise SyncwheelError('native invocation and live cgroup must be bound before running the test')
        operation.update(state='running', bound_monotonic=time.monotonic(), binding={key: copy.deepcopy(observation[key])
                         for key in ('unit', 'invocation', 'configuration', 'cgroup')})
        self.store.save(snapshot)
        return copy.deepcopy(operation)

    def cancel_reserved(self, nonce):
        snapshot = self.store.load()
        operation = self._operation(snapshot, nonce)
        if operation['state'] != 'reserved':
            raise SyncwheelError('only an undispatched reserved test job can be cancelled')
        snapshot['active'] = None
        self.store.save(snapshot)

    def finish(self, nonce, observation, *, cgroup_exists, cgroup_populated, log_digest, measurements, inputs_unchanged=True, verification=None):
        snapshot = self.store.load()
        previous = snapshot.get('receipts', {}).get(nonce)
        if previous is not None:
            # Never touch a newer active generation while returning an earlier
            # operation's already committed receipt.
            return copy.deepcopy(previous)
        operation = self._operation(snapshot, nonce)
        if operation['state'] != 'running':
            raise SyncwheelError('test job lacks a durable running binding; keep reservation')
        result = terminal_observation(operation['binding'], observation,
                                      cgroup_exists=cgroup_exists, cgroup_populated=cgroup_populated)
        if result is None:
            raise SyncwheelError('test job is running or unknown; keep reservation')
        if not log_digest:
            raise SyncwheelError('test receipt requires the collected log digest')
        after = self.observe_workspace()
        receipt = {'nonce': nonce, 'request': copy.deepcopy(operation['request']),
                   'request_digest': operation['request_digest'], 'binding': copy.deepcopy(operation['binding']),
                   'terminal': copy.deepcopy(observation), 'result': result,
                   'candidate_unchanged': after == operation['request']['workspace'],
                   'log_digest': log_digest, 'measurements': copy.deepcopy(measurements),
                   'verification': copy.deepcopy(verification if verification is not None else operation.get('verification')),
                   'timing': {'reservation_to_dispatch_seconds': operation['starting_monotonic'] - operation['reserved_monotonic'],
                              'dispatch_to_binding_seconds': operation['bound_monotonic'] - operation['starting_monotonic'],
                              'occupation_seconds': time.monotonic() - operation['reserved_monotonic']},
                   'inputs_unchanged': inputs_unchanged is True}
        receipt['qualified'] = result['passed'] and receipt['candidate_unchanged'] and receipt['inputs_unchanged']
        # Receipt persistence and physical slot release are one CAS document
        # mutation: recovery returns that receipt without another dispatch.
        snapshot.setdefault('receipts', {})[nonce] = receipt
        snapshot['active'] = None
        self.store.save(snapshot)
        return copy.deepcopy(receipt)
