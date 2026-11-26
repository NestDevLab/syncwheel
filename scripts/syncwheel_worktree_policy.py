"""User defaults, repository overrides and logical worktree admission policy.

These preferences never authorize removal, change lane records or replace Git
locks, registry CAS, publication leases or destructive-operation proofs.
"""

import copy
import json
import sys

from syncwheel_errors import SyncwheelError
from syncwheel_policy import (
    POLICY_KEY, WORKTREE_FIELDS, FIELD_VALUES, DEFAULT_PREFERENCES,
    configuration_values, resolve_preferences,
)


POLICY_VALUES = {field: tuple(reversed(FIELD_VALUES[field])) for field in WORKTREE_FIELDS}
DEFAULT_POLICY = {field: DEFAULT_PREFERENCES[field] for field in WORKTREE_FIELDS}
LANE_POLICY_KEY = 'lane_worktree_policies'


def policy_values(document, label):
    return configuration_values(document, label, fields=WORKTREE_FIELDS, allow_presets=True)


def resolve_policy(global_document, repo_document=None, *, lane_document=None):
    report = resolve_preferences(global_document, repo_document, lane_document,
                                 fields=WORKTREE_FIELDS, allow_presets=True)
    keys = ('effective', 'sources', 'global', 'repo')
    if lane_document is not None:
        keys += ('lane',)
    return {key: report[key] for key in keys}



def require_lane(lanes, lane_id, *, allow_reaped=False, allow_missing=False):
    matches = [lane for lane in lanes if lane.get('id') == lane_id]
    if not matches and allow_missing:
        return None
    if len(matches) != 1 or (matches[0].get('state') == 'reaped' and not allow_reaped):
        raise SyncwheelError(f'no current governed lane with id: {lane_id}')
    return matches[0]


def lane_entries(profile):
    entries = profile.get(LANE_POLICY_KEY, {})
    if not isinstance(entries, dict):
        raise SyncwheelError(f'profile.{LANE_POLICY_KEY} must be an object')
    return entries


def lane_document(profile, binding):
    """Unusable generations inherit; no old override follows a recycled lane id."""
    entries = lane_entries(profile)
    report = dict(binding, state='missing' if binding['generation_token'] is None else 'inherited')
    if binding['id'] not in entries:
        return {}, report
    entry = entries[binding['id']]
    if not isinstance(entry, dict) or not isinstance(entry.get('generation_token'), str):
        report['state'] = 'invalid'
        return {}, report
    recorded = entry['generation_token']
    if binding['generation_token'] is None:
        report['recorded_generation_token'] = recorded
        return {}, report
    if not recorded or recorded != binding['generation_token']:
        report.update(state='stale', recorded_generation_token=recorded)
        return {}, report
    if set(entry) - {'generation_token', POLICY_KEY}:
        report['state'] = 'invalid'
        return {}, report
    selected = {POLICY_KEY: entry.get(POLICY_KEY, {})}
    try:
        configuration_values(selected, 'lane')
    except SyncwheelError:
        report['state'] = 'invalid'
        return {}, report
    report['state'] = 'bound'
    return selected, report


def effective_for_context(global_document, profile, *, lane=None, lane_generation,
                          checkout_path=None, load_lanes=None, normalize_path=None, current_context=True):
    """Select only the actual checkout's lane, or the explicitly supplied record."""
    entries = lane_entries(profile)
    if lane is None and current_context and entries and load_lanes is not None:
        matches = [item for item in load_lanes()
                   if item.get('state') != 'reaped'
                   and normalize_path(item['path']) == checkout_path]
        if len(matches) > 1:
            raise SyncwheelError('current checkout has ambiguous governed lane registration')
        lane = matches[0] if matches else None
    if lane is None or lane.get('id') not in entries:
        return resolve_policy(global_document, profile)
    binding = {'id': lane['id'], 'generation_token': lane_generation(lane)}
    selected, report = lane_document(profile, binding)
    result = resolve_policy(global_document, profile, lane_document=selected)
    result['lane_binding'] = report
    if report['state'] in {'stale', 'invalid'}:
        print(f"WARNING: lane policy binding for {binding['id']} is {report['state']}; "
              'ignoring its overrides and inheriting user/repository preferences', file=sys.stderr)
    return result


