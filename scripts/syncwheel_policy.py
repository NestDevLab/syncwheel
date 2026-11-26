"""Pure preference resolution; admission and mandatory contracts remain with owners.

This module does not execute tests, observe reviews, authorize writes or infer a
floor from private preferences. Callers supply any mandatory field constraints
from their independently verified authority/contract source.
"""

from syncwheel_errors import SyncwheelError


POLICY_KEY = 'worktree_policy'
WORKTREE_FIELDS = ('expiry', 'writer_exclusivity')
FIELD_VALUES = {
    'expiry': ('off', 'enforced'),
    'writer_exclusivity': ('off', 'warn', 'block'),
    'checks': ('none', 'required', 'all'),
    'integration_validation': ('advisory', 'required'),
    'tests': ('off', 'targeted', 'extended'),
    'independent_review': ('off', 'sensitive', 'required'),
    'installation_smoke': ('off', 'required'),
}
DEFAULT_PREFERENCES = {
    'expiry': 'enforced',
    'writer_exclusivity': 'legacy',
    **{field: 'legacy' for field in FIELD_VALUES if field not in WORKTREE_FIELDS},
}
# A preset selects optional requirements. It is never a receipt that they ran.
PRESETS = {
    'disabled': {
        'expiry': 'off',
        'writer_exclusivity': 'off',
        'checks': 'none',
        'integration_validation': 'advisory',
        'tests': 'off',
        'independent_review': 'off',
        'installation_smoke': 'off',
    },
    'minimal': {
        'expiry': 'off',
        'writer_exclusivity': 'warn',
        'checks': 'required',
        'integration_validation': 'advisory',
        'tests': 'targeted',
        'independent_review': 'sensitive',
        'installation_smoke': 'required',
    },
    'standard': {
        'expiry': 'off',
        'writer_exclusivity': 'warn',
        'checks': 'all',
        'integration_validation': 'advisory',
        'tests': 'targeted',
        'independent_review': 'required',
        'installation_smoke': 'required',
    },
    'strict': {
        'expiry': 'off',
        'writer_exclusivity': 'warn',
        'checks': 'all',
        'integration_validation': 'required',
        'tests': 'extended',
        'independent_review': 'required',
        'installation_smoke': 'required',
    },
}


def configuration_values(document, label, *, fields=None, allow_presets=True):
    """Expand one level, with explicit fields taking precedence over its preset."""
    selected = tuple(FIELD_VALUES if fields is None else fields)
    if not isinstance(document, dict):
        raise SyncwheelError(f'{label} policy document must be an object')
    value = document.get(POLICY_KEY, {})
    if not isinstance(value, dict):
        raise SyncwheelError(f'{label}.{POLICY_KEY} must be an object')
    preset = value.get('preset') if allow_presets else None
    if allow_presets and 'preset' in value and (not isinstance(preset, str) or preset not in PRESETS):
        raise SyncwheelError(f'invalid {label}.{POLICY_KEY} preset: {preset!r}')
    expanded = {field: PRESETS[preset][field] for field in selected} if preset else {}
    for field, setting in value.items():
        if field == 'preset':
            if not isinstance(setting, str) or setting not in PRESETS:
                raise SyncwheelError(f'invalid {label}.{POLICY_KEY} preset: {setting!r}')
            if allow_presets:
                continue
            raise SyncwheelError(f'{label} presets require execution-policy')
        if field not in FIELD_VALUES or setting not in FIELD_VALUES[field]:
            raise SyncwheelError(f'invalid {label}.{POLICY_KEY} setting: {field}={setting!r}')
        if field in selected:
            expanded[field] = setting
    return expanded


def resolve_preferences(global_document, repo_document=None, lane_document=None, *,
                        floor=None, fields=None, allow_presets=True):
    """Resolve private levels, then strengthen fields to the supplied mandatory floor.

    Lane selection/binding belongs to the caller. A PID or owner string does not
    identify a lane here: this function receives an already selected document.
    No preset changes Git/CAS/ownership, authority or forge enforcement.
    """
    selected = tuple(FIELD_VALUES if fields is None else fields)
    if any(field not in FIELD_VALUES for field in selected):
        raise SyncwheelError('unsupported policy field selection')
    effective = {field: DEFAULT_PREFERENCES[field] for field in selected}
    sources = dict.fromkeys(selected, 'default')
    details = {field: {'scope': 'default', 'kind': 'default'} for field in selected}
    layers = {}
    for scope, document in (('global', global_document),
                            ('repo', {} if repo_document is None else repo_document),
                            ('lane', {} if lane_document is None else lane_document)):
        values = configuration_values(document, scope, fields=selected,
                                      allow_presets=allow_presets)
        layers[scope] = values
        explicit = document.get(POLICY_KEY, {})
        for field, value in values.items():
            effective[field] = value
            sources[field] = scope
            details[field] = {'scope': scope, 'kind': 'override' if field in explicit else 'preset'}
            if details[field]['kind'] == 'preset':
                details[field]['preset'] = explicit['preset']
    requested = dict(effective)
    if floor is None:
        floor = {}
    if not isinstance(floor, dict):
        raise SyncwheelError('mandatory policy floor must be an object')
    for field, minimum in floor.items():
        if field not in selected or minimum not in FIELD_VALUES[field]:
            raise SyncwheelError(f'invalid mandatory policy floor: {field}={minimum!r}')
        order = FIELD_VALUES[field]
        current = effective[field]
        # Legacy has no preference strength. An explicit mandatory constraint
        # still applies, rather than silently inheriting an unspecified value.
        if current == 'legacy' or order.index(current) < order.index(minimum):
            requested_provenance = dict(details[field])
            effective[field] = minimum
            sources[field] = 'floor'
            details[field] = {'scope': 'floor', 'kind': 'mandatory',
                              'requested_provenance': requested_provenance}
    return {'effective': effective, 'sources': sources, 'provenance': details,
            'requested': requested, 'floor': dict(floor), **layers}
