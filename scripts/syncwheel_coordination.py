"""Coordination snapshot validation, portable references and proposal composition.

Remote observation and publication remain outside this domain. Git ref validation
is a named caller-supplied operation, keeping transport and subprocesses out.
"""

import copy
import json
import re

from syncwheel_manifest import canonical_json_digest
from syncwheel_errors import SyncwheelError
from syncwheel_manifest import (
    validate_stack_adoption,
    CHANNEL_LIFECYCLES,
    MANIFEST_VERSION_CHANNELS,
    channel_pin_digest,
    normalize_channel_entry,
    normalize_channel_resolution,
    normalize_channel_timestamp,
    normalize_derived_provenance,
    normalize_landing_policy,
    validate_stack_dependency_graph,
)

COORDINATION_STATE_SCHEMA_VERSION = 2

COORDINATION_STATE_SCHEMA_VERSION_CHANNELS = 3

COORDINATION_STATE_SCHEMA_VERSION_RESOLUTIONS = 4

COORDINATION_STATE_SCHEMA_VERSION_ADOPTIONS = 5

COORDINATION_REMOTE_ROLE_CANONICAL = 'canonical'

COORDINATION_REMOTE_ROLE_PUBLICATION = 'publication'


def coordination_public_remote_ref_parts(value, path, *, branch_is_valid):
    """Validate and unpack a typed public remote ref, if present."""
    if isinstance(value, str):
        if not value:
            raise SyncwheelError(f'{path} must be a non-empty ref string')
        return None
    if not isinstance(value, dict):
        raise SyncwheelError(f'{path} must be a ref string or typed remote ref')
    if set(value) != {'kind', 'role', 'ref'} or value.get('kind') != 'remote-ref':
        raise SyncwheelError(f'{path} contains an invalid typed remote ref')
    role = value.get('role')
    ref = value.get('ref')
    if role not in {
        COORDINATION_REMOTE_ROLE_CANONICAL,
        COORDINATION_REMOTE_ROLE_PUBLICATION,
    } or not branch_is_valid(ref):
        raise SyncwheelError(f'{path} contains an invalid typed remote ref')
    return role, ref


def local_coordination_ref(value, defaults, *, branch_is_valid):
    """Map a portable public ref back to this checkout's remote role."""
    canonical_remote = defaults['canonical_remote']
    publication_remote = defaults['publication_remote']
    remote_ref = coordination_public_remote_ref_parts(value, 'coordination state ref', branch_is_valid=branch_is_valid)
    if remote_ref:
        role, ref = remote_ref
        remote = {
            COORDINATION_REMOTE_ROLE_CANONICAL: canonical_remote,
            COORDINATION_REMOTE_ROLE_PUBLICATION: publication_remote,
        }.get(role)
        if not remote:
            raise SyncwheelError('coordination state contains an invalid typed remote ref')
        return f"{remote}/{ref[len('refs/heads/'):]}"
    return value


def coordination_snapshot_managed_ref_names(snapshot):
    refs = []
    integration = snapshot.get('integration') or {}
    if isinstance(integration.get('branch'), str) and integration['branch']:
        refs.append(f"refs/heads/{integration['branch']}")
    for stack in snapshot.get('stacks') or []:
        if isinstance(stack, dict) and isinstance(stack.get('branch'), str) and stack['branch']:
            refs.append(f"refs/heads/{stack['branch']}")
    for channel in snapshot.get('channels') or []:
        if isinstance(channel, dict) and isinstance(channel.get('branch'), str) and channel['branch']:
            refs.append(f"refs/heads/{channel['branch']}")
    return list(dict.fromkeys(refs))


def coordination_integration_resolutions(snapshot):
    """Validate integration-owned replacements without permitting source edits."""
    resolutions = snapshot.get('integration', {}).get('resolutions', {})
    ids = {item.get('id') for item in snapshot.get('stacks', []) if isinstance(item, dict)}
    allowed = {'integration_commits', 'integration_only_commits', 'integration_resolution'}
    if not isinstance(resolutions, dict):
        raise SyncwheelError('coordination integration resolutions must be an object')
    for stack_id, resolution in resolutions.items():
        if stack_id not in ids or not isinstance(resolution, dict) or not set(resolution).issubset(allowed):
            raise SyncwheelError('coordination integration resolution has unknown stack or fields')
        for field in ('integration_commits', 'integration_only_commits'):
            if field in resolution and (
                not isinstance(resolution[field], list)
                or not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40,64}', value)
                           for value in resolution[field])
            ):
                raise SyncwheelError('coordination integration resolution commits must be object IDs')
        binding = resolution.get('integration_resolution')
        if binding is None:
            raise SyncwheelError('coordination integration resolution requires a source binding')
        if binding is not None and (
            not isinstance(binding, dict) or set(binding) != {'source_tip'}
            or not isinstance(binding['source_tip'], str)
            or not re.fullmatch(r'[0-9a-f]{40,64}', binding['source_tip'])
            or 'integration_commits' not in resolution
        ):
            raise SyncwheelError('coordination integration resolution binding is invalid')
    return resolutions


