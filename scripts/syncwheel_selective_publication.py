"""Bound ref validation for selective version 2 compose operations only."""

from syncwheel_coordination import unchanged_foreign_history_scope, stack_snapshot_map
from syncwheel_errors import SyncwheelError
from syncwheel_manifest import canonical_json_digest, stack_base_owner_id


def compose_ref_validation_scope(manifest, snapshot, state, stack_id):
    if manifest['version'] != 2 or not state:
        return None
    stacks = manifest['stacks']
    remote = state['manifest']
    foreign = unchanged_foreign_history_scope(
        remote, snapshot, stack_id, localized_stacks=stacks)
    remote_stacks = stack_snapshot_map(remote)
    deferred = {f"refs/heads/{remote_stacks[key]['branch']}":
                state['managed_refs'].get(f"refs/heads/{remote_stacks[key]['branch']}")
                for key in foreign}
    required_stacks = [stack for stack in stacks if stack['id'] not in foreign]
    integration_branch = manifest['integration']['branch']
    integration_ref = f'refs/heads/{integration_branch}'
    anchor = state['managed_refs'].get(integration_ref)
    if not anchor:
        raise SyncwheelError('selective compose requires an existing historical control anchor')
    # An explicitly selected base on integration is a real dependency. Otherwise
    # the unchanged integration tip is historical control evidence, not staging.
    # The synthetic owner cannot collide with any declared string stack ID.
    anchor_owner = {'id': object(), 'branch': integration_branch}
    if not any(stack_base_owner_id(stack, [anchor_owner]) is not None
               for stack in required_stacks):
        deferred[integration_ref] = anchor
    selected = next((stack for stack in stacks if stack['id'] == stack_id), None)
    if selected is None:
        raise SyncwheelError('selective compose target is not declared')
    source_ref = f"refs/heads/{selected['branch']}"
    required_refs = sorted((set(state['managed_refs']) | {source_ref}) - set(deferred))
    return {
        'schemaVersion': 1, 'kind': 'compose-v2-selected', 'stack': stack_id,
        'requiredStackIds': sorted(stack['id'] for stack in required_stacks),
        'requiredRefs': required_refs, 'preservedRefs': dict(sorted(deferred.items())),
        'preservedSnapshotDigest': canonical_json_digest(
            {key: remote_stacks[key] for key in foreign}),
        'integrationAnchor': {'ref': integration_ref, 'tip': anchor},
    }


def require_compose_ref_validation_scope(scope, manifest, snapshot, state, operation):
    if scope is None:
        return
    if (not isinstance(scope, dict)
            or operation != 'compose-stack:' + str(scope.get('stack'))
            or scope != compose_ref_validation_scope(manifest, snapshot, state, scope.get('stack'))):
        raise SyncwheelError('coordination compose STOP: reviewed plan drifted at ref validation scope')


def required_ref_values(values, scope):
    """Preserved refs remain in the state; their current observations are separate."""
    if scope is None:
        return dict(values)
    return {ref: tip for ref, tip in values.items() if ref not in scope['preservedRefs']}


def foreign_ref_observations(scope, observed):
    if scope is None:
        return {}
    return {ref: {'recorded': tip, 'observed': observed.get(ref),
                  'status': ('missing' if observed.get(ref) is None else
                             'unchanged' if observed.get(ref) == tip else 'diverged'),
                  'qualified': False}
            for ref, tip in scope['preservedRefs'].items()}


def same_compose_publication_plan(current, reviewed):
    """Foreign observations are diagnostic, never leases over untouched refs."""
    if current.get('refValidationScope') is None or reviewed.get('refValidationScope') is None:
        return current == reviewed
    ignored = {'planDigest', 'foreignRefObservations'}
    return ({key: value for key, value in current.items() if key not in ignored}
            == {key: value for key, value in reviewed.items() if key not in ignored})


def local_projection_observation(scope, published_tip, local_tip, *, commit_available, prove_suffix):
    """A local test projection is not authority for a selected source publication.

    The local control write still needs an existing HEAD and exact HEAD/manifest
    CAS. This observation grants no permission to align or publish integration.
    """
    if scope is None:
        suffix = prove_suffix(published_tip, local_tip)
    else:
        if not local_tip or not commit_available(local_tip):
            raise SyncwheelError('coordination compose local metadata control target is unavailable')
        try:
            suffix = prove_suffix(published_tip, local_tip)
        except SyncwheelError:
            return {'status': 'unqualified', 'controlSuffix': None}
    return {'status': 'manifest-control-suffix' if suffix else 'historical-anchor',
            'controlSuffix': suffix}
