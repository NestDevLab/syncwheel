"""Durable reservations for reviewed cooperative test jobs.

The caller supplies the existing JSON CAS store, repository observation and
native service manager. This module never acquires a publication/manifest lock,
launches a command implicitly during recovery, or treats unknown as idle.
"""

import copy
import hashlib
import json
import uuid

from syncwheel_errors import SyncwheelError


def document_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


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
        if state != ('active', 'exited') or observation.get('main_code') != 1 or observation.get('main_status') != 0:
            return None
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
        for field in ('plan_digest', 'composition_digest', 'base', 'sources', 'candidate', 'tree', 'argv', 'tools'):
            if not request.get(field):
                raise SyncwheelError(f'test request is missing {field}')
        if request['candidate'] != workspace.get('head') or request['tree'] != workspace.get('tree'):
            raise SyncwheelError('test request does not describe the exact workspace candidate')
        argv = request['argv']
        if not isinstance(argv, list) or not all(isinstance(arg, str) and '\0' not in arg for arg in argv):
            raise SyncwheelError('test argv must be a nonempty literal string list')
        operation = {'nonce': uuid.uuid4().hex, 'state': 'reserved',
                     'request': copy.deepcopy(request), 'request_digest': document_digest(request)}
        snapshot['active'] = operation
        self.store.save(snapshot)
        return copy.deepcopy(operation)

    def _operation(self, snapshot, nonce):
        operation = snapshot.get('active')
        if not operation or operation.get('nonce') != nonce:
            raise SyncwheelError('test slot reservation changed; refusing stale operation')
        return operation

    def starting(self, nonce, unit, configuration):
        snapshot = self.store.load()
        operation = self._operation(snapshot, nonce)
        if operation['state'] != 'reserved':
            raise SyncwheelError('test job was already dispatched or is uncertain; observe without redispatch')
        if operation['request']['workspace'] != self.observe_workspace():
            raise SyncwheelError('test workspace changed before dispatch')
        if not unit.endswith(nonce + '.service') or not configuration:
            raise SyncwheelError('test unit must bind the reservation nonce and exact configuration')
        operation.update(state='starting', unit=unit, configuration=copy.deepcopy(configuration))
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
        operation.update(state='running', binding={key: copy.deepcopy(observation[key])
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

    def finish(self, nonce, observation, *, cgroup_exists, cgroup_populated, log_digest, measurements):
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
                   'log_digest': log_digest, 'measurements': copy.deepcopy(measurements)}
        receipt['qualified'] = result['passed'] and receipt['candidate_unchanged']
        # Receipt persistence and physical slot release are one CAS document
        # mutation: recovery returns that receipt without another dispatch.
        snapshot.setdefault('receipts', {})[nonce] = receipt
        snapshot['active'] = None
        self.store.save(snapshot)
        return copy.deepcopy(receipt)
