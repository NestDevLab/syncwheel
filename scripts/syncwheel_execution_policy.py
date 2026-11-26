"""Optional candidate gates and explicit, SHA-bound validation receipts.

Receipts record evidence supplied by an operator; they do not execute commands,
claim CI success or grant authority. Forge requirements remain in the merge owner.
Installation smoke is release evidence, never a circular merge prerequisite.
"""

import copy
import json
import re
from pathlib import Path

from syncwheel_errors import SyncwheelError
from syncwheel_policy import resolve_preferences
from syncwheel_worktree_policy import lane_document

EVIDENCE_KEY = 'execution_evidence'
KINDS = ('tests', 'review', 'installation_smoke')


def resolve_context(global_document, profile, *, lane=None, lane_generation,
                    checkout_path=None, load_lanes=None, normalize_path=None, current_context=True):
    if lane is None and current_context and load_lanes is not None:
        matches = [item for item in load_lanes()
                   if item.get('state') != 'reaped'
                   and normalize_path(item['path']) == checkout_path]
        if len(matches) > 1:
            raise SyncwheelError('current checkout has ambiguous governed lane registration')
        lane = matches[0] if matches else None
    selected, binding = {}, None
    if lane is not None:
        selected, binding = lane_document(profile, {
            'id': lane['id'], 'generation_token': lane_generation(lane),
        })
    result = resolve_preferences(global_document, profile, selected)
    if binding:
        result['lane_binding'] = binding
    return result


def normalize_receipt(value):
    if not isinstance(value, dict):
        raise SyncwheelError('execution evidence receipt must be an object')
    head = value.get('head_sha')
    kind = value.get('kind')
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1 or kind not in KINDS:
        raise SyncwheelError('unsupported execution evidence receipt schema or kind')
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        raise SyncwheelError('execution evidence requires the exact candidate head_sha')
    for field in ('source', 'reference', 'verified_by'):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise SyncwheelError('execution evidence requires ' + field)
    if value.get('outcome') != 'success':
        raise SyncwheelError('only successful verified evidence can satisfy a gate')
    if kind == 'tests' and value.get('scope') not in ('targeted', 'extended'):
        raise SyncwheelError('test evidence requires targeted or extended scope')
    if kind == 'review':
        if value.get('scope') not in ('sensitive', 'all'):
            raise SyncwheelError('review evidence requires sensitive or all scope')
        for field in ('reviewer', 'author'):
            if not isinstance(value.get(field), str) or not value[field].strip():
                raise SyncwheelError('review evidence requires ' + field)
        if value['reviewer'].casefold() == value['author'].casefold():
            raise SyncwheelError('review evidence must come from an independent reviewer')
    if kind == 'installation_smoke':
        digest = value.get('package_sha256')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise SyncwheelError('installation smoke requires the actual installed package_sha256')
        if value.get('installed') is not True:
            raise SyncwheelError('installation smoke must exercise the installed package')
    return copy.deepcopy(value)


def record_receipts(profile, receipts):
    proposed = copy.deepcopy(profile)
    evidence = proposed.setdefault(EVIDENCE_KEY, {})
    if not isinstance(evidence, dict):
        raise SyncwheelError('execution_evidence must be an object')
    for raw in receipts:
        receipt = normalize_receipt(raw)
        bucket = evidence.setdefault(receipt['head_sha'], {})
        if not isinstance(bucket, dict):
            raise SyncwheelError('candidate execution evidence must be an object')
        existing = bucket.get(receipt['kind'])
        if existing is not None and existing != receipt:
            raise SyncwheelError('execution evidence collision for this head and kind; retain the original receipt')
        bucket[receipt['kind']] = receipt
    return proposed


def evidence_for(profile, head):
    evidence = profile.get(EVIDENCE_KEY, {})
    if not isinstance(evidence, dict) or not isinstance(evidence.get(head, {}), dict):
        raise SyncwheelError('invalid execution evidence store')
    selected = {}
    for kind, raw in evidence.get(head, {}).items():
        receipt = normalize_receipt(raw)
        if kind != receipt['kind'] or receipt['head_sha'] != head:
            raise SyncwheelError('execution evidence identity mismatch')
        selected[kind] = receipt
    return selected