def edit_lane_policy(profile, binding, *, values=None, clear=(), bind_generation=None, execution=False):
    selected, report = lane_document(profile, binding)
    if values and (not isinstance(binding['generation_token'], str) or not binding['generation_token']):
        raise SyncwheelError('lane preference updates require a current registry generation')
    if bind_generation is not None and bind_generation != binding['generation_token']:
        raise SyncwheelError('requested lane generation is not current; read lane policy status again')
    if values and report['state'] in {'stale', 'invalid'} and bind_generation is None:
        raise SyncwheelError(
            'lane policy binding is stale or invalid; pass --bind-generation '
            + binding['generation_token'] + ' to start new overrides, or clear it'
        )
    proposed = copy.deepcopy(profile)
    entries = copy.deepcopy(lane_entries(profile))
    if clear and report['state'] in {'stale', 'invalid', 'missing'}:
        # Clear-all can retire an obsolete binding without reactivating it.
        if set(clear) == set((*FIELD_VALUES, 'preset') if execution else POLICY_VALUES):
            entries.pop(binding['id'], None)
        else:
            entry = entries.get(binding['id'])
            if (report['state'] == 'invalid' or not isinstance(entry, dict)
                    or set(entry) - {'generation_token', POLICY_KEY}
                    or not isinstance(entry.get(POLICY_KEY), dict)):
                raise SyncwheelError('invalid lane policy binding; clear all fields to remove it')
            for field in clear:
                entry[POLICY_KEY].pop(field, None)
            if not entry[POLICY_KEY]:
                entries.pop(binding['id'], None)
    else:
        updated = edit_policy(selected, values=values, clear=clear, execution=execution)
        if updated.get(POLICY_KEY):
            entries[binding['id']] = {
                'generation_token': binding['generation_token'],
                POLICY_KEY: updated[POLICY_KEY],
            }
        else:
            entries.pop(binding['id'], None)
    if entries:
        proposed[LANE_POLICY_KEY] = entries
    else:
        proposed.pop(LANE_POLICY_KEY, None)
    return proposed


def save_bound_lane_profile(proposed, binding, *, registry_lock, load_lanes,
                            lane_generation, write_profile, allow_reaped=False):
    """Lock order is registry then profile CAS; never write the registry itself."""
    with registry_lock():
        current = require_lane(load_lanes(), binding['id'], allow_reaped=allow_reaped,
                               allow_missing=binding['generation_token'] is None)
        current_generation = lane_generation(current) if current is not None else None
        if current_generation != binding['generation_token']:
            raise SyncwheelError('lane generation changed concurrently; read lane policy status again')
        write_profile(proposed)


def edit_policy(document, *, values=None, clear=(), execution=False):
    proposed = copy.deepcopy(document)
    configuration_values(proposed, 'configuration')
    policy = dict(proposed.get(POLICY_KEY, {}))
    policy.update(values or {})
    for field in clear:
        policy.pop(field, None)
    if policy:
        proposed[POLICY_KEY] = policy
    else:
        proposed.pop(POLICY_KEY, None)
    configuration_values(proposed, 'configuration')
    return proposed