def validate_coordination_snapshot_refs(snapshot, *, branch_is_valid):
    if not isinstance(snapshot, dict):
        raise SyncwheelError('coordination state manifest must be an object')
    defaults = snapshot.get('defaults')
    integration = snapshot.get('integration')
    stacks = snapshot.get('stacks')
    channels = snapshot.get('channels', [])
    if not isinstance(defaults, dict) or 'base_ref' not in defaults:
        raise SyncwheelError('coordination state manifest is missing defaults.base_ref')
    if not isinstance(integration, dict) or 'base' not in integration:
        raise SyncwheelError('coordination state manifest is missing integration.base')
    derived_paths = integration.get('derived_paths')
    if snapshot.get('version') == MANIFEST_VERSION_CHANNELS:
        if (
            not isinstance(derived_paths, list)
            or not all(
                isinstance(item, str) and item and item.endswith('/')
                for item in derived_paths
            )
            or len(derived_paths) != len(set(derived_paths))
        ):
            raise SyncwheelError(
                'coordination state manifest integration.derived_paths must be '
                'a unique string array of path prefixes'
            )
        normalize_derived_provenance(
            integration.get('derived_provenance') or [],
            label='coordination state manifest integration.derived_provenance',
        )
    else:
        if derived_paths is not None:
            raise SyncwheelError(
                'coordination state manifest integration.derived_paths requires version 3'
            )
        if 'derived_provenance' in integration:
            raise SyncwheelError(
                'coordination state manifest integration.derived_provenance requires version 3'
            )
    if not isinstance(stacks, list):
        raise SyncwheelError('coordination state manifest stacks must be an array')
    if not isinstance(channels, list):
        raise SyncwheelError('coordination state manifest channels must be an array')
    coordination_integration_resolutions(snapshot)
    if 'landing' in snapshot:
        normalize_landing_policy(snapshot['landing'])
    coordination_public_remote_ref_parts(
        defaults['base_ref'], 'coordination state defaults.base_ref'
    , branch_is_valid=branch_is_valid)
    coordination_public_remote_ref_parts(
        integration['base'], 'coordination state integration.base'
    , branch_is_valid=branch_is_valid)
    for index, stack in enumerate(stacks):
        if not isinstance(stack, dict) or 'base' not in stack:
            raise SyncwheelError(f'coordination state manifest stack {index} is missing base')
        coordination_public_remote_ref_parts(
            stack['base'], f'coordination state stacks[{index}].base'
        , branch_is_valid=branch_is_valid)
        validate_stack_adoption(stack)
        dependencies = stack.get('depends_on', [])
        if (
            not isinstance(dependencies, list)
            or not all(isinstance(item, str) and item for item in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            raise SyncwheelError(
                f'coordination state manifest stack {index} has invalid depends_on'
            )
    validate_stack_dependency_graph(
        stacks,
        require_declared_dependencies=snapshot.get('version') == MANIFEST_VERSION_CHANNELS,
    )
    for index, channel in enumerate(channels):
        if not isinstance(channel, dict) or 'base' not in channel:
            raise SyncwheelError(f'coordination state manifest channel {index} is missing base')
        coordination_public_remote_ref_parts(
            channel['base'], f'coordination state channels[{index}].base'
        , branch_is_valid=branch_is_valid)
        channel_id = channel.get('id')
        if not isinstance(channel_id, str) or not channel_id:
            raise SyncwheelError(f'coordination state manifest channel {index} is missing id')
        if not isinstance(channel.get('branch'), str) or not channel['branch']:
            raise SyncwheelError(f'coordination state manifest channel {channel_id} is missing branch')
        if not isinstance(channel.get('baseRevision'), str) or not re.fullmatch(
            r'[0-9a-f]{40}', channel['baseRevision']
        ):
            raise SyncwheelError(
                f'coordination state manifest channel {channel_id} has invalid baseRevision'
            )
        if channel.get('lifecycle') not in CHANNEL_LIFECYCLES:
            raise SyncwheelError(
                f'coordination state manifest channel {channel_id} has invalid lifecycle'
            )
        composition = channel.get('composition')
        if not isinstance(composition, list):
            raise SyncwheelError(
                f'coordination state manifest channel {channel_id} has invalid composition'
            )
        for entry in composition:
            normalize_channel_entry(entry, channel_id)
        normalize_channel_resolution(
            channel.get('resolution'), channel_id, channel_pin_digest(channel)
        )
        if channel['lifecycle'] == 'ephemeral':
            expiry = channel.get('expiry')
            if not isinstance(expiry, dict):
                raise SyncwheelError(
                    f'coordination state ephemeral channel {channel_id} is missing expiry'
                )
            normalize_channel_timestamp(expiry.get('createdAt'), f'channel {channel_id} expiry.createdAt')
            normalize_channel_timestamp(expiry.get('expiresAt'), f'channel {channel_id} expiry.expiresAt')


def coordination_snapshot_schema(snapshot):
    # Older serializers silently omit adoption bindings; do not let them republish.
    if any(isinstance(stack, dict) and stack.get('adopted_history')
           for stack in snapshot.get('stacks', [])):
        return COORDINATION_STATE_SCHEMA_VERSION_ADOPTIONS
    # Older peers must reject bound resolutions rather than silently erase them.
    integration = snapshot.get('integration')
    if isinstance(integration, dict) and integration.get('resolutions'):
        return COORDINATION_STATE_SCHEMA_VERSION_RESOLUTIONS
    return (COORDINATION_STATE_SCHEMA_VERSION_CHANNELS
            if snapshot.get('version') == MANIFEST_VERSION_CHANNELS
            else COORDINATION_STATE_SCHEMA_VERSION)


def validate_coordination_state(state, expected_id=None, claims_mode='advisory', *, branch_is_valid):
    if not isinstance(state, dict):
        raise SyncwheelError('coordination state must be an object')
    if state.get('schema_version') not in {
        COORDINATION_STATE_SCHEMA_VERSION,
        COORDINATION_STATE_SCHEMA_VERSION_CHANNELS,
        COORDINATION_STATE_SCHEMA_VERSION_RESOLUTIONS,
        COORDINATION_STATE_SCHEMA_VERSION_ADOPTIONS,
    }:
        raise SyncwheelError(
            f"unsupported coordination state schema: {state.get('schema_version')!r}"
        )
    coordination_id = state.get('coordination_id')
    if not isinstance(coordination_id, str) or not coordination_id:
        raise SyncwheelError('coordination state is missing coordination_id')
    if expected_id and coordination_id != expected_id:
        raise SyncwheelError(
            f'coordination state id mismatch: expected {expected_id!r}, got {coordination_id!r}'
        )
    if not isinstance(state.get('manifest'), dict):
        raise SyncwheelError('coordination state is missing the normalized manifest snapshot')
    manifest_version = state['manifest'].get('version')
    expected_schema = coordination_snapshot_schema(state['manifest'])
    if state['schema_version'] != expected_schema:
        raise SyncwheelError(
            f'coordination state schema {state["schema_version"]} is incompatible with '
            f'manifest version {manifest_version}'
        )
    dependency_stacks = [
        stack.get('id') for stack in state['manifest'].get('stacks', [])
        if isinstance(stack, dict) and stack.get('depends_on')
    ]
    if dependency_stacks and manifest_version != MANIFEST_VERSION_CHANNELS:
        raise SyncwheelError(
            'coordination state stack depends_on requires manifest version 3: '
            + ', '.join(str(stack_id) for stack_id in dependency_stacks)
        )
    validate_coordination_snapshot_refs(state['manifest'], branch_is_valid=branch_is_valid)
    if not isinstance(state.get('manifest_digest'), str) or not re.fullmatch(
        r'[0-9a-f]{64}', state['manifest_digest']
    ):
        raise SyncwheelError('coordination state is missing manifest_digest')
    if not isinstance(state.get('managed_refs'), dict):
        raise SyncwheelError('coordination state is missing managed_refs')
    claims = state.get('claims', {})
    if not isinstance(claims, dict):
        raise SyncwheelError('coordination state claims must be an object')
    invalid_claims = sorted(set(claims) - set(state['managed_refs']))
    if invalid_claims:
        raise SyncwheelError(
            'coordination state claims contain unmanaged refs: ' + ', '.join(invalid_claims)
        )
    if claims_mode == 'required':
        unclaimed = sorted(set(state['managed_refs']) - set(claims))
        if unclaimed:
            raise SyncwheelError(
                'coordination claims required mode refuses unclaimed managed refs: '
                + ', '.join(unclaimed)
            )
    operation_token = state.get('operation_token')
    if operation_token is not None and (
        not isinstance(operation_token, str) or not operation_token
    ):
        raise SyncwheelError('coordination state operation_token must be a non-empty string')
    if not isinstance(state.get('tombstones', []), list):
        raise SyncwheelError('coordination state tombstones must be an array')
    return state


def stack_snapshot_map(snapshot):
    return {stack['id']: stack for stack in snapshot.get('stacks') or []}


def channel_snapshot_map(snapshot):
    return {channel['id']: channel for channel in snapshot.get('channels') or []}


def changed_stack_ids(base, candidate):
    base_stacks = stack_snapshot_map(base)
    candidate_stacks = stack_snapshot_map(candidate)
    return {
        stack_id
        for stack_id in set(base_stacks) | set(candidate_stacks)
        if base_stacks.get(stack_id) != candidate_stacks.get(stack_id)
    }


def changed_snapshot_fields(base, candidate):
    return sorted(
        key for key in set(base) | set(candidate)
        if base.get(key) != candidate.get(key)
    )


def local_proposal_diff(local_snapshot, published_snapshot):
    local_stacks = stack_snapshot_map(local_snapshot or {})
    published_stacks = stack_snapshot_map(published_snapshot or {})
    return {
        'added': sorted(set(local_stacks) - set(published_stacks)),
        'changed': {
            stack_id: changed_snapshot_fields(
                published_stacks[stack_id], local_stacks[stack_id]
            )
            for stack_id in sorted(set(local_stacks) & set(published_stacks))
            if local_stacks[stack_id] != published_stacks[stack_id]
        },
        'removed': sorted(set(published_stacks) - set(local_stacks)),
        'integration': changed_snapshot_fields(
            (published_snapshot or {}).get('integration') or {},
            (local_snapshot or {}).get('integration') or {},
        ),
    }


def local_proposal_lines(diff):
    lines = [f'added stack: {stack_id}' for stack_id in diff['added']]
    lines.extend(
        f"changed stack: {stack_id} ({', '.join(fields)})"
        for stack_id, fields in diff['changed'].items()
    )
    lines.extend(f'removed stack: {stack_id}' for stack_id in diff['removed'])
    if diff['integration']:
        lines.append(f"integration changed: {', '.join(diff['integration'])}")
    return lines


def snapshot_globals(snapshot):
    return {
        'version': snapshot.get('version'),
        'defaults': snapshot.get('defaults'),
        'integration': snapshot.get('integration'),
        'channels': snapshot.get('channels', []),
        'coordination': snapshot.get('coordination'),
        'landing': snapshot.get('landing'),
    }


def merge_coordination_snapshots(base, local, remote):
    """Merge only disjoint stack-record changes with an unchanged shared integration contract."""
    if not base or snapshot_globals(base) != snapshot_globals(local) or snapshot_globals(base) != snapshot_globals(remote):
        return {'status': 'conflict', 'reason': 'shared_integration_or_defaults_changed'}
    local_changes = changed_stack_ids(base, local)
    remote_changes = changed_stack_ids(base, remote)
    overlap = sorted(local_changes.intersection(remote_changes))
    if overlap:
        return {'status': 'conflict', 'reason': 'overlapping_stack_changes', 'stacks': overlap}
    if not local_changes or not remote_changes:
        return {'status': 'conflict', 'reason': 'no_disjoint_changes_to_merge'}
    merged = json.loads(json.dumps(remote))
    base_map = stack_snapshot_map(base)
    local_map = stack_snapshot_map(local)
    merged_map = stack_snapshot_map(merged)
    for stack_id in local_changes:
        if stack_id in local_map:
            merged_map[stack_id] = local_map[stack_id]
        else:
            merged_map.pop(stack_id, None)
    ordered_ids = []
    for source in (base.get('stacks') or [], remote.get('stacks') or [], local.get('stacks') or []):
        for stack in source:
            stack_id = stack['id']
            if stack_id in merged_map and stack_id not in ordered_ids:
                ordered_ids.append(stack_id)
    merged['stacks'] = [merged_map[stack_id] for stack_id in ordered_ids]
    branches = [stack['branch'] for stack in merged['stacks']]
    if len(branches) != len(set(branches)):
        return {'status': 'conflict', 'reason': 'merged_stack_branch_ownership_conflict'}
    return {
        'status': 'mergeable',
        'merged': merged,
        'local_stacks': sorted(local_changes),
        'remote_stacks': sorted(remote_changes),
    }


def additive_coordination_snapshot_delta(base, candidate, side, updated_stack=None):
    """Accept stack additions, their integration membership, and one named stack's new commits."""
    base_contract = json.loads(json.dumps(base))
    candidate_contract = json.loads(json.dumps(candidate))
    base_contract.pop('stacks', None)
    candidate_contract.pop('stacks', None)
    base_contract.get('integration', {}).pop('stacks', None)
    candidate_contract.get('integration', {}).pop('stacks', None)
    if base_contract != candidate_contract:
        raise SyncwheelError(
            f'coordination compose {side} proposal changes shared defaults or integration contract'
        )
    base_stacks = stack_snapshot_map(base)
    candidate_stacks = stack_snapshot_map(candidate)
    removed = sorted(set(base_stacks) - set(candidate_stacks))
    changed = sorted(
        stack_id for stack_id in set(base_stacks) & set(candidate_stacks)
        if stack_id != updated_stack and base_stacks[stack_id] != candidate_stacks[stack_id]
    )
    if removed or changed:
        raise SyncwheelError(
            f'coordination compose {side} proposal is not additive; '
            f'removed={removed}, changed={changed}'
        )
    base_order = [stack['id'] for stack in base.get('stacks') or []]
    candidate_order = [stack['id'] for stack in candidate.get('stacks') or []]
    if candidate_order[:len(base_order)] != base_order:
        raise SyncwheelError(f'coordination compose {side} proposal reorders existing stacks')
    added = candidate_order[len(base_order):]
    if set(added) != set(candidate_stacks) - set(base_stacks):
        raise SyncwheelError(f'coordination compose {side} proposal has ambiguous stack ordering')
    base_members = list(base.get('integration', {}).get('stacks') or [])
    candidate_members = list(candidate.get('integration', {}).get('stacks') or [])
    # legacy local drafts are stacks without integration membership
    appended = (
        [stack_id for stack_id in added if stack_id in candidate_members]
        if side == 'local' else added
    )
    if candidate_members != [*base_members, *appended]:
        raise SyncwheelError(
            f'coordination compose {side} integration membership must append exactly its new stacks'
        )
    return {'added': added, 'members': appended}


def compose_additive_coordination_snapshots(base, local, remote, requested_stack, *, branch_is_valid):
    local_delta = additive_coordination_snapshot_delta(
        base, local, 'local', updated_stack=requested_stack
    )
    remote_delta = additive_coordination_snapshot_delta(base, remote, 'remote')
    local_map = stack_snapshot_map(local)
    remote_map = stack_snapshot_map(remote)
    base_requested = stack_snapshot_map(base).get(requested_stack)
    if base_requested is None and requested_stack not in local_delta['members']:
        raise SyncwheelError(
            'coordination compose local proposal must add the requested stack'
        )
    if base_requested is not None and (
        {**base_requested, 'commits': None}
        != {**local_map[requested_stack], 'commits': None}
    ):
        raise SyncwheelError(
            'coordination compose can only change the commits of a published stack'
        )
    overlap = set(local_delta['added']).intersection(remote_delta['added'])
    conflicting = sorted(
        stack_id for stack_id in overlap if local_map[stack_id] != remote_map[stack_id]
    )
    if conflicting:
        raise SyncwheelError(
            'coordination compose has conflicting additions: ' + ', '.join(conflicting)
        )
    merged = json.loads(json.dumps(base))
    merged_map = stack_snapshot_map(merged)
    ordered_additions = []
    for source, added in ((remote_map, remote_delta['added']), (local_map, [requested_stack])):
        for stack_id in added:
            if stack_id not in merged_map:
                merged_map[stack_id] = json.loads(json.dumps(source[stack_id]))
                ordered_additions.append(stack_id)
    merged['stacks'] = [
        *(
            json.loads(json.dumps(local_map[stack['id']]))
            if stack['id'] == requested_stack else stack
            for stack in base.get('stacks') or []
        ),
        *(merged_map[stack_id] for stack_id in ordered_additions),
    ]
    merged['integration']['stacks'] = [
        *list(base.get('integration', {}).get('stacks') or []),
        *ordered_additions,
    ]
    retained = [stack_id for stack_id in local_delta['added'] if stack_id not in merged_map]
    branches = [
        *(stack['branch'] for stack in merged['stacks']),
        *(local_map[stack_id]['branch'] for stack_id in retained),
    ]
    if len(branches) != len(set(branches)):
        raise SyncwheelError('coordination compose stack branch ownership is ambiguous')
    validate_coordination_snapshot_refs(merged, branch_is_valid=branch_is_valid)
    return {
        'merged': merged,
        'localAddedStacks': [] if base_requested is not None else [requested_stack],
        'localRetainedStacks': retained,
        'remoteAddedStacks': remote_delta['added'],
    }


def apply_coordination_snapshot(manifest, snapshot, *, branch_is_valid):
    updated = json.loads(json.dumps(manifest))
    local_stacks = {
        stack['id']: stack
        for stack in updated.get('stacks') or []
        if isinstance(stack, dict) and isinstance(stack.get('id'), str)
    }
    updated['version'] = snapshot['version']
    defaults = dict(updated.get('defaults') or {})
    defaults.update(snapshot['defaults'])
    defaults['base_ref'] = local_coordination_ref(
        snapshot['defaults']['base_ref'],
        defaults,
     branch_is_valid=branch_is_valid)
    updated['defaults'] = defaults
    integration = dict(snapshot['integration'])
    resolutions = coordination_integration_resolutions(snapshot)
    integration.pop('resolutions', None)
    integration['base'] = local_coordination_ref(
        snapshot['integration']['base'],
        defaults,
     branch_is_valid=branch_is_valid)
    updated['integration'] = integration
    updated['stacks'] = []
    for stack in snapshot['stacks']:
        restored = dict(stack)
        local_stack = local_stacks.get(stack['id'], {})
        restored['base'] = local_coordination_ref(stack['base'], defaults, branch_is_valid=branch_is_valid)
        restored['target_remote'] = local_stack.get(
            'target_remote', defaults['canonical_remote']
        )
        restored.update(copy.deepcopy(resolutions.get(stack['id'], {})))
        restored['state'] = stack.get('state', 'published')
        restored['publication'] = {'enabled': restored['state'] != 'draft'}
        restored['meta'] = (
            local_stack['meta'] if isinstance(local_stack.get('meta'), dict) else {}
        )
        updated['stacks'].append(restored)
    updated['channels'] = []
    for channel in snapshot.get('channels', []):
        restored_channel = dict(channel)
        restored_channel['base'] = local_coordination_ref(channel['base'], defaults, branch_is_valid=branch_is_valid)
        restored_channel['remote'] = defaults['publication_remote']
        updated['channels'].append(restored_channel)
    if 'coordination' in snapshot:
        coordination = dict(updated.get('coordination') or {})
        coordination.update(snapshot['coordination'])
        coordination['remote'] = coordination.get('remote') or defaults['publication_remote']
        updated['coordination'] = coordination
    else:
        updated.pop('coordination', None)
    if 'landing' in snapshot:
        updated['landing'] = normalize_landing_policy(snapshot['landing'])
    return updated


COORDINATION_CLAIM_PREFIX = 'syncwheel/claim/'

def coordination_claim_ref(source_ref):
    if not isinstance(source_ref, str) or not source_ref.startswith('refs/heads/'):
        raise SyncwheelError(
            f'coordination claim requires a full refs/heads source ref: {source_ref!r}'
        )
    return f"refs/heads/{COORDINATION_CLAIM_PREFIX}{source_ref[len('refs/'):]}"


def coordination_tombstone_ref(tombstone):
    if not isinstance(tombstone, dict):
        return None
    ref = tombstone.get('ref')
    if isinstance(ref, str) and ref:
        return ref
    branch = tombstone.get('branch')
    if isinstance(branch, str) and branch:
        return f'refs/heads/{branch}'
    return None


def coordination_identity_snapshot(snapshot):
    """Strip the projection both sides of a publication resolve differently.

    ``integration.derived_provenance`` comes from this clone's last-seen state on
    the intent side and from the state just read from the remote on the
    publication side, so it cannot belong to an operation's identity.
    """
    identity = copy.deepcopy(snapshot)
    integration = identity.get('integration')
    if isinstance(integration, dict):
        integration.pop('derived_provenance', None)
    return identity


def coordination_state_operation_manifest_digest(state):
    snapshot = (state or {}).get('manifest')
    if not isinstance(snapshot, dict):
        return None
    return canonical_json_digest(coordination_identity_snapshot(snapshot))