def candidate_gates(report, profile, head, *, sensitive=True, checks=(), successful_check=None):
    """Return optional blockers and consumed evidence, without changing any floor."""
    policy = report['effective']
    receipts = evidence_for(profile, head)
    blockers, consumed = [], {}
    tests = policy['tests']
    if tests != 'legacy' and tests != 'off':
        receipt = receipts.get('tests')
        valid = receipt is not None and (tests != 'extended' or receipt['scope'] == 'extended')
        # A CI receipt names an existing check, whose exact-head live observation
        # must still succeed; local verified receipts carry their original source.
        if valid and receipt['source'] == 'ci':
            valid = successful_check is not None and any(
                check.get('name') == receipt['reference'] and check.get('headSha') == head
                and successful_check(check, head)
                for check in checks if isinstance(check, dict))
        if valid:
            consumed['tests'] = receipt
        else:
            blockers.append({'code': 'targeted_test_evidence_missing',
                             'detail': 'record successful test evidence for the exact candidate SHA'})
    review = policy['independent_review']
    if review == 'required' or (review == 'sensitive' and sensitive):
        receipt = receipts.get('review')
        if receipt is not None and (review != 'required' or receipt['scope'] == 'all'):
            consumed['review'] = receipt
        else:
            blockers.append({'code': 'independent_review_evidence_missing',
                             'detail': 'record independent review evidence for the exact candidate SHA'})
    return blockers, consumed


def release_status(report, profile, head):
    if head is not None and (not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head)):
        raise SyncwheelError('release status requires the exact candidate SHA')
    receipts = evidence_for(profile, head)
    required = report['effective']['installation_smoke'] == 'required'
    smoke = receipts.get('installation_smoke')
    return {'head_sha': head, 'required': required, 'evidence': smoke,
            'status': 'pending' if required and smoke is None else 'complete'}


def run_evidence_command(args, *, profile, write_profile, report):
    if args.policy_action == 'record':
        receipts = []
        for name in args.receipt:
            try:
                receipts.append(json.loads(Path(name).read_text()))
            except (OSError, ValueError) as exc:
                raise SyncwheelError('cannot read execution evidence receipt: ' + str(exc)) from exc
        proposed = record_receipts(profile, receipts)
        result = {'applied': False, 'proposed': proposed,
                  'notice': 'operator-verified evidence recorded; no command was executed'}
        if args.apply:
            write_profile(proposed)
            result['applied'] = True
    else:
        result = release_status(report, profile, args.head)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get('status') != 'pending' else 1


def apply_merge_floor(report, floor):
    from syncwheel_policy import FIELD_VALUES
    result = copy.deepcopy(report)
    result['floor'] = dict(floor)
    for field, minimum in floor.items():
        current = result['effective'][field]
        if current == 'legacy' or FIELD_VALUES[field].index(current) < FIELD_VALUES[field].index(minimum):
            result['effective'][field] = minimum
            result['sources'][field] = 'floor'
            result['provenance'][field] = {
                'scope': 'floor', 'kind': 'mandatory', 'authority': 'explicit clone-local merge policy',
                'requested_provenance': result['provenance'][field],
            }
    return result


def sensitive_paths(paths):
    # Instructions and unknown files are sensitive. Ordinary documentation-only
    # changes may omit the optional sensitive review.
    instruction_names = {'AGENT.md', 'AGENTS.md', 'SKILL.md'}
    return any(Path(path).name in instruction_names or not path.endswith(('.md', '.rst', '.txt'))
               for path in paths)


def merge_floor(profile):
    declared = profile.get('github_pr_merge', {})
    if not isinstance(declared, dict):
        raise SyncwheelError('github_pr_merge must be an object')
    floor = {}
    if declared.get('checks') == 'all':
        floor['checks'] = 'all'
    if declared.get('integration') == 'required':
        floor['integration_validation'] = 'required'
    return floor


def operational_selected(report):
    from syncwheel_policy import WORKTREE_FIELDS
    return any(source != 'default' for field, source in report['sources'].items()
               if field not in WORKTREE_FIELDS)


def optional_merge_policy(policy, *, operational):
    result = copy.deepcopy(policy)
    if operational:
        # These private historical filters are not repository/forge authority.
        # Live principal permissions, repo identity and required rules remain
        # with the merge owner and cannot be supplied by a local receipt.
        result['allowed_bypasses'] = []
        result.pop('pr_authors', None)
        result.pop('commit_authors', None)
        result.pop('merge_actors', None)
    return result
