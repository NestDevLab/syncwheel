"""Scenario acceptance provenance and live source verification.

The shared decision belongs to its source owner (for example a forge review),
not this clone's cache. A stored receipt or agent assessment never authenticates
an approver. Consumers re-observe the source on every applicability check.
"""

import copy
from datetime import date, datetime
import hashlib
import json
import re

from syncwheel_errors import SyncwheelError

EVIDENCE_KEY = 'acceptance_evidence'
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
PAYLOAD_FIELDS = {'schema_version', 'kind', 'scenario_id', 'scenario', 'person',
                  'accepted_on', 'binding'}


class SourceObservation(dict):
    """Live adapter result; decoded receipt JSON is never a source observation."""


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SyncwheelError('scenario acceptance requires ' + field)
    return value.strip()


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise SyncwheelError('scenario acceptance requires exact revision SHAs')
    return value


def _positive_integer(value, field):
    if type(value) is not int or value <= 0:
        raise SyncwheelError('scenario acceptance requires positive integer ' + field)
    return value


def normalize_binding(value):
    """Retain exact revision identities and ordered composition; never resolve refs."""
    if not isinstance(value, dict):
        raise SyncwheelError('acceptance binding must be an object')
    if set(value) == {'revisions'}:
        revisions = value['revisions']
        if not isinstance(revisions, list) or not revisions:
            raise SyncwheelError('acceptance binding requires non-empty revisions')
        normalized = [_sha(item) for item in revisions]
        if len(set(normalized)) != len(normalized):
            raise SyncwheelError('acceptance revisions must be unique')
        return {'revisions': normalized}
    if set(value) != {'composition'} or not isinstance(value['composition'], dict):
        raise SyncwheelError('acceptance requires revisions or an exact ordered composition')
    composition = value['composition']
    if set(composition) != {'base_sha', 'head_sha', 'stacks'} or not isinstance(composition['stacks'], list):
        raise SyncwheelError('acceptance composition requires base_sha, head_sha and ordered stacks')
    stacks, ids = [], set()
    for item in composition['stacks']:
        if not isinstance(item, dict) or set(item) != {'id', 'head_sha'}:
            raise SyncwheelError('acceptance composition requires exact stack id and head_sha')
        name = _text(item['id'], 'stack id')
        if name in ids:
            raise SyncwheelError('acceptance composition contains duplicate stack ids')
        ids.add(name)
        stacks.append({'id': name, 'head_sha': _sha(item['head_sha'])})
    return {'composition': {'base_sha': _sha(composition['base_sha']),
                            'head_sha': _sha(composition['head_sha']), 'stacks': stacks}}


def normalize_source(value):
    if isinstance(value, dict) and set(value) == {'kind', 'reference'} and value['kind'] == 'provenance':
        return {'kind': 'provenance', 'reference': _text(value['reference'], 'source reference')}
    if not isinstance(value, dict) or set(value) != {'kind', 'repository', 'pull_request', 'review_id'}:
        raise SyncwheelError('acceptance source requires an exact GitHub review locator')
    repository = value['repository']
    if value['kind'] != 'github_review' or not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise SyncwheelError('unsupported acceptance source or repository')
    return {'kind': 'github_review', 'repository': repository,
            'pull_request': _positive_integer(value['pull_request'], 'pull_request'),
            'review_id': _positive_integer(value['review_id'], 'review_id')}


