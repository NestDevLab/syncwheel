"""User defaults, repository overrides and logical worktree admission policy.

These preferences never authorize removal, change lane records or replace Git
locks, registry CAS, publication leases or destructive-operation proofs.
"""

import copy
import json
import sys

from syncwheel_errors import SyncwheelError


POLICY_KEY = 'worktree_policy'
POLICY_VALUES = {
    'expiry': ('enforced', 'off'),
    'writer_exclusivity': ('block', 'warn', 'off'),
}
DEFAULT_POLICY = {'expiry': 'enforced', 'writer_exclusivity': 'legacy'}


def policy_values(document, label):
    value = document.get(POLICY_KEY, {})
    if not isinstance(value, dict):
        raise SyncwheelError(f'{label}.{POLICY_KEY} must be an object')
    for key, setting in value.items():
        if key not in POLICY_VALUES or setting not in POLICY_VALUES[key]:
            raise SyncwheelError(f'invalid {label}.{POLICY_KEY} setting: {key}={setting!r}')
    return dict(value)


def resolve_policy(global_document, repo_document=None):
    global_values = policy_values(global_document, 'settings')
    repo_values = policy_values(repo_document or {}, 'profile')
    effective = dict(DEFAULT_POLICY)
    sources = dict.fromkeys(DEFAULT_POLICY, 'default')
    for source, values in (('global', global_values), ('repo', repo_values)):
        effective.update(values)
        sources.update(dict.fromkeys(values, source))
    return {'effective': effective, 'sources': sources,
            'global': global_values, 'repo': repo_values}


def edit_policy(document, *, values=None, clear=()):
    proposed = copy.deepcopy(document)
    policy = policy_values(proposed, 'configuration')
    policy.update(values or {})
    for field in clear:
        policy.pop(field, None)
    if policy:
        proposed[POLICY_KEY] = policy
    else:
        proposed.pop(POLICY_KEY, None)
    policy_values(proposed, 'configuration')
    return proposed


def run_policy_command(args, *, settings_path, load_json, write_json,
                       profile_path=None, load_profile=None, write_profile=None):
    """Edit only the selected private configuration, preserving other keys."""
    global_document = load_json(settings_path, {})
    repo_document = load_profile() if profile_path is not None else None
    is_global = args.policy_scope == 'global'
    path = settings_path if is_global else profile_path
    current = global_document if is_global else repo_document
    operation = args.policy_action
    proposed = current
    if operation == 'set':
        values = {field: getattr(args, field) for field in POLICY_VALUES
                  if getattr(args, field, None) is not None}
        if not values:
            raise SyncwheelError('worktree-policy set requires --expiry or --writer-exclusivity')
        proposed = edit_policy(current, values=values)
    elif operation == 'clear':
        fields = [field.replace('-', '_') for field in args.fields] or list(POLICY_VALUES)
        proposed = edit_policy(current, clear=fields)
    report = resolve_policy(proposed if is_global else global_document,
                            repo_document if is_global else proposed)
    report.update(scope=args.policy_scope, path=str(path), applied=False)
    if operation != 'status':
        report['proposed'] = proposed
        if args.apply:
            if is_global:
                write_json(path, proposed)
            else:
                write_profile(proposed)
            report['applied'] = True
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"scope: {report['scope']} ({path})")
        for field, value in report['effective'].items():
            print(f"{field}: {value} ({report['sources'][field]})")
        if operation != 'status':
            print('applied' if report['applied'] else 'preview; rerun with --apply')
    return 0


def foreign_writer_message(lane, actor):
    if lane.get('state') != 'active' or lane.get('owner') == actor:
        return None
    return (f"governed worktree {lane['id']} belongs to writer {lane.get('owner')!r}; "
            f'current writer is {actor!r}')


def admit_writers(lanes, actor, policy):
    """Explicit writer policy for non-destructive admission; legacy adds no gate."""
    mode = policy['writer_exclusivity']
    if mode in {'legacy', 'off'}:
        return
    conflicts = [message for lane in lanes
                 if (message := foreign_writer_message(lane, actor))]
    if conflicts and mode == 'block':
        raise SyncwheelError('; '.join(conflicts))
    for message in conflicts:
        print(f'WARNING: {message} (writer_exclusivity=warn)', file=sys.stderr)


def scoped_recovery_blockers(lanes, health, policy, actor):
    """Keep lifecycle/physical failures blocking when logical ownership is relaxed."""
    blockers = []
    for lane in lanes:
        if lane.get('state') == 'reaped' or lane.get('pending_reason') == 'archive_push_failed':
            continue
        if policy['writer_exclusivity'] == 'legacy':
            blockers.append(lane)
            continue
        observed = health.get(lane['id'])
        if lane.get('state') != 'active' or observed is None or observed.get('code'):
            blockers.append(lane)
    admit_writers([lane for lane in lanes if lane not in blockers], actor, policy)
    return blockers