def run_policy_command(args, *, settings_path, load_json, write_json,
                       profile_path=None, load_profile=None, write_profile=None,
                       registry_lock=None, load_lanes=None, lane_generation=None, context_report=None):
    """Edit only the selected private configuration, preserving other keys."""
    global_document = load_json(settings_path, {})
    repo_document = load_profile() if profile_path is not None else None
    is_global = args.policy_scope == 'global'
    is_lane = args.policy_scope == 'lane'
    operation = args.policy_action
    execution = getattr(args, 'execution_policy', False)
    fields = FIELD_VALUES if execution else POLICY_VALUES
    resolve = resolve_preferences if execution else resolve_policy
    if is_lane:
        selected_lane = require_lane(
            load_lanes(), args.lane, allow_reaped=operation != 'set', allow_missing=operation != 'set',
        )
        lane_binding = {
            'id': args.lane,
            'generation_token': lane_generation(selected_lane) if selected_lane is not None else None,
        }
    if execution and operation in {'record', 'release-status'}:
        from syncwheel_execution_policy import run_evidence_command
        return run_evidence_command(args, profile=repo_document, write_profile=write_profile,
                                    report=(context_report() if context_report is not None and operation == 'release-status' else
                                            resolve_preferences(global_document, repo_document)))
    path = settings_path if is_global else profile_path
    current = global_document if is_global else repo_document
    if (is_lane and lane_binding['generation_token'] is None
            and lane_binding['id'] not in lane_entries(repo_document)):
        raise SyncwheelError('no current lane or stored override with this id')
    proposed = current
    if operation == 'set':
        values = {field: getattr(args, field) for field in fields
                  if getattr(args, field, None) is not None}
        if execution and getattr(args, 'preset', None) is not None:
            values['preset'] = args.preset
        if not values:
            raise SyncwheelError('policy set requires a preference field or --preset')
        proposed = (edit_lane_policy(current, lane_binding, values=values,
                                     bind_generation=getattr(args, 'bind_generation', None), execution=execution)
                    if is_lane else edit_policy(current, values=values, execution=execution))
    elif operation == 'clear':
        fields = [field.replace('-', '_') for field in args.fields] or list(fields) + (['preset'] if execution else [])
        proposed = (edit_lane_policy(current, lane_binding, clear=fields, execution=execution)
                    if is_lane else edit_policy(current, clear=fields, execution=execution))
    report = resolve(proposed if is_global else global_document,
                            repo_document if is_global else proposed)
    if is_lane:
        selected, binding_report = lane_document(proposed, lane_binding)
        report = (resolve_preferences(global_document, proposed, selected) if execution else
                  resolve_policy(global_document, proposed, lane_document=selected))
        report['lane_binding'] = binding_report
    if execution and not is_global:
        from syncwheel_execution_policy import apply_merge_floor, merge_floor
        report = apply_merge_floor(report, merge_floor(proposed))
    report.update(scope=args.policy_scope, path=str(path), applied=False)
    if operation != 'status':
        report['proposed'] = proposed
        if args.apply:
            if is_global:
                write_json(path, proposed)
            elif is_lane:
                save_bound_lane_profile(
                    proposed, lane_binding, registry_lock=registry_lock,
                    load_lanes=load_lanes, lane_generation=lane_generation,
                    write_profile=write_profile, allow_reaped=operation == 'clear',
                )
            else:
                write_profile(proposed)
            report['applied'] = True
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"scope: {report['scope']} ({path})")
        if is_lane:
            binding_report = report['lane_binding']
            print(f"lane: {binding_report['id']} (generation={binding_report['generation_token']}; "
                  f"binding={binding_report['state']})")
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


def admit_writers(lanes, actor, policy, *, policy_for_lane=None):
    """Explicit writer policy for non-destructive admission; legacy adds no gate."""
    if policy_for_lane is not None:
        for lane in lanes:
            if foreign_writer_message(lane, actor) is not None:
                admit_writers([lane], actor, policy_for_lane(lane))
        return
    mode = policy['writer_exclusivity']
    if mode in {'legacy', 'off'}:
        return
    conflicts = [message for lane in lanes
                 if (message := foreign_writer_message(lane, actor))]
    if conflicts and mode == 'block':
        raise SyncwheelError('; '.join(conflicts))
    for message in conflicts:
        print(f'WARNING: {message} (writer_exclusivity=warn)', file=sys.stderr)


def scoped_recovery_blockers(lanes, health, policy, actor, *, policy_for_lane=None):
    """Keep lifecycle/physical failures blocking when logical ownership is relaxed."""
    blockers = []
    policies = {}

    def selected(lane):
        if lane['id'] not in policies:
            policies[lane['id']] = policy_for_lane(lane) if policy_for_lane else policy
        return policies[lane['id']]

    for lane in lanes:
        if lane.get('state') == 'reaped' or lane.get('pending_reason') == 'archive_push_failed':
            continue
        selected_policy = selected(lane)
        if selected_policy['writer_exclusivity'] == 'legacy':
            blockers.append(lane)
            continue
        observed = health.get(lane['id'])
        if lane.get('state') != 'active' or observed is None or observed.get('code'):
            blockers.append(lane)
    admit_writers([lane for lane in lanes if lane not in blockers], actor, policy,
                  policy_for_lane=selected if policy_for_lane else None)
    return blockers
