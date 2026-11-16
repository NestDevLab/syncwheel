"""Manifest schema, normalization and repository policy.

This domain operates on data only: it never reads Git refs, writes control files,
or imports the CLI. Callers own loading, persistence and mutation preflight."""

import datetime
import hashlib
import json
import re
from pathlib import Path

from syncwheel_errors import SyncwheelError


INTEGRATION_MEMBERSHIP_LEGACY = 'legacy'


INTEGRATION_MEMBERSHIP_REQUIRED = 'required'


INTEGRATION_MEMBERSHIP_POLICIES = {
    INTEGRATION_MEMBERSHIP_LEGACY,
    INTEGRATION_MEMBERSHIP_REQUIRED,
}


STACK_STATES = {'draft', 'published'}


MANIFEST_VERSIONS = {1, 2, 3}


MANIFEST_VERSION_COORDINATED = 2


MANIFEST_VERSION_CHANNELS = 3


COORDINATION_MODES = {'active-active', 'disabled'}


COORDINATION_STATE_PREFIX = 'syncwheel/state/'


COORDINATION_CLAIM_MODES = {'advisory', 'required'}


DEFAULT_COORDINATION_GC = {
    'worktree_grace_days': 7,
    'backup_retention_days': 30,
    'backup_keep': 2,
}


DEFAULT_INTEGRATION_BRANCH = 'main-integration'


SYNCWHEEL_TRACKING_VALUES = {'git-tracked', 'local-only'}


SYNCWHEEL_TRACKING_LOCAL_ONLY = 'local-only'


AUTHORITY_MODE_HUMAN_GATED = 'human-gated'


AUTHORITY_MODE_AI_MANAGED = 'ai-managed'


AUTHORITY_MODES = {AUTHORITY_MODE_HUMAN_GATED, AUTHORITY_MODE_AI_MANAGED}


AUTHORITY_CLASS_SOURCE_CHANGE = 'source_change'


AUTHORITY_CLASS_RUNTIME_CHANGE = 'runtime_change'


AUTHORITY_CLASS_DESTRUCTIVE_REWRITE = 'destructive_rewrite'


AUTHORITY_CLASSES = (
    AUTHORITY_CLASS_SOURCE_CHANGE,
    AUTHORITY_CLASS_RUNTIME_CHANGE,
    AUTHORITY_CLASS_DESTRUCTIVE_REWRITE,
)


DEFAULT_SYNCWHEEL_WORKTREE_ROOT = '.syncwheel/wt'


REPOSITORY_MODES = {'delivery', 'journal'}


DEFAULT_JOURNAL_INTERVAL = '30m'


CHANNEL_LIFECYCLES = {'shared', 'ephemeral'}


GOVERNED_WORKTREE_DEFAULT_CAPACITY = 4


GOVERNED_WORKTREE_CAPACITY_ENFORCEMENTS = ('error', 'warn')


DERIVED_OPERATION_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}')


DERIVED_PROVENANCE_FIELDS = {
    'operation_id',
    'commit',
    'paths',
    'paths_digest',
    'composition_digest',
}