def normalize_receipt(value):
    if not isinstance(value, dict) or set(value) != PAYLOAD_FIELDS | {'source'}:
        raise SyncwheelError('invalid scenario acceptance receipt fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != 'scenario_acceptance':
        raise SyncwheelError('unsupported scenario acceptance receipt schema')
    result = copy.deepcopy(value)
    for field in ('scenario_id', 'scenario', 'person'):
        result[field] = _text(value[field], field)
    try:
        if not isinstance(value['accepted_on'], str) or date.fromisoformat(value['accepted_on']).isoformat() != value['accepted_on']:
            raise ValueError('non-canonical date')
    except (ValueError, TypeError) as exc:
        raise SyncwheelError('acceptance requires accepted_on as YYYY-MM-DD') from exc
    result['binding'] = normalize_binding(value['binding'])
    result['source'] = normalize_source(value['source'])
    return result


def receipt_id(receipt):
    payload = json.dumps(normalize_receipt(receipt), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def record_receipts(profile, receipts, *, actor):
    """Cache portable provenance only; source verification is never persisted as approval."""
    actor = _text(actor, 'recording actor')
    proposed = copy.deepcopy(profile)
    evidence = proposed.setdefault(EVIDENCE_KEY, {})
    if not isinstance(evidence, dict):
        raise SyncwheelError('acceptance_evidence must be an object')
    for raw in receipts:
        receipt = normalize_receipt(raw)
        key = receipt_id(receipt)
        existing = evidence.get(key)
        if existing is not None:
            if not isinstance(existing, dict) or existing.get('receipt') != receipt:
                raise SyncwheelError('acceptance evidence collision; retain original provenance')
        else:
            evidence[key] = {'receipt': receipt, 'recorded_by': actor}
    return proposed


def observe_github_signoff(source, *, trusted_human_ids, read_json):
    """Read a shared review through the fixed forge adapter's JSON port.

    trusted_human_ids comes from maintainer-controlled policy, never from the
    receipt. An indistinguishable human/operator identity remains unverified.
    The adapter port accepts REST paths and returns decoded JSON (not input flags).
    """
    source = normalize_source(source)
    if source['kind'] != 'github_review':
        raise SyncwheelError('GitHub verification requires a GitHub review source')
    if not isinstance(trusted_human_ids, (set, frozenset, list, tuple)) or any(
            type(item) is not int or item <= 0 for item in trusted_human_ids):
        raise SyncwheelError('trusted human IDs must come from explicit source-owner policy')
    repo, number, review_id = source['repository'], source['pull_request'], source['review_id']
    review = read_json(f'repos/{repo}/pulls/{number}/reviews/{review_id}')
    principal = read_json('user')
    if not isinstance(review, dict) or review.get('id') != review_id:
        raise SyncwheelError('acceptance review identity mismatch')
    expected_pr = f'https://api.github.com/repos/{repo}/pulls/{number}'
    if review.get('pull_request_url') != expected_pr:
        raise SyncwheelError('acceptance review belongs to a different repository or pull request')
    user = review.get('user')
    if not isinstance(user, dict) or type(user.get('id')) is not int:
        raise SyncwheelError('acceptance source has no stable author identity')
    login = _text(user.get('login'), 'source author login')
    try:
        payload = json.loads(review.get('body', ''))
    except (ValueError, TypeError) as exc:
        raise SyncwheelError('acceptance review body must contain the scenario JSON payload') from exc
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_FIELDS:
        raise SyncwheelError('acceptance review body has invalid scenario payload fields')
    receipt = normalize_receipt(dict(payload, source=source))
    if receipt['person'].casefold() != login.casefold():
        raise SyncwheelError('acceptance person does not match the observed source author')
    observed_sha = _sha(review.get('commit_id'))
    binding = receipt['binding']
    # A review attaches to one exact commit. A composition also binds its
    # resulting head; its declared ordered inputs are part of the human payload.
    if 'revisions' in binding:
        attached = observed_sha in binding['revisions']
    else:
        composition = binding['composition']
        attached = observed_sha == composition['head_sha']
    if not attached:
        raise SyncwheelError('acceptance review commit is outside its declared exact binding')
    submitted = review.get('submitted_at')
    try:
        stamp = datetime.fromisoformat(submitted.replace('Z', '+00:00'))
        if stamp.tzinfo is None or stamp.date().isoformat() != receipt['accepted_on']:
            raise ValueError('date mismatch')
    except (ValueError, TypeError, AttributeError) as exc:
        raise SyncwheelError('acceptance date does not match its live source') from exc
    permission = read_json(f'repos/{repo}/collaborators/{login}/permission')
    reasons = []
    if review.get('state') != 'APPROVED':
        reasons.append('source_not_approved')
    if user.get('type') != 'User' or user['id'] not in trusted_human_ids:
        reasons.append('source_author_not_trusted_human')
    if not isinstance(principal, dict) or type(principal.get('id')) is not int:
        reasons.append('operator_identity_unverified')
    elif principal['id'] == user['id'] or str(principal.get('login', '')).casefold() == login.casefold():
        reasons.append('source_author_indistinguishable_from_operator')
    if (not isinstance(permission, dict) or not isinstance(permission.get('permission'), str)
            or permission.get('permission') not in {'admin', 'maintain', 'write'}):
        reasons.append('source_author_not_authorized')
    elif not isinstance(permission.get('user'), dict) or permission['user'].get('id') != user['id']:
        reasons.append('source_author_permission_identity_mismatch')
    return SourceObservation({'receipt': receipt, 'receipt_id': receipt_id(receipt),
            'source_verification': 'verified' if not reasons else 'unverified',
            'reasons': reasons, 'author_id': user['id'], 'review_commit_sha': observed_sha,
            'notice': 'source is re-observed; a cached receipt or agent assessment grants no approval'})


def provenance_observation(receipt):
    """Expose a recorded reference without inventing a trusted source owner."""
    receipt = normalize_receipt(receipt)
    return SourceObservation({'receipt': receipt, 'receipt_id': receipt_id(receipt),
                              'source_verification': 'unverified',
                              'reasons': ['source_owner_verification_unavailable']})


def normalize_assessment(value, *, actor):
    fields = {'schema_version', 'kind', 'acceptance_id', 'candidate', 'impact',
              'rationale', 'assessed_at', 'reference'}
    if not isinstance(value, dict) or set(value) != fields or type(value.get('schema_version')) is not int or value['schema_version'] != 1 or value['kind'] != 'scenario_assessment':
        raise SyncwheelError('invalid scenario assessment schema or fields')
    if not isinstance(value['acceptance_id'], str) or not re.fullmatch('[0-9a-f]{64}', value['acceptance_id']):
        raise SyncwheelError('assessment requires exact acceptance_id')
    if not isinstance(value['impact'], str) or value['impact'] not in {'unchanged', 'changed', 'uncertain'}:
        raise SyncwheelError('assessment impact must be unchanged, changed or uncertain')
    result = copy.deepcopy(value)
    result['candidate'] = normalize_binding(value['candidate'])
    for field in ('rationale', 'reference'):
        result[field] = _text(value[field], field)
    try:
        stamp = datetime.fromisoformat(value['assessed_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('missing timezone')
    except (ValueError, TypeError, AttributeError) as exc:
        raise SyncwheelError('assessment requires an ISO timestamp with timezone') from exc
    result['assessed_by'] = _text(actor, 'assessing actor')
    return result


def applicability(receipt, candidate, *, actor, assessment=None, observe_source):
    """Assess a candidate against a fresh source observation, without writing state."""
    receipt = normalize_receipt(receipt)
    candidate = normalize_binding(candidate)
    observed = observe_source(receipt['source'])
    if not isinstance(observed, SourceObservation) or observed.get('receipt') != receipt or observed.get('receipt_id') != receipt_id(receipt):
        raise SyncwheelError('live acceptance source changed; re-read its current decision')
    exact = candidate == receipt['binding']
    selected = normalize_assessment(assessment, actor=actor) if assessment is not None else None
    if selected is not None and (selected['acceptance_id'] != receipt_id(receipt) or selected['candidate'] != candidate):
        raise SyncwheelError('assessment belongs to a different acceptance or candidate')
    renewal = selected['impact'] != 'unchanged' if selected else not exact
    source_verified = observed.get('source_verification') == 'verified'
    return {'acceptance_id': receipt_id(receipt), 'candidate': candidate,
            'source_verification': observed.get('source_verification', 'unverified'),
            'source_reasons': observed.get('reasons', []), 'exact_binding': exact,
            'assessment': selected, 'renewal_required': renewal,
            'status': ('source_unverified' if not source_verified else
                       'renewal_required' if renewal else 'applicable'),
            'notice': 'agent delta assessment is separate from the human source decision'}


def trusted_ids(settings, repository):
    """Only the maintainer's user policy supplies trust, never candidate/profile data."""
    policy = settings.get('acceptance_trust', {})
    if not isinstance(policy, dict) or set(policy) - {'github'}:
        raise SyncwheelError('invalid maintainer acceptance_trust policy')
    github = policy.get('github', {})
    if not isinstance(github, dict):
        raise SyncwheelError('acceptance_trust.github must map repositories to human IDs')
    ids = github.get(repository, [])
    if not isinstance(ids, list) or any(type(item) is not int or item <= 0 for item in ids):
        raise SyncwheelError('maintainer trusted human IDs must be positive integers')
    return ids


def _read_input(path):
    from pathlib import Path
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise SyncwheelError('cannot read acceptance input: ' + str(exc)) from exc


def run_command(args, *, profile, write_profile, settings, actor, current_head,
                require_revision, read_source, delta_paths, repository=None, requirements=None):
    """Public command service; I/O, actor and private profile CAS remain facade ports."""
    requirements = normalize_requirements(requirements)
    if args.acceptance_action == 'record':
        receipts = [_read_input(name) for name in args.receipt]
        proposed = record_receipts(profile, receipts, actor=actor)
        result = {'applied': False, 'proposed': proposed,
                  'notice': 'provenance recorded; this operation verifies no human approval'}
        if args.apply:
            write_profile(proposed)
            result['applied'] = True
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    live = None
    if args.source:
        source = normalize_source(_read_input(args.source))
        if source['kind'] != 'github_review':
            raise SyncwheelError('source-only recovery requires a supported shared source owner')
        if not repository or source['repository'].casefold() != repository.casefold():
            raise SyncwheelError('acceptance source repository does not match the canonical remote')
        live = read_source(source, trusted_ids(settings, source['repository']))
        receipt = live['receipt']
    elif args.receipt:
        receipt = normalize_receipt(_read_input(args.receipt))
    else:
        entries = profile.get(EVIDENCE_KEY, {})
        record = entries.get(args.acceptance_id) if isinstance(entries, dict) else None
        if not isinstance(record, dict):
            raise SyncwheelError('acceptance receipt not found; recover from its shared source locator')
        receipt = normalize_receipt(record.get('receipt'))
        if receipt_id(receipt) != args.acceptance_id:
            raise SyncwheelError('cached acceptance receipt identity mismatch')

    def observe(source):
        if live is not None:
            return live
        if source['kind'] == 'github_review':
            if not repository or source['repository'].casefold() != repository.casefold():
                return provenance_observation(receipt)
            return read_source(source, trusted_ids(settings, source['repository']))
        return provenance_observation(receipt)

    actual_head = _sha(current_head())
    candidate = normalize_binding(_read_input(args.candidate)) if args.candidate else {
        'revisions': [_sha(args.head or actual_head)]}
    heads = candidate.get('revisions', [candidate.get('composition', {}).get('head_sha')])
    if actual_head not in heads:
        raise SyncwheelError('acceptance candidate does not match the live checkout or selected stack head')
    for binding in (receipt['binding'], candidate):
        revisions = (binding['revisions'] if 'revisions' in binding else
                     [binding['composition']['base_sha'], binding['composition']['head_sha'],
                      *(s['head_sha'] for s in binding['composition']['stacks'])])
        for revision in revisions:
            require_revision(revision)
    delta = candidate_delta(receipt, candidate, delta_paths)
    selected = None
    selected_actor = actor
    key = assessment_key(receipt, candidate)
    if args.acceptance_action == 'assess':
        from datetime import timezone
        selected = {'schema_version': 1, 'kind': 'scenario_assessment',
                    'acceptance_id': receipt_id(receipt), 'candidate': candidate,
                    'impact': args.impact, 'rationale': args.rationale,
                    'assessed_at': datetime.now(timezone.utc).isoformat(), 'reference': args.reference}
    elif args.assessment:
        selected = _read_input(args.assessment)
    else:
        stored = cached_assessment(profile, receipt, candidate, delta)
        if stored:
            selected, selected_actor = stored['assessment'], stored['actor']
    def observe_bound(source):
        return source_context_observation(receipt, observe(source), repository=repository,
                                          requirements=requirements, scenario=args.scenario)
    result = applicability(receipt, candidate, actor=selected_actor, assessment=selected,
                           observe_source=observe_bound)
    result.update(receipt=receipt, delta=delta, applied=False)
    if args.acceptance_action == 'assess' and args.apply:
        proposed = record_receipts(profile, [receipt], actor=actor)
        history = proposed.setdefault('acceptance_assessments', {}).setdefault(key, [])
        if not isinstance(history, list):
            raise SyncwheelError('invalid cached acceptance assessment history')
        history.append({'assessment': selected, 'actor': actor, 'delta': delta})
        write_profile(proposed)
        result['applied'] = True
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['status'] == 'applicable' else 1


def repository_from_remote(url):
    """Derive GitHub namespace from the observed canonical remote, not receipt data."""
    if not isinstance(url, str):
        return None
    match = re.fullmatch(r'(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', url.strip())
    return match.group(1) if match else None


def normalize_requirements(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'repository', 'scenarios'}:
        raise SyncwheelError('canonical scenario_acceptance requires repository and scenarios')
    repository = value['repository']
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise SyncwheelError('invalid canonical scenario acceptance repository')
    if not isinstance(value['scenarios'], list) or not value['scenarios']:
        raise SyncwheelError('canonical scenario acceptance requires non-empty scenarios')
    scenarios, seen = [], set()
    for item in value['scenarios']:
        if not isinstance(item, dict) or set(item) != {'id', 'source'}:
            raise SyncwheelError('canonical acceptance scenario requires id and source')
        scenario = _text(item['id'], 'required scenario id')
        source = normalize_source(item['source'])
        if scenario in seen or source['kind'] != 'github_review' or source['repository'].casefold() != repository.casefold():
            raise SyncwheelError('invalid or duplicate canonical acceptance source/scenario')
        seen.add(scenario)
        scenarios.append({'id': scenario, 'source': source})
    return {'repository': repository, 'scenarios': scenarios}


def candidate_delta(receipt, candidate, delta_paths):
    accepted = receipt['binding']
    before = accepted['revisions'][0] if 'revisions' in accepted and len(accepted['revisions']) == 1 else accepted.get('composition', {}).get('head_sha')
    after = candidate['revisions'][0] if 'revisions' in candidate and len(candidate['revisions']) == 1 else candidate.get('composition', {}).get('head_sha')
    return {'accepted_binding': accepted, 'candidate_binding': candidate,
            'paths': delta_paths(before, after) if before and after else None}


def assessment_key(receipt, candidate):
    return hashlib.sha256(json.dumps({'acceptance_id': receipt_id(receipt), 'candidate': candidate},
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def cached_assessment(profile, receipt, candidate, delta):
    assessments = profile.get('acceptance_assessments', {})
    if not isinstance(assessments, dict):
        raise SyncwheelError('acceptance_assessments must be an object')
    history = assessments.get(assessment_key(receipt, candidate))
    if history is None:
        return None
    if not isinstance(history, list) or not history:
        raise SyncwheelError('invalid cached acceptance assessment history')
    stored = history[-1]
    if not isinstance(stored, dict) or set(stored) != {'assessment', 'actor', 'delta'} or stored['delta'] != delta:
        raise SyncwheelError('cached candidate delta changed; assess it again')
    return stored


def source_context_observation(receipt, observed, *, repository, requirements=None, scenario=None):
    reasons = []
    source = receipt['source']
    if source['kind'] == 'github_review' and (not repository or source['repository'].casefold() != repository.casefold()):
        reasons.append('repository_identity_mismatch')
    if scenario is not None and receipt['scenario_id'] != scenario:
        reasons.append('scenario_identity_mismatch')
    if requirements:
        required = next((s for s in requirements['scenarios'] if s['id'] == receipt['scenario_id']), None)
        if required is None or required['source'] != source:
            reasons.append('canonical_scenario_source_mismatch')
    if reasons:
        return SourceObservation(dict(observed, source_verification='unverified',
                                      reasons=list(observed.get('reasons', [])) + reasons))
    return observed


def merge_gates(requirements, profile, *, repository, head, actor, observe_source, delta_paths):
    """Only a canonical declaration activates this gate; no caller receipt selects it."""
    requirements = normalize_requirements(requirements)
    if requirements is None:
        return None
    result = {'requirements': requirements, 'candidate': {'revisions': [_sha(head)]},
              'scenarios': [], 'blockers': []}
    if not repository or requirements['repository'].casefold() != repository.casefold():
        result['blockers'].append({'code': 'acceptance_repository_mismatch'})
        return result
    for required in requirements['scenarios']:
        try:
            observed = observe_source(required['source'])
            receipt = normalize_receipt(observed['receipt'])
            observed = source_context_observation(receipt, observed, repository=repository,
                                                  requirements=requirements, scenario=required['id'])
            candidate = result['candidate']
            delta = candidate_delta(receipt, candidate, delta_paths)
            stored = cached_assessment(profile, receipt, candidate, delta)
            status = applicability(receipt, candidate, actor=stored['actor'] if stored else actor,
                                   assessment=stored['assessment'] if stored else None,
                                   observe_source=lambda source: observed)
            status['delta'] = delta
            result['scenarios'].append(status)
            if status['status'] != 'applicable':
                result['blockers'].append({'code': 'scenario_acceptance_' + status['status'], 'scenario': required['id']})
        except SyncwheelError as exc:
            result['blockers'].append({'code': 'scenario_acceptance_unavailable',
                                       'scenario': required['id'], 'detail': str(exc)})
    return result