def normalize_derived_provenance(records, prefixes=None, label='derived provenance'):
    if records is None:
        records = []
    if not isinstance(records, list):
        raise SyncwheelError(f'{label} must be an array')
    normalized = []
    operation_ids = set()
    path_sets = set()
    for index, raw in enumerate(records):
        if not isinstance(raw, dict) or set(raw) != DERIVED_PROVENANCE_FIELDS:
            raise SyncwheelError(
                f'{label}[{index}] must contain exactly: '
                + ', '.join(sorted(DERIVED_PROVENANCE_FIELDS))
            )
        operation_id = raw.get('operation_id')
        commit = raw.get('commit')
        paths = raw.get('paths')
        paths_digest = raw.get('paths_digest')
        composition_digest = raw.get('composition_digest')
        if not isinstance(operation_id, str) or not DERIVED_OPERATION_ID.fullmatch(operation_id):
            raise SyncwheelError(f'{label}[{index}].operation_id is invalid')
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise SyncwheelError(f'{label}[{index}].commit must be a full SHA-1')
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, str) and path and '\0' not in path for path in paths)
            or paths != sorted(paths)
            or len(paths) != len(set(paths))
        ):
            raise SyncwheelError(
                f'{label}[{index}].paths must be a non-empty sorted unique NUL-free string array'
            )
        if prefixes is not None and not all(
            any(path.startswith(prefix) for prefix in prefixes) for path in paths
        ):
            raise SyncwheelError(f'{label}[{index}] contains a path outside integration.derived_paths')
        if not isinstance(paths_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', paths_digest):
            raise SyncwheelError(f'{label}[{index}].paths_digest must be a SHA-256')
        if not isinstance(composition_digest, str) or not re.fullmatch(
            r'[0-9a-f]{64}', composition_digest
        ):
            raise SyncwheelError(f'{label}[{index}].composition_digest must be a SHA-256')
        path_key = tuple(paths)
        if operation_id in operation_ids:
            raise SyncwheelError(f'{label} contains duplicate operation_id {operation_id!r}')
        if path_key in path_sets:
            raise SyncwheelError(f'{label} contains duplicate declared path sets')
        operation_ids.add(operation_id)
        path_sets.add(path_key)
        normalized.append({
            'operation_id': operation_id,
            'commit': commit,
            'paths': list(paths),
            'paths_digest': paths_digest,
            'composition_digest': composition_digest,
        })
    return sorted(normalized, key=lambda item: (item['paths'], item['operation_id']))


def normalize_syncwheel_tracking(value, path='manifest'):
    if value is None:
        return None
    if value not in SYNCWHEEL_TRACKING_VALUES:
        allowed = ', '.join(sorted(SYNCWHEEL_TRACKING_VALUES))
        raise SyncwheelError(f'{path} syncwheel_tracking must be one of: {allowed}')
    return value


def manifest_is_local_only(manifest):
    """A local-only manifest never becomes a tracked file, in any tree or index.

    A manifest with no tracking policy is not local-only.
    """
    return (manifest or {}).get('syncwheel_tracking') == SYNCWHEEL_TRACKING_LOCAL_ONLY


def control_manifest_stays_untracked(manifest):
    """Whether the integration tree of this repository must not carry the manifest.

    Active-active coordination binds the manifest by reading it from the
    published integration tip, so a coordinated repository keeps its control
    commit whatever the tracking policy says.
    """
    return manifest_is_local_only(manifest) and not coordination_is_active(manifest)


def default_authority_policy():
    return {
        'mode': AUTHORITY_MODE_HUMAN_GATED,
        'allow': [],
        'deny': [AUTHORITY_CLASS_DESTRUCTIVE_REWRITE],
    }


def normalize_authority_policy(value, path='manifest'):
    if value is None:
        return default_authority_policy()
    if not isinstance(value, dict):
        raise SyncwheelError(f'{path} authority must be an object')
    mode = value.get('mode')
    if mode not in AUTHORITY_MODES:
        allowed = ', '.join(sorted(AUTHORITY_MODES))
        raise SyncwheelError(f'{path} authority.mode must be one of: {allowed}')
    classes = {}
    for field in ('allow', 'deny'):
        raw = value.get(field, [])
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise SyncwheelError(f'{path} authority.{field} must be a string array')
        unknown = sorted(set(raw) - set(AUTHORITY_CLASSES))
        if unknown:
            raise SyncwheelError(
                f'{path} authority.{field} contains unknown classes: ' + ', '.join(unknown)
                + '; allowed: ' + ', '.join(AUTHORITY_CLASSES)
            )
        classes[field] = set(raw)
    if AUTHORITY_CLASS_DESTRUCTIVE_REWRITE in classes['allow']:
        raise SyncwheelError(f'{path} authority.allow may never contain {AUTHORITY_CLASS_DESTRUCTIVE_REWRITE}')
    classes['deny'].add(AUTHORITY_CLASS_DESTRUCTIVE_REWRITE)
    overlap = sorted(classes['allow'] & classes['deny'])
    if overlap:
        raise SyncwheelError(f'{path} authority classes cannot be both allowed and denied: ' + ', '.join(overlap))
    if mode == AUTHORITY_MODE_HUMAN_GATED and classes['allow']:
        raise SyncwheelError(f'{path} authority.mode={AUTHORITY_MODE_HUMAN_GATED} cannot allow any class')
    if mode == AUTHORITY_MODE_AI_MANAGED and not classes['allow']:
        raise SyncwheelError(f'{path} authority.mode={AUTHORITY_MODE_AI_MANAGED} requires at least one allowed class')
    return {
        'mode': mode,
        'allow': [item for item in AUTHORITY_CLASSES if item in classes['allow']],
        'deny': [item for item in AUTHORITY_CLASSES if item in classes['deny']],
    }


def manifest_authority(manifest):
    if not manifest:
        return default_authority_policy()
    return normalize_authority_policy(manifest.get('authority'))


def authority_allows(manifest, authority_class):
    return authority_class in manifest_authority(manifest)['allow']


def normalize_syncwheel_worktree_root(value, path='manifest'):
    if value is None:
        return DEFAULT_SYNCWHEEL_WORKTREE_ROOT
    if not isinstance(value, str) or not value.strip():
        raise SyncwheelError(f'{path} syncwheel_worktree_root must be a non-empty string')
    return value.strip()


def normalize_governed_worktree_capacity(value, path='manifest'):
    key = f'{path} governed_worktree_capacity'
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise SyncwheelError(f'{key} must be an object')
    unknown = sorted(set(value) - {'limit', 'enforcement'})
    if unknown:
        raise SyncwheelError(f"{key} has unknown field(s): {', '.join(unknown)}")
    limit = value.get('limit', GOVERNED_WORKTREE_DEFAULT_CAPACITY)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise SyncwheelError(f'{key}.limit must be a positive integer')
    enforcement = value.get('enforcement', 'warn')
    if enforcement not in GOVERNED_WORKTREE_CAPACITY_ENFORCEMENTS:
        raise SyncwheelError(
            f"{key}.enforcement must be one of: {', '.join(GOVERNED_WORKTREE_CAPACITY_ENFORCEMENTS)}"
        )
    return {'limit': limit, 'enforcement': enforcement}


def governed_worktree_capacity(manifest):
    return normalize_governed_worktree_capacity((manifest or {}).get('governed_worktree_capacity'))


def normalize_coordination_gc(value, path='coordination.gc'):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise SyncwheelError(f'{path} must be an object')
    normalized = dict(DEFAULT_COORDINATION_GC)
    for key in DEFAULT_COORDINATION_GC:
        if key not in value:
            continue
        candidate = value[key]
        if not isinstance(candidate, int) or isinstance(candidate, bool) or candidate < 1:
            raise SyncwheelError(f'{path}.{key} must be a positive integer')
        normalized[key] = candidate
    unknown = sorted(set(value) - set(DEFAULT_COORDINATION_GC))
    if unknown:
        raise SyncwheelError(f'{path} has unknown keys: {", ".join(unknown)}')
    return normalized


def normalize_coordination_id(value, path='coordination.id'):
    if not isinstance(value, str) or not value.strip():
        raise SyncwheelError(f'{path} must be a non-empty string')
    return safe_ref_segment(value)


def default_coordination_id(manifest_path):
    stem = Path(manifest_path).stem
    if stem == 'manifest':
        return 'default'
    if stem.endswith('.local'):
        stem = stem[:-len('.local')]
    return safe_ref_segment(stem or 'default')


def default_coordination_state_branch(coordination_id):
    return f'{COORDINATION_STATE_PREFIX}{normalize_coordination_id(coordination_id)}'


def normalize_coordination(value, manifest_path='manifest'):
    if not isinstance(value, dict):
        raise SyncwheelError('manifest version 2 requires a coordination object')
    mode = value.get('mode')
    if mode not in COORDINATION_MODES:
        allowed = ', '.join(sorted(COORDINATION_MODES))
        raise SyncwheelError(f'coordination.mode must be one of: {allowed}')
    coordination_id = normalize_coordination_id(value.get('id'))
    remote = value.get('remote')
    if not isinstance(remote, str) or not remote.strip():
        raise SyncwheelError('coordination.remote must be a non-empty string')
    state_branch = value.get('state_branch')
    expected_state_branch = default_coordination_state_branch(coordination_id)
    if state_branch != expected_state_branch:
        raise SyncwheelError(
            f'coordination.state_branch must be {expected_state_branch!r} for coordination id {coordination_id!r}'
        )
    normalized = {
        'mode': mode,
        'id': coordination_id,
        'remote': remote.strip(),
        'state_branch': state_branch,
        'gc': normalize_coordination_gc(value.get('gc')),
    }
    claims = value.get('claims')
    if claims is not None:
        if claims not in COORDINATION_CLAIM_MODES:
            raise SyncwheelError('coordination.claims must be one of: advisory, required')
        normalized['claims'] = claims
    unknown = sorted(set(value) - {'mode', 'id', 'remote', 'state_branch', 'gc', 'claims'})
    if unknown:
        raise SyncwheelError(f'coordination has unknown keys: {", ".join(unknown)}')
    return normalized


def normalize_integration_membership(value, path='defaults.integration_membership'):
    if value is None:
        return INTEGRATION_MEMBERSHIP_LEGACY
    if value not in INTEGRATION_MEMBERSHIP_POLICIES:
        allowed = ', '.join(sorted(INTEGRATION_MEMBERSHIP_POLICIES))
        raise SyncwheelError(f'{path} must be one of: {allowed}')
    return value


def coordination_config(manifest):
    if manifest.get('version') not in {MANIFEST_VERSION_COORDINATED, MANIFEST_VERSION_CHANNELS}:
        return None
    config = manifest.get('coordination')
    if not isinstance(config, dict):
        return config
    return {**config, 'claims': config.get('claims', 'advisory')}


def coordination_is_active(manifest):
    config = coordination_config(manifest)
    return bool(config and config.get('mode') == 'active-active')


def active_coordination_config(manifest_path, remote, coordination_id=None):
    coordination_id = normalize_coordination_id(
        coordination_id or default_coordination_id(manifest_path)
    )
    return {
        'mode': 'active-active',
        'id': coordination_id,
        'remote': remote,
        'state_branch': default_coordination_state_branch(coordination_id),
        'gc': dict(DEFAULT_COORDINATION_GC),
        'claims': 'advisory',
    }


def disabled_coordination_config(manifest_path, remote, coordination_id=None):
    coordination_id = normalize_coordination_id(
        coordination_id or default_coordination_id(manifest_path)
    )
    if not isinstance(remote, str) or not remote.strip():
        raise SyncwheelError('disabled coordination requires a non-empty publication remote name')
    return {
        'mode': 'disabled',
        'id': coordination_id,
        'remote': remote.strip(),
        'state_branch': default_coordination_state_branch(coordination_id),
        'gc': dict(DEFAULT_COORDINATION_GC),
        'claims': 'advisory',
    }


def normalize_channel_timestamp(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SyncwheelError(f'{field} must be a non-empty ISO-8601 timestamp')
    candidate = value.strip()
    try:
        datetime.datetime.fromisoformat(candidate.replace('Z', '+00:00'))
    except ValueError as exc:
        raise SyncwheelError(f'{field} must be an ISO-8601 timestamp') from exc
    return candidate


def normalize_landing_requirement(value, path='landing.checks', seen_ids=None):
    """Normalize the deliberately small all/any requirement grammar for stack land."""
    if not isinstance(value, dict):
        raise SyncwheelError(f'{path} must be an object')
    requirement_id = value.get('id')
    if not isinstance(requirement_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', requirement_id):
        raise SyncwheelError(f'{path}.id must be a 1-128 character requirement id')
    seen_ids = seen_ids if seen_ids is not None else set()
    if requirement_id in seen_ids:
        raise SyncwheelError(f'duplicate landing requirement id: {requirement_id}')
    seen_ids.add(requirement_id)
    kinds = [kind for kind in ('all', 'any', 'local', 'attestation', 'pr') if kind in value]
    if len(kinds) != 1:
        raise SyncwheelError(f'{path} must contain exactly one of all, any, local, attestation, or pr')
    kind = kinds[0]
    if kind in {'all', 'any'}:
        children = value[kind]
        if not isinstance(children, list) or not children:
            raise SyncwheelError(f'{path}.{kind} must be a non-empty array')
        if set(value) != {'id', kind}:
            raise SyncwheelError(f'{path} {kind} groups may contain only id and {kind}')
        return {'id': requirement_id, kind: [
            normalize_landing_requirement(item, f'{path}.{kind}[{index}]', seen_ids)
            for index, item in enumerate(children)
        ]}
    if kind == 'local':
        local = value['local']
        if not isinstance(local, dict) or set(local) - {'scope', 'argv', 'timeoutSeconds'}:
            raise SyncwheelError(f'{path}.local must contain scope, argv, and optional timeoutSeconds')
        scope = local.get('scope')
        argv = local.get('argv')
        timeout = local.get('timeoutSeconds', 1800)
        if scope not in {'stack', 'integration'}:
            raise SyncwheelError(f'{path}.local.scope must be stack or integration')
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise SyncwheelError(f'{path}.local.argv must be a non-empty string array')
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise SyncwheelError(f'{path}.local.timeoutSeconds must be a positive integer')
        return {'id': requirement_id, 'local': {'scope': scope, 'argv': list(argv), 'timeoutSeconds': timeout}}
    if kind == 'attestation':
        attestation = value['attestation']
        if not isinstance(attestation, dict) or set(attestation) != {'scope', 'verifierArgv'}:
            raise SyncwheelError(f'{path}.attestation must contain exactly scope and verifierArgv')
        if attestation.get('scope') not in {'stack', 'integration'}:
            raise SyncwheelError(f'{path}.attestation.scope must be stack or integration')
        argv = attestation.get('verifierArgv')
        if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
            raise SyncwheelError(f'{path}.attestation.verifierArgv must be a non-empty string array')
        return {'id': requirement_id, 'attestation': {'scope': attestation['scope'], 'verifierArgv': list(argv)}}
    pr = value['pr']
    if not isinstance(pr, dict) or set(pr) != {'checks'}:
        raise SyncwheelError(f'{path}.pr must contain exactly checks')
    checks = pr.get('checks')
    if not isinstance(checks, list) or not checks or not all(isinstance(item, str) and item for item in checks):
        raise SyncwheelError(f'{path}.pr.checks must be a non-empty string array')
    return {'id': requirement_id, 'pr': {'checks': list(checks)}}


def normalize_landing_policy(value):
    if value is None:
        return {'mode': 'disabled', 'strategy': 'merge', 'checks': None}
    if not isinstance(value, dict):
        raise SyncwheelError('landing must be an object')
    unknown = sorted(set(value) - {'mode', 'strategy', 'checks'})
    if unknown:
        raise SyncwheelError('landing has unknown keys: ' + ', '.join(unknown))
    mode = value.get('mode', 'disabled')
    if mode not in {'disabled', 'direct'}:
        raise SyncwheelError('landing.mode must be disabled or direct')
    strategy = value.get('strategy', 'merge')
    if strategy not in {'merge', 'ff-only'}:
        raise SyncwheelError('landing.strategy must be merge or ff-only')
    checks = value.get('checks')
    return {
        'mode': mode,
        'strategy': strategy,
        'checks': normalize_landing_requirement(checks) if checks is not None else None,
    }


def normalize_channel_entry(raw, channel_id):
    if not isinstance(raw, dict):
        raise SyncwheelError(f'channel {channel_id} composition entries must be objects')
    entry = dict(raw)
    stack_id = entry.get('stack')
    branch = entry.get('branch')
    stack_base = entry.get('stackBase')
    stack_base_revision = entry.get('stackBaseRevision')
    branch_revision = entry.get('branchRevision')
    commits = entry.get('commits')
    if not isinstance(stack_id, str) or not stack_id:
        raise SyncwheelError(f'channel {channel_id} composition entry needs a stack id')
    if not isinstance(branch, str) or not branch:
        raise SyncwheelError(f'channel {channel_id} stack {stack_id} needs a branch')
    if not isinstance(stack_base, str) or not stack_base:
        raise SyncwheelError(f'channel {channel_id} stack {stack_id} needs stackBase')
    if not isinstance(stack_base_revision, str) or not re.fullmatch(r'[0-9a-f]{40}', stack_base_revision):
        raise SyncwheelError(
            f'channel {channel_id} stack {stack_id} stackBaseRevision must be a full commit SHA'
        )
    if not isinstance(branch_revision, str) or not re.fullmatch(r'[0-9a-f]{40}', branch_revision):
        raise SyncwheelError(
            f'channel {channel_id} stack {stack_id} branchRevision must be a full commit SHA'
        )
    if (
        not isinstance(commits, list)
        or not all(isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}', commit) for commit in commits)
    ):
        raise SyncwheelError(
            f'channel {channel_id} stack {stack_id} commits must be ordered full commit SHAs'
        )
    dependencies = entry.get('dependsOn', [])
    if (
        not isinstance(dependencies, list)
        or not all(isinstance(item, str) and item for item in dependencies)
        or len(dependencies) != len(set(dependencies))
        or stack_id in dependencies
    ):
        raise SyncwheelError(
            f'channel {channel_id} stack {stack_id} dependsOn must be a unique string array '
            'that excludes itself'
        )
    normalized = {
        'stack': stack_id,
        'branch': branch,
        'stackBase': stack_base,
        'stackBaseRevision': stack_base_revision,
        'branchRevision': branch_revision,
        'commits': list(commits),
        'dependsOn': list(dependencies),
    }
    return normalized


def normalize_channel_resolution(raw, channel_id, pin_digest):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SyncwheelError(f'channel {channel_id} resolution must be an object')
    required = ('forPinDigest', 'revision', 'tree', 'parentRevision')
    for field in required:
        value = raw.get(field)
        expected_length = 64 if field == 'forPinDigest' else 40
        if not isinstance(value, str) or not re.fullmatch(
            rf'[0-9a-f]{{{expected_length}}}', value
        ):
            raise SyncwheelError(
                f'channel {channel_id} resolution.{field} must be a full object id or digest'
            )
    if raw['forPinDigest'] != pin_digest:
        raise SyncwheelError(
            f'channel {channel_id} resolution does not bind the current pin digest'
        )
    return {field: raw[field] for field in required}


def validate_channel_dependency_order(channel):
    seen = set()
    for entry in channel.get('composition', []):
        missing = [dependency for dependency in entry.get('dependsOn', []) if dependency not in seen]
        if missing:
            raise SyncwheelError(
                f"channel {channel['id']} stack {entry['stack']} requires earlier dependency stack(s): "
                + ', '.join(missing)
            )
        seen.add(entry['stack'])


def normalize_manifest_channels(data, defaults, repository_mode):
    raw_channels = data.get('channels', [])
    if raw_channels and repository_mode != 'delivery':
        raise SyncwheelError('manifest channels are supported only in repository_mode="delivery"')
    if not isinstance(raw_channels, list):
        raise SyncwheelError('manifest channels must be an array')
    seen_ids = set()
    seen_branches = set()
    channels = []
    for raw in raw_channels:
        if not isinstance(raw, dict):
            raise SyncwheelError('each channel entry must be an object')
        channel = dict(raw)
        channel_id = channel.get('id')
        branch = channel.get('branch')
        if not isinstance(channel_id, str) or not channel_id:
            raise SyncwheelError('each channel needs a string id')
        if channel_id in seen_ids:
            raise SyncwheelError(f'duplicate channel id: {channel_id}')
        if not isinstance(branch, str) or not branch:
            raise SyncwheelError(f'channel {channel_id} needs a branch')
        if branch in seen_branches:
            raise SyncwheelError(f'duplicate channel branch: {branch}')
        lifecycle = channel.get('lifecycle', 'shared')
        if lifecycle not in CHANNEL_LIFECYCLES:
            raise SyncwheelError(
                f'channel {channel_id} lifecycle must be one of: '
                + ', '.join(sorted(CHANNEL_LIFECYCLES))
            )
        composition = channel.get('composition', [])
        if not isinstance(composition, list):
            raise SyncwheelError(f'channel {channel_id} composition must be an array')
        normalized_composition = []
        composition_ids = set()
        for entry in composition:
            normalized_entry = normalize_channel_entry(entry, channel_id)
            if normalized_entry['stack'] in composition_ids:
                raise SyncwheelError(
                    f"channel {channel_id} contains stack {normalized_entry['stack']} more than once"
                )
            composition_ids.add(normalized_entry['stack'])
            normalized_composition.append(normalized_entry)
        normalized = {
            'id': channel_id,
            'branch': branch,
            'lifecycle': lifecycle,
            'base': channel.get('base') or defaults['base_ref'],
            'baseRevision': channel.get('baseRevision'),
            'remote': channel.get('remote') or defaults['publication_remote'],
            'composition': normalized_composition,
        }
        if (
            not isinstance(normalized['baseRevision'], str)
            or not re.fullmatch(r'[0-9a-f]{40}', normalized['baseRevision'])
        ):
            raise SyncwheelError(
                f'channel {channel_id} baseRevision must be a full commit SHA'
            )
        if lifecycle == 'ephemeral':
            expiry = channel.get('expiry')
            if not isinstance(expiry, dict):
                raise SyncwheelError(f'ephemeral channel {channel_id} requires expiry metadata')
            normalized['expiry'] = {
                'createdAt': normalize_channel_timestamp(
                    expiry.get('createdAt'), f'channel {channel_id} expiry.createdAt'
                ),
                'expiresAt': normalize_channel_timestamp(
                    expiry.get('expiresAt'), f'channel {channel_id} expiry.expiresAt'
                ),
            }
        elif 'expiry' in channel:
            raise SyncwheelError(f'shared channel {channel_id} must not define expiry metadata')
        validate_channel_dependency_order(normalized)
        pin_digest = channel_pin_digest(normalized)
        resolution = normalize_channel_resolution(
            channel.get('resolution'), channel_id, pin_digest
        )
        if resolution:
            normalized['resolution'] = resolution
        seen_ids.add(channel_id)
        seen_branches.add(branch)
        channels.append(normalized)
    return channels


def validate_channel_stack_authority(channels, stacks):
    """Bind every persisted channel entry to its authoritative stack declaration."""
    by_id = {stack['id']: stack for stack in stacks}
    for channel in channels:
        for entry in channel.get('composition', []):
            stack = by_id.get(entry['stack'])
            if stack is None:
                raise SyncwheelError(
                    f"channel {channel['id']} references unknown stack: {entry['stack']}"
                )
            if entry['branch'] != stack['branch']:
                raise SyncwheelError(
                    f"channel {channel['id']} stack {entry['stack']} pinned branch "
                    f"{entry['branch']!r} does not match stack branch {stack['branch']!r}"
                )
            if entry['stackBase'] != stack['base']:
                raise SyncwheelError(
                    f"channel {channel['id']} stack {entry['stack']} pinned stackBase "
                    f"{entry['stackBase']!r} does not match stack base {stack['base']!r}"
                )
            if list(entry.get('dependsOn', [])) != list(stack.get('depends_on', [])):
                raise SyncwheelError(
                    f"channel {channel['id']} stack {entry['stack']} pinned dependencies "
                    'do not match the authoritative stack declaration'
                )


def symbolic_base_branch_for_remote(value, remote):
    if not isinstance(value, str) or not value or re.fullmatch(r'[0-9a-f]{40}', value):
        return None
    remote_ref_prefix = f'refs/remotes/{remote}/'
    if value.startswith(remote_ref_prefix):
        return value[len(remote_ref_prefix):]
    short_remote_prefix = f'{remote}/'
    if value.startswith(short_remote_prefix):
        return value[len(short_remote_prefix):]
    if value.startswith('refs/remotes/'):
        return None
    if value.startswith('refs/heads/'):
        return value[len('refs/heads/'):]
    if value.startswith('refs/') or value in {'HEAD', '@'}:
        return None
    return value


def validate_channel_branch_ownership(manifest):
    """Keep channel refs disjoint from every authoritative repository ref role."""
    defaults = manifest['defaults']
    integration_branch = manifest['integration']['branch']
    coordination = manifest.get('coordination') or {}
    stacks = manifest.get('stacks', [])
    for channel in manifest.get('channels', []):
        branch = channel['branch']
        remote = channel['remote']
        if branch == defaults['base_branch']:
            raise SyncwheelError(
                f"channel {channel['id']} branch {branch!r} overlaps protected "
                f"defaults.base_branch on remote {remote!r}"
            )
        base_authorities = [
            ('channel.base', channel.get('base')),
            ('defaults.base_ref', defaults.get('base_ref')),
            ('integration.base', manifest['integration'].get('base')),
            *(
                (f"stack {stack['id']} base", stack.get('base'))
                for stack in stacks
            ),
        ]
        conflicting_bases = [
            label for label, value in base_authorities
            if symbolic_base_branch_for_remote(value, remote) == branch
        ]
        if conflicting_bases:
            raise SyncwheelError(
                f"channel {channel['id']} branch {branch!r} on remote {remote!r} "
                'overlaps canonical symbolic base(s): ' + ', '.join(conflicting_bases)
            )
        source_owners = sorted(
            stack['id'] for stack in stacks if stack['branch'] == branch
        )
        if branch == integration_branch or source_owners:
            raise SyncwheelError(
                f"channel {channel['id']} branch overlaps a stack or integration branch: "
                f'{branch}'
            )
        target_owners = sorted(
            stack['id'] for stack in stacks
            if stack.get('target_remote') == remote
            and stack.get('target_branch') == branch
        )
        if target_owners:
            raise SyncwheelError(
                f"channel {channel['id']} branch {branch!r} on remote {remote!r} "
                'overlaps target branch owned by stack(s): ' + ', '.join(target_owners)
            )
        if branch == coordination.get('state_branch'):
            raise SyncwheelError(
                f"channel {channel['id']} branch must not overlap coordination.state_branch"
            )


def stack_base_owner_id(stack, stacks):
    base = stack.get('base')
    if not isinstance(base, str):
        # Public coordination snapshots encode remote refs as typed objects.
        # Their explicit depends_on graph is still validated below.
        return None
    candidates = [
        (len(candidate['branch']), candidate['id'])
        for candidate in stacks
        if candidate['id'] != stack['id']
        and (
            base == candidate['branch']
            or base.endswith(f"/{candidate['branch']}")
        )
    ]
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def derive_stack_dependencies(stacks):
    for stack in stacks:
        owner = stack_base_owner_id(stack, stacks)
        if owner and owner not in stack.get('depends_on', []):
            stack['depends_on'] = [*stack.get('depends_on', []), owner]
    return stacks


def validate_stack_dependency_graph(stacks, require_declared_dependencies=True):
    by_id = {stack['id']: stack for stack in stacks}
    for stack in stacks:
        dependencies = stack.get('depends_on', [])
        unknown = sorted(set(dependencies) - set(by_id))
        if unknown:
            raise SyncwheelError(
                f"stack {stack['id']} depends_on unknown stack(s): " + ', '.join(unknown)
            )
        if stack['id'] in dependencies:
            raise SyncwheelError(f"stack {stack['id']} must not depend on itself")
        base_owner = stack_base_owner_id(stack, stacks)
        if (
            require_declared_dependencies
            and base_owner
            and base_owner not in dependencies
        ):
            raise SyncwheelError(
                f"stack {stack['id']} base {stack['base']!r} belongs to stack {base_owner}; "
                f'declare depends_on: ["{base_owner}"]'
            )

    visiting = set()
    visited = set()

    def visit(stack_id, path):
        if stack_id in visiting:
            cycle = path[path.index(stack_id):] + [stack_id]
            raise SyncwheelError('stack dependency cycle: ' + ' -> '.join(cycle))
        if stack_id in visited:
            return
        visiting.add(stack_id)
        for dependency in by_id[stack_id].get('depends_on', []):
            visit(dependency, [*path, dependency])
        visiting.remove(stack_id)
        visited.add(stack_id)

    for stack_id in by_id:
        visit(stack_id, [stack_id])


def normalize_manifest_data(data, path):
    """Normalize parsed control data without inspecting moving refs or a checkout."""
    if not isinstance(data, dict):
        raise SyncwheelError('manifest root must be an object')
    version = data.get('version')
    if version not in MANIFEST_VERSIONS:
        allowed = ', '.join(str(item) for item in sorted(MANIFEST_VERSIONS))
        raise SyncwheelError(f'manifest version must be one of: {allowed}')
    if 'syncwheel_tracking' in data:
        normalize_syncwheel_tracking(data.get('syncwheel_tracking'))
    if 'authority' in data:
        data['authority'] = normalize_authority_policy(data.get('authority'))
    repository_mode = data.get('repository_mode', 'delivery')
    if repository_mode not in REPOSITORY_MODES:
        raise SyncwheelError(
            'repository_mode must be one of: ' + ', '.join(sorted(REPOSITORY_MODES))
        )
    data['repository_mode'] = repository_mode
    if repository_mode == 'journal':
        raw_journal = data.get('journal')
        if not isinstance(raw_journal, dict):
            raise SyncwheelError('journal mode requires a journal object')
        journal = dict(raw_journal)
        for field in ('branch', 'remote'):
            if not isinstance(journal.get(field), str) or not journal[field].strip():
                raise SyncwheelError(f'journal.{field} must be a non-empty string')
        for field in ('include', 'exclude'):
            value = journal.get(field)
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise SyncwheelError(f'journal.{field} must be a string array')
            if field == 'include' and not value:
                raise SyncwheelError('journal.include must be a non-empty explicit allowlist')
            journal[field] = value
        max_bytes = journal.get('max_file_bytes')
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise SyncwheelError('journal.max_file_bytes must be a positive integer')
        journal['max_file_bytes'] = max_bytes
        interval = journal.get('interval', DEFAULT_JOURNAL_INTERVAL)
        if not isinstance(interval, str) or not re.fullmatch(r'[1-9][0-9]*(?:s|min|m|h|d|w)', interval):
            raise SyncwheelError('journal.interval must be a positive systemd duration such as 30m')
        journal['interval'] = interval
        data['journal'] = journal
    data['syncwheel_worktree_root'] = normalize_syncwheel_worktree_root(
        data.get('syncwheel_worktree_root')
    )
    if 'governed_worktree_capacity' in data:
        data['governed_worktree_capacity'] = normalize_governed_worktree_capacity(
            data['governed_worktree_capacity']
        )

    defaults = data.setdefault('defaults', {})
    canonical_remote = defaults.setdefault('canonical_remote', 'origin')
    defaults.setdefault('publication_remote', 'fork')
    defaults.setdefault('base_branch', 'main')
    defaults.setdefault('base_ref', f"{canonical_remote}/{defaults['base_branch']}")
    defaults['integration_membership'] = normalize_integration_membership(
        defaults.get('integration_membership')
    )
    if defaults.get('replay_mode') is not None:
        normalize_replay_mode(defaults['replay_mode'], 'manifest defaults.replay_mode')
    if 'landing' in data:
        data['landing'] = normalize_landing_policy(data['landing'])

    integration = data.setdefault('integration', {})
    integration.setdefault('branch', DEFAULT_INTEGRATION_BRANCH)
    integration.setdefault('base', defaults['base_ref'])
    integration.setdefault('strategy', 'cherry-pick')
    integration.setdefault('stacks', [])
    if version == MANIFEST_VERSION_CHANNELS:
        derived_paths = integration.setdefault('derived_paths', [])
        if (
            not isinstance(derived_paths, list)
            or not all(isinstance(item, str) and item.endswith('/') and item for item in derived_paths)
            or len(derived_paths) != len(set(derived_paths))
        ):
            raise SyncwheelError('integration.derived_paths must be a unique string array of path prefixes')
        if 'derived_provenance' in integration:
            integration['derived_provenance'] = normalize_derived_provenance(
                integration['derived_provenance'],
                label='integration.derived_provenance',
            )
    elif 'derived_paths' in integration:
        raise SyncwheelError('integration.derived_paths requires manifest version 3')
    elif 'derived_provenance' in integration:
        raise SyncwheelError('integration.derived_provenance requires manifest version 3')

    stacks = data.setdefault('stacks', [])
    if not isinstance(stacks, list):
        raise SyncwheelError('manifest stacks must be an array')

    seen_ids = set()
    seen_branches = set()
    normalized = []
    for raw in stacks:
        if not isinstance(raw, dict):
            raise SyncwheelError('each stack entry must be an object')
        stack = dict(raw)
        stack_id = stack.get('id')
        branch = stack.get('branch')
        commits = stack.get('commits', [])
        if not stack_id or not isinstance(stack_id, str):
            raise SyncwheelError('each stack needs a string id')
        if stack_id in seen_ids:
            raise SyncwheelError(f'duplicate stack id: {stack_id}')
        if not branch or not isinstance(branch, str):
            raise SyncwheelError(f'stack {stack_id} needs a branch')
        if branch in seen_branches:
            raise SyncwheelError(f'duplicate stack branch: {branch}')
        if not isinstance(commits, list) or not all(isinstance(c, str) and c for c in commits):
            raise SyncwheelError(f'stack {stack_id} commits must be a string array')
        integration_commits = stack.get('integration_commits')
        if integration_commits is not None and (
            not isinstance(integration_commits, list)
            or not all(isinstance(c, str) and c for c in integration_commits)
        ):
            raise SyncwheelError(
                f'stack {stack_id} integration_commits must be a string array when present'
            )
        resolution = stack.get('integration_resolution')
        if resolution is not None and (
            not isinstance(resolution, dict) or set(resolution) != {'source_tip'}
            or not isinstance(resolution.get('source_tip'), str)
            or not re.fullmatch(r'[0-9a-f]{40,64}', resolution['source_tip'])
            or integration_commits is None
        ):
            raise SyncwheelError(f'stack {stack_id} has an invalid integration_resolution binding')
        integration_only_commits = stack.get('integration_only_commits')
        if integration_only_commits is not None and (
            not isinstance(integration_only_commits, list)
            or not all(isinstance(c, str) and c for c in integration_only_commits)
            or len(integration_only_commits) != len(set(integration_only_commits))
        ):
            raise SyncwheelError(
                f'stack {stack_id} integration_only_commits must be a unique string array when present'
            )
        seen_ids.add(stack_id)
        seen_branches.add(branch)
        stack.setdefault('base', defaults['base_ref'])
        stack.setdefault('target_remote', canonical_remote)
        stack.setdefault('target_branch', defaults['base_branch'])
        stack.setdefault('integration_branch', integration['branch'])
        stack.setdefault('state', 'published')
        if stack['state'] not in STACK_STATES:
            raise SyncwheelError(
                f"stack {stack_id} state must be one of: {', '.join(sorted(STACK_STATES))}"
            )
        stack['publication'] = {'enabled': stack['state'] != 'draft'}
        if 'meta' in stack and not isinstance(stack['meta'], dict):
            raise SyncwheelError(f'stack {stack_id} meta must be an object when present')
        dependencies = stack.get('depends_on', [])
        if (
            not isinstance(dependencies, list)
            or not all(isinstance(item, str) and item for item in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            raise SyncwheelError(f'stack {stack_id} depends_on must be a unique string array')
        if dependencies and version != MANIFEST_VERSION_CHANNELS:
            raise SyncwheelError(
                f'stack {stack_id} depends_on requires manifest version 3; '
                'migrate explicitly with channel create --apply before declaring dependencies'
            )
        if dependencies:
            stack['depends_on'] = list(dependencies)
        else:
            stack.pop('depends_on', None)
        stack.setdefault('meta', {})
        validate_stack_adoption(stack)
        normalized.append(stack)
    data['stacks'] = normalized
    validate_stack_dependency_graph(
        data['stacks'], require_declared_dependencies=version == MANIFEST_VERSION_CHANNELS
    )
    data['channels'] = normalize_manifest_channels(data, defaults, repository_mode)
    validate_channel_stack_authority(data['channels'], data['stacks'])
    if version == MANIFEST_VERSION_CHANNELS and repository_mode != 'delivery':
        raise SyncwheelError('manifest version 3 is supported only for repository_mode="delivery"')
    if data.get('channels') and version != MANIFEST_VERSION_CHANNELS:
        raise SyncwheelError('manifest channels require version 3; migrate explicitly with channel create --apply')
    if version in {MANIFEST_VERSION_COORDINATED, MANIFEST_VERSION_CHANNELS} and repository_mode != 'journal':
        coordination = normalize_coordination(data.get('coordination'), path)
        if coordination['remote'] != defaults['publication_remote']:
            raise SyncwheelError(
                'coordination.remote must match defaults.publication_remote'
            )
        data['coordination'] = coordination
        if version == MANIFEST_VERSION_CHANNELS and coordination['mode'] == 'active-active':
            mismatched = [
                channel['id'] for channel in data['channels']
                if channel['remote'] != coordination['remote']
            ]
            if mismatched:
                raise SyncwheelError(
                    'active-active channel remote must match coordination.remote for: '
                    + ', '.join(mismatched)
                )
    validate_channel_branch_ownership(data)
    return data


def channel_pin_digest(channel):
    return canonical_json_digest({
        'base': channel['base'],
        'baseRevision': channel['baseRevision'],
        'composition': channel.get('composition', []),
    })


def safe_ref_segment(value):
    cleaned = value.strip().replace('\\', '/').strip('/')
    if not cleaned or cleaned.startswith('.') or '..' in cleaned:
        raise SyncwheelError(f'invalid ref segment: {value!r}')
    disallowed = set(' ~^:?*[')
    if any(char in disallowed for char in cleaned):
        raise SyncwheelError(f'invalid ref segment: {value!r}')
    if cleaned.endswith('.lock') or cleaned.endswith('/'):
        raise SyncwheelError(f'invalid ref segment: {value!r}')
    return cleaned


def canonical_json_digest(value):
    canonical = json.dumps(value, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


REPLAY_MODE_CHOICES = ('auto', 'plumbing', 'in-place', 'ephemeral', 'desk')


def normalize_replay_mode(value, source):
    if value not in REPLAY_MODE_CHOICES:
        allowed = ', '.join(REPLAY_MODE_CHOICES)
        raise SyncwheelError(f'invalid {source}: {value!r}; expected one of: {allowed}')
    return value


def stack_integration_base_commits(stack):
    """Return the source or resolved commits that materialize a stack on integration.

    Source commits remain authoritative for rebuilding the stack branch. A resolved
    integration projection can be recorded separately after conflict resolution so
    validation never asks Syncwheel to rewrite that source branch with integration
    commits.
    """
    return list(stack.get('integration_commits', stack['commits']))


def bound_merge_stack_resolution_commits(stack):
    """Only input-bound replacements participate in merge-stack replay."""
    return stack_integration_base_commits(stack) if stack.get('integration_resolution') else None


def stack_integration_only_commits(stack):
    """Return commits owned by a stack only in its integration projection."""
    return list(stack.get('integration_only_commits') or [])


def validate_stack_adoption(stack):
    """Bind an explicit history interval to its pinned replay recipe."""
    adopted = stack.get('adopted_history')
    if adopted is None:
        return None
    if (not isinstance(adopted, dict) or set(adopted) != {'base', 'tip'}
            or any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40,64}', value)
                   for value in adopted.values())):
        raise SyncwheelError('adopted_history requires full base and tip object IDs')
    expected = [] if adopted['tip'] == adopted['base'] else [adopted['tip']]
    if stack['base'] != adopted['base'] or stack['commits'] != expected:
        raise SyncwheelError('adopted history is stale; use stack set --base BASE --adopt-tip TIP')
    return adopted


def stack_integration_commits(stack):
    """Return every commit owned by a stack in the integration projection."""
    return list(dict.fromkeys([
        *stack_integration_base_commits(stack),
        *stack_integration_only_commits(stack),
    ]))


def stack_delivery_identity(stack):
    """Return fields that determine which product a stack revision delivers."""
    return {
        'branch': stack.get('branch'),
        'base': stack.get('base'),
        'state': stack.get('state', 'published'),
        'target_remote': stack.get('target_remote'),
        'target_branch': stack.get('target_branch'),
        'integration_branch': stack.get('integration_branch'),
        'depends_on': list(stack.get('depends_on') or []),
        'commits': list(stack.get('commits') or []),
        'integration_commits': stack_integration_base_commits(stack),
        'integration_only_commits': stack_integration_only_commits(stack),
        **({'adopted_history': stack['adopted_history']} if stack.get('adopted_history') else {}),
    }


def stack_map(manifest):
    return {stack['id']: stack for stack in manifest.get('stacks', [])}


def syncwheel_worktree_root(manifest):
    if not manifest:
        return DEFAULT_SYNCWHEEL_WORKTREE_ROOT
    return normalize_syncwheel_worktree_root(manifest.get('syncwheel_worktree_root'))


def resolve_worktree_root_path(repo_root, worktree_root):
    root = Path(worktree_root or DEFAULT_SYNCWHEEL_WORKTREE_ROOT).expanduser()
    if not root.is_absolute():
        root = repo_root / root
    return root.resolve()


def effective_worktree_root(manifest, override=None):
    return override or syncwheel_worktree_root(manifest)


def path_is_relative_to(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def require_stack(manifest, stack_id):
    stacks = stack_map(manifest)
    if stack_id not in stacks:
        raise SyncwheelError(f'unknown stack: {stack_id}')
    return stacks[stack_id]


def coordination_state_ref(config):
    return f"refs/heads/{config['state_branch']}"


def manifest_digest(manifest):
    return hashlib.sha256(canonical_manifest_json(manifest).encode('utf-8')).hexdigest()


def canonical_manifest_json(manifest):
    """Return the semantic manifest representation used for stable identities."""
    return json.dumps(manifest, sort_keys=True, separators=(',', ':'))


def channel_composition_digest(channel):
    return canonical_json_digest({
        'pinDigest': channel_pin_digest(channel),
        'resolution': channel.get('resolution'),
    })


def integration_composition_digest(manifest):
    """Digest only declared integration composition, not unrelated manifest metadata."""
    integration = manifest['integration']
    stacks = stack_map(manifest)
    return canonical_json_digest({
        'base': integration['base'],
        'strategy': integration['strategy'],
        'stacks': [
            {
                'id': stack_id,
                'commits': list(stacks[stack_id].get('commits') or []),
                'integration_commits': list(stacks[stack_id].get('integration_commits') or []),
                'integration_only_commits': list(stacks[stack_id].get('integration_only_commits') or []),
            }
            for stack_id in integration.get('stacks') or []
            if stack_id in stacks
        ],
    })

SYNCWHEEL_TRACKING_GIT_TRACKED = 'git-tracked'
